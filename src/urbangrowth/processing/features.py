"""Final H3 feature table assembly.

Joins per-cell land cover, transitions, static layers, permit density,
zoning entropy, and census demographics into a single panel keyed on
(h3_index, city_id, date).

Outputs per run:
  DB  : h3_features table  — core columns (built_pct, veg_pct,
        transitions_jsonb, mean_elevation, mean_slope, permit_count,
        permit_valuation, road_density)
  Disk: processed/features/{city}_h3_features.parquet — full feature set
        (all 9 class %, spectral indices, multi-lag transitions, static,
        permits, zoning entropy, census)

CLI: ug features build --city phoenix --start 2018-01 --end 2025-12
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import rasterio
import sqlalchemy as sa
import structlog
from dotenv import load_dotenv
from pyproj import Transformer

from urbangrowth.config import data_path, get_cities, get_pipeline
from urbangrowth.db import loaders
from urbangrowth.processing.change import DEFAULT_LAGS, get_transitions_for_date
from urbangrowth.processing.h3_aggregate import (
    _iter_months,
    aggregate_landcover_to_h3,
    get_city_h3_cells,
    get_h3_mapping,
)

load_dotenv()
log = structlog.get_logger(__name__)

try:
    import h3
    def _cell_to_latlng(cell):
        try:
            return h3.cell_to_latlng(cell)
        except AttributeError:
            return h3.h3_to_geo(cell)
except ImportError:
    pass

_CITY_IDS: dict[str, int] = {"phoenix": 1, "austin": 2}

# Phoenix / Austin CBD coordinates (WGS84 lat, lon)
_CBD: dict[str, tuple[float, float]] = {
    "phoenix": (33.4484, -112.0740),
    "austin":  (30.2672, -97.7431),
}


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------


def _haversine_km(lat1: np.ndarray, lon1: np.ndarray, lat2: float, lon2: float) -> np.ndarray:
    R = 6371.0
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a    = np.sin(dlat / 2)**2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon / 2)**2
    return R * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


# ---------------------------------------------------------------------------
# Static feature cache (computed once per city, reused each month)
# ---------------------------------------------------------------------------


def _static_cache_path(city: str) -> Path:
    return data_path("processed", "h3_cache") / f"{city}_static.parquet"


def _build_static_features(city: str, resolution: int = 8) -> pd.DataFrame:
    """Compute static per-cell features: elevation, slope, CBD distance,
    highway distance.  Expensive but called once and cached."""
    pipe = get_pipeline()
    cities = get_cities()

    h3_cells = get_city_h3_cells(city, resolution)
    latlngs  = np.array([_cell_to_latlng(c) for c in h3_cells])
    lats, lons = latlngs[:, 0], latlngs[:, 1]

    df = pd.DataFrame({"h3_index": h3_cells, "lat": lats, "lon": lons})

    # ── Distance to CBD ───────────────────────────────────────────────────
    cbd_lat, cbd_lon = _CBD.get(city, (lats.mean(), lons.mean()))
    df["dist_cbd_km"] = _haversine_km(lats, lons, cbd_lat, cbd_lon).round(3)

    # ── Elevation and slope ───────────────────────────────────────────────
    # Get first available composite to derive raster transform/CRS
    cog_dir = data_path(pipe["processed_data_subdirs"]["composites"], city)
    all_cogs = sorted(cog_dir.glob("*.tif"))

    if all_cogs:
        try:
            cell_ids, _ = get_h3_mapping(city, resolution, composite_path=all_cogs[0])
            n_cells = len(h3_cells)

            elev_dir = data_path(pipe["raw_data_subdirs"]["elevation"], city)
            for fname, col in [("elevation.tif", "mean_elevation"), ("slope.tif", "mean_slope")]:
                tif = elev_dir / fname
                if tif.exists():
                    with rasterio.open(tif) as src:
                        # Resample to composite grid size if needed
                        arr = src.read(
                            1,
                            out_shape=(cell_ids.shape[0], cell_ids.shape[1]),
                            resampling=rasterio.enums.Resampling.bilinear,
                        ).astype(np.float32)

                    from urbangrowth.processing.h3_aggregate import _agg_float
                    means = _agg_float(arr, cell_ids, n_cells)
                    df[col] = means.round(3)
                else:
                    df[col] = np.nan
        except Exception as exc:
            log.warning("elevation_agg_failed", city=city, error=str(exc))
            df["mean_elevation"] = np.nan
            df["mean_slope"]     = np.nan
    else:
        df["mean_elevation"] = np.nan
        df["mean_slope"]     = np.nan

    # ── Distance to nearest highway (OSM roads) ───────────────────────────
    try:
        import geopandas as gpd
        from shapely.geometry import Point

        osm_gpkg = data_path(pipe["raw_data_subdirs"]["osm"]) / city / f"{city}_osm.gpkg"
        if osm_gpkg.exists():
            roads = gpd.read_file(osm_gpkg, layer="roads")
            # Filter to highway types
            hwy_types = {"motorway", "trunk", "primary", "secondary"}
            mask = roads["highway"].isin(hwy_types) if "highway" in roads.columns else slice(None)
            hwys = roads[mask] if isinstance(mask, np.ndarray) else roads

            utm_crs   = cities[city]["utm_crs"]
            tfm_fwd   = Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True)
            tfm_back  = Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True)

            hwys_utm  = hwys.to_crs(utm_crs)
            highway_union = hwys_utm.geometry.union_all()

            cell_xs, cell_ys = tfm_fwd.transform(lons, lats)

            dist_km = np.array([
                highway_union.distance(Point(x, y)) / 1000.0
                for x, y in zip(cell_xs, cell_ys)
            ], dtype=np.float32)
            df["dist_highway_km"] = dist_km.round(3)
        else:
            df["dist_highway_km"] = np.nan
    except Exception as exc:
        log.warning("highway_dist_failed", city=city, error=str(exc))
        df["dist_highway_km"] = np.nan

    df = df.drop(columns=["lat", "lon"], errors="ignore")
    return df


def load_static_features(city: str, resolution: int = 8) -> pd.DataFrame:
    """Load or build and cache the static feature table."""
    cache = _static_cache_path(city)
    if cache.exists():
        return pd.read_parquet(cache)
    log.info("static_features_building", city=city)
    df = _build_static_features(city, resolution)
    df.to_parquet(cache, index=False)
    log.info("static_features_cached", city=city, rows=len(df), path=str(cache))
    return df


# ---------------------------------------------------------------------------
# Permit features (dynamic — rolling window from DB)
# ---------------------------------------------------------------------------


def _load_permit_features(
    city: str,
    date: str,
    h3_cells: np.ndarray,
    resolution: int = 8,
    rolling_months: int = 3,
) -> pd.DataFrame:
    """Aggregate permit count + valuation within rolling *rolling_months* window.

    Spatial assignment uses the H3 cell that contains each permit's (lat, lon).
    """
    year, month = int(date[:4]), int(date[5:7])
    total = year * 12 + month - 1
    y0, m0 = divmod(total - rolling_months, 12)
    m0 += 1
    start_date = f"{y0:04d}-{m0:02d}-01"
    end_date   = f"{year:04d}-{month:02d}-28"

    city_id = _CITY_IDS.get(city, 1)

    try:
        engine = loaders._engine()
        query  = sa.text(
            "SELECT ST_X(geometry) as lon, ST_Y(geometry) as lat, "
            "       valuation "
            "FROM city_permits "
            "WHERE city_id = :cid "
            "  AND issue_date BETWEEN :s AND :e "
            "  AND geometry IS NOT NULL"
        )
        with engine.connect() as conn:
            perm_df = pd.read_sql(query, conn, params={"cid": city_id, "s": start_date, "e": end_date})
    except Exception as exc:
        log.warning("permit_query_failed", city=city, date=date, error=str(exc))
        return pd.DataFrame({"h3_index": h3_cells, f"permit_count_{rolling_months}m": 0,
                              f"permit_valuation_{rolling_months}m": 0.0})

    if perm_df.empty:
        return pd.DataFrame({
            "h3_index": h3_cells,
            f"permit_count_{rolling_months}m": 0,
            f"permit_valuation_{rolling_months}m": 0.0,
        })

    try:
        from urbangrowth.processing.h3_aggregate import _latlng_to_cell as _ltc
        perm_df["h3_index"] = perm_df.apply(
            lambda r: _ltc(r["lat"], r["lon"], resolution), axis=1
        )
    except Exception as exc:
        log.warning("permit_h3_assign_failed", error=str(exc))
        return pd.DataFrame({
            "h3_index": h3_cells,
            f"permit_count_{rolling_months}m": 0,
            f"permit_valuation_{rolling_months}m": 0.0,
        })

    agg = perm_df.groupby("h3_index").agg(
        count=("valuation", "count"),
        valuation=("valuation", "sum"),
    ).reset_index()
    agg.columns = ["h3_index", f"permit_count_{rolling_months}m", f"permit_valuation_{rolling_months}m"]

    base = pd.DataFrame({"h3_index": h3_cells})
    return base.merge(agg, on="h3_index", how="left").fillna(
        {f"permit_count_{rolling_months}m": 0, f"permit_valuation_{rolling_months}m": 0.0}
    )


# ---------------------------------------------------------------------------
# Zoning entropy
# ---------------------------------------------------------------------------


def _load_zoning_entropy(city: str, h3_cells: np.ndarray, resolution: int = 8) -> pd.Series:
    """Compute Shannon entropy of zone types within each H3 cell."""
    try:
        import geopandas as gpd
        from shapely.geometry import Polygon

        pipe    = get_pipeline()
        zoning_dir = data_path(pipe["raw_data_subdirs"]["zoning"][city])
        gpkg = list(zoning_dir.glob("*.gpkg"))
        if not gpkg:
            raise FileNotFoundError("zoning GeoPackage not found")

        zones = gpd.read_file(gpkg[0])
        zones = zones[zones.geometry.notna()].to_crs("EPSG:4326")

        zone_col = next(
            (c for c in zones.columns if "zone" in c.lower() and "code" in c.lower()), None
        ) or zones.columns[1]

        # Build H3 cell polygons
        def _h3_poly(cell):
            try:
                boundary = h3.h3_to_geo_boundary(cell, geo_json=True)
            except AttributeError:
                boundary = h3.cell_to_boundary(cell)
                boundary = [(lon, lat) for lat, lon in boundary]
            return Polygon(boundary)

        cell_polys = gpd.GeoDataFrame(
            {"h3_index": h3_cells},
            geometry=[_h3_poly(c) for c in h3_cells],
            crs="EPSG:4326",
        )

        joined = gpd.sjoin(cell_polys, zones[[zone_col, "geometry"]], how="left", predicate="intersects")

        def _entropy(series):
            vc = series.dropna().value_counts(normalize=True)
            if vc.empty:
                return np.nan
            return float(-np.sum(vc * np.log(vc + 1e-12)))

        ent = joined.groupby("h3_index")[zone_col].apply(_entropy)
        base = pd.Series(np.nan, index=h3_cells, name="zoning_entropy")
        base.update(ent)
        return base.values

    except Exception as exc:
        log.warning("zoning_entropy_failed", city=city, error=str(exc))
        return np.full(len(h3_cells), np.nan, dtype=np.float32)


# ---------------------------------------------------------------------------
# Census features
# ---------------------------------------------------------------------------


def _load_census_features(city: str, h3_cells: np.ndarray, resolution: int = 8) -> pd.DataFrame:
    """Area-weighted block-group ACS stats aggregated to H3 cells."""
    try:
        import geopandas as gpd
        from shapely.geometry import Polygon

        pipe   = get_pipeline()
        gpkg   = data_path("raw", "census_acs", city) / "block_groups.gpkg"
        if not gpkg.exists():
            raise FileNotFoundError(f"Census GeoPackage not found: {gpkg}")

        bgs = gpd.read_file(gpkg).to_crs("EPSG:4326")

        acs_cols = [
            "total_population", "median_household_income",
            "median_home_value", "total_housing_units",
        ]
        acs_cols = [c for c in acs_cols if c in bgs.columns]

        # Build H3 cell GeoDataFrame
        def _h3_poly(cell):
            try:
                pts = h3.h3_to_geo_boundary(cell, geo_json=True)
            except AttributeError:
                pts = [(lon, lat) for lat, lon in h3.cell_to_boundary(cell)]
            return Polygon(pts)

        cell_gdf = gpd.GeoDataFrame(
            {"h3_index": h3_cells},
            geometry=[_h3_poly(c) for c in h3_cells],
            crs="EPSG:4326",
        )

        # Intersection-weighted average
        joined = gpd.overlay(cell_gdf, bgs[acs_cols + ["geometry"]], how="intersection")
        joined["area_km2"] = joined.geometry.to_crs("EPSG:3857").area / 1e6

        result = pd.DataFrame({"h3_index": h3_cells})
        for col in acs_cols:
            if col not in joined.columns:
                continue
            joined[col] = pd.to_numeric(joined[col], errors="coerce")
            agg = (
                joined.dropna(subset=[col])
                .groupby("h3_index")
                .apply(lambda g: np.average(g[col], weights=g["area_km2"]))
            ).reset_index()
            agg.columns = ["h3_index", col]
            result = result.merge(agg, on="h3_index", how="left")

        # Population density
        if "total_population" in result.columns:
            cell_area_km2 = 0.7373
            result["population_density"] = (
                result["total_population"] / cell_area_km2
            ).round(1)

        return result

    except Exception as exc:
        log.warning("census_features_failed", city=city, error=str(exc))
        return pd.DataFrame({"h3_index": h3_cells})


# ---------------------------------------------------------------------------
# Road density (from DB — already populated by osm.py)
# ---------------------------------------------------------------------------


def _load_road_density(city: str, h3_cells: np.ndarray) -> pd.Series:
    """Read road_density from h3_features table (written by osm.py)."""
    city_id = _CITY_IDS.get(city, 1)
    try:
        engine = loaders._engine()
        query  = sa.text(
            "SELECT h3_index, road_density FROM h3_features "
            "WHERE city_id = :cid AND road_density IS NOT NULL "
            "ORDER BY date DESC"
        )
        with engine.connect() as conn:
            df = pd.read_sql(query, conn, params={"cid": city_id})

        if df.empty:
            return pd.Series(np.nan, index=h3_cells)

        rd = df.drop_duplicates("h3_index").set_index("h3_index")["road_density"]
        base = pd.Series(np.nan, index=h3_cells)
        base.update(rd)
        return base

    except Exception as exc:
        log.warning("road_density_load_failed", city=city, error=str(exc))
        return pd.Series(np.nan, index=h3_cells)


# ---------------------------------------------------------------------------
# Per-month feature assembly
# ---------------------------------------------------------------------------


def build_features_for_month(
    city: str,
    date: str,
    static_df: Optional[pd.DataFrame] = None,
    census_df: Optional[pd.DataFrame] = None,
    resolution: int = 8,
) -> pd.DataFrame:
    """Assemble the full H3 feature row for *city* at *date*.

    Pass pre-loaded *static_df* and *census_df* to avoid re-loading each month.
    """
    # ── Land cover + spectral indices ─────────────────────────────────────
    lc_df = aggregate_landcover_to_h3(city, date, resolution)
    if lc_df.empty:
        return pd.DataFrame()

    df = lc_df.copy()

    # ── Transitions at 1m, 3m, 12m lags ──────────────────────────────────
    lag_dfs = get_transitions_for_date(city, date, lags=DEFAULT_LAGS, resolution=resolution)
    for lag, tdf in lag_dfs.items():
        if tdf.empty:
            continue
        rename = {
            c: f"{c}_lag{lag}m"
            for c in tdf.columns
            if c not in ("h3_index", "lag_months")
        }
        tdf = tdf.drop(columns=["lag_months"]).rename(columns=rename)
        df = df.merge(tdf, on="h3_index", how="left")

    # ── Static features ───────────────────────────────────────────────────
    if static_df is None:
        static_df = load_static_features(city, resolution)
    df = df.merge(static_df, on="h3_index", how="left")

    # ── Road density ──────────────────────────────────────────────────────
    h3_cells = df["h3_index"].values
    df["road_density"] = _load_road_density(city, h3_cells).values

    # ── Permits (3m and 12m rolling) ──────────────────────────────────────
    for window in (3, 12):
        perm_df = _load_permit_features(city, date, h3_cells, resolution, window)
        df = df.merge(perm_df, on="h3_index", how="left")

    # ── Zoning entropy ────────────────────────────────────────────────────
    df["zoning_entropy"] = _load_zoning_entropy(city, h3_cells, resolution)

    # ── Census ────────────────────────────────────────────────────────────
    if census_df is None:
        census_df = _load_census_features(city, h3_cells, resolution)
    if not census_df.empty:
        df = df.merge(census_df, on="h3_index", how="left")

    df["city_id"] = _CITY_IDS.get(city, 1)
    df["date"]    = date

    return df


# ---------------------------------------------------------------------------
# DB upsert helpers
# ---------------------------------------------------------------------------


def _upsert_to_db(df: pd.DataFrame) -> int:
    """Write core columns to the h3_features table.

    Columns that exist in h3_features schema: built_pct, veg_pct,
    transitions_jsonb, mean_elevation, mean_slope, road_density,
    permit_count, permit_valuation.
    """
    if df.empty:
        return 0

    db_cols = {
        "h3_index": "h3_index",
        "city_id":  "city_id",
        "date":     "date",
        "built_pct":  "built_pct",
        "veg_pct":    "veg_pct",
        "mean_elevation": "mean_elevation",
        "mean_slope":     "mean_slope",
        "road_density":   "road_density",
    }

    # Map permit columns
    perm_count_col = "permit_count_3m"
    perm_val_col   = "permit_valuation_3m"

    rows = []
    for _, row in df.iterrows():
        d: dict = {}
        for src, dst in db_cols.items():
            d[dst] = row.get(src)

        # transitions_jsonb — prefer lag1m, fall back to lag3m
        jsonb_src = "transitions_jsonb_lag1m" if "transitions_jsonb_lag1m" in df.columns \
                    else "transitions_jsonb_lag3m" if "transitions_jsonb_lag3m" in df.columns \
                    else None
        d["transitions_jsonb"] = row.get(jsonb_src, "{}") if jsonb_src else "{}"

        d["permit_count"]     = row.get(perm_count_col, 0)
        d["permit_valuation"] = row.get(perm_val_col,   0.0)
        rows.append(d)

    upsert_df = pd.DataFrame(rows)
    upsert_df["date"] = pd.to_datetime(upsert_df["date"] + "-01")

    return loaders.upsert_df(upsert_df, "h3_features", pk_cols=["h3_index", "city_id", "date"])


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------


def run(
    city: str = "phoenix",
    start: str = "2018-01",
    end: str = "2025-12",
    resolution: int = 8,
) -> None:
    """Build the full H3 feature panel for *city* over [start, end].

    Each month is resumable via a flag file; final full-panel parquet is
    saved at the end.
    """
    pipe    = get_pipeline()
    out_dir = data_path(pipe["processed_data_subdirs"]["feature_tables"])

    months = list(_iter_months(start, end))
    log.info("features_run_start", city=city, start=start, end=end, n_months=len(months))

    # Pre-load per-city static / census tables (expensive once; cheap to pass each month)
    log.info("loading_static_features", city=city)
    static_df = load_static_features(city, resolution)

    h3_cells  = get_city_h3_cells(city, resolution)
    log.info("loading_census_features", city=city)
    census_df = _load_census_features(city, h3_cells, resolution)

    all_frames: list[pd.DataFrame] = []
    done = skipped = missing = 0

    # Month-level cache for resumability
    month_dir = data_path(pipe["processed_data_subdirs"]["h3_features"], city, "months")

    for year, month in months:
        date      = f"{year:04d}-{month:02d}"
        month_out = month_dir / f"{date}.parquet"

        if month_out.exists():
            all_frames.append(pd.read_parquet(month_out))
            skipped += 1
            continue

        df = build_features_for_month(
            city, date,
            static_df=static_df,
            census_df=census_df,
            resolution=resolution,
        )
        if df.empty:
            missing += 1
            continue

        df.to_parquet(month_out, index=False)
        all_frames.append(df)

        try:
            _upsert_to_db(df)
        except Exception as exc:
            log.warning("db_upsert_failed", date=date, error=str(exc))

        done += 1

    # Assemble full panel parquet
    if all_frames:
        panel = pd.concat(all_frames, ignore_index=True)
        panel_path = out_dir / f"{city}_h3_features.parquet"
        panel.to_parquet(panel_path, index=False)
        log.info(
            "features_panel_saved",
            city=city, rows=len(panel), cols=len(panel.columns),
            path=str(panel_path),
        )

    log.info(
        "features_run_complete",
        city=city, total=len(months),
        done=done, skipped=skipped, missing=missing,
    )
