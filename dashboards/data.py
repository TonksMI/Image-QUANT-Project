"""Cached data loaders shared across all dashboard pages.

All functions return empty DataFrames with the correct schema when the DB
is empty or parquets are missing, so pages remain renderable before any
data has been ingested.
"""
from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(_ROOT / "src"))

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# Engine + path helpers
# ---------------------------------------------------------------------------

@st.cache_resource
def get_engine():
    from urbangrowth.db.loaders import _engine
    return _engine()


def _proc_dir() -> Path:
    try:
        from urbangrowth.config import data_path, get_pipeline
        return data_path(
            get_pipeline()["processed_data_subdirs"].get("signal_tables", "processed/signals")
        )
    except Exception:
        return _ROOT / "data" / "processed" / "signals"


def _h3_dir(city: str) -> Path:
    try:
        from urbangrowth.config import data_path
        return data_path(f"processed/h3_features/{city}")
    except Exception:
        return _ROOT / "data" / "processed" / "h3_features" / city


def _safe_read_parquet(path: Path, fallback_cols: list[str]) -> pd.DataFrame:
    try:
        return pd.read_parquet(path)
    except Exception:
        return pd.DataFrame(columns=fallback_cols)


def _safe_sql(sql: str, params: dict | None = None,
              parse_dates: list[str] | None = None,
              fallback_cols: list[str] | None = None) -> pd.DataFrame:
    try:
        from sqlalchemy import text
        engine = get_engine()
        with engine.connect() as conn:
            return pd.read_sql(text(sql), conn, params=params or {},
                               parse_dates=parse_dates or [])
    except Exception:
        return pd.DataFrame(columns=fallback_cols or [])


# ---------------------------------------------------------------------------
# National Alt Data
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def load_census_bps() -> pd.DataFrame:
    """Monthly Census BPS by jurisdiction.
    Returns: period, state, jurisdiction_id, res_units, com_units, res_value, com_value.
    """
    return _safe_sql(
        """SELECT period, state, jurisdiction_id,
                  res_units, com_units,
                  res_value, com_value
           FROM census_bps
           WHERE jurisdiction_id != 'national'
           ORDER BY period, state""",
        parse_dates=["period"],
        fallback_cols=["period", "state", "jurisdiction_id",
                       "res_units", "com_units", "res_value", "com_value"],
    )


@st.cache_data(ttl=300)
def load_census_bps_national() -> pd.DataFrame:
    return _safe_sql(
        """SELECT period, res_units, com_units, res_value, com_value
           FROM census_bps WHERE jurisdiction_id = 'national'
           ORDER BY period""",
        parse_dates=["period"],
        fallback_cols=["period", "res_units", "com_units", "res_value", "com_value"],
    )


@st.cache_data(ttl=300)
def load_ferc_queue() -> pd.DataFrame:
    """FERC interconnection queue: one row per project snapshot."""
    return _safe_sql(
        """SELECT project_id, queue_date, iso_region, state,
                  fuel_type, mw_capacity, status
           FROM ferc_queue ORDER BY queue_date""",
        parse_dates=["queue_date"],
        fallback_cols=["project_id", "queue_date", "iso_region",
                       "state", "fuel_type", "mw_capacity", "status"],
    )


@st.cache_data(ttl=300)
def load_usaspending() -> pd.DataFrame:
    """Federal contract awards from USASpending."""
    return _safe_sql(
        """SELECT award_date, recipient_name, naics_code,
                  total_obligated_amount, place_of_performance_state
           FROM usaspending_awards
           ORDER BY award_date""",
        parse_dates=["award_date"],
        fallback_cols=["award_date", "recipient_name", "naics_code",
                       "total_obligated_amount", "place_of_performance_state"],
    )


@st.cache_data(ttl=300)
def load_fred() -> pd.DataFrame:
    """FRED macro series — all series loaded together."""
    return _safe_sql(
        """SELECT series_id, date, value FROM fred_data ORDER BY series_id, date""",
        parse_dates=["date"],
        fallback_cols=["series_id", "date", "value"],
    )


# ---------------------------------------------------------------------------
# H3 city panel
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def h3_available_months(city: str) -> list[str]:
    """Return sorted YYYY-MM strings for which LC parquets exist."""
    d = _h3_dir(city)
    if not d.exists():
        return []
    return sorted(p.stem.replace("_lc", "") for p in d.glob("*_lc.parquet"))


@st.cache_data(ttl=60)
def load_h3_month(city: str, month: str) -> pd.DataFrame:
    """Load H3 panel for one city + month.
    Returns: h3_index, built_pct, veg_to_built_pct, + any other columns.
    """
    lc_path = _h3_dir(city) / f"{month}_lc.parquet"
    tr_path = _h3_dir(city) / f"{month}_trans.parquet"
    cols = ["h3_index", "built_pct"]
    try:
        lc = pd.read_parquet(lc_path) if lc_path.exists() else pd.DataFrame(columns=cols)
        tr = pd.read_parquet(tr_path) if tr_path.exists() else pd.DataFrame(columns=["h3_index"])
        if lc.empty:
            return pd.DataFrame(columns=cols)
        if not tr.empty and "h3_index" in tr.columns:
            lc = lc.merge(tr, on="h3_index", how="left")
        return lc
    except Exception:
        return pd.DataFrame(columns=cols)


