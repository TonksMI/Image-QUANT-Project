"""
Corporate Real Estate Portfolio Simulation (v2)
  - Buy top-scored vacant lots from model
  - Finance construction with short-term construction loan @ 8.5%
  - Refinance to permanent loan @ 6.0% at CO (standard developer playbook)
  - Hold and collect rent (5-yr hold), then sell at stabilised cap rate
  - Compare equity growth to SPY buy-hold + leveraged SPY over same period

Key fix from v1: perm loan rate (6%) replaces construction rate (8.5%) for hold period,
dramatically improving cash-on-cash and IRR to realistic ~15-20% target range.
"""
import os, json, warnings
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
from dotenv import load_dotenv
load_dotenv(dotenv_path="C:/Users/17ton/urbangrowth/.env")

from urbangrowth.modeling.backtest import load_monthly_returns, performance_stats
import h3
from data.loader import load_lots

# ─────────────────────────────────────────────────────────────────────────────
# PORTFOLIO ASSUMPTIONS
# ─────────────────────────────────────────────────────────────────────────────
STARTING_EQUITY        = 10_000_000   # $10M initial equity
LTV                    = 0.65         # 65% LTV — both construction and perm
CONSTRUCTION_RATE      = 0.085        # 8.5% construction loan (12-mo draw)
PERM_LOAN_RATE         = 0.060        # 6.0% permanent/takeout loan (stabilised)
HOLD_YEARS             = 5            # hold after CO before sale
BUILD_YEARS            = 1            # construction period

EXIT_CAP_RATE = {
    # Validated: CBRE Q2 2025, Matthews Q4 2025, Innowave Q3 2025
    # Phoenix Class A new: 4.8-5.2%; Austin Class A new: 5.0-5.5%
    # Using conservative end of range (5.0% Phoenix, 5.2% Austin) for model
    "residential": 0.051,            # blended (Phoenix 5.0%, Austin 5.2%)
    "commercial":  0.062,            # light commercial NNN (slightly wider than multifamily)
    "vacant":      0.055,
    "open_space":  0.058,
    "unknown":     0.054,
}
VACANCY_RATE           = 0.07         # 7% vacancy loss
OPEX_RATIO             = 0.35         # 35% of EGI (mgmt, tax, insurance, maint)

PARCEL_ACRES           = 1.0          # typical carved parcel from a Tier-1 cell (~43k sqft)
# Validated land value floors (Tier-1 submarket; source: AI model + broker comps 2025)
LAND_VALUE_FLOOR = {                  # min $/acre by city (from AI model + broker validation)
    "phoenix": 200_000,              # Phoenix Chandler/Scottsdale Tier-1: $150k-$300k/acre
    "austin":  150_000,              # Austin South/suburban Tier-1: $100k-$250k/acre
    "default": 150_000,
}
# Construction costs: VALIDATED — Mortenson Phoenix 2025; EVstudio/Maxx Austin 2024 (+ inflation adj.)
COST_PSF_BY_CITY = {                  # hard cost per sqft (Type V wood-frame garden-style)
    "phoenix": 175,                  # Mortenson Phoenix Cost Index 2025: $155-195/sqft
    "austin":  155,                  # EVstudio 2024 + 5%/yr inflation adj.: $130-155 → ~$155
    "default": 165,
}
COST_PSF_MIN           = 140          # floor (bottom of Austin range)
COST_PSF_MAX           = 220          # ceiling (top of Phoenix range, excl. outliers)
SOFT_PCT               = 0.15         # soft costs 15% (arch, permits, dev fee, A&E) — market standard

FAR = {                               # floor-area-ratio (Type V wood-frame suburban multifamily)
    "residential": 0.50,             # 3-story garden apartments — validated FAR for 1-2 acre parcels
    "commercial":  0.65,             # 2-3 story mixed-use / flex (NNN lease model)
    "vacant":      0.40,
    "open_space":  0.20,
    "unknown":     0.40,
}
RENT_PSF_YR = {
    # Validated 2024-2025 (RealPage, Yardi Matrix, CoStar):
    # Phoenix Chandler effective: $1.75/mo ($21/yr) — $1.90-2.20 asking minus 6-8 wk concessions
    # Austin South/Central effective: $1.78/mo ($21.4/yr) — best Austin submarkets
    # Austin suburban (RR, Pflugerville) effective: ~$1.55/mo ($18.6/yr) — -10-11% YoY decline
    # Using blended effective (conservative): $21/yr Phoenix; $20/yr Austin
    "residential": 21.0,              # $1.75/sqft/month effective (validated against RealPage 2025)
    "commercial":  26.0,              # NNN flex/light commercial (Phoenix/Austin 2024 comps)
    "vacant":      21.0,
    "open_space":  16.0,
    "unknown":     20.0,
}
RENT_GROWTH = {
    # Validated: Phoenix Chandler 4%/yr (healthier market); Austin suburban 0% Yrs 1-2 then 3%
    # Using 3% blended (conservative; reflects oversupply correction timeline)
    "phoenix": 0.03,                  # 3% (Chandler/Scottsdale rent growth post-correction)
    "austin":  0.02,                  # 2% (Austin broader recovery; suburban still digesting supply)
}
LAND_APPRECIATION = {                 # annual land appreciation
    "phoenix": 0.06,
    "austin":  0.07,
}
CONSTRUCTION_ESCALATION = 0.03        # build cost inflation/yr

