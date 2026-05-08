"""Long/short signal backtest.

Each month:
  1. Rank universe by walk-forward model score
  2. Long top-quintile, short bottom-quintile (equal-weight)
  3. Compute portfolio return net of 10 bps per side transaction costs

Reports:
  - CAGR, Sharpe, max drawdown, hit rate, turnover
  - Benchmark comparison (SPY, XHB, PAVE) from returns table
  - Fama-French 3-factor + momentum exposure (via pandas_datareader)
  - Sector decomposition

CLI:  ug model backtest --model ridge --horizon 1
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import statsmodels.api as sm
import structlog

from urbangrowth.config import data_path, get_pipeline
from urbangrowth.modeling._data import (
    build_forward_returns,
    build_momentum_signal,
    build_volume_signal,
    load_monthly_returns,
)

log = structlog.get_logger(__name__)

_BENCHMARKS = ("SPY", "XHB", "PAVE")


# ---------------------------------------------------------------------------
# Portfolio construction
# ---------------------------------------------------------------------------


def construct_portfolio(
    scores: pd.DataFrame,
    returns: pd.DataFrame,
    long_pct: float = 0.2,
    short_pct: float = 0.2,
    horizon: int = 1,
    tc_bps: float = 10.0,
) -> pd.DataFrame:
    """Build monthly L/S portfolio returns from walk-forward signal scores.

    Parameters
    ----------
    scores  : (period, symbol, score) — output of walk_forward_predict
    returns : long (symbol, date, horizon, fwd_return) from build_forward_returns
    long_pct  : fraction of universe in long leg
    short_pct : fraction of universe in short leg
    horizon   : forward return horizon in months
    tc_bps    : one-sided transaction cost in basis points (10 bps/side → 20 bps round-trip)

    Returns
    -------
    DataFrame: period, long_ret, short_ret, ls_ret (net), ls_gross, tc, turnover,
               n_long, n_short
    """
    # Normalise returns column names
    fwd = returns[returns["horizon"] == horizon][["date", "symbol", "fwd_return"]].copy()
    if "ticker" in fwd.columns:
        fwd = fwd.rename(columns={"ticker": "symbol"})
    fwd = fwd.rename(columns={"date": "period"})

    results = []
    prev_long_w:  dict[str, float] = {}
    prev_short_w: dict[str, float] = {}

    for period, grp in scores.groupby("period"):
        grp = grp.dropna(subset=["score"])
        n = len(grp)
        if n < 5:
            continue

        grp = grp.sort_values("score", ascending=False).reset_index(drop=True)
        n_long  = max(1, int(n * long_pct))
        n_short = max(1, int(n * short_pct))

        long_syms  = grp.iloc[:n_long]["symbol"].tolist()
        short_syms = grp.iloc[-n_short:]["symbol"].tolist()

        long_w  = {s: 1.0 / n_long  for s in long_syms}
        short_w = {s: 1.0 / n_short for s in short_syms}

        period_rets = fwd[fwd["period"] == period].set_index("symbol")["fwd_return"]

        long_ret  = float(sum(period_rets.get(s, 0.0) * w for s, w in long_w.items()))
        short_ret = float(sum(period_rets.get(s, 0.0) * w for s, w in short_w.items()))
        ls_gross  = long_ret - short_ret

        # Transaction cost: 10 bps per side × total absolute weight change
        # Sum of |delta_w| counts both entry (buy) and exit (sell) legs;
        # each dollar rebalanced costs tc_bps on the way in AND the way out.
        all_syms    = set(long_w) | set(short_w) | set(prev_long_w) | set(prev_short_w)
        long_delta  = sum(abs(long_w.get(s, 0.0) - prev_long_w.get(s, 0.0))  for s in all_syms)
        short_delta = sum(abs(short_w.get(s, 0.0) - prev_short_w.get(s, 0.0)) for s in all_syms)
        turnover    = (long_delta + short_delta) / 2          # one-way, fraction of NAV
        tc          = tc_bps / 10_000 * (long_delta + short_delta)

        results.append({
            "period":    period,
            "long_ret":  long_ret,
            "short_ret": short_ret,
            "ls_ret":    ls_gross - tc,
            "ls_gross":  ls_gross,
            "tc":        tc,
            "turnover":  turnover,
            "n_long":    n_long,
            "n_short":   n_short,
        })

        prev_long_w  = long_w
        prev_short_w = short_w

    return pd.DataFrame(results)


# ---------------------------------------------------------------------------
# Performance statistics
# ---------------------------------------------------------------------------


def performance_stats(returns_series: pd.Series) -> dict:
    """Compute CAGR, annualised Sharpe, max drawdown, hit rate."""
    monthly = returns_series.dropna()
    if len(monthly) < 6:
        return {}
    ann_ret  = float((1 + monthly).prod() ** (12 / len(monthly)) - 1)
    ann_vol  = float(monthly.std() * 12 ** 0.5)
    sharpe   = ann_ret / ann_vol if ann_vol > 0 else float("nan")
    cum      = (1 + monthly).cumprod()
    drawdown = float((cum / cum.cummax() - 1).min())
    hit_rate = float((monthly > 0).mean())
    avg_turn = float(
        returns_series.to_frame("ret")
        .assign(turnover=np.nan)
        .get("turnover", pd.Series(dtype=float))
        .mean()
    )
    return {
        "cagr":         ann_ret,
        "ann_vol":      ann_vol,
        "sharpe":       sharpe,
        "max_drawdown": drawdown,
        "hit_rate":     hit_rate,
        "n_months":     len(monthly),
    }


def _portfolio_stats(portfolio: pd.DataFrame) -> dict:
    """Performance stats including turnover, extracted from portfolio DataFrame."""
    stats = performance_stats(portfolio.set_index("period")["ls_ret"])
    stats["avg_turnover"] = float(portfolio["turnover"].mean())
    stats["avg_tc_bps"]   = float(portfolio["tc"].mean() * 10_000)
    return stats


# ---------------------------------------------------------------------------
# Benchmark comparison
# ---------------------------------------------------------------------------


def benchmark_comparison(
    portfolio: pd.DataFrame,
    raw_returns: pd.DataFrame,
) -> pd.DataFrame:
    """Compare L/S portfolio to SPY, XHB, PAVE benchmarks.

    raw_returns : (symbol, date, monthly_ret) from load_monthly_returns
    Returns DataFrame: entity, cagr, ann_vol, sharpe, max_drawdown, hit_rate, n_months
    """
    rows = []

    # L/S portfolio
    ls = portfolio.set_index("period")["ls_ret"]
    s  = performance_stats(ls)
    s["entity"] = "L/S Portfolio"
    rows.append(s)

    # Long-only leg
    lo = portfolio.set_index("period")["long_ret"]
    s  = performance_stats(lo)
    s["entity"] = "Long Leg"
    rows.append(s)

    # Benchmarks
    bench = (
        raw_returns[raw_returns["symbol"].isin(_BENCHMARKS)]
        .pivot(index="date", columns="symbol", values="monthly_ret")
    )
    for sym in _BENCHMARKS:
        if sym not in bench.columns:
            continue
        series = bench[sym].dropna()
        # Align to portfolio period range
        series = series[
            (series.index >= portfolio["period"].min()) &
            (series.index <= portfolio["period"].max())
        ]
        s = performance_stats(series)
        s["entity"] = sym
        rows.append(s)

    df = pd.DataFrame(rows)
    col_order = ["entity", "cagr", "ann_vol", "sharpe", "max_drawdown", "hit_rate", "n_months"]
    return df[[c for c in col_order if c in df.columns]]


# ---------------------------------------------------------------------------
# Factor exposure (Fama-French 3 + Momentum)
# ---------------------------------------------------------------------------


def factor_exposures(portfolio_rets: pd.Series) -> pd.DataFrame | None:
    """Regress L/S returns on Fama-French 3 + Momentum factors.

    Fetches monthly factors from Kenneth French data library via
    pandas_datareader.  Returns None gracefully if unavailable.

    Returns DataFrame: factor, beta, tstat, pvalue
    """
    try:
        import pandas_datareader.data as pdr
        start = portfolio_rets.index.min()
        ff3 = pdr.DataReader("F-F_Research_Data_Factors", "famafrench", start=start)[0] / 100.0
        umd = pdr.DataReader("F-F_Momentum_Factor",       "famafrench", start=start)[0] / 100.0
    except Exception as exc:
        log.warning("ff_factors_unavailable", error=str(exc))
        return None

    # Convert PeriodIndex → Timestamp (month-end)
    ff3.index = ff3.index.to_timestamp("M")
    umd.index = umd.index.to_timestamp("M")

    factors = ff3.join(umd, how="inner")
    # Fix whitespace in column names from French data library
    factors.columns = [c.strip() for c in factors.columns]

    p = portfolio_rets.rename("ret").to_frame()
    p.index = pd.to_datetime(p.index).to_period("M").to_timestamp("M")

    merged = p.join(factors, how="inner").dropna()
    if len(merged) < 12:
        log.warning("ff_too_few_obs", n=len(merged))
        return None

    factor_cols = [c for c in ["Mkt-RF", "SMB", "HML", "Mom"] if c in merged.columns]
    merged["excess_ret"] = merged["ret"] - merged.get("RF", 0.0)

    X = sm.add_constant(merged[factor_cols])
    y = merged["excess_ret"]
    result = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": 3})

    rows = []
    for name, coef, tval, pval in zip(
        result.params.index, result.params.values,
        result.tvalues.values, result.pvalues.values,
    ):
        rows.append({"factor": name, "beta": float(coef),
                     "tstat": float(tval), "pvalue": float(pval)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Sector decomposition
# ---------------------------------------------------------------------------


def sector_decomposition(
    scores: pd.DataFrame,
    fwd_returns: pd.DataFrame,
    long_pct: float = 0.2,
    short_pct: float = 0.2,
    horizon: int = 1,
) -> pd.DataFrame:
    """Attribute L/S return to sectors via average monthly contribution.

    Returns DataFrame: sector, side (long/short), avg_weight, avg_contribution
    """
    try:
        from urbangrowth.signals.ticker_mapping import _universe
        sector_map = {t["symbol"]: t.get("sector", "unknown") for t in _universe()}
    except Exception:
        sector_map = {}

    fwd_h = (
        fwd_returns[fwd_returns["horizon"] == horizon][["symbol", "date", "fwd_return"]]
        .rename(columns={"date": "period"})
    )

    rows = []
    for period, grp in scores.groupby("period"):
        grp = grp.dropna(subset=["score"]).sort_values("score", ascending=False).reset_index(drop=True)
        n = len(grp)
        if n < 5:
            continue
        n_long  = max(1, int(n * long_pct))
        n_short = max(1, int(n * short_pct))

        rets_t = fwd_h[fwd_h["period"] == period].set_index("symbol")["fwd_return"]

        for side, syms, sign in [("long",  grp.iloc[:n_long]["symbol"].tolist(),  1),
                                  ("short", grp.iloc[-n_short:]["symbol"].tolist(), -1)]:
            w = 1.0 / len(syms)
            for sym in syms:
                ret = float(rets_t.get(sym, 0.0))
                rows.append({
                    "period":       period,
                    "symbol":       sym,
                    "sector":       sector_map.get(sym, "unknown"),
                    "side":         side,
                    "weight":       w,
                    "contribution": sign * w * ret,
                })

    if not rows:
        return pd.DataFrame()

    detail = pd.DataFrame(rows)
    summary = (
        detail
        .groupby(["sector", "side"])
        .agg(avg_weight=("weight", "mean"), avg_contribution=("contribution", "mean"))
        .reset_index()
        .sort_values("avg_contribution", ascending=False)
        .reset_index(drop=True)
    )
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run(model_type: str = "ridge", horizon: int = 1) -> None:
    """Load walk-forward scores (computing if needed), construct L/S portfolio,
    run full analysis, and save results.

    Outputs:
      processed/signals/backtest_{model_type}_h{horizon}.parquet
      processed/signals/backtest_{model_type}_h{horizon}_sector.parquet
      processed/signals/backtest_{model_type}_h{horizon}_factors.parquet  (if FF data available)
    """
    from urbangrowth.modeling.cross_section import run_cross_sectional_model

    out_dir = data_path(
        get_pipeline()["processed_data_subdirs"].get("signal_tables", "processed/signals")
    )

    scores_path = out_dir / f"model_scores_{model_type}_h{horizon}.parquet"
    if scores_path.exists():
        scores = pd.read_parquet(scores_path)
        log.info("backtest_scores_loaded", path=str(scores_path), n=len(scores))
    else:
        scores = run_cross_sectional_model(model_type=model_type, horizon=horizon)

    if scores.empty:
        log.warning("backtest_no_scores", model=model_type, horizon=horizon)
        return

    raw_returns = load_monthly_returns()
    fwd_returns = build_forward_returns(raw_returns, [horizon])

    portfolio = construct_portfolio(scores, fwd_returns, horizon=horizon, tc_bps=10.0)
    if portfolio.empty:
        log.warning("backtest_empty_portfolio")
        return

    stats = _portfolio_stats(portfolio)
    log.info("backtest_performance", model=model_type, horizon=horizon,
             **{k: round(v, 4) for k, v in stats.items() if isinstance(v, (int, float))})

    # Save portfolio
    out_path = out_dir / f"backtest_{model_type}_h{horizon}.parquet"
    portfolio.to_parquet(out_path, index=False)
    log.info("backtest_portfolio_saved", path=str(out_path))

    # Benchmark comparison
    bench_df = benchmark_comparison(portfolio, raw_returns)
    log.info("backtest_benchmarks", results=bench_df.to_dict("records"))
    bench_df.to_parquet(out_dir / f"backtest_{model_type}_h{horizon}_bench.parquet", index=False)

    # Sector decomposition
    sector_df = sector_decomposition(scores, fwd_returns, horizon=horizon)
    if not sector_df.empty:
        sector_df.to_parquet(out_dir / f"backtest_{model_type}_h{horizon}_sector.parquet", index=False)
        log.info("backtest_sector_done", rows=len(sector_df))

    # Factor exposures (optional — requires internet + pandas_datareader)
    ls_series = portfolio.set_index("period")["ls_ret"]
    ff_df = factor_exposures(ls_series)
    if ff_df is not None:
        ff_df.to_parquet(out_dir / f"backtest_{model_type}_h{horizon}_factors.parquet", index=False)
        alpha_row = ff_df[ff_df["factor"] == "const"]
        if not alpha_row.empty:
            alpha_ann = float(alpha_row["beta"].iloc[0]) * 12
            alpha_t   = float(alpha_row["tstat"].iloc[0])
            log.info("backtest_alpha", ann_alpha=round(alpha_ann, 4), tstat=round(alpha_t, 2))
