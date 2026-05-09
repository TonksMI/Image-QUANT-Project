"""Database loaders — bulk upsert pandas DataFrames to PostgreSQL.

All loaders are idempotent: they use INSERT ... ON CONFLICT DO UPDATE
so re-running a load skips existing rows rather than duplicating them.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from urbangrowth.config import get_pipeline

log = structlog.get_logger(__name__)


def _engine() -> sa.Engine:
    pipe = get_pipeline()
    user = os.environ.get("POSTGRES_USER", "urbangrowth")
    pwd  = os.environ.get("POSTGRES_PASSWORD", "")
    host = pipe["db"]["host"]
    port = pipe["db"]["port"]
    db   = pipe["db"]["dbname"]
    url  = f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{db}"
    return sa.create_engine(url, pool_pre_ping=True)


@contextmanager
def connection():
    """Yield a SQLAlchemy connection."""
    engine = _engine()
    with engine.begin() as conn:
        yield conn


def upsert_df(df: pd.DataFrame, table: str, pk_cols: list[str]) -> int:
    """Bulk upsert a DataFrame into table, updating non-PK columns on conflict.

    Returns number of rows processed.
    """
    if df.empty:
        return 0
    engine = _engine()
    meta = sa.MetaData()
    meta.reflect(bind=engine, only=[table])
    tbl = meta.tables[table]
    non_pk = [c for c in df.columns if c not in pk_cols]

    with engine.begin() as conn:
        for batch in (df[i:i+1000] for i in range(0, len(df), 1000)):
            stmt = pg_insert(tbl).values(batch.to_dict("records"))
            if non_pk:
                stmt = stmt.on_conflict_do_update(
                    index_elements=pk_cols,
                    set_={c: stmt.excluded[c] for c in non_pk},
                )
            else:
                stmt = stmt.on_conflict_do_nothing()
            conn.execute(stmt)
    log.info("upserted", table=table, rows=len(df))
    return len(df)


def load_bps_national(df: pd.DataFrame) -> int:
    return upsert_df(df, "census_bps_national", ["date", "structure_type"])


def load_bps_metro(df: pd.DataFrame) -> int:
    return upsert_df(df, "census_bps_metro", ["date", "msa_code", "structure_type"])


def load_fred(df: pd.DataFrame) -> int:
    long = df.reset_index().melt(id_vars="date", var_name="series_id", value_name="value")
    long["date"] = long["date"].dt.to_timestamp()
    return upsert_df(long, "fred_series", ["date", "series_id"])


def load_city_permits(df: pd.DataFrame, city: str) -> int:
    df = df.copy()
    df["city"] = city
    return upsert_df(df, "city_permits", ["permit_number", "city"])


def load_signal_scores(df: pd.DataFrame, signal_name: str) -> int:
    df = df.copy()
    df["signal_name"] = signal_name
    df["date"] = df["date"].dt.to_timestamp()
    return upsert_df(df, "signal_scores", ["date", "ticker", "signal_name"])


def load_forward_returns(df: pd.DataFrame) -> int:
    df = df.copy()
    df["date"] = df["date"].dt.to_timestamp()
    return upsert_df(df, "forward_returns", ["date", "ticker", "horizon"])
