"""Plotly chart helpers used across dashboard pages."""
from __future__ import annotations

import numpy as np
import pandas as pd


def _fig_or_none(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Time series
# ---------------------------------------------------------------------------

def line_chart(df: pd.DataFrame, x: str, y: str | list[str],
               color: str | None = None, title: str = "",
               yaxis_fmt: str = "") -> object | None:
    try:
        import plotly.express as px
        cols = [y] if isinstance(y, str) else y
        if isinstance(y, list):
            df = df[[x] + cols].melt(id_vars=x, var_name="_var", value_name="_val")
            fig = px.line(df, x=x, y="_val", color="_var", title=title)
        else:
            kw = {"color": color} if color else {}
            fig = px.line(df, x=x, y=y, title=title, **kw)
        if yaxis_fmt == "pct":
            fig.update_yaxes(tickformat=".1%")
        fig.update_layout(height=350, margin=dict(t=40, b=20))
        return fig
    except Exception:
        return None


def stacked_area(df: pd.DataFrame, x: str, y: str,
                 color: str, title: str = "") -> object | None:
    try:
        import plotly.express as px
        fig = px.area(df, x=x, y=y, color=color, title=title)
        fig.update_layout(height=350, margin=dict(t=40, b=20))
        return fig
    except Exception:
        return None


def bar_chart(df: pd.DataFrame, x: str, y: str,
              color: str | None = None, title: str = "",
              orientation: str = "v") -> object | None:
    try:
        import plotly.express as px
        kw = {"color": color} if color else {}
        fig = px.bar(df, x=x, y=y, title=title,
                     orientation=orientation, **kw)
        fig.update_layout(height=350, margin=dict(t=40, b=20))
        return fig
    except Exception:
        return None


# ---------------------------------------------------------------------------
# IC / signal charts
# ---------------------------------------------------------------------------

def ic_heatmap(df: pd.DataFrame) -> object | None:
    """IC mean heatmap: features × horizons."""
    try:
        import plotly.express as px
        if df.empty or "feature_name" not in df.columns:
            return None
        pivot = df.pivot_table(index="feature_name", columns="horizon",
                               values="ic_mean", aggfunc="mean")
        fig = px.imshow(pivot, color_continuous_scale="RdBu_r",
                        color_continuous_midpoint=0,
                        title="IC Mean — Feature × Horizon",
                        aspect="auto")
        fig.update_layout(height=max(300, len(pivot) * 18), margin=dict(t=40, b=20))
        return fig
    except Exception:
        return None


def decay_curve(decay_df: pd.DataFrame, feature_name: str) -> object | None:
    try:
        import plotly.graph_objects as go
        sub = decay_df[decay_df["feature_name"] == feature_name].sort_values("horizon")
        if sub.empty:
            return None
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=sub["horizon"], y=sub["ic_mean"],
            mode="lines+markers", name="IC Mean", line_color="steelblue",
        ))
        if "ci_lo" in sub.columns and "ci_hi" in sub.columns:
            fig.add_trace(go.Scatter(
                x=pd.concat([sub["horizon"], sub["horizon"][::-1]]),
                y=pd.concat([sub["ci_hi"], sub["ci_lo"][::-1]]),
                fill="toself", fillcolor="rgba(70,130,180,0.2)",
                line=dict(color="rgba(255,255,255,0)"),
                name="95% CI",
            ))
        fig.add_hline(y=0, line_dash="dash", line_color="grey")
        fig.update_layout(title=f"IC Decay — {feature_name}",
                          xaxis_title="Horizon (months)", yaxis_title="IC Mean",
                          height=320, margin=dict(t=40, b=20))
        return fig
    except Exception:
        return None


def tstat_bar(df: pd.DataFrame, title: str = "NW t-statistic by Feature") -> object | None:
    try:
        import plotly.express as px
        if df.empty or "nw_tstat" not in df.columns:
            return None
        df = df.sort_values("nw_tstat", ascending=True).copy()
        df["color"] = df["nw_tstat"].apply(
            lambda t: "significant" if abs(t) >= 2 else "not significant"
        )
        fig = px.bar(df, x="nw_tstat", y="feature_name", orientation="h",
                     color="color",
                     color_discrete_map={"significant": "#2ca02c", "not significant": "#aec7e8"},
                     title=title)
        fig.add_vline(x=2,  line_dash="dash", line_color="green", opacity=0.5)
        fig.add_vline(x=-2, line_dash="dash", line_color="green", opacity=0.5)
        fig.update_layout(height=max(300, len(df) * 20), margin=dict(t=40, b=20),
                          showlegend=False)
        return fig
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Backtest / model charts
# ---------------------------------------------------------------------------