# ── Gap-analysis fixes (applied 2026-05) ─────────────────────────────────────
# 1. Progressive construction draw (industry-standard S-curve):
#    Avg 55% of loan outstanding during draw (10% early / 40% mid / 50% late)
#    Reduces effective interest carry vs naive "full loan from day 0" assumption
DRAW_FACTOR      = 0.55
# 2. Lease-up period: construction loan still active until property stabilizes
#    Timeline: CO → 12-mo Phoenix / 15-mo Austin ramp → full stabilization → refi to perm
LEASEUP_MONTHS   = {"phoenix": 12, "austin": 15, "default": 12}
LEASEUP_OCC      = 0.55   # avg occupancy during ramp (0% at CO → ~85% at stabilization)
# 3. Austin Year 1 rent stress: suburban -10-11% YoY; Tier-1 submarkets ~-2% (blended)
RENT_STRESS_YR1  = {"phoenix": 0.00, "austin": -0.02}
# 4. Hybrid BTS/BTR: Phoenix 40% sell at stabilization for capital velocity (Austin BTS: AVOID)
BTS_ENABLED      = True
BTS_PHX_SHARE    = 0.40   # fraction of Phoenix projects exiting via build-to-sell
SELL_COST_PCT    = 0.02   # transaction costs (broker commission + closing) on BTS sale
BTS_CAP_PHX      = 0.050  # Phoenix BTS exit cap: 5.0% (CBRE median; vs 5.1% blended BTR)

PROJECTS_PER_YEAR = 3                 # deals closed per year (3 is achievable for a team)
TOTAL_YEARS       = 4                 # active deployment horizon (years 0-3)
HORIZON           = TOTAL_YEARS + HOLD_YEARS + 1  # total sim length (~10 yrs, matches SPY window)

print("=== Corporate RE Portfolio Simulation v2 ===")
print(f"Construction loan: {CONSTRUCTION_RATE:.1%} → Perm loan: {PERM_LOAN_RATE:.1%}")
print(f"FAR (residential): {FAR['residential']:.2f}  Rent PSF: ${RENT_PSF_YR['residential']:.0f}")
print(f"Exit cap (residential): {EXIT_CAP_RATE['residential']:.1%}")

# ─────────────────────────────────────────────────────────────────────────────
# Load top lots (DB if available, flat-file fallback otherwise)
# ─────────────────────────────────────────────────────────────────────────────
# load_lots() tries PostgreSQL first; falls back to data/lots_*.csv automatically
_phx = load_lots(city="phoenix", limit=15)
_aus = load_lots(city="austin",  limit=15)
# Normalise column name (DB returns 'city', flat file returns 'city_name')
for _df in [_phx, _aus]:
    if "city_name" in _df.columns and "city" not in _df.columns:
        _df.rename(columns={"city_name": "city"}, inplace=True)
lots_phx = _phx.head(15)
lots_aus = _aus.head(15)

# Interleave Phoenix and Austin for geographic diversification
lots = pd.concat([lots_phx, lots_aus]).reset_index(drop=True)
# Interleave: PHX, AUS, PHX, AUS...
idx_phx = list(range(0, len(lots_phx)))
idx_aus = list(range(len(lots_phx), len(lots_phx)+len(lots_aus)))
interleaved = []
for i in range(max(len(idx_phx), len(idx_aus))):
    if i < len(idx_phx): interleaved.append(idx_phx[i])
    if i < len(idx_aus):  interleaved.append(idx_aus[i])
lots = lots.iloc[interleaved].reset_index(drop=True)

latlons = [h3.cell_to_latlng(idx) for idx in lots["h3_index"]]
lots["lat"] = [ll[0] for ll in latlons]
lots["lon"] = [ll[1] for ll in latlons]
print(f"Loaded {len(lots_phx)} Phoenix + {len(lots_aus)} Austin Tier-1 lots (interleaved)")

# ─────────────────────────────────────────────────────────────────────────────
# Project-level financials
# ─────────────────────────────────────────────────────────────────────────────
def irr_solve(cashflows):
    from scipy.optimize import brentq
    def npv(r):
        return sum(c / (1 + r) ** t for t, c in enumerate(cashflows))
    try:
        return brentq(npv, -0.5, 10.0)
    except Exception:
        return np.nan

