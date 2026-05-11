"""City zoning layer ingestion — Phoenix and Austin.

Downloads current zoning GeoJSON from each city's open data portal,
normalizes raw zone codes to a shared 11-category taxonomy, persists as
GeoPackage, and upserts into the ``zoning_snapshots`` PostgreSQL table.

Idempotent: if the output GeoPackage for a city+date already exists the
entire pipeline is skipped.

Usage (CLI)::

    python -m urbangrowth.data.zoning --city phoenix
    python -m urbangrowth.data.zoning --city austin --snapshot-date 2026-05-06
"""
from __future__ import annotations

import argparse
import os
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

import geopandas as gpd
import pandas as pd
import requests
import structlog
import yaml
from dotenv import load_dotenv
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import create_engine
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from urbangrowth.config import data_path, get_pipeline

load_dotenv()

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(__file__).parent.parent.parent.parent / "config"

_CITY_ID: dict[str, int] = {
    "phoenix": 1,
    "austin": 2,
}

# Phoenix ArcGIS REST endpoints (tried in order)
_PHX_ARCGIS_URL = (
    "https://services.arcgis.com/yG5s3afENB5iO9fj/arcgis/rest/services"
    "/Zoning/FeatureServer/0/query"
)
_PHX_STATIC_URL = (
    "https://opendata.arcgis.com/datasets/c607e5c53d4f4b11aafe629f2efcd07a_0.geojson"
)

# Maricopa County unincorporated (optional second layer)
_MARICOPA_URL = (
    "https://gis.maricopa.gov/arcgis/rest/services/Zoning/MapServer/0/query"
)

# Austin Socrata GeoJSON
_ATX_BASE_URL = "https://data.austintexas.gov/resource/q3y3-ungd.geojson"
_ATX_PAGE_LIMIT = 50_000

# ArcGIS REST page size
_ARCGIS_PAGE_SIZE = 1_000

# Output column order
_GDF_COLS = [
    "zone_code_raw",
    "zone_code_normalized",
    "parcel_id",
    "snapshot_date",
    "city_id",
    "geometry",
]

# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _load_norm_map() -> dict[str, dict[str, str]]:
    """Load zoning_normalization.yaml and invert to {city: {raw_code: category}}.

    The YAML has structure::

        phoenix:
          residential_low: [R1-6, R1-8, ...]
          commercial:      [C-1, C-2, ...]

    This is inverted so callers can do ``norm_map["phoenix"]["R1-6"]``
    and get back ``"residential_low"``.
    """
    path = _CONFIG_DIR / "zoning_normalization.yaml"
    with open(path, encoding="utf-8") as fh:
        raw: dict[str, dict[str, list[str]]] = yaml.safe_load(fh)

    inverted: dict[str, dict[str, str]] = {}
    for city, categories in raw.items():
        lookup: dict[str, str] = {}
        for category, codes in (categories or {}).items():
            for code in (codes or []):
                lookup[str(code).strip()] = category
        inverted[city] = lookup
    return inverted


def normalize_zone(raw_code: str, city: str, norm_map: dict[str, dict[str, str]]) -> str:
    """Return normalized category for *raw_code* from *city*.

    Resolution order:
    1. Exact match in the city lookup (case-insensitive strip).
    2. Prefix match — longest prefix wins (e.g. ``R1-6`` before ``R1``).
    3. ``"other"`` if nothing matched.
    """
    if not raw_code or not isinstance(raw_code, str):
        return "other"

    code = raw_code.strip()
    city_map = norm_map.get(city, {})

    # 1. Exact match (case-insensitive)
    if code in city_map:
        return city_map[code]
    code_upper = code.upper()
    for key, cat in city_map.items():
        if key.upper() == code_upper:
            return cat

    # 2. Longest prefix match
    best_len = 0
    best_cat = "other"
    for key, cat in city_map.items():
        key_u = key.upper()
        if code_upper.startswith(key_u) and len(key_u) > best_len:
            best_len = len(key_u)
            best_cat = cat

    return best_cat


