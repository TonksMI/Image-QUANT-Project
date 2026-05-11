"""Parcel-level enrichment: property type, land value, address per H3 cell.

Aggregates parcel data from open sources into h3_parcel_enrichments table,
keyed by (h3_index, city_id).  Used by lot_finder to score cells on:
  - dominant property type (what kind of development is currently present)
  - estimated land value per acre (cost proxy from TCAD benchmarks)
  - nearest address via Nominatim reverse geocoding
  - construction timeline estimate by property type (RSMeans benchmarks)
  - construction cost per sqft estimate

Sources:
  Austin (city_id=2):
    City of Austin Land Database — Socrata resource kk8y-6cmt
    271k parcels with lu_desc (land use description), geometry

  Phoenix (city_id=1):
    Nominatim reverse geocoding for H3 cell centroids (no bulk parcel API
    available from Maricopa County without authentication)

Land values:
  TCAD fixed-width export would give exact values but requires complex parsing.
  Instead, we estimate from a calibrated distance-to-center curve using
  Austin/Phoenix metro market data (medians from Texas Comptroller & Zillow).

Usage:
    python -m urbangrowth.data.parcel_enrichment
    python -m urbangrowth.data.parcel_enrichment --city austin
    python -m urbangrowth.data.parcel_enrichment --city phoenix --geocode-phoenix
"""
from __future__ import annotations

import argparse
import json
import time
from typing import Optional

import h3
import numpy as np
import pandas as pd
import requests
import structlog
from dotenv import load_dotenv
from sqlalchemy import text

from urbangrowth.db.loaders import _engine

load_dotenv()
log = structlog.get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_CITY_IDS = {"phoenix": 1, "austin": 2}
_CITY_CENTERS = {1: (33.4484, -112.0740), 2: (30.2672, -97.7431)}
_H3_RES = 8

# Austin: City of Austin Land Database (Socrata)
_ATX_PARCEL_URL = "https://data.austintexas.gov/resource/kk8y-6cmt.json"
_ATX_PAGE_LIMIT = 50_000

# Nominatim reverse geocoding
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_NOMINATIM_HEADERS = {"User-Agent": "urbangrowth-research/1.0"}

# Austin land use description → canonical property type
_ATX_LU_MAP = {
    "single family":           "residential",
    "large-lot single family": "residential",
    "duplexes":                "residential",
    "mobile homes":            "residential",
    "retirement housing":      "residential",
    "group quarters":          "residential",
    "apartment/condo":         "multifamily",
    "semi-institutional housi":"multifamily",
    "commercial":              "commercial",
    "mixed use":               "commercial",
    "office":                  "commercial",
    "retail":                  "commercial",
    "manufacturing":           "industrial",
    "warehousing":             "industrial",
    "miscellaneous industrial":"industrial",
    "resource extraction":     "industrial",
    "landfills":               "industrial",
    "undeveloped":             "vacant",
    "agricultural":            "vacant",
    "drainage areas":          "open_space",
    "parks/greenbelts":        "open_space",
    "preserves":               "open_space",
    "golf courses":            "open_space",
    "cemetery":                "open_space",
    "right-of-way":            "infrastructure",
    "parking":                 "infrastructure",
    "railroad facilities":     "infrastructure",
    "aviation facilities":     "infrastructure",
}

# Construction timeline estimates in months by property type (RSMeans TX/AZ)
CONSTRUCTION_MONTHS: dict[str, int] = {
    "vacant":        0,
    "residential":   9,    # SFR stick-frame: 6-12 months
    "multifamily":   20,   # garden/mid-rise: 14-28 months
    "commercial":    16,   # retail/office: 12-24 months
    "industrial":    12,   # tilt-wall warehouse: 8-16 months
    "open_space":    0,
    "infrastructure":0,
    "unknown":       12,
}

# Construction hard cost per sqft (2025 USD, TX/AZ adjusted)
CONSTRUCTION_COST_PER_SQFT: dict[str, float] = {
    "vacant":        0.0,
    "residential":   175.0,   # SFR stick-frame
    "multifamily":   215.0,   # garden-style wood-frame
    "commercial":    230.0,   # steel frame retail/office
    "industrial":    105.0,   # tilt-wall warehouse
    "open_space":    0.0,
    "infrastructure":0.0,
    "unknown":       180.0,
}

