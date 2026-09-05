param([string]$Root = (Split-Path -Parent $PSScriptRoot))
$dir = Join-Path $Root "diagnostics"
New-Item -ItemType Directory -Force $dir | Out-Null
$system = [ordered]@{ timestamp=(Get-Date).ToString("o"); computer=$env:COMPUTERNAME; user=$env:USERNAME; os=(Get-CimInstance Win32_OperatingSystem).Caption; powershell=$PSVersionTable.PSVersion.ToString() }
$gpu = @(Get-CimInstance Win32_VideoController | ForEach-Object { [ordered]@{ name=$_.Name; driver=$_.DriverVersion; ram=$_.AdapterRAM } })
$runtime = [ordered]@{ python=(Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue); ffmpeg=(Get-Command ffmpeg -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue); root=$Root }
$assets = if (Test-Path (Join-Path $Root "manifests\runtime-assets-v1.json")) { Get-Content (Join-Path $Root "manifests\runtime-assets-v1.json") -Raw } else { "{}" }
$system | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $dir "system.json") -Encoding UTF8
$gpu | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $dir "gpu.json") -Encoding UTF8
$runtime | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $dir "runtime.json") -Encoding UTF8
$assets | Set-Content (Join-Path $dir "model-assets.json") -Encoding UTF8
Write-Host "Diagnostics written to $dir"
