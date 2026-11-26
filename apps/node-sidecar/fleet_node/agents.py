import asyncio
import contextlib
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

from packages.protocol.adapters import AgentAdapter, register

from .paths import guarded_target, protected_roots, sensitive_path
from .policy import describe_tool
from . import runner
from .desktop_handoff import DesktopTranscript
from .processes import spawn_settings, stop_process
from .provider import select_live_route
from .volumes import Volume


_MODEL_STAGES = {
    "client_start", "bridge_received", "adapter_start", "upstream_send", "upstream_http",
    "adapter_chunk", "adapter_end", "bridge_error", "client_error",
}
_MODEL_ERRORS = {
    "unauthorized", "forbidden", "rate_limit", "timeout", "connection", "aborted",
    "invalid_request", "route_changed", "busy", "protocol", "provider_error",
    "bridge_unavailable", "model_refused", "unknown", "credentials_missing",
    "provider_unavailable",
}
_SDK_FAILURE_CODES = {
    "ABORTED", "ACCOUNT_QUOTA", "AUTH", "INVALID_REQUEST", "INVALID_RESPONSE",
    "MALFORMED_RESPONSE", "MESSAGES_IDLE", "STREAM_CLOSED", "UNKNOWN_MODEL",
    "INVALID_REPLAY_STATE", "UNSUPPORTED_CONTENT", "CONTEXT_WINDOW_EXCEEDED", "EMPTY_RESPONSE",
    "IMAGE_OFFLOAD_REQUIRED", "INVALID_ADAPTER", "INVALID_CREDENTIAL",
    "INVALID_MODEL_CONTEXT", "INVALID_MODEL_INFO", "INVALID_MODEL_MAX_TOKENS",
    "INVALID_MODEL_REASONING", "INVALID_PREPARED_CALL", "NO_ADAPTER", "NO_DISCOVERY",
    "QUOTA", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT", "UNKNOWN",
    "UNSUPPORTED_REASONING_EFFORT",
}
_TURN_REASONS = {"completed", "aborted", "blocked", "error", "max-tokens", "interrupted", "forked"}


def _model_diagnostic(frame):
    stage, trace = frame.get("stage"), frame.get("trace")
    if not isinstance(stage, str) or stage not in _MODEL_STAGES:
        return None
    if not isinstance(trace, str) or not re.fullmatch(r"[0-9a-f]{32}", trace):
        return None
    if frame.keys() - {"kind", "token", "stage", "trace", "category", "status"}:
        return None
    facts = {"stage": stage, "trace": trace}
    if "category" in frame:
        category = frame["category"]
        if (stage not in {"bridge_error", "client_error"}
                or not isinstance(category, str) or category not in _MODEL_ERRORS):
            return None
        facts["category"] = category
    if "status" in frame or stage == "upstream_http":
        status = frame.get("status")
        if (type(status) is not int or not 100 <= status <= 599
                or stage not in {"upstream_http", "client_error"}
                or (stage == "client_error" and status < 400)):
            return None
        facts["status"] = status
    return facts


def _diagnostic(emit, facts):
    # Only callers constructing allowlisted facts may use this event.
    summary = " ".join(f"{key}={value}" for key, value in facts.items())
    emit("log", {"text": "DSH diagnostic " + summary, "diagnostic": facts})


def _turn_facts(reason):
    reason = reason if isinstance(reason, dict) else {}
    kind = reason.get("kind")
    if not isinstance(kind, str) or kind not in _TURN_REASONS:
        kind = "unknown"
    facts = {"stage": "sdk_turn_end", "reason": kind}
    if kind == "error":
        failure = reason.get("error")
        failure = failure if isinstance(failure, dict) else {}
        code, status = failure.get("code"), failure.get("status")
        facts["sdk_code"] = code if isinstance(code, str) and code in _SDK_FAILURE_CODES else "UNKNOWN"
        if type(status) is int and 100 <= status <= 599:
            facts["sdk_status"] = status
    return facts


