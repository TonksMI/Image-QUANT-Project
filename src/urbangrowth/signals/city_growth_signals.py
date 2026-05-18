"""City-level satellite urban growth signals → ticker scores.

Aggregates Phase-7 H3 feature parquets to city-month scalars, then
distributes to tickers based on each ticker's state geographic exposure.

Features written to signal_features (source = 'city_growth'):
  city_growth_mean_built_delta   — mean monthly Δbuilt_pct across H3 cells
  city_growth_p90_built_delta    — 90th percentile of Δbuilt_pct
  city_growth_veg_to_built       — mean veg_to_built transition fraction
  city_growth_cai                — Construction Activity Index
                                   (rolling-24m z-score composite of the three above)

Each feature is weighted by the ticker's geographic exposure to the city's
primary state (AZ for phoenix, TX for austin) so tickers with higher state
concentration get a stronger signal.

Reads from: processed/h3_features/{city}/{YYYY-MM}_lc.parquet
            processed/h3_features/{city}/{YYYY-MM}_trans.parquet
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import structlog

from urbangrowth.config import data_path, get_cities, get_pipeline
from urbangrowth.db.loaders import upsert_df
from urbangrowth.signals.ticker_mapping import (
    city_primary_state,
    geographic_weights,
    tickers_with_city,
)
from urbangrowth.signals.utils import (
    filter_date_range,
    rolling_zscore,
    zscore_cross_section,
)

log = structlog.get_logger(__name__)

_SOURCE   = "city_growth"
_CITIES   = ["phoenix", "austin"]


# ---------------------------------------------------------------------------
# H3 parquet readers
# ---------------------------------------------------------------------------


def _load_city_monthly(city: str) -> pd.DataFrame:
    """Load all available LC + transition parquets for a city.

    Returns a DataFrame with columns:
        month, mean_built_pct, p90_built_pct, mean_veg_to_built
    """
    pipe    = get_pipeline()
    feat_dir = data_path(pipe["processed_data_subdirs"]["h3_features"], city)

    lc_files   = sorted(feat_dir.glob("*_lc.parquet"))
    trans_files = sorted(feat_dir.glob("*_trans.parquet"))

    if not lc_files:
        log.info("city_growth_no_lc_parquets", city=city)
        return pd.DataFrame()

    # Index trans files by month string
    trans_index: dict[str, Path] = {p.name[:7]: p for p in trans_files}

    records = []
    for lc_path in lc_files:
        month = lc_path.name[:7]
        try:
            lc = pd.read_parquet(lc_path, columns=["h3_index", "built_pct"])
        except Exception as exc:
            log.debug("city_growth_lc_read_error", path=str(lc_path), error=str(exc))
            continue

        mean_built = float(lc["built_pct"].mean(skipna=True))
        p90_built  = float(lc["built_pct"].quantile(0.90))

        mean_veg_to_built = np.nan
        if month in trans_index:
            try:
                trans = pd.read_parquet(
                    trans_index[month],
                    columns=["h3_index", "lag_months", "veg_to_built_pct"],
                )
                lag1 = trans[trans["lag_months"] == 1]
                if not lag1.empty:
                    mean_veg_to_built = float(lag1["veg_to_built_pct"].mean(skipna=True))
            except Exception:
                pass

        records.append({
            "month":             month,
            "mean_built_pct":    mean_built,
            "p90_built_pct":     p90_built,
            "mean_veg_to_built": mean_veg_to_built,
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["period"] = pd.PeriodIndex(df["month"] + "-01", freq="M")
    return df.set_index("period").sort_index()


# ---------------------------------------------------------------------------
# City-level signal computation
# ---------------------------------------------------------------------------


def _city_signals(city_df: pd.DataFrame) -> pd.DataFrame:
    """Compute city-month scalars from the raw H3 aggregations."""
    # Month-over-month delta in mean built fraction
    built_delta    = city_df["mean_built_pct"].diff(1)
    p90_built_delta = city_df["p90_built_pct"].diff(1)
    veg_to_built   = city_df["mean_veg_to_built"]

    # Construction Activity Index — rolling 24m z-score of composite
    composite = (
        rolling_zscore(built_delta,   24).fillna(0)
        + rolling_zscore(p90_built_delta, 24).fillna(0)
        + rolling_zscore(veg_to_built,    24).fillna(0)
    ) / 3.0

    feat = pd.DataFrame({
        "city_growth_mean_built_delta": built_delta,
        "city_growth_p90_built_delta":  p90_built_delta,
        "city_growth_veg_to_built":     veg_to_built,
        "city_growth_cai":              composite,
    })
    return feat


# ---------------------------------------------------------------------------
# Ticker broadcasting
# ---------------------------------------------------------------------------


def _broadcast_to_tickers(
    city_feat: pd.DataFrame,
    city: str,
    symbols: list[str],
) -> pd.DataFrame:
    """Weight city signals by each ticker's state exposure for the city's state."""
    primary_state = city_primary_state(city)
    city_feat_ts  = city_feat.copy()
    city_feat_ts.index = city_feat_ts.index.to_timestamp()

    long = (
        city_feat_ts.reset_index()
        .rename(columns={"period": "period"})
        .melt(id_vars="period", var_name="feature_name", value_name="raw_value")
    )

    frames = []
    for sym in symbols:
        weights = geographic_weights(sym)
        # Weight = exposure to the city's primary state; fallback to 0.5 if nationwide
        if primary_state:
            w = weights.get(primary_state, 0.0)
            if w == 0.0:
                # Ticker has city signal but no state data → use mean of available weights
                w = 0.25
        else:
            w = 0.25

        tmp = long.copy()
        tmp["symbol"] = sym
        tmp["feature_value"] = tmp["raw_value"] * w
        frames.append(tmp[["symbol", "period", "feature_name", "feature_value"]])

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["symbol", "period", "feature_name", "feature_value"]
    )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def run(start: str = "2018-01", end: str = "2025-12") -> None:
    """Compute city growth signals and upsert to signal_features table."""
    all_frames = []

    for city in _CITIES:
        symbols = tickers_with_city(city)
        if not symbols:
            continue

        log.info("city_growth_signals_start", city=city, n_tickers=len(symbols))

        city_df = _load_city_monthly(city)
        if city_df.empty:
            log.info("city_growth_no_data", city=city)
            continue

        city_feat  = _city_signals(city_df)
        ticker_df  = _broadcast_to_tickers(city_feat, city, symbols)
        all_frames.append(ticker_df)

    if not all_frames:
        log.warning("city_growth_signals_empty")
        return

    df = pd.concat(all_frames, ignore_index=True)
    # Sum contributions from multiple cities for tickers in both Phoenix + Austin
    df = (
        df.groupby(["symbol", "period", "feature_name"], as_index=False)["feature_value"]
        .sum()
    )
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
    df.to_parquet(out_dir / f"city_growth_signals_{start}_{end}.parquet", index=False)
    log.info("city_growth_signals_done", rows=len(df), symbols=df["symbol"].nunique())
