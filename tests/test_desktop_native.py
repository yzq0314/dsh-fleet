import asyncio
import hmac
import json
import os
import secrets
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import httpx
import yaml

from fleet_node.agents import DshAdapter, dsh_command
from fleet_node.processes import spawn_settings, stop_process
from packages.protocol.adapters import TaskRequest


def test_model_bridge_diagnostic_transport():
    node = shutil.which("node")
    if not node:
        pytest.skip("Requires Node for Desktop model bridge")
    suite = Path(__file__).with_name("model_diagnostics.mjs")
    subprocess.run([node, "--test", str(suite)], check=True, timeout=30)


async def test_installed_desktop_runtime_plugin_and_model_bridge(tmp_path, monkeypatch):
    exe = Path(
        os.environ.get("FLEET_TEST_DESKTOP_EXECUTABLE")
        or Path(os.environ.get("LOCALAPPDATA", ""))
        / "Programs/DeepSeek Harness/DeepSeek Harness.exe"
    )
    if not exe.is_file() or not shutil.which("dsh"):
        pytest.skip("Requires installed DSH Desktop Electron and DSH SDK")
    home = tmp_path / "dsh-home"
    profile = home / "profiles/desktop"
    profile.mkdir(parents=True)
    (profile / "package.json").write_text('{"dsh":{"profile":{"bundles":[]}}}')
    events, model_requests, paths = [], [], []
    faults = {"offline": False}
    turns, receipts = [], {}
    fixture_key = secrets.token_urlsafe(32)
    marker = secrets.token_hex(12)
    (tmp_path / "fixture-input.txt").write_text(marker, encoding="utf8")

    class Local(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, value):
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            paths.append(self.path)
            if "/decisions/" in self.path:
                return self.respond({"state": "decided", "decision": "approve_once"})
            self.respond([])

        def do_POST(self):
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
            )
            paths.append(self.path)
            if "/api/desktop/" in self.path:
                if self.path.endswith("/turns"):
                    return self.respond([row for row in turns if row["id"] not in receipts])
                if "/turns/" in self.path:
                    receipts[self.path.rsplit("/", 1)[1]] = body["state"]
                    return self.respond({"ok": True})
                if self.path.endswith("/events"):
                    if faults["offline"]:
                        self.send_error(503)
                        return
                    if (
                        body.get("type") == "message"
                        and body.get("data", {}).get("text") == "reject-fixture"
                    ):
                        self.send_error(422)
                        return
                    if (
                        len(
                            json.dumps(
                                body, ensure_ascii=False, separators=(",", ":")
                            ).encode()
                        )
                        > 64 * 1024
                    ):
                        self.send_error(413)
                        return
                    events.append(body)
                return self.respond(
                    {
                        "approval_id": "hub-" + str(body.get("data", {}).get("id", "")),
                        "cancel_sessions": [],
                        "conversation_protocol": 1,
                    }
                )
            model_requests.append((body, self.headers.get("x-api-key") == fixture_key))
            sequence = [
                (
                    "message_start",
                    {
                        "message": {
                            "id": "fixture",
                            "role": "assistant",
                            "type": "message",
                            "model": body["model"],
                            "content": [],
                            "usage": {"input_tokens": 1, "output_tokens": 0},
                            "stop_reason": None,
                            "stop_sequence": None,
                        }
                    },
                ),
                (
                    "content_block_start",
                    {"index": 0, "content_block": {"type": "text", "text": ""}},
                ),
                (
                    "content_block_delta",
                    {
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "desktop local answer"},
                    },
                ),
                ("content_block_stop", {"index": 0}),
                (
                    "message_delta",
                    {
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 3},
                    },
                ),
                ("message_stop", {}),
            ]
            if len(model_requests) == 1:
                sequence[1:3] = [
                    ("content_block_start", {"index": 0, "content_block": {
                        "type": "tool_use", "id": "read-test", "name": "read", "input": {}}}),
                    ("content_block_delta", {"index": 0, "delta": {
                        "type": "input_json_delta", "partial_json": '{"file_path":"fixture-input.txt"}'}}),
                ]
                sequence[-2][1]["delta"]["stop_reason"] = "tool_use"
            raw = "".join(
                "event: "
                + kind
                + "\ndata: "
                + json.dumps({"type": kind, **data})
                + "\n\n"
                for kind, data in sequence
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Local)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = None
    address = f"http://127.0.0.1:{server.server_port}"
    try:
        cfg = tmp_path / "node.yaml"
        node_key = secrets.token_urlsafe(32)
        cfg.write_text(
            yaml.safe_dump(
                {
                    "node_id": "fixture",
                    "secret": node_key,
                    "hub_url": address,
                }
            )
        )
        repo = Path(__file__).resolve().parents[1]
        import runpy

        manager = runpy.run_path(str(repo / "integrations/dsh-app-plugin/manage.py"))
        manager["manage"]("install", home, cfg)
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"SYSTEMROOT", "PATH", "WINDIR", "TEMP", "TMP", "COMSPEC"}
        }
        env.update(
            ELECTRON_RUN_AS_NODE="1",
            HOME=str(tmp_path),
            USERPROFILE=str(tmp_path),
            DSH_HOME=str(home),
            DEEPSEEK_API_KEY=fixture_key,
            DEEPSEEK_BASE_URL=address,
        )
        modules = exe.parent / "resources/app.asar/dsh/node_modules/@deepseek-ai"

        async def start_host():
            return await asyncio.create_subprocess_exec(
                str(exe),
                str(repo / "tests/desktop_host.mjs"),
                str(modules),
                str(profile / "node_modules/@dsh-fleet/desktop"),
                cwd=tmp_path,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **spawn_settings(),
            )

        host = await start_host()

        async def until(marker):
            log = []
            async with asyncio.timeout(25):
                while line := await host.stdout.readline():
                    line = line.decode(errors="replace").strip()
                    if line == marker:
                        return
                    log.append(line.replace(fixture_key, "[fixture]"))
            raise AssertionError("\n".join(log[-12:]))

        await until("FLEET_FIXTURE_READY")
        outputs = []
        selection = {}
        if entry := os.environ.get("FLEET_TEST_SDK_ENTRY"):
            selection["dsh_cli"] = {"kind": "npm", "executable": shutil.which("node"), "entry": entry}

        async with asyncio.timeout(40):
            await DshAdapter(
                {"dsh_home": str(home), "dsh_provider": {"type": "desktop"}, "secret": node_key, **selection}
            ).run(
                TaskRequest(
                    id="broker",
                    agent="dsh",
                    prompt="local integration",
                    workspace=str(tmp_path),
                ),
                lambda t, d: outputs.append((t, d)),
                lambda command, paths, risk: asyncio.sleep(
                    0, result=not risk and all(Path(p) == tmp_path / "fixture-input.txt" for p in paths)
                ),
            )
        assert ("result", {"text": "desktop local answer"}) in outputs
        assert len(model_requests) == 2 and all(ok for _, ok in model_requests)
        handoff, = [data for kind, data in outputs if kind == "desktop_handoff"]
        assert handoff["status"] == "ready", handoff
        assert handoff["workspace"] == str(tmp_path)
        host.stdin.write(b"exercise\n")
        await host.stdin.drain()
        await until("FLEET_FIXTURE_SYNCED")
        async with asyncio.timeout(5):
            while not any(e["data"].get("status") == "completed" for e in events):
                await asyncio.sleep(0.05)
        assert {e["session_id"] for e in events} == {"selected"}
        assert [
            e["data"]["decision"] for e in events if e["type"] == "approval_resolved"
        ] == ["approve_once", "deny"]
        assert any("/files/target/share" in path for path in paths)
        assert any(
            e["data"].get("truncated") and "已截断" in e["data"].get("text", "")
            for e in events
        )
        assert not any(
            e["type"] == "approval_required"
            and e["data"]["id"] in {"large", "private-args"}
            for e in events
        )
        assert "fixture-censored-value" not in json.dumps(events)
        assert any(
            e["data"].get("command") == 'read {"path":"file"}'
            and e["data"].get("workspace") == str(tmp_path)
            for e in events
            if e["type"] == "approval_required"
        )
        statefile = next((home / "fleet-desktop").glob("sync-state-*.json"))
        valid, foreign = secrets.token_hex(16), secrets.token_hex(16)
        turns.extend([
            dict(id=valid, task_id="broker", native_id=handoff["session_id"], prompt="continue in this workspace"),
            dict(id=foreign, task_id="broker", native_id="unshared-session", prompt="must not be sent"),
        ])
        async with asyncio.timeout(18):
            while len(receipts) < 2:
                await asyncio.sleep(.1)
        assert receipts == {valid: "accepted", foreign: "rejected"}
        assert any(
            e["status"] == 422
            for e in json.loads(statefile.read_text(encoding="utf-8"))["quarantined"]
        )

        async def queue_offline(n):
            faults["offline"] = True
            host.stdin.write(f"probe {n}\n".encode())
            await host.stdin.drain()
            await until("FLEET_FIXTURE_PROBED")
            async with asyncio.timeout(5):
                while not any(
                    e["data"].get("text") == f"durable-probe-{n}"
                    for e in json.loads(statefile.read_text(encoding="utf-8"))["outbox"]
                ):
                    await asyncio.sleep(0.05)

        async def shutdown():
            host.stdin.write(b"quit\n")
            await host.stdin.drain()
            assert await asyncio.wait_for(host.wait(), 8) == 0
            assert not (home / "fleet-desktop/bridge.json").exists()

        await queue_offline(41)
        await shutdown()
        manager["manage"]("install", home, cfg)
        faults["offline"] = False
        host = await start_host()
        await until("FLEET_FIXTURE_READY")
        host.stdin.write(("continue " + handoff["session_id"] + "\n").encode())
        await host.stdin.drain()
        await until("FLEET_FIXTURE_OPENED")
        # Exercise the shipped Agent registry's resume path on this durable
        # history. A synthetic provider receives the original tool result and
        # the new question together; none of the historical tools run again.
        route_patch = tmp_path / "resume-fixture.yaml"
        route_patch.write_text(yaml.safe_dump([
            {"id": "llm-deepseek", "config": {"baseURL": address, "apiKeyEnv": "DEEPSEEK_API_KEY"}},
            {"id": "persistent-bash", "disabled": True}, {"id": "persistent-pwsh", "disabled": True},
        ]), encoding="utf8")
        child_env = dict(env)
        process = await asyncio.create_subprocess_exec(
            *dsh_command(child_env, selection.get("dsh_cli")), "--profile", "sdk-minimal", "--patch", str(route_patch),
            env=child_env, cwd=tmp_path, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=4*1024*1024, **spawn_settings(),
        )
        observed = []

        async def rpc(number, method, args):
            process.stdin.write(json.dumps({"jsonrpc":"2.0","id":number,"method":method,"params":args}).encode()+b"\n")
            await process.stdin.drain()
            while line := await process.stdout.readline():
                packet = json.loads(line)
                if packet.get("id") == number:
                    assert "error" not in packet, packet
                    return packet["result"]
                if packet.get("method") == "session.event":
                    observed.append(packet["params"]["event"])
            pytest.fail("Fixture SDK closed before " + method)

        try:
            async with asyncio.timeout(35):
                await rpc(1,"initialize",{"cwd":str(tmp_path),"provider":"deepseek-official","model":"deepseek-v4-pro"})
                session = {"sessionId": handoff["session_id"]}
                await rpc(2,"session/resume",session)
                await rpc(3,"session/prompt",{**session,"contentBlocks":[{"type":"text","text":"Continue this test."}]})
                await rpc(4,"session/wait",session)
                await rpc(5,"shutdown",{})
                await process.wait()
        finally:
            await stop_process(process)
        assert not any(e["type"] == "tool/call" for e in observed)
        assert any(e["type"] == "turn/end" and e["data"]["reason"]["kind"] == "completed" for e in observed)
        assert len(model_requests) == 3
        assert marker in json.dumps(model_requests[2][0]["messages"])
        assert "Continue this test." in json.dumps(model_requests[2][0]["messages"])
        # Reopen on a fresh Desktop host after the SDK has persisted its second turn.
        await shutdown()
        host = await start_host()
        await until("FLEET_FIXTURE_READY")
        host.stdin.write(("reopen " + handoff["session_id"] + "\n").encode())
        await host.stdin.drain()
        await until("FLEET_FIXTURE_REOPENED")
        # A model bridge bearer grants inference only. The distinct paired
        # Sidecar credential can import inert history, including simultaneous retries.
        bridge = json.loads((home / "fleet-desktop/bridge.json").read_text(encoding="utf8"))
        paired = hmac.digest(node_key.encode(), b"dsh-fleet/desktop/v1", "sha256").hex()
        cap = hmac.digest(paired.encode(), ("handoff/v1/" + bridge["token"]).encode(), "sha256").hex()
        request = {"task_id": "retry-fixture", "workspace": str(tmp_path), "provider": "fixture",
                   "model": "local", "messages": [
            {"role": role, "id": role, "time": 123, "content": [{"type": "text", "text": role}]}
            for role in ("user", "assistant")]}
        async with httpx.AsyncClient(trust_env=False, base_url=bridge["url"]) as client:
            async def send(key):
                return await client.post("/handoff", headers={"Authorization": "Bearer " + key}, json=request)
            assert (await send(bridge["token"])).status_code == 403
            first, second = await asyncio.gather(send(cap), send(cap))
            assert first.status_code == second.status_code == 200
            assert first.json()["session_id"] == second.json()["session_id"]
            request["messages"][-1]["content"][0]["text"] = "changed source history"
            assert (await send(cap)).status_code == 422
        assert len(model_requests) == 3
        async with asyncio.timeout(5):
            while not any(e["data"].get("text") == "durable-probe-41" for e in events):
                await asyncio.sleep(0.05)
        await queue_offline(42)
        await shutdown()
        cfg.write_text(
            yaml.safe_dump(
                {
                    "node_id": "other",
                    "secret": secrets.token_urlsafe(32),
                    "hub_url": address + "/other-team",
                }
            )
        )
        manager["manage"]("install", home, cfg)
        faults["offline"] = False
        host = await start_host()
        await until("FLEET_FIXTURE_READY")
        host.stdin.write(b"probe 43\n")
        await host.stdin.drain()
        await until("FLEET_FIXTURE_PROBED")
        await asyncio.sleep(0.2)
        assert not any(
            e["data"].get("text") in {"durable-probe-42", "durable-probe-43"}
            for e in events
        )
        assert any(
            e["data"].get("text") == "durable-probe-42"
            for e in json.loads(statefile.read_text(encoding="utf-8"))["outbox"]
        )
        await shutdown()
    finally:
        await stop_process(host)
        server.shutdown()
        server.server_close()
        thread.join()
