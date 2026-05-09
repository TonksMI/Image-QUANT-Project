"""City-level building permit ingestion — Phoenix and Austin open data portals.

Downloads issued construction permits, normalises to a common schema,
geocodes missing coordinates via the Census batch geocoder, and upserts
to the city_permits PostgreSQL table.

Idempotent: caches each month's data as parquet and skips months already on disk.

Public API:
    ingest_phoenix(since="2015-01-01") -> pd.DataFrame
    ingest_austin(since="2015-01-01")  -> pd.DataFrame
    run(city="phoenix", since="2015-01-01") -> None

CLI:
    python -m urbangrowth.data.city_permits --city phoenix --since 2015-01-01
"""
from __future__ import annotations

import io
import os
import time
from pathlib import Path
from typing import Iterator

import geopandas as gpd
import pandas as pd
import requests
import structlog
from dotenv import load_dotenv
from shapely.geometry import Point
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from urbangrowth.config import data_path, get_pipeline
from urbangrowth.db import loaders

load_dotenv()

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CITY_IDS: dict[str, int] = {"phoenix": 1, "austin": 2}

PHOENIX_SOCRATA_URL = "https://www.phoenixopendata.com/resource/dvuu-gqtg.json"
PHOENIX_CKAN_URL = "https://www.phoenixopendata.com/api/3/action/datastore_search"
PHOENIX_RESOURCE_ID = "dvuu-gqtg"

AUSTIN_SOCRATA_URL = "https://data.austintexas.gov/resource/3syk-w9eu.json"

CENSUS_GEOCODER_URL = (
    "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
)

_PAGE_LIMIT = 50_000
_GEOCODE_BATCH_SIZE = 1_000
_GEOCODE_SLEEP_S = 1.0

# ---------------------------------------------------------------------------
# Permit type taxonomy
# ---------------------------------------------------------------------------

PERMIT_TYPE_MAP: dict[str, str] = {
    # Phoenix raw values (case-insensitive substring match)
    "new single family":        "residential_new",
    "new multi":                "residential_new",
    "new commercial":           "commercial_new",
    # Austin raw values
    "new construction single":  "residential_new",
    "new construction multi":   "residential_new",
    "new construction commerc": "commercial_new",
    # Shared
    "addition":                 "addition",
    "remodel":                  "remodel",
    "demolition":               "demolition",
}

# Sort by descending key-length so longest match wins
_SORTED_TYPE_KEYS = sorted(PERMIT_TYPE_MAP.keys(), key=len, reverse=True)


def _normalise_permit_type(raw: str | None) -> str:
    """Map a raw permit type string to the canonical taxonomy.

    Performs case-insensitive longest-substring-first matching.
    Returns ``"other"`` when nothing matches.
    """
    if not raw:
        return "other"
    lower = str(raw).lower()
    for key in _SORTED_TYPE_KEYS:
        if key in lower:
            return PERMIT_TYPE_MAP[key]
    return "other"


# ---------------------------------------------------------------------------
# HTTP helpers with retry
# ---------------------------------------------------------------------------

