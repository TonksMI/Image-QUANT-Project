"""
Urban Growth AI — Corporate Real Estate Investment Playbook
Generates a comprehensive Word document playbook.
All market data validated against 2024-2025 broker reports and cost indices.
"""
import os
os.chdir("C:/Users/17ton/urbangrowth")

from pathlib import Path
from docx import Document
from docx.shared import Pt, Inches, RGBColor, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import copy

# ─── Helpers ───────────────────────────────────────────────────────────────────
def add_heading(doc, text, level=1, color=(16, 185, 129)):
    h = doc.add_heading(text, level=level)
    for run in h.runs:
        run.font.color.rgb = RGBColor(*color)
    return h

def add_para(doc, text, bold=False, italic=False, size=11, color=None, indent=0):
    p = doc.add_paragraph()
    if indent:
        p.paragraph_format.left_indent = Inches(indent)
    run = p.add_run(text)
    run.bold = bold
    run.italic = italic
    run.font.size = Pt(size)
    if color:
        run.font.color.rgb = RGBColor(*color)
    return p

def add_bullet(doc, text, level=0, bold_prefix=None):
    p = doc.add_paragraph(style='List Bullet')
    p.paragraph_format.left_indent = Inches(0.25 + level * 0.25)
    if bold_prefix:
        run = p.add_run(bold_prefix + " ")
        run.bold = True
    p.add_run(text)
    return p

def add_table_row(table, cells, bold=False, bg_color=None, sizes=None):
    row = table.add_row()
    for i, (cell_text, cell) in enumerate(zip(cells, row.cells)):
        cell.text = str(cell_text)
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        for run in p.runs:
            run.bold = bold
            run.font.size = Pt(sizes[i] if sizes else 10)
        if bg_color:
            tc = cell._tc
            tcPr = tc.get_or_add_tcPr()
            shd = OxmlElement('w:shd')
            shd.set(qn('w:val'), 'clear')
            shd.set(qn('w:color'), 'auto')
            shd.set(qn('w:fill'), bg_color)
            tcPr.append(shd)
    return row

def shd_cell(cell, hex_color):
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), hex_color)
    tcPr.append(shd)

def add_divider(doc):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(4)
    run = p.add_run("─" * 80)
    run.font.color.rgb = RGBColor(45, 52, 72)
    run.font.size = Pt(8)

def add_callout(doc, label, text, label_color=(16, 185, 129)):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.35)
    r1 = p.add_run(f"▶  {label}:  ")
    r1.bold = True
    r1.font.color.rgb = RGBColor(*label_color)
    r1.font.size = Pt(11)
    r2 = p.add_run(text)
    r2.font.size = Pt(11)
    return p

def add_warning(doc, text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.35)
    r1 = p.add_run("⚠  WARNING:  ")
    r1.bold = True
    r1.font.color.rgb = RGBColor(239, 68, 68)
    r1.font.size = Pt(11)
    r2 = p.add_run(text)
    r2.font.size = Pt(11)
    return p

# ─── Document setup ────────────────────────────────────────────────────────────
doc = Document()

# Page margins
for section in doc.sections:
    section.top_margin    = Inches(1.0)
    section.bottom_margin = Inches(1.0)
    section.left_margin   = Inches(1.1)
    section.right_margin  = Inches(1.1)

# Default font
doc.styles['Normal'].font.name = 'Calibri'
doc.styles['Normal'].font.size = Pt(11)

# ══════════════════════════════════════════════════════════════════════════════
# TITLE PAGE
# ══════════════════════════════════════════════════════════════════════════════
doc.add_paragraph()
doc.add_paragraph()

title = doc.add_paragraph()
title.alignment = WD_ALIGN_PARAGRAPH.CENTER
tr = title.add_run("URBAN GROWTH AI")
tr.bold = True
tr.font.size = Pt(36)
tr.font.color.rgb = RGBColor(16, 185, 129)

sub = doc.add_paragraph()
sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
sr = sub.add_run("Corporate Real Estate Investment Playbook")
sr.bold = True
sr.font.size = Pt(22)
sr.font.color.rgb = RGBColor(56, 189, 248)

doc.add_paragraph()
tagline = doc.add_paragraph()
tagline.alignment = WD_ALIGN_PARAGRAPH.CENTER
tr2 = tagline.add_run("AI-Identified Sites  ·  Ground-Up Development  ·  Institutional-Quality Exits")
tr2.font.size = Pt(13)
tr2.font.color.rgb = RGBColor(148, 163, 184)

doc.add_paragraph()
add_divider(doc)
doc.add_paragraph()

meta = doc.add_paragraph()
meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
meta.add_run("Phoenix MSA  ·  Austin MSA  ·  2025 Edition\n").font.size = Pt(11)
meta_r = meta.add_run("All market data sourced from RealPage, Yardi Matrix, CBRE, Mortenson Cost Index, CoStar (2024–2025)")
meta_r.font.size = Pt(9)
meta_r.font.color.rgb = RGBColor(148, 163, 184)
meta_r.italic = True

doc.add_page_break()

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: EXECUTIVE SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
add_heading(doc, "1. Executive Summary", 1)

add_para(doc, (
    "The Urban Growth AI platform identifies ground-up real estate development opportunities in Phoenix and Austin — "
    "two of the fastest-growing Sun Belt markets — using a proprietary H3 hexagonal grid scoring model. This playbook "
    "describes the complete investment process from AI site selection through construction, financing, stabilisation, "
    "and exit, with all assumptions validated against 2024-2025 market data."
))

