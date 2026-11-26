import hmac
import json
import secrets
import sqlite3
import time

from fastapi.testclient import TestClient
from fleet_hub.app import create_app


def test_conversation_delivery_filing_and_ownership(tmp_path):
    nodes = {name: secrets.token_hex(32) for name in ("first", "other")}
    dbfile = tmp_path / "hub.sqlite"
    admin_key = secrets.token_hex(32)
    admin = {"Authorization": "Bearer " + admin_key}
    cfg = {
        "database": str(dbfile),
        "password": "fixture-only",
        "api_key": admin_key,
        "allowed_cidrs": ["127.0.0.0/8"],
        "nodes": [
            {"id": name, "alias": name, "url": "http://127.0.0.1:1", "secret": secret}
            for name, secret in nodes.items()
        ],
    }

    def plugin(name):
        token = hmac.digest(
            nodes[name].encode(), b"dsh-fleet/desktop/v1", "sha256"
        ).hex()
        return {"Authorization": "Bearer " + token}

    with TestClient(create_app(cfg), client=("127.0.0.1", 1234)) as client:
        tid = "task-from-sidecar"
        with sqlite3.connect(dbfile) as db:
            db.execute(
                "INSERT INTO tasks (id,node_id,group_id,payload,status,created) VALUES (?,?,?,?,'completed',?)",
                (
                    tid,
                    "first",
                    tid,
                    json.dumps({"prompt": "hello", "agent": "dsh"}),
                    time.time(),
                ),
            )
            db.execute(
                "INSERT INTO events(task_id,type,data,created) VALUES (?,'desktop_handoff',?,?)",
                (
                    tid,
                    json.dumps({"status": "ready", "session_id": "original-session"}),
                    time.time(),
                ),
            )
        base = "/api/tasks/" + tid
        turns = "/api/desktop/first/turns"
        body = {"request_id": secrets.token_hex(16), "text": "remember the first turn"}

        def send():
            return client.post(base + "/replies", headers=admin, json=body)

        def filing(**data):
            return client.patch(base + "/filing", headers=admin, json=data)

        assert send().status_code == 409
        assert client.post(turns, headers=plugin("other")).status_code in (401, 403)
        assert client.post(turns, headers=plugin("first")).json() == []
        assert (
            client.post(
                base + "/replies", headers=plugin("first"), json=body
            ).status_code
            == 401
        )
        assert send().is_success and send().is_success
        assert filing(removed=True).status_code == 409
        (item,) = client.post(turns, headers=plugin("first")).json()
        assert (
            item["native_id"] == "original-session" and item["id"] == body["request_id"]
        )
        wrong = "/api/desktop/other/turns/" + item["id"]
        assert (
            client.post(
                wrong, headers=plugin("other"), json={"state": "accepted"}
            ).status_code
            == 404
        )
        assert client.post(
            turns + "/" + item["id"],
            headers=plugin("first"),
            json={"state": "accepted"},
        ).is_success

        def event(kind, data):
            return client.post(
                "/api/desktop/first/events",
                headers=plugin("first"),
                json={
                    "event_id": secrets.token_hex(8),
                    "session_id": "original-session",
                    "type": kind,
                    "data": data,
                },
            )

        assert event("status", {"status": "running"}).json()["task_id"] == tid
        assert filing(removed=True).status_code == 409
        assert event("user_message", {"text": body["text"]}).is_success
        assert event("result", {"text": "second reply"}).is_success
        assert event("status", {"status": "completed"}).is_success
        assert filing(folder="Research").is_success
        overview = client.get("/api/overview", headers=admin).json()
        (task,) = overview["tasks"]
        assert (
            task["id"] == tid
            and task["result"] == "second reply"
            and task["folder"] == "Research"
        )
        assert filing(removed=True).is_success
        assert not client.get("/api/tasks", headers=admin).json()
        assert (
            client.get("/api/tasks?removed=true", headers=admin).json()[0]["id"] == tid
        )
        assert filing(removed=False).is_success
        body["request_id"] = secrets.token_hex(16)
        assert send().is_success
        with sqlite3.connect(dbfile) as db:
            db.execute(
                "UPDATE chat_turns SET created=0 WHERE id=?", (body["request_id"],)
            )
        assert client.post(turns, headers=plugin("first")).json() == []
        assert send().json()["state"] == "expired"
