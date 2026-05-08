"""Page 2 — City Explorer.

pydeck H3HexagonLayer · time scrubber · hex drill-down
"""
from __future__ import annotations

from pathlib import Path
import sys
import time

_ROOT = Path(__file__).parents[2]
_DASH = Path(__file__).parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_DASH))

import pandas as pd
import streamlit as st

from data import h3_available_months, load_h3_month, load_h3_full_panel
from components.maps import h3_deck, h3_deck_dual

st.set_page_config(page_title="City Explorer", layout="wide")
st.title("City Explorer")

# ── Sidebar controls ──────────────────────────────────────────────────────────
with st.sidebar:
    city    = st.selectbox("City", ["phoenix", "austin"])
    metric  = st.selectbox("Metric", ["built_pct", "veg_to_built_pct",
                                       "built_delta", "cai"],
                            key="city_metric")
    CITY_CENTERS = {
        "phoenix": (33.45, -112.07, 9),
        "austin":  (30.27, -97.74,  9),
    }
    lat, lon, zoom = CITY_CENTERS.get(city, (33.45, -112.07, 9))

# ── Load months ───────────────────────────────────────────────────────────────
months = h3_available_months(city)

if not months:
    st.info(f"No H3 data for **{city}** yet.\n\nRun the pipeline:\n"
            f"```\nug process h3 --city {city}\nug process change --city {city}\n"
            f"ug features build --city {city}\n```")
    st.stop()

# ── Time scrubber ─────────────────────────────────────────────────────────────
col_slider, col_play = st.columns([8, 1])
with col_slider:
    idx = st.slider("Month", 0, len(months) - 1, len(months) - 1,
                    format="", key="city_month_idx",
                    help="Drag to scrub through time")
with col_play:
    autoplay = st.button("▶", help="Animate through months")

selected_month = months[idx]
st.caption(f"Showing: **{selected_month}**  |  {len(months)} months available")

# ── Animate ───────────────────────────────────────────────────────────────────
map_placeholder  = st.empty()
info_placeholder = st.empty()

def render_month(month: str):
    df = load_h3_month(city, month)
    if df.empty or metric not in df.columns:
        # Try computing built_delta on the fly if not present
        if metric == "built_delta" and "built_pct" in df.columns:
            # Need previous month for delta — fall back to built_pct display
            df[metric] = df["built_pct"]

    if df.empty:
        map_placeholder.warning(f"No data for {month}")
        return df

    deck = h3_deck(df, metric, center_lat=lat, center_lon=lon, zoom=zoom,
                   tooltip_extra=[c for c in ["built_pct", "veg_to_built_pct"]
                                  if c in df.columns and c != metric])
    if deck:
        map_placeholder.pydeck_chart(deck, use_container_width=True)
    else:
        map_placeholder.info("pydeck not installed — `pip install pydeck`")

    return df

if autoplay:
    for i, m in enumerate(months):
        render_month(m)
        st.session_state["city_month_idx"] = i
        time.sleep(0.35)
    st.rerun()
else:
    df = render_month(selected_month)

# ── Stats strip ───────────────────────────────────────────────────────────────
if not df.empty and metric in df.columns:
    vals = df[metric].dropna()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cells", f"{len(df):,}")
    c2.metric(f"Median {metric}", f"{vals.median():.4f}")
    c3.metric(f"p90 {metric}",    f"{vals.quantile(0.9):.4f}")
    c4.metric(f"Max {metric}",    f"{vals.max():.4f}")

st.divider()

# ── Drill-down ────────────────────────────────────────────────────────────────
st.markdown("### Hex drill-down")
st.caption("Enter an H3 index (from tooltip) to see its full history.")

h3_input = st.text_input("H3 index", key="drilldown_h3",
                          placeholder="8c2a100d2d8ffff")

if h3_input and h3_input.strip():
    h3_id = h3_input.strip()
    with st.spinner("Loading full panel..."):
        full = load_h3_full_panel(city)

    cell = full[full["h3_index"] == h3_id] if not full.empty else pd.DataFrame()

    if cell.empty:
        st.warning(f"H3 index `{h3_id}` not found in {city} panel.")
    else:
        cell = cell.sort_values("period")
        st.markdown(f"**{h3_id}** — {len(cell)} months of data")

        import plotly.express as px
        numeric_cols = [c for c in cell.select_dtypes(include="number").columns
                        if c != "h3_index"]
        if numeric_cols:
            sel_col = st.selectbox("Metric to plot", numeric_cols, key="drilldown_col")
            fig = px.line(cell, x="period", y=sel_col,
                          title=f"{sel_col} over time — {h3_id[:12]}…",
                          markers=True)
            fig.update_layout(height=300)
            st.plotly_chart(fig, use_container_width=True)

        st.dataframe(cell.set_index("period"), use_container_width=True)

# ── Side-by-side comparison ───────────────────────────────────────────────────
st.divider()
st.markdown("### Side-by-side comparison")
cmp_cols = st.columns(2)
cmp_metrics = []
for i, col in enumerate(cmp_cols):
    with col:
        m = st.selectbox("Metric", ["built_pct", "veg_to_built_pct"],
                         key=f"cmp_{i}")
        cmp_metrics.append(m)

if len(set(cmp_metrics)) == 2:
    df_cmp = load_h3_month(city, selected_month)
    left, right = h3_deck_dual(df_cmp, cmp_metrics[0], cmp_metrics[1],
                                center_lat=lat, center_lon=lon, zoom=zoom)
    for col, deck in zip(cmp_cols, [left, right]):
        with col:
            if deck:
                st.pydeck_chart(deck, use_container_width=True)
            else:
                st.info("Deck unavailable")
