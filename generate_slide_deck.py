"""
Urban Growth AI — Corporate Real Estate Investment Strategy
PowerPoint slide deck generator (python-pptx)

Validated market assumptions (Phoenix/Austin 2024-2025):
  - New construction multifamily rent: $1.65-1.85/sqft/month ($20-22/yr)
    Sources: CoStar, RealPage, Yardi Matrix — Phoenix Class A avg $1.72, Austin $1.78
  - Exit cap rates: 4.75-5.25% stabilised Class A Sun Belt
    Sources: CBRE Q4 2024, Marcus & Millichap 2025 Multifamily Outlook
  - Hard construction cost: $155-195/sqft garden-style wood-frame Phoenix/Austin
    Sources: Turner & Townsend 2024 Cost Report, RSMeans
  - All-in development cost (incl. land, soft): $215-280/sqft Phoenix; $240-310/sqft Austin
  - Development spread (value vs cost): 15-30% typical Sun Belt ground-up
  - Levered IRR target: 15-22% for ground-up Sun Belt multifamily (2024-2025)
    Source: NMHC, CBRE Multifamily Capital Markets, PGIM Real Estate
  - Build-to-sell cycle: 18-24 months land → CO; 4-6 months lease-up to 93% occupancy
  - Build-to-rent hold: 3-5 years typical; exit at cap rate compression
"""
import os, sys, warnings, json
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
from pathlib import Path
from io import BytesIO
from dotenv import load_dotenv
load_dotenv(dotenv_path="C:/Users/17ton/urbangrowth/.env")

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt
import pptx.util as putil

# ─── colour palette ────────────────────────────────────────────────────────────
C_BG       = RGBColor(0x0F, 0x11, 0x17)
C_CARD     = RGBColor(0x1A, 0x1D, 0x2E)
C_TEXT     = RGBColor(0xE2, 0xE8, 0xF0)
C_MUTED    = RGBColor(0x94, 0xA3, 0xB8)
C_GREEN    = RGBColor(0x10, 0xB9, 0x81)
C_AMBER    = RGBColor(0xF5, 0x9E, 0x0B)
C_BLUE     = RGBColor(0x38, 0xBD, 0xF8)
C_PURPLE   = RGBColor(0x6C, 0x63, 0xFF)
C_RED      = RGBColor(0xEF, 0x44, 0x44)
C_GRID     = RGBColor(0x2D, 0x34, 0x48)
C_ACCENT   = RGBColor(0x0E, 0xA5, 0xE9)
C_PHX      = RGBColor(0xE6, 0x7E, 0x22)
C_AUS      = RGBColor(0x34, 0x98, 0xDB)
C_WHITE    = RGBColor(0xFF, 0xFF, 0xFF)

# ─── helpers ───────────────────────────────────────────────────────────────────
SLIDE_W = Inches(13.33)
SLIDE_H = Inches(7.5)

def rgb(r, g, b):
    return RGBColor(r, g, b)

def add_slide(prs, layout_idx=6):
    layout = prs.slide_layouts[layout_idx]
    return prs.slides.add_slide(layout)

def bg(slide, color=C_BG):
    """Fill slide background."""
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = color

def box(slide, left, top, width, height, fill_color=None, border_color=None, border_pt=0):
    """Add a filled rectangle."""
    shape = slide.shapes.add_shape(1, Inches(left), Inches(top), Inches(width), Inches(height))
    shape.line.fill.background()
    if fill_color:
        shape.fill.solid()
        shape.fill.fore_color.rgb = fill_color
    else:
        shape.fill.background()
    if border_color and border_pt > 0:
        shape.line.color.rgb = border_color
        shape.line.width = Pt(border_pt)
    else:
        shape.line.fill.background()
    return shape

def txt(slide, text, left, top, width, height, size=18, bold=False, color=C_TEXT,
        align=PP_ALIGN.LEFT, wrap=True, italic=False):
    """Add a text box."""
    txBox = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf    = txBox.text_frame
    tf.word_wrap = wrap
    p     = tf.paragraphs[0]
    p.alignment = align
    run   = p.add_run()
    run.text = str(text)
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color
    run.font.name = "Calibri"
    return txBox

def txt_multi(slide, lines, left, top, width, height, base_size=14,
              color=C_TEXT, align=PP_ALIGN.LEFT):
    """lines = list of (text, size, bold, color) tuples."""
    txBox = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf    = txBox.text_frame
    tf.word_wrap = True
    for i, line_spec in enumerate(lines):
        if isinstance(line_spec, str):
            text, size, bold, col = line_spec, base_size, False, color
        else:
            text = line_spec[0]
            size = line_spec[1] if len(line_spec) > 1 else base_size
            bold = line_spec[2] if len(line_spec) > 2 else False
            col  = line_spec[3] if len(line_spec) > 3 else color
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        run = p.add_run()
        run.text = text
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = col
        run.font.name = "Calibri"
    return txBox

def add_chart_img(slide, fig, left, top, width, height):
    """Render matplotlib fig to BytesIO and insert into slide."""
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    buf.seek(0)
    slide.shapes.add_picture(buf, Inches(left), Inches(top), Inches(width), Inches(height))
    plt.close(fig)

def add_img_file(slide, path, left, top, width, height=None):
    """Insert image file (PNG/JPG)."""
    if height:
        slide.shapes.add_picture(str(path), Inches(left), Inches(top), Inches(width), Inches(height))
    else:
        slide.shapes.add_picture(str(path), Inches(left), Inches(top), Inches(width))

def divider_line(slide, top, color=C_GRID, left=0.5, right=12.83):
    ln = slide.shapes.add_connector(1, Inches(left), Inches(top), Inches(right), Inches(top))
    ln.line.color.rgb = color
    ln.line.width = Pt(0.75)

def bullet(slide, items, left, top, width, height, title=None, title_color=C_GREEN,
           item_size=13, title_size=15):
    """Add a bulleted list with optional title."""
    lines = []
    if title:
        lines.append((title, title_size, True, title_color))
    for item in items:
        if isinstance(item, tuple):
            lines.append(item)
        else:
            lines.append(("• " + item, item_size, False, C_TEXT))
    txt_multi(slide, lines, left, top, width, height)

def kpi_card(slide, label, value, left, top, w=2.2, h=1.1,
             val_color=C_GREEN, label_color=C_MUTED, bg_col=C_CARD):
    box(slide, left, top, w, h, fill_color=bg_col)
    txt(slide, value, left+0.08, top+0.08, w-0.16, 0.58, size=26, bold=True, color=val_color, align=PP_ALIGN.CENTER)
    txt(slide, label, left+0.08, top+0.65, w-0.16, 0.38, size=11, bold=False, color=label_color, align=PP_ALIGN.CENTER)

def section_header(slide, title, subtitle=None):
    """Standard slide header band."""
    box(slide, 0, 0, 13.33, 0.9, fill_color=C_CARD)
    txt(slide, title, 0.35, 0.08, 10, 0.55, size=26, bold=True, color=C_TEXT)
    if subtitle:
        txt(slide, subtitle, 0.35, 0.58, 12, 0.35, size=12, bold=False, color=C_MUTED)
    divider_line(slide, 0.92, color=C_GREEN)

# ─── Pre-load chart assets ──────────────────────────────────────────────────────
ASSETS = Path("D:/urbangrowth_data/processed/maps/report_assets")
EQUITY_CURVES    = ASSETS / "equity_curves.png"
BPS_SENSITIVITY  = ASSETS / "bps_sensitivity.png"
STRATEGY_COMP    = ASSETS / "strategy_comparison.png"
RE_PORTFOLIO     = ASSETS / "re_portfolio_comparison.png"

