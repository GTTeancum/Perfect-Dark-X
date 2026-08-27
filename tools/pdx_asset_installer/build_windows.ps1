param(
    [string]$OutputDirectory = "$PSScriptRoot\dist"
)

$ErrorActionPreference = 'Stop'
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$icon = Join-Path $projectRoot 'Perfect Dark X.ico'
if (-not (Test-Path -LiteralPath $icon)) {
    throw "Required installer icon not found: $icon"
}

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --icon $icon `
    --name PerfectDarkXAssetInstaller `
    --paths $PSScriptRoot `
    --distpath $resolvedOutput `
    --workpath (Join-Path $resolvedOutput 'work') `
    --specpath (Join-Path $resolvedOutput 'spec') `
    (Join-Path $PSScriptRoot 'installer.py')

$installer = Join-Path $resolvedOutput 'PerfectDarkXAssetInstaller.exe'
if (-not (Test-Path -LiteralPath $installer)) {
    throw "PyInstaller did not produce $installer"
}

Write-Host "Built: $installer"