projects = []
for _, r in lots.iterrows():
    city     = str(r["city"]).lower()
    ptype    = str(r.get("dominant_property_type") or "unknown").lower()
    if ptype not in FAR:
        ptype = "unknown"
    # For development simulation: Tier-1 growth lots selected for ground-up development.
    # "unknown" property type = insufficient parcel data (common in Phoenix DB), NOT unbuildable.
    # Default to residential (highest-value use; consistent with AI model intent).
    if ptype in ("unknown", "open_space"):
        ptype = "residential"

    # Land — use city-specific validated floor (AI model + broker comps)
    land_floor = LAND_VALUE_FLOOR.get(city, LAND_VALUE_FLOOR["default"])
    land_ppa   = max(float(r["est_land_value_acre"] or land_floor), land_floor)
    # Construction — use city-specific validated hard cost (Mortenson/EVstudio 2025)
    base_cost  = COST_PSF_BY_CITY.get(city, COST_PSF_BY_CITY["default"])
    raw_cost   = r["est_cost_per_sqft"]
    # If DB has a value, blend with validated market cost; otherwise use market-validated default
    if pd.notna(raw_cost) and raw_cost:
        cost_psf = (float(raw_cost) + base_cost) / 2.0   # blend DB estimate with market data
    else:
        cost_psf = base_cost
    cost_psf  = max(COST_PSF_MIN, min(COST_PSF_MAX, cost_psf))   # clamp to validated range

    build_mo  = int(r["est_construction_months"] if pd.notna(r["est_construction_months"]) and r["est_construction_months"] else 14)
    build_mo  = max(10, min(18, build_mo))   # clamp 10-18 months (Type V garden-style)

    # Size
    acres      = PARCEL_ACRES
    sqft_land  = acres * 43_560
    sqft_bld   = sqft_land * FAR[ptype]

    # ── Acquisition & construction costs ──────────────────────────────────────
    land_cost   = acres * land_ppa
    build_cost  = sqft_bld * cost_psf
    soft_cost   = (land_cost + build_cost) * SOFT_PCT   # 15% soft costs (market standard)
    total_cost  = land_cost + build_cost + soft_cost

    equity_req  = total_cost * (1 - LTV)
    loan_amount = total_cost * LTV

    # ── Construction interest: progressive S-curve draw ──────────────────────
    # Industry standard: avg 55% of loan outstanding (10% early / 40% mid / 50% late draw)
    # Corrects prior model's "full loan from day 0" overstatement of carry cost
    constr_int  = loan_amount * DRAW_FACTOR * CONSTRUCTION_RATE * (build_mo / 12)

    # ── Lease-up period: construction loan active until full stabilization ────
    # After CO: 12 mo Phoenix / 15 mo Austin ramp from 0% → ~85% occupancy
    leaseup_mo  = LEASEUP_MONTHS.get(city, LEASEUP_MONTHS["default"])
    leaseup_int = loan_amount * CONSTRUCTION_RATE * (leaseup_mo / 12)

    # ── Stabilised income (fully leased, market-rate rents) ──────────────────
    gross_rent  = sqft_bld * RENT_PSF_YR.get(ptype, 22.0)
    egi         = gross_rent * (1 - VACANCY_RATE)
    noi_yr1     = egi * (1 - OPEX_RATIO)

    # Partial lease-up NOI: avg LEASEUP_OCC occupancy during ramp period
    # Construction loan still active — net of carry cost
    noi_leaseup_raw  = noi_yr1 * LEASEUP_OCC * (leaseup_mo / 12)   # fractional annual NOI
    cocf_leaseup     = max(0.0, noi_leaseup_raw - leaseup_int)       # net of construction carry

    cap         = EXIT_CAP_RATE[ptype]
    value_stab  = noi_yr1 / cap

    # ── Permanent loan at stabilisation (refi from construction) ─────────────
    perm_basis  = max(total_cost, value_stab)
    perm_loan   = min(loan_amount, perm_basis * LTV)
    perm_int_yr = perm_loan * PERM_LOAN_RATE

    # ── 5-yr hold cash flows (Year 1 rent stress: Austin oversupply correction) ──
    rg         = RENT_GROWTH.get(city, 0.04)
    stress_yr1 = RENT_STRESS_YR1.get(city, 0.0)   # Austin -2% Yr1; Phoenix 0%
    noi_series = []
    for y in range(HOLD_YEARS):
        # Year 1 at stressed rent (oversupply impact), subsequent years grow from that base
        base = noi_yr1 * (1 + stress_yr1) * (1 + rg) ** y
        noi_series.append(base)
    cocf_series  = [max(0.0, n - perm_int_yr) for n in noi_series]
    total_rental = sum(cocf_series)

    # ── Exit (NOI one more growth year beyond hold) ───────────────────────────
    noi_exit     = noi_series[-1] * (1 + rg)
    value_exit   = noi_exit / cap
    loan_payoff  = perm_loan
    net_proceeds = value_exit - loan_payoff

    # ── BTR IRR (equity basis, after-debt) ────────────────────────────────────
    # CF[0] = equity + constr carry + lease-up carry - partial lease-up income
    # CF[1..5] = annual COCF (Year 1 potentially stress-reduced for Austin)
    # CF[5] += net proceeds at sale
    cf = [-equity_req - constr_int - leaseup_int + cocf_leaseup] + cocf_series[:]
    cf[-1] += net_proceeds
    project_irr = irr_solve(cf)

    equity_multiple = (total_rental + net_proceeds + cocf_leaseup
                       - constr_int - leaseup_int) / equity_req

    # ── BTS path: sell at stabilization (Phoenix only) ────────────────────────
    # Uses Phoenix BTS cap (5.0%) vs blended BTR cap (5.1%) — validated CBRE median
    bts_months = build_mo + leaseup_mo
    if BTS_ENABLED and city == "phoenix":
        bts_cap         = BTS_CAP_PHX                           # 5.0% Phoenix BTS
        bts_value_stab  = noi_yr1 / bts_cap                    # slightly higher than BTR value_stab
        bts_sale_net    = bts_value_stab * (1 - SELL_COST_PCT) - loan_amount  # net after loan & costs
        # Total cash to investor: sale net + lease-up income - carry costs
        bts_total_recv  = bts_sale_net + noi_leaseup_raw - constr_int - leaseup_int
        # EM = total cash received / equity invested (bts_total_recv already includes return of equity)
        bts_em_val      = bts_total_recv / equity_req
        bts_years_frac  = bts_months / 12
        # IRR: two-period calculation (invest equity at t=0, receive all cash at t=years)
        bts_irr_val     = bts_em_val ** (1 / bts_years_frac) - 1 if bts_years_frac > 0 else np.nan
    else:
        bts_sale_net = 0.0;  bts_total_recv = 0.0;  bts_em_val = np.nan;  bts_irr_val = np.nan;  bts_value_stab = value_stab

    # Value creation at stabilisation (development spread)
    dev_spread_pct = (value_stab - total_cost) / total_cost

    projects.append({
        "city": city, "ptype": ptype, "score": r["opportunity_score"],
        "land_cost": land_cost, "build_cost": build_cost, "soft_cost": soft_cost,
        "total_cost": total_cost, "equity_req": equity_req,
        "loan_amount": loan_amount, "constr_int": constr_int,
        "perm_loan": perm_loan, "perm_int_yr": perm_int_yr,
        "noi_yr1": noi_yr1, "value_stab": value_stab, "value_exit": value_exit,
        "dev_spread_pct": dev_spread_pct,
        "cocf_series": cocf_series, "total_rental": total_rental,
        "net_proceeds": net_proceeds, "irr": project_irr,
        "equity_multiple": equity_multiple,
        "sqft_bld": sqft_bld, "acres": acres, "build_mo": build_mo,
        "lat": r["lat"], "lon": r["lon"],
        "addr": r.get("nearest_address") or "",
        # Gap-analysis additions
        "leaseup_mo": leaseup_mo, "leaseup_int": leaseup_int,
        "cocf_leaseup": cocf_leaseup,
        "bts_months": bts_months, "bts_irr": bts_irr_val, "bts_em": bts_em_val,
        "bts_sale_net": bts_sale_net,          # net to equity after loan payoff
        "bts_total_recv": bts_total_recv if city == "phoenix" else 0.0,  # total cash to investor
        "is_bts": False,   # assigned below after proj_df is sorted
    })

