param()

$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$python = @(
    (Join-Path $repoRoot ".venv310\Scripts\python.exe"),
    (Join-Path $repoRoot ".venv\Scripts\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe")
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $python) { throw "未找到可用于构建快速入口的 Python 3.10" }

$icon = Join-Path $repoRoot "assets\voicestudio.ico"
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) { throw "缺少 VoiceStudio 图标：$icon" }

Push-Location $repoRoot
try {
    & $python -m PyInstaller --noconfirm --clean --onefile --windowed `
        --name "VoiceStudio-一键启动" `
        --icon $icon `
        --distpath $repoRoot `
        --workpath (Join-Path $repoRoot "build\quick-launcher") `
        --specpath (Join-Path $repoRoot "build\quick-launcher") `
        "quick_launcher.py"
    if ($LASTEXITCODE -ne 0) { throw "快速入口构建失败" }

    $launcher = Join-Path $repoRoot "VoiceStudio-一键启动.exe"
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw "快速入口未生成：$launcher" }
    Write-Host "快速入口已生成：$launcher"
} finally {
    Pop-Location
}
