param(
    [string]$Xbe = "build-xbox-gputrace/default.xbe",
    [string]$TexturePack = "texturepack-work/release/ext_tex.pak",
    [Parameter(Mandatory = $true)]
    [string]$BuildId
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sourceXbe = [IO.Path]::GetFullPath((Join-Path $repoRoot $Xbe))
$sourceTexturePack = [IO.Path]::GetFullPath((Join-Path $repoRoot $TexturePack))
$deployDir = Join-Path $repoRoot "build-xbox/release/Perfect Dark X"
$targetXbe = Join-Path $deployDir "default.xbe"
$targetTexturePack = Join-Path $deployDir "ext_tex.pak"
$manifestPath = Join-Path $deployDir "pdx-install.json"
$markerPath = Join-Path $deployDir "DEPLOYMENT-BUILD.txt"

if (-not (Test-Path -LiteralPath $sourceXbe -PathType Leaf)) {
    throw "XBE not found: $sourceXbe"
}
if (-not (Test-Path -LiteralPath $sourceTexturePack -PathType Leaf)) {
    throw "Texture pack not found: $sourceTexturePack"
}

foreach ($required in @("files", "segs", "filenames.lst", "pdx-install.json")) {
    if (-not (Test-Path -LiteralPath (Join-Path $deployDir $required))) {
        throw "Canonical deployment is incomplete: missing $required"
    }
}

Copy-Item -LiteralPath $sourceXbe -Destination $targetXbe -Force
Copy-Item -LiteralPath $sourceTexturePack -Destination $targetTexturePack -Force

$xbeInfo = Get-Item -LiteralPath $targetXbe
$hash = (Get-FileHash -LiteralPath $targetXbe -Algorithm SHA256).Hash
$textureInfo = Get-Item -LiteralPath $targetTexturePack
$textureHash = (Get-FileHash -LiteralPath $targetTexturePack -Algorithm SHA256).Hash
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$manifest.core.sha256 = $hash.ToLowerInvariant()
$manifest.texture_pack.sha256 = $textureHash.ToLowerInvariant()
$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding utf8

$marker = @"
Perfect Dark X canonical hardware deployment

Runtime build ID: $BuildId
default.xbe SHA-256: $hash
default.xbe size: $($xbeInfo.Length) bytes
Texture pack: ext_tex.pak
ext_tex.pak SHA-256: $textureHash
ext_tex.pak size: $($textureInfo.Length) bytes

This is the only folder used for hardware deployment and testing:
$deployDir

For an existing Xbox installation, copy default.xbe from this folder.
For a clean installation, FTP the entire Perfect Dark X folder.

Diagnostic builds must write their runtime build ID near the top of pd.log.
The current build writes:
NV2A TRACE BUILD id=$BuildId
"@
$marker | Set-Content -LiteralPath $markerPath -Encoding ascii

Write-Output "Staged: $targetXbe"
Write-Output "Build : $BuildId"
Write-Output "SHA256: $hash"
Write-Output "PDTX  : $textureHash"