class MockAgentAdapter(AgentAdapter):
    def __init__(self, config=None):
        self.process = None
        self.config = config or {}

    async def run(self, task, emit, authorize):
        emit("log", {"text": "Mock Agent 已接收任务"})
        if not await authorize(
            "mock simulated operation: " + task.prompt, [], "[approval]" in task.prompt
        ):
            raise PermissionError("Approval denied")
        if "[wait]" in task.prompt:
            # A real nested tree makes cancellation testable without executing user text.
            child = "import time; time.sleep(300)"
            script = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',sys.argv[1]]); print(p.pid,flush=True); time.sleep(300)"
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                child,
                stdout=asyncio.subprocess.PIPE,
                **spawn_settings(),
            )
            child_pid = int(await self.process.stdout.readline())
            emit(
                "tool_call",
                {"name": "mock_wait", "pid": self.process.pid, "child_pid": child_pid},
            )
            await self.process.wait()
        else:
            emit("tool_call", {"name": "mock_inspect", "workspace": task.workspace})
            await asyncio.sleep(0.12)
        if "[artifact]" in task.prompt:
            volumes = [
                Volume(v)
                for v in self.config.get("volumes", [])
                if not v.get("read_only", True)
            ]
            if not volumes:
                raise ValueError("A writable mock output volume is required")
            volume = volumes[0]
            from .sample_artifacts import TITLES, samples

            contents = samples(task.prompt)
            prefix = "fleet-output/" + task.id + "."
            paths = [volume.target(prefix + suffix) for suffix in contents]
            if any(
                p.is_relative_to(root)
                for p in paths
                for root in self.config.get("_write_barriers", ())
            ):
                raise PermissionError("Read-only output directory")
            if not await authorize("mock write report", [str(p) for p in paths], False):
                raise PermissionError("Artifact write denied")
            volume.target("fleet-output").mkdir(exist_ok=True)
            for suffix, content in contents.items():
                name = prefix + suffix
                path = volume.target(name)
                with path.open("xb") as output:
                    output.write(content)
                emit(
                    "artifact",
                    {
                        "volume": volume.name,
                        "path": name,
                        "name": TITLES[suffix],
                        "size": len(content),
                    },
                )
        emit("result", {"text": "Mock 完成 · " + task.prompt})

    async def cancel(self):
        await stop_process(self.process)


def _desktop_command(exe, child_env):
    root = exe.parent
    archive = root / "resources/app.asar"
    if not exe.is_file() or not archive.is_file():
        return None
    if child_env is None:
        raise RuntimeError("Desktop SDK requires a child process environment")
    child_env["ELECTRON_RUN_AS_NODE"] = "1"
    # Electron resolves this entry inside the archive; it is not an ordinary OS file.
    cli = archive / "dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
    return [str(exe), "--expose-internals", str(cli)]


def _sdk_file(raw, suffix):
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise RuntimeError("Invalid configured DSH SDK file")
    path = Path(raw)
    if not (
        path.is_absolute()
        and re.fullmatch(r"[A-Za-z]:", path.drive)
        and path.match(suffix)
        and all(p != ".." and ":" not in p for p in path.parts[1:])
        and ctypes.windll.kernel32.GetDriveTypeW(path.anchor) == 3
    ):
        raise RuntimeError("Expected the configured DSH SDK file on a local fixed drive")
    actual = path.resolve()
    if (
        not re.fullmatch(r"[A-Za-z]:", actual.drive)
        or ctypes.windll.kernel32.GetDriveTypeW(actual.anchor) != 3
        or not actual.is_file()
    ):
        raise RuntimeError("Configured DSH SDK file unavailable")
    return path


