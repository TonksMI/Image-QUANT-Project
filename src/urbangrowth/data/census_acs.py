"""Census ACS (American Community Survey) demographics + TIGER boundaries.

Downloads ACS 5-year estimates at block-group level and TIGER/Line block-group
boundary shapefiles for Phoenix (Maricopa County) and Austin (Travis/Williamson/
Hays Counties).  Joins them to produce a spatial demographics layer and saves as
parquet + GeoPackage.  Signal computation (Phase 5) will aggregate these to H3
hexagons and then to ticker-level features.

Requires CENSUS_API_KEY env var (warn-only if absent).
"""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
import structlog
from dotenv import load_dotenv
from shapely.geometry import box
from tenacity import retry, stop_after_attempt, wait_exponential

from urbangrowth.config import data_path, get_cities

load_dotenv()

log = structlog.get_logger(__name__)

CENSUS_API_KEY: str = os.environ.get("CENSUS_API_KEY", "")
if not CENSUS_API_KEY:
    log.warning(
        "census_api_key_missing",
        msg="CENSUS_API_KEY not set — ACS requests will fail; add it to .env",
    )

_ACS_BASE = "https://api.census.gov/data"
_TIGER_BASE = "https://www2.census.gov/geo/tiger/TIGER2023/BG"

# ACS variables of interest — key = Census variable code, value = friendly name
_ACS_VARS: dict[str, str] = {
    "B01003_001E": "total_population",
    "B19013_001E": "median_household_income",
    "B25077_001E": "median_home_value",
    "B25003_002E": "owner_occupied_units",
    "B25003_003E": "renter_occupied_units",
    "B08303_001E": "workers_commuting",
    "B23025_004E": "employed_in_labor_force",
    "B25002_001E": "total_housing_units",
    "B25002_003E": "vacant_housing_units",
}

# Sentinel value Census uses to indicate missing data
_ACS_MISSING_SENTINEL = -666666666


