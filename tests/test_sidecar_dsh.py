"""Drive the installed DSH SDK and its real file tool against a loopback Messages API."""

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from fleet_node.agents import DshAdapter, _model_diagnostic, _turn_facts, dsh_command
from fleet_node.app import Node

from packages.protocol.adapters import TaskRequest


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher discovery")
def test_desktop_sdk_command_and_precedence(tmp_path, monkeypatch):
    for variable in ("LOCALAPPDATA", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    root = tmp_path / "DeepSeek Harness"
    exe = root / "DeepSeek Harness.exe"
    shim = root / "resources/runtime/cli/bin/dsh.cmd"
    archive = root / "resources/app.asar"
    shim.parent.mkdir(parents=True)
    shim.touch()
    exe.touch()
    on_path = {}
    monkeypatch.setattr(shutil, "which", on_path.get)
    env = {}
    with pytest.raises(RuntimeError, match="no supported Desktop SDK"):
        dsh_command(env)
    assert not env
    archive.touch()
    expected = [
        str(exe),
        "--expose-internals",
        str(archive / "dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"),
    ]
    assert dsh_command(env) == expected
    assert env == {"ELECTRON_RUN_AS_NODE": "1"}
    monkeypatch.delenv("ProgramFiles")
    on_path["dsh"] = str(shim)
    assert dsh_command({}) == expected

    regular_env = {}
    on_path["dsh"] = str(tmp_path / "client.exe")
    assert dsh_command(regular_env) == [on_path["dsh"]]
    sdk = tmp_path / "node_modules/@deepseek-ai/dsh/lib/bin.js"
    sdk.parent.mkdir(parents=True)
    sdk.touch()
    on_path["dsh"] = str(tmp_path / "dsh.cmd")
    on_path["node.exe"] = str(tmp_path / "node.exe")
    assert dsh_command(regular_env) == [on_path["node.exe"], str(sdk)]
    assert not regular_env


@pytest.mark.skipif(os.name != "nt", reason="Windows explicit SDK selection")
def test_configured_sdk_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *args: pytest.fail("ambient PATH lookup"))
    root = tmp_path / "custom installation"
    archive = root / "resources/app.asar"
    archive.parent.mkdir(parents=True)
    archive.touch()
    exe = root / "DeepSeek Harness.exe"
    exe.touch()
    desktop = {"kind": "desktop", "executable": str(exe)}
    env = {}
    assert dsh_command(env, desktop) == [
        str(exe),
        "--expose-internals",
        str(archive / "dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"),
    ]
    assert env == {"ELECTRON_RUN_AS_NODE": "1"}
    node = tmp_path / "node.exe"
    node.touch()
    package = tmp_path / "private npm/node_modules/@deepseek-ai/dsh"
    entry = package / "lib/bin.js"
    entry.parent.mkdir(parents=True)
    entry.touch()
    npm = {"kind": "npm", "executable": str(node), "entry": str(entry)}
    for invalid in (
        [], {}, {"kind": "shell", "executable": str(exe)},
        desktop | {"args": ["--help"]},
        desktop | {"executable": "DeepSeek Harness.exe"},
        desktop | {"executable": r"\\server.invalid\sdk\DeepSeek Harness.exe"},
        desktop | {"executable": str(node)},
        npm, npm | {"entry": str(tmp_path / "any.js")},
    ):
        with pytest.raises(RuntimeError):
            dsh_command({}, invalid)
    (package / "package.json").write_text(
        json.dumps({"name": "@deepseek-ai/dsh", "bin": {"dsh": "lib/bin.js"}})
    )
    npm_env = {}
    assert dsh_command(npm_env, npm) == [str(node), str(entry)]
    assert not npm_env
    archive.unlink()
    with pytest.raises(RuntimeError, match="layout unavailable"):
        dsh_command({}, desktop)


