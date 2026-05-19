"""Standalone scoring-pipeline runner.

Exercises all 4 fixes against the flat-file data (data/lots_*.csv and
docs/_phx_opps.csv / docs/_aus_opps.csv) without needing a live DB.

Produces a before/after report and exports updated opportunity CSVs to
docs/ so the map generators pick them up automatically.

Usage
-----
    python -X utf8 run_scoring_pipeline.py [--city phoenix|austin]
"""
from __future__ import annotations

import argparse
import math
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import h3
import numpy as np
import pandas as pd

try:
    import yaml as _yaml
except ImportError:
    _yaml = None

# ── Config helpers (mirrors lot_finder.py) ────────────────────────────────────

_ROOT = Path(__file__).parent
_SCORING_CFG_PATH = _ROOT / "config" / "scoring.yaml"
_CITIES_CFG_PATH  = _ROOT / "config" / "cities.yaml"

_SCORING_DEFAULTS = {
    "developability_mask": {
        "enabled": True,
        "min_developable_frac": 0.20,
        "geometry_cache_dir": "D:/urbangrowth_data/raw/developability",
        "padus_gpkg": None,
        "nhd_gpkg": None,
        "faa_geojson": None,
        "use_hardcoded_fallbacks": True,
    },
    "edge_effects": {
        "enabled": True,
        "min_feature_completeness": 0.90,
        "min_boundary_rings": 2,
        "mode": "exclude",
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


def _load_cfg(path, section_defaults):
    file_cfg = {}
    if _yaml and path.exists():
        with open(path) as f:
            file_cfg = _yaml.safe_load(f) or {}
    return {s: {**d, **(file_cfg.get(s) or {})} for s, d in section_defaults.items()}


def _load_cities():
    if _yaml is None or not _CITIES_CFG_PATH.exists():
        return {}
    with open(_CITIES_CFG_PATH) as f:
        return (_yaml.safe_load(f) or {}).get("cities", {})


# ── Geometry / scoring helpers ────────────────────────────────────────────────

_H3_RES8_RING_KM = 0.922
_COMPLETENESS_FEATURES = ["score", "dist_km", "acres"]   # columns in _phx/aus_opps.csv


def _rank_norm(s: pd.Series) -> pd.Series:
    return s.rank(pct=True, method="average")


def _h3_centroid(idx: str):
    return h3.cell_to_latlng(idx)   # (lat, lon)


def _dist_to_bbox_km(lat, lon, bbox):
    west, south, east, north = bbox
    lat_km = 111.32
    lon_km = 111.32 * math.cos(math.radians(lat))
    return min(
        abs(lon - west)  * lon_km,
        abs(lon - east)  * lon_km,
        abs(lat - south) * lat_km,
        abs(lat - north) * lat_km,
    )


# ── Fix 4: spatial-diversity selector ────────────────────────────────────────

def diverse_top_n(df, n=5, min_rings=10, score_col="score"):
    ranked   = df.sort_values(score_col, ascending=False).reset_index(drop=True)
    selected = []
    excluded: set = set()
    for _, row in ranked.iterrows():
        idx = row["h3_index"]
        if idx in excluded:
            continue
        selected.append(row)
        if len(selected) >= n:
            break
        excluded.update(h3.grid_disk(idx, min_rings))
    if not selected:
        return pd.DataFrame(columns=list(df.columns) + ["diverse_rank"])
    result = pd.DataFrame(selected).reset_index(drop=True)
    result["diverse_rank"] = range(1, len(result) + 1)
    return result


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_city(city_key: str, cfg: dict, cities_cfg: dict) -> pd.DataFrame:
    """Apply all 4 fixes to the flat-file opportunity CSV for *city_key*."""
    city_label = city_key.capitalize()
    opp_csv = _ROOT / "docs" / f"_{'phx' if city_key == 'phoenix' else 'aus'}_opps.csv"

    if not opp_csv.exists():
        print(f"[{city_label}] SKIP — {opp_csv} not found")
        return pd.DataFrame()

    df = pd.read_csv(opp_csv, encoding="utf-8", encoding_errors="replace")
    df = df.dropna(subset=["lat", "lon"])
    df = df[(df["lat"] != 0) & (df["lon"] != 0)]

    city_geo = cities_cfg.get(city_key, {})
    bbox     = city_geo.get("bbox", None)
    utm_crs  = city_geo.get("utm_crs", "EPSG:32612")

    n_raw = len(df)
    print(f"\n{'='*60}")
    print(f"  {city_label} — {n_raw} cells loaded from {opp_csv.name}")
    print(f"{'='*60}")
    print(f"  Original score range: {df['score'].min():.4f} – {df['score'].max():.4f}")

    # ── Fix 1: Developability mask ─────────────────────────────────────────────
    dev_cfg  = cfg["developability_mask"]
    n_before_mask = len(df)
    if dev_cfg["enabled"]:
        cache_dir_raw = dev_cfg.get("geometry_cache_dir")
        cache_dir = Path(cache_dir_raw) if cache_dir_raw else None
        try:
            from src.urbangrowth.modeling.developability_mask import build_mask
            mask_df = build_mask(
                h3_cells  = df["h3_index"].tolist(),
                city_key  = city_key,
                bbox      = bbox,
                utm_crs   = utm_crs,
                cfg       = dev_cfg,
                cache_dir = cache_dir,
            )
            df = df.merge(
                mask_df[["h3_index", "developable", "developable_frac",
                          "exclusion_reasons"]],
                on="h3_index", how="left",
            )
            df["developable"]       = df["developable"].fillna(True)
            df["developable_frac"]  = df["developable_frac"].fillna(1.0)
            df["exclusion_reasons"] = df["exclusion_reasons"].fillna("")

            masked_out = df[~df["developable"]]
            df = df[df["developable"]].copy()

            print(f"\n  Fix 1 — Developability mask:")
            print(f"    Removed  : {len(masked_out):>4} cells  ({len(masked_out)/n_before_mask:.1%})")
            if not masked_out.empty:
                for reason, cnt in masked_out["exclusion_reasons"].value_counts().items():
                    print(f"             · {reason}: {cnt}")
            print(f"    Remaining: {len(df):>4} cells")
        except Exception as exc:
            print(f"\n  Fix 1 — Developability mask SKIPPED: {exc}")
            df["developable"]       = True
            df["developable_frac"]  = 1.0
            df["exclusion_reasons"] = ""
    else:
        print("\n  Fix 1 — Developability mask DISABLED (see scoring.yaml)")
        df["developable"]       = True
        df["developable_frac"]  = 1.0
        df["exclusion_reasons"] = ""

    # ── Fix 2: Edge effects ────────────────────────────────────────────────────
    edge_cfg = cfg["edge_effects"]
    n_before_edge = len(df)

    # Feature completeness (available columns in the flat file)
    completeness_cols = [c for c in _COMPLETENESS_FEATURES if c in df.columns]
    df["feature_completeness"] = (
        df[completeness_cols].notna().sum(axis=1) / max(len(completeness_cols), 1)
    ) if completeness_cols else 1.0

    if edge_cfg["enabled"] and bbox is not None:
        df["boundary_dist_km"] = df.apply(
            lambda r: _dist_to_bbox_km(r["lat"], r["lon"], bbox), axis=1
        )
        df["boundary_rings"] = (df["boundary_dist_km"] / _H3_RES8_RING_KM).astype(int)
        df["edge_flagged"] = (
            (df["boundary_rings"] < edge_cfg["min_boundary_rings"])
            | (df["feature_completeness"] < edge_cfg["min_feature_completeness"])
        )

        n_flagged   = int(df["edge_flagged"].sum())
        n_bdy       = int((df["boundary_rings"] < edge_cfg["min_boundary_rings"]).sum())
        n_sparse    = int((df["feature_completeness"] < edge_cfg["min_feature_completeness"]).sum())

        # Count flagged Tier-1 and Tier-2 cells
        if "tier" in df.columns:
            t1_flagged = int((df["edge_flagged"] & df["tier"].str.startswith("Tier 1")).sum())
            t2_flagged = int((df["edge_flagged"] & df["tier"].str.startswith("Tier 2")).sum())
        else:
            t1_flagged = t2_flagged = "n/a"

        print(f"\n  Fix 2 — Edge effects ({edge_cfg['mode']} mode):")
        print(f"    Flagged  : {n_flagged:>4} cells  ({n_flagged/n_before_edge:.1%})")
        print(f"             · within {edge_cfg['min_boundary_rings']} rings of bbox boundary: {n_bdy}")
        print(f"             · feature completeness < {edge_cfg['min_feature_completeness']}: {n_sparse}")
        print(f"    Tier-1 flagged: {t1_flagged}   Tier-2 flagged: {t2_flagged}")

        if edge_cfg["mode"] == "exclude":
            df = df[~df["edge_flagged"]].copy()
            print(f"    Remaining: {len(df):>4} cells")
        else:
            df.loc[df["edge_flagged"], "score"] *= float(edge_cfg["discount_factor"])
            print(f"    Discounted (×{edge_cfg['discount_factor']}): {n_flagged} cells kept")
    else:
        df["boundary_rings"] = np.nan
        df["edge_flagged"]   = False
        print(f"\n  Fix 2 — Edge effects DISABLED or bbox not available")

    # ── Fix 3: Score semantics — re-rank to true percentile ────────────────────
    score_cfg = cfg["score_semantics"]
    df["score_raw"] = df["score"].copy()
    if score_cfg.get("rerank_to_percentile", True):
        df["score"] = _rank_norm(df["score"])
        print(f"\n  Fix 3 — Score semantics (reranked to percentile):")
        print(f"    Before : min={df['score_raw'].min():.4f}  max={df['score_raw'].max():.4f}")
    else:
        print(f"\n  Fix 3 — Score semantics (raw composite kept, no rerank):")

    s = df["score"]
    print(f"    After  : min={s.min():.4f}  max={s.max():.4f}  "
          f"Q25={s.quantile(.25):.4f}  Q50={s.quantile(.50):.4f}  "
          f"Q75={s.quantile(.75):.4f}  Q90={s.quantile(.90):.4f}")

    # Recompute tiers from new score distribution
    p90 = s.quantile(0.90)
    p75 = s.quantile(0.75)
    p50 = s.quantile(0.50)
    df["tier"] = np.where(s >= p90, "Tier 1 — Top 10%",
                 np.where(s >= p75, "Tier 2 — Top 25%",
                 np.where(s >= p50, "Tier 3 — Top 50%", "Below threshold")))

    n_t1 = int((df["tier"] == "Tier 1 — Top 10%").sum())
    n_t2 = int((df["tier"] == "Tier 2 — Top 25%").sum())
    print(f"\n  Pool after all filters: {len(df)} cells  "
          f"(Tier 1: {n_t1}  Tier 2: {n_t2})")

    # ── Fix 4: Spatial diversity ───────────────────────────────────────────────
    top_cfg   = cfg["top_n_selection"]
    n_picks   = int(top_cfg.get("n", 5))
    min_rings = int(top_cfg.get("min_rings", 10))

    tier1 = df[df["tier"] == "Tier 1 — Top 10%"].copy()
    if not tier1.empty:
        std_top = tier1.nlargest(n_picks, "score").reset_index(drop=True)
        div_top = diverse_top_n(tier1, n=n_picks, min_rings=min_rings, score_col="score")

        print(f"\n  Fix 4 — Top-{n_picks} selection (min {min_rings} rings ≈ "
              f"{min_rings * _H3_RES8_RING_KM:.1f} km between picks):")
        print(f"  {'Standard (unconstrained)':<30}  {'Diverse (spatially spread)'}")
        print(f"  {'-'*29}  {'-'*29}")
        for i in range(n_picks):
            sr = std_top.iloc[i] if i < len(std_top) else None
            dr = div_top.iloc[i] if i < len(div_top) else None
            s_str = (f"#{i+1} {sr['score']:.4f} lat={sr['lat']:.3f} lon={sr['lon']:.3f}"
                     if sr is not None else "—")
            d_str = (f"#{i+1} {dr['score']:.4f} lat={dr['lat']:.3f} lon={dr['lon']:.3f}"
                     if dr is not None else "—")
            same = (sr is not None and dr is not None
                    and sr["h3_index"] == dr["h3_index"])
            marker = "  (same)" if same else ""
            print(f"  {s_str:<30}  {d_str}{marker}")

    return df


def main():
    parser = argparse.ArgumentParser(description="Scoring pipeline with all 4 fixes")
    parser.add_argument("--city", default=None,
                        choices=["phoenix", "austin"],
                        help="City to score (default: both)")
    args = parser.parse_args()

    cfg        = _load_cfg(_SCORING_CFG_PATH, _SCORING_DEFAULTS)
    cities_cfg = _load_cities()
    targets    = [args.city] if args.city else ["phoenix", "austin"]

    for city_key in targets:
        result_df = run_city(city_key, cfg, cities_cfg)

        if not result_df.empty:
            # Export updated CSV so map generators pick it up
            out_name = f"_{'phx' if city_key == 'phoenix' else 'aus'}_opps_filtered.csv"
            out_path = _ROOT / "docs" / out_name
            result_df.to_csv(out_path, index=False)
            print(f"\n  Exported: {out_path}  ({len(result_df)} rows)\n")

    print("\nDone.")


if __name__ == "__main__":
    main()
