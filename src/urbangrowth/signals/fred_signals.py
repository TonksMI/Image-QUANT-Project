"""FRED macro construction series signals per ticker.

Features written to signal_features (source = 'fred'):
  fred_{SERIES}_yoy        — YoY percent change
  fred_{SERIES}_3m_mom     — 3-month moving average of the series (1m lag)
  fred_mortgage_level      — 30yr mortgage rate level (homebuilder / REIT signal)
  fred_mortgage_change_yoy — YoY change in mortgage rate (inverted housing signal)
  fred_spread_10y2y        — 10Y-2Y Treasury spread (cyclical proxy)

Series computed: TLNRESCONS, PNRESCONS, MNFCTRCONS, PWRCONS, HOUST
Only the sector-relevant subset of features is assigned to each ticker
(others = NaN → excluded from z-scoring cross-section).

The cross-section z-score creates the alpha signal: when power construction
is surging, electrical-infra tickers score higher than homebuilders.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import structlog
from sqlalchemy import text

from urbangrowth.config import data_path, get_pipeline
from urbangrowth.db.loaders import _engine, upsert_df
from urbangrowth.signals.ticker_mapping import (
    FRED_SECTOR_SERIES,
    all_symbols,
    sector_for_symbol,
)
from urbangrowth.signals.utils import filter_date_range, rolling_zscore, yoy_change

log = structlog.get_logger(__name__)

_SOURCE = "fred"

# Series to compute standard YoY + 3m momentum for
_CONSTRUCTION_SERIES = ["TLNRESCONS", "PNRESCONS", "MNFCTRCONS", "PWRCONS", "HOUST"]


# ---------------------------------------------------------------------------
# DB read
# ---------------------------------------------------------------------------


def _load_fred(engine) -> pd.DataFrame:
    """Load fred_series and pivot to wide (date × series_id)."""
    sql = text("""
        SELECT series_id, date, value
        FROM   fred_series
        WHERE  series_id = ANY(:ids)
        ORDER  BY series_id, date
    """)
    target_ids = _CONSTRUCTION_SERIES + ["MORTGAGE30US", "T10Y2Y"]
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"ids": target_ids}, parse_dates=["date"])
    if df.empty:
        return pd.DataFrame()
    wide = df.pivot(index="date", columns="series_id", values="value").sort_index()
    wide.index = wide.index.to_period("M")
    return wide


# ---------------------------------------------------------------------------
# Feature computation
# ---------------------------------------------------------------------------


def _compute_fred_features(wide: pd.DataFrame) -> pd.DataFrame:
    """Return a wide feature DataFrame (period index × feature_name columns)."""
    feat: dict[str, pd.Series] = {}

    for series in _CONSTRUCTION_SERIES:
        if series not in wide.columns:
            continue
        s = wide[series]
        feat[f"fred_{series}_yoy"]    = yoy_change(s)
        feat[f"fred_{series}_3m_mom"] = s.rolling(3, min_periods=2).mean().shift(1)

    if "MORTGAGE30US" in wide.columns:
        m = wide["MORTGAGE30US"]
        feat["fred_mortgage_level"]      = m
        feat["fred_mortgage_change_yoy"] = m.diff(12)

    if "T10Y2Y" in wide.columns:
        feat["fred_spread_10y2y"] = wide["T10Y2Y"]

    return pd.DataFrame(feat)


def _sector_applies(series_feat: str, sector: str) -> bool:
    """True if this FRED feature is relevant for the given sector."""
    # Determine which FRED series underlies this feature
    for fred_series, sectors in FRED_SECTOR_SERIES.items():
        if fred_series.lower() in series_feat.lower():
            return sector in sectors
    # Mortgage and spread features
    if "mortgage" in series_feat:
        return sector in FRED_SECTOR_SERIES.get("MORTGAGE30US", [])
    if "spread_10y2y" in series_feat:
        return sector in FRED_SECTOR_SERIES.get("T10Y2Y", [])
    return False


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def run(start: str = "2015-01", end: str = "2025-12") -> None:
    """Compute FRED signals and upsert to signal_features table."""
    engine  = _engine()
    symbols = all_symbols(include_controls=False)
    log.info("fred_signals_start", n_tickers=len(symbols), start=start, end=end)

    try:
        wide = _load_fred(engine)
    except Exception as exc:
        log.warning("fred_signals_no_data", error=str(exc))
        return

    if wide is None or wide.empty:
        log.warning("fred_signals_no_data", reason="fred_series table empty")
        return

    feat_wide = _compute_fred_features(wide)
    # Apply rolling time-series z-score per feature before broadcasting.
    # Cross-section z-score would produce NaN because all same-sector tickers
    # share identical macro values (std=0 within each period).
    for col in feat_wide.columns:
        feat_wide[col] = rolling_zscore(feat_wide[col], 24)
    feat_wide.index = feat_wide.index.to_timestamp()

    long = (
        feat_wide.reset_index()
        .rename(columns={"date": "period"})
        .melt(id_vars="period", var_name="feature_name", value_name="feature_value")
    )

    # Assign to tickers — only sector-relevant features; others become NaN
    frames = []
    for sym in symbols:
        sector = sector_for_symbol(sym) or ""
        tmp = long.copy()
        tmp["symbol"] = sym
        # Null out features not relevant to this sector
        irrelevant = ~tmp["feature_name"].apply(lambda f: _sector_applies(f, sector))
        tmp.loc[irrelevant, "feature_value"] = np.nan
        frames.append(tmp[["symbol", "period", "feature_name", "feature_value"]])

    df = pd.concat(frames, ignore_index=True)
    df = df.dropna(subset=["feature_value"])
    df = filter_date_range(df, start, end)

    df["source"]      = _SOURCE
    df["computed_at"] = pd.Timestamp.utcnow()
    upsert_df(
        df[["symbol", "period", "feature_name", "feature_value", "source"]],
        "signal_features",
        ["symbol", "period", "feature_name"],
    )

    out_dir = data_path(get_pipeline()["processed_data_subdirs"]["signal_tables"])
    df.to_parquet(out_dir / f"fred_signals_{start}_{end}.parquet", index=False)
    log.info("fred_signals_done", rows=len(df), symbols=df["symbol"].nunique())