# ---------------------------------------------------------------------------
# DB engine
# ---------------------------------------------------------------------------


def _engine():
    pipe = get_pipeline()
    user = os.environ.get("POSTGRES_USER", "urbangrowth")
    pwd  = os.environ.get("POSTGRES_PASSWORD", "changeme")
    host = pipe["db"]["host"]
    port = pipe["db"]["port"]
    db   = pipe["db"]["dbname"]
    return create_engine(
        f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{db}",
        pool_pre_ping=True,
    )


# ---------------------------------------------------------------------------
# HTTP helpers with retry
# ---------------------------------------------------------------------------


@retry(
    retry=retry_if_exception_type(requests.HTTPError),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _get_json(url: str, params: dict) -> dict:
    """GET *url* with *params*, return parsed JSON dict. Retries on HTTPError."""
    resp = requests.get(url, params=params, timeout=120)
    resp.raise_for_status()
    return resp.json()


@retry(
    retry=retry_if_exception_type(requests.HTTPError),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _get_geojson_text(url: str, params: dict | None = None) -> str:
    """GET GeoJSON bytes as text. Retries on HTTPError."""
    resp = requests.get(url, params=params or {}, timeout=300)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _to_multipolygon(geom):
    """Cast Polygon → MultiPolygon; leave MultiPolygon as-is; skip others."""
    if geom is None:
        return None
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    if isinstance(geom, MultiPolygon):
        return geom
    # GeometryCollection or other types — attempt extraction
    if hasattr(geom, "geoms"):
        polys = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon))]
        if polys:
            merged = []
            for p in polys:
                if isinstance(p, MultiPolygon):
                    merged.extend(p.geoms)
                else:
                    merged.append(p)
            return MultiPolygon(merged) if merged else None
    return None


# ---------------------------------------------------------------------------
# Phoenix ingestion — ArcGIS REST (paginated) with static fallback
# ---------------------------------------------------------------------------


def _fetch_arcgis_pages(url: str, label: str) -> list[dict]:
    """Paginate an ArcGIS Feature Service query endpoint.

    Returns a flat list of GeoJSON feature dicts.
    Raises ``requests.HTTPError`` on non-2xx responses (after retries).
    """
    features: list[dict] = []
    offset = 0
    page = 1

    while True:
        params = {
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "f": "geojson",
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": _ARCGIS_PAGE_SIZE,
        }
        log.info("arcgis_page_fetch", source=label, page=page, offset=offset)
        data = _get_json(url, params)
        page_features = data.get("features", [])
        log.info(
            "arcgis_page_received",
            source=label,
            page=page,
            features_this_page=len(page_features),
            cumulative=len(features) + len(page_features),
        )
        if not page_features:
            break
        features.extend(page_features)
        if len(page_features) < _ARCGIS_PAGE_SIZE:
            # Last page — no need to ask again
            break
        offset += _ARCGIS_PAGE_SIZE
        page += 1

    log.info("arcgis_fetch_complete", source=label, total_features=len(features))
    return features


def _features_to_gdf(features: list[dict]) -> gpd.GeoDataFrame:
    """Convert a list of GeoJSON feature dicts to a GeoDataFrame (EPSG:4326)."""
    fc = {"type": "FeatureCollection", "features": features}
    gdf = gpd.GeoDataFrame.from_features(fc, crs="EPSG:4326")
    return gdf


def _raw_zone_col_phoenix(gdf: gpd.GeoDataFrame) -> str:
    """Detect the raw zone-code column name in a Phoenix GDF."""
    candidates = ["ZONING", "ZONE_DIST", "ZONE_CODE", "ZONE", "zoning",
                  "zone_dist", "zone_code", "zone"]
    for col in candidates:
        if col in gdf.columns:
            return col
    # Fallback: first text-like column that isn't geometry
    for col in gdf.columns:
        if col.lower() not in ("geometry", "objectid", "shape_area", "shape_length"):
            return col
    raise KeyError(f"Cannot identify zone code column in Phoenix GDF. Columns: {list(gdf.columns)}")


