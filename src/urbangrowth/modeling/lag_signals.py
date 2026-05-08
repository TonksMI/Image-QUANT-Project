"""Granger causality, VAR, and lead-lag analysis.

Tests whether signal time series Granger-cause return series,
and fits reduced-form VAR(p) models to quantify lead-lag dynamics.
"""
from __future__ import annotations

import pandas as pd
import statsmodels.tsa.api as tsa
import structlog

log = structlog.get_logger(__name__)


def granger_test(
    signal: pd.Series,
    returns: pd.Series,
    max_lag: int = 6,
    significance: float = 0.05,
) -> dict:
    """Test whether signal Granger-causes returns up to max_lag months.

    Returns dict with keys: significant_lags (list[int]), min_p_value, f_stat.
    """
    raise NotImplementedError


def fit_var(
    panel: pd.DataFrame,
    signal_cols: list[str],
    return_col: str,
    max_lags: int = 4,
) -> tsa.VAR:
    """Fit a VAR(p) model on signal + return panel, selecting p by AIC.

    Returns a fitted VARResults object.
    """
    raise NotImplementedError


def run(signal_name: str, ticker: str) -> None:
    """CLI entry point."""
    raise NotImplementedError