# ---------------------------------------------------------------------------
# ACS block-group fetch
# ---------------------------------------------------------------------------


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _acs_request(url: str, params: dict) -> list[list]:
    """GET a Census API endpoint and return parsed JSON (list of lists).

    Tenacity retries up to 3 times with exponential back-off on any exception.
    """
    log.debug("acs_api_request", url=url, params={k: v for k, v in params.items() if k != "key"})
    resp = requests.get(url, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def fetch_acs_block_groups(
    state_fips: str,
    county_fips_3digit: str,
    year: int = 2022,
) -> pd.DataFrame:
    """Return DataFrame with block-group ACS estimates for one county.

    Parameters
    ----------
    state_fips:
        2-digit state FIPS code, e.g. ``"04"`` for Arizona.
    county_fips_3digit:
        3-digit county FIPS code (last 3 digits of the 5-digit FIPS),
        e.g. ``"013"`` for Maricopa County.
    year:
        ACS 5-year dataset vintage year (default 2022).

    Returns
    -------
    DataFrame indexed by ``geoid`` with one column per ``_ACS_VARS`` entry.
    Missing values (Census sentinel ``-666666666``) are replaced with ``NaN``.
    """
    var_codes = list(_ACS_VARS.keys())
    get_param = "GEO_ID,NAME," + ",".join(var_codes)

    url = f"{_ACS_BASE}/{year}/acs/acs5"
    params = {
        "get": get_param,
        "for": "block group:*",
        "in": f"state:{state_fips} county:{county_fips_3digit}",
        "key": CENSUS_API_KEY,
    }

    raw = _acs_request(url, params)
    # raw[0] is the header row; raw[1:] are data rows
    header = raw[0]
    rows = raw[1:]

    df = pd.DataFrame(rows, columns=header)

    # Build canonical 12-character GEOID from the GEO_ID column
    # GEO_ID format: "1500000US{state}{county}{tract}{bg}"
    df["geoid"] = df["GEO_ID"].str.replace("1500000US", "", regex=False)

    # Rename Census variable codes to friendly names
    df = df.rename(columns=_ACS_VARS)

    # Keep only relevant columns
    keep_cols = ["geoid", "NAME", "state", "county", "tract", "block group"] + list(_ACS_VARS.values())
    keep_cols = [c for c in keep_cols if c in df.columns]
    df = df[keep_cols].copy()

    # Coerce ACS estimate columns to numeric; replace sentinel → NaN
    for friendly_name in _ACS_VARS.values():
        if friendly_name in df.columns:
            df[friendly_name] = pd.to_numeric(df[friendly_name], errors="coerce")
            df[friendly_name] = df[friendly_name].where(
                df[friendly_name] != _ACS_MISSING_SENTINEL, other=pd.NA
            )

    log.info(
        "acs_fetched",
        state=state_fips,
        county=county_fips_3digit,
        year=year,
        rows=len(df),
    )
    return df


# ---------------------------------------------------------------------------
# TIGER block-group boundaries
# ---------------------------------------------------------------------------


def _download_tiger_zip(state_fips: str, cache_dir: Path) -> Path:
    """Download and cache the TIGER BG zip for a state; return path to zip."""
    zip_name = f"tl_2023_{state_fips}_bg.zip"
    zip_path = cache_dir / zip_name
    if zip_path.exists():
        log.info("tiger_cache_hit", path=str(zip_path))
        return zip_path

    url = f"{_TIGER_BASE}/{zip_name}"
    log.info("tiger_download_start", url=url)
    resp = requests.get(url, stream=True, timeout=300)
    resp.raise_for_status()

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with open(zip_path, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            fh.write(chunk)

    log.info("tiger_download_complete", path=str(zip_path), bytes=zip_path.stat().st_size)
    return zip_path


def fetch_tiger_block_groups(
    state_fips: str,
    county_fips_list: list[str],
    city: str,
    bbox: list[float],
) -> gpd.GeoDataFrame:
    """Download TIGER BG boundaries for a state, filter to counties + bbox.

    Parameters
    ----------
    state_fips:
        2-digit state FIPS, e.g. ``"04"``.
    county_fips_list:
        List of full 5-digit county FIPS strings, e.g. ``["04013"]``.
    city:
        City name used for the local cache directory.
    bbox:
        ``[west, south, east, north]`` in EPSG:4326.

    Returns
    -------
    GeoDataFrame in EPSG:4326 with columns ``GEOID`` (12-char) and ``geometry``.
    """
    cache_dir = data_path("raw", "census_acs", city)
    zip_path = _download_tiger_zip(state_fips, cache_dir)

    # Read shapefile directly from the zip archive
    with zipfile.ZipFile(zip_path) as zf:
        shp_name = next(
            (n for n in zf.namelist() if n.endswith(".shp")),
            None,
        )
        if shp_name is None:
            raise FileNotFoundError(f"No .shp file found inside {zip_path}")
        gdf = gpd.read_file(f"zip://{zip_path}")

    log.info("tiger_loaded", state=state_fips, total_bgs=len(gdf))

    # Ensure EPSG:4326
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")

    # Filter to target counties (COUNTYFP is 3-digit; county_fips_list is 5-digit)
    target_county_3digit = {fips[-3:] for fips in county_fips_list}
    gdf = gdf[gdf["COUNTYFP"].isin(target_county_3digit)].copy()
    log.info("tiger_county_filtered", city=city, county_bgs=len(gdf))

    # Filter to blocks that intersect the bounding box
    west, south, east, north = bbox
    bbox_geom = box(west, south, east, north)
    gdf = gdf[gdf.intersects(bbox_geom)].copy()
    log.info("tiger_bbox_filtered", city=city, bbox_bgs=len(gdf))

    # Standardise GEOID column name
    # TIGER uses GEOID (12 chars: state+county+tract+bg) in recent vintages
    if "GEOID" not in gdf.columns:
        # Fall back: construct from component fields
        gdf["GEOID"] = (
            gdf["STATEFP"].str.zfill(2)
            + gdf["COUNTYFP"].str.zfill(3)
            + gdf["TRACTCE"].str.zfill(6)
            + gdf["BLKGRPCE"].str.zfill(1)
        )

    return gdf[["GEOID", "geometry"]].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run(city: str = "phoenix", year: int = 2022) -> None:
    """Fetch ACS + TIGER for all counties in city, join, and save files.

    Outputs (per city):
    - ``raw/census_acs/{city}/acs_block_groups.parquet`` — ACS attribute data
    - ``raw/census_acs/{city}/block_groups.gpkg`` — spatial join result

    Idempotency: if the parquet already exists for this year it is reloaded
    instead of re-fetching from the Census API.

    Note: ACS data is stored as-is here.  Phase 5 (signal computation) will
    aggregate block-group estimates to H3 hexagons and then to ticker-level
    features for the ``signal_features`` table.
    """
    cities = get_cities()
    if city not in cities:
        raise ValueError(f"Unknown city '{city}'. Available: {list(cities.keys())}")

    city_cfg = cities[city]
    state_fips: str = city_cfg["state_fips"]
    county_fips_list: list[str] = city_cfg["county_fips"]
    bbox: list[float] = city_cfg["bbox"]

    out_dir = data_path("raw", "census_acs", city)
    parquet_path = out_dir / "acs_block_groups.parquet"
    gpkg_path = out_dir / "block_groups.gpkg"

    # ── ACS attribute data ────────────────────────────────────────────────────
    if parquet_path.exists():
        log.info("acs_cache_hit", city=city, year=year, path=str(parquet_path))
        acs_df = pd.read_parquet(parquet_path)
    else:
        frames: list[pd.DataFrame] = []
        for county_fips in county_fips_list:
            county_3digit = county_fips[-3:]
            df_county = fetch_acs_block_groups(state_fips, county_3digit, year=year)
            frames.append(df_county)

        acs_df = pd.concat(frames, ignore_index=True)

        # Deduplicate in case a block group spans county boundary (rare)
        acs_df = acs_df.drop_duplicates(subset=["geoid"])

        acs_df.to_parquet(parquet_path, index=False)
        log.info(
            "acs_saved",
            city=city,
            year=year,
            rows=len(acs_df),
            path=str(parquet_path),
        )

    # ── TIGER boundaries ──────────────────────────────────────────────────────
    tiger_gdf = fetch_tiger_block_groups(state_fips, county_fips_list, city, bbox)

    log.info(
        "tiger_ready",
        city=city,
        block_groups=len(tiger_gdf),
    )

    # ── Spatial join — match ACS geoid to TIGER GEOID ─────────────────────────
    # ACS geoid is a 12-char string: state(2) + county(3) + tract(6) + bg(1)
    # TIGER GEOID is the same 12-char format
    merged_gdf = tiger_gdf.merge(
        acs_df,
        left_on="GEOID",
        right_on="geoid",
        how="left",
    )

    log.info(
        "join_result",
        city=city,
        year=year,
        tiger_bgs=len(tiger_gdf),
        acs_rows=len(acs_df),
        joined_rows=len(merged_gdf),
        matched=int(merged_gdf["geoid"].notna().sum()),
        unmatched=int(merged_gdf["geoid"].isna().sum()),
    )

    # ── Save GeoPackage ───────────────────────────────────────────────────────
    try:
        merged_gdf.to_file(gpkg_path, driver="GPKG", layer="block_groups")
        log.info("gpkg_saved", city=city, year=year, rows=len(merged_gdf), path=str(gpkg_path))
    except Exception as exc:
        log.warning("gpkg_skipped", city=city, year=year, error=str(exc))

    log.info(
        "census_acs_run_complete",
        city=city,
        year=year,
        acs_parquet=str(parquet_path),
        gpkg=str(gpkg_path),
        note=(
            "ACS block-group data stored for Phase 5 aggregation to H3 hexagons "
            "and ticker-level signal_features rows."
        ),
    )
