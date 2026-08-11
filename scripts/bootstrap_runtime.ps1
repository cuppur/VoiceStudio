param(
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [ValidateSet("HF", "HF-Mirror", "ModelScope")][string]$Source = "ModelScope",
    [switch]$DownloadUVR5,
    [switch]$FunctionsOnly
)

$ErrorActionPreference = "Stop"
$Utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8
[Console]::OutputEncoding = $Utf8
$OutputEncoding = $Utf8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.IO.Compression.FileSystem

$engineCommit = "d523079fc05d9a8028d6085bffe4a2757c32abb6"
$script:CurrentStep = 0

function Write-StepState {
    param([int]$Step, [ValidateSet("running", "completed", "retrying", "failed", "skipped")][string]$State, [string]$Message)
    Write-Host ("LVS_EVENT " + (@{ type = "step"; step = $Step; state = $State; message = $Message } | ConvertTo-Json -Compress))
}

function Start-Step {
    param([int]$Step, [string]$Message)
    $script:CurrentStep = $Step
    Write-Host "[$Step/7] $Message"
    Write-StepState $Step "running" $Message
}

function Complete-Step {
    param([int]$Step, [string]$Message, [switch]$Skipped)
    Write-StepState $Step $(if ($Skipped) { "skipped" } else { "completed" }) $Message
}

function Invoke-RobustDownload {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Destination,
        [scriptblock]$Validator
    )

    $destinationPath = if ([System.IO.Path]::IsPathRooted($Destination)) {
        [System.IO.Path]::GetFullPath($Destination)
    } else {
        [System.IO.Path]::GetFullPath((Join-Path (Get-Location).ProviderPath $Destination))
    }
    $parent = Split-Path -Parent $destinationPath
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $partial = "$destinationPath.partial"

    if (Test-Path -LiteralPath $destinationPath) {
        try {
            if ((Get-Item -LiteralPath $destinationPath).Length -le 0) { throw "文件为空" }
            if ($Validator) { & $Validator $destinationPath }
            Write-Host "[Download] 使用已校验缓存：$destinationPath"
            return
        } catch {
            Write-Host "[Download] 缓存无效，将重新下载：$($_.Exception.Message)"
            Remove-Item -LiteralPath $destinationPath -Force -ErrorAction SilentlyContinue
        }
    }

    $curl = Join-Path $env:SystemRoot "System32\curl.exe"
    $validationAttempt = 0
    while ($validationAttempt -lt 2) {
        $validationAttempt++
        $downloadSucceeded = $false
        Write-Host "[Download] $Uri"

        if (Test-Path -LiteralPath $curl) {
            if (Test-Path -LiteralPath $partial) {
                & $curl -L --fail --show-error --retry 5 --retry-delay 2 --connect-timeout 30 -C - -o $partial $Uri
                $downloadSucceeded = ($LASTEXITCODE -eq 0)
                if (-not $downloadSucceeded) {
                    Write-Host "[Download] 服务端未接受续传，将自动重新完整下载。"
                }
            }
            if (-not $downloadSucceeded) {
                Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
                & $curl -L --fail --show-error --retry 5 --retry-delay 2 --connect-timeout 30 -o $partial $Uri
                $downloadSucceeded = ($LASTEXITCODE -eq 0)
            }
        } else {
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            for ($attempt = 1; $attempt -le 5; $attempt++) {
                try {
                    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $partial -TimeoutSec 1800 -MaximumRedirection 10
                    $downloadSucceeded = $true
                    break
                } catch {
                    if ($attempt -lt 5) {
                        Write-Host "[Download] 第 $attempt 次请求失败，正在重试。"
                        Start-Sleep -Seconds (2 * $attempt)
                    } else { throw }
                }
            }
        }

        if (-not $downloadSucceeded) {
            throw "下载失败，已完成自动重试：$Uri"
        }
        try {
            if (-not (Test-Path -LiteralPath $partial)) { throw "下载结束但临时文件不存在：$partial" }
            if ((Get-Item -LiteralPath $partial).Length -le 0) { throw "下载文件为空：$Uri" }
            if ($Validator) { & $Validator $partial }
            Move-Item -LiteralPath $partial -Destination $destinationPath -Force
            return
        } catch {
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            if ($validationAttempt -ge 2) { throw }
            Write-StepState $script:CurrentStep "retrying" "下载内容校验失败，正在重新下载一次"
            Write-Host "[Download] 内容校验失败，正在重新下载一次：$($_.Exception.Message)"
        }
    }
}