@pytest.mark.parametrize(
    "variant", ["http_error", "files", "strict_hash", "dynamic", "desktop", "configured_desktop", "configured_npm"]
)
async def test_real_dsh_pre_execution(tmp_path, monkeypatch, variant):
    selection = None
    if variant in ("desktop", "configured_desktop"):
        local = os.environ.get("LOCALAPPDATA")
        if os.name != "nt" or not local or not (
            Path(local) / "Programs/DeepSeek Harness/DeepSeek Harness.exe"
        ).is_file():
            pytest.skip("Optional DSH Desktop runtime absent")
        if variant == "configured_desktop":
            selection = {
                "kind": "desktop",
                "executable": str(Path(local) / "Programs/DeepSeek Harness/DeepSeek Harness.exe"),
            }
    elif variant == "configured_npm":
        shim, node_exe = shutil.which("dsh.cmd"), shutil.which("node.exe")
        if os.name != "nt" or not shim or not node_exe:
            pytest.skip("Optional npm DSH runtime absent")
        entry = Path(shim).parent / "node_modules/@deepseek-ai/dsh/lib/bin.js"
        if not entry.is_file():
            pytest.skip("Optional npm DSH runtime absent")
        selection = {"kind": "npm", "executable": node_exe, "entry": str(entry)}

    if variant == "desktop" or selection:
        which = shutil.which

        def without_dsh(name, *args, **kwargs):
            if name == "dsh" and selection:
                pytest.fail("Configured SDK fell back to PATH discovery")
            return None if name == "dsh" else which(name, *args, **kwargs)

        monkeypatch.setattr(shutil, "which", without_dsh)
        monkeypatch.delenv("ELECTRON_RUN_AS_NODE", raising=False)
        monkeypatch.setenv("DSH_HOME", str(tmp_path / "isolated-home"))
        node = Node({"secret": "local-version-fixture", "dsh_cli": selection})
        await node.refresh_health()
        assert re.match(r"\d+\.\d+\.\d+", node.dsh_version)
        assert "ELECTRON_RUN_AS_NODE" not in os.environ
    elif not shutil.which("dsh"):
        pytest.skip("Optional standalone DSH runtime absent")
    with_check = variant == "strict_hash"
    dynamic = variant == "dynamic"
    calls = []
    config = {
        "volumes": [{"name": "outputs", "path": str(tmp_path), "read_only": False}]
    }
    if selection:
        config["dsh_cli"] = selection
    if with_check:
        config["policy_mode"] = "strict_hash"
        script = tmp_path / "reviewed.py"
        program = b"import os,pathlib\n(pathlib.Path(os.environ['FLEET_WORKSPACE']) / 'record').write_text('ran')\n"
        script.write_bytes(program)
        config["checks"] = [
            {
                "name": "inspect",
                "script": str(script),
                "sha256": hashlib.sha256(program).hexdigest(),
            }
        ]

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            calls.append(body)
            if variant == "http_error":
                payload = json.dumps({"type": "error", "error": {
                    "type": "authentication_error", "message": "private-provider-fixture-body",
                }}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            index = len(calls)
            tool = index <= (3 if with_check or dynamic else 2)
            events = []

            def event(kind, **data):
                value = {"type": kind, **data}
                events.append(
                    "event: " + kind + "\ndata: " + json.dumps(value) + "\n\n"
                )

            event(
                "message_start",
                message={
                    "id": f"reply-{index}",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 4, "output_tokens": 0},
                },
            )
            if tool:
                event(
                    "content_block_start",
                    index=0,
                    content_block={
                        "type": "tool_use",
                        "id": f"tool-{index}",
                        "name": ("fleet_run" if dynamic else "fleet_check")
                        if index == 3
                        else "write",
                        "input": {},
                    },
                )
                filename = "denied.txt" if index == 1 else "permitted.txt"
                args = (
                    {"name": "inspect"}
                    if index == 3
                    else {"file_path": filename, "content": "verified"}
                )
                if dynamic and index == 2:
                    args = {"file_path": "permitted.py", "content": "print(6 * 7)"}
                if dynamic and index == 3:
                    args = {"script": "permitted.py"}
                event(
                    "content_block_delta",
                    index=0,
                    delta={
                        "type": "input_json_delta",
                        "partial_json": json.dumps(args),
                    },
                )
            else:
                event(
                    "content_block_start",
                    index=0,
                    content_block={"type": "text", "text": ""},
                )
                event(
                    "content_block_delta",
                    index=0,
                    delta={"type": "text_delta", "text": "local test finished"},
                )
            event("content_block_stop", index=0)
            event(
                "message_delta",
                delta={
                    "stop_reason": "tool_use" if tool else "end_turn",
                    "stop_sequence": None,
                },
                usage={"output_tokens": 5},
            )
            event("message_stop")
            payload = "".join(events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("DEEPSEEK_API_KEY", secrets.token_urlsafe(32))
    monkeypatch.setenv("DEEPSEEK_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv(
        "DSH_FLEET_CREDENTIAL_ORIGIN", f"http://127.0.0.1:{server.server_port}"
    )
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "isolated-home"))
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(key, raising=False)
    gate, events = [], []

    async def authorize(command, paths, high_risk):
        if command == "fleet_check inspect":
            return True
        if dynamic and (
            command.startswith("fleet_run ") or command.startswith("读取隔离执行快照")
        ):
            return True
        gate.extend(paths)
        assert not (tmp_path / "permitted.txt").exists()
        assert not (tmp_path / "denied.txt").exists()
        return paths == [
            str(tmp_path / ("permitted.py" if dynamic else "permitted.txt"))
        ]

    try:
        adapter = DshAdapter(config)
        if variant == "http_error":
            async with asyncio.timeout(50):
                with pytest.raises(RuntimeError, match="DSH ended with error · sdk_code=AUTH sdk_status=401"):
                    await adapter.run(
                        TaskRequest(id="offline", prompt="private-prompt-fixture", agent="dsh", workspace=str(tmp_path)),
                        lambda kind, data: events.append((kind, data)), authorize,
                    )
            assert calls
            facts = [d["diagnostic"] for _, d in events if "diagnostic" in d]
            assert {"stage": "sdk_turn_end", "reason": "error", "sdk_code": "AUTH", "sdk_status": 401} in facts
            assert not any(f["stage"] == "upstream_http" for f in facts)
            payload = json.dumps(events)
            assert all(value not in payload for value in (
                "private-provider-fixture-body", "private-prompt-fixture", os.environ["DEEPSEEK_API_KEY"],
                os.environ["DEEPSEEK_BASE_URL"],
            ))
            return
        async with asyncio.timeout(210 if dynamic else 50):
            await adapter.run(
                TaskRequest(
                    id="offline",
                    prompt="Perform the local fixture checks",
                    agent="dsh",
                    workspace=str(tmp_path),
                ),
                lambda t, d: events.append((t, d)),
                authorize,
            )
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
    allowed_file = "permitted.py" if dynamic else "permitted.txt"
    assert set(gate) == {
        str(tmp_path / filename) for filename in ("denied.txt", allowed_file)
    }
    assert (tmp_path / allowed_file).read_text() == (
        "print(6 * 7)" if dynamic else "verified"
    )
    assert not (tmp_path / "denied.txt").exists()
    assert len(calls) == (4 if with_check or dynamic else 3)
    expected = {"read", "write", "edit"}
    if not with_check:
        expected.add("fleet_run")
    if dynamic:
        assert "exit=0\\n42" in json.dumps(calls[-1]["messages"], ensure_ascii=False)
    if with_check:
        expected.add("fleet_check")
        assert (tmp_path / "record").read_text() == "ran"
    assert {tool["name"] for tool in calls[0]["tools"]} == expected
    assert ("result", {"text": "local test finished"}) in events
    assert ("log", {"text": "DSH SDK ready · execution guard active"}) in events
    if variant == "desktop":
        assert "ELECTRON_RUN_AS_NODE" not in os.environ
    assert [data["path"] for kind, data in events if kind == "artifact"] == [
        allowed_file
    ]


