"""Cloud-masked monthly Sentinel-2 median composites via Planetary Computer COGs.

For each city × month the pipeline:
  1. Queries PC STAC for S2-L2A scenes with cloud cover < 60 %.
  2. Builds a lazy Dask stack (time × band × y × x) in UTM via stackstac.
  3. Masks SCL classes 3 / 8 / 9 / 10 / 11 (cloud shadow, cloud, cirrus, snow).
  4. Computes pixel-wise median across the time dimension.
  5. Derives NDVI, NDBI, NDWI spectral indices.
  6. Writes a 9-band Cloud-Optimised GeoTIFF.
  7. Upserts a row into the sentinel_scenes DB table.

Output path: {data_root}/processed/composites/{city}/{YYYY-MM}.tif
Band order:  B02, B03, B04, B08, B11, B12, NDVI, NDBI, NDWI

Performance targets
  Single month Phoenix composite:  < 5 min
  Full Phoenix backfill 2018-2025: < 12 h
  Storage per city full archive:   80-150 GB

CLI: ug composite generate --city phoenix --start 2018-01 --end 2025-12
"""
from __future__ import annotations

import calendar
import datetime
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import rioxarray  # noqa: F401 — registers .rio accessor on xr.DataArray
import structlog
import xarray as xr
from dotenv import load_dotenv

from urbangrowth.config import data_path, get_cities, get_pipeline
from urbangrowth.data.sentinel2 import SCL_BAND, SPECTRAL_BANDS, load_stack, query_scenes
from urbangrowth.db import loaders

load_dotenv()
log = structlog.get_logger(__name__)

# SCL class integers to treat as cloud / shadow / invalid
_SCL_BAD = [3, 8, 9, 10, 11]

# city_id values matching the cities table seed in schema.sql
_CITY_IDS: dict[str, int] = {"phoenix": 1, "austin": 2}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _iter_months(start: str, end: str):
    """Yield (year, month) for every month in [start, end] (YYYY-MM strings)."""
    sy, sm = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    year, month = sy, sm
    while (year, month) <= (ey, em):
        yield year, month
        month += 1
        if month > 12:
            month = 1
            year += 1


def _out_path(city: str, year: int, month: int) -> Path:
    pipe = get_pipeline()
    out_dir = data_path(pipe["processed_data_subdirs"]["composites"], city)
    return out_dir / f"{year:04d}-{month:02d}.tif"


def _build_scl_mask(stack: xr.DataArray) -> xr.DataArray:
    """Return a boolean (time, y, x) mask — True where the pixel is bad.

    Compares the SCL band against _SCL_BAD class values.
    """
    scl = stack.sel(band=SCL_BAND).astype("float32")
    mask = xr.zeros_like(scl, dtype=bool)
    for cls in _SCL_BAD:
        mask = mask | (scl == float(cls))
    return mask


def _compute_indices(composite: xr.DataArray) -> xr.DataArray:
    """Append NDVI, NDBI, NDWI bands to a (band, y, x) spectral composite.

    Returns a (9, y, x) DataArray: B02 B03 B04 B08 B11 B12 NDVI NDBI NDWI.
    Division-by-zero is guarded with epsilon; output is float32.
    """
    eps = 1.0
    B04 = composite.sel(band="B04").astype("float32")
    B08 = composite.sel(band="B08").astype("float32")
    B03 = composite.sel(band="B03").astype("float32")
    B11 = composite.sel(band="B11").astype("float32")

    ndvi = (B08 - B04) / (B08 + B04 + eps)
    ndbi = (B11 - B08) / (B11 + B08 + eps)
    ndwi = (B03 - B08) / (B03 + B08 + eps)

    def _as_band(arr: xr.DataArray, name: str) -> xr.DataArray:
        return arr.expand_dims({"band": [name]})

    return xr.concat(
        [composite.astype("float32")]
        + [_as_band(ndvi, "NDVI"), _as_band(ndbi, "NDBI"), _as_band(ndwi, "NDWI")],
        dim="band",
    )