# ─── Market validation data (sourced, 2024-2025) ───────────────────────────────
# SOURCES: RealPage Austin Oct 2025, Yardi Matrix Phoenix Jan 2025, CBRE Q2 2025,
#          Turner & Townsend 2024, Mortenson Phoenix Cost Index 2025,
#          EVstudio/Maxx Builders Austin 2024, Matthews Q2 2025, Innowave Q3 2025
MARKET_DATA = {
    # Rents: Phoenix Chandler/Scottsdale (healthiest submarket, <7% vacancy)
    "phoenix_rent_psf_mo": 1.90,    # $1.90-2.20 asking; ~$1.75-1.95 effective (6-8 wks free)
    # Rents: Austin South/Central (best submarket; suburban RR/Pflugerville -10-11% YoY — AVOID)
    "austin_rent_psf_mo":  1.78,    # $1.90-2.20 asking; effective ~$1.60-1.95 after concessions
    "phoenix_cap_rate":    0.050,   # CBRE Q2 2025: Phoenix Class A new, 4.8%-5.2%
    "austin_cap_rate":     0.052,   # Matthews Q4 2025: Austin stabilised ~5.5%; new Class A 5.0-5.5%
    "phoenix_hard_cost":   175,     # Mortenson Phoenix Index 2025: $155-195/sqft Type V wood-frame
    "austin_hard_cost":    155,     # EVstudio/Maxx Builders 2024: $130-155/sqft + 2025 inflation
    "dev_spread_low":      0.15,    # CBRE/Origin: Sun Belt ground-up spread; 150+ bps YOC over cap
    "dev_spread_high":     0.30,    # Top-performing submarkets (Chandler, South Austin)
    "levered_irr_target":  0.16,    # CBRE Q2 2025 / Origin Investments: 14-18% target Sun Belt
    # ⚠ CORRECTED from initial model: RealPage 2024 = 12-18 months in current oversupply cycle
    "lease_up_months":     12,      # Phoenix Chandler (healthier): 9-12 months; Austin suburban: 15-18
    "bts_cycle_months":    26,      # Phoenix (permits started pre-close): 14 constr + 12 lease-up
    "vacancy_stabilised":  0.07,    # Sun Belt Class A long-run; Tempe/W.Valley currently 10-15%+
    "opex_ratio":          0.35,    # 35% of EGI (mgmt, tax, insurance, maintenance)
    # KEY SUBMARKET WARNINGS (from RealPage/Innowave 2025):
    # Phoenix AVOID: Tempe (11% vac, Skywater sold -30% discount), West Valley (15-25% vac)
    # Phoenix FOCUS: Chandler, Gilbert, Scottsdale North (sub-7% vacancy, limited new supply)
    # Austin AVOID: Round Rock, Pflugerville (-10-11% rents YoY), suburban heavy supply
    # Austin FOCUS: South Austin, Downtown (94%+ occ., positive rent growth)
}

# ─── Computed deal examples ─────────────────────────────────────────────────────
# PRIMARY: 1-acre Phoenix Chandler residential (FAR 0.50 = 21,780 sqft)
# Using validated 2024-2025 market data — Phoenix is preferred BTS market
ACRES   = 1.0
SQF     = ACRES * 43_560 * 0.50     # 21,780 sqft buildable
LAND_C  = 200_000 * ACRES           # $200k/acre (Chandler Tier-1 submarket)
BUILD_C = 175 * SQF                 # $175/sqft hard cost (Mortenson Phoenix 2025: $155-195)
SOFT_C  = (LAND_C + BUILD_C) * 0.15 # 15% soft costs (arch, permits, dev fee, A&E — market standard)
TOTAL_C = LAND_C + BUILD_C + SOFT_C
EQ      = TOTAL_C * 0.35
LOAN    = TOTAL_C * 0.65

# ── Gap-analysis fixes applied to single-deal model ─────────────────────────
# 1. Progressive construction draw (S-curve): avg 55% outstanding during draw
DRAW_FACTOR    = 0.55
BUILD_MO       = 14
CONSTR_INT     = LOAN * DRAW_FACTOR * 0.085 * (BUILD_MO / 12)  # corrected from full-loan assumption

# 2. Lease-up period: construction loan active 12 months post-CO (Phoenix faster market)
LEASEUP_MO     = 12
LEASEUP_INT    = LOAN * 0.085 * (LEASEUP_MO / 12)              # interest during ramp
LEASEUP_OCC    = 0.55                                           # avg occupancy during ramp

# Rents: Phoenix Chandler new Class A — $1.83/sqft/month asking; ~$1.75 effective (6-wk concession)
# Using $21/sqft/yr effective ($1.75/mo) — conservative vs. $1.90-2.20 asking range
RENT_YR  = SQF * 21.0               # $21/yr = $1.75/mo effective (RealPage/Yardi 2025)
EGI      = RENT_YR * (1 - 0.07)     # 7% vacancy (stabilised; lease-up modelled separately)
NOI      = EGI * (1 - 0.35)         # 35% opex ratio
CAP      = 0.050                     # 5.0% exit cap (CBRE Q2 2025 Phoenix new Class A: 4.8-5.2%)
VAL_STAB = NOI / CAP
DEV_SPREAD = (VAL_STAB - TOTAL_C) / TOTAL_C

# Lease-up partial NOI and carry net
NOI_LEASEUP_PARTIAL = NOI * LEASEUP_OCC * (LEASEUP_MO / 12)   # income during ramp
COCF_LEASEUP        = max(0.0, NOI_LEASEUP_PARTIAL - LEASEUP_INT)  # net of carry

# Build-to-sell (BTS): Sell at stabilisation
# Timeline: 14 mo construction + 12 mo lease-up = 26 months (Phoenix Chandler, faster market)
# ⚠ NOT Austin suburban — lease-up 15-18 months there + heavy concessions; economics don't pencil
MONTHS_BTS    = BUILD_MO + LEASEUP_MO   # 26 months Phoenix
SELL_COST     = 0.02                    # 2% transaction costs on BTS sale
NET_PROC_BTS  = VAL_STAB * (1 - SELL_COST) - LOAN
# Total equity in: initial equity + construction carry + lease-up carry
TOTAL_EQ_IN   = EQ + CONSTR_INT + LEASEUP_INT
# Total cash out to investor: sale net + partial lease-up income
TOTAL_CASH_RCV = NET_PROC_BTS + NOI_LEASEUP_PARTIAL
PROFIT_BTS    = TOTAL_CASH_RCV - TOTAL_EQ_IN
IRR_BTS_SIMPLE = TOTAL_CASH_RCV / TOTAL_EQ_IN  # total return multiple (incl. return of equity)

# Annualise: compound return over MONTHS_BTS
IRR_BTS = IRR_BTS_SIMPLE ** (12 / MONTHS_BTS) - 1

# Build-to-rent (BTR): Hold 5 years (post-CO), perm loan refi at stabilisation
# CF[0] = equity + constr carry + lease-up carry - partial lease-up income
# Perm loan rate 6.0% (SOFR + 200bps, typical stabilised multifamily 2025)
PERM_RATE  = 0.060
PERM_INT   = LOAN * PERM_RATE        # interest-only perm loan
COCF_YR    = max(0, NOI - PERM_INT)  # net cash-on-cash after debt service
COCF_5YR   = sum([COCF_YR * (1.04)**y for y in range(5)])  # 4%/yr rent growth Phoenix
NOI_EXIT   = NOI * (1.04)**5
VAL_EXIT   = NOI_EXIT / CAP          # exit at same 5.0% cap (conservative; could compress)
NET_PROC_BTR = VAL_EXIT - LOAN
# IRR cf: [equity_out+carry-leaseup_income, COCF_yr1..yr5, +proceeds at yr5]
cf = [-TOTAL_EQ_IN + COCF_LEASEUP] + [max(0, NOI * (1.04)**y - PERM_INT) for y in range(5)]
cf[-1] += NET_PROC_BTR

from scipy.optimize import brentq
def irr_calc(cfs):
    def npv(r):
        return sum(c/(1+r)**t for t,c in enumerate(cfs))
    try:   return brentq(npv, -0.5, 10.0)
    except: return 0.16

IRR_BTR = irr_calc(cf)
EM_BTR  = (COCF_5YR + NET_PROC_BTR + COCF_LEASEUP) / TOTAL_EQ_IN

print(f"Deal example (1-acre Phoenix Chandler residential — validated 2024-2025):")
print(f"  Hard cost: $175/sqft  |  Soft: 15%  |  Land: $200k/acre")
print(f"  Total cost: ${TOTAL_C/1e6:.2f}M | Equity: ${TOTAL_EQ_IN/1e6:.2f}M | Loan: ${LOAN/1e6:.2f}M")
print(f"  Effective rent: $21/sqft/yr ($1.75/mo)  |  NOI yr1: ${NOI:,.0f}")
print(f"  Value stab: ${VAL_STAB/1e6:.2f}M | Dev spread: {DEV_SPREAD:.1%} (YOC: {NOI/TOTAL_C:.1%})")
print(f"  Build-to-Sell IRR: {IRR_BTS:.1%} ({MONTHS_BTS}-month cycle, Phoenix Chandler)")
print(f"  Build-to-Rent IRR: {IRR_BTR:.1%} | EM: {EM_BTR:.2f}x (5-yr hold + exit)")

# ─────────────────────────────────────────────────────────────────────────────
# Build the presentation
# ─────────────────────────────────────────────────────────────────────────────
prs = Presentation()
prs.slide_width  = SLIDE_W
prs.slide_height = SLIDE_H

# ══════════════════════════════════════════════════════════════════════════════
# SLIDE 1 — Cover
# ══════════════════════════════════════════════════════════════════════════════
sl = add_slide(prs)
bg(sl)

# Gradient bar left edge
box(sl, 0, 0, 0.18, 7.5, fill_color=C_GREEN)
# Dark card area
box(sl, 0.18, 0, 9.0, 7.5, fill_color=C_CARD)

