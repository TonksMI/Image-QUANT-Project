# Deal Economics — Single Project Financial Model

## Representative Deal: Phoenix Chandler, Residential (Tier 1)

### Inputs (Typical Tier-1 Phoenix Cell)

| Parameter | Value | Source |
|-----------|-------|--------|
| Lot size | 208 acres (H3 resolution-8) | H3 geometry |
| Developable portion | ~50% → 104 acres | FAR × total area |
| FAR (floor-area ratio) | 0.50 (Type-V 3-story garden) | Market standard |
| GBA (gross buildable area) | 104 acres × 0.50 × 43,560 = 2.26M sqft | Calculated |
| Hard cost PSF | $175 (Phoenix) / $155 (Austin) | Mortenson / EVstudio 2025 |
| Soft costs | 15% of hard cost | Market standard |
| Total development cost | $2.26M × $175 × 1.15 = $454M | Calculated |
| LTV | 65% | Model constant |
| Equity required | $454M × 35% = $159M | Calculated |
| Construction loan | $454M × 65% = $295M | Calculated |

> **Note**: The above are for full cell development. In practice, individual projects represent a portion of a cell. The portfolio model distributes $14.35M total equity across 17 projects averaging ~$844K equity each.

### Representative Single Project (Scaled)

Assuming $1.2M total cost project (typical Chandler garden-style 12-unit building):

| Parameter | Value |
|-----------|-------|
| Total project cost | $1,200,000 |
| Equity (35%) | $420,000 |
| Construction loan (65%) | $780,000 |
| Construction rate | 8.5% |
| Build time | 11–14 months |

### Construction Phase

```
DRAW_FACTOR = 0.55  (S-curve progressive draw)

Naive construction interest (day-1 full draw):
  $780K × 8.5% × (13/12) = $71,825

Corrected (S-curve):
  $780K × 0.55 × 8.5% × (13/12) = $39,504

Savings from progressive draw: ~$32K (45% reduction)
```

### Lease-Up Phase (Post-CO)

```
Phoenix lease-up: 12 months
Austin lease-up:  15 months
Average occupancy during ramp: 55%

NOI Year 1 (stabilized): $72,000 (example)

Lease-up interest = $780K × 8.5% × (12/12) = $66,300
Partial NOI during ramp = $72K × 0.55 × (12/12) = $39,600
Net lease-up carry cost = $66,300 − $39,600 = $26,700
```

### Total Upfront Cash Burden

```
Equity invested:          $420,000
+ Construction interest:   $39,504
+ Net lease-up carry:      $26,700
─────────────────────────────────
Total cash in:            $486,204
```

### Stabilized Cash Flows (Build-to-Rent)

Assumptions for stabilized garden-style multifamily:
- Gross rent: $1.75/sqft/mo × gross leasable area
- Vacancy: 7%
- Operating expenses: 35% of effective gross income
- NOI margin: ~58% of EGI

```
Permanent loan rate:     6.0% (takeout after stabilization)
Annual NOI:              ~$72,000 (example)
Debt service (perm):     ~$48,600/yr
Net cash flow:           ~$23,400/yr (pre-tax)
```

### Exit (Year 5 Hold)

```
Exit cap rate (residential): 5.1% blended
  Phoenix: 5.0% (BTS); 5.1% (BTR hold)
  Austin:  5.2% BTR

Example BTR exit:
  NOI Year 5 (with 3% growth): $72K × (1.03)^5 = $83,500
  Gross exit value: $83,500 / 0.051 = $1,637,000
  Net of selling costs (2%): $1,604,000
  Loan payoff (perm): ~$507,000
  Net proceeds: $1,097,000
```

### IRR Calculation (BTR)

```
Cash flows:
  Year 0:  −$486,204  (equity + construction carry + lease-up net)
  Year 1:   $23,400   (stabilized CoCF)
  Year 2:   $24,100
  Year 3:   $24,800
  Year 4:   $25,500
  Year 5:   $26,300 + $1,097,000 (exit proceeds)

BTR IRR: ~14.3%  (median across 17-project portfolio)
Equity multiple: 2.1× over 6-year cycle (build + hold)
```

### Build-to-Sell (Phoenix Only — Top 40%)

```
BTS exit: sell at stabilization (end of lease-up, not year 5)
  Cycle: 11 mo build + 12 mo lease-up = 23 months total

Exit cap: 5.0% (CBRE Phoenix institutional demand)
Gross BTS value: NOI_yr1 / 0.050
Net after loan + 2% selling costs = BTS net proceeds

Total cash returned = BTS net + partial NOI during ramp − construction interest − lease-up interest

BTS IRR formula:
  bts_em = total_cash_returned / equity_invested
  BTS IRR = bts_em^(12/23) − 1 = 14.1%
```

---

## Key Assumptions Table

| Parameter | Phoenix | Austin | Source |
|-----------|---------|--------|--------|
| Hard cost PSF | $175 | $155 | Mortenson/EVstudio 2025 |
| Soft cost % | 15% | 15% | Market standard |
| FAR (residential) | 0.50 | 0.50 | Zoning/market standard |
| LTV | 65% | 65% | Lender standard |
| Construction rate | 8.5% | 8.5% | 2025 SOFR + spread |
| Perm rate | 6.0% | 6.0% | 2025 agency market |
| Draw factor | 0.55 | 0.55 | S-curve (early 10%/mid 40%/late 50%) |
| Build time | 11–14 mo | 9–12 mo | DB `est_construction_months` |
| Lease-up period | 12 mo | 15 mo | Market (Austin slower ramp) |
| Lease-up occupancy | 55% | 55% | Industry ramp curve |
| Effective rent PSF/yr | $21 | $20 | RealPage 2025 |
| Vacancy | 7% | 8% | Market (Austin higher) |
| OpEx ratio | 35% | 36% | Property management + taxes |
| Yr1 rent stress | 0% | −2% | Austin oversupply correction |
| Rent growth | 3%/yr | 2%/yr | Post-correction trajectory |
| Exit cap (BTR) | 5.1% | 5.2% | CBRE Q2 2025 conservative |
| Exit cap (BTS) | 5.0% | N/A | Phoenix institutional demand |
| Hold period | 5 years | 5 years | Model constant |
| Sell cost | 2% | 2% | Broker + closing standard |
| Land value (Tier-1) | $200K/ac | $150K/ac | County assessor + comps |

---

## Sensitivity Analysis

### IRR Sensitivity to Cap Rate (BTR Phoenix)

| Exit Cap | BTR IRR | Change |
|----------|---------|--------|
| 4.5% | 16.8% | +2.5 pts |
| 5.0% | 15.1% | +0.8 pts |
| **5.1% (base)** | **14.3%** | — |
| 5.5% | 12.9% | −1.4 pts |
| 6.0% | 11.2% | −3.1 pts |

### IRR Sensitivity to Rent Growth (BTR)

| Rent Growth | BTR IRR |
|-------------|---------|
| 1% | 12.8% |
| 2% | 13.5% |
| **3% (base Phoenix)** | **14.3%** |
| 4% | 15.1% |
| 5% | 15.9% |

### Portfolio NAV Sensitivity to Construction Cost (±10%)

| Cost PSF | Portfolio NAV |
|----------|---------------|
| $157 (−10%) | $31.8M |
| **$175 (base)** | **$29.7M** |
| $193 (+10%) | $27.4M |