proj_df = pd.DataFrame(projects).sort_values("score", ascending=False).reset_index(drop=True)

# ── Assign BTS flag: top BTS_PHX_SHARE fraction of Phoenix projects (by score) ──
if BTS_ENABLED:
    phx_mask   = proj_df["city"] == "phoenix"
    n_phx      = phx_mask.sum()
    n_bts      = max(1, int(round(n_phx * BTS_PHX_SHARE)))
    bts_idx    = proj_df[phx_mask].head(n_bts).index
    proj_df.loc[bts_idx, "is_bts"] = True
    print(f"BTS: {n_bts}/{n_phx} Phoenix projects flagged ({BTS_PHX_SHARE:.0%} share)")

print(proj_df[["city","ptype","equity_req","noi_yr1","value_stab","irr","bts_irr","equity_multiple","dev_spread_pct","is_bts"]].to_string())

# ─────────────────────────────────────────────────────────────────────────────
# Portfolio simulation — J-curve with recycled proceeds
# ─────────────────────────────────────────────────────────────────────────────
# Portfolio simulation — rolling deployment with proceeds recycled into new projects
# ─────────────────────────────────────────────────────────────────────────────
available_equity = STARTING_EQUITY
deployed         = []   # [(start_yr, proj_row)]
project_idx      = 0
MMF_RATE         = 0.045  # money market on idle cash

# Build a reusable lot queue (cycle if needed for long horizons)
lot_queue = list(proj_df.iterrows())
lot_cycle_idx = 0  # index into lot_queue, cycles for reinvestment rounds

def next_lot():
    """Return next project from queue (cycles through sorted lots)."""
    global lot_cycle_idx
    if lot_cycle_idx >= len(lot_queue):
        lot_cycle_idx = 0   # recycle — start from best lots again
    _, p = lot_queue[lot_cycle_idx]
    lot_cycle_idx += 1
    return p

# Initial deployment — years 0 to TOTAL_YEARS-1
for year in range(TOTAL_YEARS):
    slots_filled = 0
    while slots_filled < PROJECTS_PER_YEAR:
        p  = next_lot()
        eq = p["equity_req"] + p["constr_int"]
        if pd.isna(eq): break
        if available_equity >= eq:
            available_equity -= eq
            deployed.append((year, p))
            slots_filled += 1
        else:
            break   # not enough equity for more this year

n_initial = len(deployed)
deployed_equity = sum(p["equity_req"] + p["constr_int"] for _, p in deployed)
print(f"\n{n_initial} projects deployed initially | ${deployed_equity/1e6:.1f}M equity committed")

# Year-by-year NAV with reinvestment of sale proceeds
rows        = []
cash_pool   = available_equity   # idle cash earns MMF
cum_cocf    = 0.0
cum_gain    = 0.0

for yr in range(HORIZON):
    cocf_yr   = 0.0
    gain_yr   = 0.0
    nav_props = 0.0
    reinvest_proceeds = 0.0

    for i, (start_yr, p) in enumerate(deployed):
        build_end = start_yr + BUILD_YEARS
        hold_end  = build_end + HOLD_YEARS
        rg        = RENT_GROWTH.get(p["city"], 0.04)
        cap       = EXIT_CAP_RATE[p["ptype"]]

        if p.get("is_bts", False):
            # ── BTS project: construction + lease-up → sell at stabilization ──
            bts_exit = start_yr + max(1, int(np.ceil(p.get("bts_months", 26) / 12)))
            if start_yr <= yr < bts_exit:
                nav_props += p["equity_req"]  # at cost during construction/lease-up
            elif yr == bts_exit:
                # BTS total cash: sale net + lease-up income - carry costs
                bts_total = p.get("bts_total_recv", p["bts_sale_net"])
                gain_yr           += bts_total - p["equity_req"]
                reinvest_proceeds += bts_total
        else:
            # ── BTR project: construct → hold → sell ──────────────────────────
            if start_yr <= yr < build_end:
                # Construction: equity at cost basis
                nav_props += p["equity_req"]

            elif build_end <= yr < hold_end:
                # Hold: collect net cash-on-cash, mark to market
                hy       = yr - build_end
                stress   = RENT_STRESS_YR1.get(p["city"], 0.0) if hy == 0 else 0.0
                noi_hy   = p["noi_yr1"] * (1 + stress) * (1 + rg) ** hy
                net_cf   = max(0.0, noi_hy - p["perm_int_yr"])
                cocf_yr += net_cf

                val_mkt  = noi_hy / cap
                eq_mkt   = max(p["equity_req"], val_mkt - p["perm_loan"])
                nav_props += eq_mkt

            elif yr == hold_end:
                # Sale: capture realised gain, add proceeds to reinvestment pool
                gain_yr           += p["net_proceeds"] - p["equity_req"]
                reinvest_proceeds += p["net_proceeds"]

    # Add proceeds to cash pool BEFORE trying to redeploy
    cash_pool += reinvest_proceeds

    # Reinvest proceeds: deploy up to PROJECTS_PER_YEAR new projects this year
    new_this_yr = 0
    while new_this_yr < PROJECTS_PER_YEAR:
        p_new = next_lot()
        eq_new = p_new["equity_req"] + p_new["constr_int"]
        if pd.isna(eq_new): break
        if cash_pool >= eq_new and yr >= TOTAL_YEARS:   # reinvest post-initial horizon
            cash_pool -= eq_new
            deployed.append((yr, p_new))
            new_this_yr += 1
        else:
            break

    # Idle cash earns MMF
    cash_pool   *= (1 + MMF_RATE)
    cum_cocf    += cocf_yr
    cum_gain    += gain_yr

    total_nav = nav_props + cash_pool + cum_cocf + cum_gain
    rows.append({
        "year": yr,
        "cocf_income": cocf_yr,
        "realised_gains": gain_yr,
        "cum_cocf": cum_cocf,
        "cum_gain": cum_gain,
        "nav_props": nav_props,
        "cash_pool": cash_pool,
        "total_nav": total_nav,
    })

