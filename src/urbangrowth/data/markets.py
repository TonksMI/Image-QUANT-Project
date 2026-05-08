"""Equity market data ingestion via yfinance.

Downloads daily OHLCV for the 50-stock universe + 6 benchmark ETFs.
Computes daily returns, month-end returns, and excess returns vs SPY
and vs the most relevant sector ETF for each ticker.

Benchmarks: SPY, XHB, XLI, KRE, URA, PAVE
Sector ETF mapping:
    homebuilder         -> XHB
    electrical_infra    -> XLI
    civil_construction  -> XLI
    materials           -> XLI
    equipment           -> XLI
    steel               -> XLI
    reit_industrial     -> PAVE
    reit_residential    -> XHB
    regional_bank       -> KRE
    control             -> SPY

Idempotent: loads cached parquet and only fetches new dates.
CLI: ug ingest markets [--start YYYY-MM-DD] [--end YYYY-MM-DD]
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import pandas as pd
import structlog
import yfinance as yf
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

from urbangrowth.config import data_path, get_pipeline, get_universe
from urbangrowth.db import loaders

load_dotenv()
log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BENCHMARKS: list[str] = ["SPY", "XHB", "XLI", "KRE", "URA", "PAVE"]

SECTOR_ETF: dict[str, str] = {
    "homebuilder":       "XHB",
    "electrical_infra":  "XLI",
    "civil_construction": "XLI",
    "materials":         "XLI",
    "equipment":         "XLI",
    "steel":             "XLI",
    "reit_industrial":   "PAVE",
    "reit_residential":  "XHB",
    "regional_bank":     "KRE",
    "control":           "SPY",
}

_BATCH_SIZE = 40       # tickers per yfinance call
_BATCH_SLEEP = 1.0     # seconds between batches

# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=20))
def _download(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    """Single yfinance call wrapped in tenacity retry."""
    raw = yf.download(
        tickers,
        start=start,
        end=end,
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=True,
    )
    return raw


def _ensure_multiindex(raw: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    """yfinance returns flat columns for a single ticker; wrap into MultiIndex."""
    if isinstance(raw.columns, pd.MultiIndex):
        return raw
    return raw.rename(columns=lambda c: c, level=None).pipe(
        lambda df: df.set_axis(
            pd.MultiIndex.from_tuples([(col, tickers[0]) for col in df.columns]),
            axis=1,
        )
    )


def fetch_daily_prices(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    """Download daily OHLCV + Adj Close for all tickers in batches.

    Returns a MultiIndex DataFrame: index=date, columns=(price_type, ticker).
    price_type ∈ {Open, High, Low, Close, Volume, Adj Close}
    """
    frames: list[pd.DataFrame] = []
    for i in range(0, len(tickers), _BATCH_SIZE):
        batch = tickers[i : i + _BATCH_SIZE]
        log.info("downloading_batch", offset=i, count=len(batch), tickers=batch[:5])
        try:
            raw = _download(batch, start, end)
            if raw.empty:
                log.warning("empty_batch", tickers=batch)
                continue
            raw = _ensure_multiindex(raw, batch)
            frames.append(raw)
        except Exception as exc:
            log.error("batch_download_failed", tickers=batch, error=str(exc))
        if i + _BATCH_SIZE < len(tickers):
            time.sleep(_BATCH_SLEEP)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, axis=1)
    combined.index = pd.to_datetime(combined.index)
    combined.index.name = "date"
    # Drop duplicate ticker columns (shouldn't happen, but defensive)
    combined = combined.loc[:, ~combined.columns.duplicated()]
    return combined


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _cache_path(dest: Path) -> Path:
    return dest / "prices_daily_wide.parquet"


def load_cache(dest: Path) -> pd.DataFrame:
    path = _cache_path(dest)
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_parquet(path)
        df.index = pd.to_datetime(df.index)
        log.info("loaded_price_cache", rows=len(df), tickers=df.columns.get_level_values(1).nunique())
        return df
    except Exception as exc:
        log.warning("cache_load_failed", path=str(path), error=str(exc))
        return pd.DataFrame()


def incremental_fetch(
    tickers: list[str],
    start: str,
    end: str,
    cached: pd.DataFrame,
) -> pd.DataFrame:
    """Fetch only the date range beyond what's already cached."""
    if cached.empty:
        return fetch_daily_prices(tickers, start, end)

    cached_max = cached.index.max()
    next_day = (cached_max + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    if next_day >= end:
        log.info("prices_already_current", cached_max=str(cached_max.date()))
        return cached

    log.info("incremental_fetch", from_date=next_day, to_date=end)
    fresh = fetch_daily_prices(tickers, next_day, end)
    if fresh.empty:
        return cached

    combined = pd.concat([cached, fresh])
    combined = combined[~combined.index.duplicated(keep="last")]
    combined.sort_index(inplace=True)
    return combined


# ---------------------------------------------------------------------------
# Price extraction
# ---------------------------------------------------------------------------


def adj_close_wide(price_df: pd.DataFrame) -> pd.DataFrame:
    """Extract Adj Close wide DataFrame (date × ticker).

    Falls back to Close when auto_adjust=True data is cached.
    """
    level0 = price_df.columns.get_level_values(0).unique()
    key = "Adj Close" if "Adj Close" in level0 else "Close"
    ac = price_df[key].copy()
    ac.index = pd.to_datetime(ac.index)
    return ac


def build_prices_long(price_df: pd.DataFrame) -> pd.DataFrame:
    """Pivot MultiIndex price DataFrame to long format for the DB prices table.

    Output columns: symbol, date, open, high, low, close, volume, adj_close
    """
    tickers = price_df.columns.get_level_values(1).unique()
    rows: list[pd.DataFrame] = []

    for ticker in tickers:
        try:
            t = price_df.xs(ticker, level=1, axis=1).copy()
        except KeyError:
            continue

        t.columns = [c.lower().replace(" ", "_") for c in t.columns]
        # adj_close normalisation: "adj_close" comes from "Adj Close"
        if "adj_close" not in t.columns and "close" in t.columns:
            t["adj_close"] = t["close"]

        t = t.reset_index()
        t.insert(0, "symbol", ticker)
        t = t.dropna(subset=["close"])
        if "volume" in t.columns:
            t["volume"] = pd.to_numeric(t["volume"], errors="coerce").fillna(0).astype("int64")

        keep = ["symbol", "date", "open", "high", "low", "close", "volume", "adj_close"]
        t = t[[c for c in keep if c in t.columns]]
        rows.append(t)

    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


# ---------------------------------------------------------------------------
# Return computation
# ---------------------------------------------------------------------------


def build_returns_long(
    ac: pd.DataFrame,
    universe_meta: list[dict],
) -> pd.DataFrame:
    """Build long-format returns table matching the DB schema.

    daily_ret    : simple return every trading day
    monthly_ret  : simple return on last trading day of each month; NULL otherwise
    excess_ret_spy    : daily_ret - SPY daily_ret
    excess_ret_sector : daily_ret - sector_etf daily_ret

    Output columns: symbol, date, daily_ret, monthly_ret,
                    excess_ret_spy, excess_ret_sector
    """
    daily = ac.pct_change()

    # Month-end returns: last adj_close of month, pct_change
    monthly = ac.resample("ME").last().pct_change()

    # Map each month-end date → return for fast lookup
    monthly_maps: dict[str, dict] = {
        col: monthly[col].dropna().to_dict() for col in monthly.columns
    }

    spy_daily = daily["SPY"].to_dict() if "SPY" in daily.columns else {}

    # symbol → sector ETF ticker
    sym_to_etf: dict[str, str] = {
        entry["symbol"]: SECTOR_ETF.get(entry.get("sector_tag", "control"), "SPY")
        for entry in universe_meta
    }
    for bench in BENCHMARKS:
        sym_to_etf.setdefault(bench, "SPY")

    frames: list[pd.DataFrame] = []

    for sym in ac.columns:
        sym_ret = daily[sym].dropna()
        if sym_ret.empty:
            continue

        df = pd.DataFrame({
            "symbol":    sym,
            "date":      sym_ret.index,
            "daily_ret": sym_ret.values,
        })

        # monthly_ret: non-null only on month-end dates
        mmap = monthly_maps.get(sym, {})
        df["monthly_ret"] = df["date"].map(mmap)

        # excess vs SPY
        df["excess_ret_spy"] = (
            df["daily_ret"] - df["date"].map(spy_daily)
            if spy_daily else float("nan")
        )

        # excess vs sector ETF
        etf = sym_to_etf.get(sym, "SPY")
        if etf != sym and etf in daily.columns:
            etf_map = daily[etf].to_dict()
            df["excess_ret_sector"] = df["daily_ret"] - df["date"].map(etf_map)
        else:
            df["excess_ret_sector"] = df["excess_ret_spy"]

        frames.append(df)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run(start: Optional[str] = None, end: Optional[str] = None) -> None:
    """Fetch daily prices and returns for universe + benchmarks.

    Idempotent: loads parquet cache and only calls yfinance for new dates.
    Upserts prices and returns to PostgreSQL.
    """
    pipe = get_pipeline()
    universe = get_universe()
    resolved_start: str = start or pipe["dates"]["history_start"]
    resolved_end: str   = end   or pipe["dates"]["end"]

    log.info(
        "markets_ingest_start",
        start=resolved_start,
        end=resolved_end,
        universe_size=len(universe),
    )

    # Deduplicate: XHB is already in universe as the control ticker
    universe_tickers = [s["symbol"] for s in universe]
    extra = [b for b in BENCHMARKS if b not in universe_tickers]
    all_tickers = universe_tickers + extra
    log.info("ticker_list", total=len(all_tickers), extra_benchmarks=extra)

    dest = data_path(pipe["raw_data_subdirs"]["markets"])

    # Incremental download
    cached   = load_cache(dest)
    price_df = incremental_fetch(all_tickers, resolved_start, resolved_end, cached)

    if price_df.empty:
        log.error("no_price_data_returned")
        return

    # Persist cache
    price_df.to_parquet(_cache_path(dest))
    log.info("cache_written", path=str(_cache_path(dest)), shape=price_df.shape)

    # Upsert tickers metadata
    import json
    tickers_rows = []
    for entry in universe:
        tickers_rows.append({
            "symbol":         entry["symbol"],
            "name":           entry.get("name"),
            "sector_tag":     entry.get("sector_tag"),
            "primary_signals": json.dumps(entry.get("primary_signals", [])),
        })
    tickers_df = pd.DataFrame(tickers_rows)
    loaders.upsert_df(tickers_df, "tickers", pk_cols=["symbol"])

    # Upsert prices
    prices_long = build_prices_long(price_df)
    prices_long["date"] = pd.to_datetime(prices_long["date"])
    n_prices = loaders.upsert_df(prices_long, "prices", pk_cols=["symbol", "date"])

    # Upsert returns
    ac = adj_close_wide(price_df)
    returns_long = build_returns_long(ac, universe)
    returns_long["date"] = pd.to_datetime(returns_long["date"])
    n_returns = loaders.upsert_df(returns_long, "returns", pk_cols=["symbol", "date"])

    log.info(
        "markets_ingest_complete",
        tickers=len(all_tickers),
        price_rows=n_prices,
        return_rows=n_returns,
    )
