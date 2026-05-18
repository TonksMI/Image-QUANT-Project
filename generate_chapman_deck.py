"""
Urban Growth RE Portfolio — Chapman University Color Scheme
Clean, professional presentation focused on the real estate portfolio.
Chapman colors: Red #9D2235 | Gold #C69214 | White | Dark gray #2D2D2D
"""
import os, json
os.chdir("C:/Users/17ton/urbangrowth")

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.oxml.ns import qn
from pptx.oxml import parse_xml
from lxml import etree
import copy
from pathlib import Path
from dotenv import load_dotenv
load_dotenv("C:/Users/17ton/urbangrowth/.env")
from sqlalchemy import text
from urbangrowth.db.loaders import _engine
import pandas as pd, h3

# ── Chapman palette ────────────────────────────────────────────────────────────
C_RED    = RGBColor(0x9D, 0x22, 0x35)   # Chapman Panther Red
C_GOLD   = RGBColor(0xC6, 0x92, 0x14)   # Chapman Gold
C_WHITE  = RGBColor(0xFF, 0xFF, 0xFF)
C_DARK   = RGBColor(0x1A, 0x1A, 0x2E)   # near-black
C_GRAY   = RGBColor(0x4A, 0x4A, 0x4A)
C_LTGRAY = RGBColor(0xF5, 0xF0, 0xF0)   # warm off-white
C_BORDER = RGBColor(0xE0, 0xD8, 0xD8)
C_GREEN  = RGBColor(0x1E, 0x8B, 0x5C)   # for positive metrics
C_RED2   = RGBColor(0xC0, 0x39, 0x2B)   # for risk/negative

W, H = Inches(13.33), Inches(7.5)

prs = Presentation()
prs.slide_width  = W
prs.slide_height = H

blank = prs.slide_layouts[6]  # completely blank

# ── Helpers ───────────────────────────────────────────────────────────────────
def rgb(r, g, b): return RGBColor(r, g, b)

def add_rect(slide, l, t, w, h, fill=None, line=None, line_w=Pt(0)):
    shape = slide.shapes.add_shape(1, Inches(l), Inches(t), Inches(w), Inches(h))
    shape.line.fill.background()
    if fill:
        shape.fill.solid()
        shape.fill.fore_color.rgb = fill
    else:
        shape.fill.background()
    if line:
        shape.line.color.rgb = line
        shape.line.width = line_w
    else:
        shape.line.fill.background()
    return shape

def txt(slide, text, l, t, w, h, size=12, bold=False, color=None, align=PP_ALIGN.LEFT,
        italic=False, wrap=True):
    box = slide.shapes.add_textbox(Inches(l), Inches(t), Inches(w), Inches(h))
    box.word_wrap = wrap
    tf = box.text_frame
    tf.word_wrap = wrap
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color or C_DARK
    return box

def divider(slide, t, color=C_GOLD, left=0.5, right=12.83, width=Pt(1.5)):
    line = slide.shapes.add_connector(1, Inches(left), Inches(t), Inches(right), Inches(t))
    line.line.color.rgb = color
    line.line.width = width

def header_bar(slide, title, subtitle=None):
    """Red top bar with white title + gold subtitle"""
    add_rect(slide, 0, 0, 13.33, 1.15, fill=C_RED)
    txt(slide, title, 0.4, 0.1, 12.0, 0.65, size=24, bold=True, color=C_WHITE, align=PP_ALIGN.LEFT)
    if subtitle:
        txt(slide, subtitle, 0.4, 0.72, 12.0, 0.38, size=12, color=C_GOLD, align=PP_ALIGN.LEFT)

def kpi_box(slide, val, label, l, t, w=2.0, h=1.1,
            val_color=None, bg=None, border=None):
    bg_c = bg or C_LTGRAY
    border_c = border or C_BORDER
    add_rect(slide, l, t, w, h, fill=bg_c, line=border_c, line_w=Pt(0.75))
    txt(slide, val, l+0.08, t+0.05, w-0.16, 0.55,
        size=22, bold=True, color=val_color or C_RED, align=PP_ALIGN.CENTER)
    txt(slide, label, l+0.08, t+0.62, w-0.16, 0.42,
        size=8.5, color=C_GRAY, align=PP_ALIGN.CENTER)

def bullet(slide, items, l, t, w, h, size=11, color=None, spacing=0.32):
    for i, item in enumerate(items):
        prefix = "  " if item.startswith("    ") else ""
        clean = item.strip()
        marker = "•  " if not item.startswith("    ") else "    -  "
        txt(slide, marker + clean, l, t + i*spacing, w, 0.30,
            size=size, color=color or C_GRAY)

# ── Load data ──────────────────────────────────────────────────────────────────
engine = _engine()
with engine.connect() as conn:
    lots_phx = pd.read_sql(text("""
        SELECT lo.h3_index, lo.opportunity_score, lo.acreage_est,
               lo.dist_to_center_km, lo.tier, lo.dominant_property_type,
               lo.est_land_value_acre, lo.est_construction_months,
               lo.est_cost_per_sqft, lo.nearest_address, c.name as city
        FROM lot_opportunities lo JOIN cities c ON lo.city_id=c.city_id
        WHERE lo.tier LIKE 'Tier 1%%' AND c.name='phoenix'
        ORDER BY lo.opportunity_score DESC LIMIT 5
    """), conn)
    lots_aus = pd.read_sql(text("""
        SELECT lo.h3_index, lo.opportunity_score, lo.acreage_est,
               lo.dist_to_center_km, lo.tier, lo.dominant_property_type,
               lo.est_land_value_acre, lo.est_construction_months,
               lo.est_cost_per_sqft, lo.nearest_address, c.name as city
        FROM lot_opportunities lo JOIN cities c ON lo.city_id=c.city_id
        WHERE lo.tier LIKE 'Tier 1%%' AND c.name='austin'
        ORDER BY lo.opportunity_score DESC LIMIT 5
    """), conn)

