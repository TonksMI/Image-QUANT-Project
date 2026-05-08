"""National + state-weighted permit signals per ticker.

Features written to signal_features (source = 'census_bps'):
  permit_res_yoy_nat_lag{L}     — national residential YoY × sector sensitivity
  permit_com_yoy_nat_lag{L}     — national commercial  YoY × sector sensitivity
  permit_val_rolling_zscore     — rolling 24m z-score of national total_valuation
  permit_state_res_yoy_lag{L}   — geographic-weighted state residential YoY
  permit_state_com_yoy_lag{L}   — geographic-weighted state commercial  YoY

L ∈ {1, 2, 3, 6}

Sector sensitivity multiplier creates cross-sectional variance from the
common national time series: homebuilders get 1.0 × national, steel 0.4 ×, etc.
Geographic-weighted features vary further within the census_bps cross-section
based on each ticker's state footprint.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import structlog
from sqlalchemy import text

from urbangrowth.config import data_path, get_pipeline
from urbangrowth.db.loaders import _engine, upsert_df
from urbangrowth.signals.ticker_mapping import (
    PERMIT_SENSITIVITY,
    geographic_weights,
    sector_for_symbol,
    tickers_for_signal,
)
from urbangrowth.signals.utils import filter_date_range, rolling_zscore, yoy_change, zscore_cross_section

log = structlog.get_logger(__name__)

_SOURCE = "census_bps"
_LAGS   = (1, 2, 3, 6)


# ---------------------------------------------------------------------------
# DB reads
# ---------------------------------------------------------------------------


def _load_national(engine) -> pd.DataFrame:
    sql = text("""
        SELECT period,
               COALESCE(residential_permits, 0) AS res,
               COALESCE(commercial_permits,  0) AS com,
               COALESCE(total_valuation,     0) AS val
        FROM   census_bps
        WHERE  jurisdiction_id = 'national'
        ORDER  BY period
    """)
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, parse_dates=["period"])
    df = df.set_index("period").sort_index()
    df.index = df.index.to_period("M")
    return df


def _load_state(engine) -> pd.DataFrame:
    sql = text("""
        SELECT period, state,
               SUM(COALESCE(residential_permits, 0)) AS res,
               SUM(COALESCE(commercial_permits,  0)) AS com
        FROM   census_bps
        WHERE  jurisdiction_id != 'national'
          AND  state IS NOT NULL
        GROUP  BY period, state
        ORDER  BY period, state
    """)
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, parse_dates=["period"])
    df["period"] = df["period"].dt.to_period("M")
    return df


# ---------------------------------------------------------------------------
# Feature builders
# ---------------------------------------------------------------------------


def _national_features(nat: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    """Return long DataFrame of national-level permit features for all census_bps tickers."""
    res_yoy    = yoy_change(nat["res"])
    com_yoy    = yoy_change(nat["com"])
    val_zscore = rolling_zscore(nat["val"], window=24)

    # Build wide feature DataFrame keyed by period (all lags pre-computed)
    feat: dict[str, pd.Series] = {}
    for L in _LAGS:
        feat[f"permit_res_yoy_nat_lag{L}"] = res_yoy.shift(L)
        feat[f"permit_com_yoy_nat_lag{L}"] = com_yoy.shift(L)
    feat["permit_val_rolling_zscore"] = val_zscore

    wide = pd.DataFrame(feat)  # index = PeriodIndex
    wide.index = wide.index.to_timestamp()

    long = (
        wide.reset_index()
        .rename(columns={"period": "period"})
        .melt(id_vars="period", var_name="feature_name", value_name="raw_value")
    )

    # Broadcast to all symbols, scaling by sector sensitivity
    frames = []
    for sym in symbols:
        sens = PERMIT_SENSITIVITY.get(sector_for_symbol(sym) or "", 0.2)
        tmp = long.copy()
        tmp["symbol"] = sym
        tmp["feature_value"] = tmp["raw_value"] * sens
        frames.append(tmp[["symbol", "period", "feature_name", "feature_value"]])

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["symbol", "period", "feature_name", "feature_value"]
    )


def _state_features(state_df: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    """Return long DataFrame of state-weighted permit features per ticker."""
    # Pivot to (period × state) wide
    res_wide = state_df.pivot(index="period", columns="state", values="res")
    com_wide = state_df.pivot(index="period", columns="state", values="com")

    res_yoy = res_wide.pct_change(12)
    com_yoy = com_wide.pct_change(12)

    frames = []
    for sym in symbols:
        weights = geographic_weights(sym)
        if not weights:
            continue

        def _weighted(yoy_df: pd.DataFrame) -> pd.Series:
            common = [s for s in weights if s in yoy_df.columns]
            if not common:
                return pd.Series(np.nan, index=yoy_df.index, dtype=float)
            w = pd.Series({s: weights[s] for s in common})
            w /= w.sum()
            return (yoy_df[common] * w).sum(axis=1, min_count=1)

        ticker_res_yoy = _weighted(res_yoy)
        ticker_com_yoy = _weighted(com_yoy)

        for L in _LAGS:
            lag_idx = ticker_res_yoy.index.to_timestamp()
            frames.append(pd.DataFrame({
                "symbol":       sym,
                "period":       lag_idx,
                "feature_name": f"permit_state_res_yoy_lag{L}",
                "feature_value":ticker_res_yoy.shift(L).values,
            }))
            frames.append(pd.DataFrame({
                "symbol":       sym,
                "period":       lag_idx,
                "feature_name": f"permit_state_com_yoy_lag{L}",
                "feature_value":ticker_com_yoy.shift(L).values,
            }))

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["symbol", "period", "feature_name", "feature_value"]
    )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def run(start: str = "2015-01", end: str = "2025-12") -> None:
    """Compute permit signals and upsert to signal_features table."""
    engine = _engine()
    symbols = tickers_for_signal("census_bps")
    log.info("permit_signals_start", n_tickers=len(symbols), start=start, end=end)

    try:
        nat      = _load_national(engine)
        state_df = _load_state(engine)
    except Exception as exc:
        log.warning("permit_signals_no_data", error=str(exc))
        return

    if nat.empty:
        log.warning("permit_signals_no_data", reason="census_bps table empty")
        return

    nat_feat   = _national_features(nat, symbols)
    state_feat = _state_features(state_df, symbols) if not state_df.empty else pd.DataFrame(
        columns=["symbol", "period", "feature_name", "feature_value"]
    )

    df = pd.concat([nat_feat, state_feat], ignore_index=True)
    df = df.dropna(subset=["feature_value"])
    df = filter_date_range(df, start, end)
    df = zscore_cross_section(df)

    df["source"]      = _SOURCE
    df["computed_at"] = pd.Timestamp.utcnow()
    upsert_df(
        df[["symbol", "period", "feature_name", "feature_value", "source"]],
        "signal_features",
        ["symbol", "period", "feature_name"],
    )

    out_dir = data_path(get_pipeline()["processed_data_subdirs"]["signal_tables"])
    df.to_parquet(out_dir / f"permit_signals_{start}_{end}.parquet", index=False)
    log.info("permit_signals_done", rows=len(df), symbols=df["symbol"].nunique())
