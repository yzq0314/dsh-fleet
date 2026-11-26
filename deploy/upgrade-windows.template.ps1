[CmdletBinding()]
param([string]$Index = $env:UV_DEFAULT_INDEX, [string]$DshCli, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$aliasName = '@@ALIAS@@'
$taskName = 'DSH Fleet Production ' + $aliasName
$root = Join-Path (Join-Path $env:LOCALAPPDATA 'DSHFleet-Production') $aliasName
$local = Join-Path $root 'local'
$config = Join-Path $local 'node.local.yaml'
$owner = $taskName+' '+[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ([IO.File]::ReadAllText((Join-Path $root 'installation-owner.txt')) -cne $owner) {throw 'Production installation owner mismatch'}
$bundle = [Convert]::FromBase64String('@@PAYLOAD@@')
$fingerprint = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bundle)).Replace('-','').ToLowerInvariant()
if ($fingerprint -cne '@@SHA@@') {throw 'Release archive is damaged'}
$t = Get-ScheduledTask -TaskName $taskName -TaskPath '\'
$old = @($t.Actions)[0]
$releases = Join-Path $root 'releases'
$oldDir = [IO.Path]::GetFullPath($old.WorkingDirectory)
if ($t.Description -cne $owner -or $t.Actions.Count -ne 1 -or $t.Principal.LogonType -ne 'Interactive' -or [IO.Path]::GetDirectoryName($oldDir) -ine $releases -or -not $old.Arguments.Contains($config) -or -not $old.Arguments.Contains((Join-Path $oldDir 'deploy\run-sidecar.ps1'))) {throw 'Existing task differs from the Fleet logon task'}
if ($CheckOnly) {Write-Host 'Archive and production task verified';return}
$lock = [IO.File]::Open((Join-Path $local 'install.lock'),'OpenOrCreate','ReadWrite','None')
try {
    $release = Join-Path $releases ('round4b-'+$fingerprint)
    $zip = Join-Path $local ('upgrade-'+[guid]::NewGuid()+'.zip')
    if (-not [IO.Directory]::Exists($release)) {
        $stage = Join-Path $releases ('.upgrade-'+[guid]::NewGuid())
        [IO.File]::WriteAllBytes($zip,$bundle)
        try {
            Expand-Archive -LiteralPath $zip -DestinationPath $stage -ErrorAction Stop
            [IO.Directory]::Move($stage,$release)
        } finally { [IO.File]::Delete($zip) }
    }
    . (Join-Path $release 'deploy\windows-bootstrap.ps1')
    $uv=Get-FleetUv -ToolsDir (Join-Path $root 'tools')
    Push-Location -LiteralPath $release
    $prior=$env:UV_PROJECT_ENVIRONMENT
    try {
        $env:UV_PROJECT_ENVIRONMENT=Join-Path $release '.venv'
        Sync-FleetDependencies -Uv $uv -Index $Index
    } finally { $env:UV_PROJECT_ENVIRONMENT=$prior;Pop-Location }
    $python=Join-Path $release '.venv\Scripts\python.exe'
    $env:PYTHONUTF8='1'
    $env:PYTHONPATH=$release+';'+(Join-Path $release 'apps\node-sidecar')
    $check = Join-Path $release 'deploy\upgrade-health.py'
    & $python $check $config idle
    if ($LASTEXITCODE) {throw 'Current node is busy or its identity could not be verified'}
    $cliArgs=@()
    if ($DshCli) {$cliArgs=@('--cli',$DshCli)}
    & $python (Join-Path $release 'deploy\upgrade-desktop.py') --config $config @cliArgs
    if ($LASTEXITCODE) {throw 'Desktop update did not verify; current Sidecar remains running'}
    if ($oldDir -ine $release) {
        function Switch-FleetRelease([string]$From,[string]$To) {
            $switchFile = Join-Path $release 'deploy\switch-windows-release.ps1'
            try {
                & $switchFile -AliasName $aliasName -From $From -To $To
            } catch {
                if ($_.CategoryInfo.Category -ne 'PermissionDenied' -and
                    $_.Exception.HResult -ne -2147024891 -and $_.FullyQualifiedErrorId -notmatch '(?i)80070005|AccessDenied') {throw}
                $exe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
                $argsLine = '-NoProfile -NonInteractive -File "'+$switchFile+'" -AliasName '+$aliasName+' -From '+$From+' -To '+$To
                $process = Start-Process -FilePath $exe -ArgumentList $argsLine -WindowStyle Hidden -Verb RunAs -PassThru -Wait
                if ($process.ExitCode) {throw 'Task switch did not complete under the current user'}
            }
        }
        & $python $check $config idle
        if ($LASTEXITCODE) {throw 'New task arrived; retry the upgrade after completion'}
        $fromKey=[IO.Path]::GetFileName($oldDir)
        $toKey=[IO.Path]::GetFileName($release)
        if($fromKey -notmatch '^(round4b-)?[0-9a-f]{64}$') {throw 'Unrecognized production release'}
        Switch-FleetRelease $fromKey $toKey
        & $python $check $config ready
        if ($LASTEXITCODE) {
            Switch-FleetRelease $toKey $fromKey
            throw 'Health verification failed; restored previous launch action'
        }
    } else {
        & $python $check $config ready
        if ($LASTEXITCODE) {throw 'Existing release health failed'}
    }
    Write-Host 'Fleet Sidecar authenticated and ready.'
} finally { $lock.Dispose() }