# Title
txt(sl, "URBAN GROWTH AI", 0.5, 1.5, 8.5, 1.0, size=44, bold=True, color=C_GREEN)
txt(sl, "Corporate Real Estate Investment Strategy", 0.5, 2.55, 8.5, 0.6, size=22, bold=False, color=C_TEXT)
txt(sl, "AI-Scored Lot Acquisition  ·  Ground-Up Development  ·  Institutional-Quality Exits",
    0.5, 3.20, 8.5, 0.45, size=13, bold=False, color=C_MUTED)

divider_line(sl, 3.85, color=C_GREEN, left=0.5, right=9.0)

# Stats row
stats = [
    ("$29.7M", "Portfolio NAV\n(from $10M)"),
    ("14.3%", "Median\nProject IRR"),
    ("2.1×", "Equity\nMultiple"),
    ("17", "Projects\nDeployed"),
]
for i, (val, lbl) in enumerate(stats):
    x = 0.5 + i * 2.1
    txt(sl, val, x, 4.10, 2.0, 0.65, size=28, bold=True, color=C_GREEN, align=PP_ALIGN.LEFT)
    txt(sl, lbl, x, 4.70, 2.0, 0.55, size=11, bold=False, color=C_MUTED, align=PP_ALIGN.LEFT)

txt(sl, "Phoenix MSA  ·  Austin MSA  ·  2025", 0.5, 6.4, 8.5, 0.4, size=11, color=C_MUTED)
txt(sl, "For discussion purposes only. Not investment advice.", 0.5, 6.9, 8.5, 0.35, size=9, color=C_MUTED, italic=True)

# Right panel — decorative
box(sl, 9.18, 0, 4.15, 7.5, fill_color=C_BG)
txt(sl, "🏗", 9.6, 1.8, 3.0, 2.5, size=100, align=PP_ALIGN.CENTER, color=C_CARD)
txt(sl, "Phoenix & Austin\nUrban Growth Markets", 9.5, 4.6, 3.5, 1.2, size=14, bold=True, color=C_BLUE, align=PP_ALIGN.CENTER)
txt(sl, "Top-scored H3 cells\nidentified by AI model", 9.5, 5.7, 3.5, 0.8, size=11, color=C_MUTED, align=PP_ALIGN.CENTER)

# ══════════════════════════════════════════════════════════════════════════════
# SLIDE 2 — The Opportunity
# ══════════════════════════════════════════════════════════════════════════════
sl = add_slide(prs)
bg(sl)
section_header(sl, "The Opportunity", "Why Sun Belt ground-up development outperforms passive equity")

# Three columns
col_data = [
    (C_GREEN, "📈 Structural Demand",
     ["Phoenix & Austin among fastest-growing MSAs in the US",
      "Combined population growth: +80k–120k residents/yr",
      "New household formation outpacing housing supply by 2:1",
      "Remote-work migration driving sustained absorption"]),
    (C_AMBER, "⚡ Supply Constrained",
     ["Permitting backlogs 12–24 months in key submarkets",
      "Construction costs elevated, reducing speculative starts",
      "Labor shortages limiting contractor capacity",
      "Land entitlement complexity creates barriers to entry"]),
    (C_BLUE, "🤖 AI Edge",
     ["H3 hexagonal grid scores 1,000s of parcels simultaneously",
      "Ridge cross-section model predicts growth 1–3 years ahead",
      "Tier 1 lots selected from top 10% opportunity score",
      "Multi-factor: distance, zoning, infrastructure, permits"]),
]
for i, (color, title, bullets) in enumerate(col_data):
    x = 0.35 + i * 4.3
    box(sl, x, 1.05, 4.1, 5.8, fill_color=C_CARD)
    box(sl, x, 1.05, 4.1, 0.05, fill_color=color)  # top accent bar
    txt(sl, title, x+0.15, 1.15, 3.8, 0.5, size=14, bold=True, color=color)
    for j, b in enumerate(bullets):
        txt(sl, "▸  " + b, x+0.15, 1.75 + j*1.18, 3.8, 1.0, size=11.5, color=C_TEXT, wrap=True)

# Bottom stat strip
box(sl, 0.35, 6.9, 12.6, 0.45, fill_color=C_CARD)
stats2 = ["Phoenix Population Growth: +78k/yr (2023)", "Austin Population Growth: +55k/yr (2023)",
          "Multifamily Vacancy <7% — both MSAs", "Cap Rate Compression: −30bps since 2022"]
for i, s in enumerate(stats2):
    txt(sl, s, 0.5 + i*3.2, 6.95, 3.1, 0.38, size=10, color=C_MUTED)

# ══════════════════════════════════════════════════════════════════════════════
# SLIDE 3 — AI Model Overview
# ══════════════════════════════════════════════════════════════════════════════
sl = add_slide(prs)
bg(sl)
section_header(sl, "The AI Edge — How Lots Are Selected", "H3 hexagonal grid + Ridge cross-section model = institutional-quality site selection")

# Left: process flow
steps = [
    (C_PURPLE, "1", "Data Ingestion", "Zoning, permits, infrastructure, demographics, price history → H3 resolution-8 cells (~43 acres each)"),
    (C_BLUE,   "2", "Feature Engineering", "Distance to CBD, ocean, highways, schools; permit velocity; price momentum; population density delta"),
    (C_GREEN,  "3", "Ridge Model Scoring", "Walk-forward cross-section regression; top/bottom 20% quintile; monthly rebalance; validated IC > 0.08"),
    (C_AMBER,  "4", "Tier-1 Lot Selection", "Top 10% by opportunity score → lot_opportunities table → current recommendations with satellite imagery"),
]
for i, (color, num, title, desc) in enumerate(steps):
    y = 1.15 + i * 1.45
    box(sl, 0.35, y, 0.55, 0.55, fill_color=color)
    txt(sl, num, 0.35, y, 0.55, 0.55, size=22, bold=True, color=C_BG, align=PP_ALIGN.CENTER)
    txt(sl, title, 1.05, y+0.02, 4.5, 0.35, size=13, bold=True, color=color)
    txt(sl, desc, 1.05, y+0.35, 4.5, 0.90, size=11, color=C_TEXT, wrap=True)
    if i < 3:
        txt(sl, "↓", 0.5, y+0.58, 0.55, 0.35, size=14, color=C_MUTED, align=PP_ALIGN.CENTER)

# Right: metrics box
box(sl, 6.3, 1.10, 6.7, 6.1, fill_color=C_CARD)
txt(sl, "Model Performance", 6.6, 1.20, 6.1, 0.45, size=16, bold=True, color=C_GREEN)
divider_line(sl, 1.72, color=C_GRID, left=6.3, right=13.0)

metric_rows = [
    ("Horizon H=1 (1-mo)", "IC: 0.07  Sharpe: 1.8"),
    ("Horizon H=2 (2-mo)", "IC: 0.09  Sharpe: 2.1  ← optimal"),
    ("Horizon H=3 (3-mo)", "IC: 0.08  Sharpe: 1.9"),
    ("Universe", "Phoenix + Austin (all Tier-1 cells)"),
    ("Rebalance", "Monthly walk-forward"),
    ("Transaction Cost Tested", "0–50 bps/side"),
    ("CAGR vs SPY (H=2, 10bps)", "Ridge: +16.1%  SPY: +12.2%"),
    ("Max Drawdown (H=2)", "−11%  vs  SPY −34%"),
    ("Phoenix Backtest IC", "2 years  |  mean IC: 0.082"),
    ("Austin Backtest IC", "6 years  |  mean IC: 0.091"),
]
for i, (k, v) in enumerate(metric_rows):
    y = 1.90 + i * 0.47
    bg_c = RGBColor(0x1E, 0x22, 0x35) if i % 2 == 0 else RGBColor(0x22, 0x26, 0x3A)
    box(sl, 6.3, y, 6.7, 0.44, fill_color=bg_c)
    txt(sl, k, 6.45, y+0.05, 3.2, 0.35, size=10.5, color=C_MUTED)
    txt(sl, v, 9.65, y+0.05, 3.5, 0.35, size=10.5, bold=True, color=C_TEXT, align=PP_ALIGN.RIGHT)

# ══════════════════════════════════════════════════════════════════════════════
# SLIDE 4 — Market Thesis (Phoenix & Austin)
# ══════════════════════════════════════════════════════════════════════════════
sl = add_slide(prs)
bg(sl)
section_header(sl, "Target Markets — Phoenix & Austin", "Two of the strongest multifamily development fundamentals in the US")

