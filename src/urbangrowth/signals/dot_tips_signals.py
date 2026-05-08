"""State DOT Transportation Improvement Program signals per ticker.

Features written to signal_features (source = 'dot_tips'):
  dot_tips_state_weighted_lag{L}     — state-weighted monthly awarded project value
  dot_tips_state_weighted_yoy_lag{L} — YoY growth in trailing-12m awarded value

L ∈ {1, 3, 6}

Geographic weighting uses each ticker's geographic_concentration to
sum DOT award flows across the states in its revenue footprint.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import structlog
from sqlalchemy import text

from urbangrowth.config import data_path, get_pipeline
from urbangrowth.db.loaders import _engine, upsert_df
from urbangrowth.signals.ticker_mapping import geographic_weights, tickers_for_signal
from urbangrowth.signals.utils import filter_date_range, yoy_change, zscore_cross_section

log = structlog.get_logger(__name__)

_SOURCE = "dot_tips"
_LAGS   = (1, 3, 6)
_AWARDED_STATUSES = {"let", "awarded", "completed", "under construction"}


# ---------------------------------------------------------------------------
# DB read
# ---------------------------------------------------------------------------


def _load_dot_tips(engine) -> pd.DataFrame:
    sql = text("""
        SELECT state, awarded_date, total_cost
        FROM   dot_tips
        WHERE  awarded_date IS NOT NULL
          AND  total_cost   > 0
        ORDER  BY awarded_date
    """)
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, parse_dates=["awarded_date"])
    df["month"] = df["awarded_date"].dt.to_period("M")
    return df


# ---------------------------------------------------------------------------
# Feature builders
# ---------------------------------------------------------------------------


def _state_monthly(tips: pd.DataFrame) -> pd.DataFrame:
    """Sum monthly award value by state → (month, state) → total_cost."""
    return (
        tips.groupby(["month", "state"])["total_cost"]
        .sum()
        .reset_index()
    )


def _ticker_features(state_monthly: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    """Build state-weighted DOT award features per ticker."""
    # Pivot to (month × state)
    pivot = state_monthly.pivot(index="month", columns="state", values="total_cost").fillna(0.0)

    # Trailing-12m sum per state for YoY
    r12m_pivot = pivot.rolling(12, min_periods=1).sum()
    yoy_pivot  = r12m_pivot.pct_change(12)

    frames = []
    for sym in symbols:
        weights = geographic_weights(sym)
        if not weights:
            continue

        common = [s for s in weights if s in pivot.columns]
        if not common:
            continue

        w = pd.Series({s: weights[s] for s in common})
        w /= w.sum()

        weighted_monthly = (pivot[common]   * w).sum(axis=1)
        weighted_yoy     = (yoy_pivot[common] * w).sum(axis=1, min_count=1)

        idx = weighted_monthly.index.to_timestamp()

        for L in _LAGS:
            frames.append(pd.DataFrame({
                "symbol":       sym,
                "period":       idx,
                "feature_name": f"dot_tips_state_weighted_lag{L}",
                "feature_value":weighted_monthly.shift(L).values,
            }))
            frames.append(pd.DataFrame({
                "symbol":       sym,
                "period":       idx,
                "feature_name": f"dot_tips_state_weighted_yoy_lag{L}",
                "feature_value":weighted_yoy.shift(L).values,
            }))

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["symbol", "period", "feature_name", "feature_value"]
    )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def run(start: str = "2015-01", end: str = "2025-12") -> None:
    """Compute DOT TIP signals and upsert to signal_features table."""
    engine  = _engine()
    symbols = tickers_for_signal("dot_tips")
    log.info("dot_tips_signals_start", n_tickers=len(symbols), start=start, end=end)

    try:
        tips = _load_dot_tips(engine)
    except Exception as exc:
        log.warning("dot_tips_signals_no_data", error=str(exc))
        return

    if tips.empty:
        log.warning("dot_tips_signals_no_data", reason="dot_tips table empty")
        return

    state_monthly = _state_monthly(tips)
    df = _ticker_features(state_monthly, symbols)
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
    df.to_parquet(out_dir / f"dot_tips_signals_{start}_{end}.parquet", index=False)
    log.info("dot_tips_signals_done", rows=len(df), symbols=df["symbol"].nunique())
