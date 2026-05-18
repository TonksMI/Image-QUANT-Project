# Data — Flat-File Subset for Offline Analysis

This folder contains a representative subset of the Urban Growth database so anyone can run the analysis scripts without a PostgreSQL connection.

## Files

| File | Rows | Description |
|------|------|-------------|
| `lots_phoenix.csv` | 30 | Top 30 Phoenix Tier-1 opportunities (by score) |
| `lots_austin.csv` | 30 | Top 30 Austin Tier-1 opportunities (by score) |
| `lots_combined_top60.csv` | 60 | Both cities combined, ranked |
| `cities.csv` | 2 | City reference table (city_id, name, state) |
| `ic_backtest.json` | — | Walk-forward Spearman IC by city and year |
| `model_assumptions.json` | — | All financial model constants (cap rates, costs, LTV, etc.) |

## Lot CSV Columns

| Column | Type | Description |
|--------|------|-------------|
| `h3_index` | string | H3 resolution-8 cell identifier |
| `opportunity_score` | float | Model score 0–1 (higher = better) |
| `acreage_est` | float | Estimated developable acres (~208 acres per H3 cell) |
| `dist_to_center_km` | float | Distance from city CBD in km |
| `dominant_property_type` | string | residential / commercial / industrial / vacant / unknown |
| `est_land_value_acre` | float | Estimated land value per acre ($) |
| `est_construction_months` | int | Estimated build timeline |
| `est_cost_per_sqft` | float | Hard construction cost estimate ($/sqft) |
| `nearest_address` | string | Nearest street address (may be empty) |
| `city_name` | string | phoenix or austin |
| `tier` | string | "Tier 1 — Top 10%" |
| `lat` | float | Cell centroid latitude |
| `lon` | float | Cell centroid longitude |

## Running Without a Database

All generator scripts auto-detect whether PostgreSQL is available. If the DB connection fails, they fall back to these CSV files:

```bash
# Clone the repo
git clone https://github.com/TonksMI/Image-QUANT-Project.git
cd Image-QUANT-Project

# Install dependencies
pip install -e ".[dev]"

# Run portfolio simulation (uses data/ CSVs automatically if no DB)
python generate_re_portfolio_graphic.py

# Run IC chart + lots
python gen_ic_and_lots.py

# Generate HTML report
python generate_report.py

# Generate Chapman deck
python generate_chapman_deck.py
```

No `.env` file or database credentials needed — the scripts will detect no DB and load from `data/`.

## Full Dataset

The complete 2,229-opportunity dataset is in `docs/`:
- `docs/_phx_opps.csv` — 1,201 Phoenix cells (478 Tier-1, 723 Tier-2)
- `docs/_aus_opps.csv` — 1,028 Austin cells (411 Tier-1, 617 Tier-2)