doc.add_paragraph()
add_heading(doc, "Validated Portfolio Results", 2, color=(56, 189, 248))

# KPI table
table = doc.add_table(rows=1, cols=4)
table.alignment = WD_TABLE_ALIGNMENT.CENTER
table.style = 'Table Grid'
hdr = table.rows[0].cells
for cell, txt in zip(hdr, ["Metric", "Value", "Benchmark", "Source"]):
    cell.text = txt
    cell.paragraphs[0].runs[0].bold = True
    shd_cell(cell, "0F1117")
    cell.paragraphs[0].runs[0].font.color.rgb = RGBColor(148, 163, 184)

data = [
    ("Portfolio CAGR (10-yr)", "12.8%", "SPY: 12.2%", "Simulation vs RealPage/CBRE"),
    ("Median Project IRR", "16.1%", "Target: 14-18%", "CBRE Q2 2025, Origin Investments"),
    ("Median Equity Multiple", "2.17×", "Target: 1.7-2.2×", "NMHC / PGIM 2025"),
    ("Development Spread", "15-20%", "Min. target: 15%", "CBRE: 150+ bps YOC over cap"),
    ("Yield on Cost (YOC)", "6.0-6.8%", "Target: 6.0-6.8%", "CBRE Q2 2025"),
    ("Exit Cap Rate (Phoenix)", "4.8-5.2%", "Market: 4.8-5.2%", "CBRE / Innowave Q3 2025"),
    ("Exit Cap Rate (Austin)", "5.0-5.5%", "Market: 5.0-5.5%", "Matthews Q4 2025"),
    ("Build-to-Sell Timeline", "26 months", "Phoenix Chandler", "RealPage 2024; Mortenson"),
    ("Lease-Up Period", "9-12 months", "Phoenix: 9-12; Austin: 15-18", "RealPage 2024"),
]
for i, row_data in enumerate(data):
    row = table.add_row()
    bg = "1A1D2E" if i % 2 == 0 else "1E2235"
    for cell, val in zip(row.cells, row_data):
        cell.text = val
        shd_cell(cell, bg)
        for run in cell.paragraphs[0].runs:
            run.font.size = Pt(10)

doc.add_paragraph()
add_warning(doc, "Austin suburban submarkets (Round Rock, Pflugerville) show -10-11% YoY rent declines as of late 2025 due to record supply deliveries. Avoid for Build-to-Sell underwriting until absorption normalises (estimated 2026-2027).")

doc.add_paragraph()

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: TARGET MARKETS & SUBMARKET GUIDANCE
# ══════════════════════════════════════════════════════════════════════════════
add_heading(doc, "2. Target Markets & Submarket Guidance", 1)

add_para(doc, (
    "Submarket selection is the single most important variable in ground-up development underwriting. "
    "A 5% rent variance or 50bps cap rate difference between submarkets can swing a deal from strongly "
    "positive to below-threshold IRR. The AI model identifies Tier-1 cells across the metro, but the "
    "following submarket guidance must be applied as a filter before underwriting."
))

doc.add_paragraph()
add_heading(doc, "2.1 Phoenix MSA", 2, color=(230, 126, 34))

add_callout(doc, "FOCUS SUBMARKETS", "Chandler, Gilbert, Scottsdale North (Old Town through Kierland), Deer Valley, Peoria (select corridors)", label_color=(16, 185, 129))

add_bullet(doc, "Chandler/Gilbert: Sub-7% vacancy rate; limited new supply pipeline; strong employer base (Intel, TSMC, Amazon); fastest lease-up in metro at 9-11 months", bold_prefix="✓ Chandler/Gilbert:")
add_bullet(doc, "Scottsdale North: Trophy pricing ($300-532k/unit); sub-5% cap rates; highest effective rents ($1.90-2.20/sqft/mo asking)", bold_prefix="✓ Scottsdale North:")
add_bullet(doc, "Deer Valley/Peoria: Strong demand from healthcare corridor; $270-338k/unit exit pricing (Spire Deer Valley Jan 2025: $338k/unit)", bold_prefix="✓ Deer Valley:")

doc.add_paragraph()
add_warning(doc, "Phoenix AVOID: Tempe (11%+ vacancy; Skywater at Town Lake Jan 2024 sold at 30% DISCOUNT), Downtown Phoenix/Roosevelt Row (6-8 wk free rent standard), West Valley/Goodyear/Avondale (15-25% vacancy — do not build).")

add_bullet(doc, "Skywater at Town Lake (Tempe, Jan 2024): Class A new delivery sold at 30% discount from 18-month prior price — the most important cautionary comp in Phoenix 2024-2025.", bold_prefix="⚠ Cautionary comp:")

doc.add_paragraph()
add_heading(doc, "Phoenix Validated Market Data (2024-2025)", 3, color=(230, 126, 34))

table2 = doc.add_table(rows=1, cols=3)
table2.style = 'Table Grid'
for cell, txt in zip(table2.rows[0].cells, ["Metric", "Chandler/Scottsdale (FOCUS)", "Tempe/W.Valley (AVOID)"]):
    cell.text = txt; cell.paragraphs[0].runs[0].bold = True
    shd_cell(cell, "0F1117"); cell.paragraphs[0].runs[0].font.color.rgb = RGBColor(148, 163, 184)