def test_diagnostic_allowlists_exclude_private_data():
    frame = {"kind": "model_diagnostic", "token": "private", "trace": "a" * 32}
    facts = _model_diagnostic({**frame, "stage": "upstream_http", "status": 429})
    assert facts == {"stage": "upstream_http", "status": 429, "trace": "a" * 32}
    assert _model_diagnostic({**frame, "stage": "client_error", "category": "route_changed", "status": 409}) == {
        "stage": "client_error", "category": "route_changed", "status": 409, "trace": "a" * 32,
    }
    for fields in (
        {"stage": "upstream_http", "status": 401, "body": "private"},
        {"stage": "adapter_start", "status": 200},
        {"stage": "upstream_http"}, {"stage": "upstream_http", "status": True},
        {"stage": "upstream_http", "status": "401"},
        {"stage": "client_error", "status": 200}, {"stage": "private"},
        {"stage": "adapter_end", "trace": "private"},
        {"stage": "bridge_error", "category": "private"},
    ):
        assert _model_diagnostic(frame | fields) is None
    assert _turn_facts({"kind": "private"}) == {"stage": "sdk_turn_end", "reason": "unknown"}
    facts = _turn_facts({"kind": "error", "error": {
        "message": "private", "requestId": "private", "code": "private", "status": True,
    }})
    assert facts == {"stage": "sdk_turn_end", "reason": "error", "sdk_code": "UNKNOWN"}


