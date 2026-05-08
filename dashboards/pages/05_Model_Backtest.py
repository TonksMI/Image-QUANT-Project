"""Page 5 — Model + Backtest.

Equity curve · current picks · SHAP · factor exposure
"""
from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).parents[2]
_DASH = Path(__file__).parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_DASH))

import numpy as np
import pandas as pd
import streamlit as st

from data import (
    load_backtest, load_model_scores, load_bench_comparison,
    load_factor_exposures, load_sector_decomp, load_universe_df,
    load_returns_coverage,
)
from components.charts import (
    equity_curve, drawdown_chart, factor_bar, shap_bar,
)

st.set_page_config(page_title="Model + Backtest", layout="wide")
st.title("Model + Backtest")

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    model_type = st.selectbox("Model", ["ridge", "elasticnet", "lgbm"])
    horizon    = st.selectbox("Horizon (months)", [1, 2, 3])
    show_gross = st.checkbox("Show gross return (pre-TC)", value=False)

MODEL_COLORS = {"ridge": "#1f77b4", "elasticnet": "#ff7f0e", "lgbm": "#2ca02c"}

# ── Load ─────────────────────────────────────────────────────────────────────
portfolio = load_backtest(model_type, horizon)
scores    = load_model_scores(model_type, horizon)
bench     = load_bench_comparison(model_type, horizon)
ff_df     = load_factor_exposures(model_type, horizon)
sector_df = load_sector_decomp(model_type, horizon)
universe  = load_universe_df()

# ── Status ────────────────────────────────────────────────────────────────────
if portfolio.empty:
    st.info(
        f"No backtest results for **{model_type}** h={horizon}.\n\n"
        f"Run:\n```bash\nug model fit --model {model_type} --horizon {horizon}\n"
        f"ug model backtest --model {model_type} --horizon {horizon}\n```"
    )
    st.stop()

# ── Top metrics ───────────────────────────────────────────────────────────────
from urbangrowth.modeling.backtest import performance_stats
ls_col = "ls_gross" if show_gross else "ls_ret"
ls     = portfolio.set_index("period")[ls_col]
stats  = performance_stats(ls)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("CAGR",          f"{stats.get('cagr', 0):.1%}")
c2.metric("Sharpe",        f"{stats.get('sharpe', 0):.2f}")
c3.metric("Max Drawdown",  f"{stats.get('max_drawdown', 0):.1%}")
c4.metric("Hit Rate",      f"{stats.get('hit_rate', 0):.1%}")
c5.metric("Avg Turnover",  f"{portfolio['turnover'].mean():.1%}")

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_equity, tab_picks, tab_shap, tab_ff, tab_sector = st.tabs(
    ["Equity Curve", "Current Picks", "SHAP", "Factor Exposure", "Sector"]
)

# ── Tab 1: Equity + drawdown ──────────────────────────────────────────────────
with tab_equity:
    # Equity curve — all three models together
    all_portfolios = {}
    for mt in ["ridge", "elasticnet", "lgbm"]:
        p = load_backtest(mt, horizon)
        if not p.empty:
            all_portfolios[mt] = p

    # Benchmark returns (pull from returns table aligned to portfolio period)
    try:
        from urbangrowth.modeling._data import load_monthly_returns
        raw = load_monthly_returns()
        bench_wide = (
            raw[raw["symbol"].isin(["SPY", "XHB", "PAVE"])]
            .pivot(index="date", columns="symbol", values="monthly_ret")
        ) if not raw.empty else pd.DataFrame()
    except Exception:
        bench_wide = pd.DataFrame()

    fig = equity_curve(all_portfolios, bench_wide if not bench_wide.empty else None)
    if fig:
        st.plotly_chart(fig, use_container_width=True)

    # Drawdown
    fig2 = drawdown_chart(ls, model_name=model_type.upper())
    if fig2:
        st.plotly_chart(fig2, use_container_width=True)

    # Benchmark table
    if not bench.empty:
        st.markdown("**vs Benchmarks**")
        for col in ["cagr", "ann_vol", "max_drawdown", "hit_rate"]:
            if col in bench.columns:
                bench[col] = bench[col].apply(
                    lambda x: f"{x:.1%}" if pd.notna(x) and isinstance(x, float) else x
                )
        if "sharpe" in bench.columns:
            bench["sharpe"] = bench["sharpe"].apply(
                lambda x: f"{x:.2f}" if pd.notna(x) and isinstance(x, float) else x
            )
        st.dataframe(bench.set_index("entity") if "entity" in bench.columns else bench,
                     use_container_width=True)

    # Monthly returns table
    with st.expander("Monthly returns"):
        disp = portfolio[["period", "long_ret", "short_ret", "ls_ret", "ls_gross",
                           "tc", "turnover", "n_long", "n_short"]].copy()
        for col in ["long_ret", "short_ret", "ls_ret", "ls_gross", "tc", "turnover"]:
            if col in disp.columns:
                disp[col] = disp[col].apply(lambda x: f"{x:.4f}" if pd.notna(x) else "")
        st.dataframe(disp, use_container_width=True, hide_index=True)


