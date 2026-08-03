param([switch]$SkipInstaller)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$python = "C:\Users\cruelworld\AppData\Local\Programs\Python\Python310\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python).Source }
Push-Location $repoRoot
try {
    & $python -m pip install -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { throw "Development dependency installation failed" }
    $sourceRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "src"))
    Get-ChildItem -LiteralPath $sourceRoot -Recurse -Directory -Filter "__pycache__" | ForEach-Object {
        $cachePath = [System.IO.Path]::GetFullPath($_.FullName)
        if (-not $cachePath.StartsWith($sourceRoot, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe cache path: $cachePath" }
        Remove-Item -LiteralPath $cachePath -Recurse -Force
    }
    & $python -m PyInstaller --noconfirm --clean --onedir --windowed --name LocalVoiceStudio --paths src --add-data "scripts;scripts" --add-data "src/local_voice_studio;worker_source/local_voice_studio" --exclude-module torch --exclude-module torchaudio --exclude-module torchvision --exclude-module numpy launcher.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
    Copy-Item -LiteralPath "THIRD_PARTY_NOTICES.md" -Destination "dist\LocalVoiceStudio" -Force
    if (-not $SkipInstaller) {
        $iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $iscc) { throw "Inno Setup 6 was not found; use -SkipInstaller to build onedir only" }
        & $iscc "installer\LocalVoiceStudio.iss"
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup build failed" }
    }
} finally { Pop-Location }