# ── Multi-anchor land value model ────────────────────────────────────────────
# Land value is driven by proximity to multiple attractors, not just the CBD.
# Each city defines a list of value anchors: (lat, lon, base_value, decay_km).
#   base_value: $/acre at the anchor centroid
#   decay_km:   distance at which value falls to base_value/e (~37%)
#
# The estimated land value for a cell is the MAX across all anchor estimates,
# because value reflects whichever attractor you're closest to.
#
# Sources: Travis CAD medians, Maricopa County medians, CoStar submarket data
_CITY_VALUE_ANCHORS: dict[str, list[dict]] = {
    "austin": [
        # CBD / 6th Street core
        {"lat": 30.2672, "lon": -97.7431, "base": 1_800_000, "decay_km": 4.0},
        # South Congress / SoCo corridor
        {"lat": 30.2435, "lon": -97.7502, "base": 800_000,   "decay_km": 3.5},
        # Domain / North Austin tech corridor
        {"lat": 30.4013, "lon": -97.7211, "base": 700_000,   "decay_km": 5.0},
        # East Austin / Mueller redevelopment
        {"lat": 30.3001, "lon": -97.7100, "base": 600_000,   "decay_km": 3.0},
        # Round Rock / Georgetown fringe (distant but growing)
        {"lat": 30.5086, "lon": -97.6789, "base": 200_000,   "decay_km": 6.0},
        # South Austin / Buda fringe
        {"lat": 30.0850, "lon": -97.8406, "base": 180_000,   "decay_km": 5.0},
        # Cedar Park / Leander fringe (NW corridor)
        {"lat": 30.5052, "lon": -97.8203, "base": 250_000,   "decay_km": 5.0},
        # Baseline rural floor (exurbs)
        {"lat": 30.2672, "lon": -97.7431, "base": 50_000,    "decay_km": 999.0},
    ],
    "phoenix": [
        # CBD core
        {"lat": 33.4484, "lon": -112.0740, "base": 800_000,  "decay_km": 5.0},
        # Scottsdale / Old Town high-value corridor
        {"lat": 33.4942, "lon": -111.9261, "base": 600_000,  "decay_km": 5.0},
        # Tempe / ASU area
        {"lat": 33.4255, "lon": -111.9400, "base": 450_000,  "decay_km": 4.0},
        # Chandler tech corridor (Intel, TSMC)
        {"lat": 33.3062, "lon": -111.8413, "base": 400_000,  "decay_km": 6.0},
        # Mesa / Gilbert growing fringe
        {"lat": 33.3528, "lon": -111.7890, "base": 300_000,  "decay_km": 6.0},
        # Peoria / Surprise NW growth corridor
        {"lat": 33.5806, "lon": -112.2374, "base": 250_000,  "decay_km": 7.0},
        # Goodyear / Avondale SW industrial/residential
        {"lat": 33.4353, "lon": -112.3576, "base": 220_000,  "decay_km": 6.0},
        # Baseline rural floor (exurbs)
        {"lat": 33.4484, "lon": -112.0740, "base": 35_000,   "decay_km": 999.0},
    ],
    # Template for future expansion — LA shows coast + canyon + employment anchors
    "los_angeles": [
        # Santa Monica / coastal premium
        {"lat": 34.0195, "lon": -118.4912, "base": 8_000_000, "decay_km": 3.0},
        # Beverly Hills / Westside core
        {"lat": 34.0736, "lon": -118.4004, "base": 5_000_000, "decay_km": 4.0},
        # Downtown LA employment
        {"lat": 34.0522, "lon": -118.2437, "base": 2_000_000, "decay_km": 5.0},
        # Hollywood / Silverlake urban
        {"lat": 34.0928, "lon": -118.3287, "base": 1_500_000, "decay_km": 3.5},
        # Culver City / tech corridor (Amazon, Apple)
        {"lat": 34.0211, "lon": -118.3965, "base": 2_500_000, "decay_km": 3.0},
        # San Fernando Valley (Burbank, NoHo)
        {"lat": 34.1808, "lon": -118.3090, "base": 1_000_000, "decay_km": 5.0},
        # SGV / Inland fringe
        {"lat": 34.0689, "lon": -117.9300, "base": 400_000,   "decay_km": 8.0},
        # Baseline floor
        {"lat": 34.0522, "lon": -118.2437, "base": 300_000,   "decay_km": 999.0},
    ],
}

_DDL = """
CREATE TABLE IF NOT EXISTS h3_parcel_enrichments (
    h3_index               TEXT    NOT NULL,
    city_id                INTEGER NOT NULL,
    parcel_count           INTEGER,
    dominant_type          TEXT,
    type_mix               JSONB,
    est_land_value_acre    FLOAT,
    nearest_address        TEXT,
    est_construction_months INTEGER,
    est_cost_per_sqft      FLOAT,
    PRIMARY KEY (h3_index, city_id)
);
"""


