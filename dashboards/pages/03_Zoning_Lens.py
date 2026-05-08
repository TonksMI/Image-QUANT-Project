"""Page 3 — Zoning Lens.

Zoning overlay · development concentration by zoning class ·
cells developing outside permitted zones (rezone leads)
"""
from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).parents[2]
_DASH = Path(__file__).parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_DASH))

import pandas as pd
import streamlit as st

from data import load_zoning_h3, load_h3_month, h3_available_months
from components.maps import h3_deck

st.set_page_config(page_title="Zoning Lens", layout="wide")
st.title("Zoning Lens")

with st.sidebar:
    city = st.selectbox("City", ["phoenix", "austin"])

CITY_CENTERS = {
    "phoenix": (33.45, -112.07, 9),
    "austin":  (30.27, -97.74,  9),
}
lat, lon, zoom = CITY_CENTERS.get(city, (33.45, -112.07, 9))

# ── Load data ─────────────────────────────────────────────────────────────────
zoning  = load_zoning_h3(city)
months  = h3_available_months(city)

if months:
    month = st.select_slider("Month", options=months, value=months[-1])
    h3_month = load_h3_month(city, month)
else:
    h3_month = pd.DataFrame()
    month = None

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_overlay, tab_conc, tab_leads = st.tabs(
    ["Zoning Overlay", "Development by Zone", "Rezone Leads"]
)

# ── Tab 1: Zoning overlay ─────────────────────────────────────────────────────
with tab_overlay:
    st.subheader("Zoning overlay")

    if zoning.empty:
        st.info("No zoning data yet — run `ug ingest zoning`")
    else:
        zone_classes = sorted(zoning["zoning_class"].dropna().unique())
        sel_classes  = st.multiselect("Show classes", zone_classes,
                                      default=zone_classes, key="zone_sel")
        zoning_f = zoning[zoning["zoning_class"].isin(sel_classes)]

        # Encode zoning class as numeric for color mapping
        class_map = {c: i for i, c in enumerate(zone_classes)}
        zoning_f  = zoning_f.copy()
        zoning_f["_zone_num"] = zoning_f["zoning_class"].map(class_map)

        deck = h3_deck(zoning_f, "_zone_num", cmap="tab20",
                       center_lat=lat, center_lon=lon, zoom=zoom,
                       tooltip_extra=["zoning_class"])
        if deck:
            st.pydeck_chart(deck, use_container_width=True)

        st.markdown("**Zone class breakdown**")
        counts = (zoning_f.groupby("zoning_class")
                  .size().reset_index(name="n_cells")
                  .sort_values("n_cells", ascending=False))
        st.dataframe(counts, use_container_width=True, hide_index=True)

# ── Tab 2: Development concentration by zoning class ─────────────────────────
with tab_conc:
    st.subheader("Development concentration by zoning class")

    if zoning.empty or h3_month.empty:
        st.info("Requires zoning data + H3 data for the selected month.")
    else:
        merged = zoning.merge(h3_month[["h3_index", "built_pct"]], on="h3_index", how="inner")
        if merged.empty:
            st.warning("No H3 index overlap between zoning and LC data.")
        else:
            import plotly.express as px
            zone_stats = (
                merged.groupby("zoning_class")["built_pct"]
                .agg(["mean", "median", "count"])
                .reset_index()
                .rename(columns={"mean": "avg_built_pct", "median": "med_built_pct",
                                  "count": "n_cells"})
                .sort_values("avg_built_pct", ascending=False)
            )
            fig = px.bar(zone_stats, x="zoning_class", y="avg_built_pct",
                         title=f"Avg Built % by Zoning Class — {month}",
                         color="avg_built_pct", color_continuous_scale="YlOrRd")
            fig.update_layout(height=350)
            st.plotly_chart(fig, use_container_width=True)

            st.markdown("**Zone statistics**")
            zone_stats["avg_built_pct"] = zone_stats["avg_built_pct"].map("{:.3f}".format)
            zone_stats["med_built_pct"] = zone_stats["med_built_pct"].map("{:.3f}".format)
            st.dataframe(zone_stats, use_container_width=True, hide_index=True)

# ── Tab 3: Rezone leads ───────────────────────────────────────────────────────
with tab_leads:
    st.subheader("Rezone leads — development outside permitted zones")
    st.caption(
        "Cells classified as **commercial / industrial** zoning that are showing "
        "**above-median residential development** patterns. High-growth cells in "
        "low-zoning-intensity classes signal potential rezoning demand."
    )

    if zoning.empty or h3_month.empty:
        st.info("Requires both zoning and H3 data.")
    else:
        merged = zoning.merge(h3_month[["h3_index", "built_pct"]], on="h3_index", how="inner")
        if not merged.empty:
            # Define 'non-residential' zones heuristically
            non_res = [c for c in merged["zoning_class"].unique()
                       if any(kw in c.lower() for kw in
                              ["commercial", "industrial", "agriculture", "open", "agri"])]
            non_res_cells = merged[merged["zoning_class"].isin(non_res)]

            threshold = merged["built_pct"].median()
            leads = non_res_cells[non_res_cells["built_pct"] > threshold].copy()
            leads = leads.sort_values("built_pct", ascending=False)

            st.metric("Potential rezone leads", len(leads),
                      help="Non-residential cells with above-median built_pct")

            if not leads.empty:
                deck = h3_deck(leads, "built_pct", cmap="Reds",
                               center_lat=lat, center_lon=lon, zoom=zoom,
                               tooltip_extra=["zoning_class"])
                if deck:
                    st.pydeck_chart(deck, use_container_width=True)

                st.markdown(f"**Top 30 leads** (sorted by built_pct)")
                st.dataframe(
                    leads[["h3_index", "zoning_class", "built_pct"]].head(30),
                    use_container_width=True, hide_index=True,
                )
            else:
                st.success("No strong rezone signals found for current month.")
