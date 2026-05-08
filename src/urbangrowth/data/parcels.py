"""County assessor parcel / CAMA data ingestion — Maricopa (Phoenix) and Travis (Austin).

Downloads parcel boundaries and assessed values from county ArcGIS REST services.
Normalises to a common schema, saves as GeoPackage, and upserts to the ``parcels``
PostgreSQL table.

Idempotency: if the GeoPackage already exists and is newer than 7 days the download
is skipped.  Delete the file to force a refresh.

Public API
----------
    ingest_maricopa(bbox)  -> GeoDataFrame
    ingest_travis(bbox)    -> GeoDataFrame
    ingest_city(city)      -> GeoDataFrame
    run(city="phoenix")    -> None
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
import structlog
from dotenv import load_dotenv
from shapely.geometry import MultiPolygon, shape
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from urbangrowth.config import data_path, get_cities, get_pipeline
from urbangrowth.db import loaders

load_dotenv()

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CACHE_TTL_DAYS = 7
_PAGE_SIZE = 2000
_REQUEST_TIMEOUT = 90

# Maricopa County ArcGIS REST endpoints (tried in order)
_MARICOPA_URLS: list[str] = [
    "https://mcassessor.maricopa.gov/arcgis/rest/services/Parcels/MapServer/0/query",
    "https://gis.maricopa.gov/arcgis/rest/services/Assessor/Parcels/MapServer/0/query",
]
_MARICOPA_FALLBACK_URL = (
    "https://opendata.arcgis.com/datasets/ea68416bff344c68aa1e42fe75c0a0f3_0.geojson"
)

_MARICOPA_FIELDS = (
    "APN,OWNER,MAIL_ADDR,CITY,ZIP,USE_CODE,"
    "APPRAISEDVAL,LANDVAL,IMPVAL,YR_BUILT,SQ_FT,ACRES"
)

# Travis County TCAD ArcGIS REST endpoints (tried in order)
_TRAVIS_URLS: list[str] = [
    "https://services.arcgis.com/0L95CJ0VTaxqcmED/arcgis/rest/services/TCAD_Public/FeatureServer/0/query",
]
_TRAVIS_FALLBACK_URL = (
    "https://opendata.arcgis.com/datasets/a35cbc0826e140019e5e0e71cd26785d_0.geojson"
)

_TRAVIS_FIELDS = (
    "GEO_ID,PROP_ID,OWNER_NAME,LEGAL_ACREAGE,"
    "LAND_VALUE,IMPRV_VALUE,APPRAISED_VALUE,YR_BUILT,STATE_CD,SITUS_ADDR"
)

# city_id values matching schema.sql seed order
_CITY_IDS: dict[str, int] = {"phoenix": 1, "austin": 2}

# DB table columns (geometry columns handled separately via to_postgis)
_DB_COLS = ["parcel_id", "city_id", "acreage", "year_built", "geometry"]


# ---------------------------------------------------------------------------
# Retry decorator — applied to all HTTP calls
# ---------------------------------------------------------------------------

_http_retry = retry(
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)


# ---------------------------------------------------------------------------
# Generic ArcGIS REST paginator
# ---------------------------------------------------------------------------

@_http_retry
def _arcgis_page(url: str, params: dict) -> dict:
    """Fetch a single ArcGIS REST query page and return parsed JSON."""
    resp = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _build_arcgis_params(
    bbox: list[float],
    out_fields: str,
    offset: int,
) -> dict:
    west, south, east, north = bbox
    return {
        "where": "1=1",
        "outFields": out_fields,
        "returnGeometry": "true",
        "f": "geojson",
        "outSR": "4326",
        "resultOffset": offset,
        "resultRecordCount": _PAGE_SIZE,
        "geometryType": "esriGeometryEnvelope",
        "geometry": f"{west},{south},{east},{north}",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
    }


def _paginate_arcgis(
    urls: list[str],
    bbox: list[float],
    out_fields: str,
    source_label: str,
) -> list[dict]:
    """Try each URL in order and paginate through all features.

    Returns a list of raw GeoJSON Feature dicts.
    Raises RuntimeError if every URL fails.
    """
    for url in urls:
        features: list[dict] = []
        offset = 0
        try:
            while True:
                params = _build_arcgis_params(bbox, out_fields, offset)
                log.info(
                    "arcgis_page_request",
                    source=source_label,
                    url=url,
                    offset=offset,
                )
                data = _arcgis_page(url, params)

                # ArcGIS returns {"error": {...}} for invalid requests
                if "error" in data:
                    err = data["error"]
                    raise requests.HTTPError(
                        f"ArcGIS error {err.get('code')}: {err.get('message')}"
                    )

                page_features = data.get("features", [])
                features.extend(page_features)
                log.info(
                    "arcgis_page_done",
                    source=source_label,
                    offset=offset,
                    page_rows=len(page_features),
                    cumulative=len(features),
                )

                if len(page_features) < _PAGE_SIZE:
                    break
                offset += _PAGE_SIZE
                time.sleep(0.25)  # polite pacing

            log.info(
                "arcgis_paginate_complete",
                source=source_label,
                url=url,
                total_features=len(features),
            )
            return features

        except Exception as exc:
            log.warning(
                "arcgis_endpoint_failed",
                source=source_label,
                url=url,
                error=str(exc),
            )
            # Try next URL
            continue

    raise RuntimeError(
        f"All ArcGIS REST endpoints failed for {source_label}: {urls}"
    )


# ---------------------------------------------------------------------------
# GeoJSON fallback loader
# ---------------------------------------------------------------------------

@_http_retry
def _fetch_geojson_url(url: str) -> dict:
    """Download a GeoJSON snapshot URL and return parsed dict."""
    log.info("fallback_geojson_download", url=url)
    resp = requests.get(url, timeout=300)
    resp.raise_for_status()
    return resp.json()


def _geojson_to_features(geojson: dict) -> list[dict]:
    """Extract the features list from a GeoJSON FeatureCollection."""
    return geojson.get("features", [])


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _to_multipolygon(geom_dict: dict | None):
    """Convert a GeoJSON geometry dict to a Shapely MultiPolygon.

    Polygons are promoted; MultiPolygons pass through; anything else returns None.
    """
    if geom_dict is None:
        return None
    try:
        geom = shape(geom_dict)
    except Exception:
        return None
    if geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        return MultiPolygon([geom])
    if geom.geom_type == "MultiPolygon":
        return geom
    return None


def _features_to_gdf(features: list[dict], crs: str = "EPSG:4326") -> gpd.GeoDataFrame:
    """Convert a list of GeoJSON Feature dicts to a GeoDataFrame with MultiPolygon geometry."""
    if not features:
        return gpd.GeoDataFrame()

    props_list = []
    geoms = []
    for feat in features:
        props = feat.get("properties") or {}
        geom = _to_multipolygon(feat.get("geometry"))
        props_list.append(props)
        geoms.append(geom)

    df = pd.DataFrame(props_list)
    return gpd.GeoDataFrame(df, geometry=geoms, crs=crs)


# ---------------------------------------------------------------------------
# Bbox area helper
# ---------------------------------------------------------------------------

def _bbox_area_km2(bbox: list[float]) -> float:
    """Approximate bounding box area in km² using the haversine formula."""
    west, south, east, north = bbox
    r = 6371.0
    dlat = math.radians(north - south)
    dlon = math.radians(east - west)
    lat_mid = math.radians((south + north) / 2)
    return r * r * dlat * dlon * math.cos(lat_mid)


# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------

def _is_fresh(path: Path, ttl_days: int = _CACHE_TTL_DAYS) -> bool:
    """Return True if path exists and was modified within ttl_days."""
    if not path.exists():
        return False
    age_days = (time.time() - path.stat().st_mtime) / 86400
    return age_days < ttl_days


# ---------------------------------------------------------------------------
# Maricopa County (Phoenix) ingestor
# ---------------------------------------------------------------------------

def _normalise_maricopa(gdf: gpd.GeoDataFrame, bbox: list[float]) -> gpd.GeoDataFrame:
    """Map Maricopa raw columns to the normalised parcel schema."""
    # Case-insensitive column lookup helper
    col_map = {c.upper(): c for c in gdf.columns}

    def _get(key: str):
        return gdf[col_map[key]] if key in col_map else None

    def _to_float(series):
        if series is None:
            return pd.Series([None] * len(gdf), dtype="float64")
        return pd.to_numeric(series, errors="coerce")

    def _to_int_or_none(series):
        if series is None:
            return pd.array([None] * len(gdf), dtype="Int16")
        s = pd.to_numeric(series, errors="coerce")
        s = s.where(s > 0)  # treat 0 as null
        return s.astype("Int16")

    out = gpd.GeoDataFrame(crs="EPSG:4326")
    out["parcel_id"]              = _get("APN").astype(str) if _get("APN") is not None else None
    out["city_id"]                = _CITY_IDS["phoenix"]
    out["acreage"]                = _to_float(_get("ACRES"))
    out["year_built"]             = _to_int_or_none(_get("YR_BUILT"))
    out["land_use_code"]          = _get("USE_CODE").astype(str) if _get("USE_CODE") is not None else None
    out["land_value_usd"]         = _to_float(_get("LANDVAL"))
    out["improvement_value_usd"]  = _to_float(_get("IMPVAL"))
    out["total_assessed_usd"]     = _to_float(_get("APPRAISEDVAL"))
    out["geometry"]               = gdf.geometry

    # Clip to bbox (keep only parcels whose centroid falls within bbox)
    west, south, east, north = bbox
    centroids = out.geometry.centroid
    mask = (
        centroids.x.between(west, east) &
        centroids.y.between(south, north)
    )
    out = out[mask].reset_index(drop=True)
    out = out.set_geometry("geometry")
    out.crs = "EPSG:4326"

    log.info(
        "maricopa_normalised",
        rows=len(out),
        null_parcel_ids=int(out["parcel_id"].isna().sum()),
    )
    return out


def ingest_maricopa(bbox: list[float]) -> gpd.GeoDataFrame:
    """Download Maricopa County parcel data for the given bounding box.

    Tries ArcGIS REST endpoints in order; falls back to a static GeoJSON snapshot.
    Returns a GeoDataFrame in the normalised parcel schema.
    """
    try:
        features = _paginate_arcgis(
            _MARICOPA_URLS, bbox, _MARICOPA_FIELDS, "maricopa"
        )
    except RuntimeError:
        log.warning(
            "maricopa_all_rest_failed_using_fallback",
            fallback=_MARICOPA_FALLBACK_URL,
        )
        geojson = _fetch_geojson_url(_MARICOPA_FALLBACK_URL)
        features = _geojson_to_features(geojson)

    raw_gdf = _features_to_gdf(features)
    if raw_gdf.empty:
        raise RuntimeError("ingest_maricopa: no features retrieved from any source")

    return _normalise_maricopa(raw_gdf, bbox)


# ---------------------------------------------------------------------------
# Travis County (Austin) ingestor
# ---------------------------------------------------------------------------

def _normalise_travis(gdf: gpd.GeoDataFrame, bbox: list[float]) -> gpd.GeoDataFrame:
    """Map Travis/TCAD raw columns to the normalised parcel schema."""
    col_map = {c.upper(): c for c in gdf.columns}

    def _get(key: str):
        return gdf[col_map[key]] if key in col_map else None

    def _to_float(series):
        if series is None:
            return pd.Series([None] * len(gdf), dtype="float64")
        return pd.to_numeric(series, errors="coerce")

    def _to_int_or_none(series):
        if series is None:
            return pd.array([None] * len(gdf), dtype="Int16")
        s = pd.to_numeric(series, errors="coerce")
        s = s.where(s > 0)
        return s.astype("Int16")

    # TCAD uses GEO_ID as the primary stable parcel identifier; fall back to PROP_ID
    if "GEO_ID" in col_map:
        parcel_id_series = gdf[col_map["GEO_ID"]].astype(str)
    elif "PROP_ID" in col_map:
        parcel_id_series = gdf[col_map["PROP_ID"]].astype(str)
    else:
        parcel_id_series = pd.Series([None] * len(gdf), dtype=str)

    out = gpd.GeoDataFrame(crs="EPSG:4326")
    out["parcel_id"]              = parcel_id_series
    out["city_id"]                = _CITY_IDS["austin"]
    out["acreage"]                = _to_float(_get("LEGAL_ACREAGE"))
    out["year_built"]             = _to_int_or_none(_get("YR_BUILT"))
    out["land_use_code"]          = _get("STATE_CD").astype(str) if _get("STATE_CD") is not None else None
    out["land_value_usd"]         = _to_float(_get("LAND_VALUE"))
    out["improvement_value_usd"]  = _to_float(_get("IMPRV_VALUE"))
    out["total_assessed_usd"]     = _to_float(_get("APPRAISED_VALUE"))
    out["geometry"]               = gdf.geometry

    # Clip to bbox
    west, south, east, north = bbox
    centroids = out.geometry.centroid
    mask = (
        centroids.x.between(west, east) &
        centroids.y.between(south, north)
    )
    out = out[mask].reset_index(drop=True)
    out = out.set_geometry("geometry")
    out.crs = "EPSG:4326"

    log.info(
        "travis_normalised",
        rows=len(out),
        null_parcel_ids=int(out["parcel_id"].isna().sum()),
    )
    return out


def ingest_travis(bbox: list[float]) -> gpd.GeoDataFrame:
    """Download Travis County TCAD parcel data for the given bounding box.

    Tries ArcGIS REST endpoints in order; falls back to a static GeoJSON snapshot.
    Returns a GeoDataFrame in the normalised parcel schema.
    """
    try:
        features = _paginate_arcgis(
            _TRAVIS_URLS, bbox, _TRAVIS_FIELDS, "travis"
        )
    except RuntimeError:
        log.warning(
            "travis_all_rest_failed_using_fallback",
            fallback=_TRAVIS_FALLBACK_URL,
        )
        geojson = _fetch_geojson_url(_TRAVIS_FALLBACK_URL)
        features = _geojson_to_features(geojson)

    raw_gdf = _features_to_gdf(features)
    if raw_gdf.empty:
        raise RuntimeError("ingest_travis: no features retrieved from any source")

    return _normalise_travis(raw_gdf, bbox)


# ---------------------------------------------------------------------------
# City dispatch
# ---------------------------------------------------------------------------

_COUNTY_MAP: dict[str, tuple[str, callable]] = {
    "phoenix": ("maricopa", ingest_maricopa),
    "austin":  ("travis",   ingest_travis),
}


def ingest_city(city: str) -> gpd.GeoDataFrame:
    """Ingest parcel data for a named city.

    Checks the GeoPackage cache (7-day TTL) before downloading.
    Saves the full attribute schema to GeoPackage and upserts the DB subset.
    """
    if city not in _COUNTY_MAP:
        raise ValueError(f"No parcel ingestor for city '{city}'. Valid: {list(_COUNTY_MAP)}")

    county_name, ingest_fn = _COUNTY_MAP[city]
    pipeline = get_pipeline()
    cities   = get_cities()
    bbox     = cities[city]["bbox"]

    gpkg_path: Path = (
        data_path(pipeline["raw_data_subdirs"]["parcels"][city])
        / f"{county_name}_parcels.gpkg"
    )

    # ── Idempotency check ────────────────────────────────────────────────────
    if _is_fresh(gpkg_path):
        log.info(
            "parcels_cache_fresh",
            city=city,
            file=str(gpkg_path),
            message="GeoPackage is newer than 7 days; skipping download. "
                    "Delete the file to force a refresh.",
        )
        return gpd.read_file(gpkg_path)

    # ── Download ─────────────────────────────────────────────────────────────
    log.info("parcels_download_start", city=city, county=county_name, bbox=bbox)
    gdf = ingest_fn(bbox)

    # ── Save GeoPackage (full schema) ────────────────────────────────────────
    gdf.to_file(str(gpkg_path), driver="GPKG")
    log.info("parcels_gpkg_saved", city=city, rows=len(gdf), path=str(gpkg_path))

    # ── DB upsert ────────────────────────────────────────────────────────────
    _upsert_to_db(gdf, city)

    return gdf


# ---------------------------------------------------------------------------
# DB upsert helper
# ---------------------------------------------------------------------------

def _upsert_to_db(gdf: gpd.GeoDataFrame, city: str) -> None:
    """Upsert parcel data to the ``parcels`` PostgreSQL table.

    Geometry columns are written via geopandas.to_postgis (append mode).
    Non-geometry scalar columns are handled by loaders.upsert_df.
    """
    try:
        import sqlalchemy as sa
        from geoalchemy2 import Geometry  # noqa: F401 — ensures postgis type support

        from urbangrowth.db.loaders import _engine
    except ImportError as exc:
        log.warning("parcels_db_upsert_skipped", reason=str(exc))
        return

    engine = _engine()

    # Ensure target table exists
    _ensure_table(engine)

    # Subset to DB columns only; drop rows where parcel_id is null
    db_gdf = gdf[_DB_COLS].copy()
    db_gdf = db_gdf[db_gdf["parcel_id"].notna()].reset_index(drop=True)

    if db_gdf.empty:
        log.warning("parcels_db_upsert_empty", city=city)
        return

    # Delete existing rows for this city before re-inserting (manual upsert for
    # geometry tables — geopandas does not support ON CONFLICT for geometry cols)
    with engine.begin() as conn:
        conn.execute(
            sa.text("DELETE FROM parcels WHERE city_id = :city_id"),
            {"city_id": _CITY_IDS[city]},
        )
        log.info("parcels_db_deleted_existing", city=city, city_id=_CITY_IDS[city])

    db_gdf.to_postgis(
        "parcels",
        engine,
        if_exists="append",
        index=False,
        dtype={"geometry": "GEOMETRY(MultiPolygon, 4326)"},
    )
    log.info("parcels_db_upserted", city=city, rows=len(db_gdf))


def _ensure_table(engine) -> None:
    """Create the parcels table if it does not already exist."""
    import sqlalchemy as sa

    ddl = sa.text("""
        CREATE TABLE IF NOT EXISTS parcels (
            parcel_id   TEXT NOT NULL,
            city_id     INTEGER NOT NULL,
            acreage     NUMERIC(10,4),
            year_built  SMALLINT,
            geometry    GEOMETRY(MultiPolygon, 4326),
            PRIMARY KEY (parcel_id, city_id)
        )
    """)
    with engine.begin() as conn:
        conn.execute(ddl)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def run(city: str = "phoenix") -> None:
    """Ingest parcels for one city and log a structured summary.

    Args:
        city: "phoenix" or "austin"
    """
    cities = get_cities()
    bbox   = cities[city]["bbox"]
    area   = _bbox_area_km2(bbox)

    log.info("parcels_run_start", city=city, bbox=bbox, bbox_area_km2=round(area, 2))

    gdf = ingest_city(city)

    log.info(
        "parcels_run_complete",
        city=city,
        parcel_count=len(gdf),
        bbox_area_km2=round(area, 2),
    )


# ---------------------------------------------------------------------------
# __main__ shim
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ingest county parcel data.")
    parser.add_argument(
        "--city",
        default="phoenix",
        choices=list(_COUNTY_MAP),
        help="City to ingest (default: phoenix)",
    )
    args = parser.parse_args()
    run(city=args.city)
