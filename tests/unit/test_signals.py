"""Unit tests for signal utility functions."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from urbangrowth.signals.utils import (
    melt_features,
    rolling_zscore,
    yoy_change,
    zscore_cross_section,
)


def _make_signal_df(n_periods: int = 6, n_tickers: int = 5, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    periods = pd.date_range("2020-01-01", periods=n_periods, freq="MS")
    records = [
        {
            "period": p,
            "symbol": f"T{i}",
            "feature_name": "feat_a",
            "feature_value": rng.standard_normal(),
        }
        for p in periods
        for i in range(n_tickers)
    ]
    return pd.DataFrame(records)


class TestZscoreCrossSection:
    def test_mean_near_zero(self):
        df = _make_signal_df(n_periods=6, n_tickers=10)
        result = zscore_cross_section(df)
        means = result.groupby(["period", "feature_name"])["feature_value"].mean()
        assert (means.abs() < 1e-10).all()

    def test_std_near_one(self):
        df = _make_signal_df(n_periods=6, n_tickers=10)
        result = zscore_cross_section(df)
        stds = result.groupby(["period", "feature_name"])["feature_value"].std()
        assert ((stds - 1).abs() < 1e-6).all()

    def test_single_observation_gives_nan(self):
        df = pd.DataFrame([{
            "period": pd.Timestamp("2020-01-01"),
            "symbol": "A",
            "feature_name": "feat",
            "feature_value": 5.0,
        }])
        result = zscore_cross_section(df)
        assert result["feature_value"].isna().all()

    def test_preserves_shape(self):
        df = _make_signal_df(n_periods=4, n_tickers=5)
        result = zscore_cross_section(df)
        assert result.shape == df.shape

    def test_does_not_modify_input(self):
        df = _make_signal_df()
        original_values = df["feature_value"].copy()
        zscore_cross_section(df)
        pd.testing.assert_series_equal(df["feature_value"], original_values)


class TestRollingZscore:
    def test_output_length(self):
        s = pd.Series(np.random.default_rng(0).standard_normal(100))
        result = rolling_zscore(s, window=24)
        assert len(result) == len(s)

    def test_constant_series_nan_or_zero(self):
        s = pd.Series([1.0] * 30)
        result = rolling_zscore(s, window=12)
        valid = result.dropna()
        assert valid.empty or (valid.abs() < 1e-10).all()

    def test_warm_up_nans(self):
        s = pd.Series(np.arange(50, dtype=float))
        result = rolling_zscore(s, window=24)
        # First min_periods - 1 entries should be NaN (min_periods = max(4, 12))
        assert result.iloc[0:11].isna().all()


class TestYoyChange:
    def test_first_twelve_nan(self):
        s = pd.Series(range(24), dtype=float)
        result = yoy_change(s)
        assert pd.isna(result.iloc[:12]).all()

    def test_value_at_12(self):
        s = pd.Series(range(24), dtype=float)
        result = yoy_change(s)
        expected = (s.iloc[12] - s.iloc[0]) / s.iloc[0]
        assert abs(result.iloc[12] - expected) < 1e-10

    def test_flat_series_gives_zero(self):
        s = pd.Series([5.0] * 24)
        result = yoy_change(s)
        assert (result.dropna() == 0.0).all()


class TestMeltFeatures:
    def test_output_columns(self):
        wide = pd.DataFrame(
            {"a": [1.0, 2.0], "b": [3.0, 4.0]},
            index=pd.to_datetime(["2020-01", "2020-02"]),
        )
        result = melt_features(wide, symbol="DHI")
        assert set(result.columns) == {"symbol", "period", "feature_name", "feature_value"}

    def test_row_count(self):
        wide = pd.DataFrame(
            {"a": [1.0, 2.0, 3.0], "b": [4.0, 5.0, 6.0]},
            index=pd.to_datetime(["2020-01", "2020-02", "2020-03"]),
        )
        result = melt_features(wide, symbol="DHI")
        assert len(result) == 6  # 3 periods × 2 features

    def test_symbol_filled(self):
        wide = pd.DataFrame({"a": [1.0]}, index=pd.to_datetime(["2020-01"]))
        result = melt_features(wide, symbol="VMC")
        assert (result["symbol"] == "VMC").all()

    def test_prefix_applied(self):
        wide = pd.DataFrame({"col": [1.0]}, index=pd.to_datetime(["2020-01"]))
        result = melt_features(wide, symbol="A", feature_prefix="pfx_")
        assert result["feature_name"].iloc[0] == "pfx_col"
