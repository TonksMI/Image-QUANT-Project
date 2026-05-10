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

# Land value per acre estimates by distance ring (USD/acre, 2024 metro medians)
# Austin: Travis CAD median land values by submarket
# Phoenix: Maricopa County median land values by submarket
_LAND_VALUE_CURVE = {
    "austin": [
        (0,   5,  1_800_000),  # urban core
        (5,   12, 600_000),    # inner suburbs
        (12,  25, 250_000),    # outer suburbs
        (25,  40, 120_000),    # fringe
        (40, 999,  50_000),    # exurbs
    ],
    "phoenix": [
        (0,   8,  800_000),
        (8,   20, 300_000),
        (20,  35, 150_000),
        (35,  55,  80_000),
        (55, 999,  35_000),
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


# ── Land value curve ──────────────────────────────────────────────────────────

def _estimate_land_value(dist_km: float, city_name: str) -> float:
    curve = _LAND_VALUE_CURVE.get(city_name, _LAND_VALUE_CURVE["austin"])
    for lo, hi, val in curve:
        if lo <= dist_km < hi:
            return float(val)
    return float(curve[-1][2])


def _dist_km(h3_idx: str, city_id: int) -> float:
    import math
    clat, clon = _CITY_CENTERS[city_id]
    lat, lon = h3.cell_to_latlng(h3_idx)
    R = 6371.0
    dlat = math.radians(lat - clat)
    dlon = math.radians(lon - clon)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(clat)) * math.cos(math.radians(lat))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


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
        dist = _dist_km(h3_idx, city_id)
        land_val = _estimate_land_value(dist, city_name)

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
                    dist = _dist_km(idx, city_id)
                    land_val = _estimate_land_value(dist, city_name)
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
                    dist = _dist_km(idx, city_id)
                    land_val = _estimate_land_value(dist, city_name)
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
