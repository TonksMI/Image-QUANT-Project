"""Shared pytest fixtures."""
from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest
import sqlalchemy as sa


@pytest.fixture(scope="session")
def db_url() -> str:
    return os.environ.get(
        "POSTGRES_TEST_URL",
        "postgresql+psycopg2://urbangrowth:changeme@localhost:5432/urbangrowth_test",
    )


@pytest.fixture(scope="session")
def db_engine(db_url: str) -> sa.Engine:
    engine = sa.create_engine(db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def sample_universe() -> list[dict]:
    from urbangrowth.config import get_universe
    return get_universe()


@pytest.fixture
def sample_returns() -> pd.DataFrame:
    """Monthly returns long DataFrame (symbol, date, monthly_ret) for testing."""
    idx = pd.period_range("2020-01", periods=24, freq="M")
    rng = np.random.default_rng(42)
    tickers = ["DHI", "LEN", "PHM", "VMC", "PWR"]
    prices = 100 * (1 + rng.normal(0.005, 0.05, size=(len(idx), len(tickers)))).cumprod(axis=0)
    price_df = pd.DataFrame(prices, index=idx.to_timestamp(), columns=tickers)
    ret_df = price_df.pct_change().dropna()
    long = ret_df.stack().reset_index()
    long.columns = ["date", "symbol", "monthly_ret"]
    return long


@pytest.fixture
def sample_prices(sample_returns: pd.DataFrame) -> pd.DataFrame:
    """Wide monthly returns DataFrame (tickers as columns, date index)."""
    return sample_returns.pivot(index="date", columns="symbol", values="monthly_ret")


@pytest.fixture
def sample_forward_returns(sample_returns: pd.DataFrame) -> pd.DataFrame:
    from urbangrowth.modeling._data import build_forward_returns
    return build_forward_returns(sample_returns, horizons=[1, 3])


@pytest.fixture
def sample_signal_scores(sample_returns: pd.DataFrame) -> pd.DataFrame:
    """Random signal scores aligned with sample_returns for IC testing."""
    rng = np.random.default_rng(99)
    records = []
    for date in sample_returns["date"].unique():
        for ticker in sample_returns["symbol"].unique():
            records.append({"date": date, "ticker": ticker, "score": rng.standard_normal()})
    return pd.DataFrame(records)
