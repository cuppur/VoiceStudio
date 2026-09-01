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
$pretrainedRevision = "0c47645e02a7bc3688d7b263b0042c81e3cd82cd"
$assetManifestPath = Join-Path (Split-Path -Parent $PSScriptRoot) "manifests\runtime-assets-v1.json"
if (-not (Test-Path -LiteralPath $assetManifestPath)) { throw "缺少运行时资产清单：$assetManifestPath" }
$assetManifest = Get-Content -Raw -LiteralPath $assetManifestPath | ConvertFrom-Json
if ($assetManifest.schema_version -ne 1) { throw "不支持的运行时资产清单版本" }
if ($assetManifest.engine.commit -ne $engineCommit -or $assetManifest.engine.pretrained_revision -ne $pretrainedRevision) { throw "资产清单与固定引擎版本不一致" }
$script:CurrentStep = 0

function Get-PinnedAsset {
    param([Parameter(Mandatory = $true)][string]$Id)
    $entries = @($assetManifest.assets | Where-Object { $_.id -eq $Id })
    if ($entries.Count -ne 1) { throw "资产未登记或重复登记：$Id" }
    $asset = $entries[0]
    if ($asset.size -le 0 -or $asset.sha256 -notmatch '^[0-9a-fA-F]{64}$' -or @($asset.urls).Count -eq 0) { throw "资产清单条目不完整：$Id" }
    foreach ($url in @($asset.urls)) { if (-not $url.StartsWith("https://", [StringComparison]::OrdinalIgnoreCase)) { throw "资产 URL 必须使用 HTTPS：$Id" } }
    return $asset
}

function Test-PinnedFile {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)]$Asset)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "文件不存在：$Path" }
    $length = (Get-Item -LiteralPath $Path).Length
    if ($length -ne [Int64]$Asset.size) { throw "文件大小不符：$length != $($Asset.size)" }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne ([string]$Asset.sha256).ToLowerInvariant()) { throw "文件 SHA-256 不符：$($Asset.id)" }
}

function Test-InstalledFilePins {
    param(
        [Parameter(Mandatory = $true)][string]$DataRoot,
        [Parameter(Mandatory = $true)]$Manifest
    )
    try {
        foreach ($pin in @($Manifest.installed_file_pins)) {
            $target = Join-Path $DataRoot ([string]$pin.path).Replace("/", "\")
            Test-PinnedFile -Path $target -Asset $pin
        }
        return $true
    } catch {
        Write-Host "[Repair] 已安装的私有媒体工具需要恢复：$($_.Exception.Message)"
        return $false
    }
}

function Test-MiniforgeSignature {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)]$Asset)
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) { throw "Miniforge Authenticode 签名无效：$($signature.Status)" }
    $publisher = [string]$signature.SignerCertificate.Subject
    if ($publisher.IndexOf([string]$Asset.authenticode_publisher_contains, [StringComparison]::OrdinalIgnoreCase) -lt 0) { throw "Miniforge 发布者不可信：$publisher" }
}