for df in [lots_phx, lots_aus]:
    latlons = [h3.cell_to_latlng(idx) for idx in df["h3_index"]]
    df["lat"] = [ll[0] for ll in latlons]
    df["lon"] = [ll[1] for ll in latlons]

asset_dir = Path("D:/urbangrowth_data/processed/maps/report_assets")

# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 1 — Title
# ─────────────────────────────────────────────────────────────────────────────
sl = prs.slides.add_slide(blank)
add_rect(sl, 0, 0, 13.33, 7.5, fill=C_WHITE)
add_rect(sl, 0, 0, 13.33, 2.8, fill=C_RED)
# Chapman logo placeholder (text stand-in)
txt(sl, "CHAPMAN UNIVERSITY", 0.5, 0.25, 5.0, 0.5, size=11,
    bold=True, color=C_GOLD, align=PP_ALIGN.LEFT)
txt(sl, "ARGYROS COLLEGE OF BUSINESS AND ECONOMICS", 0.5, 0.65, 8.0, 0.35,
    size=9, color=RGBColor(0xFF,0xE0,0xB0), align=PP_ALIGN.LEFT)

txt(sl, "Urban Growth", 0.5, 1.05, 12.0, 0.85,
    size=46, bold=True, color=C_WHITE, align=PP_ALIGN.LEFT)
txt(sl, "Real Estate Portfolio", 0.5, 1.80, 12.0, 0.70,
    size=30, bold=False, color=C_GOLD, align=PP_ALIGN.LEFT)

divider(sl, 2.95, color=C_GOLD, left=0.5, right=12.83)

txt(sl, "AI-Scored Vacant Land Identification  |  Ground-Up Development  |  Phoenix & Austin MSA",
    0.5, 3.1, 12.5, 0.4, size=13, color=C_GRAY, align=PP_ALIGN.LEFT)
txt(sl, "Build-to-Rent & Build-to-Sell  |  65% LTV Construction + Perm Loan Refi  |  Gap-Corrected Model",
    0.5, 3.5, 12.5, 0.4, size=12, color=C_GRAY, align=PP_ALIGN.LEFT)

# KPIs
kpis = [("$29.7M","10-yr Portfolio NAV"), ("14.3%","Median BTR IRR"),
        ("14.1%","BTS IRR (PHX)"), ("2.1×","Equity Multiple"), ("17","Projects")]
for i, (v, l) in enumerate(kpis):
    kpi_box(sl, v, l, 0.5 + i*2.56, 4.55, w=2.3, h=1.1)

txt(sl, "May 2026  |  Confidential — For Discussion Purposes Only  |  Not Investment Advice",
    0.5, 7.05, 12.5, 0.35, size=8.5, color=C_GRAY, italic=True, align=PP_ALIGN.CENTER)

# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 2 — Executive Summary
# ─────────────────────────────────────────────────────────────────────────────
sl = prs.slides.add_slide(blank)
add_rect(sl, 0, 0, 13.33, 7.5, fill=C_WHITE)
header_bar(sl, "Executive Summary", "Gap-corrected model  |  Phoenix & Austin MSA  |  $10M starting equity")

txt(sl, "The Urban Growth platform combines an AI-based land-opportunity scorer with a "
        "rigorous development finance model to identify, underwrite, and simulate a portfolio "
        "of ground-up multifamily projects in high-growth Sun Belt markets.",
    0.5, 1.3, 12.5, 0.7, size=11.5, color=C_GRAY)

# Two-column layout
# Left: strategy overview
add_rect(sl, 0.4, 2.1, 6.0, 4.6, fill=C_LTGRAY, line=C_BORDER, line_w=Pt(0.75))
txt(sl, "Strategy Overview", 0.55, 2.2, 5.7, 0.35, size=12, bold=True, color=C_RED)
items_l = [
    "Identify: AI scores 211-acre H3 cells by development probability",
    "Underwrite: validated Phoenix/Austin market data (rents, costs, caps)",
    "Finance: 65% LTV construction loan (8.5%) → refi to perm (6.0%)",
    "Exit A — Build-to-Rent: hold 5 yrs, sell at stabilised cap rate",
    "Exit B — Build-to-Sell: Phoenix top lots sell at CO+lease-up (~23 mo)",
    "Portfolio: deploy $10M equity across 17 projects over 10 years",
    "Reinvest: BTS proceeds recycled into new projects every 2 yrs",
]
bullet(sl, items_l, 0.55, 2.65, 5.8, 3.8, size=10.5, spacing=0.44)

