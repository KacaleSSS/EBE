$ErrorActionPreference = 'Stop'
$ebeInstallRoot = $PSScriptRoot
$ebeVenvPath = Join-Path $ebeInstallRoot '.venv'
$ebeInterpreter = if ($env:EBE_PYTHON) { $env:EBE_PYTHON } else { 'python' }
& $ebeInterpreter -c 'import sys; assert sys.version_info >= (3, 11)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required; set EBE_PYTHON to its executable' }
if (Test-Path -LiteralPath $ebeVenvPath) {
    & (Join-Path $ebeVenvPath 'Scripts\python.exe') -c 'import sys; assert sys.version_info >= (3, 11)'
    if ($LASTEXITCODE -ne 0) { throw 'Existing .venv is incompatible. Use a fresh extracted directory; no files were deleted.' }
}
& $ebeInterpreter -m venv $ebeVenvPath
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required' }
$ebePython = Join-Path $ebeVenvPath 'Scripts\python.exe'
& $ebePython -m pip install --upgrade 'pip>=23'
if ($LASTEXITCODE -ne 0) { throw 'pip preparation failed' }
& $ebePython -m pip install "$ebeInstallRoot[pdf,images]"
if ($LASTEXITCODE -ne 0) { throw 'Installation failed' }
& (Join-Path $ebeVenvPath 'Scripts\ebe.exe') doctor
if ($LASTEXITCODE -ne 0) { throw 'Doctor failed' }
Write-Host 'Ready. Run .\.venv\Scripts\ebe.exe --help'
