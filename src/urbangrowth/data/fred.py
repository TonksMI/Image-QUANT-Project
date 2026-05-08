"""FRED macro and construction series ingestion.

Uses fredapi to pull all series defined in a YAML series manifest.
Produces a wide parquet file (one column per series_id, month-end DatetimeIndex)
and upserts long-format rows into the fred_series PostgreSQL table.

Idempotent: loads any existing parquet, then only fetches series/date ranges
that are genuinely missing before merging and writing back.

CLI usage (via `ug ingest fred`):
    ug ingest fred --series-config config/fred_series.yaml
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pandas as pd
import structlog
import yaml
from dotenv import load_dotenv
from fredapi import Fred
from tenacity import retry, stop_after_attempt, wait_exponential

from urbangrowth.config import data_path, get_pipeline
from urbangrowth.db.loaders import upsert_df

load_dotenv()

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent  # repo root


def _fred_client() -> Fred:
    key = os.environ.get("FRED_API_KEY", "")
    if not key:
        log.warning("FRED_API_KEY not set — requests will fail; add it to .env")
    return Fred(api_key=key)


def _load_series_config(series_config_path: str) -> list[dict]:
    """Load the YAML manifest and return a flat list of series dicts."""
    path = Path(series_config_path)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    entries: list[dict] = []
    for _group_name, items in cfg["fred_series"].items():
        entries.extend(items)
    return entries


# ---------------------------------------------------------------------------
# Core fetch / alignment
# ---------------------------------------------------------------------------

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def fetch_series(series_id: str, start: str, end: str) -> pd.Series:
    """Fetch a single FRED series from the API.

    Returns a pd.Series with a DatetimeIndex at the native FRED frequency.
    Raises on HTTP errors (tenacity will retry up to 3 times).
    """
    fred = _fred_client()
    s = fred.get_series(series_id, observation_start=start, observation_end=end)
    s.index = pd.to_datetime(s.index)
    s.name = series_id
    log.info(
        "fetched_series",
        series_id=series_id,
        date_range=f"{s.index.min().date()} – {s.index.max().date()}" if len(s) else "empty",
        observation_count=len(s),
    )
    return s


def align_to_month_end(series: pd.Series, freq: str) -> pd.Series:
    """Resample a raw FRED series to calendar month-end timestamps.

    Rules
    -----
    M  (monthly)  : already monthly; convert index to month-end, ffill any gaps.
    W  (weekly)   : resample to month-end using the mean of observations in that
                    month (used for MORTGAGE30US rate averages).
    D  (daily)    : resample to month-end using the last observation in that
                    month (used for T10Y2Y and DGS10 yield levels).
    """
    freq = freq.upper()
    if freq == "M":
        # Snap each observation to its month-end timestamp then ffill gaps
        s = series.copy()
        s.index = s.index + pd.offsets.MonthEnd(0)
        # Reindex to a complete month-end range so ffill has a target grid
        if len(s) == 0:
            return s
        full_idx = pd.date_range(s.index.min(), s.index.max(), freq="ME")
        s = s.reindex(full_idx).ffill()
        s.name = series.name
        return s

    elif freq == "W":
        s = series.resample("ME").mean()
        s.name = series.name
        return s

    elif freq == "D":
        s = series.resample("ME").last()
        s.name = series.name
        return s

    else:
        raise ValueError(f"Unknown frequency '{freq}' — expected M, W, or D")


# ---------------------------------------------------------------------------
# Bulk ingest
# ---------------------------------------------------------------------------

def ingest_all(
    series_config_path: str,
    start: str,
    end: str,
) -> pd.DataFrame:
    """Fetch every series in the manifest and return a wide month-end DataFrame.

    Idempotency: loads the existing parquet (if present) and only calls the
    FRED API for series/date ranges not already in the file.

    Parameters
    ----------
    series_config_path : path to fred_series.yaml (absolute or relative to repo root)
    start              : ISO date string, e.g. "2010-01-01"
    end                : ISO date string, e.g. "2026-05-01"

    Returns
    -------
    Wide DataFrame with a DatetimeIndex (month-end) and one column per series_id.
    """
    entries = _load_series_config(series_config_path)
    pipe = get_pipeline()
    out_path = data_path(pipe["raw_data_subdirs"]["fred"]) / "fred_series_wide.parquet"

    # Load existing data for idempotency
    existing: pd.DataFrame = pd.DataFrame()
    if out_path.exists():
        try:
            existing = pd.read_parquet(out_path)
            log.info("loaded_existing_parquet", path=str(out_path), shape=existing.shape)
        except Exception as exc:
            log.warning("failed_to_load_existing_parquet", path=str(out_path), error=str(exc))

    end_ts = pd.Timestamp(end)
    start_ts = pd.Timestamp(start)
    full_month_index = pd.date_range(start_ts, end_ts, freq="ME")

    new_columns: dict[str, pd.Series] = {}

    for entry in entries:
        sid: str = entry["series_id"]
        freq: str = entry["frequency"]

        # Determine what date range is actually needed
        fetch_start = start
        fetch_end = end

        if sid in existing.columns:
            col = existing[sid].dropna()
            if len(col) > 0:
                existing_max = col.index.max()
                existing_min = col.index.min()

                # Skip entirely if we already cover the full requested range
                if existing_min <= full_month_index.min() and existing_max >= full_month_index.max():
                    log.info(
                        "series_already_current",
                        series_id=sid,
                        existing_max=str(existing_max.date()),
                    )
                    new_columns[sid] = col.reindex(full_month_index)
                    continue

                # Only fetch the portion beyond what we have
                if existing_max < end_ts:
                    fetch_start = (existing_max + pd.offsets.Day(1)).strftime("%Y-%m-%d")
                    log.info(
                        "incremental_fetch",
                        series_id=sid,
                        from_date=fetch_start,
                        to_date=fetch_end,
                    )

        try:
            raw = fetch_series(sid, fetch_start, fetch_end)
            aligned = align_to_month_end(raw, freq)
            aligned = aligned.reindex(full_month_index)

            # Merge with any pre-existing data for this column
            if sid in existing.columns:
                prior = existing[sid].reindex(full_month_index)
                # New data takes precedence; fill gaps from prior
                merged = aligned.combine_first(prior)
            else:
                merged = aligned

            new_columns[sid] = merged

        except Exception as exc:
            log.error(
                "fetch_failed",
                series_id=sid,
                error=str(exc),
            )
            # Fall back to existing data if available so one failure doesn't
            # wipe an otherwise complete dataset
            if sid in existing.columns:
                new_columns[sid] = existing[sid].reindex(full_month_index)

    # Build the wide DataFrame
    if new_columns:
        wide = pd.DataFrame(new_columns, index=full_month_index)
    elif not existing.empty:
        wide = existing.reindex(full_month_index)
    else:
        wide = pd.DataFrame(index=full_month_index)

    wide.index.name = "date"
    return wide


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def run(
    series_config: str = "config/fred_series.yaml",
    start: str = "2010-01-01",
    end: Optional[str] = None,
) -> None:
    """Fetch all FRED series, write parquet, and upsert to fred_series table.

    Parameters
    ----------
    series_config : path to the YAML series manifest
    start         : history start date (ISO format)
    end           : end date (ISO format); defaults to pipeline dates.end
    """
    pipe = get_pipeline()
    resolved_end: str = end or pipe["dates"]["end"]

    log.info(
        "fred_ingest_start",
        series_config=series_config,
        start=start,
        end=resolved_end,
    )

    wide = ingest_all(
        series_config_path=series_config,
        start=start,
        end=resolved_end,
    )

    # Persist wide parquet
    out_path = data_path(pipe["raw_data_subdirs"]["fred"]) / "fred_series_wide.parquet"
    wide.to_parquet(out_path)
    log.info("parquet_written", path=str(out_path), shape=wide.shape)

    # Upsert long format into PostgreSQL
    long = (
        wide.reset_index()
        .melt(id_vars="date", var_name="series_id", value_name="value")
        .dropna(subset=["value"])
    )
    # Ensure date is a plain date (not period) for Postgres DATE column
    long["date"] = pd.to_datetime(long["date"])

    rows_upserted = upsert_df(long, "fred_series", pk_cols=["series_id", "date"])

    log.info(
        "fred_ingest_complete",
        series=list(wide.columns),
        months=len(wide),
        rows_upserted=rows_upserted,
        out=str(out_path),
    )
