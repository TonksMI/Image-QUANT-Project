# Urban Growth Research Platform — Project Documentation

**Version:** 2.0 (May 2026)  
**Markets:** Phoenix (Chandler/Scottsdale) · Austin (South/Suburban)  
**Status:** Active — 2,229 opportunities identified; deck ready for Chapman University

---

## What This Is

A quantitative platform that identifies undervalued residential development land in Sun Belt metros using satellite imagery, permit data, and ML-based scoring. It combines:

1. **AI Lot Finder** — H3 resolution-8 hexagonal grid (≈211 acres/cell) scored by ML model for development opportunity
2. **Equity Signal** — Cross-sectional factor model for public REITs/homebuilders, Ridge regression, 6+ years backtest
3. **Portfolio Simulation** — Pro-forma IRR/NAV simulation for a 17-project Build-to-Rent / Build-to-Sell program

---

## Repository Structure

```
urbangrowth/
├── docs/                          ← You are here
│   ├── README.md                  ← This file
│   ├── methodology.md             ← Full technical methodology
│   ├── data_sources.md            ← Data sources and validation
│   ├── gap_analysis.md            ← 12-point gap analysis & model corrections
│   ├── deal_economics.md          ← Single-deal financial model
│   ├── portfolio_simulation.md    ← Portfolio simulation design
│   ├── opportunities_phoenix.md   ← Phoenix Tier-1 & Tier-2 opportunities
│   └── opportunities_austin.md    ← Austin Tier-1 & Tier-2 opportunities
├── urbangrowth/
│   ├── db/                        ← Database loaders (PostgreSQL)
│   ├── models/                    ← ML scoring models
│   └── pipeline/                  ← Data ingestion pipeline
├── generate_re_portfolio_graphic.py   ← Portfolio simulation & charts
├── generate_slide_deck.py             ← Investor slide deck (dark theme)
├── generate_chapman_deck.py           ← Chapman University presentation
├── generate_report.py                 ← HTML research report
├── gen_ic_and_lots.py                 ← IC backtest chart + top lots JSON
└── .env                               ← DB credentials (not committed)
```

---

## Quick Results

| Metric | Value |
|--------|-------|
| Portfolio NAV (Year 10) | **$29.7M** |
| Median BTR IRR | **14.3%** |
| BTS IRR (Phoenix top 40%) | **14.1%** |
| Portfolio CAGR | **11.5%** |
| Equity Multiple | **2.1×** |
| Active projects | **17** |
| Total opportunities | **2,229** (889 Tier-1, 1,340 Tier-2) |
| Phoenix Tier-1 | **478 cells** |
| Austin Tier-1 | **411 cells** |
| Spearman IC (Austin mean) | **+0.091** |
| Signal Sharpe vs SPY | **+0.23** |

---

## Outputs

| File | Description |
|------|-------------|
| `D:/urbangrowth_data/processed/maps/urban_growth_report.html` | Full HTML research report (1.3 MB) |
| `D:/urbangrowth_data/processed/maps/re_portfolio_comparison.png` | Portfolio vs SPY chart |
| `D:/urbangrowth_data/processed/maps/equity_curves.png` | Equity signal backtest |
| `D:/urbangrowth_data/processed/maps/bps_sensitivity.png` | BPS sensitivity analysis |
| `D:/urbangrowth_data/processed/maps/lot_backtest_ic.png` | IC backtest (Phoenix + Austin) |
| `D:/urbangrowth_data/processed/maps/Urban_Growth_Chapman_Deck.pptx` | 13-slide Chapman deck |

---

## How to Run

```powershell
# Activate environment
D:\envs\urbangrowth\python.exe

# Generate all outputs (run in order)
$env:PYTHONIOENCODING = "utf-8"
& "D:\envs\urbangrowth\python.exe" "C:\Users\17ton\urbangrowth\gen_ic_and_lots.py"
& "D:\envs\urbangrowth\python.exe" "C:\Users\17ton\urbangrowth\generate_re_portfolio_graphic.py"
& "D:\envs\urbangrowth\python.exe" "C:\Users\17ton\urbangrowth\generate_report.py"
& "D:\envs\urbangrowth\python.exe" "C:\Users\17ton\urbangrowth\generate_chapman_deck.py"
```

---

## Key Investment Thesis

1. **Population inflows** — Phoenix (+2.1%/yr) and Austin (+2.8%/yr) population growth driven by domestic migration
2. **Supply gap** — Both metros are under-permitting relative to household formation; Phoenix starts declined 18% YoY
3. **AI-identified land** — ML model identifies cells with high built_pct growth probability; 24-month walk-forward IC validates signal
4. **Structural rent support** — Effective rents $1.75–1.78/sqft/mo in Tier-1 submarkets; 3% annual growth Phoenix, 2% Austin
