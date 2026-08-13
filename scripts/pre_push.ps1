param()

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$python = @(
    (Join-Path $repoRoot ".venv310\Scripts\python.exe"),
    (Join-Path $repoRoot ".venv\Scripts\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe")
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $python) {
    $command = Get-Command python -ErrorAction SilentlyContinue
    if (-not $command) { throw "Python 3.10 or newer was not found" }
    $python = $command.Source
}

Push-Location $repoRoot
try {
    & $python "scripts/check_public_assets.py"
    if ($LASTEXITCODE -ne 0) { throw "Public asset policy check failed" }
    & $python -m pytest -q --disable-warnings --maxfail=1
    if ($LASTEXITCODE -ne 0) { throw "Fast tests failed; push stopped" }
} finally {
    Pop-Location
}