function Test-GptSoVitsArchive {
    param([Parameter(Mandatory = $true)][string]$Path)
    Test-ZipReadable $Path
    $expectedRoot = "GPT-SoVITS-$engineCommit/"
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $names = @($archive.Entries | ForEach-Object { $_.FullName.Replace("\", "/") })
        if ($names.Count -eq 0) { throw "ZIP 中没有文件" }
        $topRoots = @($names | ForEach-Object { ($_ -split "/")[0] } | Sort-Object -Unique)
        if ($topRoots.Count -ne 1 -or ($topRoots[0] + "/") -ne $expectedRoot) {
            throw "ZIP 顶层目录与固定提交不符，预期 $expectedRoot"
        }
        foreach ($required in @("requirements.txt", "install.ps1", "GPT_SoVITS/")) {
            if (-not ($names | Where-Object { $_.StartsWith($expectedRoot + $required, [StringComparison]::OrdinalIgnoreCase) } | Select-Object -First 1)) {
                throw "ZIP 缺少 GPT-SoVITS 关键内容：$required"
            }
        }
    } finally { $archive.Dispose() }
}

function Test-ZipReadable {
    param([Parameter(Mandatory = $true)][string]$Path)
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        if ($archive.Entries.Count -eq 0) { throw "ZIP 中没有文件" }
        foreach ($entry in $archive.Entries) {
            if ($entry.FullName.EndsWith("/")) { continue }
            $stream = $entry.Open()
            try { $stream.CopyTo([System.IO.Stream]::Null) } finally { $stream.Dispose() }
        }
    } finally { $archive.Dispose() }
}

function Test-TarReadable {
    param([Parameter(Mandatory = $true)][string]$Path)
    & tar.exe -tf $Path | Select-Object -First 1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "TAR.GZ 内容无效" }
}

function Expand-ZipStaged {
    param([Parameter(Mandatory = $true)][string]$ZipPath, [Parameter(Mandatory = $true)][string]$Destination)
    $zipAbsolute = if ([IO.Path]::IsPathRooted($ZipPath)) { [IO.Path]::GetFullPath($ZipPath) } else { [IO.Path]::GetFullPath((Join-Path (Get-Location).ProviderPath $ZipPath)) }
    $destinationAbsolute = if ([IO.Path]::IsPathRooted($Destination)) { [IO.Path]::GetFullPath($Destination) } else { [IO.Path]::GetFullPath((Join-Path (Get-Location).ProviderPath $Destination)) }
    $staging = Join-Path (Split-Path -Parent $zipAbsolute) ("extract-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $staging | Out-Null
    try {
        [IO.Compression.ZipFile]::ExtractToDirectory($zipAbsolute, $staging)
        New-Item -ItemType Directory -Force -Path $destinationAbsolute | Out-Null
        Get-ChildItem -LiteralPath $staging -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $destinationAbsolute -Recurse -Force
        }
    } finally {
        if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    }
}

function Test-PrivatePython {
    param([string]$Python)
    if (-not (Test-Path -LiteralPath $Python)) { return $false }
    & $Python -X utf8 -c "import sys; assert sys.version_info[:2] == (3, 11); import pip; print(sys.version.split()[0])" | Out-Host
    return ($LASTEXITCODE -eq 0)
}

if ($FunctionsOnly) { return }

