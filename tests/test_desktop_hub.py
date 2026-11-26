import hashlib
import hmac
import secrets
import time

from fastapi.testclient import TestClient

from fleet_hub.app import create_app


def test_desktop_namespace_idempotence_and_approval_ownership(tmp_path):
    keys = {n: secrets.token_hex(26) for n in ("alpha", "beta")}
    api = secrets.token_urlsafe(30)
    config = {
        "database": str(tmp_path / "hub.sqlite"),
        "api_key": api,
        "password": secrets.token_urlsafe(16),
        "nodes": [
            {"id": n, "alias": n, "url": "http://127.0.0.1:1", "secret": v}
            for n, v in keys.items()
        ],
        "allowed_cidrs": ["127.0.0.0/8"],
        "poll_interval": 10,
        "desktop_clients": {"alpha": {"targets": ["beta"]}},
    }

    def auth(value):
        return {"Authorization": "Bearer " + value}

    def key(n):
        return hmac.new(
            keys[n].encode(), b"dsh-fleet/desktop/v1", hashlib.sha256
        ).hexdigest()

    base = "/api/desktop/alpha"
    with TestClient(create_app(config), client=("127.0.0.1", 43000)) as c:
        for value in (keys["alpha"], api, key("beta")):
            assert c.post(
                base + "/heartbeat", json={"version": "test"}, headers=auth(value)
            ).status_code in {401, 403}
        plugin = auth(key("alpha"))

        def send(eid, kind, data):
            return c.post(
                base + "/events",
                json={
                    "session_id": "bound",
                    "event_id": eid,
                    "type": kind,
                    "data": data,
                },
                headers=plugin,
            )

        assert c.get("/api/nodes", headers=plugin).status_code == 401
        assert (
            c.post(
                base + "/heartbeat", json={"version": "0.2.0"}, headers=plugin
            ).status_code
            == 200
        )
        assert c.get("/api/nodes/alpha/desktop", headers=auth(api)).json()["online"]
        assert [n["id"] for n in c.get(base + "/nodes", headers=plugin).json()] == [
            "beta"
        ]
        assert (
            c.post(
                base + "/dispatch",
                headers=plugin,
                json={"node_id": "alpha", "prompt": "inspect"},
            ).status_code
            == 403
        )
        assert c.get(base + "/files/beta/nas", headers=plugin).status_code == 403
        pending = {
            "id": "native-1",
            "reason": "test review",
            "command": "inspect",
            "expires_at": time.time() + 60,
        }
        receipt = send("event-1", "approval_required", pending).json()
        assert send("event-1", "approval_required", pending).json() == receipt
        assert send("event-1", "log", {"text": "other"}).status_code == 409
        aid = receipt["approval_id"]
        assert (
            c.post(
                "/api/approvals/" + aid,
                headers=auth(api),
                json={"decision": "approve_session_dir"},
            ).status_code
            == 422
        )
        assert (
            c.post(
                "/api/approvals/" + aid,
                headers=auth(api),
                json={"decision": "approve_once"},
            ).json()["state"]
            == "decided"
        )
        assert (
            c.post(
                "/api/approvals/" + aid, headers=auth(api), json={"decision": "deny"}
            ).status_code
            == 409
        )
        assert (
            c.get(
                "/api/desktop/beta/decisions/" + aid, headers=auth(key("beta"))
            ).status_code
            == 404
        )
        assert c.get(base + "/decisions/" + aid, headers=plugin).json() == {
            "state": "decided",
            "decision": "approve_once",
        }
        assert send(
            "event-2",
            "approval_resolved",
            {"id": "native-1", "decision": "approve_once"},
        ).is_success
        task = receipt["task_id"]
        assert (
            c.post("/api/tasks/" + task + "/cancel", headers=auth(api)).json()["status"]
            == "cancelling"
        )
        assert c.post(
            base + "/heartbeat", json={"version": "test"}, headers=plugin
        ).json()["cancel_sessions"] == ["bound"]
        assert send("event-3", "status", {"status": "cancelled"}).is_success
        events = c.get(f"/api/tasks/{task}/events", headers=auth(api)).json()
        assert [e["type"] for e in events] == [
            "approval_required",
            "approval_resolved",
            "status",
        ]
        historical = {
            "id": "offline-approval",
            "command": "inspect sample",
            "expires_at": time.time() - 180,
        }
        expired = send("offline-1", "approval_required", historical).json()
        assert (
            expired["expired"] is True
            and send("offline-1", "approval_required", historical).json() == expired
        )
        closed_id = expired["approval_id"]
        assert c.get(base + "/decisions/" + closed_id, headers=plugin).json() == {
            "state": "expired",
            "decision": "deny",
        }
        assert (
            c.post(
                "/api/approvals/" + closed_id,
                headers=auth(api),
                json={"decision": "approve_once"},
            ).status_code
            == 409
        )
        outcome = {
            "id": "offline-approval",
            "decision": "approve_once",
            "cause": "native-before-reconnect",
        }
        assert send("offline-2", "approval_resolved", outcome).is_success
        assert send("offline-2", "approval_resolved", outcome).is_success
        assert send("offline-3", "message", {"text": "following event"}).is_success
        history = c.get(f"/api/tasks/{task}/events", headers=auth(api)).json()
        assert history[-1]["data"]["text"] == "following event"
        assert (
            sum(
                e["type"] == "approval_resolved" and e["data"]["id"] == closed_id
                for e in history
            )
            == 1
        )
        assert not any(
            a["id"] == closed_id
            for a in c.get("/api/approvals", headers=auth(api)).json()
        )
        assert c.delete("/api/nodes/alpha", headers=auth(api)).is_success
        assert (
            c.post(
                base + "/heartbeat", json={"version": "test"}, headers=plugin
            ).status_code
            == 401
        )
