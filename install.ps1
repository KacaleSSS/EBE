$ErrorActionPreference = 'Stop'
$ebeInstallRoot = $PSScriptRoot
$ebeVenvPath = Join-Path $ebeInstallRoot '.venv'
python -m venv $ebeVenvPath
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required' }
$ebePython = Join-Path $ebeVenvPath 'Scripts\python.exe'
& $ebePython -m pip install "$ebeInstallRoot[pdf,images]"
if ($LASTEXITCODE -ne 0) { throw 'Installation failed' }
& (Join-Path $ebeVenvPath 'Scripts\ebe.exe') doctor
if ($LASTEXITCODE -ne 0) { throw 'Doctor failed' }
Write-Host 'Ready. Run .\.venv\Scripts\ebe.exe --help'