# ── Land value estimation ─────────────────────────────────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _estimate_land_value(h3_idx: str, city_name: str) -> float:
    """Multi-anchor land value estimate using exponential decay from each attractor.

    Value at each anchor follows V(d) = base * exp(-d / decay_km).
    The cell's estimated value is the MAX across all anchors — it benefits from
    whichever attractor it's closest to.

    This correctly handles:
    - Multiple employment subcenter gradients (Domain, Chandler tech, etc.)
    - Coastal premiums when anchors are placed at the shore (LA)
    - Far-fringe floor via a wide-decay baseline anchor
    """
    import math
    anchors = _CITY_VALUE_ANCHORS.get(city_name)
    if not anchors:
        return 50_000.0

    lat, lon = h3.cell_to_latlng(h3_idx)
    best = 0.0
    for a in anchors:
        d = _haversine_km(lat, lon, a["lat"], a["lon"])
        v = a["base"] * math.exp(-d / a["decay_km"])
        if v > best:
            best = v
    return float(best)


def _dist_km(h3_idx: str, city_id: int) -> float:
    clat, clon = _CITY_CENTERS[city_id]
    lat, lon = h3.cell_to_latlng(h3_idx)
    return _haversine_km(lat, lon, clat, clon)


# ── Nominatim ────────────────────────────────────────────────────────────────

def _reverse_geocode(lat: float, lon: float) -> str:
    try:
        r = requests.get(
            _NOMINATIM_URL,
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 16},
            headers=_NOMINATIM_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        addr = data.get("address", {})
        parts = [
            addr.get("house_number", ""),
            addr.get("road", addr.get("street", "")),
            addr.get("suburb", addr.get("neighbourhood", addr.get("city_district", ""))),
        ]
        return " ".join(p for p in parts if p).strip()
    except Exception:
        return ""


def _geocode_cells(h3_indices: list[str], rate: float = 1.1) -> dict[str, str]:
    result: dict[str, str] = {}
    log.info("nominatim_geocoding", count=len(h3_indices))
    for i, idx in enumerate(h3_indices):
        lat, lon = h3.cell_to_latlng(idx)
        result[idx] = _reverse_geocode(lat, lon)
        if (i + 1) % 25 == 0:
            log.info("geocoding_progress", done=i + 1, total=len(h3_indices))
        time.sleep(rate)
    return result


# ── Austin parcel ingestion ───────────────────────────────────────────────────

def _lu_desc_to_type(lu_desc: str | None) -> str:
    if not lu_desc:
        return "unknown"
    lower = str(lu_desc).lower().strip()
    for key, val in _ATX_LU_MAP.items():
        if key in lower:
            return val
    return "unknown"


def _geom_centroid(geom: dict) -> tuple[float, float] | None:
    """Extract approximate centroid from a GeoJSON geometry dict."""
    try:
        gtype = geom.get("type", "")
        coords = geom.get("coordinates", [])
        if gtype == "Point":
            return float(coords[1]), float(coords[0])
        elif gtype in ("Polygon", "MultiPolygon"):
            # Flatten all coordinate pairs and average
            flat: list[list[float]] = []
            if gtype == "Polygon":
                for ring in coords:
                    flat.extend(ring)
            else:
                for poly in coords:
                    for ring in poly:
                        flat.extend(ring)
            if flat:
                lons = [c[0] for c in flat]
                lats = [c[1] for c in flat]
                return sum(lats) / len(lats), sum(lons) / len(lons)
    except Exception:
        pass
    return None


def _fetch_atx_parcels() -> pd.DataFrame:
    """Page through the Austin City Land Database and return a DataFrame."""
    all_rows: list[dict] = []
    offset = 0
    page = 1

    while True:
        params = {
            "$limit": _ATX_PAGE_LIMIT,
            "$offset": offset,
            "$select": "the_geom,lu_desc,gen_lu_des,land_use",
        }
        log.info("atx_parcel_fetch", page=page, offset=offset)
        try:
            r = requests.get(_ATX_PARCEL_URL, params=params, timeout=120)
            r.raise_for_status()
            rows = r.json()
        except Exception as exc:
            log.warning("atx_fetch_error", error=str(exc), page=page)
            break

        if not rows:
            break
        all_rows.extend(rows)
        log.info("atx_page_received", page=page, n=len(rows), total=len(all_rows))
        if len(rows) < _ATX_PAGE_LIMIT:
            break
        offset += _ATX_PAGE_LIMIT
        page += 1

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    log.info("atx_parcels_raw", rows=len(df))

    # Extract centroid from geometry
    centroids = df["the_geom"].apply(
        lambda g: _geom_centroid(g) if isinstance(g, dict) else None
    )
    df["lat"] = centroids.apply(lambda c: c[0] if c else np.nan)
    df["lon"] = centroids.apply(lambda c: c[1] if c else np.nan)
    df = df[df["lat"].notna() & df["lon"].notna()].copy()

    df["property_type"] = df["lu_desc"].apply(_lu_desc_to_type)
    log.info("atx_parcels_geocoded", rows=len(df))
    return df