@pytest.mark.parametrize("enabled", [False, True])
async def test_completed_task_survives_desktop_reload(tmp_path, monkeypatch, enabled):
    from fleet_node.desktop_handoff import DesktopTranscript

    async def broken(*args):
        raise TypeError("private malformed desktop response")

    monkeypatch.setattr(DesktopTranscript, "publish", broken)
    adapter = DshAdapter({})
    adapter.model_route = SimpleNamespace(provider="local", model="local", desktop_handoff=True)
    adapter.ready.set()
    stream = asyncio.StreamReader()
    frames = [
        {"id": 1, "result": {"serverInfo": {"name": "deepseek-harness-sdk-runtime"}}},
        {"id": 2, "result": {}},
        {"method": "session.event", "params": {"sessionId": "unit", "event": {
            "type": "user/message", "data": {"source": None}}}},
        {"method": "session.event", "params": {"sessionId": "unit", "event": {
            "type": "turn/end", "data": {"reason": {"kind": "completed"}}}}},
        {"id": 3, "result": {}}, {"id": 4, "result": {}},
    ]
    for frame in frames:
        stream.feed_data(json.dumps(frame).encode() + b"\n")
    stream.feed_eof()
    adapter.process = SimpleNamespace(
        stdout=stream, wait=lambda: asyncio.sleep(0),
        stdin=SimpleNamespace(write=lambda frame: None, drain=lambda: asyncio.sleep(0)),
    )
    events = []
    await adapter.drive(TaskRequest(id="unit", agent="dsh", prompt="test", workspace=str(tmp_path),
                                   desktop_handoff=enabled), lambda kind,data: events.append((kind,data)))
    assert ("result", {"text": ""}) in events
    handoff = [data for kind,data in events if kind == "desktop_handoff"]
    assert handoff == ([{"status":"unavailable","reason":"desktop_handoff_failed"}] if enabled else [])


@pytest.mark.parametrize("blocks,reason", [
    ([{"type": "image", "attachment": "fixture"}], "unsupported_history"),
    ([{"type": "text", "text": "x" * (2 * 1024 * 1024)}], "history_too_large"),
])
def test_desktop_handoff_rejects_incomplete_history(blocks, reason):
    from fleet_node.desktop_handoff import DesktopTranscript

    transcript = DesktopTranscript()
    transcript.observe({"type": "user/message", "time": 1, "surfaceOp": "append", "data": {
        "id": "m1", "role": "user", "source": {"kind": "user"}, "content": blocks,
    }})
    assert transcript.reason == reason
    assert not transcript.messages


@pytest.mark.parametrize("rpc_code,recorded", [(-32602, -32602), ("private-code", None)])
async def test_sdk_rpc_error_keeps_only_method_and_standard_code(tmp_path, rpc_code, recorded):
    adapter = DshAdapter({})
    adapter.model_route = SimpleNamespace(provider="private-provider", model="private-model")
    stream = asyncio.StreamReader()
    stream.feed_data(json.dumps({"id": 1, "error": {
        "code": rpc_code, "message": "private-message", "data": {"token": "private-token"},
    }}).encode() + b"\n")
    stream.feed_eof()
    adapter.process = SimpleNamespace(
        stdout=stream,
        stdin=SimpleNamespace(write=lambda frame: None, drain=lambda: asyncio.sleep(0)),
    )
    events = []
    with pytest.raises(RuntimeError, match="^DSH rejected initialize$"):
        await adapter.drive(
            TaskRequest(id="rpc-error", agent="dsh", prompt="private-prompt", workspace=str(tmp_path)),
            lambda kind, data: events.append(data),
        )
    expected = {"stage": "sdk_rpc_error", "method": "initialize"}
    if recorded is not None:
        expected["rpc_code"] = recorded
    assert [event["diagnostic"] for event in events] == [expected]
    assert "private" not in json.dumps(events)