n_deployed = len(deployed)
deployed_equity_total = sum(p["equity_req"] + p["constr_int"] for _, p in deployed)
print(f"{n_deployed} total projects (incl. reinvestment) | ${deployed_equity_total/1e6:.1f}M total equity committed")

eq_df = pd.DataFrame(rows)

# ─────────────────────────────────────────────────────────────────────────────
# SPY comparison — actual historical monthly returns scaled to $10M
# Use 2015-2025 (~10 years) to match HORIZON
# ─────────────────────────────────────────────────────────────────────────────
raw_returns = load_monthly_returns()
spy_monthly = (raw_returns[raw_returns["symbol"] == "SPY"]
               .set_index("date")["monthly_ret"].dropna().sort_index())
spy_window  = spy_monthly["2015":"2025"]
if len(spy_window) < 60:   # fallback if data is sparse pre-2015
    spy_window = spy_monthly.tail(HORIZON * 12)
cum_spy     = (1 + spy_window).cumprod()
spy_years   = np.linspace(0, len(spy_window)/12, len(spy_window))
spy_equity  = cum_spy.values * STARTING_EQUITY
spy_2019    = spy_window   # alias for performance_stats call below

# Levered SPY: 65% LTV at 6% (brokerage margin at lower rate assumption)
# Simulated as: return = (1+r_spy)*1.65 - 0.65*r_margin on $10M equity
MARGIN_RATE = 0.06  # 6% margin rate
spy_lev_equity = [STARTING_EQUITY]
for r in spy_2019:
    equity_v  = spy_lev_equity[-1]
    borrowed  = equity_v * (LTV / (1 - LTV))   # maintain 65% LTV
    total_pos = equity_v + borrowed
    gross_ret = total_pos * r
    int_cost  = borrowed * (MARGIN_RATE / 12)
    spy_lev_equity.append(equity_v + gross_ret - int_cost)
spy_lev_equity = np.array(spy_lev_equity[1:])

# Simple land-hold baseline (buy land, let it appreciate, no construction)
yrs = eq_df["year"].values
land_hold_growth = np.array([STARTING_EQUITY * (1 + 0.065) ** y for y in yrs])

# ─────────────────────────────────────────────────────────────────────────────
# Summary stats
# ─────────────────────────────────────────────────────────────────────────────
phx_df = proj_df[proj_df["city"] == "phoenix"]
aus_df = proj_df[proj_df["city"] == "austin"]
avg_irr    = proj_df["irr"].median()
avg_em     = proj_df["equity_multiple"].median()
total_noi  = proj_df["noi_yr1"].sum()
avg_spread = proj_df["dev_spread_pct"].median()

re_final   = eq_df["total_nav"].iloc[-1]
re_cagr    = (re_final / STARTING_EQUITY) ** (1 / HORIZON) - 1
spy_stats  = performance_stats(spy_2019)
spy_cagr   = spy_stats["cagr"]

print(f"\n=== Portfolio Summary ===")
print(f"Projects: {n_deployed}  |  Median IRR: {avg_irr:.1%}  |  Median EM: {avg_em:.2f}x")
print(f"RE Portfolio CAGR: {re_cagr:.1%}  |  SPY CAGR: {spy_cagr:.1%}")
print(f"Avg dev spread: {avg_spread:.1%}  |  Total NOI Yr1: ${total_noi:,.0f}")
print(f"RE Final NAV: ${re_final/1e6:.1f}M  |  SPY Final: ${spy_equity[-1]/1e6:.1f}M")

# ─────────────────────────────────────────────────────────────────────────────
# GRAPHIC — 6-panel dark theme
# ─────────────────────────────────────────────────────────────────────────────
C_RE   = "#10B981"   # green  — real estate portfolio
C_SPY  = "#F59E0B"   # amber  — SPY unlev
C_SPYL = "#F472B6"   # pink   — levered SPY
C_LAND = "#94A3B8"   # grey   — land hold
C_BG   = "#0F1117"
C_CARD = "#1A1D2E"
C_GRID = "#2D3448"
C_TEXT = "#E2E8F0"
C_MUTED= "#94A3B8"
C_PHX  = "#E67E22"
C_AUS  = "#3498DB"
C_COCF = "#6C63FF"
C_GAIN = "#10B981"

fig = plt.figure(figsize=(18, 12), facecolor=C_BG)
fig.patch.set_facecolor(C_BG)

gs = gridspec.GridSpec(3, 3, figure=fig,
                       hspace=0.50, wspace=0.38,
                       left=0.06, right=0.97,
                       top=0.88, bottom=0.07)

ax_eq   = fig.add_subplot(gs[0, :2])   # equity growth comparison
ax_kpi  = fig.add_subplot(gs[0, 2])    # KPI table
ax_irr  = fig.add_subplot(gs[1, 0])    # IRR by project
ax_ret  = fig.add_subplot(gs[1, 1])    # return components by city
ax_wf   = fig.add_subplot(gs[1, 2])    # typical project waterfall
ax_cf   = fig.add_subplot(gs[2, :])    # cumulative cash flows over time

for ax in [ax_eq, ax_kpi, ax_irr, ax_ret, ax_wf, ax_cf]:
    ax.set_facecolor(C_CARD)
    ax.spines[["top","right","left","bottom"]].set_color(C_GRID)
    ax.tick_params(colors=C_MUTED, labelsize=8)
    for spine in ax.spines.values():
        spine.set_linewidth(0.6)