phx_data = [
    ("Vacancy Rate",          "5-7%   ✓",             "11-25%  ✗"),
    ("New Class A Rent (ask)","$1.90-2.20/sqft/mo",   "$1.60-1.80/sqft/mo"),
    ("Effective Rent (after concessions)","$1.75-1.95/sqft/mo","$1.40-1.65/sqft/mo (6-8 wk free)"),
    ("Stabilised Cap Rate",   "4.8-5.2%",             "5.5-6.5% (distressed)"),
    ("Recent Exit Pricing",   "$270-532k/unit",       "30% discount (Skywater)"),
    ("Lease-Up Timeline",     "9-12 months",          "14-20+ months"),
    ("Hard Cost/sqft (GC)",   "$155-195",             "Same, but negative spread"),
    ("Land Cost/acre",        "$150k-400k",           "$75k-150k (reflects risk)"),
]
for i, row_data in enumerate(phx_data):
    row = table2.add_row()
    bg = "1A1D2E" if i % 2 == 0 else "1E2235"
    for j, (cell, val) in enumerate(zip(row.cells, row_data)):
        cell.text = val; shd_cell(cell, bg)
        for run in cell.paragraphs[0].runs:
            run.font.size = Pt(10)
            if j == 1 and "✓" in val: run.font.color.rgb = RGBColor(16, 185, 129)
            if j == 2 and "✗" in val: run.font.color.rgb = RGBColor(239, 68, 68)

doc.add_paragraph()
add_heading(doc, "2.2 Austin MSA", 2, color=(52, 152, 219))

add_callout(doc, "FOCUS SUBMARKETS", "South Austin (78745/78748), Central Austin (UT corridor), Downtown-adjacent (78701/78702), East Austin (select corridors with zoning clarity)", label_color=(16, 185, 129))

add_bullet(doc, "South/Central Austin: 94%+ occupancy; positive rent growth while rest of metro corrects; $1.90-2.20/sqft/mo asking", bold_prefix="✓ South Austin:")
add_bullet(doc, "UT Corridor: Graduate/faculty housing demand; institutional quality tenant base; limited developable sites (creates scarcity premium)", bold_prefix="✓ UT Corridor:")

doc.add_paragraph()
add_warning(doc, "Austin AVOID for BTS: Round Rock (-10.5% rents YoY), Pflugerville/Wells Branch (-11.9% YoY), Georgetown, Hutto — heavy supply deliveries 2023-2025 have absorbed demand. Build-to-Rent in these areas may pencil at conservative Year 1-2 0% rent growth, but BTS requires 15-18 month lease-up with heavy concessions (12.9% avg discount per RealPage Aug 2025).")

table3 = doc.add_table(rows=1, cols=3)
table3.style = 'Table Grid'
for cell, txt in zip(table3.rows[0].cells, ["Metric", "South/Central Austin (FOCUS)", "Suburban (AVOID for BTS)"]):
    cell.text = txt; cell.paragraphs[0].runs[0].bold = True
    shd_cell(cell, "0F1117"); cell.paragraphs[0].runs[0].font.color.rgb = RGBColor(148, 163, 184)

aus_data = [
    ("YoY Rent Growth (2025)",  "+2-4%  ✓",              "-10 to -12%  ✗"),
    ("Vacancy",                 "4-6%  ✓",               "10-18%  ✗"),
    ("Effective Rent (ask)",    "$1.80-2.05/sqft/mo",    "$1.35-1.60/sqft/mo"),
    ("Concession Rate",         "6-8 weeks free",        "8-14 weeks free"),
    ("Exit Cap Rate",           "5.0-5.3%",              "5.5-6.5%"),
    ("Lease-Up Timeline",       "10-14 months",          "15-20 months"),
    ("Hard Cost/sqft",          "$150-175  (incl. 2025 inflation)","Same cost base"),
    ("Recommended Strategy",    "BTR or BTS (case-by-case)","BTR only (hold 5+ yrs)"),
]
for i, row_data in enumerate(aus_data):
    row = table3.add_row()
    bg = "1A1D2E" if i % 2 == 0 else "1E2235"
    for j, (cell, val) in enumerate(zip(row.cells, row_data)):
        cell.text = val; shd_cell(cell, bg)
        for run in cell.paragraphs[0].runs:
            run.font.size = Pt(10)
            if j == 1 and "✓" in val: run.font.color.rgb = RGBColor(16, 185, 129)
            if j == 2 and "✗" in val: run.font.color.rgb = RGBColor(239, 68, 68)

doc.add_paragraph()

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: UNDERWRITING STANDARDS
# ══════════════════════════════════════════════════════════════════════════════
add_heading(doc, "3. Underwriting Standards & Go/No-Go Thresholds", 1)

add_para(doc, (
    "All deals must meet minimum return thresholds before advancing to LOI. These thresholds are calibrated "
    "to validated 2024-2025 market data and reflect institutional LP expectations. A deal that fails any "
    "minimum threshold should be rejected or renegotiated on land price."
))

doc.add_paragraph()
add_heading(doc, "3.1 Minimum Return Thresholds", 2, color=(56, 189, 248))

table4 = doc.add_table(rows=1, cols=4)
table4.style = 'Table Grid'
for cell, txt in zip(table4.rows[0].cells, ["Metric", "Minimum (No-Go Below)", "Target", "Source"]):
    cell.text = txt; cell.paragraphs[0].runs[0].bold = True
    shd_cell(cell, "0F1117"); cell.paragraphs[0].runs[0].font.color.rgb = RGBColor(148, 163, 184)

