"""Census Building Permits Survey (BPS) ingestion module.

Fetches monthly permit data from the Census BPS API at county and national
geographic levels, rolls county data up to MSA (CBSA) level, and upserts
everything into the census_bps table.

Usage:
    python -m urbangrowth.data.census_bps --start 2010-01 --end 2025-12
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pandas as pd
import requests
import structlog
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

from urbangrowth.config import data_path
from urbangrowth.db import loaders

load_dotenv()

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BPS_API_BASE = "https://api.census.gov/data/timeseries/bps"
BPS_VARIABLES = "BLDGS1,UNITS1,BLDGS24,UNITS24,BLDGS5,UNITS5,BLDGS,UNITS,BLDGVAL"

OMB_DELINEATION_URL = (
    "https://www2.census.gov/programs-surveys/metro-micro/geographies/"
    "reference-files/2023/delineation-files/list1_2023.xls"
)

# ---------------------------------------------------------------------------
# OMB CBSA delineation helpers
# ---------------------------------------------------------------------------


def download_omb_delineation(dest_dir: Path) -> Path:
    """Download OMB CBSA delineation file (county FIPS -> CBSA code mapping).

    Uses the March 2023 delineation file. Skips download if the file already
    exists locally.

    Returns the path to the downloaded .xls file.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "list1_2023.xls"

    if dest.exists():
        log.info("omb_delineation_cached", path=str(dest))
        return dest

    log.info("downloading_omb_delineation", url=OMB_DELINEATION_URL)
    resp = requests.get(OMB_DELINEATION_URL, timeout=60)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    log.info("omb_delineation_saved", path=str(dest), bytes=len(resp.content))
    return dest


def load_cbsa_map(path: Path) -> pd.DataFrame:
    """Parse OMB delineation XLS into a county-to-CBSA mapping DataFrame.

    The Census XLS has a multi-row header; actual data begins on row 3
    (0-indexed row 2). Columns are identified by matching known header text.

    Returns a DataFrame with columns:
        cbsa_code, cbsa_title, fips_state_code, fips_county_code,
        county_name, fips5  (zero-padded 5-char combined FIPS)
    """
    # The 2023 file has 3 header rows; skip them
    raw = pd.read_excel(path, header=2, dtype=str)

    # Normalise column names by stripping whitespace
    raw.columns = [str(c).strip() for c in raw.columns]

    def _find_col(df: pd.DataFrame, *candidates: str) -> str:
        for candidate in candidates:
            matches = [c for c in df.columns if candidate.lower() in c.lower()]
            if matches:
                return matches[0]
        raise KeyError(f"Could not find column matching any of: {candidates}")

    cbsa_code_col   = _find_col(raw, "CBSA Code")
    cbsa_title_col  = _find_col(raw, "CBSA Title")
    state_fips_col  = _find_col(raw, "FIPS State Code")
    county_fips_col = _find_col(raw, "FIPS County Code")
    county_name_col = _find_col(raw, "County/County Equivalent")

    df = raw[[
        cbsa_code_col, cbsa_title_col, state_fips_col,
        county_fips_col, county_name_col,
    ]].copy()
    df.columns = [
        "cbsa_code", "cbsa_title", "fips_state_code",
        "fips_county_code", "county_name",
    ]

    # Drop rows that are section headers (no numeric CBSA code)
    df = df[df["cbsa_code"].str.match(r"^\d+$", na=False)].copy()

    # Zero-pad FIPS codes
    df["fips_state_code"]  = df["fips_state_code"].str.strip().str.zfill(2)
    df["fips_county_code"] = df["fips_county_code"].str.strip().str.zfill(3)
    df["fips5"] = df["fips_state_code"] + df["fips_county_code"]

    df = df.reset_index(drop=True)
    log.info("cbsa_map_loaded", rows=len(df))
    return df


