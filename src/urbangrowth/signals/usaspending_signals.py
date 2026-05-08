"""USASpending federal contract award signals per ticker.

Features written to signal_features (source = 'usaspending'):
  usaspend_direct_3m         — trailing 3m award flow (USD) to matched ticker
  usaspend_direct_6m         — trailing 6m award flow
  usaspend_direct_12m        — trailing 12m award flow
  usaspend_direct_yoy        — YoY delta in trailing-12m flow
  usaspend_sector_naics_3m   — sector+geo weighted NAICS award flow, 3m
  usaspend_sector_naics_12m  — sector+geo weighted NAICS award flow, 12m

Direct matching uses TICKER_RECIPIENT_MAP (name-fragment matching against
recipient_name column). Sector-level fallback applies for tickers without
direct recipients — it sums NAICS-matched awards, weighted by the ticker's
state geographic_concentration.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import structlog
from sqlalchemy import text

from urbangrowth.config import data_path, get_pipeline
from urbangrowth.db.loaders import _engine, upsert_df
from urbangrowth.signals.ticker_mapping import (
    SECTOR_NAICS,
    geographic_weights,
    sector_for_symbol,
    tickers_for_signal,
)
from urbangrowth.signals.utils import filter_date_range, yoy_change, zscore_cross_section

log = structlog.get_logger(__name__)

_SOURCE = "usaspending"

# Ticker → company name fragments for recipient matching
TICKER_RECIPIENT_MAP: dict[str, list[str]] = {
    "PWR":  ["quanta services"],
    "EME":  ["emcor"],
    "FIX":  ["comfort systems"],
    "ACM":  ["aecom"],
    "KBR":  ["kbr"],
    "J":    ["jacobs", "jacobs engineering"],
    "STRL": ["sterling", "sterling construction"],
    "ROAD": ["construction partners"],
    "GVA":  ["granite construction"],
    "CAT":  ["caterpillar"],
    "NUE":  ["nucor"],
    "CMC":  ["commercial metals"],
    "VMC":  ["vulcan materials"],
    "MLM":  ["martin marietta"],
    "PRIM": ["primoris"],
    "MYR":  ["myr group", "harlan electric", "sturgeon electric"],
}


# ---------------------------------------------------------------------------
# DB reads
# ---------------------------------------------------------------------------


def _load_awards(engine) -> pd.DataFrame:
    sql = text("""
        SELECT award_id, award_date, naics, recipient_name,
               recipient_ticker_guess, amount, state
        FROM   usaspending_awards
        WHERE  award_date IS NOT NULL
          AND  amount > 0
        ORDER  BY award_date
    """)
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, parse_dates=["award_date"])
    df["month"] = df["award_date"].dt.to_period("M")
    df["naics4"] = df["naics"].str[:4].fillna("")
    return df


# ---------------------------------------------------------------------------
# Recipient matching
# ---------------------------------------------------------------------------


def _match_recipients(awards: pd.DataFrame) -> pd.DataFrame:
    """Assign ticker to awards using TICKER_RECIPIENT_MAP name fragments."""
    awards = awards.copy()
    name_lower = awards["recipient_name"].str.lower().fillna("")

    # Use existing ticker_guess where set; otherwise try name match
    ticker_col = awards["recipient_ticker_guess"].copy()
    for ticker, fragments in TICKER_RECIPIENT_MAP.items():
        mask = ticker_col.isna() & name_lower.apply(
            lambda n: any(frag in n for frag in fragments)
        )
        ticker_col[mask] = ticker

    awards["ticker"] = ticker_col
    return awards


# ---------------------------------------------------------------------------
# Direct ticker flow features
# ---------------------------------------------------------------------------


def _direct_features(awards: pd.DataFrame) -> pd.DataFrame:
    """Rolling award flow features for tickers with direct recipient matches."""
    direct = awards.dropna(subset=["ticker"])
    if direct.empty:
        return pd.DataFrame(columns=["symbol", "period", "feature_name", "feature_value"])

    monthly = (
        direct.groupby(["ticker", "month"])["amount"]
        .sum()
        .reset_index()
        .rename(columns={"ticker": "symbol", "month": "period", "amount": "monthly_usd"})
    )

    frames = []
    for sym, grp in monthly.groupby("symbol"):
        grp = grp.set_index("period").sort_index()
        # Reindex to full month range to ensure rolling windows are correct
        full_idx = pd.period_range(grp.index.min(), grp.index.max(), freq="M")
        grp = grp.reindex(full_idx, fill_value=0.0)

        ts = grp["monthly_usd"]
        r3m  = ts.rolling(3,  min_periods=1).sum()
        r6m  = ts.rolling(6,  min_periods=1).sum()
        r12m = ts.rolling(12, min_periods=1).sum()
        yoy  = r12m.pct_change(12)

        idx = ts.index.to_timestamp()
        frames.append(pd.DataFrame({
            "symbol": sym, "period": idx,
            "feature_name": "usaspend_direct_3m",
            "feature_value": r3m.values,
        }))
        frames.append(pd.DataFrame({
            "symbol": sym, "period": idx,
            "feature_name": "usaspend_direct_6m",
            "feature_value": r6m.values,
        }))
        frames.append(pd.DataFrame({
            "symbol": sym, "period": idx,
            "feature_name": "usaspend_direct_12m",
            "feature_value": r12m.values,
        }))
        frames.append(pd.DataFrame({
            "symbol": sym, "period": idx,
            "feature_name": "usaspend_direct_yoy",
            "feature_value": yoy.values,
        }))

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["symbol", "period", "feature_name", "feature_value"]
    )


# ---------------------------------------------------------------------------
# Sector+NAICS fallback features
# ---------------------------------------------------------------------------


def _naics_sector_features(awards: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    """State+NAICS weighted award flow for tickers without direct matches.

    Sums awards by (month, state, naics4) then weights by ticker's
    geographic_concentration and sector NAICS codes.
    """
    # Aggregate by month × state × naics4
    agg = (
        awards.dropna(subset=["state"])
        .groupby(["month", "state", "naics4"])["amount"]
        .sum()
        .reset_index()
    )
    if agg.empty:
        return pd.DataFrame(columns=["symbol", "period", "feature_name", "feature_value"])

    frames = []
    for sym in symbols:
        sector  = sector_for_symbol(sym) or ""
        naics   = SECTOR_NAICS.get(sector, [])
        weights = geographic_weights(sym)
        if not naics or not weights:
            continue

        # Filter to relevant NAICS codes and ticker's states
        naics_mask  = agg["naics4"].isin(naics)
        state_mask  = agg["state"].isin(weights.keys())
        relevant    = agg[naics_mask & state_mask].copy()
        if relevant.empty:
            continue

        relevant["w"] = relevant["state"].map(weights).fillna(0.0)
        relevant["weighted_usd"] = relevant["amount"] * relevant["w"]

        monthly_w = (
            relevant.groupby("month")["weighted_usd"]
            .sum()
        )
        full_idx = pd.period_range(monthly_w.index.min(), monthly_w.index.max(), freq="M")
        monthly_w = monthly_w.reindex(full_idx, fill_value=0.0)

        r3m  = monthly_w.rolling(3,  min_periods=1).sum()
        r12m = monthly_w.rolling(12, min_periods=1).sum()
        idx  = monthly_w.index.to_timestamp()

        frames.append(pd.DataFrame({
            "symbol": sym, "period": idx,
            "feature_name": "usaspend_sector_naics_3m",
            "feature_value": r3m.values,
        }))
        frames.append(pd.DataFrame({
            "symbol": sym, "period": idx,
            "feature_name": "usaspend_sector_naics_12m",
            "feature_value": r12m.values,
        }))

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["symbol", "period", "feature_name", "feature_value"]
    )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def run(start: str = "2015-01", end: str = "2025-12") -> None:
    """Compute USASpending signals and upsert to signal_features table."""
    engine  = _engine()
    symbols = tickers_for_signal("usaspending")
    log.info("usaspending_signals_start", n_tickers=len(symbols), start=start, end=end)

    try:
        awards = _load_awards(engine)
    except Exception as exc:
        log.warning("usaspending_signals_no_data", error=str(exc))
        return

    if awards.empty:
        log.warning("usaspending_signals_no_data", reason="usaspending_awards table empty")
        return

    awards = _match_recipients(awards)

    direct_feat = _direct_features(awards)
    naics_feat  = _naics_sector_features(awards, symbols)

    df = pd.concat([direct_feat, naics_feat], ignore_index=True)
    df = df.dropna(subset=["feature_value"])
    df = filter_date_range(df, start, end)
    df = zscore_cross_section(df)

    df["source"]      = _SOURCE
    df["computed_at"] = pd.Timestamp.utcnow()
    upsert_df(
        df[["symbol", "period", "feature_name", "feature_value", "source"]],
        "signal_features",
        ["symbol", "period", "feature_name"],
    )

    out_dir = data_path(get_pipeline()["processed_data_subdirs"]["signal_tables"])
    df.to_parquet(out_dir / f"usaspending_signals_{start}_{end}.parquet", index=False)
    log.info("usaspending_signals_done", rows=len(df), symbols=df["symbol"].nunique())