def _to_h3(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["h3_index"] = df.apply(
        lambda r: h3.latlng_to_cell(float(r["lat"]), float(r["lon"]), _H3_RES),
        axis=1,
    )
    return df


def _aggregate_to_h3(df: pd.DataFrame, city_id: int, city_name: str) -> pd.DataFrame:
    agg = []
    for h3_idx, grp in df.groupby("h3_index"):
        tc = grp["property_type"].value_counts()
        dominant = tc.index[0] if len(tc) > 0 else "unknown"
        land_val = _estimate_land_value(h3_idx, city_name)

        agg.append({
            "h3_index":                h3_idx,
            "city_id":                 city_id,
            "parcel_count":            len(grp),
            "dominant_type":           dominant,
            "type_mix":                tc.to_dict(),
            "est_land_value_acre":     land_val,
            "nearest_address":         "",   # filled in separately via Nominatim
            "est_construction_months": CONSTRUCTION_MONTHS.get(dominant, 12),
            "est_cost_per_sqft":       CONSTRUCTION_COST_PER_SQFT.get(dominant, 180.0),
        })

    result = pd.DataFrame(agg)
    log.info("h3_aggregation_done", city_id=city_id, cells=len(result))
    return result


# ── Geocode opportunity cells ─────────────────────────────────────────────────

def _geocode_opportunity_cells(engine, city_id: int, limit: int = 500) -> dict[str, str]:
    """Reverse geocode the top opportunity cells that lack an address."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT lo.h3_index
            FROM lot_opportunities lo
            LEFT JOIN h3_parcel_enrichments pe
              ON pe.h3_index = lo.h3_index AND pe.city_id = lo.city_id
            WHERE lo.city_id = :cid
              AND lo.tier IN ('Tier 1 — Top 10%', 'Tier 2 — Top 25%')
              AND (pe.nearest_address IS NULL OR pe.nearest_address = '')
            ORDER BY lo.opportunity_score DESC
            LIMIT :lim
        """), {"cid": city_id, "lim": limit}).fetchall()
    cells = [r[0] for r in rows]
    if not cells:
        return {}
    return _geocode_cells(cells)


# ── Persistence ───────────────────────────────────────────────────────────────

def _upsert_enrichments(rows: list[dict], engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(_DDL))
        for i in range(0, len(rows), 500):
            chunk = rows[i : i + 500]
            conn.execute(text("""
                INSERT INTO h3_parcel_enrichments
                    (h3_index, city_id, parcel_count, dominant_type, type_mix,
                     est_land_value_acre, nearest_address,
                     est_construction_months, est_cost_per_sqft)
                VALUES
                    (:h3_index, :city_id, :parcel_count, :dominant_type, CAST(:type_mix AS jsonb),
                     :est_land_value_acre, :nearest_address,
                     :est_construction_months, :est_cost_per_sqft)
                ON CONFLICT (h3_index, city_id) DO UPDATE SET
                    parcel_count            = EXCLUDED.parcel_count,
                    dominant_type           = EXCLUDED.dominant_type,
                    type_mix                = EXCLUDED.type_mix,
                    est_land_value_acre     = EXCLUDED.est_land_value_acre,
                    nearest_address         = COALESCE(NULLIF(EXCLUDED.nearest_address,''),
                                                       h3_parcel_enrichments.nearest_address),
                    est_construction_months = EXCLUDED.est_construction_months,
                    est_cost_per_sqft       = EXCLUDED.est_cost_per_sqft
            """), chunk)
    log.info("enrichments_upserted", rows=len(rows))


def _update_addresses(addr_map: dict[str, str], city_id: int, engine) -> None:
    if not addr_map:
        return
    with engine.begin() as conn:
        for h3_idx, addr in addr_map.items():
            if addr:
                conn.execute(text("""
                    UPDATE h3_parcel_enrichments
                    SET nearest_address = :addr
                    WHERE h3_index = :idx AND city_id = :cid
                """), {"addr": addr, "idx": h3_idx, "cid": city_id})
    log.info("addresses_updated", count=sum(1 for v in addr_map.values() if v))


