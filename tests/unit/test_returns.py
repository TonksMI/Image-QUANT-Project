"""Unit tests for return computation helpers."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from urbangrowth.modeling._data import build_forward_returns, build_momentum_signal


def _make_returns(n_periods: int = 24, n_tickers: int = 5, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2020-01-01", periods=n_periods, freq="MS")
    tickers = [f"T{i}" for i in range(n_tickers)]
    return pd.DataFrame([
        {"date": d, "symbol": t, "monthly_ret": rng.normal(0.005, 0.05)}
        for d in dates
        for t in tickers
    ])


class TestBuildForwardReturns:
    def test_columns(self):
        df = _make_returns()
        result = build_forward_returns(df, horizons=[1, 3])
        assert set(result.columns) == {"symbol", "date", "horizon", "fwd_return"}

    def test_horizons_present(self):
        df = _make_returns()
        result = build_forward_returns(df, horizons=[1, 3])
        assert set(result["horizon"].unique()) == {1, 3}

    def test_h1_equals_next_month_return(self):
        """For h=1, fwd_return(t) should equal monthly_ret at t+1."""
        df = _make_returns(n_periods=6, n_tickers=1)
        result = build_forward_returns(df, horizons=[1])
        dates = sorted(df["date"].unique())
        t0, t1 = dates[0], dates[1]
        r_t1 = float(df[df["date"] == t1]["monthly_ret"].iloc[0])
        fwd_t0 = float(
            result[(result["date"] == t0) & (result["horizon"] == 1)]["fwd_return"].iloc[0]
        )
        assert abs(fwd_t0 - r_t1) < 1e-8

    def test_empty_input(self):
        empty = pd.DataFrame(columns=["symbol", "date", "monthly_ret"])
        result = build_forward_returns(empty, horizons=[1])
        assert result.empty
        assert set(result.columns) == {"symbol", "date", "horizon", "fwd_return"}

    def test_compounding_two_periods(self):
        """h=2 fwd_return(t) = compound of returns at t+1 and t+2."""
        r1, r2 = 0.02, 0.03
        dates = pd.date_range("2020-01-01", periods=4, freq="MS")
        # fwd(dates[0], h=2) = compound of dates[1] and dates[2] returns
        df = pd.DataFrame([
            {"date": dates[0], "symbol": "A", "monthly_ret": 0.01},
            {"date": dates[1], "symbol": "A", "monthly_ret": r1},
            {"date": dates[2], "symbol": "A", "monthly_ret": r2},
            {"date": dates[3], "symbol": "A", "monthly_ret": 0.01},
        ])
        result = build_forward_returns(df, horizons=[2])
        fwd = float(
            result[(result["date"] == dates[0]) & (result["horizon"] == 2)]["fwd_return"].iloc[0]
        )
        expected = (1 + r1) * (1 + r2) - 1
        assert abs(fwd - expected) < 1e-6

    def test_symbols_preserved(self):
        df = _make_returns(n_tickers=5)
        result = build_forward_returns(df, horizons=[1])
        assert set(result["symbol"].unique()) == set(df["symbol"].unique())


class TestBuildMomentumSignal:
    def test_columns(self):
        df = _make_returns()
        result = build_momentum_signal(df, window=12)
        assert set(result.columns) == {"symbol", "period", "feature_name", "feature_value"}

    def test_feature_name(self):
        df = _make_returns()
        result = build_momentum_signal(df, window=12)
        assert (result["feature_name"] == "momentum_12m").all()

    def test_empty_input(self):
        empty = pd.DataFrame(columns=["symbol", "date", "monthly_ret"])
        result = build_momentum_signal(empty)
        assert result.empty

    def test_nonnull_values_exist(self):
        df = _make_returns(n_periods=24)
        result = build_momentum_signal(df, window=12)
        non_null = result.dropna(subset=["feature_value"])
        assert len(non_null) > 0

    def test_warm_up_nans_for_short_history(self):
        # stack(dropna=True) drops warm-up NaN rows rather than keeping them,
        # so check that fewer rows are returned than total input periods.
        df = _make_returns(n_periods=8, n_tickers=1)
        result = build_momentum_signal(df, window=12)
        assert len(result) < 8
