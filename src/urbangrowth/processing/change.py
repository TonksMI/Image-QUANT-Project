"""Land cover transition computation between monthly class rasters.

For each pair of dates (t1, t2), computes a per-H3-cell transition matrix
using a single np.bincount call — O(H × W), no Python loops over cells.

Key transition pairs stored per output row:
  veg_to_built_pct   — vegetation pixels that became built
  bare_to_built_pct  — bare pixels that became built
  built_stable_pct   — built pixels that remained built (persistence)
  new_built_pct      — all new-built pixels / cell total (veg+bare+other → built)
  built_loss_pct     — built pixels lost to any other class
  transitions_jsonb  — full 9×9 matrix serialised for DB (key transitions only)

Output per (city, date) covering lags 1m / 3m / 12m:
  processed/h3_features/{city}/{YYYY-MM}_trans.parquet
  Columns: h3_index, lag_months, + transition columns above
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import rasterio
import structlog
from dotenv import load_dotenv

from urbangrowth.config import data_path, get_pipeline
from urbangrowth.processing.h3_aggregate import _iter_months, get_h3_mapping
from urbangrowth.processing.segmentation import CLASSES, N_CLASSES

load_dotenv()
log = structlog.get_logger(__name__)

_BUILT_IDX = CLASSES.index("built")           # 6
_VEG_IDX   = [CLASSES.index(c) for c in ("trees", "grass", "flooded_veg", "crops", "shrub_scrub")]
_BARE_IDX  = CLASSES.index("bare")            # 7

# Canonical lags (months) to compute and store
DEFAULT_LAGS = (1, 3, 12)


# ---------------------------------------------------------------------------
# Core transition kernel
# ---------------------------------------------------------------------------


def _load_class_raster(path: Path) -> Optional[np.ndarray]:
    """Load a uint8 class raster; return None if path is missing."""
    if not path.exists():
        return None
    with rasterio.open(path) as src:
        return src.read(1)


def compute_h3_transitions(
    class_t1: np.ndarray,
    class_t2: np.ndarray,
    cell_ids: np.ndarray,
    n_cells: int,
) -> np.ndarray:
    """Compute full (n_cells, N_CLASSES, N_CLASSES) fraction transition tensor.

    The packed-index trick:
        combined = cell_id * N² + from_class * N + to_class
    allows computing all cell × from × to counts with a single np.bincount call.

    Returns
    -------
    fracs : (n_cells, N_CLASSES, N_CLASSES) float32
        fracs[c, i, j] = fraction of cell c's t1-class-i pixels that became j at t2.
        Rows normalised by cell pixel count (not by from-class count).
        NaN for cells with zero valid pixels.
    """
    N = N_CLASSES
    valid = (class_t1 < N) & (class_t2 < N)
    flat_ids = cell_ids.ravel()[valid.ravel()].astype(np.int64)
    flat_t1  = class_t1.ravel()[valid.ravel()].astype(np.int64)
    flat_t2  = class_t2.ravel()[valid.ravel()].astype(np.int64)

    combined = flat_ids * (N * N) + flat_t1 * N + flat_t2
    counts   = np.bincount(combined, minlength=n_cells * N * N)
    counts   = counts.reshape(n_cells, N, N).astype(np.float32)

    pixel_count = counts.sum(axis=(1, 2))
    safe        = np.where(pixel_count > 0, pixel_count, 1.0)
    fracs       = counts / safe[:, np.newaxis, np.newaxis]
    fracs[pixel_count == 0] = np.nan
    return fracs


def _transitions_to_df(fracs: np.ndarray, h3_cells: np.ndarray, lag: int) -> pd.DataFrame:
    """Flatten key transitions from the (n_cells, N, N) tensor into a DataFrame."""
    n_cells = len(h3_cells)
    B = _BUILT_IDX

    # From any vegetation class to built
    veg_to_built = fracs[:, _VEG_IDX, B].sum(axis=1)
    # From bare to built
    bare_to_built = fracs[:, _BARE_IDX, B]
    # Built that stayed built
    built_stable  = fracs[:, B, B]
    # Any non-built pixel that became built
    non_built_idx = [i for i in range(N_CLASSES) if i != B]
    new_built     = fracs[:, non_built_idx, B].sum(axis=1)
    # Built that transitioned to something else
    built_loss    = fracs[:, B, non_built_idx].sum(axis=1)

    # Compact JSONB dict of key transitions for DB storage
    def _make_jsonb(i: int) -> str:
        if np.isnan(fracs[i]).all():
            return "{}"
        return json.dumps({
            "veg_to_built":  round(float(veg_to_built[i]),  6),
            "bare_to_built": round(float(bare_to_built[i]), 6),
            "built_stable":  round(float(built_stable[i]),  6),
            "new_built":     round(float(new_built[i]),      6),
            "built_loss":    round(float(built_loss[i]),     6),
        })

    df = pd.DataFrame({
        "h3_index":           h3_cells,
        "lag_months":         lag,
        "veg_to_built_pct":   veg_to_built.astype(np.float32),
        "bare_to_built_pct":  bare_to_built.astype(np.float32),
        "built_stable_pct":   built_stable.astype(np.float32),
        "new_built_pct":      new_built.astype(np.float32),
        "built_loss_pct":     built_loss.astype(np.float32),
        "transitions_jsonb":  [_make_jsonb(i) for i in range(n_cells)],
    })
    return df


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def compute_transitions(
    city: str,
    date_t1: str,
    date_t2: str,
    resolution: int = 8,
) -> pd.DataFrame:
    """Compute per-H3-cell land cover transitions between *date_t1* and *date_t2*.

    Parameters
    ----------
    city:
        City key from cities.yaml.
    date_t1, date_t2:
        Month strings ``"YYYY-MM"`` (t1 is the earlier date).
    resolution:
        H3 resolution.

    Returns
    -------
    DataFrame with transition percentages per cell, or empty if inputs missing.
    """
    pipe    = get_pipeline()
    lc_dir  = data_path(pipe["processed_data_subdirs"]["land_cover"], city)
    cog_dir = data_path(pipe["processed_data_subdirs"]["composites"], city)

    path_t1 = lc_dir / f"{date_t1}_class.tif"
    path_t2 = lc_dir / f"{date_t2}_class.tif"

    class_t1 = _load_class_raster(path_t1)
    class_t2 = _load_class_raster(path_t2)

    if class_t1 is None or class_t2 is None:
        log.debug("transitions_inputs_missing", city=city, t1=date_t1, t2=date_t2)
        return pd.DataFrame()

    comp_path = cog_dir / f"{date_t2}.tif"
    if not comp_path.exists():
        comp_path = cog_dir / f"{date_t1}.tif"

    cell_ids, h3_cells = get_h3_mapping(city, resolution, composite_path=comp_path)
    n_cells = len(h3_cells)

    # Align shapes: both rasters should be identical but guard against edge cases
    if class_t1.shape != class_t2.shape:
        log.warning(
            "shape_mismatch",
            t1=class_t1.shape, t2=class_t2.shape,
            city=city, date_t1=date_t1, date_t2=date_t2,
        )
        return pd.DataFrame()

    fracs = compute_h3_transitions(class_t1, class_t2, cell_ids, n_cells)

    # Lag in months (approximate)
    y1, m1 = int(date_t1[:4]), int(date_t1[5:7])
    y2, m2 = int(date_t2[:4]), int(date_t2[5:7])
    lag = (y2 - y1) * 12 + (m2 - m1)

    return _transitions_to_df(fracs, h3_cells, lag)


def get_transitions_for_date(
    city: str,
    date: str,
    lags: tuple[int, ...] = DEFAULT_LAGS,
    resolution: int = 8,
) -> dict[int, pd.DataFrame]:
    """Return transition DataFrames for *date* at each requested lag.

    For lag L, computes transitions between (date - L months) and *date*.
    Returns a dict {lag: DataFrame}.  Missing lag months yield empty DataFrames.
    """
    result: dict[int, pd.DataFrame] = {}
    year, month = int(date[:4]), int(date[5:7])

    for lag in lags:
        # Earlier month
        total = year * 12 + month - 1 - lag
        y0, m0 = divmod(total, 12)
        m0 += 1
        date_t0 = f"{y0:04d}-{m0:02d}"
        df = compute_transitions(city, date_t0, date, resolution=resolution)
        result[lag] = df

    return result


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------


def run(
    city: str = "phoenix",
    start: str = "2018-01",
    end: str = "2025-12",
    lags: tuple[int, ...] = DEFAULT_LAGS,
    resolution: int = 8,
) -> None:
    """Compute and cache transition parquets for all months in [start, end]."""
    pipe    = get_pipeline()
    out_dir = data_path(pipe["processed_data_subdirs"]["h3_features"], city)

    log.info("change_run_start", city=city, start=start, end=end, lags=lags)
    done = skipped = missing = 0

    for year, month in _iter_months(start, end):
        date = f"{year:04d}-{month:02d}"
        out_path = out_dir / f"{date}_trans.parquet"

        if out_path.exists():
            skipped += 1
            continue

        lag_dfs = get_transitions_for_date(city, date, lags, resolution)
        frames = [df for df in lag_dfs.values() if not df.empty]

        if not frames:
            missing += 1
            continue

        pd.concat(frames, ignore_index=True).to_parquet(out_path, index=False)
        done += 1

    log.info(
        "change_run_complete",
        city=city, done=done, skipped=skipped, missing=missing,
    )
