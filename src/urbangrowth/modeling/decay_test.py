"""IC decay analysis — predictive power at forward return horizons 0–12 months.

IC(lag=k) = Spearman(signal_t, return_{t+k}): how far ahead does the signal predict?

Expected shapes
---------------
  permits       : peaks h=1–3, fades by h=6     (monthly flow signal)
  FERC queue    : peaks h=6–12                   (project-award-to-construction lag)
  FRED macro    : broad, slow decay               (structural)
  city_growth   : peaks h=1–2, fast decay        (leading edge indicator)

CLI:  ug model decay --signal census_bps
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import structlog

from urbangrowth.config import data_path, get_pipeline
from urbangrowth.modeling._data import (
    build_forward_returns,
    build_momentum_signal,
    build_volume_signal,
    build_aligned_panel,
    list_signal_sources,
    load_monthly_returns,
    load_signal_panel,
)
from urbangrowth.modeling.ic_test import compute_ic, ic_summary
from urbangrowth.modeling.stats import fdr_correction

log = structlog.get_logger(__name__)

_DECAY_HORIZONS = (0, 1, 2, 3, 4, 6, 9, 12)


# ---------------------------------------------------------------------------
# Core decay computation (kept + extended from original)
# ---------------------------------------------------------------------------


def compute_ic_decay(
    signals: pd.DataFrame,
    returns: pd.DataFrame,
    signal_col: str,
    horizons: tuple[int, ...] = _DECAY_HORIZONS,
) -> pd.DataFrame:
    """Compute IC at each forward return horizon.

    Parameters
    ----------
    signals : long DataFrame (date|period, symbol|ticker, signal_col)
    returns : long DataFrame (date|period, symbol|ticker, horizon, fwd_return)
    signal_col : signal column in *signals*
    horizons : forward return horizons to test

    Returns
    -------
    DataFrame: horizon, ic_mean, ic_std, ic_ir, nw_tstat, ci_lo, ci_hi, hit_rate, n_months
    """
    rows = []
    for h in horizons:
        ic_df = compute_ic(signals, returns, signal_col, horizon=h)
        if ic_df.empty:
            continue
        stats = ic_summary(ic_df)
        if not stats:
            continue
        stats["horizon"] = h
        rows.append(stats)
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def plot_decay(decay_df: pd.DataFrame, signal_name: str) -> None:
    """Log IC decay profile."""
    log.info("ic_decay_summary", signal=signal_name)
    for _, row in decay_df.iterrows():
        log.info(
            "decay_lag",
            h=int(row["horizon"]),
            ic_mean=round(row["ic_mean"], 4),
            ic_ir=round(float(row.get("ic_ir", 0) or 0), 3),
            nw_t=round(float(row.get("nw_tstat", 0) or 0), 2),
        )


# ---------------------------------------------------------------------------
# Full decay analysis across sources
# ---------------------------------------------------------------------------


def run_decay_analysis(
    sources: list[str] | None = None,
    horizons: tuple[int, ...] = _DECAY_HORIZONS,
) -> pd.DataFrame:
    """Run IC decay for all features in each source.

    Returns DataFrame: source, feature_name, horizon, ic_mean, nw_tstat, ci_lo, ci_hi, …
    """
    if sources is None:
        sources = list_signal_sources()
    if not sources:
        log.warning("decay_test_no_sources")
        return pd.DataFrame()

    returns     = load_monthly_returns()
    if returns.empty:
        log.warning("decay_test_no_returns")
        return pd.DataFrame()

    fwd_returns = build_forward_returns(returns, list(horizons))
    momentum    = build_momentum_signal(returns)
    volume      = build_volume_signal()

    rows = []
    for src in sources:
        log.info("decay_test_source", source=src)
        signals = load_signal_panel(src)
        if signals.empty:
            continue

        for feat in signals["feature_name"].unique():
            # Use h=1 panel for alignment but compute IC at all horizons
            panel_h1 = build_aligned_panel(signals, fwd_returns, momentum, volume, feat, 1)
            if panel_h1 is None:
                continue

            # Build synthetic long signals/returns for compute_ic_decay
            sig_long = (
                signals[signals["feature_name"] == feat]
                .rename(columns={"period": "date", "feature_value": feat})
            )
            ret_long = fwd_returns.rename(columns={"date": "date"})
            ret_long = ret_long.copy()
            if "ticker" not in ret_long.columns and "symbol" in ret_long.columns:
                ret_long = ret_long.rename(columns={"symbol": "symbol"})

            decay_df = compute_ic_decay(sig_long, ret_long, feat, horizons=horizons)
            if decay_df.empty:
                continue

            decay_df["source"]       = src
            decay_df["feature_name"] = feat
            rows.append(decay_df)

    if not rows:
        return pd.DataFrame()

    return pd.concat(rows, ignore_index=True).sort_values(
        ["source", "feature_name", "horizon"]
    ).reset_index(drop=True)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run(signal_name: str = "all") -> None:
    """Compute IC decay and save results.

    signal_name : source name or 'all'.
    """
    sources = None if signal_name == "all" else [signal_name]

    df = run_decay_analysis(sources=sources)
    if df.empty:
        log.warning("decay_test_empty", signal=signal_name)
        return

    out_dir  = data_path(get_pipeline()["processed_data_subdirs"].get("signal_tables", "processed/signals"))
    out_path = out_dir / f"decay_results_{signal_name}.parquet"
    df.to_parquet(out_path, index=False)
    log.info("decay_test_done", rows=len(df), path=str(out_path))

    # Print peak-lag per feature to console
    peak = df.loc[df.groupby(["source", "feature_name"])["ic_mean"].idxmax()]
    log.info("decay_peak_lags", results=peak[["source", "feature_name", "horizon", "ic_mean"]].to_dict("records"))
