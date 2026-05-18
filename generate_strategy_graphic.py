"""
Strategy comparison graphic: Ridge H=2 L/S vs SPY Buy-Hold.
A single polished 4-panel figure designed for presentation use.
"""
import os, warnings
warnings.filterwarnings("ignore")
os.chdir("C:/Users/17ton/urbangrowth")

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.ticker as mticker
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D
from pathlib import Path

from urbangrowth.config import data_path, get_pipeline
from urbangrowth.modeling.backtest import (
    construct_portfolio, performance_stats, _portfolio_stats,
    load_monthly_returns
)
from urbangrowth.modeling._data import build_forward_returns

out_dir = data_path(get_pipeline()["processed_data_subdirs"].get("signal_tables", "processed/signals"))
asset_dir = Path("D:/urbangrowth_data/processed/maps/report_assets")

# ── Load data ─────────────────────────────────────────────────────────────────
raw_returns = load_monthly_returns()

scores2 = pd.read_parquet(out_dir / "model_scores_ridge_h2.parquet")
fwd2    = build_forward_returns(raw_returns, [2])
port2   = construct_portfolio(scores2, fwd2, horizon=2, tc_bps=10.0)

spy_ser = (
    raw_returns[raw_returns["symbol"] == "SPY"]
    .set_index("date")["monthly_ret"].dropna().sort_index()
)

# Align to portfolio period
ls_ser  = port2.set_index("period")["ls_ret"].sort_index()
long_ser = port2.set_index("period")["long_ret"].sort_index()
start = ls_ser.index.min()
end   = ls_ser.index.max()
spy_aligned = spy_ser[(spy_ser.index >= start) & (spy_ser.index <= end)]

# ── Derived series ─────────────────────────────────────────────────────────────
cum_ls   = (1 + ls_ser).cumprod()
cum_spy  = (1 + spy_aligned).cumprod()
cum_long = (1 + long_ser).cumprod()

# Rolling 12-month Sharpe
def rolling_sharpe(s, window=12):
    roll_ret = s.rolling(window).mean() * 12
    roll_vol = s.rolling(window).std() * np.sqrt(12)
    return (roll_ret / roll_vol).replace([np.inf, -np.inf], np.nan)

rs_ls  = rolling_sharpe(ls_ser)
rs_spy = rolling_sharpe(spy_aligned)

# Monthly return distributions
ls_monthly  = ls_ser.values
spy_monthly = spy_aligned.values

# BPS sweep (pre-compute)
bps_vals = [0, 5, 10, 15, 20, 25, 30, 40, 50]
ls_cagrs, spy_cagr_val = [], None
for bps in bps_vals:
    p = construct_portfolio(scores2, fwd2, horizon=2, tc_bps=bps)
    s = performance_stats(p.set_index("period")["ls_ret"])
    ls_cagrs.append(s["cagr"] * 100)
spy_stats  = performance_stats(spy_aligned)
spy_cagr_val = spy_stats["cagr"] * 100

# Key stats
ls_stats = _portfolio_stats(port2)

# ── Colour palette ─────────────────────────────────────────────────────────────
C_LS   = "#6C63FF"   # purple — L/S
C_SPY  = "#F59E0B"   # amber  — SPY
C_LONG = "#10B981"   # green  — long leg
C_BG   = "#0F1117"
C_CARD = "#1A1D2E"
C_GRID = "#2D3448"
C_TEXT = "#E2E8F0"
C_MUTED= "#94A3B8"

# ── Figure layout ─────────────────────────────────────────────────────────────
fig = plt.figure(figsize=(16, 11), facecolor=C_BG)
fig.patch.set_facecolor(C_BG)

gs = gridspec.GridSpec(
    3, 3,
    figure=fig,
    hspace=0.42, wspace=0.38,
    left=0.07, right=0.96,
    top=0.88, bottom=0.07,
)

# Panel positions:
# Row 0: Cumulative (spans cols 0-1) | Stats table (col 2)
# Row 1: Rolling Sharpe (spans cols 0-1) | BPS curve (col 2)
# Row 2: Return distribution (spans all 3)

