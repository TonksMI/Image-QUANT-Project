# Technical Methodology

## 1. Geographic Grid

**H3 Resolution 8** hexagonal grid (Uber H3 library)
- Cell area: ≈211 acres (0.33 sq mi)
- Coverage: Phoenix MSA + Austin MSA
- Total cells: ~15,000 (both metros combined)
- Database: PostgreSQL (`lot_opportunities` table, joined to `cities`)

Each cell stores:
- `h3_index` — unique hex identifier
- `opportunity_score` — ML model output [0, 1]
- `tier` — Tier 1 (top 10%), Tier 2 (top 10–25%)
- `acreage_est` — effective developable area
- `dist_to_center_km` — distance from CBD
- `dominant_property_type` — residential / commercial / vacant / open_space / unknown
- `est_land_value_acre` — estimated $/acre
- `est_construction_months` — build timeline
- `est_cost_per_sqft` — hard construction cost estimate

---

## 2. Opportunity Scoring Model

### Feature Engineering
Raw features (H3 cell level):
- `built_pct` — fraction of cell covered by built structures (Sentinel-2 derived)
- `built_pct_change_12mo` — trailing 12-month built% change (development momentum)
- `permit_count_3yr` — residential permit count trailing 3 years
- `dist_to_transit_km` — distance to nearest transit stop
- `dist_to_employer_km` — distance to nearest major employer cluster
- `median_income_tract` — ACS tract-level median household income
- `job_growth_rate_3yr` — trailing job growth (BLS QCEW)
- `pop_growth_rate_3yr` — trailing population growth (ACS)
- `land_entropy` — Shannon entropy of land use mix (diversity index)
- `slope_deg` — terrain slope (USGS DEM)
- `flood_zone` — FEMA flood zone flag

### Model Architecture
- **Primary**: Gradient Boosted Machine (GBM, XGBoost)
  - Walk-forward cross-validation (expanding window)
  - Target: `built_pct_change_24mo` (forward-looking)
  - AUC: 0.58 (Phoenix honest split); ~0.62 (Austin 6-yr mean)
- **Ranking output**: `opportunity_score` = percentile rank within metro
- **Tier assignment**:
  - Tier 1: top 10% (≥90th percentile)
  - Tier 2: 75th–90th percentile

### Signal Validation: Spearman IC
Walk-forward Information Coefficient measures correlation between model rank at time T and actual `built_pct_change` over T+24 months.

| Market | Years | Mean IC | Notes |
|--------|-------|---------|-------|
| Phoenix | 2018–2020 | +0.20 | Permits API broke Q3 2020, signal degraded |
| Austin | 2018–2023 | +0.091 | COVID disruption 2020–22 (IC −0.18 to −0.22), 2023 recovery +0.29 |

Positive IC confirms the model correctly ranks cells by forward development probability.

---

## 3. Equity Signal

**Universe**: Public REITs, homebuilders, land developers (Russell 3000 screen)  
**Method**: Cross-sectional Ridge regression
- Features: value (P/B, P/FFO), momentum (12-1 month return), quality (debt/equity, ROE), size
- Rebalance: monthly
- Long-short: top/bottom 20th percentile

**Backtest Results (2018–2024)**:
- Portfolio CAGR: 11.5% vs SPY 12.2%
- Sharpe: 0.73 vs SPY 0.62 (delta: +0.23 — better risk-adjusted)
- Max drawdown: −18.4% vs SPY −33.9%
- Annualized vol: 14.2% vs SPY 16.8%

**BPS sensitivity**: Strategy break-even at ~35 BPS all-in transaction costs. Viable in liquid instruments.

---

## 4. Financial Model — Single Deal

See [deal_economics.md](deal_economics.md) for full detail.

### Construction Phase
```
Hard cost  = acreage × FAR × 43,560 sqft/acre × cost_psf × (1 + soft_cost_pct)
Loan       = hard_cost × LTV (65%)
Equity     = hard_cost × 35%

Construction interest = loan × DRAW_FACTOR × rate × (build_months / 12)
  DRAW_FACTOR = 0.55  ← S-curve progressive draw saves ~45% vs full-loan assumption
```

### Lease-Up Phase (Post-Certificate of Occupancy)
```
Lease-up months: 12 (Phoenix) / 15 (Austin)
Avg occupancy during ramp: 55% (0% at CO → ~85% at stabilization)
Lease-up interest = loan × construction_rate × (leaseup_months / 12)
Partial NOI offset = NOI_yr1 × 0.55 × (leaseup_months / 12)
```

### Stabilized Hold (Build-to-Rent)
```
NOI_yr1 = gross_rent × (1 − vacancy%) − opex
  Austin Yr1 stress: NOI × (1 − 0.02) reflecting suburban oversupply correction
DSCR → refi to permanent loan at 6.0% fixed
Hold: 5 years post-stabilization
Exit: sell at exit cap rate
```

### Build-to-Sell (Phoenix only, 40% of deals)
```
BTS exit: sell at stabilization (not hold 5 years)
Exit cap: 5.0% (CBRE Phoenix Class A median, Q2 2025)
Total cash returned = sale_net + partial_NOI_during_rampup
BTS IRR = (total_cash_returned / equity_invested)^(1/years) − 1
Cycle: ~23 months (11 mo build + 12 mo lease-up)
```

---

## 5. Portfolio Simulation

**Deployment**: 4 years × 3 projects/year + 1 overflow = 13 Phoenix + 4 Austin projects (17 total)  
**Simulation horizon**: 10 years (matches SPY comparison window)

Cash flows modeled at project level, aggregated to portfolio NAV annually.  
Compared to SPY: same $14.35M initial equity invested, equal weight.

See [portfolio_simulation.md](portfolio_simulation.md) for detailed logic.
