# 00_setup_env.ps1 — Create conda environment and install PyTorch nightly
# Run from the project root: .\scripts\00_setup_env.ps1

param(
    [string]$EnvName = "urbangrowth",
    [switch]$ForceRecreate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "=== Urban Growth Research Platform — Environment Setup ===" -ForegroundColor Cyan

# 1. Check conda is available
if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    Write-Error "conda not found. Install Miniconda: https://docs.conda.io/en/latest/miniconda.html"
    exit 1
}

# 2. Create or recreate the environment
if ($ForceRecreate) {
    Write-Host "Removing existing '$EnvName' environment..." -ForegroundColor Yellow
    conda env remove -n $EnvName -y 2>$null
}

$envExists = (conda env list | Select-String $EnvName).Count -gt 0
if ($envExists -and -not $ForceRecreate) {
    Write-Host "Environment '$EnvName' already exists. Use -ForceRecreate to rebuild." -ForegroundColor Yellow
} else {
    Write-Host "Creating conda environment from environment.yml..." -ForegroundColor Green
    conda env create -f environment.yml -n $EnvName
    Write-Host "Conda environment created." -ForegroundColor Green
}

# 3. Install PyTorch nightly with CUDA 12.8 (RTX 5070 Blackwell sm_120)
Write-Host "Installing PyTorch nightly (CUDA 12.8)..." -ForegroundColor Green
conda run -n $EnvName pip install --pre torch torchvision `
    --index-url https://download.pytorch.org/whl/nightly/cu128

# 4. Install the urbangrowth package in editable mode
Write-Host "Installing urbangrowth package (editable)..." -ForegroundColor Green
conda run -n $EnvName pip install -e .

# 5. Create data directories
$DataRoot = "C:\urbangrowth_data"
$Subdirs = @(
    "raw\census_bps", "raw\ferc_queue", "raw\usaspending", "raw\fred",
    "raw\sentinel2\phoenix", "raw\sentinel2\austin",
    "raw\elevation\phoenix", "raw\elevation\austin",
    "raw\osm\phoenix", "raw\osm\austin",
    "raw\city_permits\phoenix", "raw\city_permits\austin",
    "raw\parcels\phoenix", "raw\parcels\austin",
    "raw\zoning\phoenix", "raw\zoning\austin",
    "raw\markets", "raw\census_acs\phoenix", "raw\census_acs\austin",
    "raw\dot_tips\arizona", "raw\dot_tips\texas",
    "processed\composites\phoenix", "processed\composites\austin",
    "processed\land_cover\phoenix", "processed\land_cover\austin",
    "processed\change\phoenix", "processed\change\austin",
    "processed\h3_features", "processed\signals", "processed\features",
    "models"
)
foreach ($sub in $Subdirs) {
    New-Item -ItemType Directory -Force -Path "$DataRoot\$sub" | Out-Null
}
Write-Host "Data directories created at $DataRoot" -ForegroundColor Green

# 6. Copy .env.example if .env doesn't exist
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host ".env created from .env.example — fill in your API keys." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Cyan
Write-Host "Activate: conda activate $EnvName" -ForegroundColor White
Write-Host "Next:     .\scripts\01_init_postgis.ps1" -ForegroundColor White
