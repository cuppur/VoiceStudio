param(
    [Parameter(Mandatory = $true)][string[]]$Artifact,
    [string]$OutputDirectory = "release-metadata"
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$out = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDirectory))
New-Item -ItemType Directory -Path $out -Force | Out-Null
$lines = foreach ($item in $Artifact) {
    $resolved = (Resolve-Path -LiteralPath $item).Path
    $hash = (Get-FileHash -LiteralPath $resolved -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $([System.IO.Path]::GetFileName($resolved))"
}
[System.IO.File]::WriteAllLines((Join-Path $out "SHA256SUMS.txt"), $lines, [System.Text.UTF8Encoding]::new($false))

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw "Python is required to create the CycloneDX SBOM" }
& $python.Source -m cyclonedx_py environment --output-format JSON --output-file (Join-Path $out "sbom.cdx.json")
if ($LASTEXITCODE -ne 0) { throw "CycloneDX SBOM generation failed" }