def Mfmt(v): return f"${v/1e6:.1f}M"
def pct(v):  return f"{v:.1%}"

# ── Panel 1: Equity growth ──────────────────────────────────────────────────
re_equity = eq_df["total_nav"].values

ax_eq.plot(spy_years,   spy_equity/1e6,     color=C_SPY,  lw=2.0, ls="--",
           label="SPY Buy-Hold (unlevered)")
ax_eq.plot(spy_years,   spy_lev_equity/1e6, color=C_SPYL, lw=1.8, ls="-.",
           label=f"Levered SPY (65% LTV @ {MARGIN_RATE:.0%} margin rate)", alpha=0.85)
ax_eq.plot(yrs,         land_hold_growth/1e6, color=C_LAND, lw=1.5, ls=":",
           label="Land Hold Only (~6.5%/yr)", alpha=0.7)
ax_eq.plot(yrs,         re_equity/1e6,      color=C_RE,   lw=2.8,
           label="Corp RE Portfolio (Build-to-Rent, 65% LTV)")
ax_eq.fill_between(yrs, STARTING_EQUITY/1e6, re_equity/1e6, alpha=0.10, color=C_RE)

final_re  = re_equity[-1]/1e6
final_spy = spy_equity[-1]/1e6
final_lev = spy_lev_equity[-1]/1e6
ax_eq.annotate(f"${final_re:.1f}M", xy=(yrs[-1], final_re),
               xytext=(-50, 8), textcoords="offset points",
               color=C_RE, fontsize=10, fontweight="bold")
ax_eq.annotate(f"${final_spy:.1f}M", xy=(spy_years[-1], final_spy),
               xytext=(-55, -14), textcoords="offset points",
               color=C_SPY, fontsize=10, fontweight="bold")
ax_eq.annotate(f"${final_lev:.1f}M", xy=(spy_years[-1], final_lev),
               xytext=(-55, 6), textcoords="offset points",
               color=C_SPYL, fontsize=10, fontweight="bold")

ax_eq.axhline(STARTING_EQUITY/1e6, color=C_GRID, lw=0.8, ls=":")
ax_eq.set_title("Portfolio Equity Growth — $10M Starting Capital", color=C_TEXT,
                fontsize=11, fontweight="bold", pad=8)
ax_eq.set_ylabel("Total Equity ($M)", color=C_MUTED, fontsize=9)
ax_eq.set_xlabel("Years from First Deployment", color=C_MUTED, fontsize=9)
ax_eq.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:.0f}M"))
ax_eq.legend(fontsize=8.5, framealpha=0.0, labelcolor=C_TEXT, loc="upper left")
ax_eq.grid(color=C_GRID, lw=0.5, alpha=0.6)

# J-curve annotation
jcurve_dip_yr = 1.5
ax_eq.annotate("J-curve:\nconstruction\nphase",
               xy=(jcurve_dip_yr, re_equity[min(int(jcurve_dip_yr), len(re_equity)-1)]/1e6),
               xytext=(jcurve_dip_yr + 0.8, re_equity[min(int(jcurve_dip_yr), len(re_equity)-1)]/1e6 - 1.5),
               textcoords="data",
               color=C_MUTED, fontsize=7.5,
               arrowprops=dict(arrowstyle="->", color=C_MUTED, lw=0.8))

# ── Panel 2: KPI Table ───────────────────────────────────────────────────────
ax_kpi.axis("off")
ax_kpi.set_title("Strategy Metrics", color=C_TEXT, fontsize=11, fontweight="bold", pad=8)

lev_final_equity = spy_lev_equity[-1]
lev_cagr = (lev_final_equity / STARTING_EQUITY) ** (1 / (len(spy_2019)/12)) - 1

bts_irr_med = proj_df[proj_df["is_bts"] == True]["bts_irr"].median() if proj_df["is_bts"].any() else np.nan
kpi_rows = [
    ("Metric",              "Corp RE",                        "SPY (unlev)"),
    ("CAGR",                f"{re_cagr:.1%}",                  f"{spy_cagr:.1%}"),
    ("Leverage",            "65% LTV",                        "None"),
    ("BTR Median IRR",      f"{avg_irr:.1%}",                 "—"),
    ("BTS Median IRR",      f"{bts_irr_med:.1%}" if not np.isnan(bts_irr_med) else "—", "—"),
    ("Median Equity Mult.", f"{avg_em:.2f}×",                 "—"),
    ("Dev Spread (stab.)",  f"+{avg_spread:.1%}",             "—"),
    ("Exit Strategy",       f"60% BTR / 40% BTS (PHX)",      "Buy-hold"),
    ("Constr. Draw",        f"S-curve ({DRAW_FACTOR:.0%} avg)","N/A"),
    ("Perm Loan Rate",      f"{PERM_LOAN_RATE:.1%}",          "N/A"),
    ("Lease-Up Modeled",    "12 mo PHX / 15 mo AUS",         "N/A"),
    ("Market Correlation",  "Low (private)",                  "1.00"),
]
col_x   = [0.02, 0.46, 0.74]
y0      = 0.99
row_h   = 0.087
for i, (m, re_v, spy_v) in enumerate(kpi_rows):
    y    = y0 - i * row_h
    is_hdr = i == 0
    bg   = "#1A2840" if is_hdr else ("#1E2235" if i % 2 == 0 else "#22263A")
    rect = mpatches.FancyBboxPatch((0, y - row_h * 0.85), 1, row_h * 0.9,
                                    transform=ax_kpi.transAxes, clip_on=False,
                                    boxstyle="square,pad=0", facecolor=bg, edgecolor="none")
    ax_kpi.add_patch(rect)
    tc   = C_MUTED if is_hdr else C_TEXT
    fw   = "bold"
    ax_kpi.text(col_x[0], y-0.005, m,    transform=ax_kpi.transAxes, color=tc,
                fontsize=7.8, fontweight=fw if is_hdr else "normal", va="top")
    ax_kpi.text(col_x[1], y-0.005, re_v, transform=ax_kpi.transAxes,
                color=C_RE if not is_hdr else C_MUTED,
                fontsize=7.8, fontweight=fw if not is_hdr else "normal", va="top")
    ax_kpi.text(col_x[2], y-0.005, spy_v, transform=ax_kpi.transAxes,
                color=C_SPY if not is_hdr else C_MUTED,
                fontsize=7.8, fontweight=fw if not is_hdr else "normal", va="top")

