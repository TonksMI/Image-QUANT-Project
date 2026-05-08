# 02_bootstrap_data.ps1 — Run initial data ingestion (markets + FRED + Census BPS)
# Run from project root after 01_init_postgis.ps1: .\scripts\02_bootstrap_data.ps1
# Full Sentinel-2 / satellite pipeline is separate (large downloads).

param(
    [string]$EnvName   = "urbangrowth",
    [string]$StartYear = "2015"
)

Set-StrictMode -Version Latest

function Run-Step {
    param([string]$Label, [string]$Command)
    Write-Host ""
    Write-Host ">>> $Label" -ForegroundColor Cyan
    Invoke-Expression "conda run -n $EnvName $Command"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "$Label failed (exit $LASTEXITCODE) — continuing."
    }
}

Write-Host "=== Bootstrap Data Ingestion ===" -ForegroundColor Cyan
Write-Host "Env: $EnvName | Start: $StartYear" -ForegroundColor White

Run-Step "Equity markets (yfinance)"          "urbangrowth ingest markets"
Run-Step "FRED macro series"                   "urbangrowth ingest fred"
Run-Step "Census BPS ($StartYear-present)"     "urbangrowth ingest census-bps --start-year $StartYear"
Run-Step "FERC interconnection queue"          "urbangrowth ingest ferc-queue"
Run-Step "USASpending awards"                  "urbangrowth ingest usaspending --start $StartYear-01-01"

Write-Host ""
Write-Host "=== Bootstrap complete ===" -ForegroundColor Cyan
Write-Host "Satellite + city data: run per-city ingestion steps individually" -ForegroundColor White
Write-Host "  urbangrowth ingest sentinel2 --city phoenix" -ForegroundColor DarkGray
Write-Host "  urbangrowth ingest city-permits --city phoenix" -ForegroundColor DarkGray
Write-Host "  urbangrowth ingest parcels --city phoenix" -ForegroundColor DarkGray
