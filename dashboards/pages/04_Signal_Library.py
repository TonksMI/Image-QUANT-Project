"""Page 4 — Signal Library.

Per-signal: time series · applicable tickers · IC table · decay curve
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
    load_signal_sources, load_signal_panel,
    load_ic_results, load_decay_results,
)
from components.charts import line_chart, ic_heatmap, decay_curve, tstat_bar

st.set_page_config(page_title="Signal Library", layout="wide")
st.title("Signal Library")

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    sources = load_signal_sources()
    if not sources:
        st.warning("No signal sources found in DB.")
        st.stop()

    source = st.selectbox("Signal source", sources)
    horizon_sel = st.selectbox("IC horizon", [1, 2, 3], key="sig_horizon")

# ── Load data ─────────────────────────────────────────────────────────────────
panel   = load_signal_panel(source)
ic_df   = load_ic_results()
dec_df  = load_decay_results()

# Filter to selected source
ic_src  = ic_df[ic_df["source"] == source]  if not ic_df.empty  and "source" in ic_df.columns  else ic_df
dec_src = dec_df[dec_df["source"] == source] if not dec_df.empty and "source" in dec_df.columns else dec_df

# ── Overview metrics ─────────────────────────────────────────────────────────
features = sorted(panel["feature_name"].unique()) if not panel.empty else []
tickers  = sorted(panel["symbol"].unique())       if not panel.empty else []

c1, c2, c3 = st.columns(3)
c1.metric("Features", len(features))
c2.metric("Tickers",  len(tickers))
if not ic_src.empty and "fdr_rejected" in ic_src.columns:
    n_sig = int(ic_src[ic_src["horizon"] == horizon_sel]["fdr_rejected"].sum())
    c3.metric(f"Significant (FDR, h={horizon_sel})", n_sig)

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_ts, tab_ic, tab_decay = st.tabs(
    ["Signal Time Series", "IC Table", "Decay Curves"]
)

# ── Tab 1: Time series ────────────────────────────────────────────────────────
with tab_ts:
    if panel.empty:
        st.info(f"No signal data for source **{source}** — run `ug signals build`")
    else:
        col1, col2 = st.columns([1, 2])
        with col1:
            sel_feat   = st.selectbox("Feature", features, key="sig_feat")
            sel_ticker = st.selectbox("Ticker",  ["ALL"] + tickers, key="sig_ticker")

        sub = panel[panel["feature_name"] == sel_feat].copy()
        if sel_ticker != "ALL":
            sub = sub[sub["symbol"] == sel_ticker]

        if sub.empty:
            st.warning("No data for this selection.")
        else:
            with col2:
                if sel_ticker == "ALL":
                    # Cross-section: show distribution over time
                    cs = (sub.groupby("period")["feature_value"]
                          .agg(["mean", "median",
                                lambda x: x.quantile(0.1),
                                lambda x: x.quantile(0.9)])
                          .reset_index())
                    cs.columns = ["period", "mean", "median", "p10", "p90"]
                    import plotly.graph_objects as go
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=cs["period"], y=cs["mean"],
                                             name="mean", line_color="steelblue"))
                    fig.add_trace(go.Scatter(x=cs["period"], y=cs["median"],
                                             name="median", line_color="orange",
                                             line=dict(dash="dash")))
                    fig.add_trace(go.Scatter(
                        x=pd.concat([cs["period"], cs["period"][::-1]]),
                        y=pd.concat([cs["p90"], cs["p10"][::-1]]),
                        fill="toself", fillcolor="rgba(70,130,180,0.15)",
                        line=dict(color="rgba(255,255,255,0)"),
                        name="p10–p90",
                    ))
                    fig.update_layout(title=f"{sel_feat} — cross-section distribution",
                                      height=350)
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    ts = sub.sort_values("period")[["period", "feature_value"]]
                    fig = line_chart(ts, "period", "feature_value",
                                     title=f"{sel_feat} — {sel_ticker}")
                    if fig:
                        st.plotly_chart(fig, use_container_width=True)

        st.markdown("**Applicable tickers**")
        st.write(", ".join(tickers) if tickers else "none")


# ── Tab 2: IC table ───────────────────────────────────────────────────────────
with tab_ic:
    if ic_src.empty:
        st.info("IC results not found — run `ug model ic-test --signal all`")
    else:
        st.markdown("**IC heatmap — all features × horizons**")
        fig = ic_heatmap(ic_src)
        if fig:
            st.plotly_chart(fig, use_container_width=True)

        h_df = ic_src[ic_src["horizon"] == horizon_sel].copy()
        if not h_df.empty:
            st.markdown(f"**h={horizon_sel} — t-statistic by feature**")
            fig2 = tstat_bar(h_df, title=f"NW t-stat | {source} | h={horizon_sel}")
            if fig2:
                st.plotly_chart(fig2, use_container_width=True)

        st.markdown("**Full IC table**")
        display_cols = [c for c in ["feature_name", "horizon", "ic_mean", "ic_std",
                                     "nw_tstat", "p_adjusted", "fdr_rejected", "n_months"]
                        if c in ic_src.columns]
        disp = ic_src[display_cols].copy()
        for col in ["ic_mean", "ic_std", "nw_tstat", "p_adjusted"]:
            if col in disp.columns:
                disp[col] = disp[col].map("{:.4f}".format)
        st.dataframe(disp.sort_values(["horizon", "nw_tstat"],
                                       ascending=[True, False]),
                     use_container_width=True, hide_index=True)


# ── Tab 3: Decay curves ───────────────────────────────────────────────────────
with tab_decay:
    if dec_src.empty:
        st.info("Decay results not found — run `ug model decay --signal all`")
    else:
        dec_features = sorted(dec_src["feature_name"].unique())
        if not dec_features:
            st.warning("No features in decay data for this source.")
        else:
            sel_decay = st.selectbox("Feature", dec_features, key="decay_feat")
            fig = decay_curve(dec_src, sel_decay)
            if fig:
                st.plotly_chart(fig, use_container_width=True)

            # Peak lag summary
            st.markdown("**Peak lag per feature**")
            peaks = (dec_src.loc[dec_src.groupby("feature_name")["ic_mean"].idxmax()]
                     [["feature_name", "horizon", "ic_mean", "nw_tstat"]]
                     .sort_values("ic_mean", ascending=False))
            for col in ["ic_mean", "nw_tstat"]:
                if col in peaks.columns:
                    peaks[col] = peaks[col].map("{:.4f}".format)
            st.dataframe(peaks, use_container_width=True, hide_index=True)