def _raw_parcel_col_phoenix(gdf: gpd.GeoDataFrame) -> str | None:
    """Return parcel ID column name if present, else None."""
    candidates = ["APN", "PARCEL_ID", "PARCELID", "PIN", "apn", "parcel_id"]
    for col in candidates:
        if col in gdf.columns:
            return col
    return None


def ingest_phoenix() -> gpd.GeoDataFrame:
    """Fetch Phoenix zoning from ArcGIS REST; fall back to static GeoJSON snapshot.

    Attempts paginated ArcGIS REST first. If that fails, loads the static
    GeoJSON snapshot. Also merges Maricopa County unincorporated zoning
    (city parcels take precedence for overlaps).

    Returns a GeoDataFrame with the ``_GDF_COLS`` schema.
    """
    # -- City of Phoenix zoning --
    city_features: list[dict] = []
    try:
        city_features = _fetch_arcgis_pages(_PHX_ARCGIS_URL, "phoenix_city")
    except requests.HTTPError as exc:
        log.warning(
            "phoenix_arcgis_rest_failed",
            url=_PHX_ARCGIS_URL,
            error=str(exc),
            action="falling_back_to_static_geojson",
        )

    if city_features:
        city_gdf = _features_to_gdf(city_features)
    else:
        log.info("phoenix_static_geojson_fetch", url=_PHX_STATIC_URL)
        try:
            text = _get_geojson_text(_PHX_STATIC_URL)
            import io
            city_gdf = gpd.read_file(io.StringIO(text))
            if city_gdf.crs is None:
                city_gdf = city_gdf.set_crs("EPSG:4326")
            else:
                city_gdf = city_gdf.to_crs("EPSG:4326")
        except requests.HTTPError as exc:
            log.warning(
                "phoenix_static_geojson_failed",
                url=_PHX_STATIC_URL,
                error=str(exc),
            )
            city_gdf = gpd.GeoDataFrame(columns=["geometry"], geometry="geometry",
                                        crs="EPSG:4326")

    # -- Maricopa County unincorporated (optional) --
    county_gdf: gpd.GeoDataFrame | None = None
    try:
        county_features = _fetch_arcgis_pages(_MARICOPA_URL, "maricopa_county")
        if county_features:
            county_gdf = _features_to_gdf(county_features)
    except requests.HTTPError as exc:
        log.warning(
            "maricopa_county_zoning_failed",
            url=_MARICOPA_URL,
            error=str(exc),
            action="skipping_county_layer",
        )

    # Merge: county first, city on top (city polygons take precedence)
    if county_gdf is not None and not county_gdf.empty:
        combined = gpd.GeoDataFrame(
            pd.concat([county_gdf, city_gdf], ignore_index=True),
            crs="EPSG:4326",
        )
    else:
        combined = city_gdf

    return combined


def _raw_zone_col_austin(gdf: gpd.GeoDataFrame) -> str:
    """Detect the raw zone-code column name in an Austin GDF."""
    candidates = ["zoning_zty", "zoning_type", "ZONING_ZTY", "zone_class",
                  "ZONE_CLASS", "zoning", "ZONING"]
    for col in candidates:
        if col in gdf.columns:
            return col
    for col in gdf.columns:
        if col.lower() not in ("geometry", "objectid", ":id"):
            return col
    raise KeyError(f"Cannot identify zone code column in Austin GDF. Columns: {list(gdf.columns)}")


def _raw_parcel_col_austin(gdf: gpd.GeoDataFrame) -> str | None:
    candidates = ["parcel_id_primary", "parcel_id", "PARCEL_ID", "pin", "PIN"]
    for col in candidates:
        if col in gdf.columns:
            return col
    return None


# ---------------------------------------------------------------------------
# Austin ingestion — Socrata GeoJSON (paginated)
# ---------------------------------------------------------------------------