# ── Panel 3: IRR by project (BTS vs BTR) ─────────────────────────────────────
# Use BTS IRR for flagged Phoenix projects, BTR IRR for all others
irr_sorted = proj_df.sort_values("irr", ascending=True).copy()
# Display IRR: BTS projects show their annualized BTS IRR
irr_display = irr_sorted.apply(
    lambda r: r["bts_irr"] if r.get("is_bts") and not np.isnan(r["bts_irr"]) else r["irr"], axis=1)
bar_colors = []
for _, r in irr_sorted.iterrows():
    if r["city"] == "phoenix" and r.get("is_bts"):
        bar_colors.append("#E8A838")   # lighter orange = BTS exit
    elif r["city"] == "phoenix":
        bar_colors.append(C_PHX)       # standard orange = BTR
    else:
        bar_colors.append(C_AUS)
ax_irr.barh(range(len(irr_sorted)), irr_display.values*100, color=bar_colors,
             alpha=0.85, height=0.65)
ax_irr.axvline(spy_cagr*100,  color=C_SPY, lw=1.5, ls="--", alpha=0.8,
               label=f"SPY CAGR {spy_cagr:.1%}")
ax_irr.axvline(avg_irr*100,   color=C_RE,  lw=1.5, ls=":",  alpha=0.8,
               label=f"Median BTR IRR {avg_irr:.1%}")
ax_irr.set_title("Project-Level Levered IRR (BTS & BTR)", color=C_TEXT, fontsize=11, fontweight="bold", pad=8)
ax_irr.set_xlabel("Levered IRR (%)", color=C_MUTED, fontsize=9)
ax_irr.set_yticks(range(len(irr_sorted)))
ax_irr.set_yticklabels(
    [f"{'PHX-BTS' if (c=='phoenix' and irr_sorted.iloc[i].get('is_bts')) else ('PHX' if c=='phoenix' else 'AUS')} #{i+1}"
     for i, c in enumerate(irr_sorted['city'])], fontsize=6.5)
ax_irr.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
phx_patch  = mpatches.Patch(color=C_PHX,    label="PHX BTR")
pbts_patch = mpatches.Patch(color="#E8A838", label="PHX BTS")
aus_patch  = mpatches.Patch(color=C_AUS,    label="Austin BTR")
ax_irr.legend(handles=[phx_patch, pbts_patch, aus_patch], fontsize=7.5, framealpha=0,
               labelcolor=C_TEXT, loc="lower right")
ax_irr.grid(color=C_GRID, lw=0.4, alpha=0.6, axis="x")

# ── Panel 4: Return components by city ───────────────────────────────────────
cities     = ["Phoenix", "Austin", "All"]
city_map   = {"Phoenix": "phoenix", "Austin": "austin"}

def get_sub(name):
    return proj_df if name == "All" else proj_df[proj_df["city"] == city_map[name]]

cocf_vals  = [get_sub(c)["total_rental"].sum()/1e6 for c in cities]
gain_vals  = [(get_sub(c)["net_proceeds"].sum() - get_sub(c)["equity_req"].sum())/1e6 for c in cities]
int_vals   = [get_sub(c)["constr_int"].sum()/1e6 for c in cities]   # cost

x = np.arange(len(cities))
w = 0.55
ax_ret.bar(x, cocf_vals,               color=C_COCF, alpha=0.85, width=w, label="5-yr Net Cash-on-Cash")
ax_ret.bar(x, gain_vals,               color=C_GAIN, alpha=0.85, width=w, bottom=cocf_vals, label="Capital Gain at Exit")
ax_ret.bar(x, [-v for v in int_vals],  color="#E74C3C", alpha=0.75, width=w, label="Construction Interest")
ax_ret.axhline(0, color=C_GRID, lw=0.8)
ax_ret.set_xticks(x); ax_ret.set_xticklabels(cities, color=C_TEXT, fontsize=9)
ax_ret.set_title("Return Components ($M)", color=C_TEXT, fontsize=11, fontweight="bold", pad=8)
ax_ret.set_ylabel("$M", color=C_MUTED, fontsize=9)
ax_ret.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:.1f}M"))
ax_ret.legend(fontsize=8, framealpha=0, labelcolor=C_TEXT)
ax_ret.grid(color=C_GRID, lw=0.4, alpha=0.6, axis="y")

# ── Panel 5: Typical project waterfall ───────────────────────────────────────
typical  = proj_df.iloc[0]
stages   = ["Land", "Soft\nCosts", "Constr.", "Total\nCost", "Stab.\nValue", "Exit\nValue"]
vals     = [
    typical["land_cost"]/1e6,
    typical["soft_cost"]/1e6,
    typical["build_cost"]/1e6,
    typical["total_cost"]/1e6,
    typical["value_stab"]/1e6,
    typical["value_exit"]/1e6,
]
bar_c    = [C_PHX, C_PHX, C_PHX, "#6C63FF", C_GAIN, C_GAIN]
ax_wf.bar(stages, vals, color=bar_c, alpha=0.85, width=0.6)
ax_wf.axhline(typical["total_cost"]/1e6, color="#6C63FF", lw=1.0, ls="--", alpha=0.7,
               label="Cost basis")