def _write_cog(data: xr.DataArray, path: Path) -> None:
    """Write a (band, y, x) float32 DataArray as a Cloud-Optimised GeoTIFF."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        data.astype("float32").rio.to_raster(
            str(path),
            driver="COG",
            compress="deflate",
            dtype="float32",
            windowed=False,
        )
    log.info("cog_written", path=str(path), shape=list(data.shape))


def _register_composite(
    city: str,
    year: int,
    month: int,
    cloud_pct: float,
    path: Path,
) -> None:
    """Upsert a composite metadata row into the sentinel_scenes table."""
    pipe = get_pipeline()
    data_root = Path(pipe["data_root"])
    try:
        rel_path = str(path.relative_to(data_root))
    except ValueError:
        rel_path = str(path)

    row = pd.DataFrame([{
        "scene_id":  f"composite_{city}_{year:04d}_{month:02d}",
        "city_id":   _CITY_IDS.get(city, 1),
        "date":      datetime.date(year, month, 1),
        "cloud_pct": round(cloud_pct, 2),
        "file_path": rel_path,
    }])
    loaders.upsert_df(row, "sentinel_scenes", pk_cols=["scene_id"])


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------


def monthly_median_composite(
    city: str,
    year: int,
    month: int,
    max_cloud_pct: int = 60,
) -> Optional[Path]:
    """Build one monthly cloud-masked median composite for *city*.

    Idempotent: returns the existing path immediately if the output COG exists.

    Parameters
    ----------
    city:
        City key from cities.yaml.
    year, month:
        Calendar year and month (1-based).
    max_cloud_pct:
        Scene-level cloud cover ceiling used in the STAC query.

    Returns
    -------
    Path to the output COG, or ``None`` if no scenes were found.
    """
    out = _out_path(city, year, month)
    if out.exists():
        log.info("composite_exists", city=city, year=year, month=month, path=str(out))
        return out

    cities = get_cities()
    if city not in cities:
        raise ValueError(f"Unknown city '{city}'. Options: {list(cities)}")

    city_cfg = cities[city]
    bbox_4326: list[float] = city_cfg["bbox"]
    utm_epsg: int = int(city_cfg["utm_crs"].split(":")[1])

    # Month date range
    _, last_day = calendar.monthrange(year, month)
    start_date = f"{year:04d}-{month:02d}-01"
    end_date   = f"{year:04d}-{month:02d}-{last_day:02d}"

    # ── 1. Query scenes ───────────────────────────────────────────────────────
    items = query_scenes(city, start_date, end_date, max_cloud_pct=max_cloud_pct)
    if not items:
        log.warning("no_scenes_for_month", city=city, year=year, month=month)
        return None

    mean_cloud = float(np.mean([
        it.properties.get("eo:cloud_cover", 50.0) for it in items
    ]))

    log.info(
        "composite_start",
        city=city, year=year, month=month,
        n_scenes=len(items), mean_cloud_pct=round(mean_cloud, 1),
    )

    # ── 2. Lazy stack (spectral + SCL) ────────────────────────────────────────
    stack = load_stack(
        items,
        SPECTRAL_BANDS + [SCL_BAND],
        bbox_4326,
        utm_epsg,
        resolution=10,
        chunksize=2048,
    )

    # ── 3. Cloud mask ─────────────────────────────────────────────────────────
    cloud_mask = _build_scl_mask(stack)              # (time, y, x) bool
    spec = stack.sel(band=SPECTRAL_BANDS).where(~cloud_mask)

    # ── 4. Coverage estimate — cheap scalar before the heavy compute ──────────
    valid_frac = float((~cloud_mask).mean().compute())
    coverage_pct = round(valid_frac * 100, 1)
    log.info(
        "coverage_estimate",
        city=city, year=year, month=month,
        coverage_pct=coverage_pct,
    )

    # ── 5. Pixel-wise median across time ──────────────────────────────────────
    composite = spec.median(dim="time", skipna=True)

    # ── 6. Spectral indices ───────────────────────────────────────────────────
    final = _compute_indices(composite)
    final = final.rio.write_crs(f"EPSG:{utm_epsg}")

    # ── 7. Compute + write COG ────────────────────────────────────────────────
    log.info("computing_composite", city=city, year=year, month=month)
    final_computed = final.compute()
    _write_cog(final_computed, out)

    # ── 8. Register in sentinel_scenes ───────────────────────────────────────
    _register_composite(city, year, month, mean_cloud, out)

    log.info(
        "composite_complete",
        city=city, year=year, month=month,
        path=str(out),
        coverage_pct=coverage_pct,
        size_mb=round(out.stat().st_size / 1e6, 1),
    )
    return out


# ---------------------------------------------------------------------------
# Batch runner (resumable)
# ---------------------------------------------------------------------------


def run(
    city: str = "phoenix",
    start: str = "2018-01",
    end: str = "2025-12",
    max_cloud_pct: int = 60,
) -> None:
    """Build all missing monthly composites for *city* in [start, end].

    Resumable: months whose output COG already exists are skipped without
    re-querying the STAC API or re-downloading any data.
    """
    months = list(_iter_months(start, end))
    log.info(
        "composite_run_start",
        city=city, start=start, end=end, n_months=len(months),
    )

    done = skipped = missing = 0
    for year, month in months:
        if _out_path(city, year, month).exists():
            skipped += 1
            continue
        result = monthly_median_composite(city, year, month, max_cloud_pct=max_cloud_pct)
        if result is None:
            missing += 1
        else:
            done += 1

    log.info(
        "composite_run_complete",
        city=city, total=len(months),
        done=done, skipped=skipped, no_scenes=missing,
    )