$resolvedDataRoot = [System.IO.Path]::GetFullPath($DataRoot)
$driveRoot = [System.IO.Path]::GetPathRoot($resolvedDataRoot)
if ($resolvedDataRoot -eq $driveRoot -or $resolvedDataRoot.Length -lt ($driveRoot.Length + 4)) { throw "拒绝使用不安全的数据目录：$resolvedDataRoot" }
$runtimeRoot = Join-Path $resolvedDataRoot "runtime"
$miniforgeRoot = Join-Path $runtimeRoot "miniforge"
$envRoot = Join-Path $runtimeRoot "env"
$engineParent = Join-Path $resolvedDataRoot "engines"
$engineRoot = Join-Path $engineParent "GPT-SoVITS"
$cacheRoot = Join-Path $resolvedDataRoot "cache"
$toolsRoot = Join-Path $resolvedDataRoot "tools"
New-Item -ItemType Directory -Force -Path $runtimeRoot, $cacheRoot, $toolsRoot, $engineParent | Out-Null
$installLockPath = Join-Path $runtimeRoot "install.lock"
$installLock = $null
try {
    $installLock = [System.IO.File]::Open($installLockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
} catch {
    throw "已有本地引擎安装正在运行，请等待其完成后再试。"
}

try {
    Start-Step 1 "准备私有 Python 运行环境"
    $condaExe = Join-Path $miniforgeRoot "Scripts\conda.exe"
    $envPython = Join-Path $envRoot "python.exe"
    if (Test-PrivatePython $envPython) {
        Complete-Step 1 "现有 Python 3.11 与 pip 有效，已跳过安装" -Skipped
    } else {
        if (-not (Test-Path -LiteralPath $condaExe)) {
            $installer = Join-Path $cacheRoot "Miniforge3-Windows-x86_64.exe"
            Invoke-RobustDownload "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Windows-x86_64.exe" $installer
            $install = Start-Process -FilePath $installer -ArgumentList @("/S", "/D=$miniforgeRoot") -Wait -PassThru -WindowStyle Hidden
            if ($install.ExitCode -ne 0) { throw "Miniforge 安装失败，退出码：$($install.ExitCode)" }
        }
        if (Test-Path -LiteralPath $envPython) {
            & $condaExe install -y -p $envRoot python=3.11 pip --json
        } else {
            & $condaExe create -y -p $envRoot python=3.11 pip --json
        }
        if ($LASTEXITCODE -ne 0 -or -not (Test-PrivatePython $envPython)) { throw "私有 Python 3.11 环境创建或修复失败" }
        Complete-Step 1 "私有 Python 3.11 环境已就绪"
    }

    Start-Step 2 "获取固定提交 GPT-SoVITS"
    $marker = Join-Path $engineRoot ".pinned-commit"
    $engineValid = (Test-Path -LiteralPath $marker) -and ((Get-Content -Raw -LiteralPath $marker).Trim() -eq $engineCommit) -and (Test-Path -LiteralPath (Join-Path $engineRoot "requirements.txt")) -and (Test-Path -LiteralPath (Join-Path $engineRoot "install.ps1"))
    if ($engineValid) {
        Complete-Step 2 "固定提交 GPT-SoVITS 已存在，已跳过" -Skipped
    } else {
        $zip = Join-Path $cacheRoot "$engineCommit.zip"
        Invoke-RobustDownload "https://codeload.github.com/RVC-Boss/GPT-SoVITS/zip/$engineCommit" $zip ${function:Test-GptSoVitsArchive}
        $extractRoot = Join-Path $cacheRoot ("engine-extract-" + [Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $extractRoot | Out-Null
        try {
            [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $extractRoot)
            $extracted = Join-Path $extractRoot "GPT-SoVITS-$engineCommit"
            if (-not (Test-Path -LiteralPath (Join-Path $extracted "requirements.txt"))) { throw "解压后的源码不完整" }
            if (Test-Path -LiteralPath $engineRoot) {
                $invalidRoot = Join-Path $engineParent ("GPT-SoVITS.invalid-" + (Get-Date -Format "yyyyMMddHHmmss"))
                Move-Item -LiteralPath $engineRoot -Destination $invalidRoot
            }
            Move-Item -LiteralPath $extracted -Destination $engineRoot
            Set-Content -LiteralPath $marker -Value $engineCommit -Encoding Ascii
        } finally {
            if (Test-Path -LiteralPath $extractRoot) { Remove-Item -LiteralPath $extractRoot -Recurse -Force }
        }
        Complete-Step 2 "固定提交 GPT-SoVITS 下载并解压完成"
    }

    Start-Step 3 "安装 CUDA 12.8 对应的 PyTorch 2.7.1"
    & $envPython -X utf8 -W ignore -c "import torch, torchaudio; assert torch.__version__ == '2.7.1+cu128'; assert torchaudio.__version__ == '2.7.1+cu128'"
    if ($LASTEXITCODE -eq 0) {
        Complete-Step 3 "PyTorch 2.7.1+cu128 已存在，已跳过" -Skipped
    } else {
        & $envPython -m pip install --disable-pip-version-check --upgrade pip
        & $envPython -m pip install "torch==2.7.1+cu128" "torchaudio==2.7.1+cu128" --index-url "https://download.pytorch.org/whl/cu128"
        if ($LASTEXITCODE -ne 0) { throw "PyTorch 2.7.1+cu128 安装失败" }
        Complete-Step 3 "PyTorch 2.7.1+cu128 已就绪"
    }

    Start-Step 4 "安装 GPT-SoVITS 依赖"
    $dependenciesMarker = Join-Path $runtimeRoot ".dependencies-complete"
    & $envPython -X utf8 -W ignore -c "import pyopenjtalk, librosa, gradio, onnxruntime"
    if ((Test-Path -LiteralPath $dependenciesMarker) -and $LASTEXITCODE -eq 0) {
        Complete-Step 4 "GPT-SoVITS 依赖已存在，已跳过" -Skipped
    } else {
        $localRequirements = Join-Path $engineRoot "requirements-local-voice-studio.txt"
        $requirements = Get-Content -LiteralPath (Join-Path $engineRoot "requirements.txt")
        $requirements = @($requirements | ForEach-Object {
            if ($_ -match '^--no-binary=opencc$') { '# opencc: using a Windows-compatible pure Python distribution' }
            elseif ($_ -match '^pyopenjtalk(?:[<>=].*)?$') { 'pyopenjtalk-plus==0.4.1.post7' }
            elseif ($_ -match '^jieba_fast(?:[<>=].*)?$') { '# jieba_fast: provided by the jieba compatibility shim below' }
            elseif ($_ -match '^opencc(?:[<>=].*)?$') { 'opencc-python-reimplemented==0.1.7' }
            else { $_ }
        })
        [System.IO.File]::WriteAllLines($localRequirements, $requirements, $Utf8)
        & $envPython -m pip install -r (Join-Path $engineRoot "extra-req.txt") --no-deps
        if ($LASTEXITCODE -ne 0) { throw "额外依赖安装失败" }
        & $envPython -m pip install -r $localRequirements
        if ($LASTEXITCODE -ne 0) { throw "GPT-SoVITS 依赖安装失败" }
        $sitePackages = (& $envPython -X utf8 -c "import site; print(site.getsitepackages()[0])").Trim()
        $jiebaFastRoot = Join-Path $sitePackages "jieba_fast"
        New-Item -ItemType Directory -Force -Path $jiebaFastRoot | Out-Null
        [System.IO.File]::WriteAllText((Join-Path $jiebaFastRoot "__init__.py"), "from jieba import *`nfrom jieba import setLogLevel`n", $Utf8)
        [System.IO.File]::WriteAllText((Join-Path $jiebaFastRoot "posseg.py"), "from jieba.posseg import *`n", $Utf8)
        & $envPython -X utf8 -c "import jieba_fast, jieba_fast.posseg, opencc, pyopenjtalk"
        if ($LASTEXITCODE -ne 0) { throw "Windows 兼容依赖导入验证失败" }
        & $envPython -m pip install "torch==2.7.1+cu128" "torchaudio==2.7.1+cu128" --index-url "https://download.pytorch.org/whl/cu128"
        if ($LASTEXITCODE -ne 0) { throw "固定 PyTorch 版本恢复失败" }
        Set-Content -LiteralPath $dependenciesMarker -Value (Get-Date).ToString("o") -Encoding Ascii
        Complete-Step 4 "GPT-SoVITS 依赖已安装"
    }
    # ModelScope itself can be present after a no-deps upstream install while
    # the denoise pipeline is still unusable.  Validate the exact import used
    # by cmd-denoise.py and repair only its missing runtime dependencies.
    & $envPython -X utf8 -W ignore -c "import addict, datasets, simplejson, sortedcontainers; from modelscope.pipelines import pipeline"
    if ($LASTEXITCODE -ne 0) {
        & $envPython -m pip install "addict==2.4.0" "datasets>=2.16,<4" "simplejson>=3.19,<5" "sortedcontainers==2.4.0"
        if ($LASTEXITCODE -ne 0) { throw "智能降噪依赖安装失败" }
        & $envPython -X utf8 -W ignore -c "import simplejson, sortedcontainers; from modelscope.pipelines import pipeline"
        if ($LASTEXITCODE -ne 0) { throw "智能降噪依赖导入验证失败" }
    }

    Start-Step 5 "安装 FFmpeg 与预训练模型"
    $modelsMarker = Join-Path $runtimeRoot ".models-complete"
    $coreModelsReady = (Test-Path -LiteralPath $modelsMarker) -and (Test-Path -LiteralPath (Join-Path $engineRoot "GPT_SoVITS\pretrained_models\sv")) -and (Test-Path -LiteralPath (Join-Path $engineRoot "GPT_SoVITS\text\G2PWModel"))
    $uvrWeights = Join-Path $engineRoot "tools\uvr5\uvr5_weights"
    $uvrReady = @(Get-ChildItem -LiteralPath $uvrWeights -File -Filter "*.pth" -ErrorAction SilentlyContinue).Count -gt 0
    if ($coreModelsReady -and $DownloadUVR5 -and -not $uvrReady) {
        Write-Host "[Download] 首次使用智能优化，正在按需安装 UVR5 人声分离模型"
        $uvrUrls = @{
            "HF" = "https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/main/uvr5_weights.zip"
            "HF-Mirror" = "https://hf-mirror.com/XXXXRT/GPT-SoVITS-Pretrained/resolve/main/uvr5_weights.zip"
            "ModelScope" = "https://www.modelscope.cn/models/XXXXRT/GPT-SoVITS-Pretrained/resolve/master/uvr5_weights.zip"
        }
        $uvrArchive = Join-Path $cacheRoot "uvr5_weights.zip"
        Invoke-RobustDownload -Uri $uvrUrls[$Source] -Destination $uvrArchive -Validator ${function:Test-ZipReadable}
        $uvrExtract = Join-Path $cacheRoot ("uvr5-extract-" + [Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Force -Path $uvrExtract, $uvrWeights | Out-Null
        try {
            [IO.Compression.ZipFile]::ExtractToDirectory($uvrArchive, $uvrExtract)
            $downloadedWeights = @(Get-ChildItem -LiteralPath $uvrExtract -Recurse -File -Filter "*.pth")
            if ($downloadedWeights.Count -eq 0) { throw "UVR5 压缩包中没有模型权重" }
            foreach ($weight in $downloadedWeights) { Copy-Item -LiteralPath $weight.FullName -Destination (Join-Path $uvrWeights $weight.Name) -Force }
        } finally {
            if (Test-Path -LiteralPath $uvrExtract) { Remove-Item -LiteralPath $uvrExtract -Recurse -Force }
        }
        $uvrReady = @(Get-ChildItem -LiteralPath $uvrWeights -File -Filter "*.pth").Count -gt 0
    }
    $modelsReady = $coreModelsReady -and (-not $DownloadUVR5 -or $uvrReady)
    if ($modelsReady) {
        Complete-Step 5 "FFmpeg 与预训练模型已存在，已跳过" -Skipped
    } else {
      & $condaExe install -y -p $envRoot -c conda-forge ffmpeg cmake --json
      if ($LASTEXITCODE -ne 0) { throw "FFmpeg 安装失败" }
      $condaFfmpeg = Join-Path $envRoot "Library\bin\ffmpeg.exe"
      & $condaFfmpeg -version 2>$null | Out-Null
      if ($LASTEXITCODE -eq 0) {
          Copy-Item -LiteralPath $condaFfmpeg -Destination $toolsRoot -Force
          Copy-Item -LiteralPath (Join-Path $envRoot "Library\bin\ffprobe.exe") -Destination $toolsRoot -Force
      } else {
          $systemFfmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
          $systemFfprobe = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
          if ($systemFfmpeg -and $systemFfprobe) {
              Copy-Item -LiteralPath $systemFfmpeg.Source -Destination (Join-Path $toolsRoot "ffmpeg.exe") -Force
              Copy-Item -LiteralPath $systemFfprobe.Source -Destination (Join-Path $toolsRoot "ffprobe.exe") -Force
          } else {
              $ffmpegArchive = Join-Path $cacheRoot "ffmpeg-release-essentials.zip"
              Invoke-RobustDownload "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" $ffmpegArchive ${function:Test-ZipReadable}
              $ffmpegExtract = Join-Path $cacheRoot ("ffmpeg-extract-" + [Guid]::NewGuid().ToString("N"))
              [IO.Compression.ZipFile]::ExtractToDirectory($ffmpegArchive, $ffmpegExtract)
              try {
                  $staticFfmpeg = Get-ChildItem -LiteralPath $ffmpegExtract -Recurse -File -Filter "ffmpeg.exe" | Select-Object -First 1
                  $staticFfprobe = Get-ChildItem -LiteralPath $ffmpegExtract -Recurse -File -Filter "ffprobe.exe" | Select-Object -First 1
                  if (-not $staticFfmpeg -or -not $staticFfprobe) { throw "静态 FFmpeg 压缩包缺少可执行文件" }
                  Copy-Item -LiteralPath $staticFfmpeg.FullName -Destination (Join-Path $toolsRoot "ffmpeg.exe") -Force
                  Copy-Item -LiteralPath $staticFfprobe.FullName -Destination (Join-Path $toolsRoot "ffprobe.exe") -Force
              } finally { if (Test-Path $ffmpegExtract) { Remove-Item -LiteralPath $ffmpegExtract -Recurse -Force } }
          }
      }
      & (Join-Path $toolsRoot "ffmpeg.exe") -version 2>$null | Out-Null
      if ($LASTEXITCODE -ne 0) { throw "FFmpeg 实际执行验证失败" }
      $originalPath = $env:Path
      try {
        $env:Path = @($envRoot, (Join-Path $envRoot "Scripts"), (Join-Path $envRoot "Library\bin"), (Join-Path $miniforgeRoot "Scripts"), $originalPath) -join ";"
        $installScript = Join-Path $engineRoot "install.ps1"
        $patchedScript = Join-Path $engineRoot "install-local-voice-studio.ps1"
        $content = Get-Content -Raw -LiteralPath $installScript
        $content = $content.Replace('Invoke-Pip torch torchcodec --index-url "https://download.pytorch.org/whl/cu128"', 'Invoke-Pip torch==2.7.1+cu128 torchaudio==2.7.1+cu128 --index-url "https://download.pytorch.org/whl/cu128"')
        $content = $content.Replace('Invoke-Pip -r requirements.txt', 'Invoke-Pip -r requirements-local-voice-studio.txt')
        $content = $content.Replace('Write-Info "Downloading NLTK Data..."', '$nltkRoot = (python -c "import sys; print(sys.prefix)").Trim(); if (-not (Test-Path (Join-Path $nltkRoot "nltk_data\corpora\cmudict"))) { Write-Info "Downloading NLTK Data..."')
        $content = $content.Replace('Invoke-Unzip "nltk_data.zip" (python -c "import sys; print(sys.prefix)").Trim()', 'Invoke-Unzip "nltk_data.zip" $nltkRoot } else { Write-Info "NLTK Data Exists; Skip Downloading" }')
        $content = $content.Replace('Write-Info "Downloading Open JTalk Dict..."', '$openJtalkPackage = (python -c "import os, pyopenjtalk; print(os.path.dirname(pyopenjtalk.__file__))").Trim(); if (-not (Test-Path (Join-Path $openJtalkPackage "dictionary"))) { Write-Info "Downloading Open JTalk Dict..."')
        $content = $content.Replace('Write-Success "Open JTalk Dic Downloaded"', 'Write-Success "Open JTalk Dic Downloaded" } else { Write-Info "Open JTalk dictionary bundled with pyopenjtalk-plus; Skip Downloading" }')
        $content = $content.Replace('$null = Invoke-WebRequest @params -ErrorAction Stop', '$validator = if ($OutFile -like "*.zip") { ${function:Test-ZipReadable} } elseif ($OutFile -like "*.tar.gz") { ${function:Test-TarReadable} } else { $null }; Invoke-RobustDownload -Uri $Uri -Destination $OutFile -Validator $validator')
        $safeUnzip = '$extractTemp = Join-Path ([IO.Path]::GetTempPath()) ("lvs-unzip-" + [Guid]::NewGuid().ToString("N")); New-Item -ItemType Directory -Path $extractTemp | Out-Null; try { [IO.Compression.ZipFile]::ExtractToDirectory((Resolve-Path $ZipPath).Path, $extractTemp); New-Item -ItemType Directory -Force -Path $DestPath | Out-Null; Get-ChildItem -LiteralPath $extractTemp -Force | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $DestPath -Recurse -Force } } finally { if (Test-Path -LiteralPath $extractTemp) { Remove-Item -LiteralPath $extractTemp -Recurse -Force } }'
        $content = $content.Replace('Expand-Archive -Path $ZipPath -DestinationPath $DestPath -Force', $safeUnzip)
        $content = $content.Replace('Expand-Archive -Path $ZipPath -DestinationPath $DestPath -Force', 'Expand-ZipStaged -ZipPath $ZipPath -Destination $DestPath')
        [System.IO.File]::WriteAllText($patchedScript, $content, $Utf8)
        Push-Location $engineRoot
        try {
            $arguments = @{ Device = "CU128"; Source = $Source }
            if ($DownloadUVR5) { $arguments.DownloadUVR5 = $true }
            & $patchedScript @arguments
            if ($LASTEXITCODE -ne 0) { throw "预训练模型安装失败" }
        } finally { Pop-Location }
      } finally { $env:Path = $originalPath }
      Set-Content -LiteralPath $modelsMarker -Value (Get-Date).ToString("o") -Encoding Ascii
      Complete-Step 5 "FFmpeg 与预训练模型已安装"
    }

    Start-Step 6 "验证私有 Python、Torch、CUDA、模型与 GPT-SoVITS 加载"
    $verifyScript = Join-Path $PSScriptRoot "verify_runtime.py"
    & $envPython -X utf8 $verifyScript --engine $engineRoot --tools $toolsRoot
    if ($LASTEXITCODE -ne 0) { throw "运行时完整性验证失败，本次安装不会标记为成功" }
    Complete-Step 6 "Torch、CUDA、模型与 GPT-SoVITS 加载验证通过"

    Start-Step 7 "写入安装清单"
    $manifest = @{ schema_version = 1; engine_commit = $engineCommit; engine_version = "v2ProPlus"; python = (& $envPython -c "import sys; print(sys.version.split()[0])"); python_executable = (& $envPython -c "import sys; print(sys.executable)"); torch = (& $envPython -c "import torch; print(torch.__version__)"); installed_at = (Get-Date).ToString("o") }
    $manifestJson = $manifest | ConvertTo-Json
    [System.IO.File]::WriteAllText((Join-Path $runtimeRoot "install-manifest.json"), $manifestJson, $Utf8)
    Complete-Step 7 "安装清单已写入，安装完成"
    Write-Host "安装完成。现在可以离线执行合成与训练。"
} catch {
    if ($script:CurrentStep -gt 0) { Write-StepState $script:CurrentStep "failed" $_.Exception.Message }
    Write-Error $_
    if ($installLock) { $installLock.Dispose() }
    exit 1
}
if ($installLock) { $installLock.Dispose() }