@retry(
    retry=retry_if_exception_type(requests.HTTPError),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _get_json(url: str, params: dict) -> list[dict] | dict:
    resp = requests.get(url, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


@retry(
    retry=retry_if_exception_type(requests.HTTPError),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _post_form(url: str, data: dict, files: dict) -> requests.Response:
    resp = requests.post(url, data=data, files=files, timeout=120)
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Census Geocoder
# ---------------------------------------------------------------------------

def geocode_batch(addresses: list[str]) -> dict[str, tuple[float, float]]:
    """POST addresses to the Census Geocoder batch API.

    Args:
        addresses: List of full street-address strings (may include city/state/zip).
            The function treats each element as the street portion;
            city defaults to the empty string and state defaults to the empty string.
            Pass pre-formatted strings of the form "123 Main St, Phoenix, AZ 85001"
            if you want richer context; the function will split on the first comma.

    Returns:
        Mapping of original address string -> (latitude, longitude).
        Unmatched or ambiguous addresses are omitted from the result.
    """
    result: dict[str, tuple[float, float]] = {}
    if not addresses:
        return result

    total_batches = (len(addresses) + _GEOCODE_BATCH_SIZE - 1) // _GEOCODE_BATCH_SIZE
    log.info("geocode_start", total_addresses=len(addresses), batches=total_batches)

    for batch_idx in range(total_batches):
        batch = addresses[batch_idx * _GEOCODE_BATCH_SIZE:
                          (batch_idx + 1) * _GEOCODE_BATCH_SIZE]

        # Build CSV: id,street,city,state,zip
        lines = ["id,street,city,state,zip"]
        for i, addr in enumerate(batch):
            # Best-effort split: "123 Main St, Phoenix, AZ 85001"
            parts = [p.strip() for p in addr.split(",")]
            street = parts[0] if len(parts) > 0 else addr
            city   = parts[1] if len(parts) > 1 else ""
            state  = ""
            zipcd  = ""
            if len(parts) > 2:
                # "AZ 85001" or just "85001"
                rest = parts[2].strip()
                tokens = rest.split()
                if len(tokens) == 2:
                    state, zipcd = tokens[0], tokens[1]
                elif len(tokens) == 1:
                    # could be just a zip or just a state
                    if tokens[0].isdigit():
                        zipcd = tokens[0]
                    else:
                        state = tokens[0]
            safe = lambda s: s.replace('"', '').replace(',', ' ')
            lines.append(f'{i},"{safe(street)}","{safe(city)}","{safe(state)}","{safe(zipcd)}"')

        csv_content = "\n".join(lines).encode("utf-8")

        try:
            resp = _post_form(
                CENSUS_GEOCODER_URL,
                data={
                    "benchmark": "Public_AR_Current",
                    "returntype": "locations",
                    "vintage": "Current_Current",
                },
                files={"addressFile": ("addresses.csv", io.BytesIO(csv_content), "text/csv")},
            )
        except Exception as exc:
            log.warning("geocode_batch_failed", batch=batch_idx, error=str(exc))
            if batch_idx < total_batches - 1:
                time.sleep(_GEOCODE_SLEEP_S)
            continue

        # Parse the CSV response
        # Format: id, input_address, match, match_type, matched_address, coordinates, tiger_id, tiger_side
        try:
            resp_df = pd.read_csv(
                io.StringIO(resp.text),
                header=None,
                names=["id", "input_address", "match", "match_type",
                       "matched_address", "coordinates", "tiger_id", "tiger_side"],
                dtype=str,
            )
            matched = resp_df[resp_df["match"].str.strip().str.upper() == "MATCH"]
            for _, row in matched.iterrows():
                idx = int(row["id"])
                coords_str = str(row["coordinates"]).strip()
                if "," in coords_str:
                    lon_s, lat_s = coords_str.split(",", 1)
                    try:
                        lon = float(lon_s.strip())
                        lat = float(lat_s.strip())
                        result[batch[idx]] = (lat, lon)
                    except ValueError:
                        pass
        except Exception as exc:
            log.warning("geocode_parse_failed", batch=batch_idx, error=str(exc))

        log.info(
            "geocode_batch_done",
            batch=batch_idx + 1,
            of=total_batches,
            matched_this_batch=len([k for k in result]),
        )

        if batch_idx < total_batches - 1:
            time.sleep(_GEOCODE_SLEEP_S)

    log.info("geocode_complete", matched=len(result), total=len(addresses))
    return result


# ---------------------------------------------------------------------------
# Phoenix ingestion
# ---------------------------------------------------------------------------

def _phoenix_socrata_pages(since: str) -> Iterator[list[dict]]:
    """Yield pages of Phoenix permit records from the Socrata endpoint."""
    offset = 0
    while True:
        params = {
            "$limit":  _PAGE_LIMIT,
            "$offset": offset,
            "$where":  f"issue_date >= '{since}'",
        }
        page: list[dict] = _get_json(PHOENIX_SOCRATA_URL, params)  # type: ignore[assignment]
        log.info(
            "phoenix_socrata_page",
            offset=offset,
            rows=len(page),
        )
        if not page:
            break
        yield page
        if len(page) < _PAGE_LIMIT:
            break
        offset += _PAGE_LIMIT


def _phoenix_ckan_pages(since: str) -> Iterator[list[dict]]:
    """Yield pages of Phoenix permit records from the CKAN fallback endpoint."""
    import json as _json

    offset = 0
    limit = 32_000
    while True:
        params = {
            "resource_id": PHOENIX_RESOURCE_ID,
            "limit":       limit,
            "offset":      offset,
            "filters":     _json.dumps({"STATUS": "Issued"}),
        }
        resp: dict = _get_json(PHOENIX_CKAN_URL, params)  # type: ignore[assignment]
        records: list[dict] = resp.get("result", {}).get("records", [])
        log.info(
            "phoenix_ckan_page",
            offset=offset,
            rows=len(records),
        )
        if not records:
            break
        yield records
        if len(records) < limit:
            break
        offset += limit


def _normalise_phoenix_row(row: dict) -> dict:
    """Map a raw Phoenix API row to the common permit schema."""

    def _pick(*keys: str) -> str | None:
        for k in keys:
            v = row.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return None

    def _to_float(*keys: str) -> float | None:
        for k in keys:
            v = row.get(k)
            if v is not None:
                try:
                    return float(str(v).replace(",", "").strip())
                except (ValueError, TypeError):
                    pass
        return None

    raw_type = _pick("permit_type", "TYPE_OF_WORK") or ""
    lat = _to_float("latitude")
    lon = _to_float("longitude")
    # Treat zero coordinates as missing
    if lat == 0.0:
        lat = None
    if lon == 0.0:
        lon = None

    return {
        "permit_number":       _pick("permit_number", "PERMIT_NUM"),
        "issue_date":          _pick("issue_date"),
        "permit_type":         _normalise_permit_type(raw_type),
        "work_description":    _pick("description", "DESCRIPTION"),
        "address":             _pick("address", "ADDRESS"),
        "zip_code":            _pick("zip_code", "ZIP"),
        "estimated_value_usd": _to_float("estimated_value", "VALUATION"),
        "sq_ft":               _to_float("sq_ft", "SQ_FT"),
        "units":               _to_float("number_of_units", "UNITS"),
        "latitude":            lat,
        "longitude":           lon,
    }


def ingest_phoenix(since: str = "2015-01-01") -> pd.DataFrame:
    """Download Phoenix building permits and return a normalised DataFrame.

    Tries the Socrata endpoint first; falls back to CKAN on failure.

    Args:
        since: ISO date string "YYYY-MM-DD". Only permits on or after this
               date are returned.

    Returns:
        DataFrame with columns matching the common permit schema.
    """
    raw_rows: list[dict] = []

    # --- Socrata (primary) ---
    try:
        for page in _phoenix_socrata_pages(since):
            raw_rows.extend(page)
        log.info("phoenix_socrata_done", total_raw=len(raw_rows))
    except Exception as exc:
        log.warning(
            "phoenix_socrata_failed_using_ckan",
            error=str(exc),
            rows_so_far=len(raw_rows),
        )
        raw_rows = []
        for page in _phoenix_ckan_pages(since):
            raw_rows.extend(page)
        log.info("phoenix_ckan_done", total_raw=len(raw_rows))

    if not raw_rows:
        log.warning("phoenix_no_rows", since=since)
        return pd.DataFrame(columns=_PERMIT_COLS)

    records = [_normalise_phoenix_row(r) for r in raw_rows]
    df = pd.DataFrame(records)
    df = _coerce_types(df)
    return df


# ---------------------------------------------------------------------------
# Austin ingestion
# ---------------------------------------------------------------------------

def _austin_pages(since: str) -> Iterator[list[dict]]:
    """Yield pages of Austin permit records from the Socrata endpoint."""
    offset = 0
    while True:
        params = {
            "$limit":  _PAGE_LIMIT,
            "$offset": offset,
            "$where":  f"issue_date >= '{since}'",
            "$order":  "issue_date ASC",
        }
        page: list[dict] = _get_json(AUSTIN_SOCRATA_URL, params)  # type: ignore[assignment]
        log.info("austin_socrata_page", offset=offset, rows=len(page))
        if not page:
            break
        yield page
        if len(page) < _PAGE_LIMIT:
            break
        offset += _PAGE_LIMIT


def _normalise_austin_row(row: dict) -> dict:
    """Map a raw Austin API row to the common permit schema."""

    def _pick(*keys: str) -> str | None:
        for k in keys:
            v = row.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return None

    def _to_float(*keys: str) -> float | None:
        for k in keys:
            v = row.get(k)
            if v is not None:
                try:
                    return float(str(v).replace(",", "").strip())
                except (ValueError, TypeError):
                    pass
        return None

    raw_type = _pick("workclassmapped", "permit_type_desc") or ""

    # Coordinates: top-level fields or nested location object
    lat: float | None = _to_float("latitude")
    lon: float | None = _to_float("longitude")
    if lat is None or lon is None:
        loc = row.get("location")
        if isinstance(loc, dict):
            coords = loc.get("coordinates")
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                try:
                    lon = float(coords[0])
                    lat = float(coords[1])
                except (ValueError, TypeError):
                    lat = lon = None
            else:
                lat_s = loc.get("latitude") or loc.get("lat")
                lon_s = loc.get("longitude") or loc.get("lon")
                if lat_s is not None:
                    try:
                        lat = float(lat_s)
                    except (ValueError, TypeError):
                        lat = None
                if lon_s is not None:
                    try:
                        lon = float(lon_s)
                    except (ValueError, TypeError):
                        lon = None

    if lat == 0.0:
        lat = None
    if lon == 0.0:
        lon = None

    # Austin uses "issued_date" for the date field in some API versions
    issue_date = _pick("issue_date") or _pick("issued_date")

    return {
        "permit_number":       _pick("permit_number", "permitnum"),
        "issue_date":          issue_date,
        "permit_type":         _normalise_permit_type(raw_type),
        "work_description":    _pick("description"),
        "address":             _pick("original_address1"),
        "zip_code":            _pick("original_zip"),
        "estimated_value_usd": _to_float("total_valuation", "valuation_amount"),
        "sq_ft":               _to_float("total_sq_ft", "square_feet"),
        "units":               _to_float("units", "unit_count"),
        "latitude":            lat,
        "longitude":           lon,
    }


def ingest_austin(since: str = "2015-01-01") -> pd.DataFrame:
    """Download Austin building permits and return a normalised DataFrame.

    Args:
        since: ISO date string "YYYY-MM-DD". Only permits on or after this
               date are returned.

    Returns:
        DataFrame with columns matching the common permit schema.
    """
    raw_rows: list[dict] = []
    for page in _austin_pages(since):
        raw_rows.extend(page)
    log.info("austin_socrata_done", total_raw=len(raw_rows))

    if not raw_rows:
        log.warning("austin_no_rows", since=since)
        return pd.DataFrame(columns=_PERMIT_COLS)

    records = [_normalise_austin_row(r) for r in raw_rows]
    df = pd.DataFrame(records)
    df = _coerce_types(df)
    return df


# ---------------------------------------------------------------------------
# Common normalisation helpers
# ---------------------------------------------------------------------------

_PERMIT_COLS = [
    "permit_number",
    "issue_date",
    "permit_type",
    "work_description",
    "address",
    "zip_code",
    "estimated_value_usd",
    "sq_ft",
    "units",
    "latitude",
    "longitude",
]


def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    """Apply dtype coercions to a raw-normalised permit DataFrame."""
    df = df.copy()

    for col in _PERMIT_COLS:
        if col not in df.columns:
            df[col] = None

    # Date parsing
    df["issue_date"] = pd.to_datetime(df["issue_date"], errors="coerce").dt.date

    # Numerics
    for col in ("estimated_value_usd", "sq_ft", "units"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Coordinates
    df["latitude"]  = pd.to_numeric(df["latitude"],  errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")

    # Drop rows with no permit number (can't be keyed)
    df = df[df["permit_number"].notna() & (df["permit_number"] != "")].copy()
    df = df.reset_index(drop=True)

    return df[_PERMIT_COLS]


# ---------------------------------------------------------------------------
# Geocoding missing coordinates
# ---------------------------------------------------------------------------

def _fill_coordinates(df: pd.DataFrame, city_label: str) -> tuple[pd.DataFrame, int]:
    """Geocode rows with missing lat/lon using the Census batch API.

    Returns the updated DataFrame and the number of rows that were geocoded.
    """
    missing_mask = (
        df["latitude"].isna() | df["longitude"].isna()
    ) & df["address"].notna() & (df["address"] != "")

    missing_idx = df.index[missing_mask].tolist()
    if not missing_idx:
        return df, 0

    log.info("geocoding_missing", city=city_label, count=len(missing_idx))

    addresses = df.loc[missing_idx, "address"].tolist()
    geocoded = geocode_batch(addresses)

    filled = 0
    for idx, addr in zip(missing_idx, addresses):
        if addr in geocoded:
            lat, lon = geocoded[addr]
            df.at[idx, "latitude"]  = lat
            df.at[idx, "longitude"] = lon
            filled += 1

    log.info("geocoding_done", city=city_label, filled=filled, attempted=len(missing_idx))
    return df, filled


# ---------------------------------------------------------------------------
# Parquet cache helpers
# ---------------------------------------------------------------------------

def _cache_dir(city: str) -> Path:
    pipeline = get_pipeline()
    subdir = pipeline["raw_data_subdirs"]["city_permits"][city]
    return data_path(subdir)


def _month_cache_path(city: str, year: int, month: int) -> Path:
    return _cache_dir(city) / f"permits_{year:04d}_{month:02d}.parquet"


def _months_in_range(since: str) -> list[tuple[int, int]]:
    """Return list of (year, month) tuples from `since` to today (inclusive)."""
    import datetime as _dt

    sy, sm = int(since[:4]), int(since[5:7])
    today = _dt.date.today()
    ey, em = today.year, today.month

    result: list[tuple[int, int]] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        result.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return result


def _load_cached_months(city: str, months: list[tuple[int, int]]) -> tuple[pd.DataFrame, list[tuple[int, int]]]:
    """Load any months already cached from disk.

    Returns (combined_cached_df, uncached_months).
    """
    cached_frames: list[pd.DataFrame] = []
    uncached: list[tuple[int, int]] = []

    for year, month in months:
        path = _month_cache_path(city, year, month)
        if path.exists():
            log.info("city_permits_cache_hit", city=city, year=year, month=month)
            cached_frames.append(pd.read_parquet(path))
        else:
            uncached.append((year, month))

    combined = pd.concat(cached_frames, ignore_index=True) if cached_frames else pd.DataFrame(columns=_PERMIT_COLS)
    return combined, uncached


def _save_monthly_cache(df: pd.DataFrame, city: str) -> int:
    """Partition a full DataFrame by month and save each shard to parquet.

    Returns the number of months written.
    """
    if df.empty or "issue_date" not in df.columns:
        return 0

    df = df.copy()
    df["_year"]  = pd.to_datetime(df["issue_date"], errors="coerce").dt.year
    df["_month"] = pd.to_datetime(df["issue_date"], errors="coerce").dt.month

    written = 0
    for (year, month), shard in df.groupby(["_year", "_month"]):
        if pd.isna(year) or pd.isna(month):
            continue
        path = _month_cache_path(city, int(year), int(month))
        if not path.exists():
            shard = shard.drop(columns=["_year", "_month"])
            shard.to_parquet(path, index=False)
            log.info("city_permits_cached", city=city, year=int(year), month=int(month), rows=len(shard))
            written += 1

    return written


# ---------------------------------------------------------------------------
# DB upsert via GeoDataFrame
# ---------------------------------------------------------------------------

def _upsert_permits(df: pd.DataFrame, city: str) -> None:
    """Upsert permits to the city_permits table (PostGIS-optional)."""
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    city_id = CITY_IDS[city]

    db_df = pd.DataFrame({
        "permit_id":  df["permit_number"],
        "city_id":    city_id,
        "issue_date": df["issue_date"],
        "type":       df["permit_type"],
        "valuation":  df["estimated_value_usd"],
        "lat":        df["latitude"],
        "lon":        df["longitude"],
    })
    db_df = db_df[db_df["permit_id"].notna() & (db_df["permit_id"] != "")].copy()

    pipeline = get_pipeline()
    user = os.environ.get("POSTGRES_USER", "urbangrowth")
    pwd  = os.environ.get("POSTGRES_PASSWORD", "")
    host = pipeline["db"]["host"]
    port = pipeline["db"]["port"]
    dbname = pipeline["db"]["dbname"]
    engine = sa.create_engine(
        f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{dbname}",
        pool_pre_ping=True,
    )

    # Ensure table exists and has lat/lon columns (PostGIS-optional)
    with engine.begin() as conn:
        conn.execute(sa.text("""
            CREATE TABLE IF NOT EXISTS city_permits (
                permit_id   TEXT    NOT NULL,
                city_id     INTEGER NOT NULL,
                issue_date  DATE,
                type        TEXT,
                valuation   FLOAT,
                lat         FLOAT,
                lon         FLOAT,
                PRIMARY KEY (permit_id, city_id)
            )
        """))
        conn.execute(sa.text("ALTER TABLE city_permits ADD COLUMN IF NOT EXISTS lat FLOAT"))
        conn.execute(sa.text("ALTER TABLE city_permits ADD COLUMN IF NOT EXISTS lon FLOAT"))

    upsert_sql = sa.text("""
        INSERT INTO city_permits (permit_id, city_id, issue_date, type, valuation, lat, lon)
        VALUES (:permit_id, :city_id, :issue_date, :type, :valuation, :lat, :lon)
        ON CONFLICT (permit_id, city_id) DO UPDATE SET
            issue_date = EXCLUDED.issue_date,
            type       = EXCLUDED.type,
            valuation  = EXCLUDED.valuation,
            lat        = EXCLUDED.lat,
            lon        = EXCLUDED.lon
    """)

    def _clean(v):
        if isinstance(v, float) and (v != v):  # NaN check
            return None
        return v

    rows = [
        {k: _clean(v) for k, v in rec.items()}
        for rec in db_df.to_dict("records")
    ]

    CHUNK = 500
    with engine.begin() as conn:
        for i in range(0, len(rows), CHUNK):
            conn.execute(upsert_sql, rows[i:i + CHUNK])

    log.info("city_permits_upserted", city=city, rows=len(db_df))


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def run(city: str = "phoenix", since: str = "2015-01-01") -> None:
    """Download, normalise, geocode, cache, and upsert city permits.

    Args:
        city:  "phoenix" or "austin".
        since: ISO date string "YYYY-MM-DD". Earliest permit issue date to fetch.

    The function is idempotent: months already cached on disk are loaded from
    the parquet cache rather than re-fetched from the remote API.
    """
    if city not in CITY_IDS:
        raise ValueError(f"Unsupported city '{city}'. Must be one of: {list(CITY_IDS)}")

    log.info("city_permits_run_start", city=city, since=since)

    # --- Determine which months need fetching ---
    all_months = _months_in_range(since)
    cached_df, uncached_months = _load_cached_months(city, all_months)
    cached_months_count = len(all_months) - len(uncached_months)
    log.info(
        "city_permits_cache_summary",
        city=city,
        cached_months=cached_months_count,
        months_to_fetch=len(uncached_months),
    )

    # --- Fetch uncached data ---
    fresh_df: pd.DataFrame
    if uncached_months:
        # Compute the earliest un-cached month as the effective since date
        min_year, min_month = min(uncached_months)
        fetch_since = f"{min_year:04d}-{min_month:02d}-01"

        if city == "phoenix":
            fresh_df = ingest_phoenix(since=fetch_since)
        else:
            fresh_df = ingest_austin(since=fetch_since)

        # Save new months to parquet cache
        written = _save_monthly_cache(fresh_df, city)
        log.info("city_permits_new_months_cached", city=city, months_written=written)
    else:
        fresh_df = pd.DataFrame(columns=_PERMIT_COLS)
        log.info("city_permits_all_months_cached", city=city)

    # --- Combine cached + fresh ---
    all_frames = [f for f in (cached_df, fresh_df) if not f.empty]
    if not all_frames:
        log.warning("city_permits_no_data", city=city, since=since)
        return

    df = pd.concat(all_frames, ignore_index=True)

    # Deduplicate by permit_number (keep last occurrence)
    df = df.drop_duplicates(subset=["permit_number"], keep="last").reset_index(drop=True)

    log.info("city_permits_combined", city=city, total_permits=len(df))

    # --- Geocode missing coordinates ---
    df, geocoded_count = _fill_coordinates(df, city)

    # --- Upsert to DB ---
    _upsert_permits(df, city)

    log.info(
        "city_permits_run_complete",
        city=city,
        total_permits=len(df),
        geocoded_count=geocoded_count,
        cached_months=cached_months_count,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Ingest city building permits into the urbangrowth database."
    )
    parser.add_argument(
        "--city",
        default="phoenix",
        choices=list(CITY_IDS),
        help="City to ingest (default: phoenix)",
    )
    parser.add_argument(
        "--since",
        default="2015-01-01",
        help="Earliest issue date to include, YYYY-MM-DD (default: 2015-01-01)",
    )
    args = parser.parse_args()
    run(city=args.city, since=args.since)
