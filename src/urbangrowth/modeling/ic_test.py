"""Information Coefficient (IC) testing.

Computes Spearman rank correlation between signal values and realized
forward returns at each horizon. Reports IC mean, IC IR (mean/std),
Newey-West t-stat, bootstrap confidence intervals, and incremental IC
versus momentum-only and volume-only baselines on the same ticker subset.

CLI:
    ug model ic-test --signal census_bps --horizon 1   # single source × horizon
    ug model ic-test --signal all                       # all sources × horizons 1/2/3
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import scipy.stats as scipy_stats
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
from urbangrowth.modeling.stats import (
    bootstrap_ic_ci,
    fdr_correction,
    newey_west_tstat,
)

log = structlog.get_logger(__name__)

_IC_HORIZONS = (1, 2, 3)
_MIN_CROSS_SECTION = 5    # minimum tickers per month to compute IC
_MIN_MONTHS = 12          # minimum months to report IC stats


# ---------------------------------------------------------------------------
# Core IC primitives (kept from original + enhanced)
# ---------------------------------------------------------------------------


def compute_ic(
    signals: pd.DataFrame,
    returns: pd.DataFrame,
    signal_col: str,
    horizon: int,
) -> pd.DataFrame:
    """Compute monthly Spearman IC between signal and h-month forward return.

    Parameters
    ----------
    signals : long DataFrame — columns (date | period, symbol | ticker, <signal_col>)
    returns : long DataFrame — columns (date | period, symbol | ticker, horizon, fwd_return)
    signal_col : name of the signal column in signals
    horizon : forward return horizon in months

    Returns
    -------
    DataFrame with columns: date, ic, p_value, n_stocks
    """
    # Normalise column names
    sig = signals.copy()
    if "ticker" in sig.columns:
        sig = sig.rename(columns={"ticker": "symbol"})
    if "period" in sig.columns:
        sig = sig.rename(columns={"period": "date"})

    ret = returns.copy()
    if "ticker" in ret.columns:
        ret = ret.rename(columns={"ticker": "symbol"})
    if "period" in ret.columns:
        ret = ret.rename(columns={"period": "date"})

    fwd = ret[ret["horizon"] == horizon][["date", "symbol", "fwd_return"]]
    merged = sig[["date", "symbol", signal_col]].merge(fwd, on=["date", "symbol"])
    merged = merged.dropna(subset=[signal_col, "fwd_return"])

    records = []
    for date, grp in merged.groupby("date"):
        grp = grp.dropna(subset=[signal_col, "fwd_return"])
        if len(grp) < _MIN_CROSS_SECTION:
            continue
        rho, pval = scipy_stats.spearmanr(grp[signal_col], grp["fwd_return"])
        records.append({"date": date, "ic": float(rho), "p_value": float(pval), "n_stocks": len(grp)})
    return pd.DataFrame(records)


def ic_summary(ic_df: pd.DataFrame) -> dict:
    """IC mean, IC IR, hit rate, Newey-West t-stat, bootstrap CI."""
    if ic_df.empty or "ic" not in ic_df.columns:
        return {}
    s = ic_df["ic"].dropna()
    if len(s) < _MIN_MONTHS:
        return {}
    mean_ic  = float(s.mean())
    std_ic   = float(s.std())
    ic_ir    = mean_ic / std_ic if std_ic > 0 else np.nan
    hit_rate = float((s > 0).mean())
    nw_t     = newey_west_tstat(s)
    ci_lo, ci_hi = bootstrap_ic_ci(s)
    return {
        "ic_mean":    mean_ic,
        "ic_std":     std_ic,
        "ic_ir":      ic_ir,
        "hit_rate":   hit_rate,
        "nw_tstat":   nw_t,
        "ci_lo":      ci_lo,
        "ci_hi":      ci_hi,
        "n_months":   len(s),
    }


# ---------------------------------------------------------------------------
# Full IC analysis pipeline
# ---------------------------------------------------------------------------


def _ic_for_feature(
    panel: pd.DataFrame,
    feature_col: str,
    horizon: int,
    fwd_returns: pd.DataFrame,
) -> dict | None:
    """Compute IC stats for one (feature, horizon) combination.

    panel must have columns: symbol, period, {feature_col}, fwd_return,
    momentum_12m, volume_norm.
    """
    # Build synthetic signals DataFrames for compute_ic
    sig_feat = panel[["period", "symbol", feature_col]].rename(columns={"period": "date"})
    sig_mom  = panel[["period", "symbol", "momentum_12m"]].rename(columns={"period": "date"})
    sig_vol  = panel[["period", "symbol", "volume_norm"]].rename(columns={"period": "date"})

    # Construct returns in the format compute_ic expects
    ret = panel[["period", "symbol", "fwd_return"]].copy()
    ret["horizon"] = horizon
    ret = ret.rename(columns={"period": "date"})

    ic_alt = compute_ic(sig_feat, ret, feature_col, horizon)
    ic_mom = compute_ic(sig_mom, ret, "momentum_12m", horizon)
    ic_vol = compute_ic(sig_vol, ret, "volume_norm", horizon)

    stats = ic_summary(ic_alt)
    if not stats:
        return None

    mom_stats = ic_summary(ic_mom)
    vol_stats = ic_summary(ic_vol)

    # Incremental IC = alt IC - momentum IC (on same cross-section)
    stats["feature_name"]      = feature_col
    stats["horizon"]           = horizon
    stats["mom_ic_mean"]       = mom_stats.get("ic_mean", np.nan)
    stats["vol_ic_mean"]       = vol_stats.get("ic_mean", np.nan)
    stats["incremental_ic"]    = stats["ic_mean"] - stats.get("mom_ic_mean", 0.0)
    stats["p_value_raw"]       = float(scipy_stats.t.sf(abs(stats["nw_tstat"]), df=stats["n_months"] - 1) * 2)

    return stats


def run_full_ic_analysis(
    sources: list[str] | None = None,
    horizons: tuple[int, ...] = _IC_HORIZONS,
) -> pd.DataFrame:
    """Run IC analysis across all signal sources × features × horizons.

    Returns a DataFrame with one row per (source, feature_name, horizon)
    plus FDR-adjusted p-values.
    """
    if sources is None:
        sources = list_signal_sources()
    if not sources:
        log.warning("ic_test_no_sources")
        return pd.DataFrame()

    returns    = load_monthly_returns()
    if returns.empty:
        log.warning("ic_test_no_returns")
        return pd.DataFrame()

    fwd_returns = build_forward_returns(returns, list(horizons))
    momentum    = build_momentum_signal(returns)
    volume      = build_volume_signal()

    rows = []
    for src in sources:
        log.info("ic_test_source", source=src)
        signals = load_signal_panel(src)
        if signals.empty:
            log.info("ic_test_source_empty", source=src)
            continue

        for feat in signals["feature_name"].unique():
            for h in horizons:
                panel = build_aligned_panel(signals, fwd_returns, momentum, volume, feat, h)
                if panel is None:
                    continue
                result = _ic_for_feature(panel, feat, h, fwd_returns)
                if result:
                    result["source"] = src
                    rows.append(result)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # BH-FDR correction across all (signal, horizon) tests
    p_vals = df["p_value_raw"]
    fdr_df = fdr_correction(p_vals, alpha=0.05)
    df["p_adjusted"] = fdr_df["p_adjusted"].values
    df["fdr_rejected"] = fdr_df["rejected"].values

    return df.sort_values(["source", "feature_name", "horizon"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run(signal_name: str = "all", horizon: int | None = None) -> None:
    """Compute IC stats and save results.

    signal_name : source name from signal_features, or 'all' for every source.
    horizon     : if given, run only this horizon; otherwise 1/2/3.
    """
    sources  = None if signal_name == "all" else [signal_name]
    horizons = _IC_HORIZONS if horizon is None else (horizon,)

    df = run_full_ic_analysis(sources=sources, horizons=horizons)
    if df.empty:
        log.warning("ic_test_empty_result", signal=signal_name)
        return

    out_dir = data_path(get_pipeline()["processed_data_subdirs"].get("signal_tables", "processed/signals"))
    out_path = out_dir / f"ic_results_{signal_name}_h{horizon or 'all'}.parquet"
    df.to_parquet(out_path, index=False)
    log.info("ic_test_done", rows=len(df), path=str(out_path))

    # Console summary — top-10 by IC mean at h=1
    h1 = df[df["horizon"] == 1].nlargest(10, "ic_mean")
    if not h1.empty:
        log.info("ic_test_top10_h1",
                 results=h1[["source", "feature_name", "ic_mean", "nw_tstat", "fdr_rejected"]].to_dict("records"))
