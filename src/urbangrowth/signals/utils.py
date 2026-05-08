"""Shared utilities for signal computation modules."""
from __future__ import annotations

import numpy as np
import pandas as pd


def yoy_change(s: pd.Series) -> pd.Series:
    """Percent change vs 12 months ago on a monthly time series."""
    return s.pct_change(12)


def rolling_zscore(s: pd.Series, window: int = 24) -> pd.Series:
    """Rolling z-score with the given lookback window."""
    mean = s.rolling(window, min_periods=max(4, window // 2)).mean()
    std  = s.rolling(window, min_periods=max(4, window // 2)).std().replace(0.0, np.nan)
    return (s - mean) / std


def zscore_cross_section(df: pd.DataFrame) -> pd.DataFrame:
    """Z-score feature_value for each (period, feature_name) group.

    Tickers with NaN are excluded from mean/std computation.
    Groups with a single valid observation produce NaN (std=0 → divide by NaN).
    """
    df = df.copy()
    grp = df.groupby(["period", "feature_name"])["feature_value"]
    mean_ = grp.transform("mean")
    std_  = grp.transform("std").replace(0.0, np.nan)
    df["feature_value"] = (df["feature_value"] - mean_) / std_
    return df


def melt_features(
    wide: pd.DataFrame,
    symbol: str,
    feature_prefix: str = "",
) -> pd.DataFrame:
    """Convert a wide period-indexed DataFrame to long signal_features rows.

    Parameters
    ----------
    wide : DataFrame with period (datetime) index and one column per feature
    symbol : ticker symbol
    feature_prefix : optional prefix prepended to column names
    """
    wide = wide.copy()
    wide.index.name = "period"
    long = wide.reset_index().melt(id_vars="period", var_name="feature_name", value_name="feature_value")
    long["symbol"] = symbol
    if feature_prefix:
        long["feature_name"] = feature_prefix + long["feature_name"].astype(str)
    return long[["symbol", "period", "feature_name", "feature_value"]]


def filter_date_range(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Keep rows where period is within [start, end] (YYYY-MM strings)."""
    start_dt = pd.Timestamp(start + "-01")
    end_dt   = pd.Timestamp(end   + "-01") + pd.offsets.MonthEnd(0)
    mask = (df["period"] >= start_dt) & (df["period"] <= end_dt)
    return df.loc[mask].copy()