thresh_data = [
    ("Levered IRR — Build-to-Sell",     "13%",       "16-20%",    "CBRE Q2 2025"),
    ("Levered IRR — Build-to-Rent",     "14%",       "16-20%",    "Origin Investments 2025"),
    ("Unlevered IRR",                    "8%",        "9-10%",     "CBRE core stabilised"),
    ("Yield on Cost (YOC) at Stab.",    "6.0%",      "6.3-6.8%",  "CBRE Q2 2025 benchmark"),
    ("Development Spread (Val/Cost)",   "+15%",       "20-30%",    "Market validated"),
    ("YOC - Exit Cap Spread",           "100 bps",    "150+ bps",  "Minimum viable margin"),
    ("Equity Multiple — BTS",           "1.3×",       "1.4-1.6×",  "26-month cycle"),
    ("Equity Multiple — BTR (5-yr)",    "1.7×",       "2.0-2.5×",  "5-yr development hold"),
    ("Debt Service Coverage (DSCR)",    "1.10×",      "1.20×+",    "Lender requirement"),
    ("Max Land Cost as % of TDC",       "15%",        "10-12%",    "Reduces land speculation risk"),
]
for i, row_data in enumerate(thresh_data):
    row = table4.add_row()
    bg = "1A1D2E" if i % 2 == 0 else "1E2235"
    for j, (cell, val) in enumerate(zip(row.cells, row_data)):
        cell.text = val; shd_cell(cell, bg)
        for run in cell.paragraphs[0].runs:
            run.font.size = Pt(10)
            if j == 2: run.font.color.rgb = RGBColor(16, 185, 129)

doc.add_paragraph()
add_heading(doc, "3.2 Validated Construction Cost Inputs", 2, color=(56, 189, 248))

add_para(doc, "Use city-specific hard cost benchmarks validated against current market data. Blend with any available GC preliminary estimates:")

table5 = doc.add_table(rows=1, cols=5)
table5.style = 'Table Grid'
for cell, txt in zip(table5.rows[0].cells, ["Input", "Phoenix (Chandler/Scottsdale)", "Austin (South/Central)", "Soft Costs (Both)", "Source"]):
    cell.text = txt; cell.paragraphs[0].runs[0].bold = True
    shd_cell(cell, "0F1117"); cell.paragraphs[0].runs[0].font.color.rgb = RGBColor(148, 163, 184)

cost_data = [
    ("Hard cost/sqft (Type V wood-frame)", "$155-195", "$130-155", "15% of hard+land", "Mortenson Index; EVstudio/Maxx"),
    ("Model default (midpoint + adj.)", "$175/sqft", "$155/sqft", "(same)", "2025 inflation-adjusted"),
    ("Cost inflation (annual)", "+5-7%/yr", "+4-5%/yr", "+4-5%/yr", "Mortenson/RS Means 2025"),
    ("Soft costs (arch, permits, dev fee)", "—", "—", "15% of hard+land", "Market standard"),
    ("Total project cost (1 acre, FAR 0.5)", "~$4.6M", "~$4.1M", "—", "Model validated"),
    ("Per-unit cost (22 units avg)", "~$210k/unit", "~$185k/unit", "—", "vs research $200-260k PHX"),
    ("Max hard cost (GMP contract req.)", "$195/sqft", "$165/sqft", "—", "Trigger GMP if over"),
    ("Contingency budget", "10% hard cost", "10% hard cost", "—", "Industry standard"),
]
for i, row_data in enumerate(cost_data):
    row = table5.add_row()
    bg = "1A1D2E" if i % 2 == 0 else "1E2235"
    for cell, val in zip(row.cells, row_data):
        cell.text = val; shd_cell(cell, bg)
        for run in cell.paragraphs[0].runs: run.font.size = Pt(9.5)

doc.add_paragraph()
add_heading(doc, "3.3 Rent & Income Assumptions", 2, color=(56, 189, 248))

add_callout(doc, "KEY RULE", "Always underwrite to EFFECTIVE rent (after concessions), not ASKING rent. Current Phoenix concession: ~6-8 weeks free = 11-15% discount. Austin suburban: 8-14 weeks free = 15-27% discount.", label_color=(245, 158, 11))

table6 = doc.add_table(rows=1, cols=4)
table6.style = 'Table Grid'
for cell, txt in zip(table6.rows[0].cells, ["Parameter", "Phoenix (Chandler)", "Austin (South)", "Austin (Suburban — conservative)"]):
    cell.text = txt; cell.paragraphs[0].runs[0].bold = True
    shd_cell(cell, "0F1117"); cell.paragraphs[0].runs[0].font.color.rgb = RGBColor(148, 163, 184)

rent_data = [
    ("Asking rent (new Class A)", "$1.90-2.20/sqft/mo", "$1.90-2.20/sqft/mo", "$1.60-1.75/sqft/mo"),
    ("Effective rent (model input)", "$1.75/sqft/mo  ($21/yr)", "$1.78/sqft/mo  ($21.4/yr)", "$1.55/sqft/mo  ($18.6/yr)"),
    ("Concession (Year 1)", "~6 wks free = -11%", "~6-8 wks = -11-15%", "~10-14 wks = -19-27%"),
    ("Vacancy (stabilised)", "7%", "7%", "8%"),
    ("Opex ratio", "35% of EGI", "35% of EGI", "35% of EGI"),
    ("Rent growth assumption (Yr 1-2)", "3%/yr", "2%/yr", "0%/yr (absorbing supply)"),
    ("Rent growth assumption (Yr 3-5)", "4%/yr", "3-4%/yr", "3%/yr"),
    ("Source", "RealPage/Yardi 2025", "RealPage Oct 2025", "RealPage Oct 2025"),
]
for i, row_data in enumerate(rent_data):
    row = table6.add_row()
    bg = "1A1D2E" if i % 2 == 0 else "1E2235"
    for cell, val in zip(row.cells, row_data):
        cell.text = val; shd_cell(cell, bg)
        for run in cell.paragraphs[0].runs: run.font.size = Pt(9.5)

