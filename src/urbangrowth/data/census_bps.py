"""Census Building Permits Survey (BPS) ingestion module.

Fetches monthly permit data from the Census BPS API at county and national
geographic levels, rolls county data up to MSA (CBSA) level, and upserts
everything into the census_bps table.

Usage:
    python -m urbangrowth.data.census_bps --start 2010-01 --end 2025-12
"""
from __future__ import annotations

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

BPS_CSV_BASE = "https://www2.census.gov/econ/bps"

OMB_DELINEATION_URL = (
    "https://www2.census.gov/programs-surveys/metro-micro/geographies/"
    "reference-files/2023/delineation-files/list1_2023.xlsx"
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
    dest = dest_dir / "list1_2023.xlsx"

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
# Census BPS CSV download helpers
# ---------------------------------------------------------------------------
# Census BPS publishes monthly CSV files at:
#   County: https://www2.census.gov/econ/bps/County/co{YY}{MM}y.txt
#   State:  https://www2.census.gov/econ/bps/State/st{YY}{MM}y.txt
# YY = 2-digit year, MM = zero-padded month.
# File layout: 2-row header, 1 blank row, then data.
# Values are in actual dollars (not thousands).


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    reraise=True,
)
def _download_bps_csv(url: str) -> str:
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.text


def _parse_bps_csv(text: str) -> pd.DataFrame:
    """Parse a Census BPS county or state CSV text into a clean DataFrame.

    Columns in the fixed layout (0-indexed):
        0  Survey Date (YYYYMM)
        1  FIPS State
        2  FIPS County  (absent in state files)
        3  Region Code
        4  Division Code
        5  Name
        6  1-unit Bldgs  7  1-unit Units  8  1-unit Value
        9  2-units Bldgs 10 2-units Units 11 2-units Value
        12 3-4u Bldgs   13 3-4u Units    14 3-4u Value
        15 5+u Bldgs    16 5+u Units     17 5+u Value
        (remaining columns are reportable-permit breakdowns and can be ignored)
    """
    import io
    lines = text.splitlines()
    # Skip 2 header rows and 1 blank row
    data_lines = [l for l in lines[3:] if l.strip()]
    if not data_lines:
        return pd.DataFrame()
    df = pd.read_csv(
        io.StringIO("\n".join(data_lines)),
        header=None,
        dtype=str,
        on_bad_lines="skip",
    )
    return df


