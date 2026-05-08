"""Unit tests for backtest module."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from urbangrowth.modeling.backtest import construct_portfolio, performance_stats


def _make_scores(periods, tickers, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame([
        {"period": p, "symbol": t, "score": rng.standard_normal()}
        for p in periods
        for t in tickers
    ])


def _make_fwd_returns(periods, tickers, horizon: int = 1, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame([
        {"date": p, "symbol": t, "horizon": horizon, "fwd_return": rng.normal(0.005, 0.05)}
        for p in periods
        for t in tickers
    ])


N_TICKERS = 20
PERIODS = pd.date_range("2020-01-01", periods=12, freq="MS")
TICKERS = [f"T{i}" for i in range(N_TICKERS)]


class TestPerformanceStats:
    def test_basic_keys(self):
        stats = performance_stats(pd.Series([0.01] * 24))
        assert {"cagr", "sharpe", "max_drawdown", "hit_rate", "n_months"} <= stats.keys()

    def test_all_positive_returns(self):
        stats = performance_stats(pd.Series([0.02] * 24))
        assert stats["cagr"] > 0
        assert stats["hit_rate"] == 1.0

    def test_all_negative_returns(self):
        stats = performance_stats(pd.Series([-0.02] * 24))
        assert stats["cagr"] < 0
        assert stats["hit_rate"] == 0.0

    def test_too_short_returns_empty(self):
        stats = performance_stats(pd.Series([0.01, 0.02, 0.03]))
        assert stats == {}

    def test_n_months(self):
        stats = performance_stats(pd.Series([0.01] * 36))
        assert stats["n_months"] == 36

    def test_flat_series_zero_drawdown(self):
        # Monotone positive returns → max drawdown near zero
        stats = performance_stats(pd.Series([0.01] * 24))
        assert stats["max_drawdown"] >= -0.01

    def test_sharpe_finite(self):
        rng = np.random.default_rng(0)
        stats = performance_stats(pd.Series(rng.normal(0.005, 0.05, 36)))
        assert np.isfinite(stats["sharpe"])


class TestConstructPortfolio:
    def setup_method(self):
        self.scores = _make_scores(PERIODS, TICKERS)
        self.fwd = _make_fwd_returns(PERIODS, TICKERS)

    def test_columns(self):
        result = construct_portfolio(self.scores, self.fwd, horizon=1)
        assert {"period", "long_ret", "short_ret", "ls_ret", "ls_gross",
                "tc", "turnover", "n_long", "n_short"} <= set(result.columns)

    def test_quintile_sizes(self):
        result = construct_portfolio(self.scores, self.fwd, long_pct=0.2, short_pct=0.2)
        assert (result["n_long"] == 4).all()   # 20 * 0.2
        assert (result["n_short"] == 4).all()

    def test_tc_nonnegative(self):
        result = construct_portfolio(self.scores, self.fwd, tc_bps=10.0)
        assert (result["tc"] >= 0).all()

    def test_net_le_gross(self):
        result = construct_portfolio(self.scores, self.fwd, tc_bps=10.0)
        assert (result["ls_ret"] <= result["ls_gross"] + 1e-10).all()

    def test_zero_tc_net_equals_gross(self):
        result = construct_portfolio(self.scores, self.fwd, tc_bps=0.0)
        pd.testing.assert_series_equal(
            result["ls_ret"].reset_index(drop=True),
            result["ls_gross"].reset_index(drop=True),
            check_names=False,
        )

    def test_nonempty_output(self):
        result = construct_portfolio(self.scores, self.fwd)
        assert len(result) > 0

    def test_tc_higher_bps_lowers_net(self):
        low  = construct_portfolio(self.scores, self.fwd, tc_bps=1.0)
        high = construct_portfolio(self.scores, self.fwd, tc_bps=50.0)
        assert low["ls_ret"].sum() >= high["ls_ret"].sum() - 1e-10
