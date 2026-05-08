#!/usr/bin/env bash
# 01_init_postgis.sh — Create PostgreSQL database and run schema.sql
# Run from project root in Git Bash: bash scripts/01_init_postgis.sh
#
# Requires:
#   - PostgreSQL 16 installed via EnterpriseDB (default path: C:\Program Files\PostgreSQL\16)
#   - PostGIS installed via Stack Builder
#   - postgres superuser accessible (set PGPASSWORD or use Windows auth)
#
# Optional (install from pgxn.org or compile):
#   - h3 + h3_postgis extensions (schema.sql will skip gracefully if absent)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# PostgreSQL bin — adjust if installed to a different drive/version
PG_BIN="/c/Program Files/PostgreSQL/16/bin"
export PATH="$PG_BIN:$PATH"

# Load .env for POSTGRES_* vars if present
if [[ -f "$PROJECT_ROOT/.env" ]]; then
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
fi

DB_NAME="${POSTGRES_DB:-urbangrowth}"
DB_USER="${POSTGRES_USER:-urbangrowth}"
DB_PASS="${POSTGRES_PASSWORD:-changeme}"
DB_HOST="${POSTGRES_HOST:-localhost}"
DB_PORT="${POSTGRES_PORT:-5432}"

export PGPASSWORD="${POSTGRES_SUPERUSER_PASSWORD:-${DB_PASS}}"

echo "========================================================"
echo " Urban Growth Research Platform — Database Init"
echo " Database : $DB_NAME @ $DB_HOST:$DB_PORT"
echo "========================================================"

# ── 1. Verify psql is available ──────────────────────────────────────────
echo ""
echo ">>> Step 1: Verifying psql..."
if ! command -v psql &>/dev/null; then
    echo "ERROR: psql not found at $PG_BIN"
    echo "  Install PostgreSQL 16: https://www.enterprisedb.com/downloads/postgres-postgresql-downloads"
    exit 1
fi
echo "    OK: $(psql --version)"

# ── 2. Create role ────────────────────────────────────────────────────────
echo ""
echo ">>> Step 2: Creating role '$DB_USER'..."
psql -U postgres -h "$DB_HOST" -p "$DB_PORT" -c \
    "DO \$\$ BEGIN
       IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$DB_USER') THEN
           CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS';
       END IF;
     END \$\$;" \
    2>/dev/null || echo "    (role may already exist — continuing)"
echo "    OK: role '$DB_USER' ready"

# ── 3. Create database ───────────────────────────────────────────────────
echo ""
echo ">>> Step 3: Creating database '$DB_NAME'..."
psql -U postgres -h "$DB_HOST" -p "$DB_PORT" -c \
    "CREATE DATABASE $DB_NAME OWNER $DB_USER;" 2>/dev/null \
    || echo "    (database may already exist — continuing)"
echo "    OK: database '$DB_NAME' ready"

# ── 4. Enable PostGIS extensions ─────────────────────────────────────────
echo ""
echo ">>> Step 4: Enabling PostGIS extensions..."
export PGPASSWORD="$DB_PASS"

psql -U "$DB_USER" -d "$DB_NAME" -h "$DB_HOST" -p "$DB_PORT" -c \
    "CREATE EXTENSION IF NOT EXISTS postgis;"
echo "    OK: postgis"

psql -U "$DB_USER" -d "$DB_NAME" -h "$DB_HOST" -p "$DB_PORT" -c \
    "CREATE EXTENSION IF NOT EXISTS postgis_raster;" \
    || echo "    WARNING: postgis_raster not available — skipping"
echo "    OK: postgis_raster (or skipped)"

# h3 is optional — schema.sql handles missing extensions gracefully
psql -U "$DB_USER" -d "$DB_NAME" -h "$DB_HOST" -p "$DB_PORT" -c \
    "CREATE EXTENSION IF NOT EXISTS h3;" 2>/dev/null \
    && echo "    OK: h3" \
    || echo "    INFO: h3 extension not installed (optional — h3-py handles all H3 ops in Python)"

psql -U "$DB_USER" -d "$DB_NAME" -h "$DB_HOST" -p "$DB_PORT" -c \
    "CREATE EXTENSION IF NOT EXISTS h3_postgis;" 2>/dev/null \
    && echo "    OK: h3_postgis" \
    || echo "    INFO: h3_postgis extension not installed (optional)"

# ── 5. Run schema.sql ────────────────────────────────────────────────────
echo ""
echo ">>> Step 5: Running schema.sql..."
psql -U "$DB_USER" -d "$DB_NAME" -h "$DB_HOST" -p "$DB_PORT" \
    -f "$PROJECT_ROOT/src/urbangrowth/db/schema.sql"
echo "    OK: schema applied"

# ── 6. Verify tables ─────────────────────────────────────────────────────
echo ""
echo ">>> Step 6: Verifying tables..."
TABLE_COUNT=$(psql -U "$DB_USER" -d "$DB_NAME" -h "$DB_HOST" -p "$DB_PORT" -t -c \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';" | tr -d ' ')
echo "    OK: $TABLE_COUNT tables created in public schema"

# Verify PostGIS version
PG_VERSION=$(psql -U "$DB_USER" -d "$DB_NAME" -h "$DB_HOST" -p "$DB_PORT" -t -c \
    "SELECT PostGIS_full_version();" | tr -d ' \n' | cut -c1-60)
echo "    OK: $PG_VERSION..."

echo ""
echo "========================================================"
echo " Database ready: $DB_USER@$DB_HOST:$DB_PORT/$DB_NAME"
echo " Next: conda activate urbangrowth && ug doctor"
echo "========================================================"
