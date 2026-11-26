"""Check the interpreter bundle independently of the AppContainer launch tests."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from fleet_node.runner import Plan
from fleet_node.windows_runtime import native_languages, stage_command

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native runtime layout")


@pytest.mark.parametrize(
    ("language", "filename", "program"),
    [
        (
            "python",
            "summary.py",
            "import json, sys\nfrom helper import amount\n"
            "print(amount * 7)\nprint(json.dumps(sys.path))",
        ),
        ("node", "summary.js", 'console.log(require("./helper.js").amount * 7)'),
        ("powershell", "summary.ps1", "Write-Output (6 * 7)"),
    ],
)
def test_private_runtime_starts_without_host_search_paths(
    tmp_path, language, filename, program
):
    if language not in native_languages():
        pytest.skip(f"{language} unavailable on this node")
    work = tmp_path / "work"
    work.mkdir()
    (work / filename).write_text(program, encoding="utf8")
    (work / "helper.py").write_text("amount = 6", encoding="utf8")
    (work / "helper.js").write_text("exports.amount = 6", encoding="utf8")
    cmd = stage_command(Plan(language, filename, {}, []), tmp_path, work)
    env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR"}}
    env.update(
        PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
        TEMP=str(tmp_path),
        TMP=str(tmp_path),
    )
    proc = subprocess.run(cmd, cwd=work, env=env, capture_output=True, timeout=25)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.decode("utf8").splitlines()
    assert lines[0] == "42"
    if language == "python":
        assert all(
            Path(item).resolve().is_relative_to(tmp_path)
            for item in json.loads(lines[1])
        )
    if language in {"python", "node"}:
        assert Path(cmd[0]).parent == tmp_path / "runtime"


def test_cannot_launch_outside_snapshot(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "other.py").write_text("print(42)", encoding="utf8")
    with pytest.raises(ValueError, match="outside"):
        stage_command(Plan("python", "../other.py", {}, []), tmp_path, work)


async def test_powershell_native_utf8_arguments_relative_files_and_exit(tmp_path):
    if "powershell" not in native_languages():
        pytest.skip("Windows PowerShell SDK unavailable")
    from fleet_node.windows_guest import run_native

    work = tmp_path / "work"
    work.mkdir()
    script = "summary.ps1"
    source = (
        "param([string]$InputValue)\n"
        '$ErrorActionPreference = "Stop"\n'
        'Write-Output ("结果 " + (Get-Content ./input.txt) + " " + $InputValue)\n'
        "exit 29\n"
    ).encode("utf-8")
    argument = '标记 "quoted value"; literal $variable'
    (work / script).write_bytes(source)
    (work / "input.txt").write_text("42", encoding="ascii")
    plan = Plan("powershell", script, {script: source}, [argument])
    output = await run_native(plan, tmp_path, work, 8192)
    assert output.splitlines() == ["exit=29", "结果 42 " + argument]
    assert plan.files[script] == source


@pytest.mark.parametrize(
    ("source", "exit_code"),
    [
        (
            'try { [IO.File]::ReadAllText("missing.txt") } catch { Write-Output "handled" }',
            0,
        ),
        ('throw "uncaught"', 1),
    ],
)
async def test_native_powershell_script_error_status(tmp_path, source, exit_code):
    if "powershell" not in native_languages():
        pytest.skip("Windows PowerShell SDK unavailable")
    from fleet_node.windows_guest import run_native

    work = tmp_path / "work"
    work.mkdir()
    (work / "errors.ps1").write_text(source, encoding="ascii")
    result = await run_native(
        Plan("powershell", "errors.ps1", {}, []), tmp_path, work, 8192
    )
    assert result.splitlines() == [
        f"exit={exit_code}",
        "handled" if exit_code == 0 else "uncaught",
    ]