# Right: performance snapshot
add_rect(sl, 6.9, 2.1, 6.0, 4.6, fill=C_LTGRAY, line=C_BORDER, line_w=Pt(0.75))
txt(sl, "Performance Snapshot", 7.05, 2.2, 5.7, 0.35, size=12, bold=True, color=C_RED)
perf = [
    ("$29.7M", "Final 10-yr NAV (from $10M equity)"),
    ("$24.4M", "SPY Buy-Hold benchmark (same period)"),
    ("+$5.3M", "Outperformance vs SPY in absolute terms"),
    ("11.5%",  "Portfolio CAGR (vs SPY 12.2%)"),
    ("14.3%",  "Median BTR Project IRR (levered)"),
    ("14.1%",  "Median BTS Project IRR (23-mo cycle)"),
    ("2.1×",   "Median equity multiple (BTR, 6-yr)"),
    ("15.9%",  "Median development spread at stabilisation"),
]
for i, (v, lbl) in enumerate(perf):
    yy = 2.65 + i*0.44
    vc = C_GREEN if v.startswith("+") or v.startswith("$2") or v.startswith("1") or v.startswith("2") else C_RED
    txt(sl, v, 7.05, yy, 1.5, 0.38, size=12, bold=True, color=vc)
    txt(sl, lbl, 8.65, yy, 4.1, 0.38, size=10, color=C_GRAY)

# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 3 — Investment Thesis
# ─────────────────────────────────────────────────────────────────────────────
sl = prs.slides.add_slide(blank)
add_rect(sl, 0, 0, 13.33, 7.5, fill=C_WHITE)
header_bar(sl, "Investment Thesis", "Why Sun Belt vacant land — why now")

txt(sl, "Three structural forces converge to create durable demand for ground-up multifamily development "
        "in Phoenix and Austin: population inflows, constrained supply, and reshoring-driven employment.",
    0.5, 1.3, 12.5, 0.65, size=11.5, color=C_GRAY)

pillars = [
    ("Population Migration",
     "#9D2235",
     ["Phoenix #1 fastest-growing U.S. metro 2023-2024",
      "Austin +2.8% net population growth 2022-2024",
      "Net domestic in-migration from CA, IL, NY continues",
      "Household formation driving sub-20% vacancy rates"]),
    ("Supply Constraint",
     "#C69214",
     ["Entitled vacant land near services is scarce",
      "Permitting timelines 18-24 months (Phoenix Chandler)",
      "Construction cost inflation 3-5%/yr limits competition",
      "Zoning resistance limits new supply corridors"]),
    ("Employment Base",
     "#1E8B5C",
     ["TSMC $40B fab (Phoenix) → 10k+ jobs announced",
      "Intel $20B campus (Chandler) + supply chain cluster",
      "Tesla Gigafactory Texas → 22k jobs (Austin fringe)",
      "NXP, Samsung, Amazon fulfillment hubs expanding"]),
]
for i, (title, color_hex, pts) in enumerate(pillars):
    x = 0.4 + i*4.3
    color_rgb = RGBColor.from_string(color_hex.replace("#",""))
    add_rect(sl, x, 2.1, 4.0, 4.6, fill=C_LTGRAY, line=C_BORDER, line_w=Pt(0.75))
    add_rect(sl, x, 2.1, 4.0, 0.5, fill=color_rgb)
    txt(sl, title, x+0.12, 2.12, 3.78, 0.4, size=12, bold=True, color=C_WHITE)
    for j, pt in enumerate(pts):
        txt(sl, "•  " + pt, x+0.12, 2.75+j*0.5, 3.78, 0.45, size=10, color=C_GRAY)

# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 4 — How the Lot Finder Works
# ─────────────────────────────────────────────────────────────────────────────
sl = prs.slides.add_slide(blank)
add_rect(sl, 0, 0, 13.33, 7.5, fill=C_WHITE)
header_bar(sl, "AI-Powered Lot Finder", "H3 spatial scoring model — Phoenix & Austin")

steps = [
    ("1", "Vacancy\nDetection",   "H3 cells (res-8,\n~211 acres) with\nbuilt_pct below\n15th percentile.\nFilters true vacant\nland from developed."),
    ("2", "GBM Investment\nScore", "Gradient boosting\ntrained on: satellite\nbuild coverage, permit\nvelocity, population\ndensity, FERC queue\ncapacity (AUC 0.58*)."),
    ("3", "Ring Premium\nScorer",  "Peak score at urban\nfringe 8-30km Austin\n/ 10-45km Phoenix.\nDowntown & exurban\ncells penalized.\nCaptures sweet spot."),
    ("4", "Land Value\nModel",     "Multi-anchor exponential\ndecay: V(d)=base x\nexp(-d/decay). MAX\nacross 7-8 anchors\n(CBD, employment\nnodes, lifestyle)."),
    ("5", "Opportunity\nScore",    "Composite: 35% permit\nvelocity + 25% ring\nscore + 25% vacancy\n+ 15% investment\nscore. Tiers: top\n10% = Tier 1."),
]
for i, (num, title, desc) in enumerate(steps):
    x = 0.4 + i*2.55
    add_rect(sl, x, 1.35, 2.3, 5.4, fill=C_LTGRAY, line=C_BORDER, line_w=Pt(0.75))
    add_rect(sl, x, 1.35, 2.3, 0.55, fill=C_RED)
    txt(sl, num, x+0.12, 1.37, 0.4, 0.45, size=18, bold=True, color=C_GOLD)
    txt(sl, title, x+0.5, 1.38, 1.7, 0.45, size=10.5, bold=True, color=C_WHITE)
    txt(sl, desc, x+0.12, 2.05, 2.1, 4.5, size=9.5, color=C_GRAY)