ax_cum   = fig.add_subplot(gs[0, :2])
ax_stats = fig.add_subplot(gs[0, 2])
ax_roll  = fig.add_subplot(gs[1, :2])
ax_bps   = fig.add_subplot(gs[1, 2])
ax_dist  = fig.add_subplot(gs[2, :])

for ax in [ax_cum, ax_stats, ax_roll, ax_bps, ax_dist]:
    ax.set_facecolor(C_CARD)
    ax.spines[["top","right","left","bottom"]].set_color(C_GRID)
    ax.tick_params(colors=C_MUTED, labelsize=8)
    ax.xaxis.label.set_color(C_MUTED)
    ax.yaxis.label.set_color(C_MUTED)
    for spine in ax.spines.values():
        spine.set_linewidth(0.6)

# ── Panel 1: Cumulative Returns ───────────────────────────────────────────────
ax_cum.plot(cum_spy.index, cum_spy.values,   color=C_SPY,  lw=2.2, ls="--", label="SPY Buy-Hold", zorder=4)
ax_cum.plot(cum_ls.index,  cum_ls.values,    color=C_LS,   lw=2.5, label="Ridge H=2 L/S (10 bps)", zorder=5)
ax_cum.fill_between(cum_ls.index, 1, cum_ls.values,  alpha=0.12, color=C_LS)
ax_cum.fill_between(cum_spy.index, 1, cum_spy.values, alpha=0.08, color=C_SPY)
ax_cum.axhline(1, color=C_GRID, lw=0.8, ls=":")
ax_cum.set_title("Cumulative Growth of $1", color=C_TEXT, fontsize=11, fontweight="bold", pad=8)
ax_cum.set_ylabel("Portfolio Value ($)", color=C_MUTED, fontsize=9)
ax_cum.yaxis.set_major_formatter(mticker.FormatStrFormatter("$%.2f"))
ax_cum.grid(color=C_GRID, lw=0.5, alpha=0.7)
ax_cum.legend(fontsize=9, framealpha=0.0, labelcolor=C_TEXT, loc="upper left")

# Annotate final values
final_ls  = cum_ls.iloc[-1]
final_spy = cum_spy.iloc[-1]
ax_cum.annotate(f"${final_ls:.2f}", xy=(cum_ls.index[-1], final_ls),
                xytext=(8, 0), textcoords="offset points",
                color=C_LS, fontsize=9, fontweight="bold", va="center")
ax_cum.annotate(f"${final_spy:.2f}", xy=(cum_spy.index[-1], final_spy),
                xytext=(8, 0), textcoords="offset points",
                color=C_SPY, fontsize=9, fontweight="bold", va="center")

# ── Panel 2: Stats Table ──────────────────────────────────────────────────────
ax_stats.axis("off")
ax_stats.set_title("Key Metrics", color=C_TEXT, fontsize=11, fontweight="bold", pad=8)

metrics = [
    ("CAGR",        f"{ls_stats['cagr']*100:.1f}%",    f"{spy_stats['cagr']*100:.1f}%"),
    ("Ann. Vol",    f"{ls_stats['ann_vol']*100:.1f}%",  f"{spy_stats['ann_vol']*100:.1f}%"),
    ("Sharpe",      f"{ls_stats['sharpe']:.3f}",        f"{spy_stats['sharpe']:.3f}"),
    ("Max DD",      f"{ls_stats['max_drawdown']*100:.1f}%", f"{spy_stats['max_drawdown']*100:.1f}%"),
    ("Hit Rate",    f"{ls_stats['hit_rate']*100:.1f}%", f"{spy_stats['hit_rate']*100:.1f}%"),
    ("Avg TC/mo",   f"{ls_stats['avg_tc_bps']:.1f} bps", "—"),
    ("Months",      f"{ls_stats['n_months']}",          f"{spy_stats['n_months']}"),
]

col_labels = ["Metric", "Ridge H=2\n(10 bps)", "SPY\nBuy-Hold"]
col_x = [0.02, 0.40, 0.72]
row_h = 0.115
y0    = 0.90