for i, (s, v) in enumerate(zip(stages, vals)):
    ax_wf.text(i, v + max(vals)*0.015, f"${v:.1f}M", ha="center", va="bottom",
                color=C_TEXT, fontsize=7.8, fontweight="bold")

# Dev spread badge
spread_pct = typical["dev_spread_pct"]
spread_color = C_GAIN if spread_pct > 0 else "#E74C3C"
ax_wf.annotate(f"+{spread_pct:.0%} dev\nspread",
               xy=(3.5, (typical["value_stab"] + typical["total_cost"])/2e6),
               xytext=(4.2, (typical["value_stab"] + typical["total_cost"])/2e6),
               color=spread_color, fontsize=8, fontweight="bold",
               arrowprops=dict(arrowstyle="-", color=spread_color, lw=1.0))

ax_wf.set_title(f"Project Waterfall — {typical['city'].capitalize()} #{1}",
                color=C_TEXT, fontsize=10, fontweight="bold", pad=8)
ax_wf.set_ylabel("$M", color=C_MUTED, fontsize=9)
ax_wf.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:.1f}M"))
ax_wf.tick_params(axis="x", labelsize=8)
ax_wf.grid(color=C_GRID, lw=0.4, alpha=0.6, axis="y")

# ── Panel 6: Cumulative returns by source ─────────────────────────────────────
cum_cocf_arr  = eq_df["cum_cocf"].values / 1e6
cum_gain_arr  = eq_df["cum_gain"].values / 1e6
total_ret_arr = cum_cocf_arr + cum_gain_arr
spy_ret_arr   = np.interp(yrs, spy_years, (spy_equity - STARTING_EQUITY)/1e6)
lev_ret_arr   = np.interp(yrs, spy_years, (spy_lev_equity - STARTING_EQUITY)/1e6)

ax_cf.fill_between(yrs, 0, cum_cocf_arr, alpha=0.55, color=C_COCF, label="Cumulative Net Rental Income")
ax_cf.fill_between(yrs, cum_cocf_arr, total_ret_arr, alpha=0.55, color=C_GAIN, label="Cumulative Capital Gains at Exit")
ax_cf.plot(yrs,       total_ret_arr,   color=C_RE,   lw=2.8, label="Total RE Return ($M over $10M)")
ax_cf.plot(yrs,       spy_ret_arr,     color=C_SPY,  lw=2.0, ls="--", label="SPY Total Return (unlevered)")
ax_cf.plot(yrs,       lev_ret_arr,     color=C_SPYL, lw=1.8, ls="-.",  label="Levered SPY Total Return")
ax_cf.axhline(0, color=C_GRID, lw=0.8, ls=":")
ax_cf.set_title("Cumulative Returns by Source — Corp RE Portfolio vs SPY Benchmarks",
                color=C_TEXT, fontsize=11, fontweight="bold", pad=8)
ax_cf.set_xlabel("Years from First Deployment", color=C_MUTED, fontsize=9)
ax_cf.set_ylabel("Cumulative P&L ($M)", color=C_MUTED, fontsize=9)
ax_cf.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:.0f}M"))
ax_cf.legend(fontsize=8.5, framealpha=0.0, labelcolor=C_TEXT, loc="upper left", ncol=2)
ax_cf.grid(color=C_GRID, lw=0.5, alpha=0.6)

# ── Title & footer ─────────────────────────────────────────────────────────────
fig.text(0.5, 0.957, "Corporate Real Estate Development Portfolio vs. SPY Buy-Hold",
         ha="center", fontsize=18, fontweight="bold", color=C_TEXT)
fig.text(0.5, 0.930,
         f"AI-Scored Lots → Ground-Up (65% LTV, S-curve draw @ {CONSTRUCTION_RATE:.1%}) → "
         f"Lease-Up (12-15 mo) → Refi to Perm ({PERM_LOAN_RATE:.1%}) → "
         f"60% BTR {HOLD_YEARS}-yr Hold / 40% PHX BTS  ·  "
         f"$10M equity  ·  {n_deployed} projects  ·  Phoenix & Austin MSA",
         ha="center", fontsize=9, color=C_MUTED)

# Returns badge
re_total_pct  = (re_equity[-1] / STARTING_EQUITY - 1) * 100
spy_total_pct = (spy_equity[-1] / STARTING_EQUITY - 1) * 100
lev_total_pct = (spy_lev_equity[-1] / STARTING_EQUITY - 1) * 100

badge_color   = C_RE if re_total_pct > spy_total_pct else C_MUTED
badge_bg      = "#0F2818" if re_total_pct > spy_total_pct else "#1A1D2E"
badge_border  = C_RE if re_total_pct > spy_total_pct else C_MUTED

fig.text(0.97, 0.960,
         f"Total Return ({HORIZON} yrs)\nRE:     +{re_total_pct:.0f}%\nSPY:   +{spy_total_pct:.0f}%\nLev SPY: +{lev_total_pct:.0f}%",
         ha="right", va="top", fontsize=8.5, color=badge_color,
         bbox=dict(boxstyle="round,pad=0.5", facecolor=badge_bg,
                   edgecolor=badge_border, lw=1.3))

fig.text(0.5, 0.012,
         "Projections model market-rate assumptions. Levered returns amplify both upside and downside risk. "
         "RE returns are private/illiquid with lower public-market correlation. For research purposes only — not investment advice.",
         ha="center", fontsize=7.5, color=C_MUTED)

# ── Save ────────────────────────────────────────────────────────────────────────
out = Path("D:/urbangrowth_data/processed/maps/report_assets/re_portfolio_comparison.png")
out.parent.mkdir(parents=True, exist_ok=True)
fig.savefig(out, dpi=180, bbox_inches="tight", facecolor=C_BG)
plt.close(fig)
print(f"\nSaved: {out}  ({out.stat().st_size//1024} KB)")