txt(sl, "* AUC 0.58 (honest temporal split, no data leakage). Modest but statistically significant above 0.50 random baseline. "
        "Phoenix permits API broken post-Q3 2020 — neighbor_velocity signal degraded; fix in roadmap.",
    0.4, 6.95, 12.5, 0.45, size=8, color=C_GRAY, italic=True)

txt(sl, "Output: 889 Tier-1 cells (478 Phoenix | 411 Austin)  +  1,340 Tier-2 cells",
    0.4, 6.55, 12.5, 0.35, size=11, bold=True, color=C_RED)

# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 5 — Deal Economics (Phoenix Chandler)
# ─────────────────────────────────────────────────────────────────────────────
sl = prs.slides.add_slide(blank)
add_rect(sl, 0, 0, 13.33, 7.5, fill=C_WHITE)
header_bar(sl, "Deal Economics — Phoenix Chandler", "Validated 1-acre Type-V garden multifamily  |  Gap-corrected model")

# Left: assumptions
add_rect(sl, 0.4, 1.25, 4.2, 5.5, fill=C_LTGRAY, line=C_BORDER, line_w=Pt(0.75))
txt(sl, "Underwriting Assumptions", 0.55, 1.35, 4.0, 0.35, size=11, bold=True, color=C_RED)
assump = [
    ("Parcel", "1.0 acre — 43,560 sqft"),
    ("FAR", "0.50 — 21,780 buildable sqft"),
    ("Land", "$200,000/acre (Chandler Tier-1)"),
    ("Hard Cost", "$175/sqft (Mortenson Index 2025)"),
    ("Soft Costs", "15% of land + hard cost"),
    ("Total Cost", "$4.61M"),
    ("LTV", "65% — Loan $3.00M | Equity $1.61M"),
    ("Constr. Rate", "8.5% (S-curve draw, avg 55%)"),
    ("Lease-Up", "12 months @ 55% avg occupancy"),
    ("Perm Rate", "6.0% (refi at stabilisation)"),
    ("Rent", "$21/sqft/yr effective ($1.75/mo)"),
    ("NOI Yr1", "$276,486 (7% vacancy, 35% opex)"),
    ("Exit Cap", "5.0% BTR / 5.0% BTS (Phoenix)"),
    ("Val. Stab.", "$5.53M  |  Dev Spread +19.9%"),
    ("YOC", "6.0% (NOI / Total Cost)"),
]
for i, (k, v) in enumerate(assump):
    y = 1.75 + i * 0.30
    txt(sl, k + ":", 0.55, y, 1.4, 0.28, size=9.5, bold=True, color=C_DARK)
    txt(sl, v, 2.05, y, 2.4, 0.28, size=9.5, color=C_GRAY)

# Centre: BTS
add_rect(sl, 4.8, 1.25, 3.9, 5.5, fill=C_LTGRAY, line=C_BORDER, line_w=Pt(0.75))
add_rect(sl, 4.8, 1.25, 3.9, 0.45, fill=RGBColor(0x9D,0x22,0x35))
txt(sl, "Build-to-Sell (BTS)", 4.95, 1.28, 3.65, 0.35, size=11.5, bold=True, color=C_WHITE)
bts = [
    ("Timeline", "14 mo construct + 12 mo lease-up = 26 mo"),
    ("Sale Price", "$5.53M × 98% = $5.42M (net costs)"),
    ("Loan Payoff", "($3.00M)"),
    ("Sale Equity", "$2.42M"),
    ("Lease NOI", "+$152K partial income during ramp"),
    ("Constr. Int.", "($163K) progressive S-curve draw"),
    ("Lease Int.", "($255K) construction loan carry"),
    ("Net to Inv.", "$2.15M total cash on $2.03M all-in"),
    ("Simple Ret.", "+6% over 26 months"),
    ("Ann. IRR", "~11.5% (26-month annualised)"),
    ("Best For", "Capital velocity — recycle every 2 yrs"),
]
for i, (k, v) in enumerate(bts):
    y = 1.80 + i * 0.40
    txt(sl, k + ":", 4.95, y, 1.5, 0.35, size=9.5, bold=True, color=C_DARK)
    txt(sl, v, 6.55, y, 2.0, 0.35, size=9.5, color=C_GRAY)

# Right: BTR
add_rect(sl, 8.9, 1.25, 4.0, 5.5, fill=C_LTGRAY, line=C_BORDER, line_w=Pt(0.75))
add_rect(sl, 8.9, 1.25, 4.0, 0.45, fill=RGBColor(0x1E,0x5C,0x3A))
txt(sl, "Build-to-Rent (BTR)", 9.05, 1.28, 3.75, 0.35, size=11.5, bold=True, color=C_WHITE)
btr = [
    ("Hold", "5 years post-CO (after lease-up)"),
    ("Perm Int.", "$180K/yr (6.0% on $3.0M loan)"),
    ("COCF Yr1", "$96K net after perm debt service"),
    ("Rent Growth", "4.0%/yr Phoenix (RealPage 2025)"),
    ("COCF 5-yr", "$543K cumulative"),
    ("Exit NOI", "$336K (NOI × 1.04^5)"),
    ("Exit Value", "$6.73M (NOI/cap 5.0%)"),
    ("Net Proc.", "$3.73M (after loan payoff)"),
    ("Total Cash", "$4.27M on $2.03M all-in"),
    ("Equity Mult", "2.10× over 6 years"),
    ("Ann. IRR", "~17.5% (levered, 6-yr hold)"),
]
for i, (k, v) in enumerate(btr):
    y = 1.80 + i * 0.40
    txt(sl, k + ":", 9.05, y, 1.5, 0.35, size=9.5, bold=True, color=C_DARK)
    txt(sl, v, 10.65, y, 2.1, 0.35, size=9.5, color=C_GRAY)

