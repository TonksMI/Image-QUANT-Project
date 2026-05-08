"""Unit tests for cross-section model helpers."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from urbangrowth.modeling.cross_section import aggregate_fmb, fama_macbeth


N_TICKERS = 20
N_PERIODS = 24


def _make_fmb_data(
    n_periods: int = N_PERIODS,
    n_tickers: int = N_TICKERS,
    seed: int = 0,
):
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2020-01-01", periods=n_periods, freq="MS")
    tickers = [f"T{i}" for i in range(n_tickers)]

    signals = pd.DataFrame([
        {"period": d, "symbol": t, "sig": rng.standard_normal()}
        for d in dates
        for t in tickers
    ])
    returns = pd.DataFrame([
        {"date": d, "symbol": t, "horizon": 1, "fwd_return": rng.normal(0.005, 0.05)}
        for d in dates
        for t in tickers
    ])
    return signals, returns


class TestFamaMacbeth:
    def test_output_columns(self):
        signals, returns = _make_fmb_data()
        result = fama_macbeth(signals, returns, signal_cols=["sig"], horizon=1, min_stocks=10)
        assert "date" in result.columns
        assert "sig" in result.columns
        assert "r_squared" in result.columns

    def test_nonempty_output(self):
        signals, returns = _make_fmb_data()
        result = fama_macbeth(signals, returns, signal_cols=["sig"], horizon=1, min_stocks=10)
        assert len(result) > 0

    def test_min_stocks_filter(self):
        signals, returns = _make_fmb_data(n_tickers=5)
        result = fama_macbeth(signals, returns, signal_cols=["sig"], horizon=1, min_stocks=20)
        assert result.empty

    def test_one_row_per_date(self):
        signals, returns = _make_fmb_data()
        result = fama_macbeth(signals, returns, signal_cols=["sig"], horizon=1, min_stocks=5)
        assert result["date"].nunique() == len(result)

    def test_r_squared_between_0_and_1(self):
        signals, returns = _make_fmb_data()
        result = fama_macbeth(signals, returns, signal_cols=["sig"], horizon=1, min_stocks=5)
        assert (result["r_squared"] >= -0.01).all()
        assert (result["r_squared"] <= 1.01).all()


class TestAggregateFmb:
    def test_output_columns(self):
        signals, returns = _make_fmb_data()
        slopes = fama_macbeth(signals, returns, signal_cols=["sig"], horizon=1, min_stocks=5)
        agg = aggregate_fmb(slopes)
        assert {"signal", "mean_slope", "std_slope", "nw_tstat", "p_value", "n_months"} <= set(
            agg.columns
        )

    def test_one_row_per_signal(self):
        signals, returns = _make_fmb_data()
        slopes = fama_macbeth(signals, returns, signal_cols=["sig"], horizon=1, min_stocks=5)
        agg = aggregate_fmb(slopes)
        assert len(agg) == 1
        assert agg["signal"].iloc[0] == "sig"

    def test_empty_input(self):
        empty = pd.DataFrame(columns=["date", "sig", "r_squared"])
        result = aggregate_fmb(empty)
        assert result.empty

    def test_p_value_in_0_1(self):
        signals, returns = _make_fmb_data()
        slopes = fama_macbeth(signals, returns, signal_cols=["sig"], horizon=1, min_stocks=5)
        agg = aggregate_fmb(slopes)
        assert ((agg["p_value"] >= 0) & (agg["p_value"] <= 1)).all()

    def test_n_months_correct(self):
        signals, returns = _make_fmb_data(n_periods=N_PERIODS)
        slopes = fama_macbeth(signals, returns, signal_cols=["sig"], horizon=1, min_stocks=5)
        agg = aggregate_fmb(slopes)
        assert agg["n_months"].iloc[0] == len(slopes)
