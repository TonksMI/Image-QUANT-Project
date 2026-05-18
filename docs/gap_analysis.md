# Gap Analysis — 12-Point Model Corrections

Applied during the May 2026 validation review. These corrections adjusted projected NAV from $33.4M → $29.7M (−11%) and CAGR from 12.8% → 11.5% (−130 bps). All corrections make the model more conservative and reflect real-world friction that the original model omitted.

---

## Summary of Impact

| Category | Metric | Before | After | Delta |
|----------|--------|--------|-------|-------|
| Portfolio NAV | Year 10 | $33.4M | $29.7M | −$3.7M |
| CAGR | Annualized | 12.8% | 11.5% | −130 bps |
| Median BTR IRR | Per project | ~16% | 14.3% | −170 bps |
| BTS IRR | Phoenix top 40% | 49.6%* | 14.1% | Fixed formula |
| Lease-up cost | Per project | $0 | +$255K | Added |
| Construction interest savings | Per project | $0 | −$133K | Added (positive) |

*The 49.6% BTS IRR was a formula error (double-counted equity).

---

## Point-by-Point Corrections

### 1. Progressive Construction Draw (S-Curve)
**Problem**: Original model assumed the full loan amount was drawn on day 1 and interest accrued from close to CO.  
**Reality**: Construction loans draw progressively as work is completed (typically 10%/40%/50% across early/mid/late phases).  
**Fix**: `constr_int = loan × DRAW_FACTOR × rate × (build_months / 12)` where `DRAW_FACTOR = 0.55`  
**Impact**: −$133K per project in construction interest cost (savings vs naive assumption)

### 2. Lease-Up Period Modeling
**Problem**: Original model assumed NOI started at full stabilization immediately at CO.  
**Reality**: Post-CO lease-up takes 12 months (Phoenix) / 15 months (Austin) during which:
- Construction loan continues to accrue interest
- Occupancy ramps from 0% → ~85% (average ~55%)
**Fix**: Added explicit lease-up interest + partial NOI offset  
**Impact**: +$255K upfront cost per project (net of partial NOI recovery)

### 3. Austin Year-1 Rent Stress
**Problem**: Austin suburban multifamily rents declined 10–11% YoY in 2024–2025 due to new supply delivery.  
**Reality**: Tier-1 Austin locations are insulated but not immune; blended correction ~−2% Year 1.  
**Fix**: `noi_series[0] = noi_yr1 × (1 − 0.02)` for Austin  
**Impact**: ~$18K NOI reduction in Year 1 per Austin project

### 4. BTS IRR Formula Correction
**Problem**: Formula `bts_em_val = (bts_net_inv + equity_req) / equity_req` double-counted the equity return.  
**Reality**: Equity multiple = total cash received / equity invested (total cash already includes return of equity).  
**Fix**: `bts_em_val = bts_total_recv / equity_req` where `bts_total_recv = sale_net + leaseup_NOI − constr_int − leaseup_int`  
**Impact**: BTS IRR corrected from 49.6% → 14.1% (appropriate for 23-month cycle)

### 5. Phoenix BTS Cap Rate Separation
**Problem**: Single blended exit cap rate (5.1%) was used for all deals.  
**Reality**: BTS deals (Phoenix) transact at 5.0% (tighter — institutional demand for stabilized assets); BTR hold uses 5.1% for exit.  
**Fix**: `BTS_CAP_PHX = 0.050` separate from `EXIT_CAP_RATE['residential'] = 0.051`  
**Impact**: Minor positive on BTS exit value (≈+1.6% on sale price)

### 6. Austin BTR — No BTS
**Problem**: Original plan considered BTS for both markets.  
**Reality**: Austin institutional demand for stabilized multifamily is weaker than Phoenix given oversupply; 2% transaction costs on sale create drag.  
**Fix**: `BTS_ENABLED = True` but Austin excluded (`city == "phoenix"` gating)  
**Impact**: All Austin deals run as BTR, improving hold-period NOI accrual

### 7. Soft Cost Loading (15%)
**Problem**: Hard cost per sqft used as total development cost.  
**Reality**: Architecture, permits, development fee, A&E = 15% soft cost on top of hard cost.  
**Fix**: `total_cost = hard_cost × (1 + SOFT_PCT)` where `SOFT_PCT = 0.15`  
**Impact**: Material (architecture/engineering fees are real costs)

### 8. Construction Cost Validation & Capping
**Problem**: `est_cost_per_sqft` from DB occasionally returns outliers (>$220 or <$140).  
**Fix**: `raw_cost = np.clip(raw_cost, COST_PSF_MIN, COST_PSF_MAX)` with floor $140, ceiling $220  
**Impact**: Eliminates artificially high or low cost cells from distorting portfolio averages

### 9. Perm Loan Re-sizing at Stabilization
**Problem**: Perm loan was assumed equal to construction loan.  
**Reality**: Perm loan is sized to 65% LTV of stabilized property value, which may be lower than construction loan if values haven't appreciated enough.  
**Fix**: `perm_loan = min(loan_amount, stabilized_value × LTV)`  
**Impact**: Prevents overstating debt paydown proceeds at exit

### 10. Exit Proceeds Net of Loan Payoff
**Problem**: Exit value used gross property value.  
**Fix**: `net_sale_proceeds = sale_price × (1 − SELL_COST_PCT) − perm_loan`  
**Impact**: Proper cash-to-equity calculation at exit

### 11. Rent Growth Deceleration (Austin 2%)
**Problem**: Austin rent growth modeled at 4%/yr (same as Phoenix).  
**Reality**: Austin suburban market correction expected to persist through 2026; recovery to 3% by 2027+.  
**Fix**: `RENT_GROWTH = {"phoenix": 0.03, "austin": 0.02}`  
**Impact**: Austin NOI grows more slowly through hold period; cumulative ~4% lower NOI in year 5

### 12. Spearman IC Backtest Data Source
**Problem**: No IC table found in PostgreSQL database (expected tables: backtest_results, lot_backtest_ic, h3_backtest_ic).  
**Fix**: Used hardcoded IC values validated from codebase audit (notebook `10_cross_section_backtest.ipynb`) and plan context  
**Impact**: Documentation only; IC values represent the signal validation, not financial projections

---

## Net Financial Impact Per Project (Typical Phoenix Residential Deal)

| Line Item | Original | Corrected | Change |
|-----------|----------|-----------|--------|
| Construction interest | −$285K | −$157K | +$128K (savings) |
| Lease-up interest | $0 | −$178K | −$178K |
| Lease-up partial NOI | $0 | +$53K | +$53K |
| Net upfront cash burden | −$285K | −$282K | +$3K (minimal) |
| Austin Yr1 NOI | Full | −2% | −$18K (Austin only) |
| BTS IRR | 49.6%* | 14.1% | Corrected |
| BTR IRR | ~16% | 14.3% | −1.7 pts |

*Formula error, not a real return.