# ── Tab 2: Current picks ──────────────────────────────────────────────────────
with tab_picks:
    if scores.empty:
        st.info("No walk-forward scores — run `ug model fit`")
    else:
        latest_period = scores["period"].max()
        latest_scores = (
            scores[scores["period"] == latest_period]
            .sort_values("score", ascending=False)
            .reset_index(drop=True)
        )
        st.caption(f"Scores as of **{latest_period}**")

        n = len(latest_scores)
        n_long  = max(1, int(n * 0.2))
        n_short = max(1, int(n * 0.2))

        # Enrich with universe info
        if not universe.empty and "symbol" in universe.columns:
            latest_scores = latest_scores.merge(
                universe[["symbol"] + [c for c in ["name", "sector"] if c in universe.columns]],
                on="symbol", how="left",
            )

        col1, col2 = st.columns(2)
        with col1:
            st.markdown(f"### Long ({n_long} stocks)")
            long_df = latest_scores.head(n_long)[
                [c for c in ["symbol", "name", "sector", "score"] if c in latest_scores.columns]
            ]
            st.dataframe(long_df, use_container_width=True, hide_index=True)

        with col2:
            st.markdown(f"### Short ({n_short} stocks)")
            short_df = latest_scores.tail(n_short)[
                [c for c in ["symbol", "name", "sector", "score"] if c in latest_scores.columns]
            ]
            st.dataframe(short_df, use_container_width=True, hide_index=True)

        # Full ranked list
        with st.expander("Full ranked universe"):
            st.dataframe(latest_scores, use_container_width=True, hide_index=True)


