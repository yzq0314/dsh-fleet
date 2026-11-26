import asyncio
import hashlib
import socket
import secrets
from pathlib import Path

import httpx
import pytest
import psutil

from fleet_node.app import create_app
from fleet_node.agents import DshAdapter
from fleet_node.policy import DEFAULT_RULES, TaskPolicy, compile_rules
from fleet_node.runner import Plan, execute, prepare
from packages.protocol.adapters import Decision, TaskRequest


@pytest.fixture
def task(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return TaskRequest(
        id="runner-test", agent="dsh", workspace=str(root), prompt="isolation checks"
    )


def test_default_policy_flags_privileged_operations():
    rules = compile_rules(DEFAULT_RULES)
    examples = (
        "sudo id",
        "Start-Process tool -Verb RunAs",
        "Set-ItemProperty HKCU:\\Example",
        "New-NetFirewallRule",
        "Restart-Service Example",
        "Clear-Disk",
        "Remove-Item -Recurse -Force sample",
        "git push --force",
    )
    for command in examples:
        assert any(rule.search(command) for rule in rules), command
    assert not any(rule.search("pytest tests/test_report.py") for rule in rules)


async def allow(*_):
    return True


@pytest.fixture
def listening_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        sock.settimeout(0.15)
        yield sock


async def test_dynamic_wasi_snapshot_and_guest_isolation(
    task, tmp_path, monkeypatch, listening_port
):
    workspace = Path(task.workspace)
    private = tmp_path / "outside.txt"
    private.write_text("private", encoding="utf8")
    (workspace / "sample.txt").write_text("fixture", encoding="utf8")
    (workspace / "undisclosed.txt").write_text("hidden", encoding="utf8")
    script = workspace / "compute.py"
    script.write_text(
        "import os, socket, subprocess\n"
        "assert not os.getenv('FLEET_HOST_TEST_SENTINEL')\n"
        "print(open('sample.txt').read(), 6*7)\n"
        "for path in ('undisclosed.txt', " + repr(str(private)) + "):\n"
        "    try: open(path).read()\n"
        "    except OSError: pass\n"
        "    else: raise AssertionError('host file visible')\n"
        "try: open('sample.txt', 'w').write('changed')\n"
        "except OSError: pass\n"
        "else: raise AssertionError('guest input writable')\n"
        f"try: socket.socket().connect(('127.0.0.1', {listening_port.getsockname()[1]}))\n"
        "except OSError: pass\n"
        "else: raise AssertionError('network accessible')\n"
        "try: subprocess.run(['hostname'],check=True)\n"
        "except (OSError, NotImplementedError): pass\n"
        "else: raise AssertionError('external process launched')\n"
        "print('bounded')\n",
        encoding="utf8",
    )
    monkeypatch.setenv("FLEET_HOST_TEST_SENTINEL", "private-context")

    async def authorize(command, paths, high_risk):
        if command.startswith("fleet_run "):
            assert "6*7" in command and set(paths) == {
                str(script),
                str(workspace / "sample.txt"),
            }
            script.write_text(
                "raise RuntimeError('changed after approval')", encoding="utf8"
            )
        return True

    plan = await prepare(
        {"script": "compute.py", "inputs": ["sample.txt"]}, task, {}, set(), authorize
    )
    result = await execute(plan, {"windows_runner": "isolated"})
    assert (
        result.startswith("exit=0") and "fixture 42" in result and "bounded" in result
    ), result
    assert (workspace / "sample.txt").read_text() == "fixture"
    with pytest.raises(TimeoutError):
        listening_port.accept()


async def test_registered_check_requires_strict_mode_and_original_bytes(task, tmp_path):
    script = tmp_path / "reviewed.py"
    script.write_text("print('reviewed')", encoding="utf8")
    item = {
        "name": "summary",
        "script": str(script),
        "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
    }
    config = {"checks": [item], "policy_mode": "strict_hash"}
    adapter = DshAdapter(config)
    call = {"name": "fleet_check", "args": {"name": "summary"}}
    assert await adapter.tool_allowed(task, call, allow)
    script.write_text("print('edited')", encoding="utf8")
    with pytest.raises(ValueError, match="digest mismatch"):
        await adapter.run_check("summary", task.workspace, tmp_path)
    for mode in ("balanced", "autonomous"):
        config["policy_mode"] = mode
        assert not await adapter.tool_allowed(task, call, allow)


async def test_pytest_inside_guest(task):
    workspace = Path(task.workspace)
    (workspace / "test_sum.py").write_text(
        "def test_sum():\n    assert sum([1, 2]) == 3\n", encoding="utf8"
    )
    plan = await prepare(
        {"script": "test_sum.py", "language": "pytest"}, task, {}, set(), allow
    )
    result = await execute(plan, {})
    assert result.startswith("exit=0") and "1 passed" in result, result


@pytest.mark.parametrize("mode", ["balanced", "autonomous"])
async def test_high_risk_guest_snapshot_requires_decision(task, mode):
    root = Path(task.workspace)
    script = root / "check.py"
    script.write_text("print('git push is an example')", encoding="utf8")
    config = {
        "secret": secrets.token_urlsafe(32),
        "trusted_workspaces": [str(root)],
        "policy_mode": mode,
    }
    app = create_app(config)
    pending = asyncio.get_running_loop().create_future()

    def emit(kind, data):
        if kind == "approval_required":
            pending.set_result(data)

    async with app.router.lifespan_context(app):
        policy = TaskPolicy(app.state.node, root, emit, DshAdapter(config))
        preparing = asyncio.create_task(
            prepare({"script": "check.py"}, task, config, set(), policy.authorize)
        )
        approval = await asyncio.wait_for(pending, 3)
        assert (
            "git push" in approval["command"] and approval["reason"] == "命中高危规则"
        )
        assert policy.decide(approval["id"], Decision.deny)
        with pytest.raises(PermissionError):
            await preparing


async def test_validation_modes_and_resources(task):
    base = Path(task.workspace)
    (base / "x.py").write_text("print('a' * 90000)", encoding="utf8")
    with pytest.raises(PermissionError):
        await prepare(
            {"script": "x.py"}, task, {"policy_mode": "strict_hash"}, set(), allow
        )
    for path in ("../private", ".ssh/secret", "C:/config", "/private"):
        with pytest.raises((PermissionError, ValueError)):
            await prepare({"script": "x.py", "inputs": [path]}, task, {}, set(), allow)
    inputs = [f"{i}.txt" for i in range(9)]
    for name in inputs:
        (base / name).write_text("data", encoding="utf8")
    flags = []

    async def capture(command, paths, high_risk):
        if command.startswith("fleet_run "):
            flags.append(high_risk)
        return True

    args = {"script": "x.py", "inputs": inputs}
    await prepare(args, task, {"policy_mode": "balanced"}, set(), capture)
    plan = await prepare(args, task, {"policy_mode": "autonomous"}, set(), capture)
    assert flags == [True, False]
    with pytest.raises(PermissionError):
        await execute(plan, {"policy_mode": "strict_hash"})
    output = await execute(plan, {"windows_runner": "isolated"})
    assert (
        output.startswith("exit=1") and "Guest limit:" in output and len(output) < 65000
    ), output[-150:]


async def test_mode_updates_cancel_active_and_preserve_rules(tmp_path):
    secret = secrets.token_urlsafe(32)
    app = create_app(
        {
            "secret": secret,
            "trusted_workspaces": [str(tmp_path)],
            "approval_timeout": 2,
            "agent": "mock",
        }
    )
    headers = {"Authorization": "Bearer " + secret}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=headers,
        ) as client:
            old = (await client.get("/api/policy")).json()
            assert old["mode"] == "balanced" and "python" in old["runners"]
            assert (
                await client.put("/api/policy", json={"mode": "autonomous"})
            ).status_code == 422
            assert (
                await client.put(
                    "/api/policy",
                    json={"mode": "autonomous", "confirm_autonomous": "yes"},
                )
            ).status_code == 422
            assert (
                await client.put(
                    "/api/policy",
                    json={"mode": "autonomous", "confirm_autonomous": True},
                )
            ).status_code == 200
            assert (
                await client.post(
                    "/api/tasks",
                    json={
                        "id": "wait-job",
                        "prompt": "[approval]",
                        "workspace": str(tmp_path),
                    },
                )
            ).is_success
            for _ in range(50):
                events = (await client.get("/api/tasks/wait-job/events")).json()
                if any(e["type"] == "approval_required" for e in events):
                    break
                await asyncio.sleep(0.03)
            assert any(e["type"] == "approval_required" for e in events)
            response = (
                await client.put("/api/policy", json={"mode": "strict_hash"})
            ).json()
            assert response["rules"] == old["rules"] and response["runners"] == []
            events = (await client.get("/api/tasks/wait-job/events")).json()
            assert events[-1]["data"]["status"] == "cancelled"
            assert (await client.put("/api/policy", json={"rules": ["danger"]})).json()[
                "mode"
            ] == "strict_hash"


