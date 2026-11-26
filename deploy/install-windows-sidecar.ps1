[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Config,
    [string]$Hub, [string]$Token = $env:FLEET_JOIN_TOKEN, [string]$Url,
    [string]$Alias = $env:COMPUTERNAME, [string[]]$Tag = @(),
    [string]$BindIp, [string[]]$Cidr = @(), [string[]]$Workspace = @(),
    [string]$TaskName = 'DSH Fleet Sidecar', [switch]$SkipFirewall, [string]$Index = $env:UV_DEFAULT_INDEX
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$configPath = [IO.Path]::GetFullPath($Config)
. (Join-Path $PSScriptRoot 'windows-bootstrap.ps1')
if (-not $SkipFirewall) {
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'To configure the firewall, run PowerShell as the intended user with elevation, or use -SkipFirewall with a preconfigured rule.'
    }
}
Push-Location -LiteralPath $repo
try {
    $uv = Get-FleetUv
    Sync-FleetDependencies -Uv $uv -Index $Index -Sandbox
    $python = Join-Path $repo '.venv\Scripts\python.exe'
    & $python -c 'import sys; assert sys.version_info >= (3,11)'
    if ($LASTEXITCODE) { throw 'Python 3.11 or newer is required' }
    if (-not (Test-Path -LiteralPath $configPath)) {
        if (-not ($Hub -and $Token -and $Url)) { throw 'A new config requires -Hub, -Url and -Token (or FLEET_JOIN_TOKEN).' }
        $pairArgs = @((Join-Path $repo 'deploy\pair.py'),'--hub',$Hub,'--url',$Url,'--alias',$Alias,'--config',$configPath)
        foreach ($entry in $Cidr) { $pairArgs += @('--cidr',$entry) }
        foreach ($entry in $Workspace) { $pairArgs += @('--workspace',$entry) }
        foreach ($entry in $Tag) { $pairArgs += @('--tag',$entry) }
        if ($BindIp) { $pairArgs += @('--bind-ip',$BindIp) }
        $oldJoin = $env:FLEET_JOIN_TOKEN
        try {
            $env:FLEET_JOIN_TOKEN = $Token
            & $python @pairArgs
            if ($LASTEXITCODE) { throw 'Pairing failed' }
        } finally { $env:FLEET_JOIN_TOKEN = $oldJoin }
    }
    Set-FleetPrivateDacl -Path $configPath
    if (-not $SkipFirewall) {
        $net = & $python -c "import json,sys,yaml; d=yaml.safe_load(open(sys.argv[1],encoding='utf-8-sig')); print(json.dumps(dict(port=d['port'],cidrs=d['allowed_cidrs'])))" $configPath | ConvertFrom-Json
        if ($LASTEXITCODE -or -not $net.cidrs) { throw 'Invalid firewall configuration' }
        $rule = "DSHFleet-$($net.port)"
        if (Get-NetFirewallRule -Name $rule -ErrorAction SilentlyContinue) { Remove-NetFirewallRule -Name $rule }
        New-NetFirewallRule -Name $rule -DisplayName "Fleet Sidecar $($net.port)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $net.port -Program $python -RemoteAddress $net.cidrs -Profile Any | Out-Null
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $launcher = Join-Path $PSScriptRoot 'run-sidecar.ps1'
    $cmdArgs = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "{0}" -Config "{1}"' -f $launcher,$configPath
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $cmdArgs -WorkingDirectory $repo
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    if ((Get-ScheduledTask -TaskName $TaskName).State -eq 'Running') { Stop-ScheduledTask -TaskName $TaskName }
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Registered $TaskName for $identity. Check deploy/doctor.py after startup."
} finally { Pop-Location }
