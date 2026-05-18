-- Urban Growth Research Platform — PostgreSQL schema
-- Requires: PostgreSQL 16+; PostGIS 3.4 is OPTIONAL (geometry columns skipped if absent)
-- h3 / h3_postgis are optional (install via pgxn on Windows if needed)
-- Run: psql -U urbangrowth -d urbangrowth -f src/urbangrowth/db/schema.sql

-- ── EXTENSIONS ───────────────────────────────────────────────────────────────
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS postgis;
    CREATE EXTENSION IF NOT EXISTS postgis_raster;
    RAISE NOTICE 'PostGIS extensions loaded.';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'PostGIS not available — geometry columns will be omitted. Install PostGIS 3.4+ for full spatial support.';
END;
$$;

-- h3 / h3_postgis: optional on Windows — all H3 logic can run in Python (h3-py)
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS h3;
    CREATE EXTENSION IF NOT EXISTS h3_postgis;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'h3/h3_postgis extensions not available — H3 operations will use Python h3-py';
END;
$$;

-- ── CITY-LEVEL TABLES ─────────────────────────────────────────────────────────

-- cities table: bbox geometry column added conditionally if PostGIS is present
CREATE TABLE IF NOT EXISTS cities (
    city_id     SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,     -- "phoenix" | "austin"
    state       CHAR(2) NOT NULL,
    utm_epsg    INTEGER NOT NULL           -- 32612 (Phoenix) | 32614 (Austin)
);

DO $$
BEGIN
    ALTER TABLE cities ADD COLUMN IF NOT EXISTS bbox geometry(Polygon, 4326);
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Skipping cities.bbox geometry column (PostGIS unavailable).';
END;
$$;

-- Seed the two cities
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'st_makeenvelope') THEN
        INSERT INTO cities (name, state, bbox, utm_epsg) VALUES
            ('phoenix', 'AZ',
             ST_MakeEnvelope(-112.35, 33.20, -111.65, 33.85, 4326), 32612),
            ('austin',  'TX',
             ST_MakeEnvelope(-97.98,  30.10, -97.40,  30.65, 4326), 32614)
        ON CONFLICT (name) DO NOTHING;
    ELSE
        INSERT INTO cities (name, state, utm_epsg) VALUES
            ('phoenix', 'AZ', 32612),
            ('austin',  'TX', 32614)
        ON CONFLICT (name) DO NOTHING;
    END IF;
END;
$$;

-- zoning_snapshots: geometry column added conditionally
CREATE TABLE IF NOT EXISTS zoning_snapshots (
    id                      BIGSERIAL PRIMARY KEY,
    city_id                 INTEGER NOT NULL REFERENCES cities(city_id),
    snapshot_date           DATE NOT NULL,
    parcel_id               TEXT,
    zone_code_raw           TEXT,
    zone_code_normalized    TEXT    -- residential | commercial | industrial | mixed | open_space
);

DO $$
BEGIN
    ALTER TABLE zoning_snapshots ADD COLUMN IF NOT EXISTS geometry geometry(MultiPolygon, 4326);
    CREATE INDEX IF NOT EXISTS idx_zoning_geom ON zoning_snapshots USING GIST(geometry);
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Skipping zoning_snapshots.geometry (PostGIS unavailable).';
END;
$$;
CREATE INDEX IF NOT EXISTS idx_zoning_city_date ON zoning_snapshots (city_id, snapshot_date);

-- city_permits: geometry column added conditionally
CREATE TABLE IF NOT EXISTS city_permits (
    permit_id   TEXT NOT NULL,
    city_id     INTEGER NOT NULL REFERENCES cities(city_id),
    issue_date  DATE,
    type        TEXT,           -- residential_new | commercial_new | addition | remodel
    valuation   NUMERIC(14,2),
    PRIMARY KEY (permit_id, city_id)
);

DO $$
BEGIN
    ALTER TABLE city_permits ADD COLUMN IF NOT EXISTS geometry geometry(Point, 4326);
    CREATE INDEX IF NOT EXISTS idx_city_permits_geom ON city_permits USING GIST(geometry);
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Skipping city_permits.geometry (PostGIS unavailable).';
END;
$$;
CREATE INDEX IF NOT EXISTS idx_city_permits_date ON city_permits (issue_date);

-- parcels: geometry column added conditionally
CREATE TABLE IF NOT EXISTS parcels (
    parcel_id   TEXT NOT NULL,
    city_id     INTEGER NOT NULL REFERENCES cities(city_id),
    acreage     NUMERIC(10,4),
    year_built  SMALLINT,
    PRIMARY KEY (parcel_id, city_id)
);

DO $$
BEGIN
    ALTER TABLE parcels ADD COLUMN IF NOT EXISTS geometry geometry(MultiPolygon, 4326);
    CREATE INDEX IF NOT EXISTS idx_parcels_geom ON parcels USING GIST(geometry);
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Skipping parcels.geometry (PostGIS unavailable).';
END;
$$;

CREATE TABLE IF NOT EXISTS sentinel_scenes (
    scene_id    TEXT PRIMARY KEY,
    city_id     INTEGER REFERENCES cities(city_id),
    date        DATE NOT NULL,
    cloud_pct   NUMERIC(5,2),
    file_path   TEXT NOT NULL   -- relative to URBANGROWTH_DATA_ROOT
);
CREATE INDEX IF NOT EXISTS idx_sentinel_city_date ON sentinel_scenes (city_id, date);

CREATE TABLE IF NOT EXISTS landcover_rasters (
    id              SERIAL PRIMARY KEY,
    city_id         INTEGER NOT NULL REFERENCES cities(city_id),
    date            DATE NOT NULL,
    file_path       TEXT NOT NULL,
    model_version   TEXT,
    UNIQUE (city_id, date, model_version)
);

