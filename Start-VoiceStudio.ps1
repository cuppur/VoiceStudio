param([switch]$Diagnostics)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ($(if ($Diagnostics) { "launcher-diagnostics.log" } else { "launcher.log" }))
function Write-Log([string]$Message) { $line = "$(Get-Date -Format o) $Message"; Add-Content -LiteralPath $log -Value $line; Write-Host $line }
Write-Log "Checking VoiceStudio..."
$exe = @(
  (Join-Path $root "dist\LocalVoiceStudio\LocalVoiceStudio.exe"),
  (Join-Path $root "dist\LocalVoiceStudio\VoiceStudio.exe"),
  (Join-Path $root "build\LocalVoiceStudio\LocalVoiceStudio.exe")
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ($exe) {
  Write-Log "Packaged executable: $exe"
  $args = @()
  if ($Diagnostics) { $args += "--diagnostics" }
  Start-Process -FilePath $exe -ArgumentList $args -WorkingDirectory (Split-Path -Parent $exe)
  exit 0
}
$python = @(
  (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe"),
  (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe")
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $python) { Write-Log "Python 3.10/3.11 not found; install or repair the local runtime."; exit 2 }
$env:PYTHONPATH = Join-Path $root "src"
$env:VOICESTUDIO_LAUNCHER = "1"
Write-Log "Python runtime: $python"
if ($Diagnostics) { Write-Log "Diagnostics requested; launching normal app without automatic downloads." }
& $python -m local_voice_studio *> (Join-Path $logDir "app.log")
$rc = $LASTEXITCODE
Write-Log "Application exit code: $rc"
exit $rc
