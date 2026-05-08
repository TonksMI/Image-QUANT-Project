"""Page 1 — National Alt Data.

Census BPS county choropleth · FERC queue by ISO · USASpending by sector · FRED series
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

from data import (
    load_census_bps, load_census_bps_national,
    load_ferc_queue, load_usaspending, load_fred,
)
from components.maps import state_choropleth, county_choropleth
from components.charts import line_chart, stacked_area, bar_chart

st.set_page_config(page_title="National Alt Data", layout="wide")
st.title("National Alt Data")

_EMPTY = st.empty()

# ── Tabs ─────────────────────────────────────────────────────────────────────
tab_bps, tab_ferc, tab_usa, tab_fred = st.tabs(
    ["Census BPS", "FERC Queue", "USASpending", "FRED Macro"]
)

# ─────────────────────────────────────────────────────────────────────────────
# 1. Census BPS
# ─────────────────────────────────────────────────────────────────────────────
with tab_bps:
    st.subheader("Census Building Permits Survey")

    bps     = load_census_bps()
    bps_nat = load_census_bps_national()

    if bps.empty:
        st.info("No Census BPS data yet — run `ug ingest census-bps`")
    else:
        col_ctrl, _ = st.columns([2, 3])
        with col_ctrl:
            metric  = st.selectbox("Metric", ["res_units", "com_units", "res_value", "com_value"],
                                   key="bps_metric")
            periods = sorted(bps["period"].dt.to_period("M").astype(str).unique())
            if periods:
                sel_period = st.select_slider("Month", options=periods, value=periods[-1],
                                              key="bps_period")
                bps_m = bps[bps["period"].dt.to_period("M").astype(str) == sel_period]
            else:
                bps_m = bps.copy()

        c1, c2 = st.columns([3, 2])
        with c1:
            # State-level choropleth (aggregate by state)
            state_agg = bps_m.groupby("state")[metric].sum().reset_index()
            fig = state_choropleth(state_agg, "state", metric,
                                   title=f"{metric} by State — {sel_period}")
            if fig:
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.dataframe(state_agg, use_container_width=True)

        with c2:
            st.markdown("**Top 15 states**")
            top = state_agg.nlargest(15, metric)[["state", metric]]
            st.dataframe(top, use_container_width=True, hide_index=True)

        # National trend
        st.markdown("**National trend**")
        if not bps_nat.empty:
            fig2 = line_chart(bps_nat, "period", ["res_units", "com_units"],
                              title="National BPS — Residential vs Commercial Units")
            if fig2:
                st.plotly_chart(fig2, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# 2. FERC Queue
# ─────────────────────────────────────────────────────────────────────────────
with tab_ferc:
    st.subheader("FERC Interconnection Queue")

    ferc = load_ferc_queue()

    if ferc.empty:
        st.info("No FERC data yet — run `ug ingest ferc-queue`")
    else:
        col1, col2 = st.columns([1, 2])
        with col1:
            status_filter = st.multiselect(
                "Status filter",
                options=sorted(ferc["status"].dropna().unique()),
                default=sorted(ferc["status"].dropna().unique()),
                key="ferc_status",
            )
        ferc_f = ferc[ferc["status"].isin(status_filter)] if status_filter else ferc

        # Monthly MW additions by ISO
        ferc_f["month"] = ferc_f["queue_date"].dt.to_period("M").dt.to_timestamp()
        monthly_iso = (
            ferc_f.groupby(["month", "iso_region"])["mw_capacity"]
            .sum()
            .reset_index()
        )
        fig = stacked_area(monthly_iso, "month", "mw_capacity", "iso_region",
                           title="Queue MW Added per Month by ISO Region")
        if fig:
            st.plotly_chart(fig, use_container_width=True)

        # Fuel mix
        fuel_mix = ferc_f.groupby("fuel_type")["mw_capacity"].sum().reset_index()
        fig2 = bar_chart(fuel_mix.sort_values("mw_capacity", ascending=False),
                         "fuel_type", "mw_capacity", title="Queue MW by Fuel Type")
        if fig2:
            st.plotly_chart(fig2, use_container_width=True)

        st.markdown(f"**{len(ferc_f):,} queue entries** (filtered)")


# ─────────────────────────────────────────────────────────────────────────────
# 3. USASpending
# ─────────────────────────────────────────────────────────────────────────────
with tab_usa:
    st.subheader("Federal Contract Awards (USASpending)")

    usa = load_usaspending()

    if usa.empty:
        st.info("No USASpending data yet — run `ug ingest usaspending`")
    else:
        usa["month"] = usa["award_date"].dt.to_period("M").dt.to_timestamp()
        top_states = (
            usa.groupby("place_of_performance_state")["total_obligated_amount"]
            .sum().nlargest(15).reset_index()
            .rename(columns={"total_obligated_amount": "total_awards"})
        )
        monthly = (
            usa.groupby("month")["total_obligated_amount"]
            .sum().reset_index()
        )

        c1, c2 = st.columns(2)
        with c1:
            fig = bar_chart(top_states, "place_of_performance_state", "total_awards",
                            title="Awards by State (top 15)", orientation="h")
            if fig:
                st.plotly_chart(fig, use_container_width=True)
        with c2:
            fig2 = line_chart(monthly, "month", "total_obligated_amount",
                              title="Monthly Award Obligations ($)")
            if fig2:
                st.plotly_chart(fig2, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# 4. FRED Macro
# ─────────────────────────────────────────────────────────────────────────────
with tab_fred:
    st.subheader("FRED Construction / Macro Series")

    fred = load_fred()

    if fred.empty:
        st.info("No FRED data yet — run `ug ingest fred`")
    else:
        series_list = sorted(fred["series_id"].unique())
        selected = st.multiselect("Series", series_list,
                                  default=series_list[:4],
                                  key="fred_series")
        if selected:
            sub = fred[fred["series_id"].isin(selected)]
            for sid in selected:
                s = sub[sub["series_id"] == sid][["date", "value"]].dropna()
                if s.empty:
                    continue
                fig = line_chart(s, "date", "value", title=sid)
                if fig:
                    st.plotly_chart(fig, use_container_width=True)