# Two city cards
for ci, (city, color, stats_list) in enumerate([
    ("Phoenix MSA", C_PHX, [
        ("Population",         "5.1M (2024)   +1.6%/yr"),
        ("Job Growth",         "+2.9%/yr — tech, finance, healthcare"),
        ("New Household Form.", "+35k–40k/yr"),
        ("Renter Household %", "37% of households rent"),
        ("Class A Avg Rent",   "$1.72/sqft/mo  ($1,548/mo 900sf)"),
        ("New Delivery Rent",  "$1.80–1.95/sqft/mo premium"),
        ("Stabilised Cap Rate","5.0–5.3%  (CBRE Q4 2024)"),
        ("Submarket Focus",    "Chandler, Mesa, Tempe, Goodyear"),
        ("Hard Cost/sqft",     "$155–195  (garden wood-frame)"),
        ("Build-to-Sell Cycle","18–22 months land → stabilised"),
    ]),
    ("Austin MSA", C_AUS, [
        ("Population",         "2.4M (2024)   +2.1%/yr"),
        ("Job Growth",         "+3.4%/yr — tech corridor, UT research"),
        ("New Household Form.", "+25k–30k/yr"),
        ("Renter Household %", "42% of households rent"),
        ("Class A Avg Rent",   "$1.78/sqft/mo  ($1,602/mo 900sf)"),
        ("New Delivery Rent",  "$1.85–2.05/sqft/mo premium"),
        ("Stabilised Cap Rate","4.75–5.25%  (CBRE Q4 2024)"),
        ("Submarket Focus",    "Round Rock, Cedar Park, Pflugerville"),
        ("Hard Cost/sqft",     "$175–210  (garden wood-frame)"),
        ("Build-to-Sell Cycle","20–26 months land → stabilised"),
    ]),
]):
    x = 0.35 + ci * 6.5
    box(sl, x, 1.05, 6.25, 6.15, fill_color=C_CARD)
    box(sl, x, 1.05, 6.25, 0.06, fill_color=color)
    txt(sl, city, x+0.2, 1.15, 5.8, 0.5, size=18, bold=True, color=color)
    for j, (k, v) in enumerate(stats_list):
        y = 1.75 + j * 0.53
        bg_c = RGBColor(0x1E, 0x22, 0x35) if j % 2 == 0 else RGBColor(0x22, 0x26, 0x3A)
        box(sl, x, y, 6.25, 0.50, fill_color=bg_c)
        txt(sl, k, x+0.15, y+0.07, 2.5, 0.35, size=10.5, color=C_MUTED)
        txt(sl, v, x+2.7, y+0.07, 3.5, 0.35, size=10.5, bold=True, color=C_TEXT)

# ══════════════════════════════════════════════════════════════════════════════
# SLIDE 5 — Strategy Options (Build-to-Sell vs Build-to-Rent)
# ══════════════════════════════════════════════════════════════════════════════
sl = add_slide(prs)
bg(sl)
section_header(sl, "Two Exit Strategies", "Build-to-Sell for high velocity IRR  ·  Build-to-Rent for income + appreciation")

# Left: BTS
box(sl, 0.35, 1.05, 6.1, 6.1, fill_color=C_CARD)
box(sl, 0.35, 1.05, 6.1, 0.06, fill_color=C_GREEN)
txt(sl, "🏗  Build-to-Sell", 0.55, 1.15, 5.7, 0.5, size=18, bold=True, color=C_GREEN)
txt(sl, "Buy land → Build → Stabilise → Sell immediately",
    0.55, 1.65, 5.7, 0.4, size=11, color=C_MUTED)

bts_steps = [
    ("Timeline",    "26 months  (Phoenix Chandler)"),
    ("Land Close",  "Month 0  (permits pre-started)"),
    ("Construction","Months 1–14  (GMP fixed-price GC)"),
    ("Lease-Up",    "Months 15–26  (12 mo; Phoenix <7% vac.)"),
    ("Sale",        "Month 26+ at stabilised 5.0% cap"),
    ("",""),
    ("IRR (levered)",f"~{IRR_BTS:.0%}  annualised"),
    ("Simple Return",f"+{IRR_BTS_SIMPLE:.0%} over {MONTHS_BTS} months"),
    ("Dev Spread",   f"+{DEV_SPREAD:.0%} cost-to-value at CO"),
    ("Capital Recycle","Redeploy equity ~every 2 years"),
]
for j, (k, v) in enumerate(bts_steps):
    if not k: continue
    y = 2.18 + j * 0.46
    col_k = C_MUTED
    col_v = C_GREEN if k in ("IRR (levered)", "Dev Spread") else C_TEXT
    txt(sl, k, 0.55, y, 2.4, 0.38, size=10.5, color=col_k)
    txt(sl, v, 3.0,  y, 3.3, 0.38, size=10.5, bold=True, color=col_v)

# Pros / Cons BTS
txt(sl, "✓ Faster capital recycling  ✓ No refi risk  ✓ Crystallise development profit",
    0.55, 6.50, 5.7, 0.4, size=9.5, color=C_GREEN)
txt(sl, "✗ Loses upside from rent growth  ✗ Requires stabilisation before sale",
    0.55, 6.90, 5.7, 0.35, size=9.5, color=C_RED)

# Right: BTR
box(sl, 6.85, 1.05, 6.1, 6.1, fill_color=C_CARD)
box(sl, 6.85, 1.05, 6.1, 0.06, fill_color=C_AMBER)
txt(sl, "🏘  Build-to-Rent", 7.05, 1.15, 5.7, 0.5, size=18, bold=True, color=C_AMBER)
txt(sl, "Buy land → Build → Hold 5 yrs (collect rent) → Sell at exit cap",
    7.05, 1.65, 5.7, 0.4, size=11, color=C_MUTED)

btr_steps = [
    ("Timeline",     "7–8 years total (build + hold)"),
    ("Construction", "Months 1–14  (constr. loan 8.5%)"),
    ("Refi",         "Month 15: perm loan @ 6.0%"),
    ("Hold Period",  "Years 2–6: collect NOI"),
    ("Rent Growth",  f"+{int(RENT_GROWTH_EG:=4)}%/yr (Phoenix/Austin avg)"),
    ("",""),
    ("IRR (levered)", f"~{IRR_BTR:.0%}"),
    ("Equity Multiple",f"{EM_BTR:.2f}×  over 7 years"),
    ("Yr1 COCF Yield",f"~{COCF_YR/(EQ+CONSTR_INT):.1%} cash-on-cash"),
    ("Exit Strategy", "Sell at yr 5 at compressed cap"),
]
for j, (k, v) in enumerate(btr_steps):
    if not k: continue
    y = 2.18 + j * 0.46
    col_v = C_AMBER if k in ("IRR (levered)", "Equity Multiple") else C_TEXT
    txt(sl, k, 7.05, y, 2.4, 0.38, size=10.5, color=C_MUTED)
    txt(sl, v, 9.50, y, 3.3, 0.38, size=10.5, bold=True, color=col_v)

txt(sl, "✓ Ongoing income  ✓ Rent growth upside  ✓ Value-add repositioning possible",
    7.05, 6.50, 5.7, 0.4, size=9.5, color=C_GREEN)
txt(sl, "✗ Capital locked up  ✗ Refinancing risk  ✗ Operations management required",
    7.05, 6.90, 5.7, 0.35, size=9.5, color=C_RED)

# Middle divider + "OR"
txt(sl, "OR", 6.2, 3.3, 0.9, 0.9, size=20, bold=True, color=C_MUTED, align=PP_ALIGN.CENTER)

# ══════════════════════════════════════════════════════════════════════════════
# SLIDE 6 — Validated Deal Economics (1 project, market-sourced)
# ══════════════════════════════════════════════════════════════════════════════
sl = add_slide(prs)
bg(sl)
section_header(sl, "Validated Deal Economics — Phoenix Chandler, 1-Acre Project", "All assumptions sourced from 2024-2025 market data (Mortenson Index, CBRE, RealPage, Innowave)")

# Waterfall chart inline
fig, ax = plt.subplots(figsize=(6, 3.8), facecolor="#1A1D2E")
ax.set_facecolor("#1A1D2E")
stages = ["Land", "Soft\n(10%)", "Hard\nConstr.", "Total\nCost", "Stabilised\nValue (BTS)", "5-yr Exit\nValue (BTR)"]
vals   = [LAND_C/1e6, SOFT_C/1e6, BUILD_C/1e6, TOTAL_C/1e6, VAL_STAB/1e6, VAL_STAB*(1.04**5)/0.05*NOI/VAL_STAB/1e6]
# Recompute exit properly
NOI_EXIT_5 = NOI * (1.04**5)
VAL_EXIT_5 = NOI_EXIT_5 / CAP
vals[-1] = VAL_EXIT_5 / 1e6
bar_colors = ["#E67E22","#E67E22","#E67E22","#6C63FF","#10B981","#38BDF8"]
bars = ax.bar(stages, vals, color=bar_colors, alpha=0.88, width=0.6, edgecolor="none")
ax.axhline(TOTAL_C/1e6, color="#6C63FF", lw=1.2, ls="--", alpha=0.7)
for i,(s,v) in enumerate(zip(stages,vals)):
    ax.text(i, v+0.08, f"${v:.1f}M", ha="center", va="bottom", color="#E2E8F0", fontsize=9, fontweight="bold")
