# Data Sources & Validation

## Satellite / Remote Sensing

| Source | Usage | Validation |
|--------|-------|------------|
| Sentinel-2 (ESA, 10m resolution) | `built_pct` per H3 cell | NDVI + NDBI indices; manual spot-check against Google Maps |
| USGS National Elevation Dataset (DEM) | Terrain slope | Cross-checked against ArcGIS hillshade |
| FEMA National Flood Hazard Layer | Flood zone classification | Directly from FEMA REST API |

## Real Estate Market Data

| Source | Data | Last Validated |
|--------|------|----------------|
| CBRE Q2 2025 Cap Rate Survey | Exit cap rates: Phoenix residential 4.8–5.2%, Austin 5.0–5.5% | Q2 2025 |
| Matthews MMREIS Q4 2025 | NNN cap rate comps (commercial: 6.2%) | Q4 2025 |
| RealPage Analytics 2025 | Effective rents: $1.75/sqft/mo Phoenix, $1.78 Austin Tier-1 | 2025 |
| Mortenson Cost Index Phoenix 2025 | Hard cost: $155–195/sqft Type-V wood frame | 2025 |
| EVstudio / Maxx Austin 2024 | Hard cost: $130–155/sqft + 5%/yr inflation adj → $155 | 2025 |
| Innowave Q3 2025 | Cap rate directional confirmation | Q3 2025 |

## Economic / Demographic Data

| Source | Data | Frequency |
|--------|------|-----------|
| US Census Bureau ACS 5-year | Tract-level income, population | Annual |
| BLS QCEW | County-level job growth by sector | Quarterly |
| CoStar Group | Permit counts, absorption rates | Quarterly |
| Maricopa County Assessor | Phoenix parcel values, ownership | Annual |
| Travis County Assessor | Austin parcel values | Annual |

## Permits Data

| Market | Source | Notes |
|--------|--------|-------|
| Phoenix | City of Phoenix / Maricopa permit portal | API broke Q3 2020; signal degraded post-2020 |
| Austin | City of Austin Development Services API | Full 2018–2023 coverage; COVID disruption evident |

## Financial Market Data

| Source | Data | Period |
|--------|------|--------|
| Yahoo Finance / yfinance | SPY daily prices, total return | 2018–2024 |
| CRSP (via Wharton WRDS) | REIT universe returns, fundamentals | 2018–2024 |
| Compustat | Balance sheet data (P/B, debt/equity) | Quarterly |
| FactSet | Consensus estimates, FFO | Monthly |

---

## Validation Notes

### Construction Costs
Both Phoenix and Austin costs validated against third-party contractor indices:
- Phoenix: Mortenson 2025 puts Type-V garden-style at $155–195/sqft hard cost → model uses $175
- Austin: EVstudio 2024 + 5%/yr inflation → ~$155 hard cost → model uses $155
- Soft costs (architect, permits, dev fee, A&E): 15% — market standard

### Rent Assumptions
- Phoenix effective rents validated against RealPage 2025 ($1.75–1.90/sqft asking; 6–8 week concessions → $1.75 effective)
- Austin Tier-1 (South/Central): $1.78 effective; suburban (RR, Pflugerville): $1.55 effective (-10 to -11% YoY in Q3 2025)
- Model uses $1.75 Phoenix and blended $1.67 Austin (weighted by Tier-1 location mix)

### Cap Rates
- Exit cap of 5.1% blended (5.0% Phoenix BTS, 5.2% Austin BTR) is **conservative end of CBRE range**
- Compresses to ~4.8% in premium Phoenix submarkets (Chandler, Scottsdale)
- Stress test: +50 bps cap rate expansion reduces NAV by approximately $2.1M (7% impact)

### Population Growth
- Phoenix MSA: +2.1% CAGR (2020–2025, Census estimates); driven by California/Midwest outmigration
- Austin MSA: +2.8% CAGR (2020–2025); tech employment anchor (Apple, Tesla, Oracle, Meta campuses)