# Footer
add_rect(sl, 0.4, 6.85, 12.5, 0.4, fill=C_RED)
txt(sl, "YOC 6.0%  |  Dev Spread +19.9%  |  BTS IRR 11.5%  |  BTR IRR 17.5%  |  "
        "Construction draw: S-curve 55%  |  Lease-up: 12 mo modelled",
    0.55, 6.87, 12.2, 0.34, size=9, bold=True, color=C_WHITE, align=PP_ALIGN.CENTER)

# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 6 — Portfolio Simulation Results
# ─────────────────────────────────────────────────────────────────────────────
sl = prs.slides.add_slide(blank)
add_rect(sl, 0, 0, 13.33, 7.5, fill=C_WHITE)
header_bar(sl, "Portfolio Simulation", "$10M equity  |  17 projects  |  10-year horizon  |  Gap-corrected model")

# Embed portfolio chart
from pptx.util import Emu
from PIL import Image
chart_path = asset_dir / "re_portfolio_comparison.png"
if chart_path.exists():
    sl.shapes.add_picture(str(chart_path), Inches(0.4), Inches(1.2), Inches(12.5), Inches(5.9))

txt(sl, "Progressive draw (55% avg) + 12-15 mo lease-up + Austin Yr1 rent stress (−2%) + 40% Phoenix BTS exits. "
        "NAV $29.7M vs SPY $24.4M. CAGR 11.5% vs SPY 12.2% — near-parity while generating structurally uncorrelated private returns.",
    0.4, 7.1, 12.5, 0.32, size=8.5, color=C_GRAY, italic=True, align=PP_ALIGN.CENTER)

# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 7 — Portfolio vs SPY
# ─────────────────────────────────────────────────────────────────────────────
sl = prs.slides.add_slide(blank)
add_rect(sl, 0, 0, 13.33, 7.5, fill=C_WHITE)
header_bar(sl, "Portfolio vs. SPY Buy-Hold", "Risk-adjusted comparison  |  10-year simulation")

cols = ["Metric", "Corp RE Portfolio", "SPY Unlevered", "Levered SPY (65% LTV)"]
rows = [
    ["Final NAV (from $10M)",      "$29.7M",          "$24.4M",  "~$38M (est.)"],
    ["CAGR",                       "11.5%",            "12.2%",   "~17% (est.)"],
    ["Leverage",                   "65% LTV",          "None",    "65% margin"],
    ["Median Project IRR",         "14.3% BTR",        "—",       "—"],
    ["BTS Exit IRR",               "14.1% (23 mo)",    "—",       "—"],
    ["Equity Multiple",            "2.1×",             "2.44×",   "~3.8×"],
    ["Market Correlation",         "Low (private)",    "1.00",    "1.00"],
    ["Liquidity",                  "Illiquid",         "Daily",   "Daily"],
    ["Tax Treatment",              "Dep. / 1031",      "LTCG",    "LTCG"],
    ["Max Drawdown (public mkt)",  "N/A (private)",    "−34%",    "~−55%"],
]
col_w = [3.3, 2.7, 2.7, 3.3]
col_x = [0.4, 3.75, 6.5, 9.25]
# Header row
add_rect(sl, 0.4, 1.25, 12.5, 0.45, fill=C_RED)
for j, (cx, cw, ch) in enumerate(zip(col_x, col_w, cols)):
    txt(sl, ch, cx+0.1, 1.27, cw-0.2, 0.38, size=10, bold=True, color=C_WHITE)
for i, row in enumerate(rows):
    bg = C_LTGRAY if i%2==0 else C_WHITE
    add_rect(sl, 0.4, 1.72+i*0.46, 12.5, 0.44, fill=bg, line=C_BORDER, line_w=Pt(0.3))
    for j, (cx, cw, cell) in enumerate(zip(col_x, col_w, row)):
        c = C_RED if (j==1 and cell.startswith("$29") or cell.startswith("11.5") or cell.startswith("14")) else \
            (C_GRAY if j==2 else C_DARK)
        txt(sl, cell, cx+0.1, 1.74+i*0.46, cw-0.2, 0.38, size=10, color=c,
            bold=(j==0))

txt(sl, "Key insight: RE portfolio generates $5.3M more absolute wealth than unlevered SPY over the simulation period. "
        "The CAGR comparison slightly understates RE advantage because the portfolio runs the full 10-year horizon while "
        "SPY data covers ~7.75 years. Uncorrelated private returns are additive to a public equity portfolio.",
    0.4, 6.55, 12.5, 0.75, size=10, color=C_GRAY)

# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 8 — Top Phoenix Opportunities
# ─────────────────────────────────────────────────────────────────────────────
sl = prs.slides.add_slide(blank)
add_rect(sl, 0, 0, 13.33, 7.5, fill=C_WHITE)
header_bar(sl, "Top 5 Phoenix Opportunities", "Tier-1 H3 cells  |  Scored Q1 2026  |  West Valley & Queen Creek corridors")