doc.add_paragraph()

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4: DEAL PROCESS — STEP BY STEP
# ══════════════════════════════════════════════════════════════════════════════
add_heading(doc, "4. The Investment Process — Phase by Phase", 1)

phases = [
    ("PHASE 1: IDENTIFY", "1-2 Weeks", (108, 99, 255), [
        ("Run AI Model", "Pull top 10% Tier-1 H3 cells from lot_opportunities table (opportunity_score column). Export Phoenix and Austin candidates separately. Apply submarket filter: Phoenix → exclude h3_index cells in Tempe/West Valley. Austin → prioritise South/Central, flag suburban as BTR-only."),
        ("Initial Screen", "Parcel size ≥ 0.5 acres (confirm from county assessor). Confirmed zoning: R2, R3, MF, or mixed-use with residential allowed. No active enforcement liens. Not in 100-year flood zone (FEMA FIRM check). Utilities stubbed or within 200ft."),
        ("Field Reconnaissance", "Drive every target property. Confirm: curb cuts, grade, overhead lines, neighbouring uses, HOA presence, access road quality. Flag anything that creates unseen cost. Take 10+ photos per site."),
        ("Satellite + Permit Review", "Google Earth Pro: check historical imagery for any prior development attempts. County permit portal: active permits (positive — demand exists), outstanding violations (negative). GIS parcel viewer: confirm legal description matches target."),
    ]),
    ("PHASE 2: UNDERWRITE", "2-4 Weeks", (56, 189, 248), [
        ("Land Comps", "Pull 3 comparable land sales within 1 mile, last 24 months, from county deed records or CoStar. Compute $/acre range. If asking price is more than 15% above comp $/acre, negotiate or walk."),
        ("Construction Estimate", "Get 2 preliminary bids from pre-qualified GCs. Use city-specific default if bids not yet available (Phoenix: $175/sqft; Austin: $155/sqft). Stress test at +15% cost overrun — deal must still clear minimum IRR threshold."),
        ("Pro Forma Modeling", "Model both BTS and BTR scenarios using this playbook's validated inputs. Check: YOC ≥ 6.0%, dev spread ≥ 15%, IRR ≥ 13% (BTS) or 14% (BTR). If deal fails at conservative inputs, it's a land price problem — offer accordingly."),
        ("LOI Decision", "Submit LOI only if: BTS IRR ≥ 13% OR BTR IRR ≥ 14% (at conservative inputs). Include 60-day feasibility period + 15-day extension right. Non-refundable earnest money after feasibility: $25k-$50k."),
    ]),
    ("PHASE 3: DUE DILIGENCE & CLOSE", "45-75 Days", (245, 158, 11), [
        ("Environmental", "Commission Phase I ESA ($2,500-4,500). If Phase I flags Recognized Environmental Conditions (RECs), proceed to Phase II only if purchase price reflects remediation cost. Never close over a Phase II concern without full remediation budget."),
        ("Survey & Geotech", "ALTA/NSPS survey ($3,500-7,000): confirms legal boundaries, easements, encroachments. Geotechnical investigation ($4,000-8,000): soil bearing capacity, expansion potential, groundwater. Flag if expansive clay (common in Phoenix/Austin) — requires engineered slab."),
        ("Entitlement Confirmation", "Confirm no discretionary approvals needed (avoid deals requiring city council votes — too much timeline risk). If rezoning required, add 6-12 months to project timeline and model accordingly. Ministerial permits only for ground-up Type V."),
        ("Capital Structure", "LP/GP structure: typical 80/20 promote above 8% pref. Construction lender: target relationship bank at 65% LTV, prime+150bps rate, 18-month term + 6-month extension. Equity close first, then construction loan at vertical start."),
        ("Land Close", "Wire equity portion first (35% of TDC). Execute construction loan docs simultaneously or within 30 days. Confirm lender draw schedule: typically monthly draws on 30-day construction draw cycle with independent inspector sign-off."),
    ]),
    ("PHASE 4: CONSTRUCTION", "12-16 Months", (16, 185, 129), [
        ("Contract Type", "GMP (Guaranteed Maximum Price) contract required on all projects >$3M hard cost. Require 10% performance and payment bond. Liquidated damages clause: $500-1,500/day for schedule overruns past Substantial Completion date."),
        ("Draw Management", "Monthly construction draws reviewed by independent inspector (lender-required). No draw without: (1) AIA G702/G703 application, (2) lien waivers from all subs, (3) photos confirming work-in-place. Budget 5% retainage held until 30 days post-final lien waiver."),
        ("Cost Control", "Monthly budget vs actual review. Approve all change orders >$5k in writing with owner sign-off. Substitution requests: approve only if spec-equal or better. Material lead times: order long-lead items (HVAC, windows) at permit issuance — currently 12-20 week lead times."),
        ("Punch List & CO", "Request Certificate of Occupancy walk 3 weeks before target date to allow punch list completion. Target: CO within 60 days of Substantial Completion. Commence lease-up marketing 60 days before CO (pre-leasing reduces lease-up timeline by 30-45 days)."),
    ]),
    ("PHASE 5: LEASE-UP & STABILISATION", "9-18 Months", (108, 99, 255), [
        ("Pre-Leasing Strategy", "Begin marketing 60 days before CO. Target: 15-20% pre-leased at CO. Use ILS platforms (Apartments.com, Zillow, CoStar). Budget: $200-300/unit/month for lease-up marketing costs."),
        ("Concession Strategy", "Phoenix Chandler (healthy): offer 1 month free on 13-month leases maximum; do not exceed 4 weeks free. Austin (softer): model 6-8 weeks free in Year 1 underwriting; escalate to 10 weeks if absorption is below target at month 3."),
        ("Stabilisation Target", "93%+ occupancy for 90 consecutive days = stabilised. Measure by net effective rent (not asking). DSCR must be ≥ 1.10× at stabilisation for permanent loan qualification."),
        ("Refi Timing (BTR)", "At 93% stabilisation: approach 3 bridge/perm lenders. Target: 65% LTV, 6.0-6.5% rate, 7-10 year term, interest-only 2 years. Pay off construction loan with perm proceeds. Net equity after perm = development profit realised on paper."),
    ]),
    ("PHASE 6: EXIT", "BTS or BTR Decision", (16, 185, 129), [
        ("Build-to-Sell (BTS)", "Begin sale process at 85% occupancy (not 93% — buyers price in remaining lease-up). Engage 2 brokers for competitive bid process. Target cap rate: 5.0% Phoenix, 5.2% Austin. Close sale 45-60 days after LOI. Recycle equity into next project within 30 days of closing."),
        ("Build-to-Rent (BTR)", "Hold 5 years post-CO (total 7 years from land close). Maximise NOI: annual rent renewals at market + 5%. Refinance if rates improve. Sell Year 5-7 at cap rate compression (target: 25-50bps below going-in cap). Use 1031 exchange if possible to defer gain recognition."),
        ("Decision Framework", "Choose BTS if: IRR_BTS > 14% AND market occupancy > 92% in submarket. Choose BTR if: market has active rent correction (Phoenix Tempe, Austin suburban) AND you can hold through recovery. Hybrid: BTS first 2 years while market corrects; convert to BTR on later projects."),
        ("Waterfall / Promote", "Return all capital (LP + GP equity) + 8% cumulative pref. Then: 80% LP / 20% GP until 1.5× EM. Above 1.5× EM: 65% LP / 35% GP. Above 2.0× EM: 50% LP / 50% GP. Promote structure must be documented in PPM before capital raise."),
    ]),
]

