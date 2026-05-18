"""
Generate individual light-mode opportunity maps for Phoenix and Austin.

Outputs:
  D:/urbangrowth_data/processed/maps/opportunity_map_phoenix.png
  D:/urbangrowth_data/processed/maps/opportunity_map_austin.png
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
import matplotlib.patheffects as pe
from matplotlib.gridspec import GridSpec
from pathlib import Path
import contextily as ctx

# ── Output paths ──────────────────────────────────────────────────────────────
OUT_DIR = Path("D:/urbangrowth_data/processed/maps")
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_PHX = OUT_DIR / "opportunity_map_phoenix.png"
OUT_AUS = OUT_DIR / "opportunity_map_austin.png"

# ── Light-mode palette ────────────────────────────────────────────────────────
C_BG      = "#F7F9FC"
C_CARD    = "#FFFFFF"
C_BORDER  = "#CBD5E1"
C_TEXT    = "#1E293B"
C_MUTED   = "#64748B"
C_SUB     = "#94A3B8"

# Phoenix: deep orange Tier-1, warm tan Tier-2
C_T1_PHX  = "#C0390B"   # strong burnt orange
C_T2_PHX  = "#F0A070"   # light salmon

# Austin: deep teal Tier-1, pale sky Tier-2
C_T1_AUS  = "#1565C0"   # deep blue
C_T2_AUS  = "#90CAF9"   # pale blue

C_TOP5    = "#16A34A"   # green star highlights

TILE = ctx.providers.Esri.WorldStreetMap  # full street + building detail

# ── Load data ─────────────────────────────────────────────────────────────────
phx_raw = pd.read_csv("docs/_phx_opps.csv", encoding="utf-8", encoding_errors="replace")
aus_raw = pd.read_csv("docs/_aus_opps.csv", encoding="utf-8", encoding_errors="replace")
phx_raw["city"] = "phoenix"
aus_raw["city"] = "austin"

def prep(df):
    df = df.dropna(subset=["lat","lon"])
    df = df[(df["lat"] != 0) & (df["lon"] != 0)].copy()
    df["is_tier1"] = df["tier"].str.startswith("Tier 1")
    mn, mx = df["score"].min(), df["score"].max()
    df["score_norm"] = (df["score"] - mn) / (mx - mn + 1e-9)
    return df

phx_df = prep(phx_raw)
aus_df = prep(aus_raw)

for label, df in [("Phoenix", phx_df), ("Austin", aus_df)]:
    t1 = df.is_tier1.sum()
    t2 = (~df.is_tier1).sum()
    print(f"{label}: {len(df)} cells  |  Tier-1: {t1}  Tier-2: {t2}  "
          f"|  Score range: {df.score.min():.3f}–{df.score.max():.3f}")

# ── Core draw function ────────────────────────────────────────────────────────
def make_city_map(city_df, city_name, c_t1, c_t2, out_path, accent="#16A34A"):
    gdf = gpd.GeoDataFrame(
        city_df,
        geometry=gpd.points_from_xy(city_df["lon"], city_df["lat"]),
        crs="EPSG:4326",
    ).to_crs("EPSG:3857")

    t1   = gdf[gdf.is_tier1]
    t2   = gdf[~gdf.is_tier1]
    top5 = gdf.nlargest(5, "score")

    # ── Figure ────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(16, 13), facecolor=C_BG)
    ax.set_facecolor(C_BG)

    # Tier-2 (background layer)
    sz_t2 = 7 + t2["score_norm"] * 12
    t2.plot(ax=ax, color=c_t2, markersize=sz_t2, alpha=0.60,
            marker="h", linewidth=0)

    # Tier-1 (foreground layer)
    sz_t1 = 18 + t1["score_norm"] * 40
    t1.plot(ax=ax, color=c_t1, markersize=sz_t1, alpha=0.85,
            marker="h", linewidth=0)

    # Top-5 callout: filled green star + dark ring
    top5.plot(ax=ax, color=accent, markersize=90, alpha=1.0,
              marker="*", linewidth=0, zorder=6)
    top5.plot(ax=ax, facecolor="none", edgecolor="#1E293B",
              markersize=110, linewidth=1.4,
              marker="o", zorder=5)

    # Map tile
    try:
        ctx.add_basemap(ax, source=TILE, zoom=12, attribution=False)
    except Exception as e:
        print(f"  Tile warning: {e}")

    # Clean axes
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_edgecolor(C_BORDER)
        spine.set_linewidth(1.0)

    # ── Title block ───────────────────────────────────────────────────────────
    fig.text(0.50, 0.965,
             f"{city_name} MSA — Development Opportunity Map",
             ha="center", va="top", fontsize=20, fontweight="bold",
             color=C_TEXT)
    fig.text(0.50, 0.945,
             f"H3 Resolution-8 grid  ·  {len(t1):,} Tier-1 cells (top 10%)  ·  "
             f"{len(t2):,} Tier-2 cells (top 10–25%)  ·  May 2026",
             ha="center", va="top", fontsize=10.5, color=C_MUTED)

    # ── Stats panel (top-right inset) ─────────────────────────────────────────
    stats_lines = [
        f"Total cells scored:   {len(gdf):,}",
        f"Tier-1 (top 10%):     {len(t1):,}",
        f"Tier-2 (top 10–25%):  {len(t2):,}",
        f"Highest score:        {gdf.score.max():.4f}",
        f"Mean score (Tier-1):  {t1.score.mean():.4f}",
        f"Mean score (Tier-2):  {t2.score.mean():.4f}",
    ]
    stats_text = "\n".join(stats_lines)
    ax.text(0.985, 0.985, stats_text,
            transform=ax.transAxes, fontsize=9, va="top", ha="right",
            color=C_TEXT, linespacing=1.7, family="monospace",
            bbox=dict(boxstyle="round,pad=0.55", facecolor=C_CARD,
                      edgecolor=C_BORDER, linewidth=1.0, alpha=0.93))

    # ── Top-5 labels ──────────────────────────────────────────────────────────
    for _, row in top5.iterrows():
        rank = int(row["rank"]) if "rank" in row.index else ""
        label_txt = f"#{rank}" if rank else "★"
        ax.annotate(
            label_txt,
            xy=(row.geometry.x, row.geometry.y),
            xytext=(14, 14), textcoords="offset points",
            fontsize=8.5, fontweight="bold", color=accent,
            path_effects=[pe.withStroke(linewidth=2.5, foreground="white")],
            arrowprops=dict(arrowstyle="-", color=accent,
                            lw=0.9, connectionstyle="arc3,rad=0.1"),
            zorder=7,
        )

    # ── Legend ────────────────────────────────────────────────────────────────
    legend_elements = [
        mpatches.Patch(facecolor=c_t1, edgecolor="none",
                       label=f"Tier 1 — Top 10%  ({len(t1):,} cells)"),
        mpatches.Patch(facecolor=c_t2, edgecolor="none",
                       label=f"Tier 2 — Top 10–25%  ({len(t2):,} cells)"),
        plt.Line2D([0], [0], marker="*", color="w",
                   markerfacecolor=accent, markersize=13,
                   linestyle="None", label="Top 5 opportunities"),
    ]
    ax.legend(
        handles=legend_elements,
        loc="lower left",
        frameon=True,
        facecolor=C_CARD,
        edgecolor=C_BORDER,
        labelcolor=C_TEXT,
        fontsize=10,
        borderpad=0.8,
        handlelength=1.5,
        labelspacing=0.7,
    )

    # ── Footer ────────────────────────────────────────────────────────────────
    fig.text(0.50, 0.012,
             "Opportunity score = ML model percentile rank (GBM, walk-forward CV).  "
             "Cell size proportional to score.  Tier-1 ≥ 90th percentile within metro.  "
             "Map tiles © CARTO / OpenStreetMap contributors.",
             ha="center", va="bottom", fontsize=8, color=C_SUB)

    plt.tight_layout(rect=[0, 0.022, 1, 0.94])
    fig.savefig(out_path, dpi=200, bbox_inches="tight",
                facecolor=C_BG, edgecolor="none")
    plt.close(fig)
    kb = out_path.stat().st_size // 1024
    print(f"Saved: {out_path}  ({kb} KB)")

# ── Generate both maps ────────────────────────────────────────────────────────
print("\nRendering Phoenix…")
make_city_map(phx_df, "Phoenix", C_T1_PHX, C_T2_PHX, OUT_PHX)

print("Rendering Austin…")
make_city_map(aus_df, "Austin",  C_T1_AUS, C_T2_AUS, OUT_AUS)

print("\nDone.")
