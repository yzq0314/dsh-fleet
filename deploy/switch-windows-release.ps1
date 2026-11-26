# Switch only the action on an existing limited interactive Fleet task.
[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^[a-zA-Z0-9_-]{1,80}$')][string]$AliasName,
      [Parameter(Mandatory)][ValidatePattern('^(round4b-)?[0-9a-f]{64}$')][string]$From,
      [Parameter(Mandatory)][ValidatePattern('^(round4b-)?[0-9a-f]{64}$')][string]$To)
$ErrorActionPreference = 'Stop'
$name = 'DSH Fleet Production ' + $AliasName
$site = Join-Path (Join-Path $env:LOCALAPPDATA 'DSHFleet-Production') $AliasName
$owner = $name+' '+[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ([IO.File]::ReadAllText((Join-Path $site 'installation-owner.txt')) -cne $owner) {throw 'Use the same signed-in desktop user'}
$task = Get-ScheduledTask -TaskName $name -TaskPath '\'
$action = @($task.Actions)[0]
$oldDir = Join-Path (Join-Path $site 'releases') $From
$newDir = Join-Path (Join-Path $site 'releases') $To
$config = Join-Path $site 'local\node.local.yaml'
if ($task.Actions.Count -ne 1 -or $task.Description -cne $owner -or
    $task.Principal.LogonType -ne 'Interactive' -or $task.Principal.RunLevel -ne 'Limited' -or
    $action.WorkingDirectory -ine $oldDir -or -not $action.Arguments.Contains((Join-Path $oldDir 'deploy\run-sidecar.ps1')) -or
    -not $action.Arguments.Contains($config) -or -not (Test-Path -LiteralPath (Join-Path $newDir 'deploy\run-sidecar.ps1'))) {
    throw 'Existing registration differs from this upgrade'
}
$new = New-ScheduledTaskAction -Execute $action.Execute -Argument $action.Arguments.Replace($oldDir,$newDir) -WorkingDirectory $newDir
Set-ScheduledTask -TaskName $name -TaskPath '\' -Action $new | Out-Null
try {
    Stop-ScheduledTask -TaskName $name
    $limit=(Get-Date).AddSeconds(20)
    while ((Get-ScheduledTask -TaskName $name).State -eq 'Running' -and (Get-Date) -lt $limit) {Start-Sleep -Milliseconds 200}
    Start-ScheduledTask -TaskName $name
} catch {
    Set-ScheduledTask -TaskName $name -TaskPath '\' -Action $action | Out-Null
    Start-ScheduledTask -TaskName $name
    throw
}
