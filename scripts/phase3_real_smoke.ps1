param(
    [Parameter(Mandatory=$true)][string]$Model,
    [Parameter(Mandatory=$true)][string]$InputDir,
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [string]$RvcPython = "$env:LOCALAPPDATA\LocalVoiceStudio\runtime\rvc-env\Scripts\python.exe",
    [string]$EngineRoot = "$env:LOCALAPPDATA\LocalVoiceStudio\engines\RVC"
)
$ErrorActionPreference = 'Stop'
$bridge = (Resolve-Path "$PSScriptRoot\..\src\local_voice_studio\singing\rvc_bridge.py").Path
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$env:PYTHONPATH = "$env:LOCALAPPDATA\LocalVoiceStudio\runtime\env\Lib\site-packages;$EngineRoot"
$previousLocation = Get-Location
Set-Location -LiteralPath $EngineRoot
$inputs = @(Get-ChildItem -LiteralPath $InputDir -Filter '*.wav' | Sort-Object Name | Select-Object -First 3)
if ($inputs.Count -lt 3) { throw "至少需要三个 Vocal WAV 输入" }
$rows = @()
foreach ($pitch in @(0, 2)) {
    foreach ($input in $inputs) {
        $output = Join-Path $OutputDir ("{0}_pitch{1}.wav" -f $input.BaseName, $pitch)
        $timer = [Diagnostics.Stopwatch]::StartNew()
        & $RvcPython $bridge --input $input.FullName --model $Model --pitch $pitch --output $output
        $exitCode = $LASTEXITCODE
        $timer.Stop()
        $rows += [pscustomobject]@{
            input = $input.FullName
            pitch = $pitch
            output = $output
            exit_code = $exitCode
            elapsed_ms = $timer.ElapsedMilliseconds
            bytes = if (Test-Path -LiteralPath $output) { (Get-Item -LiteralPath $output).Length } else { 0 }
        }
        if ($exitCode -ne 0 -or -not (Test-Path -LiteralPath $output)) { throw "转换失败: $($input.Name), pitch=$pitch" }
    }
}
$rows | ConvertTo-Json -Depth 3 | Tee-Object -FilePath (Join-Path $OutputDir 'report.json')
Set-Location $previousLocation
