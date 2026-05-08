"""Raster → H3 hex grid aggregation for land cover and spectral indices.

Converts monthly class rasters + composites to per-H3 feature DataFrames at
resolution 8 (~0.74 km², ~460 m edge — neighbourhood scale).

Core design
  1. Build a pixel→cell mapping once per city (subsample 20 × 20, ~0.5 s).
     The mapping is cached as a .npz file and reused for all months.
  2. Aggregate class fractions and spectral-index means per cell using
     np.bincount — O(H × W), vectorised, no Python loops over cells.
  3. If h3ronpy is available it handles the mapping step in Rust; otherwise
     the pure-Python fallback is used.

Output (parquet per month):
  processed/h3_features/{city}/{YYYY-MM}_lc.parquet
  Columns: h3_index, water_pct, trees_pct, grass_pct, flooded_veg_pct,
           crops_pct, shrub_pct, built_pct, bare_pct, snow_pct,
           veg_pct, ndvi_mean, ndbi_mean, ndwi_mean, pixel_count, date
"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import rasterio
import structlog
from dotenv import load_dotenv
from pyproj import Transformer

from urbangrowth.config import data_path, get_cities, get_pipeline
from urbangrowth.processing.segmentation import CLASSES, N_CLASSES

load_dotenv()
log = structlog.get_logger(__name__)

try:
    import h3
except ImportError as exc:
    raise ImportError("Install h3-py: conda install -c conda-forge h3-py") from exc

# h3-py 3.x / 4.x compatibility shims
def _latlng_to_cell(lat: float, lon: float, res: int) -> str:
    try:
        return h3.latlng_to_cell(lat, lon, res)   # 4.x
    except AttributeError:
        return h3.geo_to_h3(lat, lon, res)         # 3.x

def _cell_to_latlng(cell: str) -> tuple[float, float]:
    try:
        return h3.cell_to_latlng(cell)             # 4.x
    except AttributeError:
        return h3.h3_to_geo(cell)                  # 3.x

def _polyfill_bbox(west, south, east, north, res: int) -> set[str]:
    # h3-py 4.x API
    try:
        poly = h3.LatLngPoly([
            (south, west), (south, east), (north, east), (north, west),
        ])
        return set(h3.h3shape_to_cells(poly, res))
    except AttributeError:
        pass
    # h3-py 3.x fallback
    geojson = {
        "type": "Polygon",
        "coordinates": [[
            [west, south], [east, south], [east, north],
            [west, north], [west, south],
        ]],
    }
    try:
        return set(h3.polyfill_geojson(geojson, res))
    except AttributeError:
        return set(h3.polyfill(geojson, res, geo_json_conformant=True))

# h3ronpy fast path
try:
    from h3ronpy.raster import raster_to_h3 as _h3ronpy_raster_to_h3
    _H3RONPY = True
    log.debug("h3ronpy_available")
except ImportError:
    _H3RONPY = False

# DynamicWorld composite band positions (0-based)
_BAND_NDVI = 6
_BAND_NDBI = 7
_BAND_NDWI = 8


# ---------------------------------------------------------------------------
# City H3 cell set
# ---------------------------------------------------------------------------


def get_city_h3_cells(city: str, resolution: int = 8) -> np.ndarray:
    """Return sorted array of H3 cell strings covering *city*'s bbox."""
    cities = get_cities()
    if city not in cities:
        raise ValueError(f"Unknown city '{city}'. Options: {list(cities)}")
    west, south, east, north = cities[city]["bbox"]
    cells = sorted(_polyfill_bbox(west, south, east, north, resolution))
    return np.array(cells, dtype=object)


# ---------------------------------------------------------------------------
# Pixel → H3 mapping (cached)
# ---------------------------------------------------------------------------


def _cache_path(city: str, resolution: int) -> Path:
    return data_path("processed", "h3_cache") / f"{city}_r{resolution}.npz"