# ---------------------------------------------------------------------------
# Census BPS API helpers
# ---------------------------------------------------------------------------


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _get_bps(params: dict) -> list:
    """Execute a single Census API GET request with retry on transient errors."""
    resp = requests.get(BPS_API_BASE, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def fetch_bps_api(year: int, month: int, geo_level: str, api_key: str) -> pd.DataFrame:
    """Fetch BPS data from the Census API for one month and geographic level.

    geo_level: 'national' | 'county'

    For national: for=us:1
    For county:   for=county:*&in=state:*  (all counties in all states)

    Returns a raw DataFrame whose columns come directly from the API JSON header.
    Applies a 0.5-second sleep after each call to respect rate limits.
    """
    time_str = f"{year:04d}-{month:02d}"

    params: dict[str, str] = {
        "get": BPS_VARIABLES,
        "time": time_str,
        "key": api_key,
    }

    if geo_level == "national":
        params["for"] = "us:1"
    elif geo_level == "county":
        params["for"] = "county:*"
        params["in"] = "state:*"
    else:
        raise ValueError(f"Unknown geo_level: {geo_level!r}")

    log.debug("bps_api_request", year=year, month=month, geo_level=geo_level)
    data = _get_bps(params)

    # Census API returns a list-of-lists; first row is the header
    headers = data[0]
    rows    = data[1:]
    df = pd.DataFrame(rows, columns=headers)

    time.sleep(0.5)  # polite rate limiting
    return df


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def parse_bps_response(
    df: pd.DataFrame, year: int, month: int, geo_level: str
) -> pd.DataFrame:
    """Normalise a raw Census API DataFrame to the census_bps table schema.

    Schema columns produced:
        period              DATE (first day of month)
        jurisdiction_id     TEXT  — 5-char county FIPS or 'US'
        jurisdiction_name   TEXT
        state               CHAR(2)
        msa_code            TEXT  (NULL at this stage; filled by rollup)
        residential_permits INTEGER  (UNITS1 + UNITS24 + UNITS5)
        commercial_permits  INTEGER  (0 — BPS only tracks residential)
        total_valuation     NUMERIC  (BLDGVAL * 1000 to convert $thousands -> $)
    """
    out = df.copy()

    period = pd.Timestamp(year=year, month=month, day=1).date()

    # Coerce numeric columns; missing values become 0
    for col in ["UNITS1", "UNITS24", "UNITS5", "BLDGVAL"]:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)

    out["residential_permits"] = (
        out["UNITS1"] + out["UNITS24"] + out["UNITS5"]
    ).astype(int)
    out["commercial_permits"] = 0
    out["total_valuation"] = out["BLDGVAL"] * 1000

    out["period"]   = period
    out["msa_code"] = None

    if geo_level == "national":
        out["jurisdiction_id"]   = "US"
        out["jurisdiction_name"] = "United States"
        out["state"]             = None
    elif geo_level == "county":
        # API returns columns 'state' (2-digit FIPS) and 'county' (3-digit FIPS)
        out["fips_state"]  = out["state"].str.zfill(2)
        out["fips_county"] = out["county"].str.zfill(3)
        out["jurisdiction_id"] = out["fips_state"] + out["fips_county"]

        # Use NAME column if the API returns it; otherwise leave None
        name_candidates = [c for c in out.columns if c.upper() == "NAME"]
        out["jurisdiction_name"] = (
            out[name_candidates[0]] if name_candidates else None
        )

        # Keep state as 2-digit FIPS string
        out["state"] = out["fips_state"]
    else:
        raise ValueError(f"Unknown geo_level: {geo_level!r}")

    keep = [
        "period", "jurisdiction_id", "jurisdiction_name", "state",
        "msa_code", "residential_permits", "commercial_permits", "total_valuation",
    ]
    return out[keep].reset_index(drop=True)


# ---------------------------------------------------------------------------
# MSA rollup
# ---------------------------------------------------------------------------


def rollup_to_msa(county_df: pd.DataFrame, cbsa_map: pd.DataFrame) -> pd.DataFrame:
    """Merge county-level permit data with the CBSA map and aggregate to MSA.

    county_df must have jurisdiction_id equal to the 5-char county FIPS.
    cbsa_map must have columns: fips5, cbsa_code, cbsa_title.

    Returns a DataFrame in census_bps schema with jurisdiction_id = cbsa_code.
    Counties that do not belong to any CBSA are silently dropped (rural areas).
    """
    merged = county_df.merge(
        cbsa_map[["fips5", "cbsa_code", "cbsa_title"]],
        left_on="jurisdiction_id",
        right_on="fips5",
        how="inner",
    )

    if merged.empty:
        log.warning(
            "rollup_to_msa_no_matches",
            county_rows=len(county_df),
            cbsa_rows=len(cbsa_map),
        )
        return pd.DataFrame(columns=[
            "period", "jurisdiction_id", "jurisdiction_name",
            "state", "msa_code", "residential_permits",
            "commercial_permits", "total_valuation",
        ])

    agg = (
        merged
        .groupby(["period", "cbsa_code", "cbsa_title"], as_index=False)
        .agg(
            residential_permits=("residential_permits", "sum"),
            commercial_permits=("commercial_permits", "sum"),
            total_valuation=("total_valuation", "sum"),
        )
    )

    agg["jurisdiction_id"]   = agg["cbsa_code"]
    agg["jurisdiction_name"] = agg["cbsa_title"]
    agg["state"]             = None
    agg["msa_code"]          = agg["cbsa_code"]

    keep = [
        "period", "jurisdiction_id", "jurisdiction_name", "state",
        "msa_code", "residential_permits", "commercial_permits", "total_valuation",
    ]
    return agg[keep].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Month iterator
