$ErrorActionPreference = 'Stop'

function Test-WhisperFlowPython {
    param([string]$Executable, [string[]]$Prefix = @())
    if (-not $Executable) { return $false }
    try {
        & $Executable @Prefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher -and (Test-WhisperFlowPython $launcher.Source @('-3'))) {
    & $launcher.Source -3 "$PSScriptRoot\install.py"
    exit $LASTEXITCODE
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python -and (Test-WhisperFlowPython $python.Source)) {
    & $python.Source "$PSScriptRoot\install.py"
    exit $LASTEXITCODE
}

# A per-user, signed CPython bootstrap keeps this file the only prerequisite on
# a clean Windows installation. The immutable version remains available even
# after newer Python releases appear.
$pythonVersion = '3.13.7'
$bootstrap = Join-Path $env:TEMP "python-$pythonVersion-amd64.exe"
$uri = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-amd64.exe"
Invoke-WebRequest -Uri $uri -OutFile $bootstrap
$signature = Get-AuthenticodeSignature -FilePath $bootstrap
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
    throw 'Die digitale Signatur des Python-Installers ist ungültig.'
}
Start-Process -FilePath $bootstrap -Wait -ArgumentList @(
    '/quiet',
    'InstallAllUsers=0',
    'PrependPath=1',
    'Include_launcher=1',
    'Include_pip=1'
)
$installedPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
if (-not (Test-WhisperFlowPython $installedPython)) {
    throw 'Python konnte nicht automatisch eingerichtet werden.'
}
& $installedPython "$PSScriptRoot\install.py"
exit $LASTEXITCODE