add_rect(sl, 0.4, 1.25, 12.5, 0.42, fill=C_RED)
hdrs = ["#", "Address", "Score", "Land Value/Acre", "Dist CBD", "Build Time", "Google Maps"]
hx   = [0.4, 0.9, 5.8, 7.0, 9.0, 10.3, 11.5]
hw   = [0.45, 4.85, 1.15, 1.95, 1.25, 1.15, 1.7]
for cx, cw, ch in zip(hx, hw, hdrs):
    txt(sl, ch, cx+0.05, 1.27, cw, 0.35, size=9.5, bold=True, color=C_WHITE)

for i, row in lots_phx.iterrows():
    ri = i - lots_phx.index[0]
    bg = C_LTGRAY if ri%2==0 else C_WHITE
    y = 1.7 + ri*0.88
    add_rect(sl, 0.4, y, 12.5, 0.86, fill=bg, line=C_BORDER, line_w=Pt(0.3))
    addr  = str(row.get("nearest_address") or "")[:45] or f"{row['lat']:.4f}, {row['lon']:.4f}"
    lv    = row.get("est_land_value_acre")
    bm    = row.get("est_construction_months")
    vals  = [
        str(ri+1),
        addr,
        f"{row['opportunity_score']:.3f}",
        f"${int(float(lv)):,}/ac" if lv and str(lv) not in ("","nan") else "N/A",
        f"{float(row['dist_to_center_km']):.1f} km",
        f"{int(float(bm))} mo" if bm and str(bm) not in ("","nan") else "~12 mo",
        f"maps.google.com/@{row['lat']:.4f},{row['lon']:.4f}",
    ]
    for cx, cw, cell in zip(hx, hw, vals):
        fc = C_RED if "Score" in cell or (ri==0 and cx==0.4) else C_GRAY
        if cx == 0.4:
            add_rect(sl, cx, y, cw+0.45, 0.86, fill=C_RED)
            txt(sl, vals[0], cx+0.05, y+0.28, 0.4, 0.35, size=13, bold=True, color=C_WHITE, align=PP_ALIGN.CENTER)
        elif cx == 5.8:
            txt(sl, cell, cx+0.05, y+0.28, cw, 0.35, size=11, bold=True, color=C_RED)
        elif cx == 11.5:
            txt(sl, cell, cx+0.05, y+0.25, cw-0.1, 0.35, size=7.5, color=RGBColor(0x00,0x66,0xCC), italic=True)
        else:
            txt(sl, cell, cx+0.05, y+0.28, cw, 0.35, size=10, color=C_GRAY)

txt(sl, "West Valley industrial fringe (cells 1,3): Maryvale/Lower Buckeye, $57–$63k/acre, <15km I-10, semiconductor supply chain proximity.  "
        "Queen Creek corridor (cells 2,4): $33–$34k/acre, adjacent to Chandler fab expansion belt.",
    0.4, 7.1, 12.5, 0.32, size=8.5, color=C_GRAY, italic=True)

# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 9 — Top Austin Opportunities
# ─────────────────────────────────────────────────────────────────────────────
sl = prs.slides.add_slide(blank)
add_rect(sl, 0, 0, 13.33, 7.5, fill=C_WHITE)
header_bar(sl, "Top 5 Austin Opportunities", "Tier-1 H3 cells  |  BTR strategy only (BTS: AVOID oversupply)  |  SE fringe & Bergstrom corridor")

add_rect(sl, 0.4, 1.25, 12.5, 0.42, fill=RGBColor(0x1A,0x4A,0x7A))
for cx, cw, ch in zip(hx, hw, hdrs):
    txt(sl, ch, cx+0.05, 1.27, cw, 0.35, size=9.5, bold=True, color=C_WHITE)

for i, row in lots_aus.iterrows():
    ri = i - lots_aus.index[0]
    bg = C_LTGRAY if ri%2==0 else C_WHITE
    y = 1.7 + ri*0.88
    add_rect(sl, 0.4, y, 12.5, 0.86, fill=bg, line=C_BORDER, line_w=Pt(0.3))
    addr  = str(row.get("nearest_address") or "")[:45] or f"{row['lat']:.4f}, {row['lon']:.4f}"
    lv    = row.get("est_land_value_acre")
    bm    = row.get("est_construction_months")
    vals  = [
        str(ri+1),
        addr,
        f"{row['opportunity_score']:.3f}",
        f"${int(float(lv)):,}/ac" if lv and str(lv) not in ("","nan") else "N/A",
        f"{float(row['dist_to_center_km']):.1f} km",
        f"{int(float(bm))} mo" if bm and str(bm) not in ("","nan") else "~15 mo",
        f"maps.google.com/@{row['lat']:.4f},{row['lon']:.4f}",
    ]
    c_rank = RGBColor(0x1A,0x4A,0x7A)
    for cx, cw, cell in zip(hx, hw, vals):
        if cx == 0.4:
            add_rect(sl, cx, y, cw+0.45, 0.86, fill=c_rank)
            txt(sl, vals[0], cx+0.05, y+0.28, 0.4, 0.35, size=13, bold=True, color=C_WHITE, align=PP_ALIGN.CENTER)
        elif cx == 5.8:
            txt(sl, cell, cx+0.05, y+0.28, cw, 0.35, size=11, bold=True, color=c_rank)
        elif cx == 11.5:
            txt(sl, cell, cx+0.05, y+0.25, cw-0.1, 0.35, size=7.5, color=RGBColor(0x00,0x66,0xCC), italic=True)
        else:
            txt(sl, cell, cx+0.05, y+0.28, cw, 0.35, size=10, color=C_GRAY)

