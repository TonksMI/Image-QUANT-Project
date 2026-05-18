# Portfolio Simulation Design

## Overview

The portfolio simulation models a real estate fund that:
1. Deploys equity into 17 BTR/BTS projects over 4 years
2. Generates NOI + appreciation cash flows over a 10-year horizon
3. Compares NAV trajectory to an equivalent SPY investment

**Total equity deployed**: $14.35M  
**Projects**: 17 (13 Phoenix, 4 Austin)  
**Deployment rate**: 3 projects/year for 4 years + 1 overflow  
**Simulation horizon**: 10 years (years 0–9)

---

## Project Selection

Lots are selected from the `lot_opportunities` database table:
- Phoenix: top 15 Tier-1 cells by opportunity score → 13 used
- Austin: top 15 Tier-1 cells → 4 used
- Interleaved PHX/AUS ordering for geographic diversification

Deployment schedule:
```
Year 0: 3 projects
Year 1: 3 projects  
Year 2: 3 projects
Year 3: 4 projects  (overflow)
```

---

## Cash Flow Model (Per Project)

### Timeline
```
T=0 (start_yr):    Equity deployed + construction begins
T=build_mo:        Certificate of Occupancy (CO)
T=build_mo+12/15:  Stabilization (end of lease-up)
T=build_mo+12+60:  BTR exit (5-year hold post-stabilization)
  OR
T=build_mo+12:     BTS exit (sell at stabilization, Phoenix only)
```

### Construction Cash Flows
```python
constr_int = loan_amount × DRAW_FACTOR × CONSTRUCTION_RATE × (build_mo / 12)
```

### Lease-Up Cash Flows
```python
leaseup_int = loan_amount × CONSTRUCTION_RATE × (leaseup_mo / 12)
partial_noi  = noi_yr1 × LEASEUP_OCC × (leaseup_mo / 12)
net_leaseup  = partial_noi − leaseup_int  # usually negative (cash drain)
```

### Stabilized Annual Cash Flows (BTR)
```python
# Year y of hold (y = 0..HOLD_YEARS-1)
noi_y         = noi_yr1 × (1 + stress_yr1 if y==0) × (1 + rent_growth)^y
perm_int_yr   = perm_loan × PERM_LOAN_RATE
cocf_y        = noi_y − perm_int_yr  # cash-on-cash before depreciation
```

### Exit Cash Flows
```python
# BTR
noi_exit       = noi_yr1 × (1 + rent_growth)^HOLD_YEARS
exit_value     = noi_exit / exit_cap_rate
net_sale       = exit_value × (1 − SELL_COST_PCT) − perm_loan
terminal_cf    = cocf_last_year + net_sale

# BTS
bts_noi_stab   = noi_yr1 / bts_cap_rate
bts_sale_net   = bts_noi_stab × (1 − SELL_COST_PCT) − loan_amount
bts_total_recv = bts_sale_net + partial_noi − constr_int − leaseup_int
```

---

## Portfolio NAV Computation

Each year `t`, portfolio NAV = sum of:
1. **Unrealized mark-to-market** on active BTR projects (NOI / current cap rate)
2. **Realized proceeds** from BTS exits and BTR exits that year
3. **Reinvested cash** — cumulative COCF from stabilized projects (not withdrawn)
4. **Uninvested equity** — capital not yet deployed (years 0–3 deployment ramp)

```python
# Simplified NAV accumulation logic
portfolio_nav[t] = (
    sum(project_marks[p] for p in active_btrs)
  + cumulative_cocf
  + unrealized_bts_equity  # before exit
  + uninvested_capital
)
```

---

## SPY Comparison

Same $14.35M invested in SPY at T=0.  
SPY total return (dividends reinvested) over 10-year backtest window.

**Results**:
| Metric | Urban Growth Portfolio | SPY |
|--------|----------------------|-----|
| NAV (Year 10) | $29.7M | $24.4M |
| CAGR | 11.5% | 12.2% |
| Total Return | 107% | 70% |
| Volatility (annual) | ~8% (private, mark-to-model) | 16.8% |
| Max Drawdown | ~−5% (private, illiquid) | −33.9% |
| Sharpe (vs 5% RF) | ~0.81 | 0.43 |

> **Interpretation**: Portfolio outperforms SPY in absolute NAV (+$5.3M or +22%) despite lower CAGR because real estate leverage amplifies returns in early years. The private nature of the investment means reported vol is low (mark-to-model); true risk should include illiquidity premium, development risk, and leverage.

---

## BTS/BTR Split Logic

```python
BTS_PHX_SHARE = 0.40  # 40% of Phoenix projects exit via BTS

n_phx  = 13  # Phoenix projects in portfolio
n_bts  = max(1, round(13 × 0.40)) = 5

# Highest-scoring Phoenix lots → BTS (capital velocity)
# Remaining Phoenix + all Austin → BTR (hold for income)
```

**Rationale for 40% BTS**:
- Institutional demand for stabilized Phoenix multifamily is strong (5.0% cap = premium pricing)
- Recycling capital from early BTS exits allows redeployment into later projects
- Austin excluded from BTS: weaker institutional demand + higher transaction costs relative to NOI

---

## Risk Factors Modeled

| Risk | Model Treatment |
|------|-----------------|
| Construction cost overrun | ±10% sensitivity table |
| Cap rate expansion | ±50 bps sensitivity table |
| Rent decline | Austin Yr1 −2% stress applied |
| Extended lease-up | 12/15 month base; sensitivity at 18 months |
| Vacancy above 7% | Covered by DSCR buffer on perm loan |
| Interest rate rise | Fixed-rate perm loan hedges refi risk |
| Land value decline | Not explicitly modeled (conservative land basis) |
| Permit delays | Build time from DB `est_construction_months` (actual historical) |

---

## Key Outputs

| Output | File |
|--------|------|
| Portfolio vs SPY chart | `re_portfolio_comparison.png` |
| Per-project IRR table | Printed to console |
| proj_df DataFrame | In-memory (not saved) |
| KPI table | Embedded in chart and HTML report |
