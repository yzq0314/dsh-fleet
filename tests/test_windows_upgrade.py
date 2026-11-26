"""Exercise the task action swap without touching the user's scheduler."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5.1")
def test_upgrade_switch_preserves_task_principal_and_config(tmp_path):
    script = tmp_path / "case.ps1"
    script.write_text(r"""
$ErrorActionPreference='Stop'
$aliasName='fixture'
$name='DSH Fleet Production fixture'
$owner=$name+' '+[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$root=Join-Path (Join-Path $env:LOCALAPPDATA 'DSHFleet-Production') $aliasName
$from='a'*64
$to='round4b-'+'b'*64
$old=Join-Path (Join-Path $root 'releases') $from
$new=Join-Path (Join-Path $root 'releases') $to
[void][IO.Directory]::CreateDirectory((Join-Path $new 'deploy'))
[IO.File]::WriteAllText((Join-Path $root 'installation-owner.txt'),$owner)
[IO.File]::WriteAllText((Join-Path $new 'deploy/run-sidecar.ps1'),'fixture')
$config=Join-Path $root 'local\node.local.yaml'
$launcher=Join-Path $old 'deploy\run-sidecar.ps1'
$global:FleetTestTask=[pscustomobject]@{Actions=@([pscustomobject]@{WorkingDirectory=$old;Execute='powershell.exe';Arguments=('-File "'+$launcher+'" -Config "'+$config+'"')});Description=$owner;Principal=[pscustomobject]@{LogonType='Interactive';RunLevel='Limited'};State='Running'}
function Get-ScheduledTask($TaskName,$TaskPath){return $global:FleetTestTask}
function New-ScheduledTaskAction($Execute,$Argument,$WorkingDirectory){[pscustomobject]@{Execute=$Execute;Arguments=$Argument;WorkingDirectory=$WorkingDirectory}}
function Set-ScheduledTask($TaskName,$TaskPath,$Action){$global:FleetTestTask.Actions=@($Action)}
function Stop-ScheduledTask($TaskName){$global:FleetTestTask.State='Ready'}
function Start-ScheduledTask($TaskName){$global:FleetTestTask.State='Running'}
& $env:FLEET_TEST_SWITCH -AliasName $aliasName -From $from -To $to
if($global:FleetTestTask.Actions[0].WorkingDirectory -ne $new -or -not $global:FleetTestTask.Actions[0].Arguments.Contains($config) -or $global:FleetTestTask.State -ne 'Running'){throw 'Incorrect action swap'}
$global:FleetTestTask.Principal.RunLevel='Highest'
$rejected=$false
try {& $env:FLEET_TEST_SWITCH -AliasName $aliasName -From $to -To $from} catch {$rejected=$true}
if(-not $rejected){throw 'Unexpected elevated principal accepted'}
""",encoding="utf-8-sig")
    subprocess.run([shutil.which("powershell.exe"),"-NoProfile","-NonInteractive","-File",str(script)],
                   check=True, timeout=20, env={**os.environ,"LOCALAPPDATA":str(tmp_path),
                      "FLEET_TEST_SWITCH":str(ROOT/"deploy/switch-windows-release.ps1")})
