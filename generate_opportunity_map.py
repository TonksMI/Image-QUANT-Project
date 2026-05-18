"""
Generate a dual-city opportunity map PNG showing all 2,229 H3 cells
(Phoenix + Austin) colored by tier and opportunity score.

Output: D:/urbangrowth_data/processed/maps/opportunity_map.png
"""
import os, warnings
warnings.filterwarnings("ignore")
os.chdir("C:/Users/17ton/urbangrowth")

import numpy as np
import pandas as pd
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.colors as mcolors
from matplotlib.colorbar import ColorbarBase
from matplotlib.gridspec import GridSpec
from pathlib import Path
import contextily as ctx

# ── Palette ───────────────────────────────────────────────────────────────────
C_BG     = "#0F1117"
C_CARD   = "#1A1D2E"
C_GRID   = "#2D3448"
C_TEXT   = "#E2E8F0"
C_MUTED  = "#94A3B8"
C_PHX    = "#E67E22"   # orange
C_AUS    = "#3498DB"   # blue
C_T1_PHX = "#F39C12"   # bright gold — Phoenix Tier 1
C_T2_PHX = "#784212"   # dark amber  — Phoenix Tier 2
C_T1_AUS = "#2980B9"   # bright blue — Austin Tier 1
C_T2_AUS = "#154360"   # dark navy   — Austin Tier 2
C_ACCENT = "#10B981"   # emerald — top-5 highlight

OUT = Path("D:/urbangrowth_data/processed/maps/opportunity_map.png")
OUT.parent.mkdir(parents=True, exist_ok=True)

# ── 1. Load data ──────────────────────────────────────────────────────────────
phx_csv = Path("docs/_phx_opps.csv")
aus_csv = Path("docs/_aus_opps.csv")

phx = pd.read_csv(phx_csv, encoding="utf-8", encoding_errors="replace")
aus = pd.read_csv(aus_csv, encoding="utf-8", encoding_errors="replace")
phx["city"] = "phoenix"
aus["city"] = "austin"
df = pd.concat([phx, aus], ignore_index=True)

# Drop rows without valid coordinates
df = df.dropna(subset=["lat","lon"])
df = df[(df["lat"] != 0) & (df["lon"] != 0)]

# Tier flag
df["is_tier1"] = df["tier"].str.startswith("Tier 1")

# Score normalised for sizing
df["score_norm"] = (df["score"] - df["score"].min()) / (df["score"].max() - df["score"].min() + 1e-9)

print(f"Total cells: {len(df)}  |  PHX: {(df.city=='phoenix').sum()}  |  AUS: {(df.city=='austin').sum()}")
print(f"Tier-1: {df.is_tier1.sum()}  Tier-2: {(~df.is_tier1).sum()}")

# ── 2. Build GeoDataFrame ─────────────────────────────────────────────────────
gdf = gpd.GeoDataFrame(
    df,
    geometry=gpd.points_from_xy(df["lon"], df["lat"]),
    crs="EPSG:4326"
).to_crs("EPSG:3857")   # Web Mercator for contextily

phx_gdf = gdf[gdf.city == "phoenix"]
aus_gdf = gdf[gdf.city == "austin"]

# Top 5 per city for highlight callouts
phx_top5 = phx_gdf.nlargest(5, "score")
aus_top5 = aus_gdf.nlargest(5, "score")

# ── 3. Figure layout ──────────────────────────────────────────────────────────
fig = plt.figure(figsize=(22, 14), facecolor=C_BG)
gs = GridSpec(
    2, 2,
    figure=fig,
    width_ratios=[1.1, 1],
    height_ratios=[0.06, 1],
    hspace=0.04,
    wspace=0.03,
    left=0.03, right=0.97,
    top=0.92, bottom=0.05,
)

ax_title  = fig.add_subplot(gs[0, :])
ax_phx    = fig.add_subplot(gs[1, 0])
ax_aus    = fig.add_subplot(gs[1, 1])

for ax in [ax_title, ax_phx, ax_aus]:
    ax.set_facecolor(C_BG)
ax_title.axis("off")

# ── 4. Title bar ──────────────────────────────────────────────────────────────
ax_title.text(0.5, 0.85,
    "Urban Growth Opportunity Map — Phoenix & Austin",
    ha="center", va="top", fontsize=22, fontweight="bold",
    color=C_TEXT, transform=ax_title.transAxes)
ax_title.text(0.5, 0.05,
    f"2,229 H3 Resolution-8 cells scored by ML model  ·  "
    f"Phoenix: 478 Tier-1 + 723 Tier-2  ·  Austin: 411 Tier-1 + 617 Tier-2  ·  May 2026",
    ha="center", va="bottom", fontsize=10, color=C_MUTED,
    transform=ax_title.transAxes)