def ingest_austin() -> gpd.GeoDataFrame:
    """Fetch Austin zoning from the Austin Open Data Socrata endpoint.

    Paginates with ``$limit`` / ``$offset`` until a page returns fewer rows
    than the limit.

    Returns a GeoDataFrame with raw source columns (not yet normalized).
    """
    all_frames: list[gpd.GeoDataFrame] = []
    offset = 0
    page = 1

    while True:
        params = {
            "$limit": _ATX_PAGE_LIMIT,
            "$offset": offset,
        }
        log.info("austin_socrata_page_fetch", page=page, offset=offset)
        try:
            text = _get_geojson_text(_ATX_BASE_URL, params)
        except requests.HTTPError as exc:
            log.warning(
                "austin_socrata_failed",
                url=_ATX_BASE_URL,
                error=str(exc),
            )
            break

        import io
        frame = gpd.read_file(io.StringIO(text))
        n = len(frame)
        log.info(
            "austin_socrata_page_received",
            page=page,
            features_this_page=n,
            cumulative=sum(len(f) for f in all_frames) + n,
        )

        if n == 0:
            break
        if frame.crs is None:
            frame = frame.set_crs("EPSG:4326")
        else:
            frame = frame.to_crs("EPSG:4326")
        all_frames.append(frame)

        if n < _ATX_PAGE_LIMIT:
            break
        offset += _ATX_PAGE_LIMIT
        page += 1

    if not all_frames:
        log.warning("austin_no_data_returned")
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs="EPSG:4326")

    gdf = gpd.GeoDataFrame(
        pd.concat(all_frames, ignore_index=True),
        crs="EPSG:4326",
    )
    log.info("austin_fetch_complete", total_features=len(gdf))
    return gdf


# ---------------------------------------------------------------------------
# Normalization & schema assembly
# ---------------------------------------------------------------------------


def _assemble_gdf(
    raw_gdf: gpd.GeoDataFrame,
    city: str,
    snapshot_date: str,
    norm_map: dict[str, dict[str, str]],
    zone_col: str,
    parcel_col: str | None,
) -> gpd.GeoDataFrame:
    """Apply normalization and produce a GDF with the canonical ``_GDF_COLS`` schema."""
    gdf = raw_gdf.copy()

    # Zone code raw
    gdf["zone_code_raw"] = (
        gdf[zone_col].astype(str).str.strip()
        if zone_col in gdf.columns
        else pd.Series(["unknown"] * len(gdf), dtype=str)
    )

    # Normalized category
    gdf["zone_code_normalized"] = gdf["zone_code_raw"].apply(
        lambda code: normalize_zone(code, city, norm_map)
    )

    # Parcel ID
    gdf["parcel_id"] = (
        gdf[parcel_col].astype(str).where(gdf[parcel_col].notna())
        if parcel_col and parcel_col in gdf.columns
        else None
    )

    gdf["snapshot_date"] = snapshot_date
    gdf["city_id"] = _CITY_ID[city]

    # Cast all geometries to MultiPolygon
    gdf["geometry"] = gdf["geometry"].apply(_to_multipolygon)

    # Drop rows where geometry conversion failed
    before = len(gdf)
    gdf = gdf[gdf["geometry"].notna()].copy()
    if len(gdf) < before:
        log.warning(
            "geometry_cast_dropped_rows",
            city=city,
            dropped=before - len(gdf),
            remaining=len(gdf),
        )

    return gpd.GeoDataFrame(gdf[_GDF_COLS], geometry="geometry", crs="EPSG:4326")


def _log_normalization_summary(gdf: gpd.GeoDataFrame, city: str) -> None:
    """Log count per normalized category."""
    counts = gdf["zone_code_normalized"].value_counts().to_dict()
    log.info(
        "normalization_summary",
        city=city,
        total_features=len(gdf),
        **{f"cat_{k}": v for k, v in counts.items()},
    )


