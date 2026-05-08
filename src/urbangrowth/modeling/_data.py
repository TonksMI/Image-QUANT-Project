"""Data loading helpers shared across modeling modules.

All functions return clean DataFrames; they fail silently with a warning
log if the DB is empty so notebooks remain runnable before data is ingested.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import structlog
from sqlalchemy import text

from urbangrowth.db.loaders import _engine

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Signal feature panel
# ---------------------------------------------------------------------------


def load_signal_panel(source: str | None = None) -> pd.DataFrame:
    """Load signal_features from DB.

    Returns long DataFrame: symbol, period (Timestamp), feature_name, feature_value[, source].
    If source is given, filters to that source only.
    """
    if source:
        sql = text("""
            SELECT symbol, period, feature_name, feature_value
            FROM   signal_features
            WHERE  source = :src
            ORDER  BY period, symbol, feature_name
        """)
        params: dict = {"src": source}
    else:
        sql = text("""
            SELECT symbol, period, feature_name, feature_value, source
            FROM   signal_features
            ORDER  BY period, symbol, feature_name
        """)
        params = {}
    try:
        with _engine().connect() as conn:
            df = pd.read_sql(sql, conn, params=params, parse_dates=["period"])
    except Exception as exc:
        log.warning("signal_panel_load_failed", error=str(exc))
        return pd.DataFrame(columns=["symbol", "period", "feature_name", "feature_value"])
    return df


def list_signal_sources() -> list[str]:
    """Return all distinct source values in signal_features."""
    sql = text("SELECT DISTINCT source FROM signal_features ORDER BY source")
    try:
        with _engine().connect() as conn:
            return pd.read_sql(sql, conn)["source"].tolist()
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------


def load_monthly_returns() -> pd.DataFrame:
    """Load returns table.

    Returns: symbol, date (Timestamp), monthly_ret.
    """
    sql = text("""
        SELECT symbol, date, monthly_ret
        FROM   returns
        WHERE  monthly_ret IS NOT NULL
        ORDER  BY date, symbol
    """)
    try:
        with _engine().connect() as conn:
            return pd.read_sql(sql, conn, parse_dates=["date"])
    except Exception as exc:
        log.warning("returns_load_failed", error=str(exc))
        return pd.DataFrame(columns=["symbol", "date", "monthly_ret"])


def build_forward_returns(
    returns: pd.DataFrame,
    horizons: list[int] = (1, 2, 3, 6, 12),
) -> pd.DataFrame:
    """Compute compound forward returns at each horizon.

    fwd_return(t, h) = compound return over months t+1 … t+h.
    Uses log-return compounding for numerical stability.

    Returns long DataFrame: symbol, date (Timestamp), horizon, fwd_return.
    """
    if returns.empty:
        return pd.DataFrame(columns=["symbol", "date", "horizon", "fwd_return"])

    pivot = returns.pivot(index="date", columns="symbol", values="monthly_ret").sort_index()
    log_ret = np.log1p(pivot.astype(float))
    cumlog  = log_ret.cumsum()

    rows = []
    for h in horizons:
        if h == 0:
            fwd = pd.DataFrame(0.0, index=cumlog.index, columns=cumlog.columns)
        else:
            # fwd(t, h) = cumlog(t+h) - cumlog(t)
            fwd = np.expm1(cumlog.shift(-h) - cumlog)

        long = fwd.stack(dropna=True).reset_index()
        long.columns = ["date", "symbol", "fwd_return"]
        long["horizon"] = h
        rows.append(long)

    return pd.concat(rows, ignore_index=True)


# ---------------------------------------------------------------------------
# Control signals
# ---------------------------------------------------------------------------


def build_momentum_signal(
    returns: pd.DataFrame,
    window: int = 12,
) -> pd.DataFrame:
    """12m trailing compound return (skip most recent month) as momentum signal.

    Returns long: symbol, period (Timestamp), feature_name='momentum_12m', feature_value.
    """
    if returns.empty:
        return pd.DataFrame(columns=["symbol", "period", "feature_name", "feature_value"])

    pivot = returns.pivot(index="date", columns="symbol", values="monthly_ret").sort_index()
    # Skip month t (implementation lag), use t-1 to t-12
    shifted  = pivot.shift(1)
    log_ret  = np.log1p(shifted.astype(float))
    mom_log  = log_ret.rolling(window, min_periods=max(4, window // 2)).sum()
    momentum = np.expm1(mom_log)

    long = momentum.stack(dropna=True).reset_index()
    long.columns = ["period", "symbol", "feature_value"]
    long["feature_name"] = "momentum_12m"
    return long[["symbol", "period", "feature_name", "feature_value"]]


def build_volume_signal() -> pd.DataFrame:
    """3m/12m normalised average daily volume from the prices table.

    Returns long: symbol, period (Timestamp), feature_name='volume_norm', feature_value.
    """
    sql = text("""
        SELECT symbol,
               date_trunc('month', date)::date AS month,
               AVG(volume)                     AS avg_vol
        FROM   prices
        WHERE  volume > 0
        GROUP  BY symbol, date_trunc('month', date)
        ORDER  BY month, symbol
    """)
    try:
        with _engine().connect() as conn:
            df = pd.read_sql(sql, conn, parse_dates=["month"])
    except Exception as exc:
        log.warning("volume_load_failed", error=str(exc))
        return pd.DataFrame(columns=["symbol", "period", "feature_name", "feature_value"])

    if df.empty:
        return pd.DataFrame(columns=["symbol", "period", "feature_name", "feature_value"])

    pivot   = df.pivot(index="month", columns="symbol", values="avg_vol").sort_index()
    vol_3m  = pivot.rolling(3,  min_periods=2).mean()
    vol_12m = pivot.rolling(12, min_periods=6).mean().replace(0.0, np.nan)
    norm    = (vol_3m / vol_12m).shift(1)  # 1m lag

    long = norm.stack(dropna=True).reset_index()
    long.columns = ["period", "symbol", "feature_value"]
    long["feature_name"] = "volume_norm"
    return long[["symbol", "period", "feature_name", "feature_value"]]


# ---------------------------------------------------------------------------
# Convenience: aligned panel
# ---------------------------------------------------------------------------


def build_aligned_panel(
    signal_long: pd.DataFrame,
    fwd_returns: pd.DataFrame,
    momentum: pd.DataFrame,
    volume: pd.DataFrame,
    feature_name: str,
    horizon: int,
) -> pd.DataFrame | None:
    """Merge one feature with forward returns + controls for IC / regression.

    Returns wide DataFrame with columns:
        symbol, period, feature_value, fwd_return, momentum_12m, volume_norm
    or None if fewer than 20 (symbol, period) pairs survive the join.
    """
    feat = signal_long[signal_long["feature_name"] == feature_name].copy()
    feat = feat.rename(columns={"period": "date", "feature_value": feature_name})

    fwd_h = fwd_returns[fwd_returns["horizon"] == horizon][["symbol", "date", "fwd_return"]]

    mom_w = momentum.rename(columns={"period": "date", "feature_value": "momentum_12m"})
    mom_w = mom_w.drop(columns="feature_name", errors="ignore")

    vol_w = volume.rename(columns={"period": "date", "feature_value": "volume_norm"})
    vol_w = vol_w.drop(columns="feature_name", errors="ignore")

    panel = (
        feat[["symbol", "date", feature_name]]
        .merge(fwd_h,               on=["symbol", "date"], how="inner")
        .merge(mom_w[["symbol", "date", "momentum_12m"]], on=["symbol", "date"], how="left")
        .merge(vol_w[["symbol", "date", "volume_norm"]],  on=["symbol", "date"], how="left")
    )
    panel = panel.dropna(subset=[feature_name, "fwd_return"])
    if len(panel) < 20:
        return None
    return panel.rename(columns={"date": "period"})
