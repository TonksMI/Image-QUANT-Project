"""
Generate individual light-mode opportunity maps for Phoenix and Austin,
with a Top-5 value forecast table beneath each map.

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
C_HDR     = "#1E293B"   # table header bg
C_HDR_TXT = "#FFFFFF"
C_ROW_ALT = "#F1F5F9"   # alternating row bg

C_T1_PHX  = "#C0390B"
C_T2_PHX  = "#F0A070"
C_T1_AUS  = "#1565C0"
C_T2_AUS  = "#90CAF9"
C_TOP5    = "#16A34A"

TILE = ctx.providers.Esri.WorldStreetMap

# ── Financial model constants (matches generate_re_portfolio_graphic.py) ──────
FAR          = {"residential": 0.50, "commercial": 0.65, "vacant": 0.40,
                "open_space": 0.20, "industrial": 0.55, "multifamily": 0.60,
                "unknown": 0.40}
RENT_PSF_YR  = {"residential": 21.0, "commercial": 26.0, "vacant": 21.0,
                "open_space": 16.0, "industrial": 18.0, "multifamily": 21.0,
                "unknown": 20.0}
EXIT_CAP     = {"residential": 0.051, "commercial": 0.062, "vacant": 0.055,
                "open_space": 0.058, "industrial": 0.060, "multifamily": 0.051,
                "unknown": 0.054}
VACANCY      = {"phoenix": 0.07, "austin": 0.08}
OPEX_RATIO   = 0.35
SOFT_PCT     = 0.15
LTV          = 0.65
CONSTR_RATE  = 0.085
PERM_RATE    = 0.060
DRAW_FACTOR  = 0.55
LEASEUP_MO   = {"phoenix": 12, "austin": 15}
LEASEUP_OCC  = 0.55
RENT_STRESS  = {"phoenix": 0.00, "austin": -0.02}
RENT_GROWTH  = {"phoenix": 0.03, "austin": 0.02}
HOLD_YEARS   = 5
SELL_COST    = 0.02
COST_PSF     = {"phoenix": 175, "austin": 155}
COST_MIN, COST_MAX = 140, 220

def _irr(cfs, guess=0.15, tol=1e-6, max_iter=200):
    """Simple Newton IRR solver."""
    r = guess
    for _ in range(max_iter):
        f  = sum(c / (1 + r) ** t for t, c in enumerate(cfs))
        df = sum(-t * c / (1 + r) ** (t + 1) for t, c in enumerate(cfs))
        if abs(df) < 1e-12:
            break
        r1 = r - f / df
        if abs(r1 - r) < tol:
            return r1
        r = r1
    return r

def forecast_lot(row, city):
    """Return dict of current + projected financial metrics for one H3 cell."""
    ptype   = str(row.get("type", "unknown")).lower()
    if ptype not in FAR:
        ptype = "unknown"
    acres   = float(row.get("acres", 208))
    raw_cpf = float(row.get("cost_psf", COST_PSF.get(city, 165)) or COST_PSF.get(city, 165))
    cpf     = np.clip(raw_cpf, COST_MIN, COST_MAX)
    lv_acre = float(row.get("land_val_acre", 0) or 0)
    build_mo= float(row.get("build_mo", 12) or 12)
    if build_mo == 0:
        build_mo = 12

    # ── Current land value ────────────────────────────────────────────────────
    land_val = lv_acre * acres

    # ── Development sizing ────────────────────────────────────────────────────
    buildable_sf = acres * FAR[ptype] * 43_560
    hard_cost    = buildable_sf * cpf
    total_cost   = hard_cost * (1 + SOFT_PCT)
    equity       = total_cost * (1 - LTV)
    loan         = total_cost * LTV

    # ── Construction carry ────────────────────────────────────────────────────
    constr_int   = loan * DRAW_FACTOR * CONSTR_RATE * (build_mo / 12)

    # ── Lease-up carry ────────────────────────────────────────────────────────
    lup_mo       = LEASEUP_MO[city]
    leaseup_int  = loan * CONSTR_RATE * (lup_mo / 12)

    # ── Year-1 NOI (stabilised) ───────────────────────────────────────────────
    eff_rent     = buildable_sf * RENT_PSF_YR[ptype]
    vac          = VACANCY[city]
    noi_yr1_raw  = eff_rent * (1 - vac) * (1 - OPEX_RATIO)
    stress       = RENT_STRESS[city]
    noi_yr1      = noi_yr1_raw * (1 + stress)

    # Partial NOI during lease-up ramp
    partial_noi  = noi_yr1 * LEASEUP_OCC * (lup_mo / 12)
    net_leaseup  = partial_noi - leaseup_int  # usually negative

    # ── Stabilised value (at CO + lease-up) ──────────────────────────────────
    cap          = EXIT_CAP[ptype]
    stab_val     = noi_yr1 / cap

    # ── Permanent loan (re-sized at stabilisation) ────────────────────────────
    perm_loan    = min(loan, stab_val * LTV)
    perm_int_yr  = perm_loan * PERM_RATE

    # ── Hold cash flows (Year 1–5) ────────────────────────────────────────────
    rg           = RENT_GROWTH[city]
    cocf_series  = []
    for y in range(HOLD_YEARS):
        noi_y  = noi_yr1 * (1 + rg) ** y
        cocf_series.append(noi_y - perm_int_yr)

    # ── Exit value (Year 5) ───────────────────────────────────────────────────
    noi_yr5      = noi_yr1 * (1 + rg) ** HOLD_YEARS
    exit_val     = noi_yr5 / cap
    net_proceeds = exit_val * (1 - SELL_COST) - perm_loan

    # ── IRR cashflows ─────────────────────────────────────────────────────────
    cf0 = -(equity + constr_int + leaseup_int - partial_noi)
    cfs = [cf0] + cocf_series[:]
    cfs[-1] += net_proceeds
    try:
        irr = _irr(cfs)
    except Exception:
        irr = float("nan")

    # ── Equity multiple ───────────────────────────────────────────────────────
    total_in  = abs(cf0)
    total_out = sum(c for c in cfs[1:] if c > 0)
    em        = (total_in + total_out) / total_in if total_in > 0 else float("nan")

    return {
        "land_val":   land_val,
        "total_cost": total_cost,
        "stab_val":   stab_val,
        "exit_val":   exit_val,
        "noi_yr1":    noi_yr1,
        "noi_yr5":    noi_yr5,
        "irr":        irr,
        "em":         em,
        "equity":     equity,
    }

# ── Load + prep data ──────────────────────────────────────────────────────────
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
    t1 = df.is_tier1.sum(); t2 = (~df.is_tier1).sum()
    print(f"{label}: {len(df)} cells | T1: {t1} T2: {t2} | "
          f"Score: {df.score.min():.3f}–{df.score.max():.3f}")

# ── Build top-5 forecast tables ───────────────────────────────────────────────
def build_forecast_table(city_df, city):
    top5 = city_df.nlargest(5, "score").reset_index(drop=True)
    rows = []
    for i, row in top5.iterrows():
        f = forecast_lot(row, city)
        rows.append({
            "rank":       i + 1,
            "h3":         row["h3_index"][:12] + "…",
            "lat":        f"{row['lat']:.4f}",
            "lon":        f"{row['lon']:.4f}",
            "score":      f"{row['score']:.4f}",
            "type":       str(row.get("type","—")).title(),
            "land_now":   f"${f['land_val']/1e6:.2f}M"  if f['land_val'] > 0 else "—",
            "dev_cost":   f"${f['total_cost']/1e6:.1f}M",
            "stab_val":   f"${f['stab_val']/1e6:.1f}M",
            "exit_val":   f"${f['exit_val']/1e6:.1f}M",
            "noi_yr1":    f"${f['noi_yr1']/1e3:.0f}K",
            "noi_yr5":    f"${f['noi_yr5']/1e3:.0f}K",
            "irr":        f"{f['irr']*100:.1f}%" if not np.isnan(f['irr']) else "—",
            "em":         f"{f['em']:.2f}×"      if not np.isnan(f['em']) else "—",
        })
    return pd.DataFrame(rows), top5

phx_tbl, phx_top5_raw = build_forecast_table(phx_df, "phoenix")
aus_tbl, aus_top5_raw = build_forecast_table(aus_df, "austin")

print("\nPhoenix Top-5 Forecast:")
print(phx_tbl[["rank","score","land_now","dev_cost","stab_val","exit_val","irr","em"]].to_string(index=False))
print("\nAustin Top-5 Forecast:")
print(aus_tbl[["rank","score","land_now","dev_cost","stab_val","exit_val","irr","em"]].to_string(index=False))

# ── Draw function ─────────────────────────────────────────────────────────────
TABLE_COLS = [
    ("rank",     "#"),
    ("score",    "Opp. Score"),
    ("type",     "Type"),
    ("land_now", "Land Value\n(Current)"),
    ("dev_cost", "Total Dev\nCost"),
    ("stab_val", "Stabilised\nValue (~2 yr)"),
    ("exit_val", "Exit Value\n(Yr 5)"),
    ("noi_yr1",  "NOI\nYear 1"),
    ("noi_yr5",  "NOI\nYear 5"),
    ("irr",      "Est. IRR"),
    ("em",       "Equity\nMultiple"),
]

def draw_forecast_table(ax_tbl, df_tbl, c_t1, city_name):
    ax_tbl.set_facecolor(C_BG)
    ax_tbl.axis("off")

    col_keys   = [c[0] for c in TABLE_COLS]
    col_labels = [c[1] for c in TABLE_COLS]
    n_cols     = len(col_keys)
    n_rows     = len(df_tbl)

    # Column widths (proportional, must sum to 1)
    col_w = [0.04, 0.08, 0.08, 0.09, 0.09, 0.10, 0.10, 0.09, 0.09, 0.07, 0.09]
    assert len(col_w) == n_cols

    x_starts = np.cumsum([0] + col_w[:-1])
    row_h    = 0.14    # fraction of axes height per row
    hdr_y    = 0.98    # top of header
    pad      = 0.012

    # Section title
    ax_tbl.text(0.0, 1.05, f"Top 5 {city_name} Opportunities — Value Forecast",
                transform=ax_tbl.transAxes,
                fontsize=11, fontweight="bold", color=C_TEXT, va="bottom")
    ax_tbl.text(1.0, 1.05,
                "Current land value → stabilised (CO + lease-up) → 5-yr exit.  "
                "IRR = unlevered project return on equity.",
                transform=ax_tbl.transAxes,
                fontsize=7.5, color=C_MUTED, va="bottom", ha="right")

    # Header row
    for j, (lbl, xs, w) in enumerate(zip(col_labels, x_starts, col_w)):
        rect = plt.Rectangle((xs, hdr_y - row_h), w - pad, row_h,
                              transform=ax_tbl.transAxes, clip_on=False,
                              facecolor=c_t1, edgecolor="white", linewidth=0.6)
        ax_tbl.add_patch(rect)
        ax_tbl.text(xs + w / 2 - pad / 2, hdr_y - row_h / 2,
                    lbl, transform=ax_tbl.transAxes,
                    ha="center", va="center", fontsize=7.8,
                    fontweight="bold", color="white", linespacing=1.3)

    # Data rows
    for i, (_, row) in enumerate(df_tbl.iterrows()):
        y_top = hdr_y - row_h * (i + 1) - 0.01
        bg    = C_CARD if i % 2 == 0 else C_ROW_ALT

        for j, (key, xs, w) in enumerate(zip(col_keys, x_starts, col_w)):
            rect = plt.Rectangle((xs, y_top - row_h), w - pad, row_h,
                                  transform=ax_tbl.transAxes, clip_on=False,
                                  facecolor=bg, edgecolor=C_BORDER, linewidth=0.4)
            ax_tbl.add_patch(rect)

            val   = str(row[key])
            bold  = (key == "rank")
            color = C_TOP5 if key == "irr" else (C_TEXT if not bold else c_t1)
            ax_tbl.text(xs + w / 2 - pad / 2, y_top - row_h / 2,
                        val, transform=ax_tbl.transAxes,
                        ha="center", va="center", fontsize=8.2,
                        fontweight="bold" if bold else "normal", color=color)

    # Arrow annotation: Land → Stab → Exit
    arrow_y = hdr_y - row_h * (n_rows + 1) - 0.04
    for label, x_center in [
        ("Current Land Value", x_starts[3] + col_w[3]/2),
        ("→  Stabilised (~2 yr)", x_starts[5] + col_w[5]/2),
        ("→  5-Year Exit", x_starts[6] + col_w[6]/2),
    ]:
        ax_tbl.text(x_center, arrow_y, label,
                    transform=ax_tbl.transAxes,
                    ha="center", va="top", fontsize=7.5,
                    color=C_MUTED, style="italic")

def make_city_map(city_df, city_name, c_t1, c_t2, out_path, df_tbl, top5_raw):
    gdf = gpd.GeoDataFrame(
        city_df,
        geometry=gpd.points_from_xy(city_df["lon"], city_df["lat"]),
        crs="EPSG:4326",
    ).to_crs("EPSG:3857")

    t1   = gdf[gdf.is_tier1]
    t2   = gdf[~gdf.is_tier1]
    top5_gdf = gpd.GeoDataFrame(
        top5_raw,
        geometry=gpd.points_from_xy(top5_raw["lon"], top5_raw["lat"]),
        crs="EPSG:4326",
    ).to_crs("EPSG:3857")

    # ── Figure: map (top 68%) + table (bottom 32%) ───────────────────────────
    fig = plt.figure(figsize=(18, 20), facecolor=C_BG)
    gs  = GridSpec(2, 1, figure=fig, height_ratios=[1.9, 0.85],
                   hspace=0.10, left=0.03, right=0.97, top=0.95, bottom=0.02)
    ax_map = fig.add_subplot(gs[0])
    ax_tbl = fig.add_subplot(gs[1])
    ax_map.set_facecolor(C_BG)

    # ── Map layers ────────────────────────────────────────────────────────────
    t2.plot(ax=ax_map, color=c_t2, markersize=7  + t2["score_norm"] * 12,
            alpha=0.60, marker="h", linewidth=0)
    t1.plot(ax=ax_map, color=c_t1, markersize=18 + t1["score_norm"] * 40,
            alpha=0.85, marker="h", linewidth=0)

    top5_gdf.plot(ax=ax_map, color=C_TOP5, markersize=90, alpha=1.0,
                  marker="*", linewidth=0, zorder=6)
    top5_gdf.plot(ax=ax_map, facecolor="none", edgecolor="#1E293B",
                  markersize=110, linewidth=1.4, marker="o", zorder=5)

    try:
        ctx.add_basemap(ax_map, source=TILE, zoom=12, attribution=False)
    except Exception as e:
        print(f"  Tile warning: {e}")

    ax_map.set_xticks([]); ax_map.set_yticks([])
    for spine in ax_map.spines.values():
        spine.set_edgecolor(C_BORDER); spine.set_linewidth(1.0)

    # ── Top-5 rank labels on map ──────────────────────────────────────────────
    for i, (_, row) in enumerate(top5_gdf.iterrows()):
        ax_map.annotate(
            f"#{i+1}",
            xy=(row.geometry.x, row.geometry.y),
            xytext=(16, 16), textcoords="offset points",
            fontsize=9, fontweight="bold", color=C_TOP5,
            path_effects=[pe.withStroke(linewidth=2.5, foreground="white")],
            arrowprops=dict(arrowstyle="-", color=C_TOP5,
                            lw=0.9, connectionstyle="arc3,rad=0.1"),
            zorder=8,
        )

    # ── Map title ─────────────────────────────────────────────────────────────
    fig.text(0.50, 0.965,
             f"{city_name} MSA — Development Opportunity Map",
             ha="center", va="top", fontsize=21, fontweight="bold", color=C_TEXT)
    fig.text(0.50, 0.947,
             f"H3 Resolution-8  ·  {len(t1):,} Tier-1 (top 10%)  ·  "
             f"{len(t2):,} Tier-2 (top 10–25%)  ·  May 2026",
             ha="center", va="top", fontsize=10.5, color=C_MUTED)

    # ── Stats inset ───────────────────────────────────────────────────────────
    stats_lines = [
        f"Total cells scored:   {len(gdf):,}",
        f"Tier-1 (top 10%):     {len(t1):,}",
        f"Tier-2 (top 10–25%):  {len(t2):,}",
        f"Highest score:        {gdf.score.max():.4f}",
        f"Mean score (Tier-1):  {t1.score.mean():.4f}",
        f"Mean score (Tier-2):  {t2.score.mean():.4f}",
    ]
    ax_map.text(0.985, 0.985, "\n".join(stats_lines),
                transform=ax_map.transAxes, fontsize=9, va="top", ha="right",
                color=C_TEXT, linespacing=1.7, family="monospace",
                bbox=dict(boxstyle="round,pad=0.55", facecolor=C_CARD,
                          edgecolor=C_BORDER, linewidth=1.0, alpha=0.93))

    # ── Legend ────────────────────────────────────────────────────────────────
    legend_els = [
        mpatches.Patch(facecolor=c_t1, edgecolor="none",
                       label=f"Tier 1 — Top 10%  ({len(t1):,} cells)"),
        mpatches.Patch(facecolor=c_t2, edgecolor="none",
                       label=f"Tier 2 — Top 10–25%  ({len(t2):,} cells)"),
        plt.Line2D([0],[0], marker="*", color="w",
                   markerfacecolor=C_TOP5, markersize=13,
                   linestyle="None", label="Top 5 opportunities"),
    ]
    ax_map.legend(handles=legend_els, loc="lower left", frameon=True,
                  facecolor=C_CARD, edgecolor=C_BORDER, labelcolor=C_TEXT,
                  fontsize=10, borderpad=0.8, handlelength=1.5, labelspacing=0.7)

    # ── Forecast table ────────────────────────────────────────────────────────
    draw_forecast_table(ax_tbl, df_tbl, c_t1, city_name)

    # ── Footer ────────────────────────────────────────────────────────────────
    fig.text(0.50, 0.005,
             "Stabilised value = NOI Yr1 / exit cap rate.  "
             "Exit value = NOI Yr5 / exit cap rate (5-yr hold, net of 2% selling costs).  "
             "IRR = equity IRR incl. construction carry + lease-up.  "
             "Map tiles © Esri / OpenStreetMap contributors.",
             ha="center", va="bottom", fontsize=7.5, color=C_SUB)

    fig.savefig(out_path, dpi=200, bbox_inches="tight",
                facecolor=C_BG, edgecolor="none")
    plt.close(fig)
    kb = out_path.stat().st_size // 1024
    print(f"Saved: {out_path}  ({kb} KB)")

# ── Render ────────────────────────────────────────────────────────────────────
print("\nRendering Phoenix…")
make_city_map(phx_df, "Phoenix", C_T1_PHX, C_T2_PHX, OUT_PHX, phx_tbl, phx_top5_raw)

print("Rendering Austin…")
make_city_map(aus_df, "Austin",  C_T1_AUS, C_T2_AUS, OUT_AUS, aus_tbl, aus_top5_raw)

print("\nDone.")
