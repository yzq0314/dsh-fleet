"""Exercise the deployment entry points under Windows PowerShell 5.1 without changing the host."""
import contextlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]
PS = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(os.name != "nt" or not PS, reason="Windows PowerShell 5.1")


def shell(tmp, script, **env):
    harness = tmp / "check.ps1"
    harness.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'PowerShell 5.1 required' }\n"
        ". $env:FLEET_TEST_HELPERS\n" + script,
        encoding="utf-8-sig",
    )
    return subprocess.run(
        [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        cwd=tmp,
        env={**os.environ, **env, "FLEET_TEST_ROOT": str(tmp),
             "PSModuleAnalysisCachePath": str(tmp / "ps-cache"),
             "FLEET_TEST_HELPERS": str(ROOT / "deploy/windows-bootstrap.ps1")},
        capture_output=True, text=True, errors="replace", timeout=90,
    )


def test_only_dacl_is_set_on_file_and_directory(tmp_path):
    result = shell(tmp_path, r"""
$root = Join-Path $env:FLEET_TEST_ROOT 'site'
[void][IO.Directory]::CreateDirectory($root)
$file = Join-Path $root 'node.yaml'
[IO.File]::WriteAllText($file, 'synthetic')
$current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
foreach ($target in @($root, $file)) {
    $item = Get-Item -LiteralPath $target
    $before = $item.GetAccessControl()
    Set-FleetPrivateDacl $target
    $after = $item.GetAccessControl()
    if ($after.Owner -ne $before.Owner -or $after.Group -ne $before.Group) { throw 'Ownership changed' }
    if (-not $after.AreAccessRulesProtected) { throw 'Inherited permissions' }
    $rules = @($after.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
    if ($rules.Count -ne 2 -or $rules.IdentityReference.Value -notcontains $current -or
        $rules.IdentityReference.Value -notcontains 'S-1-5-18') { throw 'Unexpected trustees' }
    foreach ($rule in $rules) {
        if ($rule.FileSystemRights -ne 'FullControl' -or $rule.AccessControlType -ne 'Allow') { throw 'Unexpected access' }
    }
}
if ([IO.File]::ReadAllText($file) -ne 'synthetic') { throw 'Modified contents' }
""")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("status", [0, 29])
def test_redirected_native_log_preserves_lifetime_and_exit_code(tmp_path, status):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    shutil.copy(ROOT / "deploy/run-sidecar.ps1", deploy)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(tmp_path / ".venv")], check=True)
    module = tmp_path / "apps/node-sidecar/fleet_node"
    module.mkdir(parents=True)
    (module / "main.py").write_text(
        "from pathlib import Path\nimport sys, time\n"
        "print('INFO: test server started', file=sys.stderr, flush=True)\n"
        "time.sleep(.15)\nPath('completed').touch()\n"
        f"sys.exit({status})\n", encoding="ascii")
    result = shell(tmp_path, r"""
& (Join-Path $env:FLEET_TEST_ROOT 'deploy/run-sidecar.ps1') -Config 'test.yaml' *> 'node.log'
exit $LASTEXITCODE
""")
    assert result.returncode == status, result.stderr
    assert (tmp_path / "completed").is_file()
    assert "INFO: test server started" in (tmp_path / "node.log").read_text(encoding="utf-16")


@contextlib.contextmanager
def github_response(status):
    requests = []
    class Reply(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Reply)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/meta"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert requests == ["/meta"]


def local_uv_wheel(directory):
    name = "uv-0.11.1.dist-info"
    files = {
        "uv_fixture.py": "def main(): print('isolated uv fixture')\n",
        f"{name}/METADATA": "Metadata-Version: 2.1\nName: uv\nVersion: 0.11.1\n",
        f"{name}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{name}/entry_points.txt": "[console_scripts]\nuv = uv_fixture:main\n",
    }
    record = f"{name}/RECORD"
    files[record] = "".join(f"{key},,\n" for key in [*files, record])
    with ZipFile(directory / "uv-0.11.1-py3-none-any.whl", "w") as archive:
        for key, content in files.items():
            archive.writestr(key, content)


@pytest.mark.parametrize(("http_status", "winget_ok"), [(403, True), (403, False), (429, False), (200, True)])
def test_offline_uv_fallback(tmp_path, http_status, winget_ok):
    # Route the actual PS 5.1 HTTP cmdlet to loopback. Only replace discovery of host tools.
    (tmp_path / "winget.cmd").write_text(
        '@echo off\necho %* > "%FLEET_TEST_ROOT%\\winget-call"\n'
        + ('mkdir "%LOCALAPPDATA%\\Microsoft\\WinGet\\Links"\n'
           'copy /Y "%SystemRoot%\\System32\\where.exe" "%LOCALAPPDATA%\\Microsoft\\WinGet\\Links\\uv.exe" >nul\nexit /b 0\n'
           if winget_ok else 'echo unavailable 1>&2\nexit /b 1\n'), encoding="ascii")
    local_uv_wheel(tmp_path)
    with github_response(http_status) as url:
        result = shell(tmp_path, r"""
function Get-Command($Name, $CommandType, $ErrorAction) {
    switch ($Name) {
        'winget.exe' { return [pscustomobject]@{Source=(Join-Path $env:FLEET_TEST_ROOT 'winget.cmd')} }
        'python.exe' { return [pscustomobject]@{Source=$env:FLEET_TEST_PYTHON} }
        'py.exe' { return }
        'uv.exe' { return }
        default { throw 'Unexpected tool lookup' }
    }
}
function Invoke-RestMethod($Uri, $Headers, $TimeoutSec, $ErrorAction) {
    if ($Uri -notlike 'https://api.github.com/repos/astral-sh/uv/releases/tags/*') { throw 'Unexpected URI' }
    Microsoft.PowerShell.Utility\Invoke-RestMethod -Uri $env:FLEET_TEST_GITHUB -ErrorAction Stop
}
$uv = Get-FleetUv -ToolsDir (Join-Path $env:FLEET_TEST_ROOT 'tools')
if ($uv -isnot [string] -or -not [IO.File]::Exists($uv)) { throw 'No single executable returned' }
[IO.File]::WriteAllText((Join-Path $env:FLEET_TEST_ROOT 'resolved.txt'), $uv)
""", FLEET_TEST_GITHUB=url, FLEET_TEST_PYTHON=sys.executable,
           LOCALAPPDATA=str(tmp_path / "profile"), PROCESSOR_ARCHITECTURE="AMD64", PROCESSOR_ARCHITEW6432="",
           PIP_NO_INDEX="1", PIP_FIND_LINKS=str(tmp_path))
    if http_status == 200:
        assert result.returncode != 0 and not (tmp_path / "winget-call").exists()
        assert "checksum" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    assert "--id astral-sh.uv --exact" in (tmp_path / "winget-call").read_text()
    uv = Path((tmp_path / "resolved.txt").read_text())
    if winget_ok:
        assert uv.parent.name == "Links"
    else:
        assert subprocess.check_output([str(uv)], text=True).strip() == "isolated uv fixture"
