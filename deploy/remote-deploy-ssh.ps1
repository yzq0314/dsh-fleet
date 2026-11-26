[CmdletBinding(PositionalBinding=$false)]
param(
    [Parameter(Mandatory)][string[]]$Target,
    [string]$Bootstrap = (Join-Path $PSScriptRoot '..\local\bootstrap.local.yaml'),
    [string]$Config, [string]$RemoteDir = '.dsh-fleet-releases',
    [Alias('dry-run')][switch]$DryRun,
    [Parameter(ValueFromRemainingArguments)][string[]]$Options
)
$ErrorActionPreference = 'Stop'
foreach ($option in $Options) {
    if ($option -ne '--dry-run') { throw "Unknown option: $option" }
    $DryRun = $true
}
$uv = (Get-Command uv -ErrorAction Stop).Source
$argsToPass = @('run','--project',(Join-Path $PSScriptRoot '..'),'python',(Join-Path $PSScriptRoot 'remote_stage.py'),'--bootstrap',$Bootstrap,'--target',($Target -join ','),'--remote-dir',$RemoteDir)
if ($Config) { $argsToPass += @('--config',$Config) }
if ($DryRun) { $argsToPass += '--dry-run' }
& $uv @argsToPass
exit $LASTEXITCODE
