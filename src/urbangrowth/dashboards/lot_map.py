"""Interactive empty lot opportunity map.

Renders a Folium choropleth showing underdeveloped H3 cells ranked by
opportunity_score from the lot_opportunities table.

Outputs:
    D:/urbangrowth_data/processed/maps/lot_map_{city}.html

Usage:
    python -m urbangrowth.dashboards.lot_map
    python -m urbangrowth.dashboards.lot_map --city austin
"""
from __future__ import annotations

import argparse
from pathlib import Path

import folium
import h3
import numpy as np
import pandas as pd
import structlog
from branca.colormap import LinearColormap
from dotenv import load_dotenv
from sqlalchemy import text

from urbangrowth.config import data_path
from urbangrowth.db.loaders import _engine

load_dotenv()
log = structlog.get_logger(__name__)

_CITY_CONFIG = {
    "phoenix": {"city_id": 1, "center": [33.4484, -112.0740], "zoom": 10},
    "austin":  {"city_id": 2, "center": [30.2672, -97.7431],  "zoom": 11},
}

_OUT_DIR = data_path("processed/maps")

_TIER_COLORS = {
    "Tier 1 — Top 10%": "#b2182b",
    "Tier 2 — Top 25%": "#f4a582",
    "Tier 3 — Top 50%": "#92c5de",
    "Below threshold":   "#d9d9d9",
}