# ── 5. Helper: draw one city panel ───────────────────────────────────────────
def draw_panel(ax, city_gdf, top5_gdf, city_label, c_t1, c_t2, tile_source):
    # Separate tiers
    t1 = city_gdf[city_gdf.is_tier1]
    t2 = city_gdf[~city_gdf.is_tier1]

    # Sizes: Tier-1 bigger, scaled by score
    sz_t2 = 6  + t2["score_norm"] * 10
    sz_t1 = 14 + t1["score_norm"] * 30

    # Plot Tier-2 first (background)
    t2.plot(ax=ax, color=c_t2, markersize=sz_t2, alpha=0.55,
            marker="h", linewidth=0)

    # Plot Tier-1 on top
    t1.plot(ax=ax, color=c_t1, markersize=sz_t1, alpha=0.82,
            marker="h", linewidth=0)

    # Top-5 highlights — white ring + star marker
    top5_gdf.plot(ax=ax, color=C_ACCENT, markersize=60, alpha=1.0,
                  marker="*", linewidth=0, zorder=5)
    top5_gdf.plot(ax=ax, facecolor="none", edgecolor="white",
                  markersize=72, alpha=0.9,
                  marker="o", linewidth=1.2, zorder=4)

    # Tile background
    try:
        ctx.add_basemap(ax, source=tile_source, zoom=10,
                        attribution=False, reset_extent=False)
    except Exception as e:
        print(f"  Tile load warning ({city_label}): {e}")

    # Spines & ticks
    for spine in ax.spines.values():
        spine.set_edgecolor(C_GRID)
        spine.set_linewidth(0.8)
    ax.set_xticks([])
    ax.set_yticks([])

    # City label box
    ax.text(0.02, 0.97, city_label,
            transform=ax.transAxes, fontsize=15, fontweight="bold",
            color="white", va="top",
            bbox=dict(boxstyle="round,pad=0.3", facecolor=C_CARD,
                      edgecolor=c_t1, linewidth=1.5, alpha=0.92))

    # Stats box (bottom-left)
    stats = (f"Tier 1: {len(t1):,}   Tier 2: {len(t2):,}\n"
             f"Top score: {city_gdf['score'].max():.3f}\n"
             f"Avg score (T1): {t1['score'].mean():.3f}")
    ax.text(0.02, 0.04, stats,
            transform=ax.transAxes, fontsize=8.5,
            color=C_MUTED, va="bottom",
            bbox=dict(boxstyle="round,pad=0.35", facecolor=C_BG,
                      edgecolor=C_GRID, linewidth=0.8, alpha=0.9),
            linespacing=1.6)

TILE = ctx.providers.CartoDB.DarkMatter

print("Rendering Phoenix panel…")
draw_panel(ax_phx, phx_gdf, phx_top5, "Phoenix MSA", C_T1_PHX, C_T2_PHX, TILE)

print("Rendering Austin panel…")
draw_panel(ax_aus, aus_gdf, aus_top5, "Austin MSA", C_T1_AUS, C_T2_AUS, TILE)

# ── 6. Legend ────────────────────────────────────────────────────────────────
legend_elements = [
    mpatches.Patch(facecolor=C_T1_PHX, label="Phoenix Tier 1 (top 10%)"),
    mpatches.Patch(facecolor=C_T2_PHX, label="Phoenix Tier 2 (top 10–25%)"),
    mpatches.Patch(facecolor=C_T1_AUS, label="Austin Tier 1 (top 10%)"),
    mpatches.Patch(facecolor=C_T2_AUS, label="Austin Tier 2 (top 10–25%)"),
    plt.Line2D([0],[0], marker="*", color="w", markerfacecolor=C_ACCENT,
               markersize=12, linestyle="None", label="Top 5 per city"),
]
fig.legend(
    handles=legend_elements,
    loc="lower center",
    ncol=5,
    frameon=True,
    facecolor=C_CARD,
    edgecolor=C_GRID,
    labelcolor=C_TEXT,
    fontsize=9.5,
    bbox_to_anchor=(0.5, 0.00),
    borderpad=0.7,
    handlelength=1.4,
)

# ── 7. Save ───────────────────────────────────────────────────────────────────
print("Saving…")
fig.savefig(OUT, dpi=200, bbox_inches="tight",
            facecolor=C_BG, edgecolor="none")
plt.close(fig)
kb = OUT.stat().st_size // 1024
print(f"Saved: {OUT}  ({kb} KB)")