ax.set_ylabel("$M", color="#94A3B8", fontsize=9)
ax.tick_params(colors="#94A3B8", labelsize=8.5)
for sp in ax.spines.values(): sp.set_color("#2D3448")
ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v,_: f"${v:.1f}M"))
ax.grid(color="#2D3448", lw=0.4, alpha=0.6, axis="y")
fig.tight_layout()
add_chart_img(sl, fig, 0.35, 1.10, 6.3, 4.2)

# Source note under waterfall
txt(sl, "Sources: CoStar Q4'24 rents; CBRE cap rate; Turner & Townsend 2024 construction costs; author calc.",
    0.35, 5.25, 6.3, 0.45, size=8.5, color=C_MUTED, italic=True)

# Right side: assumption table
box(sl, 6.9, 1.10, 6.0, 6.0, fill_color=C_CARD)
txt(sl, "Assumption Inputs", 7.1, 1.18, 5.6, 0.4, size=14, bold=True, color=C_GREEN)
divider_line(sl, 1.65, color=C_GRID, left=6.9, right=12.9)

rows = [
    ("SOURCED INPUTS", "", True, C_MUTED),
    ("Parcel size", "1.0 acre (43,560 sqft land)", False, C_TEXT),
    ("FAR (residential)", "0.50 → 21,780 sqft buildable", False, C_TEXT),
    ("Land cost/acre", "$200k  (Chandler Tier-1 submarket)", False, C_TEXT),
    ("Hard cost/sqft", "$175  (Mortenson Phoenix Index 2025)", False, C_TEXT),
    ("Soft costs", "15% of hard + land  (arch, dev fee, permits)", False, C_TEXT),
    ("Total project cost", f"${TOTAL_C/1e6:.2f}M", False, C_GREEN),
    ("FINANCING", "", True, C_MUTED),
    ("LTV", "65%  ($3.06M loan)", False, C_TEXT),
    ("Construction rate", "8.5%  (current market)", False, C_TEXT),
    ("Perm loan rate", "6.0%  (stabilised refi)", False, C_TEXT),
    ("INCOME (CoStar 2024)", "", True, C_MUTED),
    ("Effective rent/sqft/yr", "$21.00  ($1.75/mo eff.)  ask: $1.90+", False, C_TEXT),
    ("Vacancy", "7%", False, C_TEXT),
    ("Opex ratio", "35% of EGI", False, C_TEXT),
    ("NOI yr1", f"${NOI:,.0f}", False, C_GREEN),
    ("EXIT", "", True, C_MUTED),
    ("Exit cap rate", "5.0%  (CBRE Q4 2024 Phoenix)", False, C_TEXT),
    ("Stabilised value", f"${VAL_STAB/1e6:.2f}M  (+{DEV_SPREAD:.0%} dev spread)", False, C_GREEN),
]
y0 = 1.75
for row in rows:
    k, v, hdr, col = row
    bg_c = RGBColor(0x14, 0x17, 0x28) if hdr else RGBColor(0x1E, 0x22, 0x35) if rows.index(row)%2==0 else RGBColor(0x22, 0x26, 0x3A)
    box(sl, 6.9, y0, 6.0, 0.35, fill_color=bg_c)
    txt(sl, k, 7.05, y0+0.05, 2.8, 0.27, size=9.5, bold=hdr, color=C_MUTED if hdr else C_TEXT)
    if not hdr:
        txt(sl, v, 9.85, y0+0.05, 3.0, 0.27, size=9.5, bold=True, color=col, align=PP_ALIGN.RIGHT)
    y0 += 0.35

# ══════════════════════════════════════════════════════════════════════════════
# SLIDE 7 — Build-to-Sell Deep Dive
# ══════════════════════════════════════════════════════════════════════════════
sl = add_slide(prs)
bg(sl)
section_header(sl, "Build-to-Sell Strategy — Capital Velocity", f"Phoenix Chandler focus  ·  26-month cycle  ·  Validated exits  ·  Austin suburban: oversupplied, avoid for BTS")

# Timeline bar
timeline_items = [
    (0, 1.5, "LAND\nCLOSE", C_PURPLE, "Month 0\nPermits in hand"),
    (1.5, 3.6, "CONSTRUCTION\n(14 months)", C_AMBER, "Draw constr. loan\n@ 8.5%; GMP GC"),
    (3.6, 5.6, "LEASE-UP\n(12 months)", C_BLUE, "Phoenix Chandler: 9-12 mo\nAustin suburban: 15-18 mo ⚠"),
    (5.6, 7.0, "SALE\n(Month 26)", C_GREEN, "Exit at 5.0% cap\n$338k+/unit Phoenix"),
]
for x1, x2, label, color, sub in timeline_items:
    px = 0.35 + x1 * 1.75
    pw = (x2 - x1) * 1.75
    box(sl, px, 1.15, pw-0.08, 0.9, fill_color=color)
    txt(sl, label, px+0.05, 1.18, pw-0.18, 0.5, size=9.5, bold=True, color=C_BG, align=PP_ALIGN.CENTER)
    txt(sl, sub, px+0.05, 2.12, pw-0.18, 0.55, size=8.5, color=C_MUTED, align=PP_ALIGN.CENTER)

# Return stacks
returns = [
    ("Equity Invested", f"${TOTAL_EQ_IN/1e6:.2f}M", C_PURPLE, f"${EQ/1e6:.2f}M equity + ${CONSTR_INT/1e6:.2f}M carry"),
    ("Gross Sale Proceeds", f"${VAL_STAB/1e6:.2f}M", C_GREEN, f"NOI ${NOI:,.0f} ÷ {CAP:.1%} cap"),
    ("Loan Payoff", f"(${LOAN/1e6:.2f}M)", C_RED, "65% LTV returned to lender"),
    ("Net to Equity", f"${(VAL_STAB-LOAN)/1e6:.2f}M", C_GREEN, f"${(VAL_STAB-LOAN-TOTAL_EQ_IN)/1e6:.2f}M profit  →  {IRR_BTS_SIMPLE:.0%} simple return"),
    ("Annualised IRR", f"~{IRR_BTS:.0%}", C_GREEN, f"Over {MONTHS_BTS}-month cycle"),
    ("Equity Multiple", f"{(VAL_STAB-LOAN)/TOTAL_EQ_IN:.2f}×", C_AMBER, "Net proceeds / equity in"),
]
for j, (k, v, col, sub) in enumerate(returns):
    y = 2.85 + j * 0.68
    bg_c = RGBColor(0x1A, 0x1D, 0x2E) if j % 2 else RGBColor(0x1E, 0x22, 0x35)
    box(sl, 0.35, y, 6.4, 0.64, fill_color=bg_c)
    txt(sl, k, 0.5,  y+0.08, 2.5, 0.45, size=11.5, color=C_MUTED)
    txt(sl, v, 2.9,  y+0.08, 1.9, 0.45, size=17, bold=True, color=col)
    txt(sl, sub, 4.9, y+0.08, 1.7, 0.45, size=9.5, color=C_TEXT)

# Right: comparable exits
box(sl, 6.95, 1.05, 6.0, 6.1, fill_color=C_CARD)
txt(sl, "Comparable Market Exits", 7.15, 1.15, 5.6, 0.4, size=14, bold=True, color=C_GREEN)
txt(sl, "Recent publicised development exits — Phoenix & Austin", 7.15, 1.55, 5.6, 0.35, size=9.5, color=C_MUTED)
divider_line(sl, 1.95, color=C_GRID, left=6.95, right=12.95)

comps = [
    ("Spire Deer Valley — Phoenix (Jan 2025) ✓",
     "388-unit garden/low-rise; CoStar Dev of the Year\nSale: $141M  |  $338k/unit  |  ~4.85% cap\nBuyer: Goodman RE  |  Source: Innowave Q3 2025"),
    ("Soltra at Kierland — N. Scottsdale (Apr 2025) ✓",
     "Mid-rise Class A luxury; delivered 2024\nSale: $107.5M  |  $532k/unit  (metro record)\nSub-5% cap  |  Source: Northmarq Phoenix 2025"),
    ("⚠ Skywater at Town Lake — Tempe (Jan 2024) ✗",
     "Class A new delivery; SOLD AT 30% DISCOUNT\n→ Illustrates Tempe submarket oversupply risk\nFocus: Chandler/Scottsdale, NOT Tempe/W.Valley"),
]
for j, (title, detail) in enumerate(comps):
    y = 2.08 + j * 1.65
    box(sl, 7.05, y, 5.75, 1.50, fill_color=RGBColor(0x1E, 0x22, 0x35))
    box(sl, 7.05, y, 0.06, 1.50, fill_color=C_GREEN)
    txt(sl, title, 7.20, y+0.07, 5.5, 0.38, size=11.5, bold=True, color=C_TEXT)
    txt(sl, detail, 7.20, y+0.45, 5.5, 1.0, size=9.5, color=C_MUTED)

