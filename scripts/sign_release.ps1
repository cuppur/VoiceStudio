param(
    [Parameter(Mandatory = $true)][string[]]$Path,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9A-Fa-f]{40}$')][string]$CertificateThumbprint,
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
$signtool = @(
    "$env:ProgramFiles(x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe",
    "$env:ProgramFiles(x86)\Windows Kits\10\bin\10.0.22621.0\x64\signtool.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $signtool) {
    $candidate = Get-ChildItem "$env:ProgramFiles(x86)\Windows Kits\10\bin" -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($candidate) { $signtool = $candidate.FullName }
}
if (-not $signtool) { throw "SignTool was not found in the Windows SDK" }

$certificate = Get-ChildItem Cert:\CurrentUser\My | Where-Object {
    $_.Thumbprint -eq $CertificateThumbprint.ToUpperInvariant() -and $_.HasPrivateKey
} | Select-Object -First 1
if (-not $certificate) { throw "A matching code-signing certificate with private key was not found" }

foreach ($item in $Path) {
    $resolved = (Resolve-Path -LiteralPath $item).Path
    & $signtool sign /sha1 $CertificateThumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 $resolved
    if ($LASTEXITCODE -ne 0) { throw "Signing failed: $resolved" }
    & $signtool verify /pa /all /v $resolved
    if ($LASTEXITCODE -ne 0) { throw "Signature verification failed: $resolved" }
}