def _build_mapping(
    transform: rasterio.Affine,
    crs: rasterio.CRS,
    H: int,
    W: int,
    resolution: int = 8,
    subsample: int = 20,
) -> tuple[np.ndarray, np.ndarray]:
    """Compute per-pixel H3 cell IDs for a raster grid.

    Returns
    -------
    cell_ids : (H, W) uint32 — integer cell ID for each pixel
    h3_cells : (N,) object — H3 string index for each integer ID
    """
    # Subsampled pixel grid
    r_idx = np.arange(0, H, subsample)
    c_idx = np.arange(0, W, subsample)
    H_s, W_s = len(r_idx), len(c_idx)

    c_grid, r_grid = np.meshgrid(c_idx, r_idx)
    xs, ys = rasterio.transform.xy(
        transform, r_grid.ravel(), c_grid.ravel(), offset="center"
    )

    # Reproject to WGS84
    if crs.to_epsg() != 4326:
        tfm = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        lons, lats = tfm.transform(np.array(xs), np.array(ys))
    else:
        lons, lats = np.array(xs), np.array(ys)

    log.info(
        "h3_mapping_computing",
        n_samples=len(lats),
        H=H, W=W,
        subsample=subsample,
        resolution=resolution,
    )

    # Vectorised H3 lookup
    vfunc = np.vectorize(lambda lat, lon: _latlng_to_cell(lat, lon, resolution))
    h3_sub = vfunc(lats, lons).reshape(H_s, W_s)

    # Integer IDs
    unique_h3, inverse = np.unique(h3_sub, return_inverse=True)
    cell_ids_sub = inverse.reshape(H_s, W_s).astype(np.uint32)

    # Upscale to full resolution (nearest-neighbour repeat)
    cell_ids_full = np.repeat(
        np.repeat(cell_ids_sub, subsample, axis=0), subsample, axis=1
    )[:H, :W]

    log.info(
        "h3_mapping_built",
        n_cells=len(unique_h3),
        coverage_pct=round(100 * H_s * W_s / (H * W), 1),
    )
    return cell_ids_full, unique_h3


