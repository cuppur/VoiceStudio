param(
    [switch]$SkipInstaller,
    [switch]$Release,
    [ValidatePattern('^[0-9A-Fa-f]{40}$')][string]$CertificateThumbprint,
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$python = @(
    (Join-Path $repoRoot ".venv310\Scripts\python.exe"),
    (Join-Path $repoRoot ".venv\Scripts\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe")
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $python) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw "Python 3.10 or newer was not found" }
    $python = $pythonCommand.Source
}
Push-Location $repoRoot
try {
    if ($Release -and $SkipInstaller) { throw "Release builds must include the signed installer" }
    if ($Release -and -not $CertificateThumbprint) { throw "Release builds require -CertificateThumbprint" }
    & $python -m pip install -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { throw "Development dependency installation failed" }
    $sourceRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "src"))
    Get-ChildItem -LiteralPath $sourceRoot -Recurse -Directory -Filter "__pycache__" | ForEach-Object {
        $cachePath = [System.IO.Path]::GetFullPath($_.FullName)
        if (-not $cachePath.StartsWith($sourceRoot, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe cache path: $cachePath" }
        Remove-Item -LiteralPath $cachePath -Recurse -Force
    }
    $appIcon = Join-Path $repoRoot "assets\voicestudio.ico"
    if (-not (Test-Path -LiteralPath $appIcon -PathType Leaf)) { throw "Application icon is missing: $appIcon" }
    & $python -m PyInstaller --noconfirm --clean --onedir --windowed --name LocalVoiceStudio --icon $appIcon --paths src --add-data "scripts;scripts" --add-data "manifests;manifests" --add-data "locks;locks" --add-data "src/local_voice_studio;worker_source/local_voice_studio" --add-data "src/local_voice_studio/ui/theme;local_voice_studio/ui/theme" --add-data "src/local_voice_studio/ui/resources/icons;local_voice_studio/ui/resources/icons" --exclude-module torch --exclude-module torchaudio --exclude-module torchvision --exclude-module numpy launcher.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
    $packagedRoot = Join-Path $repoRoot "dist\LocalVoiceStudio\_internal"
    $requiredPackagedFiles = @(
        (Join-Path $packagedRoot "scripts\bootstrap_runtime.ps1"),
        (Join-Path $packagedRoot "manifests\runtime-assets-v1.json"),
        (Join-Path $packagedRoot "locks\conda-win-64.lock"),
        (Join-Path $packagedRoot "locks\requirements-win-cu128.lock")
    )
    foreach ($requiredFile in $requiredPackagedFiles) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) { throw "Packaged runtime resource missing: $requiredFile" }
    }
    $probeRoot = Join-Path $env:TEMP "VoiceStudio-packaged-bootstrap-probe"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $requiredPackagedFiles[0] -DataRoot $probeRoot -FunctionsOnly
    if ($LASTEXITCODE -ne 0) { throw "Packaged bootstrap resource probe failed" }
    Copy-Item -LiteralPath "THIRD_PARTY_NOTICES.md" -Destination "dist\LocalVoiceStudio" -Force
    & (Join-Path $PSScriptRoot "build_quick_launcher.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Quick launcher build failed" }
    if ($Release) {
        & (Join-Path $PSScriptRoot "sign_release.ps1") -Path "dist\LocalVoiceStudio\LocalVoiceStudio.exe" -CertificateThumbprint $CertificateThumbprint -TimestampUrl $TimestampUrl
        if ($LASTEXITCODE -ne 0) { throw "Executable signing failed" }
    } else {
        Write-Host "UNSIGNED DEVELOPMENT BUILD - not for formal release"
    }
    if (-not $SkipInstaller) {
        $iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $iscc) { throw "Inno Setup 6 was not found; use -SkipInstaller to build onedir only" }
        if ($Release) {
            $signtool = Get-ChildItem "$env:ProgramFiles(x86)\Windows Kits\10\bin" -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
                Sort-Object FullName -Descending | Select-Object -First 1
            if (-not $signtool) { throw "SignTool was not found" }
            $signCommand = "`"$($signtool.FullName)`" sign /sha1 $CertificateThumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 `$f"
            & $iscc "/DSignBuild" "/Svoicestudio=$signCommand" "installer\LocalVoiceStudio.iss"
        } else {
            & $iscc "installer\LocalVoiceStudio.iss"
        }
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup build failed" }
        if ($Release) {
            $installer = Get-ChildItem "dist\installer\LocalVoiceStudio-Setup-*.exe" | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
            & (Join-Path $PSScriptRoot "sign_release.ps1") -Path $installer.FullName -CertificateThumbprint $CertificateThumbprint -TimestampUrl $TimestampUrl
        }
    }
} finally { Pop-Location }
