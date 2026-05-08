# 01_init_postgis.ps1 — Create PostgreSQL database and run schema.sql
# Requires PostgreSQL 16 + PostGIS installed via EnterpriseDB installer
# Run from project root: .\scripts\01_init_postgis.ps1

param(
    [string]$DbName   = "urbangrowth",
    [string]$DbUser   = "urbangrowth",
    [string]$PgBin    = "C:\Program Files\PostgreSQL\16\bin",
    [switch]$DropFirst
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$psql    = Join-Path $PgBin "psql.exe"
$createdb = Join-Path $PgBin "createdb.exe"
$createuser = Join-Path $PgBin "createuser.exe"

if (-not (Test-Path $psql)) {
    Write-Error "psql not found at $psql. Adjust -PgBin or install PostgreSQL 16."
    exit 1
}

Write-Host "=== Initialising PostgreSQL database '$DbName' ===" -ForegroundColor Cyan

# Drop if requested
if ($DropFirst) {
    Write-Host "Dropping existing database '$DbName'..." -ForegroundColor Yellow
    & $psql -U postgres -c "DROP DATABASE IF EXISTS $DbName;" 2>$null
    & $psql -U postgres -c "DROP ROLE IF EXISTS $DbUser;" 2>$null
}

# Create role
Write-Host "Creating role '$DbUser'..." -ForegroundColor Green
& $psql -U postgres -c "DO `$`$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$DbUser') THEN CREATE ROLE $DbUser LOGIN PASSWORD 'changeme'; END IF; END `$`$;"

# Create database
Write-Host "Creating database '$DbName'..." -ForegroundColor Green
& $psql -U postgres -c "CREATE DATABASE $DbName OWNER $DbUser;" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Database '$DbName' may already exist — continuing." -ForegroundColor Yellow
}

# Run schema
Write-Host "Running schema.sql..." -ForegroundColor Green
& $psql -U $DbUser -d $DbName -f "src\urbangrowth\db\schema.sql"

Write-Host ""
Write-Host "=== Database ready ===" -ForegroundColor Cyan
Write-Host "Connection: postgresql://$DbUser@localhost:5432/$DbName" -ForegroundColor White
Write-Host "Next:       .\scripts\02_bootstrap_data.ps1" -ForegroundColor White