@st.cache_data(ttl=60)
def load_h3_full_panel(city: str) -> pd.DataFrame:
    """Load all months for a city as a single long DataFrame."""
    months = h3_available_months(city)
    if not months:
        return pd.DataFrame(columns=["h3_index", "period", "built_pct"])
    frames = []
    for m in months:
        df = load_h3_month(city, m)
        if not df.empty:
            df["period"] = pd.Timestamp(m + "-01")
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["h3_index", "period", "built_pct"]
    )


# ---------------------------------------------------------------------------
# Zoning
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def load_zoning_h3(city: str) -> pd.DataFrame:
    """H3 cells with dominant zoning class."""
    return _safe_sql(
        """SELECT h3_index, zoning_class, city FROM zoning_h3
           WHERE city = :city ORDER BY h3_index""",
        params={"city": city},
        fallback_cols=["h3_index", "zoning_class", "city"],
    )


# ---------------------------------------------------------------------------
# Signal library
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def load_signal_sources() -> list[str]:
    try:
        from urbangrowth.modeling._data import list_signal_sources
        return list_signal_sources()
    except Exception:
        return []


@st.cache_data(ttl=300)
def load_signal_panel(source: str | None = None) -> pd.DataFrame:
    try:
        from urbangrowth.modeling._data import load_signal_panel as _lsp
        return _lsp(source)
    except Exception:
        return pd.DataFrame(columns=["symbol", "period", "feature_name", "feature_value"])


@st.cache_data(ttl=300)
def load_ic_results() -> pd.DataFrame:
    return _safe_read_parquet(
        _proc_dir() / "ic_results_all_hall.parquet",
        ["source", "feature_name", "horizon", "ic_mean", "nw_tstat",
         "p_adjusted", "fdr_rejected"],
    )


@st.cache_data(ttl=300)
def load_decay_results() -> pd.DataFrame:
    return _safe_read_parquet(
        _proc_dir() / "decay_results_all.parquet",
        ["source", "feature_name", "horizon", "ic_mean", "nw_tstat"],
    )


# ---------------------------------------------------------------------------
# Model + backtest
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def load_backtest(model_type: str = "ridge", horizon: int = 1) -> pd.DataFrame:
    return _safe_read_parquet(
        _proc_dir() / f"backtest_{model_type}_h{horizon}.parquet",
        ["period", "long_ret", "short_ret", "ls_ret", "ls_gross",
         "tc", "turnover", "n_long", "n_short"],
    )


@st.cache_data(ttl=60)
def load_model_scores(model_type: str = "ridge", horizon: int = 1) -> pd.DataFrame:
    return _safe_read_parquet(
        _proc_dir() / f"model_scores_{model_type}_h{horizon}.parquet",
        ["period", "symbol", "score"],
    )


@st.cache_data(ttl=300)
def load_bench_comparison(model_type: str = "ridge", horizon: int = 1) -> pd.DataFrame:
    return _safe_read_parquet(
        _proc_dir() / f"backtest_{model_type}_h{horizon}_bench.parquet",
        ["entity", "cagr", "ann_vol", "sharpe", "max_drawdown", "hit_rate"],
    )


@st.cache_data(ttl=300)
def load_factor_exposures(model_type: str = "ridge", horizon: int = 1) -> pd.DataFrame:
    return _safe_read_parquet(
        _proc_dir() / f"backtest_{model_type}_h{horizon}_factors.parquet",
        ["factor", "beta", "tstat", "pvalue"],
    )


@st.cache_data(ttl=300)
def load_sector_decomp(model_type: str = "ridge", horizon: int = 1) -> pd.DataFrame:
    return _safe_read_parquet(
        _proc_dir() / f"backtest_{model_type}_h{horizon}_sector.parquet",
        ["sector", "side", "avg_weight", "avg_contribution"],
    )


@st.cache_data(ttl=300)
def load_universe_df() -> pd.DataFrame:
    try:
        from urbangrowth.config import get_universe
        return pd.DataFrame(get_universe())
    except Exception:
        return pd.DataFrame(columns=["symbol", "name", "sector"])


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------

@st.cache_data(ttl=120)
def load_coverage_report() -> pd.DataFrame:
    """Coverage: rows per (source, period) in signal_features."""
    return _safe_sql(
        """SELECT source, DATE_TRUNC('month', period) AS period,
                  COUNT(DISTINCT symbol) AS n_tickers,
                  COUNT(DISTINCT feature_name) AS n_features,
                  COUNT(*) AS n_rows
           FROM signal_features
           GROUP BY source, DATE_TRUNC('month', period)
           ORDER BY source, period""",
        parse_dates=["period"],
        fallback_cols=["source", "period", "n_tickers", "n_features", "n_rows"],
    )


@st.cache_data(ttl=120)
def load_returns_coverage() -> pd.DataFrame:
    return _safe_sql(
        """SELECT DATE_TRUNC('month', date) AS period,
                  COUNT(DISTINCT symbol) AS n_tickers
           FROM returns GROUP BY 1 ORDER BY 1""",
        parse_dates=["period"],
        fallback_cols=["period", "n_tickers"],
    )


@st.cache_data(ttl=120)
def load_h3_coverage(city: str) -> pd.DataFrame:
    months = h3_available_months(city)
    rows = []
    for m in months:
        df = load_h3_month(city, m)
        rows.append({
            "period": pd.Timestamp(m + "-01"),
            "n_cells": len(df),
            "pct_built_valid": float(df["built_pct"].notna().mean()) if not df.empty else 0.0,
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["period", "n_cells", "pct_built_valid"]
    )
