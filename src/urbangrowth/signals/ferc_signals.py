"""FERC interconnection queue signals per ticker.

Features written to signal_features (source = 'ferc_queue'):
  ferc_net_mw_lag{L}           — total net MW entering queue this month
  ferc_solar_mw_lag{L}         — solar MW entering queue
  ferc_wind_mw_lag{L}          — wind/storage MW entering queue
  ferc_withdrawal_rate_lag{L}  — fraction of active MW flagged withdrawn
  ferc_active_transition_lag{L}— MW transitioning to 'active' status
  ferc_region_weighted_mw_lag{L}— region-weighted net MW per ticker footprint

L ∈ {1, 2, 3, 6, 12}

Economic mechanism: project awards → construction ~6-18 months later.
Region weighting uses ISO_TO_STATES from ticker_mapping to map
regional MW flow to each ticker's state footprint.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import structlog
from sqlalchemy import text

from urbangrowth.config import data_path, get_pipeline
from urbangrowth.db.loaders import _engine, upsert_df
from urbangrowth.signals.ticker_mapping import (
    geographic_weights,
    iso_state_weights,
    tickers_for_signal,
    ISO_TO_STATES,
)
from urbangrowth.signals.utils import filter_date_range, yoy_change, zscore_cross_section

log = structlog.get_logger(__name__)

_SOURCE = "ferc_queue"
_LAGS   = (1, 2, 3, 6, 12)
_SOLAR_FUELS    = {"solar", "pv", "solar pv"}
_WIND_FUELS     = {"wind", "wind (onshore)", "wind (offshore)", "battery", "storage", "battery storage"}


# ---------------------------------------------------------------------------
# DB reads
# ---------------------------------------------------------------------------


def _load_queue(engine) -> pd.DataFrame:
    """Load ferc_queue, deduplicated to first appearance per project."""
    sql = text("""
        SELECT project_id, region, fuel_type, mw_capacity, status,
               queue_date, withdrawn,
               MIN(snapshot_date) AS first_seen
        FROM   ferc_queue
        GROUP  BY project_id, region, fuel_type, mw_capacity, status,
                  queue_date, withdrawn
        ORDER  BY queue_date
    """)
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, parse_dates=["queue_date"])
    df["fuel_lower"] = df["fuel_type"].str.lower().fillna("unknown")
    return df


def _load_active_transitions(engine) -> pd.DataFrame:
    """Approximate monthly MW going 'active' using snapshot transitions."""
    sql = text("""
        WITH ranked AS (
            SELECT project_id, region, fuel_type, mw_capacity, status,
                   snapshot_date,
                   LAG(status) OVER (PARTITION BY project_id ORDER BY snapshot_date) AS prev_status
            FROM ferc_queue
        )
        SELECT project_id, region, fuel_type, mw_capacity, snapshot_date
        FROM   ranked
        WHERE  status = 'active'
          AND  (prev_status IS NULL OR prev_status != 'active')
        ORDER  BY snapshot_date
    """)
    with engine.connect() as conn:
        return pd.read_sql(sql, conn, parse_dates=["snapshot_date"])


# ---------------------------------------------------------------------------
# Monthly flow computation
# ---------------------------------------------------------------------------


def _monthly_flows(queue: pd.DataFrame) -> pd.DataFrame:
    """Compute national monthly MW flows from queue entry dates."""
    q = queue.dropna(subset=["queue_date"]).copy()
    q["month"] = q["queue_date"].dt.to_period("M")

    net_mw   = q.groupby("month")["mw_capacity"].sum()
    solar_mw = (
        q[q["fuel_lower"].isin(_SOLAR_FUELS)].groupby("month")["mw_capacity"].sum()
    )
    wind_mw = (
        q[q["fuel_lower"].isin(_WIND_FUELS)].groupby("month")["mw_capacity"].sum()
    )
    flows = pd.DataFrame({"net_mw": net_mw, "solar_mw": solar_mw, "wind_mw": wind_mw}).fillna(0.0)
    # Withdrawal rate = withdrawn MW this month / cumulative backlog
    withdrawn_monthly = (
        q[q["withdrawn"] == True]
        .groupby("month")["mw_capacity"]
        .sum()
    )
    cumulative = q.groupby("month")["mw_capacity"].sum().cumsum()
    withdrawal_rate = (withdrawn_monthly / cumulative).fillna(0.0)

    flows["withdrawal_rate"] = withdrawal_rate
    return flows


def _regional_flows(queue: pd.DataFrame) -> pd.DataFrame:
    """Sum net MW additions per (month, region). Region normalized to uppercase."""
    q = queue.dropna(subset=["queue_date"]).copy()
    q["month"] = q["queue_date"].dt.to_period("M")
    q["region"] = q["region"].str.upper().fillna("")
    return (
        q[q["region"] != ""].groupby(["month", "region"])["mw_capacity"]
        .sum()
        .reset_index()
        .rename(columns={"mw_capacity": "net_mw"})
    )


# ---------------------------------------------------------------------------
# Feature builders
# ---------------------------------------------------------------------------


def _national_features(flows: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    """Return long DataFrame of national FERC flow features."""
    feat: dict[str, pd.Series] = {}
    for L in _LAGS:
        feat[f"ferc_net_mw_lag{L}"]          = flows["net_mw"].shift(L)
        feat[f"ferc_solar_mw_lag{L}"]         = flows["solar_mw"].shift(L)
        feat[f"ferc_wind_mw_lag{L}"]          = flows["wind_mw"].shift(L)
        feat[f"ferc_withdrawal_rate_lag{L}"]  = flows["withdrawal_rate"].shift(L)

    wide = pd.DataFrame(feat)
    wide.index = wide.index.to_timestamp()

    long = (
        wide.reset_index()
        .rename(columns={"month": "period"})
        .melt(id_vars="period", var_name="feature_name", value_name="feature_value")
    )

    frames = []
    for sym in symbols:
        tmp = long.copy()
        tmp["symbol"] = sym
        frames.append(tmp[["symbol", "period", "feature_name", "feature_value"]])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["symbol", "period", "feature_name", "feature_value"]
    )


def _region_weighted_features(
    regional: pd.DataFrame,
    active_trans: pd.DataFrame,
    symbols: list[str],
) -> pd.DataFrame:
    """Build region-weighted net-MW feature per ticker."""
    # Pivot regional flows to (month × region)
    pivot = regional.pivot(index="month", columns="region", values="net_mw").fillna(0.0)

    # Active transition MW per region per month
    if not active_trans.empty:
        active_trans["month"] = active_trans["snapshot_date"].dt.to_period("M")
        act_pivot = (
            active_trans.groupby(["month", "region"])["mw_capacity"]
            .sum()
            .unstack("region")
            .fillna(0.0)
        )
    else:
        act_pivot = pd.DataFrame(index=pivot.index)

    frames = []
    for sym in symbols:
        ticker_weights = geographic_weights(sym)
        if not ticker_weights:
            continue

        # Map ISO regions → states → aggregate weight per region
        region_weight: dict[str, float] = {}
        for iso, states in ISO_TO_STATES.items():
            if iso not in pivot.columns:
                continue
            w = sum(ticker_weights.get(s, 0.0) for s in states)
            if w > 0:
                region_weight[iso] = w

        if not region_weight:
            continue

        # Use raw geographic weights (NOT normalized to 1) so that tickers with
        # higher exposure to a region receive proportionally larger signals.
        # Normalizing to sum=1 would destroy cross-sectional variation when only
        # one ISO/region is present (all tickers would get identical values).
        rw = pd.Series(region_weight)

        # Weighted MW flow
        common = [r for r in rw.index if r in pivot.columns]
        if not common:
            continue
        weighted_mw = (pivot[common] * rw[common]).sum(axis=1)

        # Active transitions weighted
        if not active_trans.empty:
            act_common = [r for r in rw.index if r in act_pivot.columns]
            if act_common:
                weighted_act = (act_pivot[act_common] * rw[act_common]).sum(axis=1)
            else:
                weighted_act = pd.Series(0.0, index=weighted_mw.index)
        else:
            weighted_act = pd.Series(0.0, index=weighted_mw.index)

        idx = weighted_mw.index.to_timestamp()
        for L in _LAGS:
            frames.append(pd.DataFrame({
                "symbol":       sym,
                "period":       idx,
                "feature_name": f"ferc_region_weighted_mw_lag{L}",
                "feature_value":weighted_mw.shift(L).values,
            }))
            frames.append(pd.DataFrame({
                "symbol":       sym,
                "period":       idx,
                "feature_name": f"ferc_active_transition_lag{L}",
                "feature_value":weighted_act.reindex(weighted_mw.index, fill_value=0.0).shift(L).values,
            }))

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["symbol", "period", "feature_name", "feature_value"]
    )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def run(start: str = "2015-01", end: str = "2025-12") -> None:
    """Compute FERC queue signals and upsert to signal_features table."""
    engine  = _engine()
    symbols = tickers_for_signal("ferc_queue")
    log.info("ferc_signals_start", n_tickers=len(symbols), start=start, end=end)

    try:
        queue      = _load_queue(engine)
        active_trans = _load_active_transitions(engine)
    except Exception as exc:
        log.warning("ferc_signals_no_data", error=str(exc))
        return

    if queue.empty:
        log.warning("ferc_signals_no_data", reason="ferc_queue table empty")
        return

    flows    = _monthly_flows(queue)
    regional = _regional_flows(queue)

    nat_feat    = _national_features(flows, symbols)
    region_feat = _region_weighted_features(regional, active_trans, symbols)

    df = pd.concat([nat_feat, region_feat], ignore_index=True)
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
    df.to_parquet(out_dir / f"ferc_signals_{start}_{end}.parquet", index=False)
    log.info("ferc_signals_done", rows=len(df), symbols=df["symbol"].nunique())