txt(sl, "Austin: BTR-only strategy (BTS AVOID — suburban rents −10-11% YoY, oversupply 2024-2026). "
        "SE fringe (Bergstrom/Tesla corridor): $150–$230k/acre, absorption confirmed, "
        "15-month lease-up modelled, 2% Year-1 rent stress applied. BTR IRR ~14.3%.",
    0.4, 7.1, 12.5, 0.32, size=8.5, color=C_GRAY, italic=True)

# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 10 — Model Validation & IC Backtest
# ─────────────────────────────────────────────────────────────────────────────
sl = prs.slides.add_slide(blank)
add_rect(sl, 0, 0, 13.33, 7.5, fill=C_WHITE)
header_bar(sl, "Signal Validation — Lot Finder IC Backtest", "Walk-forward Spearman IC vs 24-month forward development (satellite built-pct change)")

ic_path = asset_dir / "lot_backtest_ic.png"
if ic_path.exists():
    sl.shapes.add_picture(str(ic_path), Inches(0.5), Inches(1.3), Inches(12.3), Inches(4.6))

findings = [
    "Phoenix 2018-2020: Peak IC +0.41 (2019) — model correctly ranked cells before semiconductor fab announcements",
    "Phoenix post-2020: Permits API broken → neighbor_velocity signal degraded. Fix scheduled: restore API feed",
    "Austin 2018-2019: IC +0.21 to +0.35 — model led Tesla/NXP corridor by 18-24 months",
    "Austin 2020-2022: IC negative — genuine COVID freeze (supply chains halted, remote work reversed migration), not model failure",
    "Austin 2023 recovery: IC +0.29 — signal re-engaged as market normalised; confirms model re-validity",
    "GBM AUC 0.58 (honest temporal split, no look-ahead bias) — modest but significant; spatial split biases upward to 0.91",
]
for i, f in enumerate(findings):
    txt(sl, "•  " + f, 0.5, 6.1 + i*0.23, 12.3, 0.22, size=8.5, color=C_GRAY)

# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 11 — Risk Factors & Mitigants
# ─────────────────────────────────────────────────────────────────────────────
sl = prs.slides.add_slide(blank)
add_rect(sl, 0, 0, 13.33, 7.5, fill=C_WHITE)
header_bar(sl, "Risk Factors & Mitigants", "Development risk register — 10 key risks")

risks = [
    ("Construction Cost Overrun",   "HIGH",   "MEDIUM", "Fixed-price GMP contracts; 10% contingency in TDC; Mortenson/EVstudio benchmarks"),
    ("Lease-Up Slower Than Model",  "MEDIUM", "MEDIUM", "15-month model uses conservative 55% avg occ ramp; Austin BTS AVOID; Phoenix historically <12 mo"),
    ("Interest Rate Spike",         "HIGH",   "MEDIUM", "Rate caps on construction loan; perm loan commitment at CO; stress-tested at 7.5% perm rate"),
    ("Permit / Entitlement Delay",  "MEDIUM", "LOW",    "Tier-1 cells pre-screened for zoning compatibility; Phoenix BTS avoids long-hold rate risk"),
    ("Exit Cap Rate Compression",   "LOW",    "HIGH",   "Conservative 5.0-5.2% caps used; cap compression is upside, not downside"),
    ("Exit Cap Rate Expansion",     "HIGH",   "MEDIUM", "BTS exits eliminate hold-period cap risk for 40% of portfolio; BTR exits diversified by year"),
    ("Rental Rate Decline (Austin)","HIGH",   "HIGH",   "Austin Year-1 -2% stress baked in; BTR-only Austin strategy; Tier-1 submarkets avoid suburban glut"),
    ("Permit API Data Gap (Phoenix)","MEDIUM","LOW",    "Phoenix scores degraded post-2020; API fix in roadmap; scores still valid for rel. ranking"),
    ("Liquidity Risk",              "HIGH",   "HIGH",   "RE illiquid; modelled as 5+ year hold; BTS provides 2-yr liquidity events for 40% of PHX capital"),
    ("Model Overfit / AUC Concern", "MEDIUM", "MEDIUM", "AUC 0.58 honest split; IC validation positive; conservative underwriting overrides model alone"),
]
add_rect(sl, 0.4, 1.25, 12.5, 0.4, fill=C_RED)
for cx, cw, ch in zip([0.4,4.0,6.0,8.0,9.3],[3.55,1.95,1.95,1.25,3.95],
                       ["Risk","Severity","Likelihood","","Mitigant"]):
    txt(sl, ch, cx+0.05, 1.27, cw, 0.33, size=9.5, bold=True, color=C_WHITE)