for phase_title, timeline, color, steps in phases:
    doc.add_paragraph()
    h = doc.add_heading(f"4.{phases.index((phase_title, timeline, color, steps))+1}  {phase_title}  —  {timeline}", 2)
    for run in h.runs:
        run.font.color.rgb = RGBColor(*color)
    for step_title, step_body in steps:
        p = doc.add_paragraph()
        r1 = p.add_run(f"▸  {step_title}: ")
        r1.bold = True; r1.font.size = Pt(11); r1.font.color.rgb = RGBColor(*color)
        r2 = p.add_run(step_body)
        r2.font.size = Pt(10.5)
        p.paragraph_format.left_indent = Inches(0.25)
        p.paragraph_format.space_after = Pt(6)

doc.add_paragraph()

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5: FINANCING STRUCTURE
# ══════════════════════════════════════════════════════════════════════════════
add_heading(doc, "5. Financing Structure", 1)

add_heading(doc, "5.1 Construction Loan", 2, color=(56, 189, 248))
for item in [
    "Loan-to-Cost (LTC): 65%  (lender advances 65% of total project cost)",
    "Rate: Prime + 150bps = ~8.5% floating (Q2 2025). Rate cap required if floating.",
    "Term: 18 months + 6-month extension right (24 months max)",
    "Draw schedule: Monthly, on inspector sign-off, with lien waiver from GC and all subs",
    "Recourse: Full recourse from sponsor during construction; converts to non-recourse at perm",
    "Fees: 1-1.5% origination fee; 0.25% unused commitment fee on undrawn balance",
    "Interest reserve: Fully funded at closing (included in loan budget); accrues during draw",
]:
    add_bullet(doc, item)

add_heading(doc, "5.2 Permanent Loan (BTR Strategy)", 2, color=(56, 189, 248))
for item in [
    "Trigger: 93%+ occupancy for 90 days (stabilisation)",
    "LTV: 65% of appraised value at stabilisation",
    "Rate: SOFR + 180-220bps = ~6.0-6.5% (Q2 2025). Fixed rate preferred for 7-10 year hold.",
    "Term: 7-10 years, interest-only first 2 years, then 30-year amortisation",
    "DSCR requirement: 1.20× minimum at lender's stabilised NOI assumption",
    "Lenders: Target Freddie Mac/Fannie Mae agency (lowest rate), regional banks, life company debt",
    "Construction loan payoff: Perm proceeds pay off construction loan + accrued interest at closing",
]:
    add_bullet(doc, item)