def dsh_command(child_env=None, selection=None):
    """Resolve the SDK argv; only a Desktop launch needs an extra child environment flag."""
    if selection is not None:
        if os.name != "nt" or not isinstance(selection, dict):
            raise RuntimeError("Expected a Windows dsh_cli mapping")
        kind = selection.get("kind")
        if kind == "desktop" and set(selection) == {"kind", "executable"}:
            exe = _sdk_file(selection["executable"], "DeepSeek Harness.exe")
            command = _desktop_command(exe, child_env)
            if command:
                return command
            raise RuntimeError("Configured Desktop SDK layout unavailable")
        if kind == "npm" and set(selection) == {"kind", "executable", "entry"}:
            node = _sdk_file(selection["executable"], "node.exe")
            entry = _sdk_file(
                selection["entry"], "node_modules/@deepseek-ai/dsh/lib/bin.js"
            )
            try:
                package = json.loads((entry.parents[1] / "package.json").read_bytes())
                valid = (
                    package["name"] == "@deepseek-ai/dsh"
                    and package["bin"]["dsh"] in ("./lib/bin.js", "lib/bin.js")
                )
            except (OSError, UnicodeError, ValueError, TypeError, KeyError):
                valid = False
            if not valid:
                raise RuntimeError("Configured npm SDK package manifest unavailable or invalid")
            return [str(node), str(entry)]
        raise RuntimeError("Invalid dsh_cli kind or fields")

    command = shutil.which("dsh")
    if os.name != "nt":
        if command:
            return [command]
        raise RuntimeError("DSH executable unavailable")

    # Never invoke a batch shim. Use the npm entry or the official Desktop runtime directly.
    desktop_roots = []
    shim_path = Path("resources/runtime/cli/bin/dsh.cmd")
    if command:
        entry = Path(command).parent / "node_modules/@deepseek-ai/dsh/lib/bin.js"
        node = shutil.which("node.exe")
        if node and entry.is_file():
            return [node, str(entry)]
        if command.lower().endswith(".exe"):
            return [command]
        path = Path(command)
        if path.match(str(shim_path)):
            desktop_roots.append(path.parents[len(shim_path.parts) - 1])

    for variable, suffix in (
        ("LOCALAPPDATA", "Programs/DeepSeek Harness"),
        ("ProgramW6432", "DeepSeek Harness"),
        ("ProgramFiles", "DeepSeek Harness"),
        ("ProgramFiles(x86)", "DeepSeek Harness"),
    ):
        parent = os.environ.get(variable)
        if parent:
            desktop_roots.append(Path(parent) / suffix)

    # User-level npm and nvm installations need not export their shim directory
    # to the logon task. An explicit dsh_cli selection still takes precedence.
    node = shutil.which("node.exe")
    npm_roots = []
    if os.environ.get("APPDATA"):
        npm_roots.append(Path(os.environ["APPDATA"]) / "npm")
    for key in ("NVM_SYMLINK", "npm_config_prefix"):
        if os.environ.get(key):
            npm_roots.append(Path(os.environ[key]))
    if node:
        npm_roots.append(Path(node).parent)
    for root in npm_roots:
        if root.is_absolute():
            entry = root / "node_modules/@deepseek-ai/dsh/lib/bin.js"
            runtime = root / "node.exe"
            if entry.is_file() and (runtime.is_file() or node):
                return dsh_command(child_env, {"kind":"npm", "entry":str(entry),
                                    "executable":str(runtime) if runtime.is_file() else node})

    # An npx install may exist without a global shim. An ambiguous cache requires
    # an explicit SDK selection so an upgrade cannot silently switch versions.
    cache = os.environ.get("npm_config_cache")
    if not cache and os.environ.get("LOCALAPPDATA"):
        cache = str(Path(os.environ["LOCALAPPDATA"]) / "npm-cache")
    if node and cache and Path(cache).is_absolute():
        entries = list((Path(cache) / "_npx").glob("*/node_modules/@deepseek-ai/dsh/lib/bin.js"))
        if len(entries) == 1:
            return dsh_command(child_env, {"kind":"npm","executable":node,"entry":str(entries[0])})

    for root in dict.fromkeys(desktop_roots):
        if not root.is_absolute() or not re.fullmatch(r"[A-Za-z]:", root.drive):
            continue
        if (root / "DeepSeek Harness.exe").is_file():
            desktop = _desktop_command(root / "DeepSeek Harness.exe", child_env)
            if desktop:
                return desktop
    if command:
        raise RuntimeError("Supported DSH Node launcher or Desktop SDK not found")
    raise RuntimeError("DSH executable unavailable; no supported Desktop SDK found")


