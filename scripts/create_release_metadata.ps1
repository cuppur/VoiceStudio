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

# --- Product SBOM (CycloneDX JSON + SPDX 2.3 tag-value) -----------------------
# Keep this generation dependency-free and merge the runtime manifest with the
# installed environment.  Unknown licenses are emitted as NOASSERTION.
$resolvedPython = $null
if ($Python -ne "") {
    $resolvedPython = Get-Item -LiteralPath $Python -ErrorAction SilentlyContinue
    if (-not $resolvedPython) { throw "指定的 Python 路径不存在：$Python" }
} else {
    $candidates = @()
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { $resolvedPython = Get-Item -LiteralPath $candidate; break }
    }
}
if (-not $resolvedPython) { throw "Python is required to create the SBOM" }
$pythonSource = [string]$resolvedPython
if ([System.IO.Path]::IsPathRooted($pythonSource) -eq $false) { $pythonSource = $resolvedPython.FullName }
$sbomScript = Join-Path $repoRoot "scripts/create_product_sbom.py"
& $pythonSource $sbomScript --output-dir $out --project-root $repoRoot
if ($LASTEXITCODE -ne 0) { throw "Product SBOM generation failed" }
