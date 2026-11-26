function Sync-FleetDependencies {
    param([string]$Uv, [string]$Index = $env:UV_DEFAULT_INDEX, [switch]$Sandbox)
    $extra = @()
    if ($Sandbox) { $extra = @('--extra','sandbox') }
    $certs = $env:UV_SYSTEM_CERTS
    try {
        if (-not $certs) { $env:UV_SYSTEM_CERTS = 'true' }
        if (-not $Index) {
            & $Uv sync --locked --no-dev @extra
            if ($LASTEXITCODE) { throw 'Locked sync failed' }
            return
        }
        # Preserve artifact hashes from the lock while letting pip select the mirror.
        $requirements = [IO.Path]::GetTempFileName()
        try {
            & $Uv export --frozen --no-dev @extra --no-emit-project --format requirements-txt --output-file $requirements | Out-Null
            if ($LASTEXITCODE) { throw 'Dependency export failed' }
            $target = if ($env:UV_PROJECT_ENVIRONMENT) { $env:UV_PROJECT_ENVIRONMENT } else { '.venv' }
            $python = Join-Path $target 'Scripts\python.exe'
            if (-not (Test-Path -LiteralPath $python)) {
                & $Uv venv $target --python '>=3.11'
                if ($LASTEXITCODE) { throw 'Virtual environment creation failed' }
            }
            & $Uv pip sync --python $python --require-hashes --default-index $Index $requirements
            if ($LASTEXITCODE) { throw 'Mirror dependency sync failed' }
            & $Uv pip install --python $python --no-deps --default-index $Index -e .
            if ($LASTEXITCODE) { throw 'Fleet installation failed' }
        } finally { [IO.File]::Delete($requirements) }
    } finally { $env:UV_SYSTEM_CERTS = $certs }
}
# Windows PowerShell 5.1 deployment helpers. Dot-source from the current user's installer.
function Set-FleetPrivateDacl {
    param([Parameter(Mandatory)][string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Refusing a linked ACL target.' }
    $acl = if ($item.PSIsContainer) {
        [Security.AccessControl.DirectorySecurity]::new()
    } else {
        [Security.AccessControl.FileSecurity]::new()
    }
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = if ($item.PSIsContainer) { 'ContainerInherit,ObjectInherit' } else { 'None' }
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    foreach ($sid in @($user, $system)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl',$inherit,'None','Allow'))
    }
    # Only the Access section has been modified. Leave owner, group and SACL untouched.
    if ($PSVersionTable.PSEdition -eq 'Desktop') {
        $item.SetAccessControl($acl)
    } else {
        [IO.FileSystemAclExtensions]::SetAccessControl($item, $acl)
    }
}

function Install-FleetUvWithPackageManager {
    param([string]$ToolsDir, [string]$Version)
    $ErrorActionPreference = 'Continue'
    $winget = Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($winget) {
        & $winget.Source install --id astral-sh.uv --exact --source winget --scope user --accept-source-agreements --accept-package-agreements --disable-interactivity | Out-Host
        if ($LASTEXITCODE -eq 0) {
            $installed = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue
            if ($installed) { return $installed.Source }
            $linked = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe'
            if ([IO.File]::Exists($linked)) { return $linked }
        }
    }
    $python = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
    $flags = @('-3')
    if (-not $python) {
        $python = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue
        $flags = @()
    }
    if (-not $python) { throw 'uv installation requires WinGet or Python with venv and pip.' }
    $envDir = Join-Path $ToolsDir 'uv-python'
    & $python.Source @flags -m venv $envDir | Out-Host
    if ($LASTEXITCODE) { throw 'Unable to create the private uv Python environment.' }
    & (Join-Path $envDir 'Scripts\python.exe') -m pip install --disable-pip-version-check --only-binary=:all: "uv==$Version" | Out-Host
    $installed = Join-Path $envDir 'Scripts\uv.exe'
    if ($LASTEXITCODE -or -not [IO.File]::Exists($installed)) { throw 'Unable to install uv with pip.' }
    return $installed
}

function Get-FleetUv {
    param([string]$ToolsDir = (Join-Path $env:LOCALAPPDATA 'DSHFleet\tools'))
    $installed = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($installed) { return $installed.Source }
    $ToolsDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ToolsDir)
    $version = '0.11.1'
    $destination = Join-Path $ToolsDir "uv-$version.exe"
    foreach ($path in @($destination, (Join-Path $ToolsDir 'uv-python\Scripts\uv.exe'))) {
        if ([IO.File]::Exists($path)) { return $path }
    }
    [void][IO.Directory]::CreateDirectory($ToolsDir)
    $arch = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    if ($arch -notin @('X64','Arm64')) { return Install-FleetUvWithPackageManager $ToolsDir $version }
    $target = if ($arch -eq 'ARM64') { 'aarch64' } else { 'x86_64' }
    $asset = "uv-$target-pc-windows-msvc.zip"
    $url = "https://github.com/astral-sh/uv/releases/download/$version/$asset"
    try {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/astral-sh/uv/releases/tags/$version" -Headers @{'User-Agent'='dsh-fleet-bootstrap'} -TimeoutSec 20 -ErrorAction Stop
    } catch {
        $response = $_.Exception.Response
        if ($null -eq $response -or [int]$response.StatusCode -notin @(403,429)) { throw }
        return Install-FleetUvWithPackageManager $ToolsDir $version
    }
    $entry = @($release.assets | Where-Object { $_.name -ceq $asset -and $_.browser_download_url -ceq $url })
    if ($entry.Count -ne 1 -or $entry[0].digest -notmatch '^sha256:([0-9a-f]{64})$') {
        throw 'The official uv release metadata does not contain an archive checksum.'
    }
    $expected = $Matches[1]
    $zip = Join-Path $ToolsDir ([guid]::NewGuid().ToString() + '.zip')
    $exe = $zip + '.exe'
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip -TimeoutSec 120 -ErrorAction Stop
        if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ine $expected) { throw 'uv archive checksum mismatch.' }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [IO.Compression.ZipFile]::OpenRead($zip)
        try {
            $bins = @($archive.Entries | Where-Object { $_.Name -ceq 'uv.exe' })
            if ($bins.Count -ne 1) { throw 'Unexpected uv package layout.' }
            [IO.Compression.ZipFileExtensions]::ExtractToFile($bins[0], $exe)
        } finally { $archive.Dispose() }
        try { [IO.File]::Move($exe, $destination) }
        catch [IO.IOException] {
            if (-not [IO.File]::Exists($destination) -or
                (Get-FileHash -LiteralPath $destination).Hash -ne (Get-FileHash -LiteralPath $exe).Hash) { throw }
        }
    } finally {
        [IO.File]::Delete($zip)
        [IO.File]::Delete($exe)
    }
    return $destination
}
