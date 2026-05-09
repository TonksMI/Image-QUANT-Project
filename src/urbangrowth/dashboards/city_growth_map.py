"""Interactive city investment score map.

Renders an H3-choropleth map showing which sub-city areas are predicted to
grow fastest in built_pct over the next 12 months (investment_score from
h3_predictions table).

Outputs:
    D:/urbangrowth_data/processed/maps/city_growth_map_{city}.html  (Folium)
    D:/urbangrowth_data/processed/maps/city_growth_map_{city}.png   (static screenshot via kaleido/plotly)

Usage:
    python -m urbangrowth.dashboards.city_growth_map
    python -m urbangrowth.dashboards.city_growth_map --city phoenix
    python -m urbangrowth.dashboards.city_growth_map --city austin
"""
from __future__ import annotations

import argparse
import json
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
    "phoenix": {
        "city_id": 1,
        "center": [33.4484, -112.0740],
        "zoom": 10,
    },
    "austin": {
        "city_id": 2,
        "center": [30.2672, -97.7431],
        "zoom": 11,
    },
}

_OUT_DIR = data_path("processed/maps")


def _load_predictions(city_id: int) -> pd.DataFrame:
    engine = _engine()
    with engine.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT p.h3_index, p.investment_score, p.as_of_date,
                       f.built_pct, f.veg_pct
                FROM h3_predictions p
                LEFT JOIN h3_features f
                    ON f.h3_index = p.h3_index
                    AND f.city_id = p.city_id
                    AND f.date = p.as_of_date
                WHERE p.city_id = :city_id
                  AND p.model_version = 'gbm_v1'
                ORDER BY p.as_of_date DESC
            """),
            conn,
            params={"city_id": city_id},
        )
    if df.empty:
        log.warning("no_predictions_found", city_id=city_id)
        return df
    # Use most recent as_of_date only
    latest = df["as_of_date"].max()
    df = df[df["as_of_date"] == latest].copy()
    log.info("predictions_loaded", city_id=city_id, rows=len(df), as_of=str(latest))
    return df


def _h3_to_geojson(h3_index: str) -> dict:
    boundary = h3.cell_to_boundary(h3_index)
    coords = [[lng, lat] for lat, lng in boundary]
    coords.append(coords[0])  # close ring
    return {"type": "Polygon", "coordinates": [coords]}


def _score_to_tier(score: float, q10: float, q25: float, q75: float, q90: float) -> str:
    if score >= q90:
        return "Top 10% — High growth potential"
    elif score >= q75:
        return "Top 25%"
    elif score >= q25:
        return "Mid range"
    else:
        return "Low growth potential"


def build_folium_map(city_name: str) -> Path:
    cfg = _CITY_CONFIG[city_name]
    preds = _load_predictions(cfg["city_id"])
    if preds.empty:
        log.warning("skipping_empty", city=city_name)
        return Path()

    q10 = preds["investment_score"].quantile(0.10)
    q25 = preds["investment_score"].quantile(0.25)
    q75 = preds["investment_score"].quantile(0.75)
    q90 = preds["investment_score"].quantile(0.90)

    colormap = LinearColormap(
        colors=["#2166ac", "#92c5de", "#f7f7f7", "#f4a582", "#d6604d", "#b2182b"],
        vmin=preds["investment_score"].min(),
        vmax=preds["investment_score"].max(),
        caption="Investment Score (12-month growth probability)",
    )

    m = folium.Map(
        location=cfg["center"],
        zoom_start=cfg["zoom"],
        tiles="CartoDB positron",
    )

    # Downsample for performance: include all top-10% cells + 20% random sample of rest
    top = preds[preds["investment_score"] >= q90]
    rest = preds[preds["investment_score"] < q90].sample(
        frac=0.2, random_state=42
    )
    subset = pd.concat([top, rest])
    log.info("rendering_cells", city=city_name, total=len(preds), rendered=len(subset))

    for _, row in subset.iterrows():
        geo = _h3_to_geojson(row["h3_index"])
        score = float(row["investment_score"])
        tier = _score_to_tier(score, q10, q25, q75, q90)
        built = row.get("built_pct") or np.nan
        veg = row.get("veg_pct") or np.nan
        built_str = f"{built*100:.1f}%" if not np.isnan(built) else "N/A"
        veg_str = f"{veg*100:.1f}%" if not np.isnan(veg) else "N/A"

        popup_html = f"""
        <b>H3 Cell:</b> {row['h3_index']}<br>
        <b>Investment Score:</b> {score:.3f}<br>
        <b>Tier:</b> {tier}<br>
        <b>Current Built %:</b> {built_str}<br>
        <b>Current Veg %:</b> {veg_str}<br>
        <b>As of:</b> {row['as_of_date']}
        """

        folium.GeoJson(
            data=geo,
            style_function=lambda feature, s=score: {
                "fillColor": colormap(s),
                "color": "none",
                "fillOpacity": 0.65 + 0.25 * (s - preds["investment_score"].min())
                / max(preds["investment_score"].max() - preds["investment_score"].min(), 1e-9),
                "weight": 0,
            },
            tooltip=folium.Tooltip(f"Score: {score:.3f} | {tier}"),
            popup=folium.Popup(popup_html, max_width=300),
        ).add_to(m)

    colormap.add_to(m)

    # Add a title
    title_html = f"""
    <div style="position: fixed; top: 10px; left: 50px; z-index:1000;
                background-color: white; padding: 10px; border-radius: 5px;
                box-shadow: 2px 2px 6px rgba(0,0,0,0.3); font-family: Arial;">
        <b>Urban Growth — {city_name.title()} Investment Map</b><br>
        <small>GBM model | 12-month built-area growth prediction</small>
    </div>
    """
    m.get_root().html.add_child(folium.Element(title_html))

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _OUT_DIR / f"city_growth_map_{city_name}.html"
    m.save(str(out_path))
    log.info("map_saved", city=city_name, path=str(out_path))
    return out_path


def run(cities: list[str] | None = None) -> None:
    targets = cities or list(_CITY_CONFIG.keys())
    for city in targets:
        if city not in _CITY_CONFIG:
            log.warning("unknown_city", city=city, known=list(_CITY_CONFIG.keys()))
            continue
        build_folium_map(city)


def main() -> None:
    parser = argparse.ArgumentParser(description="City H3 investment score map")
    parser.add_argument("--city", type=str, default=None,
                        help="City name (phoenix or austin). Defaults to all.")
    args = parser.parse_args()
    run(cities=[args.city] if args.city else None)


if __name__ == "__main__":
    main()
