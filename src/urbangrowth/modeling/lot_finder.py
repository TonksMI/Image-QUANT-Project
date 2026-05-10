"""Empty lot opportunity finder with walk-forward backtest.

Identifies sub-city H3 cells that are currently underdeveloped (low built_pct)
but sit in the path of expansion — scoring them on spatial pressure from
neighbors, fringe-zone proximity, and satellite-derived vacancy.

Fixes vs. original version:
  1. Permit velocity anchored to scoring date (not absolute max date).
  2. Neighbor k-ring permit velocity replaces own-cell velocity — captures
     spillover pressure: if surrounding cells are building, this cell is next.
  3. Ring-based proximity: score peaks at the urban-fringe ring (city-specific),
     penalizes downtown (already saturated) and far exurbs (no near-term
     demand). Removes the monotone center-bias that made downtown always win.
  4. Rank-based normalization (percentile ranks) replaces min-max. Scale-
     invariant across cities and stable across runs with different candidate
     pools.
  5. Vacancy supplemented by veg_pct when built_pct is near-uniform (Phoenix
     satellite issue). If the 95th-pct of built_pct < 0.01, veg_pct becomes
     the primary vacancy proxy (more vegetation = more undeveloped land).
  6. Backtest uses the same composite formula as build_opportunities (minus the
     GBM investment_score which requires look-ahead to compute historically).
     This means the IC actually validates what gets deployed.
  7. Already-saturated cells excluded: cells with built_pct > p75 of the
     scoring snapshot are dropped before scoring — they are developed areas
     that the GBM happens to score highly due to proximity/permit history.

Data sources:
  - h3_features:     satellite-derived built/veg percentages, permit aggregates
  - h3_predictions:  GBM investment scores (supplementary, not primary signal)
  - city_permits:    permit counts geocoded to H3 cells
  - zoning_snapshots: zoning category per cell (optional)

Usage:
    python -m urbangrowth.modeling.lot_finder
    python -m urbangrowth.modeling.lot_finder --city austin
    python -m urbangrowth.modeling.lot_finder --backtest-only
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import h3
import numpy as np
import pandas as pd
import structlog
from dotenv import load_dotenv
from scipy.stats import spearmanr
from sqlalchemy import text

from urbangrowth.config import data_path
from urbangrowth.db.loaders import _engine

load_dotenv()
log = structlog.get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_CITY_IDS = {"phoenix": 1, "austin": 2}
_CITY_CENTERS = {1: (33.4484, -112.0740), 2: (30.2672, -97.7431)}

# Urban-fringe ring where undeveloped lots have highest development pressure.
# (ring_min_km, ring_max_km) — score=1.0 inside ring, ramps down outside.
_CITY_RING_KM = {
    1: (10, 45),  # phoenix: sprawl frontier is ~10-45km out
    2: (8, 30),   # austin: growth frontier is ~8-30km out
}

# Already-saturated filter: cells with built_pct above this percentile are
# excluded from the candidate pool (they are developed, not vacant lots).
_SATURATION_PCT = 0.75

# k-ring radius for neighbor permit velocity
_NEIGHBOR_K = 2

# Walk-forward backtest look-forward window in months
_LOOKFORWARD_MONTHS = 24

# Production score weights (must sum to 1.0)
# GBM investment_score supplements but doesn't dominate the fundamental signals.
_W_NEIGHBOR_VELOCITY = 0.35
_W_RING_PROXIMITY    = 0.25
_W_VACANCY           = 0.25
_W_INVESTMENT        = 0.15

# Backtest weights (no GBM score available historically — renormalized)
_BT_W_NEIGHBOR_VELOCITY = 0.42
_BT_W_RING_PROXIMITY    = 0.30
_BT_W_VACANCY           = 0.28

_OUT_DIR = data_path("processed/signals")
_DDL = """
CREATE TABLE IF NOT EXISTS lot_opportunities (
    id                      SERIAL PRIMARY KEY,
    h3_index                TEXT    NOT NULL,
    city_id                 INTEGER NOT NULL,
    as_of_date              DATE    NOT NULL,
    opportunity_score       FLOAT   NOT NULL,
    investment_score        FLOAT,
    built_pct               FLOAT,
    veg_pct                 FLOAT,
    neighbor_velocity       FLOAT,
    dist_to_center_km       FLOAT,
    ring_score              FLOAT,
    zoning_category         TEXT,
    acreage_est             FLOAT,
    tier                    TEXT,
    nearest_address         TEXT,
    dominant_property_type  TEXT,
    est_land_value_acre     FLOAT,
    est_construction_months INTEGER,
    est_cost_per_sqft       FLOAT,
    UNIQUE (h3_index, city_id, as_of_date)
);
"""


# ── Geometry helpers ──────────────────────────────────────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _h3_centroid(h3_index: str) -> tuple[float, float]:
    lat, lng = h3.cell_to_latlng(h3_index)
    return lat, lng


def _h3_area_km2(h3_index: str) -> float:
    return h3.cell_area(h3_index, unit="km^2")


def _ring_score(dist_km: float, ring_min: float, ring_max: float) -> float:
    """Score that peaks at 1.0 inside [ring_min, ring_max] and ramps down outside.

    Inside the ring (urban fringe) = highest development pressure.
    Downtown (< ring_min) = already saturated, scored lower.
    Far exurbs (> ring_max) = insufficient near-term demand, scored lower.
    """
    ramp = ring_max - ring_min
    if dist_km < ring_min:
        return dist_km / ring_min          # 0 at center → 1 at ring_min
    elif dist_km <= ring_max:
        return 1.0                         # full score inside fringe ring
    else:
        return max(0.0, 1.0 - (dist_km - ring_max) / ramp)  # taper beyond ring


def _rank_norm(s: pd.Series) -> pd.Series:
    """Percentile-rank normalization: maps values to [0, 1] uniformly.

    Unlike min-max, this is stable across runs with different candidate pools
    and immune to outlier distortion.  Two runs with different subsets will
    produce consistent relative orderings.
    """
    return s.rank(pct=True, method="average")


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_h3_features(engine, city_id: int) -> pd.DataFrame:
    with engine.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT h3_index, date, built_pct, veg_pct,
                       permit_count, permit_valuation
                FROM h3_features
                WHERE city_id = :cid
                ORDER BY date
            """),
            conn,
            params={"cid": city_id},
        )
    df["date"] = pd.to_datetime(df["date"])
    log.info("h3_features_loaded", city_id=city_id, rows=len(df),
             dates=df["date"].nunique(), cells=df["h3_index"].nunique())
    return df


