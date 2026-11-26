param([Parameter(Mandatory)][string]$Config)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$env:PYTHONPATH = "$repo;$repo\apps\node-sidecar"
Set-Location -LiteralPath $repo
$python = (Get-Item -LiteralPath (Join-Path $repo '.venv\Scripts\python.exe')).FullName
$ErrorActionPreference = 'Continue'
& $python -m fleet_node.main --config $Config
exit $LASTEXITCODE
