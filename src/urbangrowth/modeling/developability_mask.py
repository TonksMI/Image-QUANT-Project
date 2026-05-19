"""Developability mask for H3 resolution-8 cells.

Identifies cells that overlap protected land, tribal reservations, water bodies,
and airport footprints, then computes a *developable_frac* per cell.

Exclusion sources (tried in order, with local caching):
  1. USGS PAD-US 3.0 REST API  — protected / conservation / tribal land
  2. USGS NHD REST API         — waterbodies (lakes, ponds, reservoirs)
  3. OSM Overpass API          — airport footprints (aeroway=aerodrome)
  4. Hardcoded fallback        — approximate polygons for named preserves,
                                 tribal lands, and airports in each MSA
                                 (always applied as an additional safety net)

If the caller supplies pre-fetched file paths via `cfg` the corresponding API
call is skipped and the local file is loaded instead.

Public API
----------
    mask_df = build_mask(
        h3_cells  = list_of_h3_index_strings,
        city_key  = "phoenix",                   # or "austin"
        bbox      = [-112.35, 33.20, -111.65, 33.85],  # [W, S, E, N]
        utm_crs   = "EPSG:32612",
        cfg       = scoring_cfg["developability_mask"],
        cache_dir = Path("D:/urbangrowth_data/raw/developability"),
    )
    # Returns DataFrame: h3_index | developable | developable_frac | exclusion_reasons

Notes
-----
- All area maths are done in the local UTM CRS (projected) for accuracy.
- Intersection computation uses geopandas + shapely (already in the project env).
- Overpass and USGS calls time out after 90 s; failures fall through to the
  next source rather than raising, so the pipeline keeps running.
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import h3 as h3lib
import numpy as np
import pandas as pd

try:
    import geopandas as gpd
    import requests
    from shapely.geometry import MultiPolygon, Polygon, box
    from shapely.ops import unary_union
    _GEO_AVAILABLE = True
except ImportError:
    _GEO_AVAILABLE = False

try:
    import structlog
    log = structlog.get_logger(__name__)
except ImportError:
    import logging
    log = logging.getLogger(__name__)

# ── Hardcoded fallback polygons ───────────────────────────────────────────────
# Approximate bounding rectangles (W, S, E, N) for known major exclusion areas.
# These are applied even when API downloads succeed.
# Coordinates are rough; the APIs provide accurate geometries when available.
#
# Source notes:
#   Phoenix: McDowell Sonoran Preserve, Salt River tribal lands, Fort McDowell,
#            Tonto National Forest (south edge), Sky Harbor Airport
#   Austin:  Lake Travis, Lady Bird Lake, Lake Austin, Barton Creek Greenbelt,
#            Austin-Bergstrom International Airport

_HARDCODED_EXCLUSIONS: dict[str, list[dict]] = {
    "phoenix": [
        # McDowell Sonoran Preserve — ~30,000 acres, NE Scottsdale
        {"label": "protected",
         "bbox": (-111.900, 33.540, -111.740, 33.730)},
        # Salt River Pima-Maricopa Indian Community
        {"label": "tribal",
         "bbox": (-111.840, 33.385, -111.645, 33.510)},
        # Fort McDowell Yavapai Nation — NE of Salt River reservation
        {"label": "tribal",
         "bbox": (-111.770, 33.510, -111.580, 33.660)},
        # Tonto National Forest — southern edge that dips into MSA bbox
        {"label": "protected",
         "bbox": (-112.000, 33.700, -111.650, 33.850)},
        # Phoenix Sky Harbor International Airport
        {"label": "airport",
         "bbox": (-112.040, 33.415, -111.970, 33.460)},
        # Luke Air Force Base (western boundary)
        {"label": "airport",
         "bbox": (-112.380, 33.520, -112.340, 33.560)},
        # Estrella Mountain Regional Park (SW Phoenix)
        {"label": "protected",
         "bbox": (-112.360, 33.400, -112.250, 33.490)},
        # White Tank Mountain Regional Park (W Phoenix)
        {"label": "protected",
         "bbox": (-112.560, 33.540, -112.430, 33.680)},
        # South Mountain Park / Preserve (S Phoenix)
        {"label": "protected",
         "bbox": (-112.130, 33.295, -111.930, 33.370)},
    ],
    "austin": [
        # Lake Travis — large Highland Lakes reservoir (W of Austin)
        {"label": "water",
         "bbox": (-97.990, 30.320, -97.760, 30.530)},
        # Lake Austin — narrow reservoir through central-west Austin
        {"label": "water",
         "bbox": (-97.880, 30.270, -97.740, 30.390)},
        # Lady Bird Lake / Town Lake — central Austin
        {"label": "water",
         "bbox": (-97.810, 30.230, -97.685, 30.260)},
        # Barton Creek Greenbelt — SW Austin
        {"label": "protected",
         "bbox": (-97.840, 30.215, -97.745, 30.285)},
        # Austin-Bergstrom International Airport
        {"label": "airport",
         "bbox": (-97.695, 30.183, -97.638, 30.220)},
        # Balcones Canyonlands NWR (NW Austin / Williamson County)
        {"label": "protected",
         "bbox": (-97.980, 30.540, -97.780, 30.650)},
        # Lake Walter E. Long (eastern Austin)
        {"label": "water",
         "bbox": (-97.590, 30.290, -97.505, 30.360)},
    ],
}


# ── H3 → GeoDataFrame ─────────────────────────────────────────────────────────

def _h3_cells_to_gdf(h3_cells: list[str]) -> "gpd.GeoDataFrame":
    """Convert H3 index strings to a GeoDataFrame of Shapely Polygons (EPSG:4326)."""
    records = []
    for cell in h3_cells:
        # h3.cell_to_boundary returns [(lat, lon), ...] — Shapely wants (lon, lat)
        boundary = h3lib.cell_to_boundary(cell)
        polygon = Polygon([(lon, lat) for lat, lon in boundary])
        records.append({"h3_index": cell, "geometry": polygon})
    return gpd.GeoDataFrame(records, crs="EPSG:4326")


# ── Exclusion geometry sources ────────────────────────────────────────────────

def _bbox_to_envelope(bbox: list[float]) -> "Polygon":
    """Return a Shapely box from [W, S, E, N]."""
    return box(bbox[0], bbox[1], bbox[2], bbox[3])


def _hardcoded_exclusions_gdf(city_key: str) -> "gpd.GeoDataFrame":
    """Return a GeoDataFrame of hardcoded fallback exclusion polygons."""
    entries = _HARDCODED_EXCLUSIONS.get(city_key, [])
    if not entries:
        return gpd.GeoDataFrame(columns=["geometry", "excl_label"], crs="EPSG:4326")
    records = [
        {"geometry": _bbox_to_envelope(e["bbox"]), "excl_label": e["label"]}
        for e in entries
    ]
    return gpd.GeoDataFrame(records, crs="EPSG:4326")


def _fetch_padus(bbox: list[float], cache_path: Path | None) -> "gpd.GeoDataFrame | None":
    """Fetch PAD-US 3.0 protected areas for *bbox* from the USGS ArcGIS REST service.

    Returns a GeoDataFrame (EPSG:4326) with column ``excl_label="protected"``,
    or None on failure.

    Pagination is handled automatically (ArcGIS default limit = 1 000 records).
    """
    if cache_path and cache_path.exists():
        try:
            gdf = gpd.read_file(cache_path)
            gdf["excl_label"] = "protected"
            log.info("padus_loaded_from_cache", path=str(cache_path), rows=len(gdf))
            return gdf
        except Exception as exc:
            log.warning("padus_cache_load_failed", error=str(exc))

    w, s, e, n = bbox
    # PAD-US 3.0 — primary endpoint (USGS ArcGIS Server).
    # Alternative if this returns 404: ArcGIS Online hosted layer at
    #   https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/
    #       USA_Protected_Areas/FeatureServer/0/query
    base_url = (
        "https://gis1.usgs.gov/arcgis/rest/services/PADUS3_0/MapServer/0/query"
    )
    all_features: list[dict] = []
    offset = 0
    page_size = 1000

    while True:
        params = {
            "geometry": f"{w},{s},{e},{n}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "Mang_Name,Des_Tp,Loc_Ds,State_Nm",
            "returnGeometry": "true",
            "f": "geojson",
            "outSR": "4326",
            "resultRecordCount": str(page_size),
            "resultOffset": str(offset),
        }
        try:
            resp = requests.get(base_url, params=params, timeout=90)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("padus_api_failed", error=str(exc), offset=offset)
            break

        features = data.get("features", [])
        all_features.extend(features)
        if len(features) < page_size:
            break  # last page
        offset += page_size

    if not all_features:
        log.warning("padus_no_features_returned")
        return None

    try:
        gdf = gpd.GeoDataFrame.from_features(all_features, crs="EPSG:4326")
        gdf["excl_label"] = "protected"
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            gdf.to_file(str(cache_path), driver="GeoJSON")
        log.info("padus_fetched", rows=len(gdf))
        return gdf
    except Exception as exc:
        log.warning("padus_parse_failed", error=str(exc))
        return None


def _fetch_nhd_water(bbox: list[float], cache_path: Path | None) -> "gpd.GeoDataFrame | None":
    """Fetch NHD waterbody polygons (lakes, ponds, reservoirs) for *bbox*.

    Uses the USGS National Map REST service (NHD WaterbodyLayer).
    Returns GeoDataFrame (EPSG:4326) with ``excl_label="water"``, or None.
    """
    if cache_path and cache_path.exists():
        try:
            gdf = gpd.read_file(cache_path)
            gdf["excl_label"] = "water"
            log.info("nhd_loaded_from_cache", path=str(cache_path), rows=len(gdf))
            return gdf
        except Exception as exc:
            log.warning("nhd_cache_load_failed", error=str(exc))

    w, s, e, n = bbox
    # NHD WaterbodyLayer via National Map REST API.
    # Layer 8 = NHDWaterbody polygon features (lakes, ponds, reservoirs).
    # Alternative if this 404s: use layer index 4 or check the MapServer
    #   catalogue at https://hydrowfs.nationalmap.gov/arcgis/rest/services/nhd/MapServer
    base_url = (
        "https://hydrowfs.nationalmap.gov/arcgis/rest/services/nhd/MapServer/8/query"
    )
    params = {
        "geometry": f"{w},{s},{e},{n}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "GNIS_NAME,FTYPE",
        "returnGeometry": "true",
        "f": "geojson",
        "outSR": "4326",
        "resultRecordCount": "2000",
    }
    try:
        resp = requests.get(base_url, params=params, timeout=90)
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features", [])
    except Exception as exc:
        log.warning("nhd_api_failed", error=str(exc))
        return None

    if not features:
        log.info("nhd_no_features_for_bbox")
        return None

    try:
        gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
        gdf["excl_label"] = "water"
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            gdf.to_file(str(cache_path), driver="GeoJSON")
        log.info("nhd_fetched", rows=len(gdf))
        return gdf
    except Exception as exc:
        log.warning("nhd_parse_failed", error=str(exc))
        return None


def _fetch_airports_osm(bbox: list[float], cache_path: Path | None) -> "gpd.GeoDataFrame | None":
    """Fetch airport footprints from OSM Overpass for *bbox*.

    Queries for aeroway=aerodrome and aeroway=runway closed ways.
    Returns GeoDataFrame (EPSG:4326) with ``excl_label="airport"``, or None.
    """
    if cache_path and cache_path.exists():
        try:
            gdf = gpd.read_file(cache_path)
            gdf["excl_label"] = "airport"
            log.info("airports_loaded_from_cache", path=str(cache_path), rows=len(gdf))
            return gdf
        except Exception as exc:
            log.warning("airports_cache_load_failed", error=str(exc))

    w, s, e, n = bbox
    # Overpass QL: closed ways tagged as aerodrome or runway
    # "out geom;" returns node coords embedded in each way — no need for separate node lookup
    query = f"""[out:json][timeout:90];