async def test_budget_and_cancellation_of_live_wasi_worker(monkeypatch):
    memory = Plan(
        "python",
        "memory.py",
        {
            "memory.py": (
                b"try: data = bytearray(256 * 1024 * 1024)\nexcept MemoryError: print('bounded memory')"
            )
        },
        [],
    )
    config = {"windows_runner": "isolated"}
    assert "bounded memory" in await execute(memory, config)
    plan = Plan("python", "busy.py", {"busy.py": b"while True: pass"}, [])
    output = await execute(plan, config)
    assert output.startswith("exit=1") and "Guest limit:" in output
    original = asyncio.create_subprocess_exec
    started = asyncio.get_running_loop().create_future()

    async def recording_spawn(*args, **kwargs):
        proc = await original(*args, **kwargs)
        started.set_result(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_spawn)
    work = asyncio.create_task(execute(plan, config))
    proc = await asyncio.wait_for(started, 5)
    await asyncio.sleep(0.35)
    parent = psutil.Process(proc.pid)
    family = [parent, *parent.children(recursive=True)]
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert all(not p.is_running() or p.status() == psutil.STATUS_ZOMBIE for p in family)


@pytest.mark.parametrize(
    ("language", "entry"),
    [("node", "node"), ("powershell", "pwsh"), ("pytest", "python")],
)
def test_container_transport_uses_only_a_local_engine_and_pinned_readonly_bundle(
    tmp_path, monkeypatch, language, entry
):
    from fleet_node.runner import container_command

    monkeypatch.setattr("fleet_node.runner.shutil.which", lambda _: "docker")
    digest = "sha256:" + "a" * 64
    cfg = {"sandbox_images": {language: digest}}
    base, cmd = container_command(
        cfg, Plan(language, "sample", {}, []), tmp_path, "fleet-test"
    )
    assert base[-1].startswith(("npipe:", "unix:"))
    assert cmd[cmd.index("--entrypoint") + 1 : cmd.index("--entrypoint") + 3] == [
        entry,
        digest,
    ]
    assert (
        "--pull=never" in cmd
        and "--read-only" in cmd
        and cmd[cmd.index("--network") + 1] == "none"
    )
    assert cmd[cmd.index("--mount") + 1].endswith("target=/work,readonly")
    with pytest.raises(ValueError):
        container_command(
            {"sandbox_images": {language: "moving-tag"}},
            Plan(language, "sample", {}, []),
            tmp_path,
            "fleet-test",
        )


@pytest.mark.parametrize(
    ("language", "name", "source"),
    [
        ("node", "sample.js", "console.log(6 * 7)"),
        ("powershell", "sample.ps1", "Write-Output (6 * 7)"),
    ],
)
async def test_balanced_ordinary_container_script_no_extra_approval(
    task, language, name, source
):
    (Path(task.workspace) / name).write_text(source, encoding="utf8")
    cfg = {"sandbox_images": {language: "sha256:" + "a" * 64}}
    app = create_app(
        dict(cfg, secret=secrets.token_urlsafe(32), trusted_workspaces=[task.workspace])
    )
    async with app.router.lifespan_context(app):
        policy = TaskPolicy(
            app.state.node,
            Path(task.workspace),
            lambda *_: pytest.fail("unexpected approval"),
            DshAdapter(cfg),
        )
        plan = await prepare(
            {"script": name, "language": language}, task, cfg, set(), policy.authorize
        )
        assert plan.files[name].decode() == source