def _load_investment_scores(engine, city_id: int) -> pd.DataFrame:
    with engine.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT h3_index, investment_score, as_of_date
                FROM h3_predictions
                WHERE city_id = :cid
                  AND model_version = 'gbm_v1'
                ORDER BY as_of_date DESC
            """),
            conn,
            params={"cid": city_id},
        )
    if df.empty:
        return df
    latest = df["as_of_date"].max()
    return df[df["as_of_date"] == latest].copy()


def _load_enrichments(engine, city_id: int) -> pd.DataFrame:
    """Load parcel enrichments: dominant property type, land value, addresses."""
    try:
        with engine.connect() as conn:
            return pd.read_sql(
                text("""
                    SELECT h3_index, dominant_type, est_land_value_acre,
                           nearest_address, est_construction_months, est_cost_per_sqft
                    FROM h3_parcel_enrichments
                    WHERE city_id = :cid
                """),
                conn,
                params={"cid": city_id},
            )
    except Exception as exc:
        log.warning("enrichments_load_failed", error=str(exc))
        return pd.DataFrame(columns=[
            "h3_index", "dominant_type", "est_land_value_acre",
            "nearest_address", "est_construction_months", "est_cost_per_sqft",
        ])


def _load_zoning(engine, city_id: int) -> pd.DataFrame:
    try:
        with engine.connect() as conn:
            cnt = conn.execute(text("SELECT COUNT(*) FROM zoning_snapshots")).scalar()
            if cnt == 0:
                return pd.DataFrame(columns=["h3_index", "zoning_category"])
            df = pd.read_sql(
                text("""
                    SELECT h3_index, zoning_category
                    FROM zoning_snapshots
                    WHERE city_id = :cid
                    ORDER BY snapshot_date DESC
                """),
                conn,
                params={"cid": city_id},
            )
        return df.drop_duplicates("h3_index", keep="first")
    except Exception as exc:
        log.warning("zoning_load_failed", error=str(exc))
        return pd.DataFrame(columns=["h3_index", "zoning_category"])


# ── Permit velocity (anchored to scoring date) ────────────────────────────────

def _compute_permit_velocity(
    features: pd.DataFrame,
    as_of_date: pd.Timestamp,
    window_months: int = 12,
) -> dict[str, float]:
    """Rolling permit count per h3_index in the 12 months ending at as_of_date.

    Anchored to the scoring date — not the absolute max date in the table —
    so the velocity window matches the feature snapshot being scored.

    Returns a dict {h3_index: velocity} for fast k-ring lookups.
    """
    cutoff = as_of_date - pd.DateOffset(months=window_months)
    recent = features[
        (features["date"] > cutoff) & (features["date"] <= as_of_date)
    ]
    if recent.empty or "permit_count" not in recent.columns:
        return {}
    velocity = (
        recent.groupby("h3_index")["permit_count"]
        .sum()
        .to_dict()
    )
    return velocity


def _neighbor_velocity(h3_index: str, velocity: dict[str, float], k: int = _NEIGHBOR_K) -> float:
    """Sum of permit counts in the k-ring neighborhood (excluding the cell itself).

    If surrounding cells are building, this vacant cell is next in line.
    This spatial spillover signal is stronger than own-cell permit count for
    identifying the frontier of expansion.
    """
    neighbors = set(h3.grid_disk(h3_index, k)) - {h3_index}
    return sum(velocity.get(n, 0.0) for n in neighbors)


# ── Vacancy signal ────────────────────────────────────────────────────────────

def _vacancy_signal(snap: pd.DataFrame) -> pd.Series:
    """Composite vacancy proxy robust to near-zero built_pct data.

    When built_pct is near-uniform (e.g. Phoenix where satellite classification
    gives values < 0.01 everywhere), it carries no information.  In that case,
    veg_pct becomes the primary proxy: more vegetation = less developed land.

    Returns a Series (same index as snap) where higher = more vacant.
    """
    bp = snap["built_pct"].fillna(0.0)
    vp = snap["veg_pct"].fillna(0.0)

    # If 95th-pct of built_pct is below 1%, data is degenerate — use veg_pct
    if bp.quantile(0.95) < 0.01:
        log.info("vacancy_using_veg_pct", reason="built_pct_near_zero")
        return vp  # higher veg = more undeveloped
    else:
        return 1.0 - bp  # higher = less built = more vacant


# ── Scoring ───────────────────────────────────────────────────────────────────

def _assign_tier(score: float, p50: float, p75: float, p90: float) -> str:
    if score >= p90:
        return "Tier 1 — Top 10%"
    elif score >= p75:
        return "Tier 2 — Top 25%"
    elif score >= p50:
        return "Tier 3 — Top 50%"
    return "Below threshold"


def build_opportunities(city_id: int, engine) -> pd.DataFrame:
    """Score underdeveloped H3 cells for the given city.

    Composite opportunity score:
      35% — neighbor permit velocity (k-ring pressure from surrounding cells)
      25% — ring-based proximity (fringe zone gets max score; downtown penalized)
      25% — vacancy signal (low built_pct or high veg_pct)
      15% — GBM investment score (supplementary ML signal)
    """
    feats = _load_h3_features(engine, city_id)
    if feats.empty:
        log.warning("no_h3_features", city_id=city_id)
        return pd.DataFrame()

    scores      = _load_investment_scores(engine, city_id)
    zoning      = _load_zoning(engine, city_id)
    enrichments = _load_enrichments(engine, city_id)

    # Use the latest date with non-null built_pct as the scoring snapshot
    has_built = feats[feats["built_pct"].notna() & (feats["built_pct"] > 0)]
    if has_built.empty:
        log.warning("no_nonzero_built_pct", city_id=city_id)
        return pd.DataFrame()
    scoring_date = has_built["date"].max()
    snap = feats[feats["date"] == scoring_date].copy()
    log.info("scoring_date", city_id=city_id, date=str(scoring_date.date()),
             cells=len(snap))

    # Permit velocity anchored to this scoring date
    velocity = _compute_permit_velocity(feats, scoring_date)

    # Exclude already-saturated cells: those with built_pct above p75
    # These are developed areas — not vacant lot opportunities
    sat_thresh = snap["built_pct"].quantile(_SATURATION_PCT)
    candidates = snap[snap["built_pct"] <= sat_thresh].copy()
    log.info("saturation_filter", city_id=city_id,
             threshold=round(float(sat_thresh), 4),
             removed=len(snap) - len(candidates),
             remaining=len(candidates))

    if candidates.empty:
        log.warning("no_candidates_after_saturation_filter", city_id=city_id)
        return pd.DataFrame()

    # Merge GBM investment scores
    if not scores.empty:
        candidates = candidates.merge(
            scores[["h3_index", "investment_score"]], on="h3_index", how="left"
        )
    else:
        candidates["investment_score"] = np.nan

    # Merge zoning
    if not zoning.empty:
        candidates = candidates.merge(zoning, on="h3_index", how="left")
    else:
        candidates["zoning_category"] = "unknown"

    # Merge parcel enrichments (property type, land value, address, build timeline)
    if not enrichments.empty:
        candidates = candidates.merge(enrichments, on="h3_index", how="left")
    else:
        candidates["dominant_type"]           = "unknown"
        candidates["est_land_value_acre"]     = np.nan
        candidates["nearest_address"]         = ""
        candidates["est_construction_months"] = 12
        candidates["est_cost_per_sqft"]       = 180.0

    # Geometry
    center_lat, center_lon = _CITY_CENTERS[city_id]
    ring_min, ring_max = _CITY_RING_KM[city_id]

    def _dist(idx: str) -> float:
        lat, lon = _h3_centroid(idx)
        return _haversine_km(lat, lon, center_lat, center_lon)

    candidates["dist_to_center_km"] = candidates["h3_index"].map(_dist)
    candidates["ring_score_raw"] = candidates["dist_to_center_km"].map(
        lambda d: _ring_score(d, ring_min, ring_max)
    )
    candidates["acreage_est"] = candidates["h3_index"].map(
        lambda idx: _h3_area_km2(idx) * 247.105
    )

    # Neighbor permit velocity
    candidates["neighbor_velocity"] = candidates["h3_index"].map(
        lambda idx: _neighbor_velocity(idx, velocity)
    )

    # Vacancy signal
    candidates["vacancy_raw"] = _vacancy_signal(candidates).values

    # Rank-based normalization (percentile ranks, scale-invariant)
    nv_norm  = _rank_norm(candidates["neighbor_velocity"])
    ring_norm = _rank_norm(candidates["ring_score_raw"])
    vac_norm  = _rank_norm(candidates["vacancy_raw"])
    inv_norm  = _rank_norm(candidates["investment_score"].fillna(
        candidates["investment_score"].median()
    ))

    candidates["opportunity_score"] = (
        _W_NEIGHBOR_VELOCITY * nv_norm
        + _W_RING_PROXIMITY  * ring_norm
        + _W_VACANCY         * vac_norm
        + _W_INVESTMENT      * inv_norm
    )

    p50 = candidates["opportunity_score"].quantile(0.50)
    p75 = candidates["opportunity_score"].quantile(0.75)
    p90 = candidates["opportunity_score"].quantile(0.90)
    candidates["tier"] = candidates["opportunity_score"].map(
        lambda s: _assign_tier(s, p50, p75, p90)
    )

    candidates["as_of_date"] = scoring_date.date()
    candidates["city_id"] = city_id

    result = candidates[[
        "h3_index", "city_id", "as_of_date",
        "opportunity_score", "investment_score",
        "built_pct", "veg_pct",
        "neighbor_velocity", "dist_to_center_km", "ring_score_raw",
        "zoning_category", "acreage_est", "tier",
        "nearest_address", "dominant_type",
        "est_land_value_acre", "est_construction_months", "est_cost_per_sqft",
    ]].rename(columns={
        "ring_score_raw": "ring_score",
        "dominant_type":  "dominant_property_type",
    }).sort_values(
        "opportunity_score", ascending=False
    ).reset_index(drop=True)

    log.info("opportunities_scored", city_id=city_id, total=len(result),
             tier1=int((result["tier"] == "Tier 1 — Top 10%").sum()))
    return result


# ── Walk-forward backtest ─────────────────────────────────────────────────────

def backtest(city_id: int, engine) -> pd.DataFrame:
    """Walk-forward backtest using the same composite formula as build_opportunities.

    At each historical signal_date:
      - Exclude already-saturated cells (built_pct > p75)
      - Score remaining cells with the three non-GBM components
        (neighbor velocity, ring proximity, vacancy)
      - Measure actual built_pct change at signal_date + LOOKFORWARD_MONTHS
      - Compute Spearman IC between signal and outcome

    Using the same formula means the IC directly validates what gets deployed.
    GBM investment_score is excluded here because computing it historically
    would require retraining per fold (look-ahead otherwise).
    """
    feats = _load_h3_features(engine, city_id)
    if feats.empty:
        return pd.DataFrame()

    center_lat, center_lon = _CITY_CENTERS[city_id]
    ring_min, ring_max = _CITY_RING_KM[city_id]
    dates = sorted(feats["date"].unique())

    # Precompute centroid distances (static — H3 cells don't move)
    all_cells = feats["h3_index"].unique()
    dist_map = {
        idx: _haversine_km(*_h3_centroid(idx), center_lat, center_lon)
        for idx in all_cells
    }
    ring_map = {
        idx: _ring_score(dist_map[idx], ring_min, ring_max)
        for idx in all_cells
    }

    results = []
    for signal_date in dates:
        fwd_date = signal_date + pd.DateOffset(months=_LOOKFORWARD_MONTHS)
        if fwd_date not in feats["date"].values:
            continue

        snap = feats[feats["date"] == signal_date].copy()
        fwd = (
            feats[feats["date"] == fwd_date][["h3_index", "built_pct"]]
            .rename(columns={"built_pct": "built_pct_fwd"})
        )

        # Skip dates with no real satellite data
        if snap["built_pct"].isna().all() or snap["built_pct"].max() == 0:
            continue

        # Exclude already-saturated cells (same filter as production)
        sat_thresh = snap["built_pct"].quantile(_SATURATION_PCT)
        snap = snap[snap["built_pct"] <= sat_thresh].copy()
        if len(snap) < 30:
            continue

        merged = snap.merge(fwd, on="h3_index", how="inner")
        if len(merged) < 30:
            continue

        merged["built_pct_change"] = merged["built_pct_fwd"] - merged["built_pct"]

        # Permit velocity anchored to this signal_date
        velocity = _compute_permit_velocity(feats, signal_date)

        merged["neighbor_velocity"] = merged["h3_index"].map(
            lambda idx: _neighbor_velocity(idx, velocity)
        )
        merged["ring_score_val"] = merged["h3_index"].map(
            lambda idx: ring_map.get(idx, 0.0)
        )
        merged["vacancy_raw"] = _vacancy_signal(merged).values

        # Same rank-norm as production
        nv_norm   = _rank_norm(merged["neighbor_velocity"])
        ring_norm = _rank_norm(merged["ring_score_val"])
        vac_norm  = _rank_norm(merged["vacancy_raw"])

        signal = (
            _BT_W_NEIGHBOR_VELOCITY * nv_norm
            + _BT_W_RING_PROXIMITY  * ring_norm
            + _BT_W_VACANCY         * vac_norm
        )

        ic, pval = spearmanr(signal, merged["built_pct_change"])

        q75_sig = signal.quantile(0.75)
        q75_out = merged["built_pct_change"].quantile(0.75)
        top_sig = signal >= q75_sig
        top_out = merged["built_pct_change"] >= q75_out
        hit_rate = (
            (top_sig & top_out).sum() / top_sig.sum()
            if top_sig.sum() > 0 else np.nan
        )

        results.append({
            "signal_date": signal_date,
            "year": signal_date.year,
            "n_cells": len(merged),
            "ic": ic,
            "pval": pval,
            "hit_rate": hit_rate,
            "mean_built_change_top": float(merged.loc[top_sig, "built_pct_change"].mean()),
            "mean_built_change_bottom": float(merged.loc[~top_sig, "built_pct_change"].mean()),
        })

    if not results:
        log.warning("backtest_no_results", city_id=city_id)
        return pd.DataFrame()

    bt = pd.DataFrame(results)
    summary = bt.groupby("year").agg(
        mean_ic=("ic", "mean"),
        mean_hit_rate=("hit_rate", "mean"),
        mean_built_change_top=("mean_built_change_top", "mean"),
        mean_built_change_bottom=("mean_built_change_bottom", "mean"),
        n_periods=("ic", "count"),
    ).reset_index()

    log.info("backtest_complete", city_id=city_id, years=len(summary))
    for _, row in summary.iterrows():
        log.info(
            "backtest_year",
            city_id=city_id,
            year=int(row["year"]),
            ic=round(float(row["mean_ic"]), 3),
            hit_rate=round(float(row["mean_hit_rate"]), 3),
            top_change=round(float(row["mean_built_change_top"]), 4),
        )
    return summary


# ── Persistence ───────────────────────────────────────────────────────────────

def save_opportunities(opps: pd.DataFrame, engine) -> None:
    if opps.empty:
        return

    city_id = int(opps["city_id"].iloc[0])
    as_of   = str(opps["as_of_date"].iloc[0])

    with engine.begin() as conn:
        conn.execute(text(_DDL))
        # Migrate schema: add new columns if they don't exist (idempotent)
        for col, typedef in [
            ("neighbor_velocity",       "FLOAT"),
            ("ring_score",              "FLOAT"),
            ("nearest_address",         "TEXT"),
            ("dominant_property_type",  "TEXT"),
            ("est_land_value_acre",     "FLOAT"),
            ("est_construction_months", "INTEGER"),
            ("est_cost_per_sqft",       "FLOAT"),
        ]:
            conn.execute(text(
                f"ALTER TABLE lot_opportunities ADD COLUMN IF NOT EXISTS {col} {typedef}"
            ))
        # Remove old column if present
        conn.execute(text(
            "ALTER TABLE lot_opportunities DROP COLUMN IF EXISTS permit_velocity"
        ))
        conn.execute(text("""
            DELETE FROM lot_opportunities
            WHERE city_id = :cid AND as_of_date = :dt
        """), {"cid": city_id, "dt": as_of})

        rows = []
        for _, r in opps.iterrows():
            def _f(col):
                v = r.get(col, np.nan)
                return None if (v is None or (isinstance(v, float) and np.isnan(v))) else float(v)

            ec = r.get("est_construction_months")
            rows.append({
                "h3_index":                str(r["h3_index"]),
                "city_id":                 city_id,
                "as_of_date":              as_of,
                "opportunity_score":       float(r["opportunity_score"]),
                "investment_score":        _f("investment_score"),
                "built_pct":               _f("built_pct"),
                "veg_pct":                 _f("veg_pct"),
                "neighbor_velocity":       _f("neighbor_velocity"),
                "dist_to_center_km":       _f("dist_to_center_km"),
                "ring_score":              _f("ring_score"),
                "zoning_category":         r.get("zoning_category") or "unknown",
                "acreage_est":             _f("acreage_est"),
                "tier":                    str(r.get("tier", "")),
                "nearest_address":         r.get("nearest_address") or "",
                "dominant_property_type":  r.get("dominant_property_type") or "unknown",
                "est_land_value_acre":     _f("est_land_value_acre"),
                "est_construction_months": None if (ec is None or (isinstance(ec, float) and np.isnan(ec))) else int(ec),
                "est_cost_per_sqft":       _f("est_cost_per_sqft"),
            })

        chunk_size = 500
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i : i + chunk_size]
            conn.execute(
                text("""
                    INSERT INTO lot_opportunities
                        (h3_index, city_id, as_of_date, opportunity_score,
                         investment_score, built_pct, veg_pct, neighbor_velocity,
                         dist_to_center_km, ring_score, zoning_category,
                         acreage_est, tier,
                         nearest_address, dominant_property_type,
                         est_land_value_acre, est_construction_months, est_cost_per_sqft)
                    VALUES
                        (:h3_index, :city_id, :as_of_date, :opportunity_score,
                         :investment_score, :built_pct, :veg_pct, :neighbor_velocity,
                         :dist_to_center_km, :ring_score, :zoning_category,
                         :acreage_est, :tier,
                         :nearest_address, :dominant_property_type,
                         :est_land_value_acre, :est_construction_months, :est_cost_per_sqft)
                    ON CONFLICT (h3_index, city_id, as_of_date) DO UPDATE SET
                        opportunity_score       = EXCLUDED.opportunity_score,
                        investment_score        = EXCLUDED.investment_score,
                        built_pct               = EXCLUDED.built_pct,
                        veg_pct                 = EXCLUDED.veg_pct,
                        neighbor_velocity       = EXCLUDED.neighbor_velocity,
                        dist_to_center_km       = EXCLUDED.dist_to_center_km,
                        ring_score              = EXCLUDED.ring_score,
                        zoning_category         = EXCLUDED.zoning_category,
                        acreage_est             = EXCLUDED.acreage_est,
                        tier                    = EXCLUDED.tier,
                        nearest_address         = EXCLUDED.nearest_address,
                        dominant_property_type  = EXCLUDED.dominant_property_type,
                        est_land_value_acre     = EXCLUDED.est_land_value_acre,
                        est_construction_months = EXCLUDED.est_construction_months,
                        est_cost_per_sqft       = EXCLUDED.est_cost_per_sqft
                """),
                chunk,
            )

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    city_name = next(
        (k for k, v in _CITY_IDS.items() if v == city_id), "unknown"
    )
    out_path = _OUT_DIR / f"lot_opportunities_{city_name}.parquet"
    opps.to_parquet(out_path, index=False)
    log.info("opportunities_saved", rows=len(opps), path=str(out_path))


# ── Entry point ───────────────────────────────────────────────────────────────

def run(cities: list[str] | None = None, backtest_only: bool = False) -> None:
    engine = _engine()
    targets = {k: v for k, v in _CITY_IDS.items()
               if cities is None or k in cities}

    for city_name, city_id in targets.items():
        log.info("lot_finder_start", city=city_name)

        bt_summary = backtest(city_id, engine)
        if not bt_summary.empty:
            _OUT_DIR.mkdir(parents=True, exist_ok=True)
            bt_path = _OUT_DIR / f"lot_backtest_{city_name}.parquet"
            bt_summary.to_parquet(bt_path, index=False)
            log.info("backtest_saved", city=city_name, path=str(bt_path))

        if not backtest_only:
            opps = build_opportunities(city_id, engine)
            save_opportunities(opps, engine)


def main() -> None:
    parser = argparse.ArgumentParser(description="Empty lot opportunity finder")
    parser.add_argument("--city", type=str, default=None,
                        help="City to score (phoenix or austin). Defaults to all.")
    parser.add_argument("--backtest-only", action="store_true",
                        help="Run backtest only, skip opportunity scoring.")
    args = parser.parse_args()
    run(
        cities=[args.city] if args.city else None,
        backtest_only=args.backtest_only,
    )


if __name__ == "__main__":
    main()
