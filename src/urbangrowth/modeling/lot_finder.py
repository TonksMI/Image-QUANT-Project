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

try:
    import yaml as _yaml
except ImportError:
    _yaml = None  # fall back to hardcoded defaults

load_dotenv()
log = structlog.get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_CITY_IDS   = {"phoenix": 1, "austin": 2}
_CITY_KEYS  = {v: k for k, v in _CITY_IDS.items()}   # {1: "phoenix", 2: "austin"}
_CITY_CENTERS = {1: (33.4484, -112.0740), 2: (30.2672, -97.7431)}

# H3 resolution-8 approximate ring spacing in km (hex edge-length * 2 ≈ 0.922 km).
# Used to convert metric boundary distances into ring counts.
_H3_RES8_RING_KM = 0.922

# Feature columns evaluated for completeness ratio (Fix 2).
_COMPLETENESS_FEATURES = [
    "built_pct", "veg_pct", "permit_count",
    "investment_score", "ring_score_raw", "neighbor_velocity",
]

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
    opportunity_score_raw   FLOAT,
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
    -- Fix 1: developability mask
    developable             BOOLEAN,
    developable_frac        FLOAT,
    exclusion_reasons       TEXT,
    -- Fix 2: edge effects
    feature_completeness    FLOAT,
    boundary_rings          INTEGER,
    edge_flagged            BOOLEAN,
    UNIQUE (h3_index, city_id, as_of_date)
);
"""

# Columns added in later schema versions — applied as idempotent migrations
_SCHEMA_MIGRATIONS = [
    ("neighbor_velocity",       "FLOAT"),
    ("ring_score",              "FLOAT"),
    ("nearest_address",         "TEXT"),
    ("dominant_property_type",  "TEXT"),
    ("est_land_value_acre",     "FLOAT"),
    ("est_construction_months", "INTEGER"),
    ("est_cost_per_sqft",       "FLOAT"),
    # Fix 1
    ("opportunity_score_raw",   "FLOAT"),
    ("developable",             "BOOLEAN"),
    ("developable_frac",        "FLOAT"),
    ("exclusion_reasons",       "TEXT"),
    # Fix 2
    ("feature_completeness",    "FLOAT"),
    ("boundary_rings",          "INTEGER"),
    ("edge_flagged",            "BOOLEAN"),
]


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


# ── Scoring config ────────────────────────────────────────────────────────────

_SCORING_CFG_PATH  = Path(__file__).parents[3] / "config" / "scoring.yaml"
_CITIES_CFG_PATH   = Path(__file__).parents[3] / "config" / "cities.yaml"

# In-code defaults — used when scoring.yaml is missing or a key is absent.
_SCORING_DEFAULTS: dict = {
    "developability_mask": {
        "enabled": True,
        "min_developable_frac": 0.20,
        "geometry_cache_dir": None,
        "padus_gpkg": None,
        "nhd_gpkg": None,
        "faa_geojson": None,
        "use_hardcoded_fallbacks": True,
    },
    "edge_effects": {
        "enabled": True,
        "min_feature_completeness": 0.90,
        "min_boundary_rings": 2,
        "mode": "exclude",       # "exclude" | "discount"
        "discount_factor": 0.75,
    },
    "score_semantics": {
        "rerank_to_percentile": True,
    },
    "top_n_selection": {
        "n": 5,
        "min_rings": 10,
    },
}


def _load_scoring_cfg() -> dict:
    """Load config/scoring.yaml and deep-merge with hardcoded defaults."""
    file_cfg: dict = {}
    if _yaml is not None and _SCORING_CFG_PATH.exists():
        try:
            with open(_SCORING_CFG_PATH) as fh:
                file_cfg = _yaml.safe_load(fh) or {}
        except Exception as exc:
            log.warning("scoring_cfg_load_failed", path=str(_SCORING_CFG_PATH),
                        error=str(exc))
    # Per-section merge: file values override defaults, missing keys fall to default
    return {
        section: {**defaults, **(file_cfg.get(section) or {})}
        for section, defaults in _SCORING_DEFAULTS.items()
    }


def _load_cities_cfg() -> dict:
    """Return the 'cities' dict from config/cities.yaml (or {} on error)."""
    if _yaml is None or not _CITIES_CFG_PATH.exists():
        return {}
    try:
        with open(_CITIES_CFG_PATH) as fh:
            raw = _yaml.safe_load(fh) or {}
        return raw.get("cities", {})
    except Exception as exc:
        log.warning("cities_cfg_load_failed", error=str(exc))
        return {}


def _dist_to_bbox_km(lat: float, lon: float, bbox: list) -> float:
    """Approximate km from (lat, lon) to the nearest edge of *bbox* [W,S,E,N].

    Uses a planar approximation accurate to ~1 % at Phoenix/Austin latitudes.
    """
    west, south, east, north = bbox
    lat_km = 111.32
    lon_km = 111.32 * math.cos(math.radians(lat))
    return min(
        abs(lon - west)  * lon_km,
        abs(lon - east)  * lon_km,
        abs(lat - south) * lat_km,
        abs(lat - north) * lat_km,
    )


# ── Fix 4: Spatial-diversity Top-N selector ───────────────────────────────────

def diverse_top_n(
    df: pd.DataFrame,
    n: int = 5,
    min_rings: int = 10,
    score_col: str = "opportunity_score",
) -> pd.DataFrame:
    """Greedy Top-N selector with a spatial-diversity constraint.

    Algorithm
    ---------
    1. Sort candidates by *score_col* descending.
    2. Pick the highest-scoring cell; add it to the selection.
    3. Exclude all cells within *min_rings* H3 rings of the selected cell.
    4. Repeat until *n* cells are selected or candidates are exhausted.

    This prevents the common failure mode where all Top-N picks cluster
    into 2-3 adjacent high-scoring neighbourhoods.

    Parameters
    ----------
    df        : DataFrame with 'h3_index' and *score_col*.
    n         : Number of cells to return.
    min_rings : Minimum H3 grid-ring distance between any two selected cells.
    score_col : Column to sort by (descending).

    Returns
    -------
    DataFrame with the selected rows plus a ``diverse_rank`` column (1 = best).
    The original unconstrained rank is preserved via ``score_col`` ordering.
    """
    ranked   = df.sort_values(score_col, ascending=False).reset_index(drop=True)
    selected = []
    excluded: set[str] = set()

    for _, row in ranked.iterrows():
        idx = row["h3_index"]
        if idx in excluded:
            continue
        selected.append(row)
        if len(selected) >= n:
            break
        # Mask the neighbourhood so subsequent picks are spatially separated
        neighborhood = h3.grid_disk(idx, min_rings)
        excluded.update(neighborhood)

    if not selected:
        return pd.DataFrame(columns=list(df.columns) + ["diverse_rank"])
    result = pd.DataFrame(selected).reset_index(drop=True)
    result["diverse_rank"] = range(1, len(result) + 1)
    return result


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

    Pipeline fixes applied (controlled via config/scoring.yaml):
      Fix 1 — Developability mask (protected/tribal/water/airport exclusion)
      Fix 2 — Edge-effect filter (MSA-boundary buffer + feature completeness)
      Fix 3 — Score re-ranking to true percentile [0, 1]
    Spatial diversity (Fix 4) is applied by diverse_top_n() at call-time.
    """
    scoring_cfg = _load_scoring_cfg()
    # bbox / utm_crs loaded lazily inside dev-mask block; initialise here for edge effects
    bbox    = None
    utm_crs = "EPSG:32612"
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

    # ── Fix 2a: Feature completeness ─────────────────────────────────────────
    # Ratio of non-null features per cell.  Computed now (before any cells are
    # dropped) so completeness reflects the full feature pipeline.
    completeness_cols = [c for c in _COMPLETENESS_FEATURES if c in candidates.columns]
    candidates["feature_completeness"] = (
        candidates[completeness_cols].notna().sum(axis=1) / max(len(completeness_cols), 1)
    )

    n_after_saturation = len(candidates)

    # ── Fix 1: Developability mask ────────────────────────────────────────────
    # Drop cells whose overlap with protected/tribal/water/airport land exceeds
    # (1 - min_developable_frac) of the cell area.  Non-developable cells are
    # retained in masked_out so they can be audited.
    n_before_mask = n_after_saturation
    masked_out    = pd.DataFrame()
    dev_cfg       = scoring_cfg["developability_mask"]

    if dev_cfg["enabled"]:
        city_key = _CITY_KEYS.get(city_id, "unknown")
        cities_cfg = _load_cities_cfg()
        city_geo   = cities_cfg.get(city_key, {})
        bbox       = city_geo.get("bbox", None)
        utm_crs    = city_geo.get("utm_crs", "EPSG:32612")

        cache_dir_raw = dev_cfg.get("geometry_cache_dir")
        cache_dir = Path(cache_dir_raw) if cache_dir_raw else None

        try:
            from urbangrowth.modeling.developability_mask import build_mask
            mask_df = build_mask(
                h3_cells  = candidates["h3_index"].tolist(),
                city_key  = city_key,
                bbox      = bbox,
                utm_crs   = utm_crs,
                cfg       = dev_cfg,
                cache_dir = cache_dir,
            )
            candidates = candidates.merge(
                mask_df[["h3_index", "developable", "developable_frac",
                          "exclusion_reasons"]],
                on="h3_index", how="left",
            )
            # Cells not in mask output (shouldn't happen) default to developable
            candidates["developable"]       = candidates["developable"].fillna(True)
            candidates["developable_frac"]  = candidates["developable_frac"].fillna(1.0)
            candidates["exclusion_reasons"] = candidates["exclusion_reasons"].fillna("")

            masked_out = candidates[~candidates["developable"]].copy()
            candidates = candidates[candidates["developable"]].copy()

            log.info("developability_mask_applied",
                     city_id=city_id,
                     removed=len(masked_out),
                     remaining=len(candidates),
                     reason_breakdown=masked_out["exclusion_reasons"]
                         .value_counts().to_dict() if not masked_out.empty else {})
        except Exception as exc:
            log.warning("developability_mask_failed", error=str(exc))
            candidates["developable"]       = True
            candidates["developable_frac"]  = 1.0
            candidates["exclusion_reasons"] = ""
    else:
        candidates["developable"]       = True
        candidates["developable_frac"]  = 1.0
        candidates["exclusion_reasons"] = ""

    # ── Fix 2b: Edge-effects filter ───────────────────────────────────────────
    # Flag cells that are within `min_boundary_rings` of the MSA bbox boundary
    # OR have feature completeness below `min_feature_completeness`.
    # Depending on config mode, either drop or discount these cells.
    edge_cfg     = scoring_cfg["edge_effects"]
    n_before_edge = len(candidates)

    if edge_cfg["enabled"] and bbox is not None:
        city_key_for_edge = _CITY_KEYS.get(city_id, "unknown")
        if city_key_for_edge == "unknown" and bbox is None:
            cities_cfg = _load_cities_cfg()
            city_geo   = cities_cfg.get(_CITY_KEYS.get(city_id, ""), {})
            bbox       = city_geo.get("bbox", None)

        if bbox is not None:
            candidates["boundary_dist_km"] = candidates["h3_index"].map(
                lambda idx: _dist_to_bbox_km(*_h3_centroid(idx), bbox)
            )
            candidates["boundary_rings"] = (
                candidates["boundary_dist_km"] / _H3_RES8_RING_KM
            ).astype(int)
        else:
            candidates["boundary_dist_km"] = np.nan
            candidates["boundary_rings"]   = 999  # unknown → not flagged

        candidates["edge_flagged"] = (
            (candidates["boundary_rings"] < edge_cfg["min_boundary_rings"])
            | (candidates["feature_completeness"] < edge_cfg["min_feature_completeness"])
        )

        n_edge_flagged = int(candidates["edge_flagged"].sum())
        log.info("edge_effects_filter",
                 city_id=city_id,
                 flagged=n_edge_flagged,
                 mode=edge_cfg["mode"],
                 min_rings=edge_cfg["min_boundary_rings"],
                 min_completeness=edge_cfg["min_feature_completeness"])

        if edge_cfg["mode"] == "exclude":
            candidates = candidates[~candidates["edge_flagged"]].copy()
        else:  # "discount" — reduce ring_score_raw for edge cells
            factor = float(edge_cfg.get("discount_factor", 0.75))
            candidates.loc[candidates["edge_flagged"], "ring_score_raw"] *= factor
    else:
        candidates["boundary_dist_km"] = np.nan
        candidates["boundary_rings"]   = np.nan
        candidates["edge_flagged"]     = False

    # Rank-based normalization (percentile ranks, scale-invariant)
    nv_norm   = _rank_norm(candidates["neighbor_velocity"])
    ring_norm = _rank_norm(candidates["ring_score_raw"])
    vac_norm  = _rank_norm(candidates["vacancy_raw"])
    inv_norm  = _rank_norm(candidates["investment_score"].fillna(
        candidates["investment_score"].median()
    ))

    # Raw composite: weighted average of component percentile ranks.
    # Maximum is ~0.70-0.74 because no single cell tops all 4 sub-dimensions.
    candidates["opportunity_score_raw"] = (
        _W_NEIGHBOR_VELOCITY * nv_norm
        + _W_RING_PROXIMITY  * ring_norm
        + _W_VACANCY         * vac_norm
        + _W_INVESTMENT      * inv_norm
    )

    # ── Fix 3: Score re-ranking to true percentile ────────────────────────────
    # Re-rank the composite so that opportunity_score ∈ [0, 1] uniformly —
    # the top cell scores 1.0 and the bottom cell scores ≈ 0.0.
    # The un-reranked value is kept in opportunity_score_raw for audit.
    score_cfg = scoring_cfg["score_semantics"]
    if score_cfg.get("rerank_to_percentile", True):
        candidates["opportunity_score"] = _rank_norm(candidates["opportunity_score_raw"])
    else:
        candidates["opportunity_score"] = candidates["opportunity_score_raw"]

    # Score distribution summary (always logged for audit / CI)
    s = candidates["opportunity_score"]
    city_label = _CITY_KEYS.get(city_id, str(city_id))
    print(f"\n── Score distribution [{city_label}] ──────────────────────────────")
    print(f"   Scored cells : {len(s)}")
    print(f"   min={s.min():.4f}  max={s.max():.4f}  "
          f"Q25={s.quantile(.25):.4f}  Q50={s.quantile(.50):.4f}  "
          f"Q75={s.quantile(.75):.4f}  Q90={s.quantile(.90):.4f}")
    if score_cfg.get("rerank_to_percentile", True):
        raw = candidates["opportunity_score_raw"]
        print(f"   Raw composite: min={raw.min():.4f}  max={raw.max():.4f}  "
              f"(reranked → true percentile)")
    print()

    p50 = candidates["opportunity_score"].quantile(0.50)
    p75 = candidates["opportunity_score"].quantile(0.75)
    p90 = candidates["opportunity_score"].quantile(0.90)
    candidates["tier"] = candidates["opportunity_score"].map(
        lambda sc: _assign_tier(sc, p50, p75, p90)
    )

    # Before/after pipeline summary
    n_tier1 = int((candidates["tier"] == "Tier 1 — Top 10%").sum())
    n_tier2 = int((candidates["tier"] == "Tier 2 — Top 25%").sum())
    print(f"── Pipeline summary [{city_label}] ────────────────────────────────")
    print(f"   Saturation filter : {len(candidates) + len(masked_out) + (n_before_edge - len(candidates)):>5} → kept after sat filter")
    print(f"   Developability mask: removed {n_before_mask - len(candidates) - (n_before_edge - len(candidates)):>4} cells "
          f"(protected/tribal/water/airport)")
    edge_removed = n_before_edge - len(candidates) if edge_cfg["enabled"] else 0
    print(f"   Edge-effects filter: removed {edge_removed:>4} cells "
          f"(within {edge_cfg['min_boundary_rings']} rings of boundary / low completeness)")
    print(f"   Final scored pool  : {len(candidates):>5} cells")
    print(f"   Tier 1 (Top 10%)   : {n_tier1:>5} cells")
    print(f"   Tier 2 (Top 25%)   : {n_tier2:>5} cells")
    print()

    candidates["as_of_date"] = scoring_date.date()
    candidates["city_id"]   = city_id

    # Ensure all new columns exist (guards against disabled-mask paths)
    for col, default in [
        ("developable",        True),
        ("developable_frac",   1.0),
        ("exclusion_reasons",  ""),
        ("feature_completeness", 1.0),
        ("boundary_rings",     np.nan),
        ("edge_flagged",       False),
        ("opportunity_score_raw", candidates.get("opportunity_score_raw",
                                                  candidates["opportunity_score"])),
    ]:
        if col not in candidates.columns:
            candidates[col] = default

    result = candidates[[
        "h3_index", "city_id", "as_of_date",
        "opportunity_score", "opportunity_score_raw", "investment_score",
        "built_pct", "veg_pct",
        "neighbor_velocity", "dist_to_center_km", "ring_score_raw",
        "zoning_category", "acreage_est", "tier",
        "nearest_address", "dominant_type",
        "est_land_value_acre", "est_construction_months", "est_cost_per_sqft",
        # Fix 1 columns
        "developable", "developable_frac", "exclusion_reasons",
        # Fix 2 columns
        "feature_completeness", "boundary_rings", "edge_flagged",
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
        # Idempotent schema migrations: add any new columns that don't exist yet
        for col, typedef in _SCHEMA_MIGRATIONS:
            conn.execute(text(
                f"ALTER TABLE lot_opportunities ADD COLUMN IF NOT EXISTS {col} {typedef}"
            ))
        # Remove legacy column if present
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

            def _b(col, default=None):
                v = r.get(col, default)
                return None if v is None else bool(v)

            ec = r.get("est_construction_months")
            br = r.get("boundary_rings")
            rows.append({
                "h3_index":                str(r["h3_index"]),
                "city_id":                 city_id,
                "as_of_date":              as_of,
                "opportunity_score":       float(r["opportunity_score"]),
                "opportunity_score_raw":   _f("opportunity_score_raw"),
                "investment_score":        _f("investment_score"),
                "built_pct":               _f("built_pct"),
                "veg_pct":                 _f("veg_pct"),
                "neighbor_velocity":       _f("neighbor_velocity"),
                "dist_to_center_km":       _f("dist_to_center_km"),
                "ring_score":              _f("ring_score"),
                "zoning_category":         r.get("zoning_category") or "unknown",
                "acreage_est":             _f("acreage_est"),
                "tier":                    str(r.get("tier", "")),
                "nearest_address":         "" if pd.isna(r.get("nearest_address")) else (r.get("nearest_address") or ""),
                "dominant_property_type":  r.get("dominant_property_type") or "unknown",
                "est_land_value_acre":     _f("est_land_value_acre"),
                "est_construction_months": None if (ec is None or (isinstance(ec, float) and np.isnan(ec))) else int(ec),
                "est_cost_per_sqft":       _f("est_cost_per_sqft"),
                # Fix 1
                "developable":             _b("developable", True),
                "developable_frac":        _f("developable_frac"),
                "exclusion_reasons":       r.get("exclusion_reasons") or "",
                # Fix 2
                "feature_completeness":    _f("feature_completeness"),
                "boundary_rings":          None if (br is None or (isinstance(br, float) and np.isnan(br))) else int(br),
                "edge_flagged":            _b("edge_flagged", False),
            })

        chunk_size = 500
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i : i + chunk_size]
            conn.execute(
                text("""
                    INSERT INTO lot_opportunities
                        (h3_index, city_id, as_of_date,
                         opportunity_score, opportunity_score_raw, investment_score,
                         built_pct, veg_pct, neighbor_velocity,
                         dist_to_center_km, ring_score, zoning_category,
                         acreage_est, tier,
                         nearest_address, dominant_property_type,
                         est_land_value_acre, est_construction_months, est_cost_per_sqft,
                         developable, developable_frac, exclusion_reasons,
                         feature_completeness, boundary_rings, edge_flagged)
                    VALUES
                        (:h3_index, :city_id, :as_of_date,
                         :opportunity_score, :opportunity_score_raw, :investment_score,
                         :built_pct, :veg_pct, :neighbor_velocity,
                         :dist_to_center_km, :ring_score, :zoning_category,
                         :acreage_est, :tier,
                         :nearest_address, :dominant_property_type,
                         :est_land_value_acre, :est_construction_months, :est_cost_per_sqft,
                         :developable, :developable_frac, :exclusion_reasons,
                         :feature_completeness, :boundary_rings, :edge_flagged)
                    ON CONFLICT (h3_index, city_id, as_of_date) DO UPDATE SET
                        opportunity_score       = EXCLUDED.opportunity_score,
                        opportunity_score_raw   = EXCLUDED.opportunity_score_raw,
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
                        est_cost_per_sqft       = EXCLUDED.est_cost_per_sqft,
                        developable             = EXCLUDED.developable,
                        developable_frac        = EXCLUDED.developable_frac,
                        exclusion_reasons       = EXCLUDED.exclusion_reasons,
                        feature_completeness    = EXCLUDED.feature_completeness,
                        boundary_rings          = EXCLUDED.boundary_rings,
                        edge_flagged            = EXCLUDED.edge_flagged
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
    engine  = _engine()
    targets = {k: v for k, v in _CITY_IDS.items()
               if cities is None or k in cities}
    scoring_cfg = _load_scoring_cfg()

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

            # ── Fix 4: Spatial-diversity Top-N comparison ─────────────────────
            top_cfg = scoring_cfg["top_n_selection"]
            n_picks = int(top_cfg.get("n", 5))
            min_rings = int(top_cfg.get("min_rings", 10))

            tier1 = opps[opps["tier"] == "Tier 1 — Top 10%"].copy()
            if not tier1.empty:
                print(f"── Fix 4: Top-{n_picks} comparison [{city_name}] ───────────────────────")

                # Standard (unconstrained) top-N
                std_top = tier1.nlargest(n_picks, "opportunity_score")[
                    ["h3_index", "opportunity_score", "dist_to_center_km",
                     "dominant_property_type"]
                ].reset_index(drop=True)
                print(f"  Standard top-{n_picks} (unconstrained):")
                for i, row in std_top.iterrows():
                    lat, lon = h3.cell_to_latlng(row["h3_index"])
                    print(f"    #{i+1}  score={row['opportunity_score']:.4f}"
                          f"  lat={lat:.4f} lon={lon:.4f}"
                          f"  dist={row.get('dist_to_center_km', float('nan')):.1f}km"
                          f"  type={row.get('dominant_property_type','?')}")

                # Diverse top-N
                div_top = diverse_top_n(tier1, n=n_picks, min_rings=min_rings)
                print(f"  Diverse top-{n_picks} (min {min_rings} rings apart):")
                for _, row in div_top.iterrows():
                    lat, lon = h3.cell_to_latlng(row["h3_index"])
                    print(f"    #{int(row['diverse_rank'])}  score={row['opportunity_score']:.4f}"
                          f"  lat={lat:.4f} lon={lon:.4f}"
                          f"  dist={row.get('dist_to_center_km', float('nan')):.1f}km"
                          f"  type={row.get('dominant_property_type','?')}")
                print()


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