class DshAdapter(AgentAdapter):
    """Per-task isolated SDK profile. The file tools cross a pre-execution gate."""

    def __init__(self, config):
        self.config = config
        self.process = None
        self.protected = protected_roots(config)
        self.protected.add(
            Path(
                config.get("dsh_home")
                or os.environ.get("DSH_HOME")
                or Path.home() / ".dsh"
            ).resolve()
        )
        self.read_only = list(config.get("_write_barriers", ()))
        self.ready = asyncio.Event()
        self.writes = {}
        self.checks = {}
        for check in config.get("checks", []):
            script = Path(check["script"])
            if (
                not re.fullmatch(r"[\w-]{1,64}", check["name"])
                or not script.is_absolute()
                or not re.fullmatch(r"[0-9a-fA-F]{64}", check["sha256"])
            ):
                raise ValueError(
                    "A local check needs a name, absolute script path and SHA-256 digest"
                )
            self.checks[check["name"]] = (script, check["sha256"].lower())
            self.protected.add(script.resolve())

    async def cancel(self):
        await stop_process(self.process)

    def parse(self, event):
        kind = event.get("type", "")
        data = event.get("data", {})
        if kind == "assistant/message":
            content = data.get("message", {}).get("content", [])
            return "message", {
                "text": "".join(
                    b.get("text", "") for b in content if b.get("type") == "text"
                )
            }
        if kind == "tool/call":
            return "tool_call", {
                "name": data.get("name"),
                "call_id": data.get("callId"),
            }
        if kind == "tool/result":
            message = data.get("message", {})
            return "tool_result", {
                "call_id": message.get("toolCallId"),
                "success": message.get("isError") is not True,
            }
        return kind, data

    async def run(self, task, emit, authorize):
        with tempfile.TemporaryDirectory(prefix="fleet-dsh-") as temporary:
            root = Path(temporary)
            self.protected.add(root)
            token = secrets.token_urlsafe(40)
            callbacks = set()
            diagnostic_count = 0

            async def gate(reader, writer):
                nonlocal diagnostic_count
                callbacks.add(asyncio.current_task())
                allowed = False
                result = None
                try:
                    async with asyncio.timeout(5):
                        message = json.loads(await reader.readline())
                    if not isinstance(message, dict):
                        return
                    if not hmac.compare_digest(str(message.get("token", "")), token):
                        return
                    if message.get("kind") == "ready":
                        self.ready.set()
                        allowed = True
                    elif message.get("kind") == "model_diagnostic":
                        facts = _model_diagnostic(message)
                        if facts is not None:
                            allowed = True
                            if diagnostic_count < 512:
                                _diagnostic(emit, facts)
                                diagnostic_count += 1
                            elif diagnostic_count == 512:
                                _diagnostic(emit, {"stage": "model_diagnostics_truncated"})
                                diagnostic_count += 1
                    elif message.get("kind") == "tool":
                        allowed = await self.tool_allowed(task, message, authorize)
                    elif message.get("kind") == "run_guest":
                        plan = await runner.prepare(
                            message.get("args"),
                            task,
                            self.config,
                            self.protected,
                            authorize,
                        )
                        result = await runner.execute(plan, self.config)
                        allowed = True
                    elif (
                        message.get("kind") == "run_check"
                        and message.get("name") in self.checks
                        and self.config.get("policy_mode") == "strict_hash"
                    ):
                        result = await self.run_check(
                            message["name"], task.workspace, root
                        )
                        allowed = True
                except (
                    ValueError,
                    KeyError,
                    TypeError,
                    OSError,
                    TimeoutError,
                    RuntimeError,
                ):
                    pass
                finally:
                    writer.write(
                        json.dumps({"allowed": allowed, "result": result}).encode()
                        + b"\n"
                    )
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
                    callbacks.discard(asyncio.current_task())

            server = await asyncio.start_server(gate, "127.0.0.1", 0, limit=1024 * 1024)
            try:
                patch = root / "fleet.yaml"
                records = [
                    {"id": name, "disabled": True}
                    for name in (
                        "persistent-bash",
                        "persistent-pwsh",
                        "tool-working-directory",
                        "mcp-resources",
                    )
                ]
                records.extend(
                    [
                        {
                            "id": "tool-fs",
                            "config": {
                                "read": True,
                                "write": True,
                                "edit": True,
                                "readImage": False,
                            },
                        },
                        {
                            "insert": [
                                {
                                    "id": "fleet-guard",
                                    "name": Path(__file__)
                                    .with_name("guard.mjs")
                                    .as_uri(),
                                }
                            ]
                        },
                    ]
                )
                self.model_route = await select_live_route(self.config)
                source = self.model_route.source
                _diagnostic(emit, {
                    "stage": "route_selected",
                    "source": source if source in {
                        "desktop", "sidecar", "dsh-profile", "dsh-home-env", "environment",
                    } else "unknown",
                })
                records.extend(self.model_route.patches)
                patch.write_text(yaml.safe_dump(records), encoding="utf-8")
                env = {
                    k: v
                    for k, v in os.environ.items()
                    if k.upper()
                    in {
                        "PATH",
                        "PATHEXT",
                        "SYSTEMROOT",
                        "WINDIR",
                        "COMSPEC",
                        "TMP",
                        "TEMP",
                        "TMPDIR",
                        "HOME",
                        "USERPROFILE",
                        "APPDATA",
                        "LOCALAPPDATA",
                        "LANG",
                        "HTTPS_PROXY",
                        "HTTP_PROXY",
                        "NO_PROXY",
                        "SSL_CERT_FILE",
                        "NODE_EXTRA_CA_CERTS",
                    }
                }
                env.update(
                    **self.model_route.environment,
                    DSH_HOME=str(root),
                    FLEET_GUARD_KEY=token,
                    FLEET_CHECK_NAMES=json.dumps(
                        list(self.checks)
                        if self.config.get("policy_mode") == "strict_hash"
                        else []
                    ),
                    FLEET_RUNNERS=json.dumps(
                        runner.available(self.config)
                        if self.config.get("policy_mode", "balanced") != "strict_hash"
                        else []
                    ),
                    FLEET_GUARD_PORT=str(server.sockets[0].getsockname()[1]),
                    FLEET_GUARD_TIMEOUT=str(
                        int((2 * self.config.get("approval_timeout", 120) + 200) * 1000)
                    ),
                )
                selection = self.config.get("dsh_cli")
                command = dsh_command(env, selection)
                if selection is not None:
                    self.protected.add(Path(command[0]).resolve().parent)
                    if selection["kind"] == "npm":
                        self.protected.add(Path(command[1]).parents[3].resolve())
                self.process = await asyncio.create_subprocess_exec(
                    *command,
                    "--profile",
                    "sdk-minimal",
                    "--patch",
                    str(patch),
                    cwd=root,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=4 * 1024 * 1024,
                    **spawn_settings(),
                )
                await self.drive(task, emit)
            finally:
                server.close()
                await server.wait_closed()
                for callback in tuple(callbacks):
                    callback.cancel()
                await asyncio.gather(*tuple(callbacks), return_exceptions=True)
                await self.cancel()

    async def tool_allowed(self, task, message, authorize):
        name, args = message.get("name"), message.get("args", {})
        if name == "fleet_run":
            # Final approval and execution share one snapshot in the registered
            # tool's run_guest callback after DSH validates parameter types.
            return (
                isinstance(args, dict)
                and self.config.get("policy_mode", "balanced") != "strict_hash"
            )
        if (
            name == "fleet_check"
            and isinstance(args, dict)
            and set(args) == {"name"}
            and args["name"] in self.checks
            and self.config.get("policy_mode") == "strict_hash"
        ):
            return await authorize(
                "fleet_check " + args["name"], [task.workspace], False
            )
        if name not in {"read", "write", "edit"} or not isinstance(args, dict):
            return False
        path = args.get("file_path")
        target = guarded_target(Path(task.workspace), path)
        if target is None or sensitive_path(target, self.protected):
            return False
        read_only = list(self.read_only) + [
            Path(v.get("path") or v["root"]).resolve()
            for v in self.config.get("volumes", [])
            if v.get("read_only", True)
        ]
        if name != "read" and any(target.is_relative_to(p) for p in read_only):
            return False
        if not await authorize(describe_tool(name, args), [str(target)], False):
            return False
        if guarded_target(Path(task.workspace), path) != target:
            return False
        previewable_reference = name == "read" and target.suffix.lower() in {
            ".md",
            ".markdown",
            ".csv",
            ".png",
            ".jpg",
            ".jpeg",
            ".svg",
        }
        if (name in {"edit", "write"} or previewable_reference) and isinstance(
            message.get("call_id"), str
        ):
            self.writes[message["call_id"]] = target
        return True

    def artifact(self, data, task=None):
        target = self.writes.pop(data.get("call_id"), None)
        if target is None or not data["success"] or not target.is_file():
            return None
        for entry in self.config.get("volumes", []):
            volume = Volume(entry)
            if target.is_relative_to(volume.root):
                path = target.relative_to(volume.root).as_posix()
                if volume.target(path) == target:
                    return {
                        "volume": volume.name,
                        "path": path,
                        "name": target.name,
                        "size": target.stat().st_size,
                    }
        if task is not None:
            from .artifacts import capture

            return capture(self.config, task, target, self.protected)
        return None

    async def run_check(self, name, workspace, root):
        script, expected = self.checks[name]
        with script.open("rb") as handle:
            content = handle.read(1024 * 1024 + 1)
        if (
            len(content) > 1024 * 1024
            or hashlib.sha256(content).hexdigest() != expected
        ):
            raise ValueError("Registered check digest mismatch")
        with tempfile.TemporaryDirectory(dir=root) as staging:
            copy = Path(staging) / "check.py"
            copy.write_bytes(content)
            env = {
                k: os.environ[k]
                for k in ("SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR", "LANG")
                if k in os.environ
            }
            env["FLEET_WORKSPACE"] = workspace
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                str(copy),
                cwd=staging,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **spawn_settings(),
            )
            try:
                result = bytearray()
                async with asyncio.timeout(30):
                    while chunk := await process.stdout.read(8192):
                        result.extend(chunk)
                        if len(result) > 64 * 1024:
                            raise ValueError("Registered check output limit exceeded")
                    code = await process.wait()
                    return f"exit={code}\n" + result.decode("utf-8", errors="replace")
            finally:
                await stop_process(process)

    async def drive(self, task, emit):
        process = self.process
        answer = ""
        terminal = {"reason": "missing"}
        transcript = DesktopTranscript() if task.desktop_handoff else None

        async def call(number, method, params):
            nonlocal answer, terminal
            frame = {"jsonrpc": "2.0", "id": number, "method": method, "params": params}
            process.stdin.write(json.dumps(frame).encode() + b"\n")
            await process.stdin.drain()
            while line := await process.stdout.readline():
                try:
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise ValueError
                except (ValueError, RecursionError):
                    _diagnostic(emit, {"stage": "sdk_invalid_frame", "method": method})
                    raise RuntimeError("Invalid DSH SDK frame during " + method) from None
                if item.get("id") == number:
                    if "error" in item:
                        error = item["error"]
                        code = error.get("code") if isinstance(error, dict) else None
                        facts = {"stage": "sdk_rpc_error", "method": method}
                        if type(code) is int and (
                            code in {-32700, -32600, -32601, -32602, -32603}
                            or -32099 <= code <= -32000
                        ):
                            facts["rpc_code"] = code
                        _diagnostic(emit, facts)
                        raise RuntimeError("DSH rejected " + method)
                    return item.get("result", {})
                payload = item.get("params", {})
                if (
                    item.get("method") == "session.event"
                    and payload.get("sessionId") == task.id
                ):
                    if transcript is not None and getattr(self.model_route, "desktop_handoff", False):
                        transcript.observe(payload["event"])
                    kind, data = self.parse(payload["event"])
                    if kind == "turn/end":
                        terminal = _turn_facts(data.get("reason"))
                        if terminal["reason"] != "completed":
                            _diagnostic(emit, terminal)
                    elif kind in {"message", "tool_call", "tool_result"}:
                        if kind == "message" and data["text"]:
                            answer = data["text"]
                        emit(kind, data)
                        if kind == "tool_result" and (
                            entry := await asyncio.to_thread(self.artifact, data, task)
                        ):
                            emit("artifact", entry)
            _diagnostic(emit, {"stage": "sdk_stream_closed", "method": method})
            raise RuntimeError("DSH closed its SDK stream during " + method)

        startup_phase = "initialize"
        try:
            async with asyncio.timeout(self.config.get("dsh_startup_timeout", 45)):
                identity = await call(
                    1,
                    "initialize",
                    {
                        "cwd": task.workspace,
                        "provider": self.model_route.provider,
                        "model": self.model_route.model,
                    },
                )
                if (
                    identity.get("serverInfo", {}).get("name")
                    != "deepseek-harness-sdk-runtime"
                ):
                    raise RuntimeError("Unrecognized DSH SDK runtime")
                startup_phase = "execution_guard"
                await self.ready.wait()
        except TimeoutError:
            _diagnostic(emit, {"stage": "sdk_startup_timeout", "phase": startup_phase})
            raise RuntimeError("DSH startup timed out during " + startup_phase) from None
        emit("log", {"text": "DSH SDK ready · execution guard active"})
        await call(
            2,
            "session/prompt",
            {
                "sessionId": task.id,
                "contentBlocks": [{"type": "text", "text": task.prompt}],
            },
        )
        await call(3, "session/wait", {"sessionId": task.id})
        if terminal["reason"] != "completed":
            summary = "DSH ended with " + terminal["reason"]
            if "sdk_code" in terminal:
                summary += " · sdk_code=" + terminal["sdk_code"]
            if "sdk_status" in terminal:
                summary += " sdk_status=" + str(terminal["sdk_status"])
            raise RuntimeError(summary)
        emit("result", {"text": answer})
        async with asyncio.timeout(10):
            await call(4, "shutdown", {})
            await process.wait()
        if transcript is not None:
            try:
                handoff = await transcript.publish(task, self.config, self.model_route)
            except Exception:
                # The SDK has already finished. An optional desktop import (or a
                # plugin reload) cannot change that outcome or disclose its body.
                handoff = {"status": "unavailable", "reason": "desktop_handoff_failed"}
            emit("desktop_handoff", handoff)


register("mock", MockAgentAdapter)
register("dsh", DshAdapter)