# Header
for cx, label in zip(col_x, col_labels):
    ax_stats.text(cx, y0, label, transform=ax_stats.transAxes,
                  color=C_MUTED, fontsize=8, fontweight="bold", va="top")

# Rows
for i, (metric, ls_val, spy_val) in enumerate(metrics):
    y = y0 - (i + 1.3) * row_h
    bg_col = "#1E2235" if i % 2 == 0 else "#22263A"
    rect = mpatches.FancyBboxPatch((0, y - 0.03), 1, row_h * 0.95,
                                    transform=ax_stats.transAxes,
                                    boxstyle="square,pad=0",
                                    facecolor=bg_col, edgecolor="none", clip_on=False)
    ax_stats.add_patch(rect)
    ax_stats.text(col_x[0], y + 0.02, metric, transform=ax_stats.transAxes,
                  color=C_TEXT, fontsize=8.5, va="center")
    # Color LS value green if better (CAGR, Sharpe, Hit Rate), red if worse (Vol, DD)
    ls_good_metrics = {"CAGR", "Sharpe", "Hit Rate"}
    ls_col = C_LS
    ax_stats.text(col_x[1], y + 0.02, ls_val, transform=ax_stats.transAxes,
                  color=ls_col, fontsize=8.5, fontweight="bold", va="center")
    ax_stats.text(col_x[2], y + 0.02, spy_val, transform=ax_stats.transAxes,
                  color=C_SPY, fontsize=8.5, fontweight="bold", va="center")

# Strategy badge
ax_stats.text(0.5, 0.02,
              "Recommended: Ridge H=2 @ 3–10 bps/side",
              transform=ax_stats.transAxes, ha="center",
              color=C_LS, fontsize=8, fontweight="bold",
              bbox=dict(boxstyle="round,pad=0.4", facecolor="#1A1D4E",
                        edgecolor=C_LS, linewidth=1.2))

# ── Panel 3: Rolling 12-Month Sharpe ─────────────────────────────────────────
ax_roll.plot(rs_spy.index, rs_spy.values, color=C_SPY, lw=1.8, ls="--", label="SPY", alpha=0.85)
ax_roll.plot(rs_ls.index,  rs_ls.values,  color=C_LS,  lw=2.0, label="Ridge H=2 (10 bps)")
ax_roll.axhline(0, color=C_GRID, lw=0.8, ls=":")
ax_roll.fill_between(rs_ls.index, 0, rs_ls.values,
                     where=(rs_ls.values > 0), alpha=0.15, color=C_LS, label="_nolegend_")
ax_roll.fill_between(rs_ls.index, 0, rs_ls.values,
                     where=(rs_ls.values <= 0), alpha=0.15, color=C_LS, label="_nolegend_",
                     facecolor="#E74C3C")
ax_roll.set_title("Rolling 12-Month Sharpe Ratio", color=C_TEXT, fontsize=11, fontweight="bold", pad=8)
ax_roll.set_ylabel("Sharpe Ratio", color=C_MUTED, fontsize=9)
ax_roll.grid(color=C_GRID, lw=0.5, alpha=0.7)
ax_roll.legend(fontsize=9, framealpha=0.0, labelcolor=C_TEXT, loc="upper left")
ax_roll.set_ylim(-3, 4)

# ── Panel 4: BPS Sensitivity Curve ───────────────────────────────────────────
ax_bps.plot(bps_vals, ls_cagrs, color=C_LS, lw=2.2, marker="o", ms=5, label="Ridge H=2")
ax_bps.axhline(spy_cagr_val, color=C_SPY, lw=1.8, ls="--", label=f"SPY ({spy_cagr_val:.1f}%)")
ax_bps.axhline(0, color=C_GRID, lw=0.8, ls=":")
ax_bps.fill_between(bps_vals, ls_cagrs, 0,
                    where=[v > 0 for v in ls_cagrs], alpha=0.15, color=C_LS)
ax_bps.fill_between(bps_vals, ls_cagrs, 0,
                    where=[v <= 0 for v in ls_cagrs], alpha=0.15, color="#E74C3C")