# ── Entry point ───────────────────────────────────────────────────────────────

def run(cities: list[str] | None = None, geocode_phoenix: bool = False,
        geocode_addresses: bool = True) -> None:
    engine = _engine()
    targets = {k: v for k, v in _CITY_IDS.items()
               if cities is None or k in cities}

    for city_name, city_id in targets.items():
        log.info("enrichment_start", city=city_name)

        if city_name == "austin":
            raw = _fetch_atx_parcels()
            if not raw.empty:
                raw = _to_h3(raw)
                enriched = _aggregate_to_h3(raw, city_id, city_name)
                rows = []
                for _, r in enriched.iterrows():
                    rows.append({
                        "h3_index":                str(r["h3_index"]),
                        "city_id":                 city_id,
                        "parcel_count":            int(r["parcel_count"]),
                        "dominant_type":           str(r["dominant_type"]),
                        "type_mix":                json.dumps(r["type_mix"]),
                        "est_land_value_acre":     float(r["est_land_value_acre"]),
                        "nearest_address":         "",
                        "est_construction_months": int(r["est_construction_months"]),
                        "est_cost_per_sqft":       float(r["est_cost_per_sqft"]),
                    })
                _upsert_enrichments(rows, engine)
            else:
                log.warning("austin_no_parcel_data")

        elif city_name == "phoenix":
            if geocode_phoenix:
                # Phoenix: build enrichment rows from distance-curve land values
                # for all top-tier opportunity cells
                with engine.connect() as conn:
                    cells = [r[0] for r in conn.execute(text("""
                        SELECT DISTINCT h3_index FROM lot_opportunities
                        WHERE city_id = :cid
                          AND tier IN ('Tier 1 — Top 10%', 'Tier 2 — Top 25%')
                        ORDER BY h3_index
                    """), {"cid": city_id}).fetchall()]

                rows = []
                for idx in cells:
                    land_val = _estimate_land_value(idx, city_name)
                    rows.append({
                        "h3_index":                idx,
                        "city_id":                 city_id,
                        "parcel_count":            None,
                        "dominant_type":           "unknown",
                        "type_mix":                "{}",
                        "est_land_value_acre":     land_val,
                        "nearest_address":         "",
                        "est_construction_months": CONSTRUCTION_MONTHS["unknown"],
                        "est_cost_per_sqft":       CONSTRUCTION_COST_PER_SQFT["unknown"],
                    })
                _upsert_enrichments(rows, engine)
            else:
                log.info("phoenix_land_value_only",
                         hint="Use --geocode-phoenix to also populate addresses via Nominatim")
                with engine.connect() as conn:
                    cells = [r[0] for r in conn.execute(text("""
                        SELECT DISTINCT h3_index FROM lot_opportunities
                        WHERE city_id = :cid
                    """), {"cid": city_id}).fetchall()]
                rows = []
                for idx in cells:
                    land_val = _estimate_land_value(idx, city_name)
                    rows.append({
                        "h3_index":                idx,
                        "city_id":                 city_id,
                        "parcel_count":            None,
                        "dominant_type":           "unknown",
                        "type_mix":                "{}",
                        "est_land_value_acre":     land_val,
                        "nearest_address":         "",
                        "est_construction_months": CONSTRUCTION_MONTHS["unknown"],
                        "est_cost_per_sqft":       CONSTRUCTION_COST_PER_SQFT["unknown"],
                    })
                _upsert_enrichments(rows, engine)

        # Reverse geocode top opportunity cells for addresses
        if geocode_addresses:
            log.info("geocoding_top_cells", city=city_name)
            addr_map = _geocode_opportunity_cells(engine, city_id, limit=300)
            _update_addresses(addr_map, city_id, engine)

        log.info("enrichment_done", city=city_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Parcel enrichment ingestion")
    parser.add_argument("--city", type=str, default=None)
    parser.add_argument("--geocode-phoenix", action="store_true")
    parser.add_argument("--no-geocode", action="store_true",
                        help="Skip Nominatim geocoding (faster, no addresses)")
    args = parser.parse_args()
    run(
        cities=[args.city] if args.city else None,
        geocode_phoenix=args.geocode_phoenix,
        geocode_addresses=not args.no_geocode,
    )


if __name__ == "__main__":
    main()