(
  way["aeroway"~"aerodrome|runway"]({s},{w},{n},{e});
  relation["aeroway"="aerodrome"]({s},{w},{n},{e});
);
out geom;"""

    try:
        resp = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            headers={
                "User-Agent": "urbangrowth-scoring-pipeline/1.0 (research)",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=90,
        )
        resp.raise_for_status()
        elements = resp.json().get("elements", [])
    except Exception as exc:
        log.warning("overpass_airports_failed", error=str(exc))
        return None

    polygons: list[dict] = []
    for el in elements:
        if el.get("type") != "way":
            continue
        geom_nodes = el.get("geometry", [])
        if not geom_nodes:
            continue
        coords = [(g["lon"], g["lat"]) for g in geom_nodes]
        if len(coords) < 4:
            continue
        try:
            poly = Polygon(coords)
            if poly.is_valid and not poly.is_empty:
                polygons.append({"geometry": poly, "excl_label": "airport"})
        except Exception:
            continue

    if not polygons:
        log.info("overpass_no_airport_polygons")
        return None

    gdf = gpd.GeoDataFrame(polygons, crs="EPSG:4326")
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        gdf.to_file(str(cache_path), driver="GeoJSON")
    log.info("airports_fetched_osm", rows=len(gdf))
    return gdf


def _load_or_fetch_exclusions(
    city_key: str,
    bbox: list[float],
    cfg: dict[str, Any],
    cache_dir: Path | None,
) -> "gpd.GeoDataFrame":
    """Assemble the full exclusion GeoDataFrame for *city_key*.

    Priority for each layer:
      - Pre-fetched file in cfg ("padus_gpkg", "nhd_gpkg", "faa_geojson") if provided
      - API download (cached to cache_dir after first successful call)
      - Skip layer silently if both fail

    Hardcoded fallback polygons are ALWAYS appended.
    """
    parts: list["gpd.GeoDataFrame"] = []

    def _cache(name: str) -> Path | None:
        return (cache_dir / f"{city_key}_{name}.geojson") if cache_dir else None

    # ── Protected / tribal land (PAD-US) ──────────────────────────────────────
    padus_path = cfg.get("padus_gpkg")
    if padus_path:
        try:
            gdf = gpd.read_file(padus_path)
            gdf["excl_label"] = "protected"
            parts.append(gdf[["geometry", "excl_label"]])
        except Exception as exc:
            log.warning("padus_file_load_failed", path=padus_path, error=str(exc))
    else:
        fetched = _fetch_padus(bbox, _cache("padus"))
        if fetched is not None:
            parts.append(fetched[["geometry", "excl_label"]])

    # ── Water bodies (NHD) ────────────────────────────────────────────────────
    nhd_path = cfg.get("nhd_gpkg")
    if nhd_path:
        try:
            gdf = gpd.read_file(nhd_path)
            gdf["excl_label"] = "water"
            parts.append(gdf[["geometry", "excl_label"]])
        except Exception as exc:
            log.warning("nhd_file_load_failed", path=nhd_path, error=str(exc))
    else:
        fetched = _fetch_nhd_water(bbox, _cache("nhd"))
        if fetched is not None:
            parts.append(fetched[["geometry", "excl_label"]])

    # ── Airport footprints (OSM Overpass) ─────────────────────────────────────
    faa_path = cfg.get("faa_geojson")
    if faa_path:
        try:
            gdf = gpd.read_file(faa_path)
            gdf["excl_label"] = "airport"
            parts.append(gdf[["geometry", "excl_label"]])
        except Exception as exc:
            log.warning("faa_file_load_failed", path=faa_path, error=str(exc))
    else:
        fetched = _fetch_airports_osm(bbox, _cache("airports_osm"))
        if fetched is not None:
            parts.append(fetched[["geometry", "excl_label"]])

    # ── Hardcoded fallback polygons (always applied) ──────────────────────────
    if cfg.get("use_hardcoded_fallbacks", True):
        hc = _hardcoded_exclusions_gdf(city_key)
        if not hc.empty:
            parts.append(hc[["geometry", "excl_label"]])

    if not parts:
        log.warning("no_exclusion_data", city_key=city_key)
        return gpd.GeoDataFrame(columns=["geometry", "excl_label"], crs="EPSG:4326")

    combined = gpd.GeoDataFrame(
        pd.concat(parts, ignore_index=True), crs="EPSG:4326"
    )
    # Buffer by 0 to fix any minor topology issues
    combined["geometry"] = combined.geometry.buffer(0)
    combined = combined[combined.geometry.is_valid & ~combined.geometry.is_empty]
    log.info("exclusion_zones_combined", city_key=city_key, rows=len(combined))
    return combined


# ── Developable fraction computation ─────────────────────────────────────────

def _compute_developable_fracs(
    cells_gdf: "gpd.GeoDataFrame",
    excl_gdf: "gpd.GeoDataFrame",
    utm_crs: str,
) -> pd.DataFrame:
    """Compute the developable fraction for each H3 cell.

    Projects both GeoDataFrames to *utm_crs* for accurate area arithmetic.
    Returns a DataFrame with columns: h3_index, developable_frac, excl_area_m2,
    cell_area_m2, exclusion_reasons (pipe-delimited unique labels).
    """
    # Project to local UTM for accurate area (metres²)
    cells_utm = cells_gdf.to_crs(utm_crs)
    excl_utm  = excl_gdf.to_crs(utm_crs)

    # Build a spatial index over exclusion zones for fast intersection
    excl_sindex = excl_utm.sindex

    rows = []
    for _, cell in cells_utm.iterrows():
        cell_geom = cell.geometry
        cell_area = cell_geom.area  # m²

        # Candidate exclusion zones (bounding-box pre-filter via spatial index)
        candidate_idxs = list(excl_sindex.intersection(cell_geom.bounds))
        if not candidate_idxs:
            rows.append({
                "h3_index": cell["h3_index"],
                "developable_frac": 1.0,
                "excl_area_m2": 0.0,
                "cell_area_m2": cell_area,
                "exclusion_reasons": "",
            })
            continue

        candidates = excl_utm.iloc[candidate_idxs]
        # Clip to those that truly intersect (spatial index returns bounding-box candidates)
        truly_intersecting = candidates[candidates.geometry.intersects(cell_geom)]
        if truly_intersecting.empty:
            rows.append({
                "h3_index": cell["h3_index"],
                "developable_frac": 1.0,
                "excl_area_m2": 0.0,
                "cell_area_m2": cell_area,
                "exclusion_reasons": "",
            })
            continue

        # Union all overlapping exclusion zones, then intersect with cell
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            excl_union = unary_union(truly_intersecting.geometry.values)
            overlap = cell_geom.intersection(excl_union)

        excl_area = overlap.area if not overlap.is_empty else 0.0
        dev_frac  = max(0.0, (cell_area - excl_area) / cell_area) if cell_area > 0 else 0.0
        reasons   = "|".join(sorted(truly_intersecting["excl_label"].unique()))

        rows.append({
            "h3_index": cell["h3_index"],
            "developable_frac": round(dev_frac, 4),
            "excl_area_m2": round(excl_area, 1),
            "cell_area_m2": round(cell_area, 1),
            "exclusion_reasons": reasons,
        })

    return pd.DataFrame(rows)


# ── Public API ────────────────────────────────────────────────────────────────

def build_mask(
    h3_cells: list[str],
    city_key: str,
    bbox: list[float] | None,
    utm_crs: str = "EPSG:32612",
    cfg: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
) -> pd.DataFrame:
    """Compute the developability mask for a list of H3 resolution-8 cells.

    Parameters
    ----------
    h3_cells  : List of H3 index strings to evaluate.
    city_key  : 'phoenix' or 'austin' — selects hardcoded fallback polygons.
    bbox      : [west, south, east, north] in EPSG:4326.  Used to query APIs;
                if None the hardcoded fallbacks still apply.
    utm_crs   : Local UTM projection for accurate area arithmetic.
    cfg       : Section dict from scoring.yaml["developability_mask"].
    cache_dir : Local directory for caching downloaded geometry files.

    Returns
    -------
    DataFrame with columns:
        h3_index          — H3 index string
        developable       — bool: developable_frac >= min_developable_frac
        developable_frac  — float [0, 1]: fraction of cell area that is buildable
        exclusion_reasons — pipe-delimited labels (e.g. "protected|water")
    """
    if not _GEO_AVAILABLE:
        log.warning("developability_mask_skipped",
                    reason="geopandas/shapely not installed")
        return pd.DataFrame({
            "h3_index": h3_cells,
            "developable": True,
            "developable_frac": 1.0,
            "exclusion_reasons": "",
        })

    cfg = cfg or {}
    min_frac = float(cfg.get("min_developable_frac", 0.20))
    effective_bbox = bbox if bbox else [-180, -90, 180, 90]

    # 1. Build exclusion GeoDataFrame
    excl_gdf = _load_or_fetch_exclusions(city_key, effective_bbox, cfg, cache_dir)

    # 2. Convert H3 cells to geometry
    cells_gdf = _h3_cells_to_gdf(h3_cells)

    # 3. If no exclusion zones loaded, all cells are developable
    if excl_gdf.empty:
        log.warning("no_exclusion_zones_loaded", city_key=city_key)
        return pd.DataFrame({
            "h3_index": h3_cells,
            "developable": True,
            "developable_frac": 1.0,
            "exclusion_reasons": "",
        })

    # 4. Compute developable fractions (UTM-projected area intersection)
    fracs_df = _compute_developable_fracs(cells_gdf, excl_gdf, utm_crs)

    # 5. Apply threshold
    fracs_df["developable"] = fracs_df["developable_frac"] >= min_frac

    n_removed = int((~fracs_df["developable"]).sum())
    if n_removed > 0:
        reason_counts = (
            fracs_df.loc[~fracs_df["developable"], "exclusion_reasons"]
            .value_counts()
            .to_dict()
        )
        log.info("mask_applied",
                 city_key=city_key,
                 total=len(fracs_df),
                 removed=n_removed,
                 reason_counts=reason_counts)

    return fracs_df[["h3_index", "developable", "developable_frac", "exclusion_reasons"]]
