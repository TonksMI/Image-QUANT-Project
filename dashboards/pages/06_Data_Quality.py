"""Page 6 — Data Quality.

Coverage % per month · segmentation confidence · missing data flags by source
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
    load_coverage_report, load_returns_coverage,
    load_h3_coverage, h3_available_months,
)

st.set_page_config(page_title="Data Quality", layout="wide")
st.title("Data Quality")

with st.sidebar:
    city = st.selectbox("City (H3 coverage)", ["phoenix", "austin"])

# ── Signal coverage ───────────────────────────────────────────────────────────
st.subheader("Signal coverage — tickers × features per month")

cov = load_coverage_report()

if cov.empty:
    st.info("No signal_features data yet — run `ug signals build`")
else:
    sources = sorted(cov["source"].unique())
    sel_src = st.multiselect("Sources", sources, default=sources)
    cov_f   = cov[cov["source"].isin(sel_src)]

    import plotly.express as px

    # Tickers per month by source
    fig = px.line(cov_f, x="period", y="n_tickers", color="source",
                  title="Tickers with signals per month by source")
    fig.update_layout(height=320)
    st.plotly_chart(fig, use_container_width=True)

    # Features per month
    fig2 = px.line(cov_f, x="period", y="n_features", color="source",
                   title="Distinct features per month by source")
    fig2.update_layout(height=280)
    st.plotly_chart(fig2, use_container_width=True)

    # Heatmap: source × month coverage
    try:
        pivot = cov_f.pivot_table(
            index="source", columns=cov_f["period"].dt.strftime("%Y-%m"),
            values="n_tickers", aggfunc="mean"
        )
        fig3 = px.imshow(pivot, color_continuous_scale="Blues",
                         title="Avg tickers covered — source × month",
                         aspect="auto")
        fig3.update_layout(height=max(200, len(pivot) * 30))
        st.plotly_chart(fig3, use_container_width=True)
    except Exception:
        pass

    with st.expander("Raw coverage table"):
        st.dataframe(cov_f, use_container_width=True, hide_index=True)

st.divider()

# ── Returns coverage ──────────────────────────────────────────────────────────
st.subheader("Returns coverage — tickers in returns table")

ret_cov = load_returns_coverage()

if ret_cov.empty:
    st.info("No returns data yet — run `ug ingest markets`")
else:
    fig = px.bar(ret_cov, x="period", y="n_tickers",
                 title="Tickers with monthly return data",
                 color_discrete_sequence=["steelblue"])
    fig.update_layout(height=280)
    st.plotly_chart(fig, use_container_width=True)
    c1, c2 = st.columns(2)
    c1.metric("Total months", len(ret_cov))
    c2.metric("Avg tickers/month", f"{ret_cov['n_tickers'].mean():.1f}")

st.divider()

# ── H3 coverage ───────────────────────────────────────────────────────────────
st.subheader(f"H3 coverage — {city.title()}")

months = h3_available_months(city)
if not months:
    st.info(f"No H3 data for {city} — run `ug process h3 --city {city}`")
else:
    h3_cov = load_h3_coverage(city)
    if not h3_cov.empty:
        c1, c2, c3 = st.columns(3)
        c1.metric("Months available", len(months))
        c2.metric("Avg cells/month", f"{h3_cov['n_cells'].mean():,.0f}")
        c3.metric("Avg coverage",    f"{h3_cov['pct_built_valid'].mean():.1%}",
                  help="Fraction of cells with non-null built_pct")

        fig = px.bar(h3_cov, x="period", y="n_cells",
                     title=f"H3 cells per month — {city.title()}",
                     color_discrete_sequence=["#2ca02c"])
        fig.update_layout(height=260)
        st.plotly_chart(fig, use_container_width=True)

        fig2 = px.line(h3_cov, x="period", y="pct_built_valid",
                       title="Fraction of cells with valid built_pct")
        fig2.update_yaxes(tickformat=".1%")
        fig2.update_layout(height=220)
        st.plotly_chart(fig2, use_container_width=True)

st.divider()

# ── Missing-data flag table ───────────────────────────────────────────────────
st.subheader("Missing-data flags")

flags = []

# Check each expected source
expected_sources = ["permit", "ferc", "usaspending", "fred", "dot_tips", "city_growth"]
actual_sources   = sorted(cov["source"].unique()) if not cov.empty else []
for src in expected_sources:
    flags.append({
        "source":  src,
        "status":  "✅ present" if src in actual_sources else "❌ missing",
        "n_months": int((cov[cov["source"] == src]["period"].nunique())
                        if (not cov.empty and src in actual_sources) else 0),
    })

# Returns
flags.append({
    "source":  "returns",
    "status":  "✅ present" if not ret_cov.empty else "❌ missing",
    "n_months": len(ret_cov),
})

# H3
flags.append({
    "source":  f"h3_{city}",
    "status":  f"✅ {len(months)} months" if months else "❌ missing",
    "n_months": len(months),
})

st.dataframe(pd.DataFrame(flags), use_container_width=True, hide_index=True)

# ── Data freshness ────────────────────────────────────────────────────────────
if not cov.empty:
    st.markdown("**Most recent data by source**")
    freshness = (cov.groupby("source")["period"].max()
                 .reset_index()
                 .rename(columns={"period": "latest_period"})
                 .sort_values("latest_period", ascending=False))
    freshness["latest_period"] = freshness["latest_period"].dt.strftime("%Y-%m")
    st.dataframe(freshness, use_container_width=True, hide_index=True)