def fetch_bps_csv(year: int, month: int, geo_level: str) -> pd.DataFrame:
    """Download Census BPS data for one month from direct CSV files.

    geo_level: 'county' | 'state'

    Returns raw parsed DataFrame.
    """
    yy = f"{year % 100:02d}"
    mm = f"{month:02d}"
    if geo_level == "county":
        url = f"{BPS_CSV_BASE}/County/co{yy}{mm}y.txt"
    elif geo_level == "state":
        url = f"{BPS_CSV_BASE}/State/st{yy}{mm}y.txt"
    else:
        raise ValueError(f"Unknown geo_level: {geo_level!r}")

    log.debug("bps_csv_request", year=year, month=month, geo_level=geo_level, url=url)
    text = _download_bps_csv(url)
    df = _parse_bps_csv(text)
    time.sleep(0.3)
    return df


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def parse_bps_response(
    df: pd.DataFrame, year: int, month: int, geo_level: str
) -> pd.DataFrame:
    """Normalise a raw Census BPS CSV DataFrame to the census_bps table schema.

    CSV column layout (0-indexed):
        County files: [0]=date [1]=fips_state [2]=fips_county [3]=region [4]=div
                      [5]=name [6-8]=1unit [9-11]=2unit [12-14]=3-4unit [15-17]=5plus
        State files:  [0]=date [1]=fips_state [2]=region [3]=div
                      [4]=name [5-7]=1unit [8-10]=2unit [11-13]=3-4unit [14-16]=5plus

    Schema columns produced:
        period              DATE (first day of month)
        jurisdiction_id     TEXT  — 5-char county FIPS, 2-char state FIPS, or 'US'
        jurisdiction_name   TEXT
        state               CHAR(2)
        msa_code            TEXT  (NULL at this stage; filled by rollup)
        residential_permits INTEGER
        commercial_permits  INTEGER  (0 — BPS tracks residential only)
        total_valuation     NUMERIC  (dollars)
    """
    if df.empty:
        return pd.DataFrame(columns=[
            "period", "jurisdiction_id", "jurisdiction_name", "state",
            "msa_code", "residential_permits", "commercial_permits", "total_valuation",
        ])

    out = df.copy()
    period = pd.Timestamp(year=year, month=month, day=1).date()

    def _num(col_idx: int) -> "pd.Series":
        return pd.to_numeric(out.iloc[:, col_idx], errors="coerce").fillna(0)

    if geo_level == "county":
        # cols: 0=date 1=state 2=county 3=region 4=div 5=name
        #       6=1u_bldg 7=1u_units 8=1u_val
        #       9=2u_bldg 10=2u_units 11=2u_val
        #       12=34u_bldg 13=34u_units 14=34u_val
        #       15=5p_bldg 16=5p_units 17=5p_val
        fips_state  = out.iloc[:, 1].str.strip().str.zfill(2)
        fips_county = out.iloc[:, 2].str.strip().str.zfill(3)
        name        = out.iloc[:, 5].str.strip()
        units       = _num(7) + _num(10) + _num(13) + _num(16)
        valuation   = _num(8) + _num(11) + _num(14) + _num(17)
        juris_id    = fips_state + fips_county
        state_col   = fips_state

    elif geo_level == "state":
        # cols: 0=date 1=state 2=region 3=div 4=name
        #       5=1u_bldg 6=1u_units 7=1u_val
        #       8=2u_bldg 9=2u_units 10=2u_val
        #       11=34u_bldg 12=34u_units 13=34u_val
        #       14=5p_bldg 15=5p_units 16=5p_val
        #
        # State CSV also contains Division (D1-D9), Region (R1-R4), and US
        # summary rows — keep only rows with 2-digit numeric state FIPS
        fips_raw = out.iloc[:, 1].str.strip()
        numeric_state = fips_raw.str.match(r'^\d{1,2}$')
        out = out[numeric_state].copy()
        if out.empty:
            return pd.DataFrame(columns=[
                "period", "jurisdiction_id", "jurisdiction_name", "state",
                "msa_code", "residential_permits", "commercial_permits", "total_valuation",
            ])
        fips_state  = out.iloc[:, 1].str.strip().str.zfill(2)
        name        = out.iloc[:, 4].str.strip()

        def _num(col_idx: int) -> "pd.Series":  # re-bind after filtering
            return pd.to_numeric(out.iloc[:, col_idx], errors="coerce").fillna(0)

        units       = _num(6) + _num(9) + _num(12) + _num(15)
        valuation   = _num(7) + _num(10) + _num(13) + _num(16)
        juris_id    = fips_state
        state_col   = fips_state

    else:
        raise ValueError(f"Unknown geo_level: {geo_level!r}")

    result = pd.DataFrame({
        "period":               period,
        "jurisdiction_id":      juris_id,
        "jurisdiction_name":    name,
        "state":                state_col,
        "msa_code":             None,
        "residential_permits":  units.astype(int),
        "commercial_permits":   0,
        "total_valuation":      valuation,
    })
    return result.reset_index(drop=True)


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
    """Iterate over months, fetch BPS CSV data, roll up to MSA, upsert to DB.

    Uses direct CSV downloads from www2.census.gov/econ/bps/ (county + state).
    Idempotent: county parquet cache skips re-download.

    Cache location: <data_root>/raw/census_bps/county_YYYY-MM.parquet
    """
    cache_dir: Path = data_path("raw/census_bps")
    ref_dir         = data_path("raw/reference")
    xls_path        = download_omb_delineation(ref_dir)
    cbsa_map        = load_cbsa_map(xls_path)

    month_list = months_in_range(start, end)
    log.info("bps_run_start", start=start, end=end, months=len(month_list))

    for year, month in month_list:
        label      = f"{year:04d}-{month:02d}"
        cache_file = cache_dir / f"county_{label}.parquet"

        # ── County-level ─────────────────────────────────────────────────
        if cache_file.exists():
            log.info("bps_cache_hit", month=label)
            county_raw = pd.read_parquet(cache_file)
        else:
            log.info("bps_fetching", month=label, geo="county")
            try:
                county_raw = fetch_bps_csv(year, month, "county")
            except Exception as exc:
                log.warning("bps_fetch_failed", month=label, geo="county", error=str(exc))
                continue
            county_raw.to_parquet(cache_file, index=False)

        county_df = parse_bps_response(county_raw, year, month, "county")

        # ── State-level (for national aggregation) ───────────────────────
        try:
            state_raw = fetch_bps_csv(year, month, "state")
            state_df  = parse_bps_response(state_raw, year, month, "state")
        except Exception as exc:
            log.warning("bps_fetch_failed", month=label, geo="state", error=str(exc))
            state_df = pd.DataFrame()

        # ── MSA rollup ───────────────────────────────────────────────────
        msa_df = rollup_to_msa(county_df, cbsa_map)

        # ── National aggregate (sum of state rows) ───────────────────────
        if not state_df.empty:
            national_row = pd.DataFrame([{
                "period":               pd.Timestamp(year=year, month=month, day=1).date(),
                "jurisdiction_id":      "national",
                "jurisdiction_name":    "United States",
                "state":                None,
                "msa_code":             None,
                "residential_permits":  int(state_df["residential_permits"].sum()),
                "commercial_permits":   0,
                "total_valuation":      float(state_df["total_valuation"].sum()),
            }])
        else:
            national_row = pd.DataFrame()

        # ── Combine and upsert ───────────────────────────────────────────
        combined = pd.concat(
            [r for r in (county_df, state_df, national_row, msa_df) if not r.empty],
            ignore_index=True,
        )

        if combined.empty:
            log.warning("bps_no_data", month=label)
            continue

        combined = combined.drop_duplicates(subset=["period", "jurisdiction_id"], keep="last")
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
            state=len(state_df) if not state_df.empty else 0,
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