CREATE TABLE IF NOT EXISTS h3_features (
    h3_index            TEXT NOT NULL,      -- H3 hex address string
    city_id             INTEGER NOT NULL REFERENCES cities(city_id),
    date                DATE NOT NULL,
    built_pct           NUMERIC(6,4),
    veg_pct             NUMERIC(6,4),
    transitions_jsonb   JSONB,              -- {"bare→built": n, "veg→built": n, ...}
    mean_elevation      NUMERIC(8,2),
    mean_slope          NUMERIC(6,3),
    road_density        NUMERIC(8,4),       -- km of road per km²
    permit_count        INTEGER,
    permit_valuation    NUMERIC(14,2),
    PRIMARY KEY (h3_index, city_id, date)
);
CREATE INDEX IF NOT EXISTS idx_h3_city_date ON h3_features (city_id, date);

-- ── NATIONAL ALT DATA ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS census_bps (
    period                  DATE NOT NULL,          -- first day of month
    jurisdiction_id         TEXT NOT NULL,           -- FIPS or "national"
    jurisdiction_name       TEXT,
    state                   CHAR(2),
    msa_code                TEXT,
    residential_permits     INTEGER,
    commercial_permits      INTEGER,
    total_valuation         NUMERIC(16,2),
    PRIMARY KEY (period, jurisdiction_id)
);
CREATE INDEX IF NOT EXISTS idx_bps_state ON census_bps (state, period);

CREATE TABLE IF NOT EXISTS ferc_queue (
    snapshot_date           DATE NOT NULL,
    project_id              TEXT NOT NULL,
    region                  TEXT,           -- ISO/RTO region
    fuel_type               TEXT,           -- solar | wind | natural_gas | battery | other
    mw_capacity             NUMERIC(10,2),
    status                  TEXT,           -- active | withdrawn | operational
    queue_date              DATE,
    expected_inservice_date DATE,
    withdrawn               BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (snapshot_date, project_id)
);
CREATE INDEX IF NOT EXISTS idx_ferc_status ON ferc_queue (status, snapshot_date);

CREATE TABLE IF NOT EXISTS usaspending_awards (
    award_id                TEXT PRIMARY KEY,
    award_date              DATE,
    naics                   TEXT,
    recipient_name          TEXT,
    recipient_ticker_guess  TEXT,           -- populated by signals/usaspending_signals.py
    amount                  NUMERIC(16,2),
    state                   CHAR(2),
    performance_zip         TEXT
);
CREATE INDEX IF NOT EXISTS idx_awards_naics       ON usaspending_awards (naics, award_date);
CREATE INDEX IF NOT EXISTS idx_awards_ticker      ON usaspending_awards (recipient_ticker_guess);
CREATE INDEX IF NOT EXISTS idx_awards_state_date  ON usaspending_awards (state, award_date);

CREATE TABLE IF NOT EXISTS fred_series (
    series_id   TEXT NOT NULL,
    date        DATE NOT NULL,
    value       NUMERIC(16,6),
    PRIMARY KEY (series_id, date)
);

CREATE TABLE IF NOT EXISTS dot_tips (
    state           CHAR(2) NOT NULL,
    project_id      TEXT NOT NULL,
    fiscal_year     SMALLINT,
    status          TEXT,
    project_type    TEXT,           -- highway | transit | bridge | other
    total_cost      NUMERIC(14,2),
    county          TEXT,
    awarded_date    DATE,
    PRIMARY KEY (state, project_id)
);
CREATE INDEX IF NOT EXISTS idx_dot_tips_state ON dot_tips (state, fiscal_year);

-- ── EQUITY UNIVERSE ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tickers (
    symbol              TEXT PRIMARY KEY,
    name                TEXT,
    sector_tag          TEXT,           -- matches universe.yaml sector_tag values
    primary_signals     JSONB           -- ["ferc_queue", "census_bps", ...]
);

CREATE TABLE IF NOT EXISTS prices (
    symbol      TEXT NOT NULL,
    date        DATE NOT NULL,
    open        NUMERIC(12,4),
    high        NUMERIC(12,4),
    low         NUMERIC(12,4),
    close       NUMERIC(12,4),
    volume      BIGINT,
    adj_close   NUMERIC(12,4),
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices (date);

CREATE TABLE IF NOT EXISTS returns (
    symbol              TEXT NOT NULL,
    date                DATE NOT NULL,
    daily_ret           NUMERIC(10,6),
    monthly_ret         NUMERIC(10,6),
    excess_ret_spy      NUMERIC(10,6),
    excess_ret_sector   NUMERIC(10,6),
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_returns_date ON returns (date);

-- Idempotent migration for existing databases
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'returns' AND column_name = 'excess_ret_sector'
    ) THEN
        ALTER TABLE returns ADD COLUMN excess_ret_sector NUMERIC(10,6);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'ferc_queue' AND column_name = 'iso'
    ) THEN
        ALTER TABLE ferc_queue ADD COLUMN iso TEXT;
        ALTER TABLE ferc_queue ADD COLUMN project_name TEXT;
        ALTER TABLE ferc_queue ADD COLUMN state CHAR(2);
        ALTER TABLE ferc_queue ADD COLUMN county TEXT;
    END IF;
END $$;

-- ── SIGNAL FEATURE STORE ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS signal_features (
    symbol          TEXT NOT NULL,
    period          DATE NOT NULL,      -- first day of month
    feature_name    TEXT NOT NULL,      -- e.g. "permit_yoy_growth_1m_lag"
    feature_value   NUMERIC(16,6),
    source          TEXT,               -- "census_bps" | "ferc_queue" | etc.
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, period, feature_name)
);
CREATE INDEX IF NOT EXISTS idx_features_source ON signal_features (source, period);