def _load_opportunities(city_id: int, engine) -> pd.DataFrame:
    with engine.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT h3_index, opportunity_score, investment_score,
                       built_pct, veg_pct, neighbor_velocity,
                       ring_score, dist_to_center_km, zoning_category,
                       acreage_est, tier, as_of_date,
                       nearest_address, dominant_property_type,
                       est_land_value_acre, est_construction_months, est_cost_per_sqft
                FROM lot_opportunities
                WHERE city_id = :cid
                ORDER BY as_of_date DESC, opportunity_score DESC
            """),
            conn,
            params={"cid": city_id},
        )
    if df.empty:
        return df
    latest = df["as_of_date"].max()
    df = df[df["as_of_date"] == latest].copy()
    log.info("opportunities_loaded", city_id=city_id, rows=len(df), as_of=str(latest))
    return df


def _h3_to_geojson(h3_index: str) -> dict:
    boundary = h3.cell_to_boundary(h3_index)
    coords = [[lng, lat] for lat, lng in boundary]
    coords.append(coords[0])
    return {"type": "Polygon", "coordinates": [coords]}


def build_lot_map(city_name: str, engine) -> Path:
    cfg = _CITY_CONFIG[city_name]
    opps = _load_opportunities(cfg["city_id"], engine)
    if opps.empty:
        log.warning("no_opportunities", city=city_name,
                    hint="Run: python -m urbangrowth.modeling.lot_finder")
        return Path()

    colormap = LinearColormap(
        colors=["#d9d9d9", "#92c5de", "#f4a582", "#b2182b"],
        vmin=opps["opportunity_score"].min(),
        vmax=opps["opportunity_score"].max(),
        caption="Lot Opportunity Score (0=low, 1=high)",
    )

    m = folium.Map(
        location=cfg["center"],
        zoom_start=cfg["zoom"],
        tiles="CartoDB positron",
    )

    # Render all Tier 1 + 2 + 50% sample of Tier 3
    tier1_2 = opps[opps["tier"].isin(["Tier 1 — Top 10%", "Tier 2 — Top 25%"])]
    tier3 = opps[opps["tier"] == "Tier 3 — Top 50%"].sample(
        frac=0.5, random_state=42
    ) if "Tier 3 — Top 50%" in opps["tier"].values else pd.DataFrame()
    subset = pd.concat([tier1_2, tier3])
    log.info("rendering_cells", city=city_name, total=len(opps), rendered=len(subset))

    for _, row in subset.iterrows():
        geo = _h3_to_geojson(row["h3_index"])
        score = float(row["opportunity_score"])
        tier  = row.get("tier", "")
        built = row.get("built_pct")
        veg   = row.get("veg_pct")
        inv   = row.get("investment_score")
        nv    = row.get("neighbor_velocity", 0) or 0
        rs    = row.get("ring_score")
        dist  = row.get("dist_to_center_km")
        zone  = row.get("zoning_category") or "unknown"
        acres = row.get("acreage_est")
        addr  = row.get("nearest_address") or ""
        ptype = row.get("dominant_property_type") or "unknown"
        lval  = row.get("est_land_value_acre")
        cmons = row.get("est_construction_months")
        ccost = row.get("est_cost_per_sqft")

        def _fmt_pct(v) -> str:
            return f"{float(v)*100:.1f}%" if v is not None and not (isinstance(v, float) and np.isnan(v)) else "N/A"

        def _fmt_float(v, fmt=".3f") -> str:
            return format(float(v), fmt) if v is not None and not (isinstance(v, float) and np.isnan(v)) else "N/A"

        def _fmt_currency(v) -> str:
            if v is None or (isinstance(v, float) and np.isnan(v)):
                return "N/A"
            n = float(v)
            return f"${n:,.0f}"

        addr_line = f"<b>Nearest Address:</b> {addr}<br>" if addr else ""
        lval_str  = _fmt_currency(lval)
        cmon_str  = f"{int(cmons)} months" if cmons is not None and not (isinstance(cmons, float) and np.isnan(cmons)) else "N/A"
        ccost_str = f"${float(ccost):.0f}/sqft" if ccost is not None and not (isinstance(ccost, float) and np.isnan(ccost)) else "N/A"

        popup_html = f"""
        <b>H3 Cell:</b> {row['h3_index']}<br>
        <b>Opportunity Score:</b> {score:.3f}<br>
        <b>Tier:</b> {tier}<br>
        {addr_line}
        <hr style="margin:4px 0">
        <b>Property Type:</b> {ptype}<br>
        <b>Est. Land Value:</b> {lval_str}/acre<br>
        <b>Est. Construction Cost:</b> {ccost_str}<br>
        <b>Est. Build Timeline:</b> {cmon_str}<br>
        <hr style="margin:4px 0">
        <b>Current Built %:</b> {_fmt_pct(built)}<br>
        <b>Current Veg %:</b> {_fmt_pct(veg)}<br>
        <b>Investment Score (GBM):</b> {_fmt_float(inv)}<br>
        <b>Neighbor Permit Velocity:</b> {int(nv)}<br>
        <b>Fringe Ring Score:</b> {_fmt_float(rs)}<br>
        <b>Dist to Center:</b> {_fmt_float(dist, '.1f')} km<br>
        <b>Zoning:</b> {zone}<br>
        <b>Est. Acreage:</b> {_fmt_float(acres, '.1f')} ac<br>
        <b>As of:</b> {row['as_of_date']}
        """

        folium.GeoJson(
            data=geo,
            style_function=lambda feature, s=score: {
                "fillColor": colormap(s),
                "color": "black" if s >= opps["opportunity_score"].quantile(0.90) else "none",
                "weight": 0.5 if s >= opps["opportunity_score"].quantile(0.90) else 0,
                "fillOpacity": 0.55 + 0.35 * (s - opps["opportunity_score"].min())
                / max(opps["opportunity_score"].max() - opps["opportunity_score"].min(), 1e-9),
            },
            tooltip=folium.Tooltip(f"Score: {score:.3f} | {tier}"),
            popup=folium.Popup(popup_html, max_width=320),
        ).add_to(m)

    colormap.add_to(m)

    # Legend
    legend_html = "".join(
        f'<div style="display:flex;align-items:center;margin-bottom:3px;">'
        f'<div style="width:14px;height:14px;background:{color};margin-right:6px;border-radius:2px;"></div>'
        f'<small>{label}</small></div>'
        for label, color in _TIER_COLORS.items()
    )
    title_html = f"""
    <div style="position:fixed;top:10px;left:50px;z-index:1000;
                background:white;padding:12px;border-radius:6px;
                box-shadow:2px 2px 8px rgba(0,0,0,0.3);font-family:Arial;max-width:260px;">
        <b style="font-size:13px;">Urban Growth — {city_name.title()} Vacant Lot Map</b><br>
        <small style="color:#555;">Underdeveloped cells with high growth potential</small>
        <hr style="margin:6px 0">
        {legend_html}
        <hr style="margin:6px 0">
        <small><b>{len(tier1_2)}</b> high-opportunity cells shown</small>
    </div>
    """
    m.get_root().html.add_child(folium.Element(title_html))

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _OUT_DIR / f"lot_map_{city_name}.html"
    m.save(str(out_path))
    log.info("lot_map_saved", city=city_name, path=str(out_path))
    return out_path


def run(cities: list[str] | None = None) -> None:
    engine = _engine()
    targets = cities or list(_CITY_CONFIG.keys())
    for city in targets:
        if city not in _CITY_CONFIG:
            log.warning("unknown_city", city=city)
            continue
        build_lot_map(city, engine)


def main() -> None:
    parser = argparse.ArgumentParser(description="Vacant lot opportunity map")
    parser.add_argument("--city", type=str, default=None,
                        help="City (phoenix or austin). Defaults to all.")
    args = parser.parse_args()
    run(cities=[args.city] if args.city else None)


if __name__ == "__main__":
    main()