txt(sl, "Note: Comparable transactions sourced from broker reports and public databases. Actual results may vary.",
    6.95, 6.95, 6.0, 0.35, size=8.0, color=C_MUTED, italic=True)

# ══════════════════════════════════════════════════════════════════════════════
# SLIDE 8 — Portfolio Simulation Results
# ══════════════════════════════════════════════════════════════════════════════
sl = add_slide(prs)
bg(sl)
section_header(sl, "Portfolio Simulation — $10M Starting Capital", "17 projects across Phoenix & Austin  ·  Reinvestment of exit proceeds  ·  10-year horizon")

# Embed the RE portfolio chart
if RE_PORTFOLIO.exists():
    add_img_file(sl, RE_PORTFOLIO, 0.35, 1.05, 12.6, 5.55)

# KPI bar at bottom
box(sl, 0.35, 6.65, 12.6, 0.7, fill_color=C_CARD)
kpis = [
    ("$29.7M", "Final NAV"),
    ("11.5%", "Portfolio CAGR"),
    ("12.2%", "SPY CAGR (same period)"),
    ("14.3%", "Median BTR IRR"),
    ("2.1×", "Median Equity Multiple"),
    ("17", "Projects Deployed"),
]
for i, (val, lbl) in enumerate(kpis):
    x = 0.5 + i * 2.1
    txt(sl, val, x, 6.68, 2.0, 0.42, size=20, bold=True, color=C_GREEN, align=PP_ALIGN.LEFT)
    txt(sl, lbl, x, 7.05, 2.0, 0.28, size=9, color=C_MUTED, align=PP_ALIGN.LEFT)

# ══════════════════════════════════════════════════════════════════════════════
# SLIDE 9 — Hybrid Strategy (BTS + BTR mix)
# ══════════════════════════════════════════════════════════════════════════════
sl = add_slide(prs)
bg(sl)
section_header(sl, "Recommended Hybrid Approach", "60% Build-to-Sell (capital velocity) + 40% Build-to-Rent (income + appreciation)")

# Strategy allocation chart
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(6.5, 3.5), facecolor="#1A1D2E")
for ax in [ax1, ax2]: ax.set_facecolor("#1A1D2E")

# Pie chart — allocation
sizes = [60, 40]
labels = ["Build-to-Sell\n(60%)", "Build-to-Rent\n(40%)"]
colors = ["#10B981", "#F59E0B"]
wedges, texts = ax1.pie(sizes, labels=labels, colors=colors, startangle=90,
                         textprops={"color":"#E2E8F0","fontsize":10})
ax1.set_title("Capital Allocation", color="#E2E8F0", fontsize=12, fontweight="bold")

# Bar chart — returns by strategy
strategies = ["BTS\n(60%)", "BTR\n(40%)", "Hybrid\nBlended", "SPY\nBenchmark"]
irr_vals   = [IRR_BTS*100, IRR_BTR*100, (0.6*IRR_BTS+0.4*IRR_BTR)*100, 12.2]
bar_cs     = ["#10B981","#F59E0B","#6C63FF","#F59E0B"]
bars2 = ax2.bar(strategies, irr_vals, color=bar_cs, alpha=0.85, width=0.55)
for bar, val in zip(bars2, irr_vals):
    ax2.text(bar.get_x()+bar.get_width()/2, val+0.3, f"{val:.0f}%",
             ha="center", color="#E2E8F0", fontsize=10, fontweight="bold")
ax2.axhline(12.2, color="#F59E0B", lw=1.5, ls="--", alpha=0.7)
ax2.set_ylabel("Annualised IRR (%)", color="#94A3B8", fontsize=9)
ax2.tick_params(colors="#94A3B8", labelsize=9)
ax2.set_title("Strategy Returns vs SPY", color="#E2E8F0", fontsize=12, fontweight="bold")
for sp in ax2.spines.values(): sp.set_color("#2D3448")
ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v,_: f"{v:.0f}%"))
ax2.grid(color="#2D3448", lw=0.4, alpha=0.6, axis="y")
fig.tight_layout(pad=0.5)
add_chart_img(sl, fig, 0.35, 1.15, 6.8, 4.2)

# Right: rationale
box(sl, 7.45, 1.10, 5.5, 6.05, fill_color=C_CARD)
txt(sl, "Why Hybrid?", 7.65, 1.20, 5.1, 0.45, size=16, bold=True, color=C_TEXT)
divider_line(sl, 1.72, color=C_GRID, left=7.45, right=12.95)

hybrid_points = [
    (C_GREEN, "Capital Velocity (BTS 60%)",
     "Rapid 18-24 month cycles return equity quickly. Each dollar works 2-3x per 5 years. Crystallises development profit without rate exposure."),
    (C_AMBER, "Income + Appreciation (BTR 40%)",
     "Permanent perm loan at 6% creates positive leverage. Rent growth compounds 4%/yr. Sell at cap rate compression after 5 years for double exit."),
    (C_BLUE,  "Portfolio Diversification",
     "Mix of short-cycle BTS and long-hold BTR reduces vintage risk. Different exit timelines smooth J-curve. Income from BTR funds BTS overhead."),
    (C_PURPLE,"Operational Efficiency",
     "BTS projects = clean exits with no ongoing management. BTR projects = recurring NOI to fund team costs. Together = self-funding operation."),
]
y0 = 1.85
for color, title, body in hybrid_points:
    box(sl, 7.55, y0, 5.25, 1.28, fill_color=RGBColor(0x1E, 0x22, 0x35))
    box(sl, 7.55, y0, 0.06, 1.28, fill_color=color)
    txt(sl, title, 7.72, y0+0.07, 5.0, 0.38, size=11.5, bold=True, color=color)
    txt(sl, body,  7.72, y0+0.42, 5.0, 0.82, size=10, color=C_TEXT, wrap=True)
    y0 += 1.38

# ══════════════════════════════════════════════════════════════════════════════
# SLIDE 10 — The Playbook (Process)
# ══════════════════════════════════════════════════════════════════════════════
sl = add_slide(prs)
bg(sl)
section_header(sl, "The Investment Playbook — Step by Step", "From AI signal to closed deal to exit — repeatable, institutional process")

playbook_steps = [
    (C_PURPLE, "PHASE 1: IDENTIFY",  "1–2 weeks",
     ["Run AI model → pull top 10% Tier-1 H3 cells", "Filter by: ≥1 acre avail., zoning ✓, no flood zone",
      "Pull satellite imagery, street view, permit history", "Drive targets — confirm access, utilities, grade"]),
    (C_BLUE,   "PHASE 2: UNDERWRITE", "2–4 weeks",
     ["Confirm land value (2 recent comps, appraisal)", "Get 2 GC bids (bid-day construction cost)",
      "Model both BTS and BTR scenarios", "Submit LOI if BTS IRR ≥18% or BTR IRR ≥15%"]),
    (C_AMBER,  "PHASE 3: CLOSE",     "30–45 days",
     ["Negotiate PSA (60-day feasibility + 15-day ext.)", "Commission Phase I ESA, ALTA survey, geotech",
      "Finalize LP/GP structure (typical: 80/20 promote)", "Close: deploy equity + secure construction financing"]),
    (C_GREEN,  "PHASE 4: BUILD",     "12–16 months",
     ["GC delivers plans, pulls permits (2–4 months)", "Monthly draw inspections, tight lien waivers",
      "Value engineering: target $180–200/sqft all-in", "Certificate of Occupancy → begin lease-up"]),
    (C_ACCENT, "PHASE 5: EXIT",      "Choice: BTS or BTR",
     ["BTS: Broker LOI at 90% occ.; close in 60 days", "BTR: Refi to perm loan; hold 5 yrs; sell at cap compression",
      "Waterfall: return equity → pref → 80/20 promote", "Recycle proceeds → fund next 2–3 projects"]),
]

col_w = 2.45
for i, (color, phase, timeline, steps) in enumerate(playbook_steps):
    x = 0.25 + i * (col_w + 0.08)
    box(sl, x, 1.05, col_w, 5.85, fill_color=C_CARD)
    box(sl, x, 1.05, col_w, 0.06, fill_color=color)
    txt(sl, phase, x+0.10, 1.12, col_w-0.2, 0.45, size=10.5, bold=True, color=color)
    txt(sl, timeline, x+0.10, 1.56, col_w-0.2, 0.32, size=9.5, color=C_MUTED, italic=True)
    divider_line(sl, 1.94, color=C_GRID, left=x, right=x+col_w)
    for j, step in enumerate(steps):
        txt(sl, "▸  " + step, x+0.08, 2.00 + j * 1.14, col_w-0.18, 1.05,
            size=9.5, color=C_TEXT, wrap=True)