def equity_curve(portfolio_dfs: dict[str, pd.DataFrame],
                 bench_df: pd.DataFrame | None = None) -> object | None:
    try:
        import plotly.graph_objects as go
        colors = {"ridge": "#1f77b4", "elasticnet": "#ff7f0e", "lgbm": "#2ca02c"}
        bench_colors = {"SPY": "#7f7f7f", "XHB": "#d62728", "PAVE": "#9467bd"}
        fig = go.Figure()
        for mt, port in portfolio_dfs.items():
            if port.empty:
                continue
            port = port.sort_values("period")
            cum = (1 + port.set_index("period")["ls_ret"]).cumprod()
            fig.add_trace(go.Scatter(x=cum.index, y=cum.values,
                                     name=mt.upper(), line_color=colors.get(mt),
                                     line=dict(width=2)))
        if bench_df is not None and not bench_df.empty:
            for sym in bench_df.columns:
                s = bench_df[sym].dropna()
                cum = (1 + s).cumprod()
                fig.add_trace(go.Scatter(x=cum.index, y=cum.values,
                                         name=sym, line_color=bench_colors.get(sym, "grey"),
                                         line=dict(width=1.2, dash="dash"),
                                         opacity=0.8))
        fig.add_hline(y=1.0, line_dash="dot", line_color="black", opacity=0.4)
        fig.update_layout(title="Cumulative Return — L/S Portfolio vs Benchmarks",
                          yaxis_title="Growth of $1", height=400,
                          margin=dict(t=40, b=20), legend=dict(orientation="h"))
        return fig
    except Exception:
        return None


def drawdown_chart(ls_series: pd.Series, model_name: str = "") -> object | None:
    try:
        import plotly.graph_objects as go
        cum = (1 + ls_series.dropna()).cumprod()
        dd  = (cum / cum.cummax() - 1) * 100
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=dd.index, y=dd.values,
                                  fill="tozeroy", fillcolor="rgba(214,39,40,0.3)",
                                  line_color="#d62728", name="Drawdown"))
        max_dd = dd.min()
        fig.update_layout(title=f"{model_name} Drawdown",
                          yaxis_title="Drawdown (%)", height=280,
                          margin=dict(t=40, b=20))
        fig.add_annotation(text=f"MaxDD {max_dd:.1f}%", x=0.02, y=0.1,
                           xref="paper", yref="paper", showarrow=False,
                           font=dict(color="darkred"))
        return fig
    except Exception:
        return None


def shap_bar(importance_df: pd.DataFrame, top_n: int = 20) -> object | None:
    try:
        import plotly.express as px
        if importance_df.empty:
            return None
        df = importance_df.nlargest(top_n, "mean_abs_shap").sort_values("mean_abs_shap")
        fig = px.bar(df, x="mean_abs_shap", y="feature", orientation="h",
                     title=f"Top {top_n} Features — Mean |SHAP|",
                     color_discrete_sequence=["steelblue"])
        fig.update_layout(height=max(300, top_n * 20), margin=dict(t=40, b=20))
        return fig
    except Exception:
        return None


def factor_bar(ff_df: pd.DataFrame) -> object | None:
    try:
        import plotly.express as px
        if ff_df.empty:
            return None
        ff_df = ff_df[ff_df["factor"] != "const"].copy()
        ff_df["sig"] = ff_df["pvalue"] < 0.05
        fig = px.bar(ff_df, x="factor", y="beta",
                     color="sig",
                     color_discrete_map={True: "#2ca02c", False: "#aec7e8"},
                     title="Factor Betas (green = p < 0.05)",
                     error_y=None)
        fig.add_hline(y=0, line_dash="dash", line_color="grey")
        fig.update_layout(height=320, margin=dict(t=40, b=20), showlegend=False)
        return fig
    except Exception:
        return None