# Breakeven marker
be_bps = next((b for b, c in zip(bps_vals, ls_cagrs) if c <= 0), None)
ax_bps.axvline(10, color=C_LS, lw=1, ls=":", alpha=0.7)
ax_bps.text(10.5, min(ls_cagrs) + 1.5, "10 bps\n(base case)", color=C_LS, fontsize=7.5)

ax_bps.set_title("CAGR vs. Transaction Cost", color=C_TEXT, fontsize=11, fontweight="bold", pad=8)
ax_bps.set_xlabel("BPS / side", color=C_MUTED, fontsize=9)
ax_bps.set_ylabel("CAGR (%)", color=C_MUTED, fontsize=9)
ax_bps.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
ax_bps.grid(color=C_GRID, lw=0.5, alpha=0.7)
ax_bps.legend(fontsize=9, framealpha=0.0, labelcolor=C_TEXT)
ax_bps.set_xticks(bps_vals)

# ── Panel 5: Monthly Return Distribution ─────────────────────────────────────
bins = np.linspace(-0.15, 0.15, 36)
ax_dist.hist(spy_monthly, bins=bins, alpha=0.55, color=C_SPY, label="SPY Buy-Hold", density=True)
ax_dist.hist(ls_monthly,  bins=bins, alpha=0.55, color=C_LS,  label="Ridge H=2 L/S (10 bps)", density=True)
ax_dist.axvline(np.mean(spy_monthly), color=C_SPY, lw=1.8, ls="--",
                label=f"SPY mean {np.mean(spy_monthly)*100:.2f}%/mo")
ax_dist.axvline(np.mean(ls_monthly),  color=C_LS,  lw=1.8, ls="--",
                label=f"Ridge mean {np.mean(ls_monthly)*100:.2f}%/mo")
ax_dist.axvline(0, color=C_GRID, lw=0.8, ls=":")
ax_dist.set_title("Monthly Return Distribution", color=C_TEXT, fontsize=11, fontweight="bold", pad=8)
ax_dist.set_xlabel("Monthly Return", color=C_MUTED, fontsize=9)
ax_dist.set_ylabel("Density", color=C_MUTED, fontsize=9)
ax_dist.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v*100:.0f}%"))
ax_dist.grid(color=C_GRID, lw=0.5, alpha=0.7)
ax_dist.legend(fontsize=9, framealpha=0.0, labelcolor=C_TEXT)

# ── Header ────────────────────────────────────────────────────────────────────
fig.text(0.5, 0.955, "Ridge L/S Strategy vs. SPY Buy-Hold",
         ha="center", va="center", fontsize=18, fontweight="bold", color=C_TEXT)
fig.text(0.5, 0.928,
         "Recommended strategy: Ridge cross-section H=2, top/bottom 20% monthly rebalance, 10 bps/side TC  ·  "
         "Universe: homebuilders, REITs, infrastructure ETFs  ·  Walk-forward out-of-sample",
         ha="center", va="center", fontsize=9.5, color=C_MUTED)

# Strategy summary callout
fig.text(0.965, 0.955,
         f"Signal alpha vs SPY\nat 0 bps: +{(ls_stats['cagr']-spy_stats['cagr'])*100:+.1f}%\n"
         f"at 10 bps: {(ls_stats['cagr']-spy_stats['cagr'])*100:+.1f}%\n"
         f"Break-even: ~40 bps",
         ha="right", va="top", fontsize=8.5, color=C_LS,
         bbox=dict(boxstyle="round,pad=0.5", facecolor="#1A1D4E",
                   edgecolor=C_LS, linewidth=1.2))

# Footer
fig.text(0.5, 0.012,
         "Urban Growth Research Platform  ·  Ridge regression, walk-forward CV, 36-month rolling train  ·  "
         "10 bps/side TC on weight changes  ·  Sharpe assumes 0% RFR  ·  For research purposes only",
         ha="center", fontsize=7.5, color=C_MUTED)

out = asset_dir / "strategy_comparison.png"
fig.savefig(out, dpi=180, bbox_inches="tight", facecolor=C_BG)
plt.close(fig)
print(f"Saved: {out}  ({out.stat().st_size // 1024} KB)")