function Invoke-PinnedAssetDownload {
    param([Parameter(Mandatory = $true)][string]$Id, [Parameter(Mandatory = $true)][string]$Destination, [scriptblock]$Validator)
    $asset = Get-PinnedAsset $Id
    $lastError = $null
    foreach ($url in @($asset.urls)) {
        try {
            $integrityValidator = {
                param($Path)
                Test-PinnedFile -Path $Path -Asset $asset
                if ($Validator) { & $Validator $Path }
            }.GetNewClosure()
            Invoke-RobustDownload -Uri $url -Destination $Destination -Validator $integrityValidator -RequireHttps
            return $asset
        } catch {
            $lastError = $_
            Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath "$Destination.partial" -Force -ErrorAction SilentlyContinue
        }
    }
    throw "所有已登记镜像均失败：$Id；$($lastError.Exception.Message)"
}

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
        [scriptblock]$Validator,
        [switch]$RequireHttps
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
                $protocolArgs = $(if ($RequireHttps) { @("--proto", "=https", "--proto-redir", "=https") } else { @() })
                & $curl @protocolArgs -L --fail --show-error --retry 5 --retry-delay 2 --connect-timeout 30 -C - -o $partial $Uri
                $downloadSucceeded = ($LASTEXITCODE -eq 0)
                if (-not $downloadSucceeded) {
                    Write-Host "[Download] 服务端未接受续传，将自动重新完整下载。"
                }
            }
            if (-not $downloadSucceeded) {
                Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
                & $curl @protocolArgs -L --fail --show-error --retry 5 --retry-delay 2 --connect-timeout 30 -o $partial $Uri
                $downloadSucceeded = ($LASTEXITCODE -eq 0)
            }
        } else {
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            for ($attempt = 1; $attempt -le 5; $attempt++) {
                try {
                    $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $partial -TimeoutSec 1800 -MaximumRedirection 10 -PassThru
                    if ($RequireHttps -and $response.BaseResponse.ResponseUri.Scheme -ne "https") { throw "下载被重定向到非 HTTPS 地址" }
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
        $totalSize = [Int64]0
        foreach ($entry in $archive.Entries) {
            $normalized = $entry.FullName.Replace("\", "/")
            if ([IO.Path]::IsPathRooted($normalized) -or $normalized -match '(^|/)\.\.(/|$)' -or $normalized -match '^[A-Za-z]:') { throw "ZIP 包含不安全路径：$normalized" }
            $unixMode = (($entry.ExternalAttributes -shr 16) -band 0xF000)
            if ($unixMode -eq 0xA000) { throw "ZIP 包含禁止的符号链接：$normalized" }
            $totalSize += [Int64]$entry.Length
            if ($totalSize -gt 20GB) { throw "ZIP 解压后大小超过安全限制" }
            if ($entry.FullName.EndsWith("/")) { continue }
            $stream = $entry.Open()
            try { $stream.CopyTo([System.IO.Stream]::Null) } finally { $stream.Dispose() }
        }
    } finally { $archive.Dispose() }
}

function Test-TarReadable {
    param([Parameter(Mandatory = $true)][string]$Path)
    $tar = Join-Path $env:SystemRoot "System32\tar.exe"
    if (-not (Test-Path -LiteralPath $tar)) { throw "Windows 私有 TAR 工具不可用" }
    & $tar -tf $Path | Select-Object -First 1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "TAR.GZ 内容无效" }
}

function Test-ExistingInstallManifest {
    param([Parameter(Mandatory = $true)][string]$RuntimeRoot, [Parameter(Mandatory = $true)][string]$DataRoot)
    $path = Join-Path $RuntimeRoot ("install-" + "manifest.json")
    if (-not (Test-Path -LiteralPath $path)) { return $false }
    try {
        $manifest = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
        if ($manifest.schema_version -ne 2 -or $manifest.engine_commit -ne $engineCommit -or $manifest.pretrained_revision -ne $pretrainedRevision) { return $false }
        foreach ($item in @($manifest.verified_files)) {
            $relative = ([string]$item.path).Replace("/", "\")
            if ([IO.Path]::IsPathRooted($relative) -or $relative -match '(^|\\)\.\.(\\|$)') { return $false }
            $target = [IO.Path]::GetFullPath((Join-Path $DataRoot $relative))
            if (-not $target.StartsWith($DataRoot + "\", [StringComparison]::OrdinalIgnoreCase)) { return $false }
            Test-PinnedFile -Path $target -Asset $item
        }
        return $true
    } catch { return $false }
}

function Expand-ZipStaged {
    param([Parameter(Mandatory = $true)][string]$ZipPath, [Parameter(Mandatory = $true)][string]$Destination)
    $zipAbsolute = if ([IO.Path]::IsPathRooted($ZipPath)) { [IO.Path]::GetFullPath($ZipPath) } else { [IO.Path]::GetFullPath((Join-Path (Get-Location).ProviderPath $ZipPath)) }
    $destinationAbsolute = if ([IO.Path]::IsPathRooted($Destination)) { [IO.Path]::GetFullPath($Destination) } else { [IO.Path]::GetFullPath((Join-Path (Get-Location).ProviderPath $Destination)) }
    Test-ZipReadable $zipAbsolute
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
            $miniforgeAsset = Invoke-PinnedAssetDownload -Id "miniforge-win-x64" -Destination $installer
            Test-MiniforgeSignature -Path $installer -Asset $miniforgeAsset
            $install = Start-Process -FilePath $installer -ArgumentList @("/S", "/D=$miniforgeRoot") -Wait -PassThru -WindowStyle Hidden
            if ($install.ExitCode -ne 0) { throw "Miniforge 安装失败，退出码：$($install.ExitCode)" }
        }
        $condaLock = Join-Path (Split-Path -Parent $PSScriptRoot) "locks\conda-win-64.lock"
        if (-not (Test-Path -LiteralPath $condaLock)) { throw "缺少 Conda 显式锁文件" }
        if (Test-Path -LiteralPath $envPython) { throw "私有环境已损坏；请移走 runtime\env 后用显式锁重新安装" }
        & $condaExe create -y -p $envRoot --file $condaLock --json
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
        Invoke-PinnedAssetDownload -Id "gpt-sovits-source" -Destination $zip -Validator ${function:Test-GptSoVitsArchive} | Out-Null
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
    $pipLock = Join-Path (Split-Path -Parent $PSScriptRoot) "locks\requirements-win-cu128.lock"
    $wheelDir = Join-Path (Split-Path -Parent $PSScriptRoot) "locks\wheels"
    if (-not (Test-Path -LiteralPath $pipLock)) { throw "缺少 Python hash lock" }
    if (-not (Test-Path -LiteralPath $wheelDir)) { throw "缺少已验证的本地 wheel 目录" }
    & $envPython -X utf8 -W ignore -c "import torch, torchaudio; assert torch.__version__ == '2.7.1+cu128'; assert torchaudio.__version__ == '2.7.1+cu128'"
    if ($LASTEXITCODE -eq 0) {
        Complete-Step 3 "PyTorch 2.7.1+cu128 已存在，已跳过" -Skipped
    } else {
        & $envPython -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: --find-links $wheelDir -r $pipLock
        if ($LASTEXITCODE -ne 0) { throw "PyTorch 2.7.1+cu128 安装失败" }
        Complete-Step 3 "PyTorch 2.7.1+cu128 已就绪"
    }

    Start-Step 4 "安装 GPT-SoVITS 依赖"
    $dependenciesMarker = Join-Path $runtimeRoot ".dependencies-complete"
    & $envPython -X utf8 -W ignore -c "import pyopenjtalk, librosa, gradio, onnxruntime"
    if ((Test-Path -LiteralPath $dependenciesMarker) -and $LASTEXITCODE -eq 0) {
        Complete-Step 4 "GPT-SoVITS 依赖已存在，已跳过" -Skipped
    } else {
        & $envPython -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: --find-links $wheelDir -r $pipLock
        if ($LASTEXITCODE -ne 0) { throw "GPT-SoVITS 依赖安装失败" }
        $sitePackages = (& $envPython -X utf8 -c "import site; print(site.getsitepackages()[0])").Trim()
        $jiebaFastRoot = Join-Path $sitePackages "jieba_fast"
        New-Item -ItemType Directory -Force -Path $jiebaFastRoot | Out-Null
        [System.IO.File]::WriteAllText((Join-Path $jiebaFastRoot "__init__.py"), "from jieba import *`nfrom jieba import setLogLevel`n", $Utf8)
        [System.IO.File]::WriteAllText((Join-Path $jiebaFastRoot "posseg.py"), "from jieba.posseg import *`n", $Utf8)
        & $envPython -X utf8 -c "import jieba_fast, jieba_fast.posseg, opencc, pyopenjtalk"
        if ($LASTEXITCODE -ne 0) { throw "Windows 兼容依赖导入验证失败" }
        Set-Content -LiteralPath $dependenciesMarker -Value (Get-Date).ToString("o") -Encoding Ascii
        Complete-Step 4 "GPT-SoVITS 依赖已安装"
    }
    # ModelScope itself can be present after a no-deps upstream install while
    # the denoise pipeline is still unusable.  Validate the exact import used
    # by cmd-denoise.py and repair only its missing runtime dependencies.
    & $envPython -X utf8 -W ignore -c "import addict, datasets, simplejson, sortedcontainers; from modelscope.pipelines import pipeline"
    if ($LASTEXITCODE -ne 0) {
        & $envPython -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: --find-links $wheelDir -r $pipLock
        if ($LASTEXITCODE -ne 0) { throw "智能降噪依赖安装失败" }
        & $envPython -X utf8 -W ignore -c "import simplejson, sortedcontainers; from modelscope.pipelines import pipeline"
        if ($LASTEXITCODE -ne 0) { throw "智能降噪依赖导入验证失败" }
    }

    Write-Host "[Runtime] 准备隔离 RVC Python 3.11 环境"
    $rvcEnvRoot = Join-Path $runtimeRoot "rvc-env"
    $rvcPython = Join-Path $rvcEnvRoot "python.exe"
    if (-not (Test-Path -LiteralPath $rvcPython)) {
        & $condaExe create -y -p $rvcEnvRoot python=3.11 pip
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $rvcPython)) { throw "RVC 隔离 Python 创建失败" }
    }
    & $rvcPython -m pip install --disable-pip-version-check --no-input torch==2.7.1+cu128 torchaudio==2.7.1+cu128 --index-url https://download.pytorch.org/whl/cu128 --extra-index-url https://pypi.org/simple
    if ($LASTEXITCODE -ne 0) { throw "RVC CUDA PyTorch 安装失败" }
    $rvcRequirements = Join-Path $resolvedDataRoot "engines\RVC\requirments_cu128_py312.txt"
    if (Test-Path -LiteralPath $rvcRequirements) {
        & $rvcPython -m pip install --disable-pip-version-check --no-input -r $rvcRequirements
        if ($LASTEXITCODE -ne 0) { throw "RVC 依赖安装失败" }
    }
    & $rvcPython -c "import torch; assert torch.__version__ == '2.7.1+cu128'; assert torch.cuda.is_available()"
    if ($LASTEXITCODE -ne 0) { throw "RVC CUDA 运行时验证失败" }
    Write-Host "[Runtime] 隔离 RVC 环境已就绪"

    Start-Step 5 "安装 FFmpeg 与预训练模型"
    $modelsMarker = Join-Path $runtimeRoot ".models-complete"
    # A schema-v1 manifest is an upgrade signal, not proof that already-installed
    # models or private tools are missing.  Verify the actual artifacts first;
    # step 6 performs a real model load and step 7 atomically records their v2 hashes.
    $coreModelsReady = (Test-Path -LiteralPath $modelsMarker) -and (Test-Path -LiteralPath (Join-Path $engineRoot "GPT_SoVITS\pretrained_models\sv")) -and (Test-Path -LiteralPath (Join-Path $engineRoot "GPT_SoVITS\text\G2PWModel"))
    $privateToolsReady = Test-InstalledFilePins -DataRoot $resolvedDataRoot -Manifest $assetManifest
    $uvrWeights = Join-Path $engineRoot "tools\uvr5\uvr5_weights"
    $uvrReady = @(Get-ChildItem -LiteralPath $uvrWeights -File -Filter "*.pth" -ErrorAction SilentlyContinue).Count -gt 0
    if ($coreModelsReady -and $DownloadUVR5 -and -not $uvrReady) {
        Write-Host "[Download] 首次使用智能优化，正在按需安装 UVR5 人声分离模型"
        $uvrArchive = Join-Path $cacheRoot "uvr5_weights.zip"
        Invoke-PinnedAssetDownload -Id "uvr5-weights" -Destination $uvrArchive -Validator ${function:Test-ZipReadable} | Out-Null
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
    # Phase 3 singing runtime is installed through the same pinned-asset path.
    # It lives beside GPT-SoVITS and never shares its Python environment.
    $rvcRoot = Join-Path $resolvedDataRoot "engines\RVC"
    $rvcMarker = Join-Path $rvcRoot ".pinned-commit"
    $rvcSourceAsset = Get-PinnedAsset -Id "rvc-v2-source"
    $rvcReady = (Test-Path -LiteralPath $rvcMarker) -and ((Get-Content -Raw -LiteralPath $rvcMarker).Trim() -eq [string]$rvcSourceAsset.version)
    if (-not $rvcReady) {
        $rvcZip = Join-Path $cacheRoot "rvc-$($rvcSourceAsset.version).zip"
        Invoke-PinnedAssetDownload -Id "rvc-v2-source" -Destination $rvcZip -Validator ${function:Test-ZipReadable} | Out-Null
        $rvcExtract = Join-Path $cacheRoot ("rvc-extract-" + [Guid]::NewGuid().ToString("N")); New-Item -ItemType Directory -Path $rvcExtract | Out-Null
        try {
            [IO.Compression.ZipFile]::ExtractToDirectory($rvcZip, $rvcExtract)
            $rvcExtracted = Get-ChildItem -LiteralPath $rvcExtract -Directory | Select-Object -First 1
            if (-not $rvcExtracted -or -not (Test-Path (Join-Path $rvcExtracted.FullName "webui.py"))) { throw "RVC 固定源码不完整" }
            if (Test-Path -LiteralPath $rvcRoot) { Move-Item -LiteralPath $rvcRoot -Destination (Join-Path (Split-Path $rvcRoot) ("RVC.invalid-" + (Get-Date -Format "yyyyMMddHHmmss"))) }
            Move-Item -LiteralPath $rvcExtracted.FullName -Destination $rvcRoot
            Set-Content -LiteralPath $rvcMarker -Value $rvcSourceAsset.version -Encoding Ascii
            foreach ($packageDir in @("train", "tools", "i18n", "configs")) {
                $init = Join-Path $rvcRoot (Join-Path $packageDir "__init__.py")
                if (-not (Test-Path -LiteralPath $init)) { New-Item -ItemType File -Path $init | Out-Null }
            }
        } finally { if (Test-Path -LiteralPath $rvcExtract) { Remove-Item -LiteralPath $rvcExtract -Recurse -Force } }
    }
    foreach ($rvcId in @("rvc-hubert-config", "rvc-hubert-preprocessor-config", "rvc-hubert-model", "rvc-rmvpe", "rvc-v2-generator", "rvc-v2-discriminator")) {
        $rvcAsset = Get-PinnedAsset -Id $rvcId
        $rvcDestination = Join-Path $resolvedDataRoot ([string]$rvcAsset.destination).Replace("/", "\")
        Invoke-PinnedAssetDownload -Id $rvcId -Destination $rvcDestination | Out-Null
    }
    }
    $modelsReady = $coreModelsReady -and $privateToolsReady -and (-not $DownloadUVR5 -or $uvrReady)
    if ($modelsReady) {
        Complete-Step 5 "FFmpeg 与预训练模型已存在，已跳过" -Skipped
    } else {
      $condaFfmpeg = Join-Path $envRoot "Library\bin\ffmpeg.exe"
      & $condaFfmpeg -version 2>$null | Out-Null
      if ($LASTEXITCODE -eq 0) {
          Copy-Item -LiteralPath $condaFfmpeg -Destination $toolsRoot -Force
          Copy-Item -LiteralPath (Join-Path $envRoot "Library\bin\ffprobe.exe") -Destination $toolsRoot -Force
      } else { throw "显式 Conda 锁环境缺少 FFmpeg；拒绝使用系统 PATH 或浮动下载" }
      & (Join-Path $toolsRoot "ffmpeg.exe") -version 2>$null | Out-Null
      if ($LASTEXITCODE -ne 0) { throw "FFmpeg 实际执行验证失败" }
      $originalPath = $env:Path
      try {
        $env:Path = @($envRoot, (Join-Path $envRoot "Scripts"), (Join-Path $envRoot "Library\bin"), (Join-Path $miniforgeRoot "Scripts"), $originalPath) -join ";"
        $installScript = Join-Path $engineRoot "install.ps1"
        $patchedScript = Join-Path $engineRoot "install-local-voice-studio.ps1"
        $content = Get-Content -Raw -LiteralPath $installScript
        $content = $content.Replace('Invoke-Pip torch torchcodec --index-url "https://download.pytorch.org/whl/cu128"', 'Write-Info "Pinned PyTorch already installed by VoiceStudio hash lock"')
        $content = $content.Replace('Invoke-Pip -r extra-req.txt --no-deps', 'Write-Info "Pinned extra dependencies already installed by VoiceStudio hash lock"')
        $content = $content.Replace('Invoke-Pip -r requirements.txt', 'Write-Info "Pinned dependencies already installed by VoiceStudio hash lock"')
        $content = $content.Replace('Write-Info "Downloading NLTK Data..."', '$nltkRoot = (python -c "import sys; print(sys.prefix)").Trim(); if (-not (Test-Path (Join-Path $nltkRoot "nltk_data\corpora\cmudict"))) { Write-Info "Downloading NLTK Data..."')
        $content = $content.Replace('Invoke-Unzip "nltk_data.zip" (python -c "import sys; print(sys.prefix)").Trim()', 'Invoke-Unzip "nltk_data.zip" $nltkRoot } else { Write-Info "NLTK Data Exists; Skip Downloading" }')
        $content = $content.Replace('Write-Info "Downloading Open JTalk Dict..."', '$openJtalkPackage = (python -c "import os, pyopenjtalk; print(os.path.dirname(pyopenjtalk.__file__))").Trim(); if (-not (Test-Path (Join-Path $openJtalkPackage "dictionary"))) { Write-Info "Downloading Open JTalk Dict..."')
        $content = $content.Replace('Write-Success "Open JTalk Dic Downloaded"', 'Write-Success "Open JTalk Dic Downloaded" } else { Write-Info "Open JTalk dictionary bundled with pyopenjtalk-plus; Skip Downloading" }')
        $content = $content.Replace('$null = Invoke-WebRequest @params -ErrorAction Stop', '$validator = if ($OutFile -like "*.zip") { ${function:Test-ZipReadable} } elseif ($OutFile -like "*.tar.gz") { ${function:Test-TarReadable} } else { $null }; $assetId = switch ([IO.Path]::GetFileName($OutFile)) { "pretrained_models.zip" { "pretrained-models" } "G2PWModel.zip" { "g2pw-model" } "uvr5_weights.zip" { "uvr5-weights" } "nltk_data.zip" { "nltk-data" } "open_jtalk_dic_utf_8-1.11.tar.gz" { "open-jtalk-dictionary" } default { throw "上游脚本请求了未登记资产：$OutFile" } }; Invoke-PinnedAssetDownload -Id $assetId -Destination $OutFile -Validator $validator | Out-Null')
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
    $verifiedFiles = [System.Collections.Generic.List[object]]::new()
    foreach ($pin in @($assetManifest.installed_file_pins)) {
        $target = Join-Path $resolvedDataRoot ([string]$pin.path).Replace("/", "\")
        Test-PinnedFile -Path $target -Asset $pin
        $verifiedFiles.Add(@{ path = [string]$pin.path; size = [Int64]$pin.size; sha256 = ([string]$pin.sha256).ToLowerInvariant(); kind = "tool" })
    }
    $modelRoots = @(
        (Join-Path $engineRoot "GPT_SoVITS\pretrained_models"),
        (Join-Path $engineRoot "GPT_SoVITS\text\G2PWModel")
    )
    foreach ($modelRoot in $modelRoots) {
        if (-not (Test-Path -LiteralPath $modelRoot)) { throw "模型目录缺失：$modelRoot" }
        foreach ($file in Get-ChildItem -LiteralPath $modelRoot -Recurse -File | Sort-Object FullName) {
            if (-not $file.FullName.StartsWith($resolvedDataRoot + "\", [StringComparison]::OrdinalIgnoreCase)) { throw "模型文件越界：$($file.FullName)" }
            $relative = $file.FullName.Substring($resolvedDataRoot.Length + 1).Replace("\", "/")
            $verifiedFiles.Add(@{ path = $relative; size = [Int64]$file.Length; sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant(); kind = "model" })
        }
    }
    $assetRecords = [System.Collections.Generic.List[object]]::new()
    foreach ($asset in @($assetManifest.assets)) {
        $assetPath = Join-Path $resolvedDataRoot ([string]$asset.destination).Replace("/", "\")
        if (Test-Path -LiteralPath $assetPath -PathType Leaf) {
            Test-PinnedFile -Path $assetPath -Asset $asset
            $assetRecords.Add(@{ id = $asset.id; version = $asset.version; size = [Int64]$asset.size; sha256 = ([string]$asset.sha256).ToLowerInvariant(); source = @($asset.urls)[0] })
        }
    }
    $condaLock = Join-Path (Split-Path -Parent $PSScriptRoot) "locks\conda-win-64.lock"
    $pipLock = Join-Path (Split-Path -Parent $PSScriptRoot) "locks\requirements-win-cu128.lock"
    $manifest = @{
        schema_version = 2
        asset_manifest_version = $assetManifest.manifest_version
        asset_manifest_sha256 = (Get-FileHash -LiteralPath $assetManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
        engine_commit = $engineCommit
        pretrained_revision = $pretrainedRevision
        engine_version = "v2ProPlus"
        python = (& $envPython -c "import sys; print(sys.version.split()[0])")
        python_executable = (& $envPython -c "import sys; print(sys.executable)")
        torch = (& $envPython -c "import torch; print(torch.__version__)")
        lockfiles = @{
            conda = @{ path = "locks/conda-win-64.lock"; sha256 = (Get-FileHash -LiteralPath $condaLock -Algorithm SHA256).Hash.ToLowerInvariant() }
            pip = @{ path = "locks/requirements-win-cu128.lock"; sha256 = $(if (Test-Path -LiteralPath $pipLock) { (Get-FileHash -LiteralPath $pipLock -Algorithm SHA256).Hash.ToLowerInvariant() } else { "not-installed" }) }
        }
        assets = $assetRecords
        verified_files = $verifiedFiles
        installed_at = (Get-Date).ToString("o")
    }
    $manifestJson = $manifest | ConvertTo-Json -Depth 8
    $manifestPath = Join-Path $runtimeRoot "install-manifest.json"
    $manifestTemp = "$manifestPath.tmp"
    [System.IO.File]::WriteAllText($manifestTemp, $manifestJson, $Utf8)
    Move-Item -LiteralPath $manifestTemp -Destination $manifestPath -Force
    Complete-Step 7 "安装清单已写入，安装完成"
    Write-Host "安装完成。现在可以离线执行合成与训练。"
} catch {
    if ($script:CurrentStep -gt 0) { Write-StepState $script:CurrentStep "failed" $_.Exception.Message }
    Write-Error $_
    if ($installLock) { $installLock.Dispose() }
    exit 1
}
if ($installLock) { $installLock.Dispose() }
