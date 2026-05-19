"""
Generate per-city opportunity maps, standalone forecast tables, and CSVs.

Outputs (all in D:/urbangrowth_data/processed/maps/):
  opportunity_map_phoenix.png        — map only
  opportunity_map_austin.png         — map only
  opportunity_table_phoenix.png      — top-5 forecast table only
  opportunity_table_austin.png       — top-5 forecast table only
  opportunities_phoenix_forecast.csv — all Tier-1 cells + forecast values
  opportunities_austin_forecast.csv  — all Tier-1 cells + forecast values
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

OUT_DIR = Path("D:/urbangrowth_data/processed/maps")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Palette ───────────────────────────────────────────────────────────────────
C_BG      = "#F7F9FC"
C_CARD    = "#FFFFFF"
C_BORDER  = "#CBD5E1"
C_TEXT    = "#1E293B"
C_MUTED   = "#64748B"
C_SUB     = "#94A3B8"
C_ROW_ALT = "#F1F5F9"
C_TOP5    = "#16A34A"

C_T1_PHX  = "#C0390B"
C_T2_PHX  = "#F0A070"
C_T1_AUS  = "#1565C0"
C_T2_AUS  = "#90CAF9"

TILE = ctx.providers.Esri.WorldStreetMap

# ── Financial model constants ─────────────────────────────────────────────────
FAR         = {"residential": 0.50, "commercial": 0.65, "vacant": 0.40,
               "open_space": 0.20, "industrial": 0.55, "multifamily": 0.60,
               "unknown": 0.40}
RENT_PSF_YR = {"residential": 21.0, "commercial": 26.0, "vacant": 21.0,
               "open_space": 16.0, "industrial": 18.0, "multifamily": 21.0,
               "unknown": 20.0}
EXIT_CAP    = {"residential": 0.051, "commercial": 0.062, "vacant": 0.055,
               "open_space": 0.058, "industrial": 0.060, "multifamily": 0.051,
               "unknown": 0.054}
VACANCY     = {"phoenix": 0.07, "austin": 0.08}
OPEX_RATIO  = 0.35
SOFT_PCT    = 0.15
LTV         = 0.65
CONSTR_RATE = 0.085
PERM_RATE   = 0.060
DRAW_FACTOR = 0.55
LEASEUP_MO  = {"phoenix": 12, "austin": 15}
LEASEUP_OCC = 0.55
RENT_STRESS = {"phoenix": 0.00, "austin": -0.02}
RENT_GROWTH = {"phoenix": 0.03, "austin": 0.02}
HOLD_YEARS  = 5
SELL_COST   = 0.02
COST_PSF    = {"phoenix": 175, "austin": 155}
COST_MIN, COST_MAX = 140, 220

def _irr(cfs, guess=0.15, tol=1e-6, max_iter=200):
    r = guess
    for _ in range(max_iter):
        f  = sum(c / (1+r)**t for t,c in enumerate(cfs))
        df = sum(-t*c / (1+r)**(t+1) for t,c in enumerate(cfs))
        if abs(df) < 1e-12: break
        r1 = r - f/df
        if abs(r1-r) < tol: return r1
        r = r1
    return r

def forecast_lot(row, city):
    ptype    = str(row.get("type", "unknown")).lower().split()[0]
    if ptype not in FAR: ptype = "unknown"
    acres    = float(row.get("acres", 208) or 208)
    cpf      = np.clip(float(row.get("cost_psf", COST_PSF[city]) or COST_PSF[city]),
                       COST_MIN, COST_MAX)
    lv_acre  = float(row.get("land_val_acre", 0) or 0)
    build_mo = float(row.get("build_mo", 12) or 12) or 12

    land_val     = lv_acre * acres
    buildable_sf = acres * FAR[ptype] * 43_560
    hard_cost    = buildable_sf * cpf
    total_cost   = hard_cost * (1 + SOFT_PCT)
    equity       = total_cost * (1 - LTV)
    loan         = total_cost * LTV

    constr_int   = loan * DRAW_FACTOR * CONSTR_RATE * (build_mo / 12)
    lup_mo       = LEASEUP_MO[city]
    leaseup_int  = loan * CONSTR_RATE * (lup_mo / 12)

    noi_yr1_raw  = buildable_sf * RENT_PSF_YR[ptype] * (1 - VACANCY[city]) * (1 - OPEX_RATIO)
    noi_yr1      = noi_yr1_raw * (1 + RENT_STRESS[city])
    partial_noi  = noi_yr1 * LEASEUP_OCC * (lup_mo / 12)

    cap          = EXIT_CAP[ptype]
    stab_val     = noi_yr1 / cap
    perm_loan    = min(loan, stab_val * LTV)
    perm_int_yr  = perm_loan * PERM_RATE
    rg           = RENT_GROWTH[city]

    cocf_series  = [noi_yr1 * (1+rg)**y - perm_int_yr for y in range(HOLD_YEARS)]
    noi_yr5      = noi_yr1 * (1+rg) ** HOLD_YEARS
    exit_val     = noi_yr5 / cap
    net_proceeds = exit_val * (1 - SELL_COST) - perm_loan

    cf0 = -(equity + constr_int + leaseup_int - partial_noi)
    cfs = [cf0] + cocf_series[:]
    cfs[-1] += net_proceeds
    try:    irr = _irr(cfs)
    except: irr = float("nan")

    total_in  = abs(cf0)
    total_out = sum(c for c in cfs[1:] if c > 0)
    em = (total_in + total_out) / total_in if total_in > 0 else float("nan")

    return dict(land_val=land_val, total_cost=total_cost, equity=equity,
                stab_val=stab_val, exit_val=exit_val,
                noi_yr1=noi_yr1, noi_yr5=noi_yr5, irr=irr, em=em,
                buildable_sf=buildable_sf, cap_rate=cap)

# ── Load data ─────────────────────────────────────────────────────────────────
phx_raw = pd.read_csv("docs/_phx_opps.csv", encoding="utf-8", encoding_errors="replace")
aus_raw = pd.read_csv("docs/_aus_opps.csv", encoding="utf-8", encoding_errors="replace")
phx_raw["city"] = "phoenix"
aus_raw["city"] = "austin"

def prep(df):
    df = df.dropna(subset=["lat","lon"]).copy()
    df = df[(df["lat"] != 0) & (df["lon"] != 0)]
    df["is_tier1"]   = df["tier"].str.startswith("Tier 1")
    mn, mx = df["score"].min(), df["score"].max()
    df["score_norm"] = (df["score"] - mn) / (mx - mn + 1e-9)
    return df.reset_index(drop=True)

phx_df = prep(phx_raw)
aus_df = prep(aus_raw)

# ── 1. Generate full-opportunity CSVs (all Tier-1) ────────────────────────────
def build_forecast_df(city_df, city):
    """Compute forecast for every Tier-1 cell and return as DataFrame."""
    t1 = city_df[city_df.is_tier1].copy().reset_index(drop=True)
    records = []
    for _, row in t1.iterrows():
        f = forecast_lot(row, city)
        records.append({
            "rank":               int(row.get("rank", 0)),
            "h3_index":           row["h3_index"],
            "city":               city,
            "tier":               row["tier"],
            "opportunity_score":  round(row["score"], 4),
            "lat":                round(row["lat"], 5),
            "lon":                round(row["lon"], 5),
            "acreage_est":        round(row["acres"], 1),
            "dist_to_center_km":  round(row["dist_km"], 1),
            "property_type":      row["type"],
            "land_value_current": round(f["land_val"]),
            "total_dev_cost":     round(f["total_cost"]),
            "equity_required":    round(f["equity"]),
            "buildable_sqft":     round(f["buildable_sf"]),
            "noi_year1":          round(f["noi_yr1"]),
            "noi_year5":          round(f["noi_yr5"]),
            "stabilised_value":   round(f["stab_val"]),
            "exit_value_yr5":     round(f["exit_val"]),
            "exit_cap_rate":      round(f["cap_rate"], 3),
            "est_irr_pct":        round(f["irr"] * 100, 2) if not np.isnan(f["irr"]) else None,
            "equity_multiple":    round(f["em"], 2)        if not np.isnan(f["em"])  else None,
            "value_uplift":       round(f["exit_val"] - f["land_val"])
                                  if f["land_val"] > 0 else None,
        })
    return pd.DataFrame(records).sort_values("opportunity_score", ascending=False)

print("Computing Phoenix forecasts…")
phx_fc = build_forecast_df(phx_df, "phoenix")
phx_fc.to_csv(OUT_DIR / "opportunities_phoenix_forecast.csv", index=False)
print(f"  Saved: opportunities_phoenix_forecast.csv  ({len(phx_fc)} rows)")

print("Computing Austin forecasts…")
aus_fc = build_forecast_df(aus_df, "austin")
aus_fc.to_csv(OUT_DIR / "opportunities_austin_forecast.csv", index=False)
print(f"  Saved: opportunities_austin_forecast.csv  ({len(aus_fc)} rows)")

# ── 2. Build top-5 display table ──────────────────────────────────────────────
TABLE_COLS = [
    ("rank",       "#"),
    ("score",      "Score"),
    ("type",       "Type"),
    ("land_now",   "Land Value\n(Current)"),
    ("dev_cost",   "Total Dev\nCost"),
    ("stab_val",   "Stabilised\nValue (~2 yr)"),
    ("exit_val",   "Exit Value\n(Year 5)"),
    ("uplift",     "Value\nUplift"),
    ("noi_yr1",    "NOI Yr 1"),
    ("noi_yr5",    "NOI Yr 5"),
    ("irr",        "Est. IRR"),
    ("em",         "Equity\nMultiple"),
]

def build_top5_display(city_df, city):
    top5 = city_df.nlargest(5, "score").reset_index(drop=True)
    rows, raw = [], []
    for i, row in top5.iterrows():
        f = forecast_lot(row, city)
        uplift = f["exit_val"] - f["land_val"] if f["land_val"] > 0 else 0
        rows.append({
            "rank":     i + 1,
            "score":    f"{row['score']:.4f}",
            "type":     str(row.get("type","—")).title(),
            "land_now": f"${f['land_val']/1e6:.2f}M" if f["land_val"] > 0 else "—",
            "dev_cost": f"${f['total_cost']/1e6:.1f}M",
            "stab_val": f"${f['stab_val']/1e6:.1f}M",
            "exit_val": f"${f['exit_val']/1e6:.1f}M",
            "uplift":   f"${uplift/1e6:.1f}M"         if uplift > 0 else "—",
            "noi_yr1":  f"${f['noi_yr1']/1e3:.0f}K",
            "noi_yr5":  f"${f['noi_yr5']/1e3:.0f}K",
            "irr":      f"{f['irr']*100:.1f}%" if not np.isnan(f["irr"]) else "—",
            "em":       f"{f['em']:.2f}×"      if not np.isnan(f["em"])  else "—",
        })
        raw.append(row)
    return pd.DataFrame(rows), pd.DataFrame(raw).reset_index(drop=True)

phx_tbl, phx_top5_raw = build_top5_display(phx_df, "phoenix")
aus_tbl, aus_top5_raw = build_top5_display(aus_df, "austin")

# ── 3. Standalone map PNG ─────────────────────────────────────────────────────
def save_map(city_df, city_name, c_t1, c_t2, top5_raw, out_path):
    gdf = gpd.GeoDataFrame(city_df,
                           geometry=gpd.points_from_xy(city_df["lon"], city_df["lat"]),
                           crs="EPSG:4326").to_crs("EPSG:3857")
    t1, t2 = gdf[gdf.is_tier1], gdf[~gdf.is_tier1]
    top5_gdf = gpd.GeoDataFrame(top5_raw,
                                geometry=gpd.points_from_xy(top5_raw["lon"], top5_raw["lat"]),
                                crs="EPSG:4326").to_crs("EPSG:3857")

    fig, ax = plt.subplots(figsize=(16, 13), facecolor=C_BG)
    ax.set_facecolor(C_BG)

    t2.plot(ax=ax, color=c_t2, markersize=7  + t2["score_norm"]*12, alpha=0.60, marker="h", linewidth=0)
    t1.plot(ax=ax, color=c_t1, markersize=18 + t1["score_norm"]*40, alpha=0.85, marker="h", linewidth=0)
    top5_gdf.plot(ax=ax, color=C_TOP5, markersize=90, alpha=1.0, marker="*", linewidth=0, zorder=6)
    top5_gdf.plot(ax=ax, facecolor="none", edgecolor=C_TEXT, markersize=110, linewidth=1.4, marker="o", zorder=5)

    try:
        ctx.add_basemap(ax, source=TILE, zoom=12, attribution=False)
    except Exception as e:
        print(f"  Tile warning: {e}")

    ax.set_xticks([]); ax.set_yticks([])
    for sp in ax.spines.values(): sp.set_edgecolor(C_BORDER); sp.set_linewidth(1.0)

    for i, (_, row) in enumerate(top5_gdf.iterrows()):
        ax.annotate(f"#{i+1}",
                    xy=(row.geometry.x, row.geometry.y),
                    xytext=(16, 16), textcoords="offset points",
                    fontsize=9, fontweight="bold", color=C_TOP5,
                    path_effects=[pe.withStroke(linewidth=2.5, foreground="white")],
                    arrowprops=dict(arrowstyle="-", color=C_TOP5, lw=0.9,
                                   connectionstyle="arc3,rad=0.1"), zorder=8)

    fig.text(0.50, 0.965, f"{city_name} MSA — Development Opportunity Map",
             ha="center", va="top", fontsize=21, fontweight="bold", color=C_TEXT)
    fig.text(0.50, 0.946,
             f"H3 Resolution-8  ·  {len(t1):,} Tier-1 (top 10%)  ·  "
             f"{len(t2):,} Tier-2 (top 10–25%)  ·  May 2026",
             ha="center", va="top", fontsize=10.5, color=C_MUTED)

    stats = (f"Total cells:          {len(gdf):,}\n"
             f"Tier-1 (top 10%):     {len(t1):,}\n"
             f"Tier-2 (top 10–25%):  {len(t2):,}\n"
             f"Highest score:        {gdf.score.max():.4f}\n"
             f"Mean score (Tier-1):  {t1.score.mean():.4f}\n"
             f"Mean score (Tier-2):  {t2.score.mean():.4f}")
    ax.text(0.985, 0.985, stats, transform=ax.transAxes, fontsize=9,
            va="top", ha="right", color=C_TEXT, linespacing=1.7, family="monospace",
            bbox=dict(boxstyle="round,pad=0.55", facecolor=C_CARD,
                      edgecolor=C_BORDER, linewidth=1.0, alpha=0.93))

    legend_els = [
        mpatches.Patch(facecolor=c_t1, edgecolor="none", label=f"Tier 1 — Top 10%  ({len(t1):,} cells)"),
        mpatches.Patch(facecolor=c_t2, edgecolor="none", label=f"Tier 2 — Top 10–25%  ({len(t2):,} cells)"),
        plt.Line2D([0],[0], marker="*", color="w", markerfacecolor=C_TOP5,
                   markersize=13, linestyle="None", label="Top 5 opportunities"),
    ]
    ax.legend(handles=legend_els, loc="lower left", frameon=True,
              facecolor=C_CARD, edgecolor=C_BORDER, labelcolor=C_TEXT,
              fontsize=10, borderpad=0.8, handlelength=1.5, labelspacing=0.7)

    fig.text(0.50, 0.012,
             "Opportunity score = ML model percentile rank (GBM, walk-forward CV).  "
             "Cell size proportional to score.  Map tiles © Esri / OpenStreetMap contributors.",
             ha="center", va="bottom", fontsize=8, color=C_SUB)

    plt.tight_layout(rect=[0, 0.022, 1, 0.94])
    fig.savefig(out_path, dpi=200, bbox_inches="tight", facecolor=C_BG, edgecolor="none")
    plt.close(fig)
    print(f"  Saved: {out_path.name}  ({out_path.stat().st_size//1024} KB)")

# ── 4. Standalone table PNG ───────────────────────────────────────────────────
def save_table(df_tbl, city_name, c_t1, out_path):
    col_keys   = [c[0] for c in TABLE_COLS]
    col_labels = [c[1] for c in TABLE_COLS]
    n_cols     = len(col_keys)
    n_rows     = len(df_tbl)

    fig, ax = plt.subplots(figsize=(18, 4.8), facecolor=C_BG)
    ax.set_facecolor(C_BG); ax.axis("off")

    # Title
    fig.text(0.50, 0.97,
             f"{city_name} — Top 5 Opportunity Value Forecast",
             ha="center", va="top", fontsize=16, fontweight="bold", color=C_TEXT)
    fig.text(0.50, 0.88,
             "Current land value  →  stabilised value at lease-up (~2 yr)  →  "
             "5-year exit value.   IRR = equity return incl. construction carry & lease-up.",
             ha="center", va="top", fontsize=9, color=C_MUTED)

    # Column widths
    col_w = [0.035, 0.065, 0.075, 0.090, 0.090, 0.100, 0.100, 0.085, 0.085, 0.085, 0.065, 0.080]
    assert len(col_w) == n_cols, f"{len(col_w)} vs {n_cols}"
    pad     = 0.008
    x0      = [sum(col_w[:j]) for j in range(n_cols)]
    row_h   = 0.155
    hdr_top = 0.78

    # Header
    for lbl, xs, w in zip(col_labels, x0, col_w):
        rect = plt.Rectangle((xs, hdr_top - row_h), w-pad, row_h,
                              transform=ax.transAxes, clip_on=False,
                              facecolor=c_t1, edgecolor="white", linewidth=0.5)
        ax.add_patch(rect)
        ax.text(xs + w/2 - pad/2, hdr_top - row_h/2, lbl,
                transform=ax.transAxes, ha="center", va="center",
                fontsize=8, fontweight="bold", color="white", linespacing=1.25)

    # Data rows
    for i, (_, row) in enumerate(df_tbl.iterrows()):
        y_top = hdr_top - row_h*(i+1) - 0.01
        bg    = C_CARD if i % 2 == 0 else C_ROW_ALT
        for key, xs, w in zip(col_keys, x0, col_w):
            rect = plt.Rectangle((xs, y_top - row_h), w-pad, row_h,
                                  transform=ax.transAxes, clip_on=False,
                                  facecolor=bg, edgecolor=C_BORDER, linewidth=0.4)
            ax.add_patch(rect)
            val   = str(row[key])
            bold  = key == "rank"
            color = C_TOP5 if key == "irr" else (c_t1 if bold else C_TEXT)
            ax.text(xs + w/2 - pad/2, y_top - row_h/2, val,
                    transform=ax.transAxes, ha="center", va="center",
                    fontsize=9, fontweight="bold" if bold else "normal", color=color)

    # Value progression label
    arrow_y = hdr_top - row_h*(n_rows+1) - 0.04
    for lbl, cx in [
        ("Current Land Value", x0[3] + col_w[3]/2),
        ("→  Stabilised (~2 yr)", x0[5] + col_w[5]/2),
        ("→  5-Year Exit",        x0[6] + col_w[6]/2),
        ("→  Value Uplift",       x0[7] + col_w[7]/2),
    ]:
        ax.text(cx, arrow_y, lbl, transform=ax.transAxes,
                ha="center", va="top", fontsize=8, color=C_MUTED, style="italic")

    # Footer
    fig.text(0.50, 0.01,
             "Dev cost = hard cost + 15% soft costs.  "
             "Stabilised = NOI Yr1 / exit cap.  Exit = NOI Yr5 / exit cap net of 2% selling costs.  "
             "5-yr hold, 6% perm loan, 8.5% construction rate, 55% progressive draw.",
             ha="center", va="bottom", fontsize=7.5, color=C_SUB)

    plt.tight_layout(rect=[0, 0.05, 1, 0.84])
    fig.savefig(out_path, dpi=200, bbox_inches="tight", facecolor=C_BG, edgecolor="none")
    plt.close(fig)
    print(f"  Saved: {out_path.name}  ({out_path.stat().st_size//1024} KB)")

# ── Run all outputs ───────────────────────────────────────────────────────────
print("\nRendering Phoenix map…")
save_map(phx_df, "Phoenix", C_T1_PHX, C_T2_PHX, phx_top5_raw,
         OUT_DIR / "opportunity_map_phoenix.png")

print("Rendering Austin map…")
save_map(aus_df, "Austin",  C_T1_AUS, C_T2_AUS, aus_top5_raw,
         OUT_DIR / "opportunity_map_austin.png")

print("Rendering Phoenix table…")
save_table(phx_tbl, "Phoenix", C_T1_PHX, OUT_DIR / "opportunity_table_phoenix.png")

print("Rendering Austin table…")
save_table(aus_tbl, "Austin",  C_T1_AUS, OUT_DIR / "opportunity_table_austin.png")

print("\nAll done.")