# ---------------------------------------------------------------------------
# GeoPackage output
# ---------------------------------------------------------------------------


def _gpkg_path(city: str, snapshot_date: str) -> Path:
    pipeline = get_pipeline()
    subdir = pipeline["raw_data_subdirs"]["zoning"][city]
    return data_path(subdir) / f"{city}_zoning_{snapshot_date}.gpkg"


# ---------------------------------------------------------------------------
# DB upsert via geopandas to_postgis
# ---------------------------------------------------------------------------


def _upsert_to_postgis(gdf: gpd.GeoDataFrame) -> None:
    """Append the zoning GeoDataFrame to ``zoning_snapshots`` via to_postgis."""
    try:
        engine = _engine()
        gdf.to_postgis(
            "zoning_snapshots",
            engine,
            if_exists="append",
            index=False,
        )
        log.info("postgis_upsert_complete", rows=len(gdf))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "postgis_upsert_failed",
            error=str(exc),
            action="data_saved_to_gpkg_only",
        )


# ---------------------------------------------------------------------------
# Public run() entry point
# ---------------------------------------------------------------------------


def run(city: str = "phoenix", snapshot_date: Optional[str] = None) -> None:
    """Download, normalize, persist, and upsert zoning data for *city*.

    Args:
        city:          ``"phoenix"`` or ``"austin"``.
        snapshot_date: ISO date string ``"YYYY-MM-DD"``. Defaults to today.
    """
    if city not in _CITY_ID:
        raise ValueError(f"Unsupported city '{city}'. Choose from: {list(_CITY_ID)}")

    snapshot_date = snapshot_date or date.today().isoformat()

    # Idempotency check
    gpkg = _gpkg_path(city, snapshot_date)
    if gpkg.exists():
        log.info("zoning_gpkg_exists_skip", city=city, snapshot_date=snapshot_date,
                 path=str(gpkg))
        return

    log.info("zoning_run_start", city=city, snapshot_date=snapshot_date)

    # Load normalization map
    norm_map = _load_norm_map()

    # Fetch raw data
    if city == "phoenix":
        raw_gdf = ingest_phoenix()
        zone_col = _raw_zone_col_phoenix(raw_gdf) if not raw_gdf.empty else "ZONING"
        parcel_col = _raw_parcel_col_phoenix(raw_gdf) if not raw_gdf.empty else None
    else:
        raw_gdf = ingest_austin()
        zone_col = _raw_zone_col_austin(raw_gdf) if not raw_gdf.empty else "zoning_zty"
        parcel_col = _raw_parcel_col_austin(raw_gdf) if not raw_gdf.empty else None

    log.info(
        "raw_fetch_complete",
        city=city,
        raw_rows=len(raw_gdf),
        zone_col=zone_col,
        parcel_col=parcel_col,
    )

    if raw_gdf.empty:
        log.warning("zoning_raw_empty", city=city,
                    action="aborting_no_data_to_persist")
        return

    # Normalize and assemble canonical schema
    gdf = _assemble_gdf(raw_gdf, city, snapshot_date, norm_map, zone_col, parcel_col)

    _log_normalization_summary(gdf, city)

    # Save GeoPackage
    gdf.to_file(str(gpkg), driver="GPKG")
    log.info("zoning_gpkg_saved", city=city, rows=len(gdf), path=str(gpkg))

    # Upsert to PostgreSQL
    _upsert_to_postgis(gdf)

    log.info("zoning_run_complete", city=city, snapshot_date=snapshot_date,
             rows=len(gdf))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest city zoning layers.")
    parser.add_argument(
        "--city",
        default="phoenix",
        choices=list(_CITY_ID),
        help="City to ingest (default: phoenix).",
    )
    parser.add_argument(
        "--snapshot-date",
        default=None,
        help="ISO date string YYYY-MM-DD (default: today).",
    )
    args = parser.parse_args()
    run(city=args.city, snapshot_date=args.snapshot_date)