# ---------------------------------------------------------------------------


def months_in_range(start: str, end: str) -> list[tuple[int, int]]:
    """Return a list of (year, month) tuples covering YYYY-MM range inclusive.

    Example:
        months_in_range("2024-11", "2025-02")
        -> [(2024, 11), (2024, 12), (2025, 1), (2025, 2)]
    """
    sy, sm = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]),   int(end[5:7])

    result: list[tuple[int, int]] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        result.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run(start: str = "2010-01", end: str = "2025-12") -> None:
    """Iterate over months, fetch BPS data, roll up to MSA, and upsert to DB.

    Idempotent: if a parquet cache file exists for a given month it is loaded
    from disk rather than re-fetched from the API.

    Cache location: <data_root>/raw/census_bps/county_YYYY-MM.parquet
    """
    api_key = os.environ.get("CENSUS_API_KEY", "")
    if not api_key:
        raise RuntimeError("CENSUS_API_KEY is not set in environment / .env file")

    # Prepare cache directory (data_path creates dirs automatically)
    cache_dir: Path = data_path("raw/census_bps")

    # Download and load CBSA delineation map once
    ref_dir  = data_path("raw/reference")
    xls_path = download_omb_delineation(ref_dir)
    cbsa_map = load_cbsa_map(xls_path)

    month_list = months_in_range(start, end)
    log.info("bps_run_start", start=start, end=end, months=len(month_list))

    for year, month in month_list:
        label      = f"{year:04d}-{month:02d}"
        cache_file = cache_dir / f"county_{label}.parquet"

        # ── County-level data ────────────────────────────────────────────
        if cache_file.exists():
            log.info("bps_cache_hit", month=label)
            county_raw = pd.read_parquet(cache_file)
        else:
            log.info("bps_fetching", month=label, geo="county")
            try:
                county_raw = fetch_bps_api(year, month, "county", api_key)
            except Exception as exc:
                log.warning(
                    "bps_fetch_failed", month=label, geo="county", error=str(exc)
                )
                continue
            county_raw.to_parquet(cache_file, index=False)

        county_df = parse_bps_response(county_raw, year, month, "county")

        # ── National-level data (lightweight; no separate cache) ─────────
        try:
            national_raw = fetch_bps_api(year, month, "national", api_key)
            national_df  = parse_bps_response(national_raw, year, month, "national")
        except Exception as exc:
            log.warning(
                "bps_fetch_failed", month=label, geo="national", error=str(exc)
            )
            national_df = pd.DataFrame()

        # ── MSA rollup ───────────────────────────────────────────────────
        msa_df = rollup_to_msa(county_df, cbsa_map)

        # ── Combine and upsert ───────────────────────────────────────────
        combined = pd.concat(
            [r for r in (county_df, national_df, msa_df) if not r.empty],
            ignore_index=True,
        )

        if combined.empty:
            log.warning("bps_no_data", month=label)
            continue

        # Ensure correct dtypes before upsert
        combined["residential_permits"] = combined["residential_permits"].astype("Int64")
        combined["commercial_permits"]  = combined["commercial_permits"].astype("Int64")
        combined["total_valuation"] = pd.to_numeric(
            combined["total_valuation"], errors="coerce"
        )

        rows = loaders.upsert_df(combined, "census_bps", ["period", "jurisdiction_id"])
        log.info(
            "bps_month_done",
            month=label,
            rows_upserted=rows,
            county=len(county_df),
            msa=len(msa_df),
            national=len(national_df) if not national_df.empty else 0,
        )

    log.info("bps_run_complete", start=start, end=end)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Ingest Census BPS data into the urbangrowth database."
    )
    parser.add_argument(
        "--start",
        default="2010-01",
        help="Start month YYYY-MM (default: 2010-01)",
    )
    parser.add_argument(
        "--end",
        default="2025-12",
        help="End month YYYY-MM (default: 2025-12)",
    )
    args = parser.parse_args()
    run(start=args.start, end=args.end)
