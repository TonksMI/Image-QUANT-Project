"""Unit tests for statistical functions."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from urbangrowth.modeling.stats import (
    autocorrelation_lags,
    bootstrap_ic_ci,
    fdr_correction,
    newey_west_tstat,
)


class TestNeweyWestTstat:
    def test_large_positive_series(self):
        s = pd.Series([0.05] * 36)
        t = newey_west_tstat(s)
        assert t > 5.0

    def test_returns_float(self):
        s = pd.Series([0.01, -0.02, 0.03] * 20)
        assert isinstance(newey_west_tstat(s), float)

    def test_zero_mean_noise_small_t(self):
        rng = np.random.default_rng(42)
        s = pd.Series(rng.normal(0, 1, 120))
        t = newey_west_tstat(s)
        assert abs(t) < 3.5

    def test_negative_series_negative_t(self):
        s = pd.Series([-0.03] * 48)
        t = newey_west_tstat(s)
        assert t < 0


class TestFdrCorrection:
    def test_output_columns(self):
        p = pd.Series({"a": 0.001, "b": 0.05, "c": 0.5})
        result = fdr_correction(p)
        assert set(result.columns) >= {"signal", "p_value", "p_adjusted", "rejected"}

    def test_row_count(self):
        p = pd.Series(np.linspace(0.001, 0.5, 20))
        result = fdr_correction(p)
        assert len(result) == 20

    def test_strong_pvalue_rejected(self):
        p = pd.Series({"strong": 0.0001, "weak": 0.9})
        result = fdr_correction(p)
        assert bool(result.loc[result["signal"] == "strong", "rejected"].values[0])
        assert not bool(result.loc[result["signal"] == "weak", "rejected"].values[0])

    def test_adjusted_ge_raw(self):
        p = pd.Series(np.linspace(0.001, 0.1, 20))
        result = fdr_correction(p)
        assert (result["p_adjusted"] >= result["p_value"] - 1e-12).all()


class TestBootstrapIcCi:
    def test_lower_less_than_upper(self):
        ic = pd.Series(np.random.default_rng(0).normal(0.05, 0.02, 60))
        lo, hi = bootstrap_ic_ci(ic, n_boot=200)
        assert lo < hi

    def test_covers_known_mean(self):
        ic = pd.Series([0.04] * 36)
        lo, hi = bootstrap_ic_ci(ic, n_boot=500, ci=0.95)
        assert lo <= 0.04 + 1e-10 and hi >= 0.04 - 1e-10

    def test_returns_two_floats(self):
        ic = pd.Series(np.arange(20, dtype=float))
        result = bootstrap_ic_ci(ic, n_boot=100)
        assert isinstance(result, tuple) and len(result) == 2
        lo, hi = result
        assert isinstance(lo, float) and isinstance(hi, float)

    def test_wider_ci_gives_wider_interval(self):
        ic = pd.Series(np.random.default_rng(7).normal(0.03, 0.05, 48))
        lo95, hi95 = bootstrap_ic_ci(ic, n_boot=300, ci=0.95)
        lo68, hi68 = bootstrap_ic_ci(ic, n_boot=300, ci=0.68)
        assert (hi95 - lo95) >= (hi68 - lo68) - 1e-10


class TestAutocorrelationLags:
    def test_output_length(self):
        s = pd.Series(np.random.default_rng(0).standard_normal(60))
        result = autocorrelation_lags(s, max_lag=12)
        assert len(result) == 12

    def test_index_range(self):
        s = pd.Series(np.random.default_rng(0).standard_normal(60))
        result = autocorrelation_lags(s, max_lag=6)
        assert list(result.index) == [1, 2, 3, 4, 5, 6]

    def test_ar1_has_lag1_autocorr(self):
        rng = np.random.default_rng(42)
        e = rng.normal(0, 1, 200)
        s = pd.Series(np.cumsum(e) * 0.1)
        result = autocorrelation_lags(s, max_lag=3)
        assert result.iloc[0] > 0.5  # positive autocorrelation at lag 1