# ── Tab 3: SHAP ───────────────────────────────────────────────────────────────
with tab_shap:
    st.caption("Re-fits LightGBM on full in-sample data for feature importance.  "
               "Not the walk-forward model — illustrative only.")

    if st.button("Compute SHAP (may take ~30s)", key="shap_btn"):
        with st.spinner("Fitting LightGBM + computing SHAP..."):
            try:
                import lightgbm as lgb
                import shap as shap_lib
                from urbangrowth.modeling.cross_section import build_model_panel
                from urbangrowth.modeling._data import (
                    build_forward_returns, build_momentum_signal,
                    build_volume_signal, list_signal_sources, load_signal_panel as lsp,
                    load_monthly_returns,
                )
                from sklearn.impute import SimpleImputer

                raw = load_monthly_returns()
                fwd = build_forward_returns(raw, [horizon])
                mom = build_momentum_signal(raw)
                vol = build_volume_signal()
                srcs = list_signal_sources()
                sigs = pd.concat([lsp(s) for s in srcs], ignore_index=True) if srcs else pd.DataFrame()

                if sigs.empty:
                    st.warning("No signal data available.")
                else:
                    panel = build_model_panel(sigs, mom, vol)
                    id_cols = ["period", "symbol"]
                    feat_cols = [c for c in panel.columns if c not in id_cols]

                    fwd_h = (fwd[fwd["horizon"] == horizon][["symbol", "date", "fwd_return"]]
                             .rename(columns={"date": "period"}))
                    data = panel.merge(fwd_h, on=["symbol", "period"], how="inner")

                    X = data[feat_cols].values.astype(float)
                    y = data["fwd_return"].values.astype(float)
                    valid = np.isfinite(y)
                    X, y = X[valid], y[valid]

                    imp = SimpleImputer(strategy="constant", fill_value=0.0)
                    X = imp.fit_transform(X)

                    mdl = lgb.LGBMRegressor(
                        n_estimators=200, num_leaves=15, learning_rate=0.05,
                        subsample=0.8, colsample_bytree=0.8,
                        random_state=42, verbose=-1,
                    )
                    mdl.fit(X, y)

                    explainer = shap_lib.TreeExplainer(mdl)
                    rng = np.random.default_rng(42)
                    idx = rng.choice(len(X), size=min(2000, len(X)), replace=False)
                    sv  = explainer.shap_values(X[idx])

                    imp_df = pd.DataFrame({
                        "feature":        feat_cols,
                        "mean_abs_shap":  np.abs(sv).mean(axis=0),
                    }).sort_values("mean_abs_shap", ascending=False)

                    st.session_state["shap_importance"] = imp_df

            except ImportError as e:
                st.error(f"Install lightgbm and shap: `pip install lightgbm shap`\n{e}")
            except Exception as e:
                st.error(f"SHAP computation failed: {e}")

    if "shap_importance" in st.session_state:
        imp_df = st.session_state["shap_importance"]
        fig = shap_bar(imp_df)
        if fig:
            st.plotly_chart(fig, use_container_width=True)
        st.dataframe(imp_df.head(30), use_container_width=True, hide_index=True)
    else:
        st.info("Click the button above to compute SHAP values.")


# ── Tab 4: Factor exposure ────────────────────────────────────────────────────
with tab_ff:
    if ff_df.empty:
        st.info(
            "No factor exposure data — either run the backtest (which fetches FF data "
            "automatically) or check internet connectivity for pandas_datareader."
        )
    else:
        fig = factor_bar(ff_df)
        if fig:
            st.plotly_chart(fig, use_container_width=True)

        alpha_row = ff_df[ff_df["factor"] == "const"]
        if not alpha_row.empty:
            alpha_ann = float(alpha_row["beta"].iloc[0]) * 12
            alpha_t   = float(alpha_row["tstat"].iloc[0])
            color = "green" if alpha_t > 2 else ("red" if alpha_t < -2 else "orange")
            st.markdown(
                f"**Annualised alpha: {alpha_ann:.2%}** "
                f"(t = {alpha_t:.2f}) "
                + (":green[significant]" if abs(alpha_t) > 2 else ":orange[not significant]")
            )

        disp = ff_df.copy()
        disp["beta"]   = disp["beta"].map("{:.4f}".format)
        disp["tstat"]  = disp["tstat"].map("{:.2f}".format)
        disp["pvalue"] = disp["pvalue"].map("{:.3f}".format)
        st.dataframe(disp, use_container_width=True, hide_index=True)


# ── Tab 5: Sector decomposition ───────────────────────────────────────────────
with tab_sector:
    if sector_df.empty:
        st.info("No sector decomposition data — run `ug model backtest`")
    else:
        import plotly.express as px
        c1, c2 = st.columns(2)
        for col, side in zip([c1, c2], ["long", "short"]):
            with col:
                sub = sector_df[sector_df["side"] == side].sort_values(
                    "avg_contribution", ascending=True
                )
                colors = ["#2ca02c" if v > 0 else "#d62728"
                          for v in sub["avg_contribution"]]
                fig = px.bar(sub, x="avg_contribution", y="sector",
                             orientation="h",
                             title=f"{side.capitalize()} leg — avg monthly contribution",
                             color_discrete_sequence=[None])
                fig.update_traces(marker_color=colors)
                fig.add_vline(x=0, line_dash="dash", line_color="grey")
                fig.update_layout(height=350, showlegend=False)
                st.plotly_chart(fig, use_container_width=True)

        st.dataframe(sector_df, use_container_width=True, hide_index=True)
