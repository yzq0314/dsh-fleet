import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

import pytest

from fleet_hermes import feishu_cards as cards


def test_card_protocol_restart_replay_and_resolution(tmp_path, monkeypatch):
    calls, ids = [], {}

    class Stub(BaseHTTPRequestHandler):
        def call(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(
                (self.command, self.path, data, self.headers.get("Authorization"))
            )
            code = 200
            if self.path == "/token":
                assert data["grant_type"] == "client_credentials"
                body = {"code": 0, "access_token": "test-only", "expires_in": 600}
            elif self.command == "POST":
                first = data["uuid"] not in ids
                message = ids.setdefault(data["uuid"], "om_mock")
                # Simulate create succeeding at Feishu but the acknowledgement failing.
                code = 503 if first else 200
                body = {"code": 0, "data": {"message_id": message}}
            else:
                body = {"code": 0, "data": {}}
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_POST = do_PATCH = call

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(cards, "TOKEN_URL", origin + "/token")
    monkeypatch.setattr(cards, "MESSAGES", origin + "/messages")
    settings = {
        "FLEET_FEISHU_APP_ID": "app-demo",
        "FLEET_FEISHU_APP_SECRET": "synthetic",
        "FLEET_FEISHU_CHAT_ID": "chat-test",
        "FLEET_CONSOLE_URL": "https://hub.example.invalid",
    }
    event_file = tmp_path / "events.jsonl"
    state = tmp_path / "bridge.sqlite"
    pending = {
        "id": 10,
        "node_id": "node-a",
        "task_id": "task-a",
        "type": "approval_required",
        "data": {
            "id": "approval-a",
            "command": "read sample.txt",
            "workspace": "demo",
            "paths": ["sample.txt"],
            "reason": "test reason",
        },
    }
    resolved = dict(
        pending,
        id=12,
        type="approval_resolved",
        data={"id": "approval-a", "decision": "deny", "cause": "timeout"},
    )

    def line(event):
        return json.dumps(event).encode() + b"\n"

    bridge = cards.Bridge(state, cards.Feishu(settings))
    try:
        event_file.write_bytes(line(pending)[:-1])
        bridge.drain(event_file)
        assert calls == []
        event_file.write_bytes(line(pending))
        with pytest.raises(HTTPError):
            bridge.drain(event_file)
        bridge.db.close()
        bridge = cards.Bridge(state, cards.Feishu(settings))
        bridge.drain(event_file)
        with event_file.open("ab") as f:
            f.write(line(pending) + line(resolved) + line(resolved) + line(pending))
        bridge.drain(event_file)
        assert len(ids) == 1
        outgoing = [call for call in calls if call[1] != "/token"]
        assert [call[:2] for call in outgoing] == [
            ("POST", "/messages?receive_id_type=chat_id"),
            ("POST", "/messages?receive_id_type=chat_id"),
            ("PATCH", "/messages/om_mock"),
        ]
        for method, _, data, auth in outgoing:
            assert auth == "Bearer test-only"
            content = json.loads(data["content"])
            assert content["config"]["update_multi"] is True
            assert "read sample.txt" in content["elements"][0]["text"]["content"]
            assert (
                content["elements"][1]["actions"][0]["url"]
                == settings["FLEET_CONSOLE_URL"]
            )
            if method == "POST":
                assert (
                    data["receive_id"] == "chat-test"
                    and data["msg_type"] == "interactive"
                )
            else:
                assert "超时" in content["header"]["title"]["content"]
        # A rotated/replayed journal, even resolving another ID ahead of its
        # pending notification, must never restore a stale card.
        rotated = tmp_path / "rotated.jsonl"
        rotated.write_bytes(
            line(resolved)
            + line(pending)
            + line(
                dict(
                    resolved,
                    id=22,
                    data={"id": "second", "cause": "cancelled", "decision": "closed"},
                )
            )
            + line(dict(pending, id=21, data=dict(pending["data"], id="second")))
        )
        count = len(calls)
        bridge.drain(rotated)
        assert len(calls) == count
    finally:
        bridge.db.close()
        server.shutdown()
        thread.join()
        server.server_close()


def test_missing_or_untrusted_config_and_large_cards():
    with pytest.raises(ValueError, match="private settings"):
        cards.Feishu({})
    with pytest.raises(ValueError, match="HTTPS"):
        cards.Feishu(
            dict(
                FLEET_FEISHU_APP_ID="a",
                FLEET_FEISHU_APP_SECRET="b",
                FLEET_FEISHU_CHAT_ID="c",
                FLEET_CONSOLE_URL="http://hub.example.invalid",
            )
        )
    card = cards.card_for(
        "id", {"command": "字" * 30000}, "pending", "https://hub.example.invalid"
    )
    assert "大小限制" in card["elements"][0]["text"]["content"]
    assert (
        "失败"
        in cards.card_for("id", {}, "failed", "https://hub.example.invalid")["header"][
            "title"
        ]["content"]
    )