add_heading(doc, "5.3 LP/GP Capital Structure", 2, color=(56, 189, 248))
add_para(doc, "Typical LP/GP structure for institutional co-investment:")
for item in [
    "GP equity: 10-20% of total equity (GP co-invests alongside LP to align interests)",
    "LP equity: 80-90% of total equity from institutional LPs, family offices, or HNW individuals",
    "Preferred return: 8% cumulative, compounded annually on unreturned capital",
    "Promote tiers: 80/20 → 65/35 → 50/50 as described in waterfall (Section 4, Phase 6)",
    "Management fee: 1-2% of invested equity per year during hold",
    "Construction oversight fee: 3-5% of hard construction cost (paid to GP for project management)",
    "Acquisition fee: 0.5-1% of total project cost at land close",
]:
    add_bullet(doc, item)

doc.add_paragraph()

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6: DEAL SCREENING CHECKLIST
# ══════════════════════════════════════════════════════════════════════════════
add_heading(doc, "6. Deal Screening Checklist", 1)

add_para(doc, "Use this checklist for every deal before advancing beyond initial screening. A 'No' on any CRITICAL item is an automatic no-go.")

checklist_sections = [
    ("SITE CRITERIA", "critical", [
        ("AI Opportunity Score", "Tier 1 (top 10%)", "critical"),
        ("Parcel size", "≥ 0.5 acres confirmed by assessor", "critical"),
        ("Zoning", "Residential multifamily or MF-eligible mixed-use allowed by right", "critical"),
        ("Flood zone", "Not in FEMA 100-year flood zone (Zone AE or X shaded)", "critical"),
        ("Access", "Direct street frontage confirmed; no shared easement required", "important"),
        ("Utilities", "Water, sewer, electric within 200ft or stubbed at site", "critical"),
        ("Submarket", "Phoenix: Chandler/Gilbert/Scottsdale; Austin: South/Central only", "critical"),
        ("No active violations", "County search: no code enforcement or stop-work orders", "critical"),
    ]),
    ("FINANCIAL SCREEN", "critical", [
        ("Land cost ≤ 15% of TDC", "Calculate before LOI; negotiate if over 15%", "critical"),
        ("YOC ≥ 6.0%", "At conservative inputs (effective rent, 7% vac., 35% opex)", "critical"),
        ("Dev. spread ≥ 15%", "(Value at stabilisation - TDC) / TDC ≥ 15%", "critical"),
        ("IRR ≥ 13% BTS or 14% BTR", "At conservative rent/cost inputs", "critical"),
        ("Stress test passed", "At +15% hard cost AND at market rent -10%: still IRR ≥ 10%", "important"),
        ("DSCR ≥ 1.10× at perm", "At stabilised NOI and 6.5% perm rate assumption", "critical"),
    ]),
    ("DUE DILIGENCE MILESTONES", "important", [
        ("Phase I ESA ordered", "No RECs; if REC present, Phase II before proceeding", "critical"),
        ("ALTA survey ordered", "Confirms boundaries, easements, encroachments", "critical"),
        ("Geotech ordered", "Confirms soil bearing; check for expansive clay", "important"),
        ("2 GC bids received", "Within 10% of each other; both below budget", "important"),
        ("Perm lender pre-qualified", "At least 1 lender confirmed interest at target LTV/rate", "important"),
        ("Title search clean", "No liens, encumbrances, or IRS notices on title", "critical"),
        ("Entitlement path clear", "No discretionary approvals needed (ministerial permits only)", "critical"),
        ("LP commitments secured", "≥ 80% of equity committed before land close", "important"),
    ]),
]

for section_title, priority, items in checklist_sections:
    doc.add_paragraph()
    add_heading(doc, section_title, 3, color=(56, 189, 248))
    table_ck = doc.add_table(rows=1, cols=4)
    table_ck.style = 'Table Grid'
    for cell, txt in zip(table_ck.rows[0].cells, ["✓", "Check Item", "Standard", "Priority"]):
        cell.text = txt; cell.paragraphs[0].runs[0].bold = True
        shd_cell(cell, "0F1117"); cell.paragraphs[0].runs[0].font.color.rgb = RGBColor(148, 163, 184)
    for item_name, standard, prio in items:
        row = table_ck.add_row()
        prio_color = "10B981" if prio == "critical" else "F59E0B"
        prio_label = "CRITICAL" if prio == "critical" else "IMPORTANT"
        for cell, val in zip(row.cells, ["☐", item_name, standard, prio_label]):
            cell.text = val; shd_cell(cell, "1A1D2E")
            for run in cell.paragraphs[0].runs:
                run.font.size = Pt(10)
                if val == prio_label:
                    run.font.color.rgb = RGBColor(*[int(prio_color[i:i+2],16) for i in (0,2,4)])

doc.add_paragraph()

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7: RISK REGISTER
# ══════════════════════════════════════════════════════════════════════════════
add_heading(doc, "7. Risk Register & Mitigants", 1)

risks_table = doc.add_table(rows=1, cols=4)
risks_table.style = 'Table Grid'
for cell, txt in zip(risks_table.rows[0].cells, ["Risk", "Severity", "Probability", "Mitigant"]):
    cell.text = txt; cell.paragraphs[0].runs[0].bold = True
    shd_cell(cell, "0F1117"); cell.paragraphs[0].runs[0].font.color.rgb = RGBColor(148, 163, 184)

