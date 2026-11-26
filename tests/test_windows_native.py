"""Exercise native Windows interpreters with no container engine running."""

import asyncio
import os
import socket
from types import SimpleNamespace

import psutil
import pytest

from fleet_node.runner import Plan, available, execute, prepare

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="Requires Windows AppContainer and Job objects"
)


@pytest.mark.parametrize(
    ("language", "name", "source"),
    [
        ("python", "probe.py", "print(sum([35, 7]))"),
        ("powershell", "probe.ps1", "Write-Output (35 + 7)"),
        ("node", "probe.js", "console.log(35 + 7)"),
    ],
)
async def test_balanced_uses_real_installed_interpreter(
    tmp_path, language, name, source, monkeypatch
):
    if language not in available({}):
        pytest.skip(f"{language} is not installed")
    from fleet_node import runner

    def no_docker(*args):
        pytest.fail("Default Windows balanced execution tried to use Docker")

    monkeypatch.setattr(runner, "container_command", no_docker)
    (tmp_path / name).write_text(source, encoding="utf8")

    async def authorize(command, paths, high_risk):
        assert not high_risk
        assert paths == [str(tmp_path / name)]
        return True

    plan = await prepare(
        {"script": name, "language": language},
        SimpleNamespace(workspace=str(tmp_path)),
        {},
        set(),
        authorize,
    )
    output = await execute(plan, {})
    assert output.splitlines() == ["exit=0", "42"], output


async def test_native_private_workspace_network_and_job_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_NATIVE_TEST_PRIVATE", "private")
    secret = tmp_path / "undeclared.txt"
    secret.write_text("synthetic-private")
    addendum = tmp_path / "added.txt"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.1)
        source = f"""
import os, socket, subprocess
assert os.environ.get("FLEET_NATIVE_TEST_PRIVATE") is None
assert open("input.txt").read() == "sample"
for path, mode in [({str(secret)!r}, "r"), ("input.txt","w"), ({str(addendum)!r}, "w")]:
    try:
        open(path, mode).close()
    except OSError:
        pass
    else:
        raise AssertionError("unexpected filesystem capability")
try:
    socket.create_connection(("127.0.0.1", {listener.getsockname()[1]}), timeout=1).close()
except OSError:
    pass
else:
    raise AssertionError("network access")
try:
    subprocess.run([os.path.join(os.environ["SystemRoot"], "System32", "hostname.exe")], check=True)
except OSError:
    pass
else:
    raise AssertionError("child process permitted")
print("isolated")
"""
        result = await execute(
            Plan(
                "python",
                "probe.py",
                {"probe.py": source.encode(), "input.txt": b"sample"},
                [],
            ),
            {},
        )
        assert result.splitlines() == ["exit=0", "isolated"], result
        with pytest.raises(socket.timeout):
            listener.accept()
        assert secret.read_text() == "synthetic-private" and not addendum.exists()


@pytest.mark.parametrize("language", ["node", "powershell"])
async def test_interpreters_read_only_declared_file_and_deny_network(
    tmp_path, language
):
    if language not in available({}):
        pytest.skip(f"{language} is not installed")
    private = tmp_path / "private.txt"
    private.write_text("private")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.1)
        port = str(listener.getsockname()[1])
        if language == "node":
            script = "files.js"
            source = """
const fs = require('node:fs');
const net = require('node:net');
const args = process.argv.slice(2);
if (fs.readFileSync('shared.txt', 'utf8') !== 'fixture') throw Error('input');
if (fs.existsSync(args[0])) throw Error('private file visible');
const connection = net.createConnection(Number(args[1]), '127.0.0.1');
connection.once('connect', () => { connection.destroy(); process.exitCode = 2; });
connection.once('error', () => console.log(args[2]));
"""
        else:
            script = "files.ps1"
            source = """
param($privatePath, $portNumber, $message)
$ErrorActionPreference = 'Stop'
if ((Get-Content -LiteralPath 'shared.txt' -Raw) -ne 'fixture') { throw 'input' }
if ([IO.File]::Exists($privatePath)) { throw 'private file visible' }
$client = $null
$connected = $false
try {
    $client = New-Object System.Net.Sockets.TcpClient
    $client.Connect('127.0.0.1', [int]$portNumber)
    $connected = $true
} catch { } finally { if ($null -ne $client) { $client.Close() } }
if ($connected) { throw 'network access' }
Write-Output $message
"""
        message = 'sample "参数" 42'
        plan = Plan(
            language,
            script,
            {script: source.encode(), "shared.txt": b"fixture"},
            [str(private), port, message],
        )
        result = await execute(plan, {})
        assert result.splitlines() == ["exit=0", message], result
        with pytest.raises(socket.timeout):
            listener.accept()


async def test_native_cancel_joins_job(monkeypatch):
    from fleet_node import windows_guest

    started = asyncio.get_running_loop().create_future()
    loop = asyncio.get_running_loop()
    original = windows_guest.read_output

    def record(proc, *args):
        from ctypes import wintypes

        get_pid = windows_guest.win_function(
            windows_guest.kernel, "GetProcessId", wintypes.DWORD, wintypes.HANDLE
        )
        pid = get_pid(proc)
        loop.call_soon_threadsafe(started.set_result, pid)
        return original(proc, *args)

    monkeypatch.setattr(windows_guest, "read_output", record)
    running = asyncio.create_task(
        execute(
            Plan("python", "long.py", {"long.py": b"import time; time.sleep(60)"}, []),
            {},
        )
    )
    try:
        pid = await asyncio.wait_for(started, 30)
        child = psutil.Process(pid)
        assert child.is_running()
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert not child.is_running()