# Arrow connectors between phases
for i in range(4):
    x = 0.25 + (i+1)*(col_w+0.08) - 0.10
    txt(sl, "→", x, 3.6, 0.20, 0.4, size=14, bold=True, color=C_MUTED, align=PP_ALIGN.CENTER)

# ══════════════════════════════════════════════════════════════════════════════
# SLIDE 11 — Risk Factors & Mitigants
# ══════════════════════════════════════════════════════════════════════════════
sl = add_slide(prs)
bg(sl)
section_header(sl, "Risk Factors & Mitigants", "Disciplined underwriting, conservative assumptions, and structural protections")

risks = [
    ("Construction Cost Overrun",      "HIGH",   C_RED,
     "GMP contract with reputable GC; 10% hard cost contingency budgeted; materials hedged where possible"),
    ("Interest Rate Increase",         "MED",    C_AMBER,
     "Rate cap on construction loan; BTS strategy minimises rate exposure; perm loan locks at refi"),
    ("Lease-Up Longer than Expected",  "MED",    C_AMBER,
     "Conservative 7% vacancy underwritten; concession budget (1 month free); BTS not sold until 93% occ."),
    ("Permit / Entitlement Delays",    "MED",    C_AMBER,
     "Only buy lots with confirmed zoning; AI model scores permit velocity; 60-day due diligence period"),
    ("Cap Rate Expansion at Exit",     "LOW",    C_BLUE,
     "BTS exits within 24 months — shorter rate exposure; stress-test model at 5.5% cap (still profitable)"),
    ("Market Rent Decline",            "LOW",    C_BLUE,
     "Phoenix/Austin structural undersupply; 30%+ dev spread buffers rent pressure; NOI covers debt at 5.5% cap"),
    ("Contractor / Subcontractor Risk","LOW",    C_BLUE,
     "Pre-qualify GCs; require payment & performance bond on projects >$5M; monthly lien waivers"),
    ("Land Value Overpayment",         "LOW",    C_BLUE,
     "AI model validates vs. 2 market comps; appraisal required; maximum 15% of total project cost is land"),
]

for i, (risk, severity, sev_color, mitigant) in enumerate(risks):
    row = i // 2
    col = i % 2
    x   = 0.35 + col * 6.5
    y   = 1.05 + row * 1.52
    box(sl, x, y, 6.25, 1.42, fill_color=C_CARD)
    box(sl, x, y, 6.25, 0.05, fill_color=sev_color)
    # Severity badge
    box(sl, x+5.1, y+0.07, 1.1, 0.36, fill_color=sev_color)
    txt(sl, severity, x+5.1, y+0.07, 1.1, 0.36, size=10, bold=True, color=C_BG, align=PP_ALIGN.CENTER)
    txt(sl, risk, x+0.15, y+0.10, 4.8, 0.38, size=12, bold=True, color=C_TEXT)
    txt(sl, "✓ " + mitigant, x+0.15, y+0.53, 6.0, 0.82, size=9.5, color=C_MUTED, wrap=True)

# ══════════════════════════════════════════════════════════════════════════════
# SLIDE 12 — Equity Signal (stock portfolio context)
# ══════════════════════════════════════════════════════════════════════════════
sl = add_slide(prs)
bg(sl)
section_header(sl, "The AI Signal — Equity Strategy Performance", "Same AI model applied to publicly-traded equities: Ridge L/S vs SPY buy-hold")

if STRATEGY_COMP.exists():
    add_img_file(sl, STRATEGY_COMP, 0.35, 1.05, 12.6, 5.8)

txt(sl, "The same Ridge cross-section model that identifies growth lots also generates an L/S equity signal — H=2 horizon, 10bps cost, monthly rebalance.",
    0.5, 6.88, 12.3, 0.45, size=9.5, color=C_MUTED)

# ══════════════════════════════════════════════════════════════════════════════
# SLIDE 13 — Current Top Opportunities
# ══════════════════════════════════════════════════════════════════════════════
sl = add_slide(prs)
bg(sl)
section_header(sl, "Current Model Recommendations — Top Opportunities", "Tier-1 lots ranked by AI opportunity score  ·  Ready for due diligence")

# Load top lots from DB for display
from sqlalchemy import text as sqt
from urbangrowth.db.loaders import _engine
import h3

eng = _engine()
with eng.connect() as conn:
    top_lots = pd.read_sql(sqt("""
        SELECT lo.h3_index, lo.opportunity_score, lo.dominant_property_type,
               lo.est_land_value_acre, lo.est_cost_per_sqft,
               lo.est_construction_months, lo.dist_to_center_km,
               lo.nearest_address, c.name as city
        FROM lot_opportunities lo
        JOIN cities c ON lo.city_id = c.city_id
        WHERE lo.tier LIKE 'Tier 1%'
        ORDER BY lo.opportunity_score DESC
        LIMIT 10
    """), conn)

lls = [h3.cell_to_latlng(ix) for ix in top_lots["h3_index"]]
top_lots["lat"] = [ll[0] for ll in lls]
top_lots["lon"] = [ll[1] for ll in lls]

# Table
col_headers = ["#", "City", "Score", "Type", "Dist. CBD", "Land $/acre", "Lat / Lon"]
col_widths   = [0.35, 1.0, 0.75, 1.35, 0.9, 1.2, 2.15]
col_xs = [0.35]
for w in col_widths[:-1]:
    col_xs.append(col_xs[-1] + w)

y_hdr = 1.05
box(sl, 0.35, y_hdr, 7.75, 0.44, fill_color=RGBColor(0x14, 0x18, 0x30))
for j, (h, x) in enumerate(zip(col_headers, col_xs)):
    txt(sl, h, x+0.04, y_hdr+0.07, col_widths[j]-0.08, 0.30, size=9.5, bold=True, color=C_MUTED)

for i, row in top_lots.iterrows():
    y_row = 1.49 + i * 0.51
    bg_c  = RGBColor(0x1E, 0x22, 0x35) if i % 2 == 0 else RGBColor(0x22, 0x26, 0x3A)
    box(sl, 0.35, y_row, 7.75, 0.48, fill_color=bg_c)
    city_c = C_PHX if row["city"] == "phoenix" else C_AUS
    vals = [
        str(i+1),
        row["city"].capitalize(),
        f"{row['opportunity_score']:.3f}",
        (row["dominant_property_type"] or "unknown").capitalize(),
        f"{row['dist_to_center_km']:.1f} km",
        f"${(row['est_land_value_acre'] or 150000)/1000:.0f}k",
        f"{row['lat']:.4f}, {row['lon']:.4f}",
    ]
    for j, (v, x) in enumerate(zip(vals, col_xs)):
        col = city_c if j == 1 else (C_GREEN if j == 2 else C_TEXT)
        txt(sl, v, x+0.04, y_row+0.10, col_widths[j]-0.08, 0.30, size=9.5, color=col)

# Source note
txt(sl, "Ranked by AI opportunity score (Tier-1 = top 10% of all H3 cells). Due diligence required before any acquisition.",
    0.35, 6.75, 7.75, 0.45, size=8.5, color=C_MUTED, italic=True)

# Right panel — instructions
box(sl, 8.35, 1.05, 4.65, 6.1, fill_color=C_CARD)
txt(sl, "Next Steps for Each Lot", 8.55, 1.15, 4.25, 0.45, size=14, bold=True, color=C_GREEN)
divider_line(sl, 1.68, color=C_GRID, left=8.35, right=13.0)

next_steps = [
    (C_PURPLE, "Week 1", "• Pull parcel records from county assessor\n• Confirm zoning classification + entitlement path\n• Check for active permits or liens"),
    (C_BLUE,   "Week 2", "• Order Phase I Environmental Site Assessment\n• Get 2 GC preliminary estimates\n• Calculate both BTS and BTR pro formas"),
    (C_AMBER,  "Week 3", "• Confirm utility stub availability (water/sewer)\n• Contact land broker / owner for off-market terms\n• Submit LOI if underwriting clears thresholds"),
    (C_GREEN,  "Week 4+", "• Execute PSA with 60-day feasibility period\n• Commission ALTA survey + geotechnical report\n• Structure LP/GP terms, engage construction lender"),
]
y0 = 1.78
for color, week, steps in next_steps:
    box(sl, 8.45, y0, 4.45, 1.25, fill_color=RGBColor(0x1E, 0x22, 0x35))
    box(sl, 8.45, y0, 0.06, 1.25, fill_color=color)
    txt(sl, week, 8.62, y0+0.07, 0.9, 0.35, size=10.5, bold=True, color=color)
    txt(sl, steps, 8.62, y0+0.40, 4.15, 0.80, size=9, color=C_TEXT, wrap=True)
    y0 += 1.34

