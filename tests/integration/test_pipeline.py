"""Integration tests: full pipeline on synthetic in-memory data.

No database required — all data is generated in-memory.
Tests the contract between each pipeline stage.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from urbangrowth.modeling._data import build_forward_returns, build_momentum_signal
from urbangrowth.modeling.backtest import construct_portfolio, performance_stats
from urbangrowth.modeling.cross_section import aggregate_fmb, fama_macbeth
from urbangrowth.modeling.stats import bootstrap_ic_ci, fdr_correction, newey_west_tstat

N_TICKERS = 30
N_PERIODS = 36
TICKERS = [f"T{i:02d}" for i in range(N_TICKERS)]
DATES = pd.date_range("2020-01-01", periods=N_PERIODS, freq="MS")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def synthetic_returns() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    return pd.DataFrame([
        {"date": d, "symbol": t, "monthly_ret": rng.normal(0.005, 0.05)}
        for d in DATES
        for t in TICKERS
    ])


@pytest.fixture(scope="module")
def forward_returns(synthetic_returns: pd.DataFrame) -> pd.DataFrame:
    return build_forward_returns(synthetic_returns, horizons=[1, 3])


@pytest.fixture(scope="module")
def momentum(synthetic_returns: pd.DataFrame) -> pd.DataFrame:
    return build_momentum_signal(synthetic_returns, window=12)


@pytest.fixture(scope="module")
def synthetic_scores(synthetic_returns: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(99)
    return pd.DataFrame([
        {"period": d, "symbol": t, "score": rng.standard_normal()}
        for d in DATES
        for t in TICKERS
    ])


# ---------------------------------------------------------------------------
# Returns pipeline
# ---------------------------------------------------------------------------


def test_forward_returns_shape(forward_returns: pd.DataFrame):
    assert len(forward_returns) > 0
    assert set(forward_returns.columns) == {"symbol", "date", "horizon", "fwd_return"}


def test_forward_returns_horizons(forward_returns: pd.DataFrame):
    assert set(forward_returns["horizon"].unique()) == {1, 3}


def test_forward_returns_tickers(forward_returns: pd.DataFrame, synthetic_returns: pd.DataFrame):
    assert set(forward_returns["symbol"].unique()) == set(synthetic_returns["symbol"].unique())


def test_forward_returns_no_inf(forward_returns: pd.DataFrame):
    assert np.isfinite(forward_returns["fwd_return"].dropna()).all()


# ---------------------------------------------------------------------------
# Momentum signal
# ---------------------------------------------------------------------------


def test_momentum_columns(momentum: pd.DataFrame):
    assert set(momentum.columns) == {"symbol", "period", "feature_name", "feature_value"}


def test_momentum_feature_name(momentum: pd.DataFrame):
    assert (momentum["feature_name"] == "momentum_12m").all()


def test_momentum_nonnull_values_exist(momentum: pd.DataFrame):
    assert len(momentum.dropna(subset=["feature_value"])) > 0


def test_momentum_tickers(momentum: pd.DataFrame):
    assert set(momentum["symbol"].unique()) == set(TICKERS)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_newey_west_on_realistic_ic():
    rng = np.random.default_rng(0)
    ic_series = pd.Series(rng.normal(0.04, 0.10, 48))
    t = newey_west_tstat(ic_series)
    assert isinstance(t, float) and np.isfinite(t)


def test_fdr_correction_length():
    rng = np.random.default_rng(1)
    pvals = pd.Series(rng.uniform(0, 1, 50))
    result = fdr_correction(pvals)
    assert len(result) == 50


def test_bootstrap_ic_ci_interval():
    rng = np.random.default_rng(5)
    ic = pd.Series(rng.normal(0.03, 0.05, 48))
    lo, hi = bootstrap_ic_ci(ic, n_boot=300)
    assert lo < hi and np.isfinite(lo) and np.isfinite(hi)


# ---------------------------------------------------------------------------
# Portfolio construction
# ---------------------------------------------------------------------------


def test_construct_portfolio_runs(synthetic_scores: pd.DataFrame, forward_returns: pd.DataFrame):
    portfolio = construct_portfolio(
        synthetic_scores, forward_returns,
        long_pct=0.2, short_pct=0.2, horizon=1, tc_bps=10.0,
    )
    assert len(portfolio) > 0
    assert "ls_ret" in portfolio.columns


def test_performance_stats_on_portfolio(
    synthetic_scores: pd.DataFrame, forward_returns: pd.DataFrame
):
    portfolio = construct_portfolio(
        synthetic_scores, forward_returns,
        long_pct=0.2, short_pct=0.2, horizon=1, tc_bps=10.0,
    )
    ls = portfolio.set_index("period")["ls_ret"]
    stats = performance_stats(ls)
    assert "cagr" in stats
    assert np.isfinite(stats["sharpe"])


def test_tc_reduces_net_return(
    synthetic_scores: pd.DataFrame, forward_returns: pd.DataFrame
):
    port_10  = construct_portfolio(synthetic_scores, forward_returns, tc_bps=10.0, horizon=1)
    port_0   = construct_portfolio(synthetic_scores, forward_returns, tc_bps=0.0,  horizon=1)
    assert port_10["ls_ret"].sum() <= port_0["ls_ret"].sum() + 1e-10


def test_turnover_between_0_and_1(
    synthetic_scores: pd.DataFrame, forward_returns: pd.DataFrame
):
    portfolio = construct_portfolio(
        synthetic_scores, forward_returns, long_pct=0.2, short_pct=0.2, horizon=1
    )
    assert (portfolio["turnover"] >= 0).all()
    assert (portfolio["turnover"] <= 2.0 + 1e-6).all()


# ---------------------------------------------------------------------------
# Fama-MacBeth end-to-end
# ---------------------------------------------------------------------------


def test_fmb_pipeline(forward_returns: pd.DataFrame):
    rng = np.random.default_rng(7)
    signals = pd.DataFrame([
        {"period": d, "symbol": t, "sig": rng.standard_normal()}
        for d in DATES
        for t in TICKERS
    ])
    slopes = fama_macbeth(signals, forward_returns, signal_cols=["sig"], horizon=1, min_stocks=10)
    agg = aggregate_fmb(slopes)
    assert len(slopes) > 0
    assert len(agg) > 0
    assert 0 <= float(agg["p_value"].iloc[0]) <= 1


def test_fmb_two_signals(forward_returns: pd.DataFrame):
    rng = np.random.default_rng(8)
    signals = pd.DataFrame([
        {"period": d, "symbol": t, "s1": rng.standard_normal(), "s2": rng.standard_normal()}
        for d in DATES
        for t in TICKERS
    ])
    slopes = fama_macbeth(signals, forward_returns, signal_cols=["s1", "s2"], horizon=1, min_stocks=10)
    agg = aggregate_fmb(slopes)
    assert {"s1", "s2"} == set(agg["signal"].unique())