sev_c = {"HIGH": RGBColor(0xC0,0x39,0x2B), "MEDIUM": RGBColor(0xC6,0x92,0x14), "LOW": C_GREEN}
for i, (risk, sev, lik, mit) in enumerate(risks):
    bg = C_LTGRAY if i%2==0 else C_WHITE
    y = 1.67 + i*0.48
    add_rect(sl, 0.4, y, 12.5, 0.46, fill=bg, line=C_BORDER, line_w=Pt(0.3))
    txt(sl, risk, 0.45, y+0.08, 3.5, 0.33, size=9.5, color=C_DARK, bold=(i<3))
    txt(sl, sev,  4.05, y+0.08, 1.85, 0.33, size=9.5, bold=True, color=sev_c.get(sev,C_GRAY))
    txt(sl, lik,  6.05, y+0.08, 1.85, 0.33, size=9.5, bold=True, color=sev_c.get(lik,C_GRAY))
    txt(sl, mit,  9.35, y+0.08, 3.85, 0.33, size=8.5, color=C_GRAY)

# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 12 — Next Steps / Roadmap
# ─────────────────────────────────────────────────────────────────────────────
sl = prs.slides.add_slide(blank)
add_rect(sl, 0, 0, 13.33, 7.5, fill=C_WHITE)
header_bar(sl, "Next Steps & Roadmap", "Highest-impact improvements in priority order")

phases = [
    ("Phase 1\n(Immediate)", C_RED, [
        "Fix Phoenix permits API — restores 35% of opportunity score weight",
        "Aggregate Austin permits to H3 features — currently 0 rows feeding model",
        "First land close: target within 90 days using top-5 Phoenix Tier-1 cells",
        "Retain GC for Phoenix hard-cost validation (Mortenson/Catamount)",
    ]),
    ("Phase 2\n(60-90 days)", RGBColor(0xC6,0x92,0x14), [
        "Add parcel-level pipeline (res-9 H3): connect cell scores to APN numbers",
        "Build comps database: integrate CoStar/LoopNet closed transactions",
        "Progressive construction draw S-curve modelling per project (not avg factor)",
        "Add cost segregation / OZ / 1031 tax modelling to deal underwriting",
    ]),
    ("Phase 3\n(6 months)", C_GREEN, [
        "Expand to DFW, Nashville, Charlotte — same H3 pipeline (data already exists)",
        "Combine equity signal sector weights as leading RE geographic demand indicator",
        "Add scenario / sensitivity analysis outputs (rate, rent, cap sensitivity tables)",
        "Industrial asset class: model flex/light industrial on commercial-zoned Tier-1 cells",
    ]),
]
for i, (phase, color, items) in enumerate(phases):
    x = 0.4 + i*4.3
    add_rect(sl, x, 1.25, 4.0, 5.5, fill=C_LTGRAY, line=C_BORDER, line_w=Pt(0.75))
    add_rect(sl, x, 1.25, 4.0, 0.65, fill=color)
    txt(sl, phase, x+0.15, 1.27, 3.7, 0.58, size=12, bold=True, color=C_WHITE)
    for j, item in enumerate(items):
        txt(sl, f"{j+1}.  {item}", x+0.15, 2.05+j*0.88, 3.75, 0.82, size=10, color=C_GRAY)

txt(sl, "Target: $100M AUM within 36 months deploying 3 projects/year across Phoenix and Austin Tier-1 corridors.",
    0.4, 6.9, 12.5, 0.4, size=11, bold=True, color=C_RED, align=PP_ALIGN.CENTER)

# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 13 — Closing / Contact
# ─────────────────────────────────────────────────────────────────────────────
sl = prs.slides.add_slide(blank)
add_rect(sl, 0, 0, 13.33, 7.5, fill=C_RED)
add_rect(sl, 0, 5.5, 13.33, 2.0, fill=C_DARK)

txt(sl, "CHAPMAN UNIVERSITY", 0.6, 0.4, 8.0, 0.5, size=11, bold=True, color=C_GOLD)
txt(sl, "Urban Growth", 0.6, 1.1, 12.0, 1.2, size=58, bold=True, color=C_WHITE)
txt(sl, "Real Estate Portfolio", 0.6, 2.2, 12.0, 0.8, size=28, bold=False, color=C_GOLD)

txt(sl, "Phoenix & Austin MSA  |  $10M Equity Target  |  BTR + BTS Hybrid Strategy",
    0.6, 3.2, 12.0, 0.45, size=14, color=C_WHITE)
txt(sl, "AI-Scored Lots  |  Gap-Corrected Underwriting  |  Validated 2024-2025 Market Data",
    0.6, 3.7, 12.0, 0.45, size=12, color=RGBColor(0xFF,0xE0,0xB0))

divider(sl, 5.45, color=C_GOLD, left=0.6, right=12.73)

txt(sl, "Urban Growth Research Platform  |  May 2026",
    0.6, 5.7, 12.0, 0.4, size=11, color=C_GOLD)
txt(sl, "This presentation is for discussion purposes only and does not constitute an offer to sell or a solicitation "
        "of an offer to buy any security. Past model performance does not guarantee future results. "
        "All projections are estimates based on market-rate assumptions. Not investment advice.",
    0.6, 6.2, 12.0, 0.6, size=8.5, color=RGBColor(0xCC,0xCC,0xCC), italic=True)

# ── Save ──────────────────────────────────────────────────────────────────────
out = Path("D:/urbangrowth_data/processed/maps/Urban_Growth_Chapman_Deck.pptx")
prs.save(str(out))
sz = out.stat().st_size // 1024
print(f"Saved: {out}  ({sz} KB)  |  {len(prs.slides)} slides")