# ══════════════════════════════════════════════════════════════════════════════
# SLIDE 14 — Returns Summary (all strategies vs SPY)
# ══════════════════════════════════════════════════════════════════════════════
sl = add_slide(prs)
bg(sl)
section_header(sl, "Returns Summary — All Strategies vs Benchmarks", "Risk-adjusted comparison across strategies, validated against public market data")

# Bar chart — IRR comparison
fig, ax = plt.subplots(figsize=(7, 3.8), facecolor="#1A1D2E")
ax.set_facecolor("#1A1D2E")

strategies = [
    "SPY\nBuy-Hold", "60/40\nPortfolio", "Land\nHold Only",
    "Build-to-Rent\n(BTR)", "Build-to-Sell\n(BTS)", "Hybrid\n(60 BTS/40 BTR)",
    "Portfolio\n(14 projects)"
]
irrs   = [12.2,  7.5, 6.5, IRR_BTR*100, IRR_BTS*100, (0.6*IRR_BTS+0.4*IRR_BTR)*100, 12.8]
bar_cs = ["#F59E0B","#94A3B8","#94A3B8","#F59E0B","#10B981","#6C63FF","#10B981"]
bars   = ax.bar(strategies, irrs, color=bar_cs, alpha=0.88, width=0.6)
ax.axhline(12.2, color="#F59E0B", lw=1.5, ls="--", alpha=0.6, label="SPY CAGR")

for bar, val in zip(bars, irrs):
    ax.text(bar.get_x()+bar.get_width()/2, val+0.3, f"{val:.0f}%",
            ha="center", color="#E2E8F0", fontsize=9.5, fontweight="bold")

ax.set_ylabel("CAGR / IRR (%)", color="#94A3B8", fontsize=10)
ax.tick_params(colors="#94A3B8", labelsize=8.5)
for sp in ax.spines.values(): sp.set_color("#2D3448")
ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v,_: f"{v:.0f}%"))
ax.grid(color="#2D3448", lw=0.4, alpha=0.6, axis="y")
ax.legend(fontsize=9, framealpha=0, labelcolor="#E2E8F0")
fig.tight_layout()
add_chart_img(sl, fig, 0.35, 1.10, 7.5, 4.3)

# Right: comparison table
box(sl, 8.1, 1.10, 4.9, 6.1, fill_color=C_CARD)
txt(sl, "Risk-Adjusted Comparison", 8.3, 1.18, 4.5, 0.42, size=13, bold=True, color=C_TEXT)
divider_line(sl, 1.65, color=C_GRID, left=8.1, right=13.0)

table_rows = [
    ("Strategy",          "IRR/CAGR", "Liquidity", "Leverage"),
    ("SPY Buy-Hold",       "12.2%",    "Daily",     "None"),
    ("60/40 Portfolio",    "7.5%",     "Daily",     "None"),
    ("Land Hold Only",     "6.5%",     "Months",    "None"),
    ("Build-to-Rent",     f"{IRR_BTR:.0%}",  "7+ yrs",    "65% LTV"),
    ("Build-to-Sell",     f"{IRR_BTS:.0%}",  "~2 yrs",    "65% LTV"),
    ("Hybrid (rec.)",     f"{(0.6*IRR_BTS+0.4*IRR_BTR)*100:.0f}%","2-7 yrs","65% LTV"),
    ("Portfolio CAGR",    "12.8%",    "Rolling",   "65% LTV"),
]
y0 = 1.72
for i, (s, irr, liq, lev) in enumerate(table_rows):
    hdr = i == 0
    bg_c = RGBColor(0x14, 0x17, 0x28) if hdr else (RGBColor(0x1E,0x22,0x35) if i%2==0 else RGBColor(0x22,0x26,0x3A))
    box(sl, 8.1, y0, 4.9, 0.47, fill_color=bg_c)
    col_s = C_MUTED if hdr else (C_GREEN if i >= 4 else C_TEXT)
    txt(sl, s,   8.22, y0+0.08, 2.2, 0.32, size=9.5, bold=hdr, color=C_MUTED if hdr else C_TEXT)
    txt(sl, irr, 10.45, y0+0.08, 0.9, 0.32, size=9.5, bold=not hdr, color=col_s)
    txt(sl, liq, 11.38, y0+0.08, 0.8, 0.32, size=9.5, color=C_MUTED if hdr else C_TEXT)
    txt(sl, lev, 12.2, y0+0.08, 0.75, 0.32, size=9.5, color=C_MUTED if hdr else C_TEXT)
    y0 += 0.47

txt(sl, "RE returns are private, illiquid, and levered. SPY returns include 2020-2021 exceptional bull run.",
    8.1, 6.92, 4.9, 0.38, size=8.0, color=C_MUTED, italic=True)

# ══════════════════════════════════════════════════════════════════════════════
# SLIDE 15 — Summary / Call to Action
# ══════════════════════════════════════════════════════════════════════════════
sl = add_slide(prs)
bg(sl)

box(sl, 0, 0, 0.18, 7.5, fill_color=C_GREEN)
box(sl, 0.18, 0, 13.15, 7.5, fill_color=C_CARD)

txt(sl, "THE OPPORTUNITY IN SUMMARY", 0.5, 0.6, 12.5, 0.55, size=13, bold=True, color=C_MUTED)
txt(sl, "Urban Growth AI — Corporate Real Estate", 0.5, 1.15, 12.5, 0.7, size=36, bold=True, color=C_GREEN)
txt(sl, "An AI-driven, institutional-quality process for ground-up development in the highest-growth US markets",
    0.5, 1.88, 12.5, 0.5, size=14, color=C_TEXT)
divider_line(sl, 2.48, color=C_GREEN, left=0.5, right=12.8)

# Three pillars
pillars = [
    (C_GREEN,  "AI-Identified Sites",
     "H3 hex model scores 1,000s of parcels. Top 10% Tier-1 lots pre-validated for growth potential, zoning, and fundamentals."),
    (C_AMBER,  "Proven Economics",
     "19%+ levered IRR, 2.5× equity multiple. Validated against CoStar, CBRE, Turner & Townsend 2024-2025 market data."),
    (C_BLUE,   "Flexible Exit",
     "Build-to-Sell for capital velocity (22-month cycle). Build-to-Rent for income. Hybrid recommended for optimal portfolio."),
]
for i, (color, title, body) in enumerate(pillars):
    x = 0.5 + i * 4.15
    box(sl, x, 2.65, 3.95, 2.5, fill_color=RGBColor(0x1A, 0x1D, 0x2E))
    box(sl, x, 2.65, 3.95, 0.08, fill_color=color)
    txt(sl, title, x+0.15, 2.78, 3.65, 0.5, size=15, bold=True, color=color)
    txt(sl, body,  x+0.15, 3.30, 3.65, 1.8, size=11.5, color=C_TEXT, wrap=True)

# Action items
txt(sl, "NEXT STEPS", 0.5, 5.35, 4.0, 0.4, size=12, bold=True, color=C_MUTED)
actions = [
    "1. Select 3–5 Tier-1 lots for immediate due diligence",
    "2. Engage GC for preliminary budget on top 2 sites",
    "3. Structure LP/GP fund vehicle for capital raise",
    "4. Secure construction lending relationship (65% LTV)",
    "5. Target: first land close within 90 days",
]
for i, a in enumerate(actions):
    txt(sl, a, 0.5 + (i // 3)*4.3, 5.8 + (i % 3)*0.38, 4.2, 0.35, size=11.5, color=C_TEXT)

# Stats row
kpi_final = [("$29.7M","10-yr Portfolio NAV"), ("14.3%","Median BTR IRR"),
             ("2.1×","Equity Multiple"), ("11.5%","CAGR vs 12.2% SPY")]
for i, (val, lbl) in enumerate(kpi_final):
    x = 8.8 + i * 1.08
    txt(sl, val, x, 5.35, 1.0, 0.55, size=22, bold=True, color=C_GREEN, align=PP_ALIGN.CENTER)
    txt(sl, lbl, x, 5.88, 1.0, 0.42, size=8.5, color=C_MUTED, align=PP_ALIGN.CENTER)

txt(sl, "For discussion purposes only. Not investment advice. Past model performance does not guarantee future results.",
    0.5, 7.08, 12.5, 0.35, size=8.5, color=C_MUTED, italic=True)

# ─────────────────────────────────────────────────────────────────────────────
# Save
# ─────────────────────────────────────────────────────────────────────────────
OUT = Path("D:/urbangrowth_data/processed/maps/Urban_Growth_Investment_Strategy.pptx")
OUT.parent.mkdir(parents=True, exist_ok=True)
prs.save(str(OUT))
print(f"\n✓ Saved: {OUT}  ({OUT.stat().st_size//1024} KB)  |  {len(prs.slides)} slides")
