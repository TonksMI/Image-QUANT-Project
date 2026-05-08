"""Statistical diagnostics and multiple comparison correction.

Provides:
  - Newey-West HAC t-statistics for IC time series
  - Benjamini-Hochberg FDR correction for multi-signal testing
  - Autocorrelation / serial correlation diagnostics
  - Bootstrap confidence intervals for IC and Sharpe
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import scipy.stats as stats
import statsmodels.stats.multitest as multitest
import structlog

log = structlog.get_logger(__name__)


def newey_west_tstat(series: pd.Series, lags: int | None = None) -> float:
    """Compute t-statistic for mean of series using Newey-West HAC standard errors."""
    import statsmodels.api as sm
    n = len(series)
    if lags is None:
        lags = int(4 * (n / 100) ** (2 / 9))
    model = sm.OLS(series.values, np.ones(n))
    result = model.fit(cov_type="HAC", cov_kwds={"maxlags": lags})
    return float(result.tvalues[0])


def fdr_correction(p_values: pd.Series, alpha: float = 0.05) -> pd.DataFrame:
    """Apply Benjamini-Hochberg FDR correction to a Series of p-values.

    Returns DataFrame with columns: signal, p_value, p_adjusted, rejected.
    """
    reject, p_adj, _, _ = multitest.multipletests(p_values.values, alpha=alpha, method="fdr_bh")
    return pd.DataFrame({
        "signal": p_values.index,
        "p_value": p_values.values,
        "p_adjusted": p_adj,
        "rejected": reject,
    })


def bootstrap_ic_ci(
    ic_series: pd.Series,
    n_boot: int = 1000,
    ci: float = 0.95,
    random_state: int = 42,
) -> tuple[float, float]:
    """Bootstrap confidence interval for mean IC.

    Returns (lower, upper) bounds.
    """
    rng = np.random.default_rng(random_state)
    means = [rng.choice(ic_series.values, size=len(ic_series), replace=True).mean() for _ in range(n_boot)]
    lower = np.percentile(means, (1 - ci) / 2 * 100)
    upper = np.percentile(means, (1 + ci) / 2 * 100)
    return float(lower), float(upper)


def autocorrelation_lags(series: pd.Series, max_lag: int = 12) -> pd.Series:
    """Compute autocorrelation at lags 1..max_lag."""
    return pd.Series(
        {lag: series.autocorr(lag) for lag in range(1, max_lag + 1)},
        name="autocorr",
    )
