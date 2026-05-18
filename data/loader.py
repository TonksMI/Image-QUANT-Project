"""
Flat-file fallback loader for analysis scripts.

Usage:
    from data.loader import load_lots, load_ic_backtest, load_assumptions

These functions first try the PostgreSQL database. If the DB is unavailable
(no .env, wrong credentials, no server), they fall back to the CSV/JSON files
in this folder so the analysis can run offline.
"""

from __future__ import annotations
import json
import warnings
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent


# ── Helpers ───────────────────────────────────────────────────────────────────

def _try_db_engine():
    """Return a SQLAlchemy engine or None if DB is unavailable."""
    try:
        from dotenv import load_dotenv
        load_dotenv(DATA_DIR.parent / ".env")
        from urbangrowth.db.loaders import _engine
        engine = _engine()
        # Quick connectivity check
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine
    except Exception:
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def load_lots(city: str | None = None, tier: str = "Tier 1", limit: int = 30) -> pd.DataFrame:
    """
    Load top lot opportunities.

    Parameters
    ----------
    city    : 'phoenix', 'austin', or None (both)
    tier    : tier prefix to filter on (default 'Tier 1')
    limit   : max rows per city (only applies to DB path; CSV already capped at 30/city)

    Returns DataFrame with columns:
        h3_index, opportunity_score, acreage_est, dist_to_center_km,
        dominant_property_type, est_land_value_acre, est_construction_months,
        est_cost_per_sqft, nearest_address, city_name, tier, lat, lon
    """
    import h3 as h3lib

    engine = _try_db_engine()

    if engine is not None:
        from sqlalchemy import text
        city_filter = f"AND c.name = '{city}'" if city else ""
        with engine.connect() as conn:
            df = pd.read_sql(text(f"""
                SELECT lo.h3_index, lo.opportunity_score, lo.acreage_est,
                       lo.dist_to_center_km, lo.dominant_property_type,
                       lo.est_land_value_acre, lo.est_construction_months,
                       lo.est_cost_per_sqft,
                       COALESCE(lo.nearest_address,'') as nearest_address,
                       c.name as city_name, lo.tier
                FROM lot_opportunities lo
                JOIN cities c ON lo.city_id = c.city_id
                WHERE lo.tier LIKE '{tier}%' {city_filter}
                ORDER BY lo.opportunity_score DESC
                LIMIT {limit * (1 if city else 2)}
            """), conn)
        if "lat" not in df.columns:
            latlons = [h3lib.cell_to_latlng(idx) for idx in df["h3_index"]]
            df["lat"] = [ll[0] for ll in latlons]
            df["lon"] = [ll[1] for ll in latlons]
        return df.reset_index(drop=True)

    # ── Fallback: flat files ──────────────────────────────────────────────────
    warnings.warn(
        "Database unavailable — loading lots from data/ flat files.",
        stacklevel=2,
    )

    if city == "phoenix":
        paths = [DATA_DIR / "lots_phoenix.csv"]
    elif city == "austin":
        paths = [DATA_DIR / "lots_austin.csv"]
    else:
        paths = [DATA_DIR / "lots_phoenix.csv", DATA_DIR / "lots_austin.csv"]

    frames = [pd.read_csv(p) for p in paths if p.exists()]
    if not frames:
        raise FileNotFoundError(
            f"No flat-file lots found in {DATA_DIR}. "
            "Run the export script or check your DB connection."
        )
    df = pd.concat(frames, ignore_index=True)
    if tier:
        df = df[df["tier"].str.startswith(tier)]
    return df.reset_index(drop=True)


def load_ic_backtest() -> dict:
    """
    Load walk-forward IC backtest results.

    Returns dict:
        {
          "phoenix": {"years": [...], "ic": [...], "note": "..."},
          "austin":  {"years": [...], "ic": [...], "note": "..."}
        }
    """
    path = DATA_DIR / "ic_backtest.json"
    if not path.exists():
        raise FileNotFoundError(f"IC backtest data not found at {path}")
    with open(path) as f:
        return json.load(f)


def load_assumptions() -> dict:
    """
    Load model financial assumptions (cap rates, LTV, costs, etc.).

    Returns the model_assumptions.json dict.
    """
    path = DATA_DIR / "model_assumptions.json"
    if not path.exists():
        raise FileNotFoundError(f"Model assumptions not found at {path}")
    with open(path) as f:
        return json.load(f)


def load_cities() -> pd.DataFrame:
    """Load cities reference table (city_id, name, state)."""
    engine = _try_db_engine()
    if engine is not None:
        from sqlalchemy import text
        with engine.connect() as conn:
            return pd.read_sql(text("SELECT * FROM cities"), conn)

    path = DATA_DIR / "cities.csv"
    if not path.exists():
        raise FileNotFoundError(f"Cities CSV not found at {path}")
    return pd.read_csv(path)
