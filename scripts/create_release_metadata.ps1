param(
    [Parameter(Mandatory = $true)][string[]]$Artifact,
    [string]$OutputDirectory = "release-metadata",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    $out = [System.IO.Path]::GetFullPath($OutputDirectory)
} else {
    $out = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDirectory))
}
New-Item -ItemType Directory -Path $out -Force | Out-Null

# --- SHA256SUMS -------------------------------------------------------------
$lines = foreach ($item in $Artifact) {
    $resolved = (Resolve-Path -LiteralPath $item).Path
    $hash = (Get-FileHash -LiteralPath $resolved -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $([System.IO.Path]::GetFileName($resolved))"
}
[System.IO.File]::WriteAllLines((Join-Path $out "SHA256SUMS.txt"), $lines, [System.Text.UTF8Encoding]::new($false))

# --- SBOM (CycloneDX JSON + SPDX 2.3 tag-value) ------------------------------
# cyclonedx-py 仅支持 JSON/XML 输出（无 SPDX），故先用它生成 CycloneDX JSON，
# 再用 lib4sbom 转换为 SPDX 2.3 tag-value。
$python = $null
if ($Python -ne "") {
    $python = Get-Item -LiteralPath $Python -ErrorAction SilentlyContinue
    if (-not $python) { throw "指定的 Python 路径不存在：$Python" }
} else {
    $candidates = @()
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { $python = Get-Item -LiteralPath $candidate; break }
    }
}
if (-not $python) { throw "Python is required to create the SBOM" }
$pythonSource = [string]$python
if ([System.IO.Path]::IsPathRooted($pythonSource) -eq $false) { $pythonSource = $python.FullName }
$hasCycloneDx = & $pythonSource -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('cyclonedx_py') else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "未安装 cyclonedx-py，请先安装：$pythonSource -m pip install 'cyclonedx-bom>=5,<7'"
}
$hasLib4Sbom = & $pythonSource -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('lib4sbom') else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "未安装 lib4sbom（用于 CycloneDX 转 SPDX），请先安装：$pythonSource -m pip install lib4sbom"
}
& $pythonSource -m cyclonedx_py environment --output-format JSON --output-file (Join-Path $out "sbom.cdx.json")
if ($LASTEXITCODE -ne 0) { throw "CycloneDX SBOM generation failed" }
$cdxJson = Join-Path $out "sbom.cdx.json"
$spdxOut = Join-Path $out "sbom.spdx"
$spdxScript = @"
from lib4sbom.parser import SBOMParser
from lib4sbom.generator import SBOMGenerator
parser = SBOMParser()
parser.parse_file(r"$cdxJson")
data = parser.get_sbom()
generator = SBOMGenerator(sbom_type="spdx", format="tag")
generator.generate("LocalVoiceStudio", data, filename=r"$spdxOut")
print("SPDX OK")
"@
$scriptFile = Join-Path $out "convert_spdx.py"
[System.IO.File]::WriteAllText($scriptFile, $spdxScript, [System.Text.UTF8Encoding]::new($false))
& $pythonSource $scriptFile
if ($LASTEXITCODE -ne 0) { throw "SPDX SBOM generation failed" }
Remove-Item -LiteralPath $scriptFile -Force