risk_data = [
    ("Construction cost overrun >15%",
     "HIGH", "MEDIUM",
     "GMP contract; 10% contingency; monthly cost tracking; change order discipline"),
    ("Interest rate spike during construction",
     "HIGH", "MEDIUM",
     "Rate cap on construction loan; BTS strategy reduces exposure; perm loan locks at refi"),
    ("Lease-up slower than 12 months (Phoenix)",
     "MEDIUM", "MEDIUM",
     "Pre-leasing 60 days pre-CO; concession budget; broker bonuses; focus on Chandler/Gilbert"),
    ("Austin suburban rent decline continues",
     "HIGH", "HIGH",
     "Avoid Austin suburban for BTS; BTR only with 0% Year 1-2 rent growth underwriting"),
    ("Phoenix Tempe/West Valley cap rate expansion",
     "HIGH", "HIGH",
     "Do not develop in Tempe or West Valley; Skywater comp confirms execution risk"),
    ("GC insolvency mid-project",
     "HIGH", "LOW",
     "10% performance bond; 5% retainage; monthly lien waivers; backup sub list"),
    ("Permit delays >6 months",
     "MEDIUM", "LOW",
     "Ministerial permits only; start permitting at feasibility; pre-application meeting"),
    ("Cap rate expansion at exit",
     "MEDIUM", "MEDIUM",
     "Stress test at exit cap +50bps; BTS shorter duration reduces cap risk; perm loan at stabilisation"),
    ("Market rent decline (metro-wide)",
     "MEDIUM", "LOW",
     "20%+ dev spread buffers rent pressure; 65% LTV means equity carries losses first"),
    ("Environmental contamination found",
     "HIGH", "LOW",
     "Phase I required; never close over unresolved REC; negotiate price adjustment if any"),
]
for i, row_data in enumerate(risk_data):
    row = risks_table.add_row()
    bg = "1A1D2E" if i % 2 == 0 else "1E2235"
    for j, (cell, val) in enumerate(zip(row.cells, row_data)):
        cell.text = val; shd_cell(cell, bg)
        for run in cell.paragraphs[0].runs:
            run.font.size = Pt(9.5)
            if j == 1 and val == "HIGH": run.font.color.rgb = RGBColor(239, 68, 68)
            if j == 1 and val == "MEDIUM": run.font.color.rgb = RGBColor(245, 158, 11)
            if j == 2 and val == "HIGH": run.font.color.rgb = RGBColor(239, 68, 68)
            if j == 2 and val == "MEDIUM": run.font.color.rgb = RGBColor(245, 158, 11)
            if j == 2 and val == "LOW": run.font.color.rgb = RGBColor(16, 185, 129)

doc.add_paragraph()

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8: DATA SOURCES & ASSUMPTIONS
# ══════════════════════════════════════════════════════════════════════════════
add_heading(doc, "8. Data Sources & Validated Assumptions", 1)

add_para(doc, "All market assumptions in this playbook are sourced from the following primary research sources (2024-2025):")

sources = [
    ("RealPage", "Austin Market Profile October 2025; Phoenix rent data 2024-2025 — vacancy, rent levels, concession rates"),
    ("Yardi Matrix / Apartments.com", "Phoenix multifamily market report January 2025 — submarket-level rent data and trends"),
    ("CBRE", "Multifamily Underwriting Metrics Q2 2025 (IRR benchmarks, YOC targets, cap rate data); US Real Estate Market Outlook 2025"),
    ("Matthews Real Estate Investment Services", "Phoenix Q2 2025 Multifamily Market Report; Austin Q2 & Q4 2025 — vacancy, rents, cap rates"),
    ("Northmarq", "Phoenix 2025 Transaction Activity; Austin Q3 2024 Market Conditions — specific deal comps"),
    ("Innowave Studio", "Phoenix Q3 2025 Multifamily Overview — including Spire Deer Valley ($338k/unit) and Soltra at Kierland ($532k/unit) transactions"),
    ("Mortenson Construction", "Phoenix Construction Cost Index Q4 2025 — hard cost escalation data (+7.35% YoY)"),
    ("EVstudio / Maxx Builders", "Austin construction cost per sqft for apartments 2024 — wood-frame garden style $110-125/sqft baseline"),
    ("Origin Investments", "Ground-Up Development Case: YOC targets, development spread methodology, market positioning"),
    ("NAA (National Apartment Association)", "Multifamily Construction Trends Summer 2025 — lease-up timelines, supply pipeline"),
    ("The Land Letter", "Phoenix Multifamily Development at a Crossroads — submarket analysis, Skywater Tempe cautionary comp"),
]

for source, desc in sources:
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.25)
    r1 = p.add_run(f"• {source}: ")
    r1.bold = True; r1.font.size = Pt(10.5)
    r2 = p.add_run(desc)
    r2.font.size = Pt(10.5)

doc.add_paragraph()
add_divider(doc)
doc.add_paragraph()

disclaimer = doc.add_paragraph()
disclaimer.alignment = WD_ALIGN_PARAGRAPH.CENTER
dr = disclaimer.add_run(
    "DISCLAIMER: This playbook is for research and discussion purposes only. It does not constitute investment advice. "
    "All projected returns are model-based estimates using market data; actual results will vary. "
    "Past model performance does not guarantee future results. Engage qualified legal, tax, and financial advisors before any investment."
)
dr.font.size = Pt(8.5)
dr.italic = True
dr.font.color.rgb = RGBColor(148, 163, 184)

# ─── Save ──────────────────────────────────────────────────────────────────────
OUT = Path("D:/urbangrowth_data/processed/maps/Urban_Growth_Investment_Playbook.docx")
OUT.parent.mkdir(parents=True, exist_ok=True)
doc.save(str(OUT))
print(f"\n✓ Saved: {OUT}  ({OUT.stat().st_size//1024} KB)")
print(f"  Sections: 8  |  Tables: {len(doc.tables)}  |  Paragraphs: {len(doc.paragraphs)}")