def get_h3_mapping(
    city: str,
    resolution: int = 8,
    composite_path: Optional[Path] = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Load cached (cell_ids, h3_cells) or build from *composite_path*.

    The cache is keyed on city + resolution. Rebuild if raster dimensions
    do not match.
    """
    cache = _cache_path(city, resolution)

    if cache.exists():
        saved = np.load(cache, allow_pickle=True)
        if composite_path is not None:
            with rasterio.open(composite_path) as src:
                H, W = src.height, src.width
            if saved["cell_ids"].shape == (H, W):
                log.info("h3_mapping_cache_hit", city=city, resolution=resolution)
                return saved["cell_ids"], saved["h3_cells"]
            log.info("h3_mapping_cache_stale_rebuilding", city=city)
        else:
            return saved["cell_ids"], saved["h3_cells"]

    if composite_path is None:
        raise FileNotFoundError(
            f"H3 mapping cache not found for {city} r{resolution}. "
            "Provide composite_path to build it."
        )

    with rasterio.open(composite_path) as src:
        transform = src.transform
        crs       = src.crs
        H, W      = src.height, src.width

    cell_ids, h3_cells = _build_mapping(transform, crs, H, W, resolution)

    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache, cell_ids=cell_ids, h3_cells=h3_cells)
    log.info("h3_mapping_cached", path=str(cache))

    return cell_ids, h3_cells


# ---------------------------------------------------------------------------
# Aggregation kernels
# ---------------------------------------------------------------------------


def _agg_classes(
    class_arr: np.ndarray,
    cell_ids: np.ndarray,
    n_cells: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (n_cells, N_CLASSES) fraction array and (n_cells,) pixel_count.

    Pixels with class >= N_CLASSES are treated as nodata.
    """
    valid_mask = class_arr.ravel() < N_CLASSES
    ids_v = cell_ids.ravel()[valid_mask]
    cls_v = class_arr.ravel()[valid_mask].astype(np.intp)

    pixel_count = np.bincount(ids_v, minlength=n_cells).astype(np.float32)
    fracs = np.zeros((n_cells, N_CLASSES), dtype=np.float32)
    for c in range(N_CLASSES):
        mask_c = cls_v == c
        fracs[:, c] = np.bincount(ids_v[mask_c], minlength=n_cells)

    safe = np.where(pixel_count > 0, pixel_count, 1.0)
    fracs /= safe[:, np.newaxis]
    fracs[pixel_count == 0] = np.nan
    return fracs, pixel_count


def _agg_float(
    float_arr: np.ndarray,
    cell_ids: np.ndarray,
    n_cells: int,
) -> np.ndarray:
    """Return (n_cells,) per-cell nanmean of *float_arr*."""
    flat = float_arr.ravel().astype(np.float64)
    ids  = cell_ids.ravel()
    valid = np.isfinite(flat)

    count = np.bincount(ids[valid], minlength=n_cells).astype(np.float64)
    total = np.bincount(ids[valid], weights=flat[valid], minlength=n_cells)
    result = np.where(count > 0, total / count, np.nan).astype(np.float32)
    return result


# ---------------------------------------------------------------------------
# Per-month aggregation
# ---------------------------------------------------------------------------


def aggregate_landcover_to_h3(
    city: str,
    date: str,
    resolution: int = 8,
) -> pd.DataFrame:
    """Aggregate one month of land cover + spectral indices to H3 resolution.

    Parameters
    ----------
    city:
        City key from cities.yaml.
    date:
        Month string ``"YYYY-MM"``.
    resolution:
        H3 resolution (default 8).

    Returns
    -------
    DataFrame with one row per H3 cell. Columns: h3_index, {class}_pct ×9,
    veg_pct, ndvi_mean, ndbi_mean, ndwi_mean, pixel_count, date.
    Returns empty DataFrame if inputs are missing.
    """
    pipe = get_pipeline()
    lc_dir  = data_path(pipe["processed_data_subdirs"]["land_cover"], city)
    cog_dir = data_path(pipe["processed_data_subdirs"]["composites"], city)

    class_path = lc_dir  / f"{date}_class.tif"
    comp_path  = cog_dir / f"{date}.tif"

    if not class_path.exists():
        log.warning("class_raster_missing", city=city, date=date, path=str(class_path))
        return pd.DataFrame()
    if not comp_path.exists():
        log.warning("composite_missing", city=city, date=date, path=str(comp_path))
        return pd.DataFrame()

    # Pixel → H3 mapping
    cell_ids, h3_cells = get_h3_mapping(city, resolution, composite_path=comp_path)
    n_cells = len(h3_cells)

    # Load class raster
    with rasterio.open(class_path) as src:
        class_arr = src.read(1)

    # Load spectral bands from composite (NDVI=band7, NDBI=band8, NDWI=band9, 1-based)
    with rasterio.open(comp_path) as src:
        n_bands = src.count
        ndvi = src.read(_BAND_NDVI + 1).astype(np.float32) if n_bands > _BAND_NDVI else None
        ndbi = src.read(_BAND_NDBI + 1).astype(np.float32) if n_bands > _BAND_NDBI else None
        ndwi = src.read(_BAND_NDWI + 1).astype(np.float32) if n_bands > _BAND_NDWI else None

    # Replace ±inf from index computation with NaN
    for arr in (ndvi, ndbi, ndwi):
        if arr is not None:
            arr[~np.isfinite(arr)] = np.nan

    fracs, pixel_count = _agg_classes(class_arr, cell_ids, n_cells)

    row: dict[str, object] = {"h3_index": h3_cells.tolist()}
    for i, cls in enumerate(CLASSES):
        row[f"{cls}_pct"] = fracs[:, i].tolist()

    # Composite vegetation group
    veg_idx = [CLASSES.index(c) for c in ("trees", "grass", "flooded_veg", "crops", "shrub_scrub")]
    row["veg_pct"] = fracs[:, veg_idx].sum(axis=1).tolist()

    if ndvi is not None:
        row["ndvi_mean"] = _agg_float(ndvi, cell_ids, n_cells).tolist()
    if ndbi is not None:
        row["ndbi_mean"] = _agg_float(ndbi, cell_ids, n_cells).tolist()
    if ndwi is not None:
        row["ndwi_mean"] = _agg_float(ndwi, cell_ids, n_cells).tolist()

    row["pixel_count"] = pixel_count.tolist()

    df = pd.DataFrame(row)
    df["date"] = date
    return df


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------


def _iter_months(start: str, end: str):
    sy, sm = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    year, month = sy, sm
    while (year, month) <= (ey, em):
        yield year, month
        month += 1
        if month > 12:
            month = 1
            year += 1


def run(
    city: str = "phoenix",
    start: str = "2018-01",
    end: str = "2025-12",
    resolution: int = 8,
) -> None:
    """Aggregate all available land cover months to H3 and save parquets."""
    pipe = get_pipeline()
    out_dir = data_path(pipe["processed_data_subdirs"]["h3_features"], city)

    log.info("h3_agg_run_start", city=city, start=start, end=end, resolution=resolution)
    done = skipped = missing = 0

    for year, month in _iter_months(start, end):
        date = f"{year:04d}-{month:02d}"
        out_path = out_dir / f"{date}_lc.parquet"
        if out_path.exists():
            skipped += 1
            continue

        df = aggregate_landcover_to_h3(city, date, resolution)
        if df.empty:
            missing += 1
            continue

        df.to_parquet(out_path, index=False)
        done += 1

    log.info(
        "h3_agg_run_complete",
        city=city, done=done, skipped=skipped, missing=missing,
    )
