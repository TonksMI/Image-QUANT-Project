# Urban Growth — Unattended Pipeline Instructions
# For use with: claude --dangerously-skip-permissions

## Environment
- Python: D:\envs\urbangrowth\python.exe
- Working dir: C:\Users\17ton\urbangrowth
- PYTHONPATH: src  (or C:/Users/17ton/urbangrowth/src for explicit runs)
- DB: PostgreSQL urbangrowth@localhost:5432 (see .env)
- Data root: D:\urbangrowth_data

## Current State (as of 2026-05-10, session 3)
- DB signal_features: 88,522 rows (FERC + census_bps + FRED + city_growth + usaspending)
- ferc_queue: 462 rows (CAISO + NYISO — PJM/SPP/MISO/ERCOT/ISO-NE stale/blocked)
- dot_tips: 510 rows (TX only — awarded_date=NULL in all rows, signals blocked)
- city_permits: 189,863 rows (Austin only — Phoenix API returns 404)
- h3_predictions: 7,122 rows (Phoenix + Austin, as_of 2026-05-08, model=gbm_v1) [h3_classifier rewritten — AUC now honest 0.58]
- lot_opportunities: 4,775 Phoenix + 4,110 Austin cells (tier1=478/411)
- h3_parcel_enrichments: 4,775 Phoenix + 1,944 Austin cells (property type, land value, addresses)
- Ridge H=2: Sharpe=0.266, CAGR=8.44%, alpha=15.49%, hit_rate=62.3% ← best horizon
- H3 GBM classifier: AUC=0.58 (honest temporal split), F1_pos=0.59 (top features: veg_pct, bare_to_built)
- Lot backtest IC: Phoenix 0.77 (2019) | Austin 0.68 (2018), negative 2020-2022 (COVID disruption, real finding)
- Maps: D:\urbangrowth_data\processed\maps\lot_map_{phoenix,austin}.html (popups: property type, land value, build timeline, cost, address)

## Running backtest
```
PYTHONPATH=src D:\envs\urbangrowth\python.exe -c "
import os; os.chdir('C:/Users/17ton/urbangrowth')
from urbangrowth.modeling.backtest import run
run(model_type='ridge', horizon=1)
run(model_type='ridge', horizon=2)
run(model_type='ridge', horizon=3)
"
```

## Running H3 investment classifier
```
PYTHONPATH=src D:\envs\urbangrowth\python.exe -m urbangrowth.modeling.h3_classifier
```
Output: D:\urbangrowth_data\processed\signals\h3_pred.parquet + h3_predictions table

## Running empty lot opportunity finder + backtest
```
PYTHONPATH=C:/Users/17ton/urbangrowth/src D:\envs\urbangrowth\python.exe -c "
import os; os.chdir('C:/Users/17ton/urbangrowth')
from urbangrowth.modeling.lot_finder import run
run()
"
```
Output:
  D:\urbangrowth_data\processed\signals\lot_opportunities_{city}.parquet
  D:\urbangrowth_data\processed\signals\lot_backtest_{city}.parquet
  DB table: lot_opportunities

## Generating city investment maps
```
PYTHONPATH=C:/Users/17ton/urbangrowth/src D:\envs\urbangrowth\python.exe -c "
import os; os.chdir('C:/Users/17ton/urbangrowth')
from urbangrowth.dashboards.city_growth_map import run
run()
"
```
Output: D:\urbangrowth_data\processed\maps\city_growth_map_{phoenix,austin}.html

## Generating empty lot maps
```
PYTHONPATH=C:/Users/17ton/urbangrowth/src D:\envs\urbangrowth\python.exe -c "
import os; os.chdir('C:/Users/17ton/urbangrowth')
from urbangrowth.dashboards.lot_map import run
run()
"
```
Output: D:\urbangrowth_data\processed\maps\lot_map_{phoenix,austin}.html

## Running parcel enrichment (property type, land value, addresses)
```
# Austin (Socrata land database — 271k parcels → H3 aggregation):
PYTHONPATH=src D:\envs\urbangrowth\python.exe -m urbangrowth.data.parcel_enrichment --city austin --no-geocode

# Phoenix (distance-curve land values only — no parcel API):
PYTHONPATH=src D:\envs\urbangrowth\python.exe -m urbangrowth.data.parcel_enrichment --city phoenix

# Geocode top opportunity cells (Nominatim, 1.1s/cell, ~300 cells per city = ~11 min):
PYTHONPATH=src D:\envs\urbangrowth\python.exe -c "
from urbangrowth.data.parcel_enrichment import _engine, _geocode_opportunity_cells, _update_addresses
engine = _engine()
for cid in [1, 2]:
    addr = _geocode_opportunity_cells(engine, city_id=cid, limit=300)
    _update_addresses(addr, city_id=cid, engine=engine)
"

# After geocoding, sync addresses to lot_opportunities and regenerate maps:
PYTHONPATH=src D:\envs\urbangrowth\python.exe -c "
from urbangrowth.db.loaders import _engine
from sqlalchemy import text
engine = _engine()
with engine.begin() as conn:
    conn.execute(text('''
        UPDATE lot_opportunities lo
        SET nearest_address = pe.nearest_address
        FROM h3_parcel_enrichments pe
        WHERE lo.h3_index = pe.h3_index AND lo.city_id = pe.city_id
          AND pe.nearest_address IS NOT NULL AND pe.nearest_address != ''
    '''))
from urbangrowth.dashboards.lot_map import run
run()
"
```
Output: h3_parcel_enrichments table + lot_opportunities (address, property type, land value, build timeline, cost)

## Pending Tasks (run in order)

### 1. Fix FERC ISOs — stale URLs need manual lookup
- PJM: https://pjm.com/pub/planning/iq_queues/xcl_queue.xls → 404, check https://pjm.com/planning/project-connect
- SPP: https://www.spp.org/documents/36282/ → 404, check https://www.spp.org/markets-operations/
- MISO: 403 blocked, needs cookie/browser session
- ERCOT: check https://www.ercot.com/gridinfo/resource for current GIS report link
- ISO-NE: check https://www.iso-ne.com/isoexpress/web/reports/operations/-/tree/interfaces

After fixing any ISO URL, re-run:
```
PYTHONPATH=src D:\envs\urbangrowth\python.exe -m urbangrowth.data.ferc_queue
PYTHONPATH=src D:\envs\urbangrowth\python.exe -m urbangrowth.signals.ferc_signals
```

### 2. Phoenix city permits — API returning 404
Phoenix open data resource ID dvuu-gqtg gives 404. Check:
  https://www.phoenixopendata.com/dataset/development-services-permits

### 3. Fix Austin zoning ingestion
Zoning dataset moved. Resource ID q3y3-ungd is in code (zoning.py line 71) but returned 0 rows.
Check the actual column name for zoning type — current code looks for "zoning_zty".
  https://data.austintexas.gov/resource/q3y3-ungd.geojson

After fixing, re-run:
```
PYTHONPATH=src D:\envs\urbangrowth\python.exe -m urbangrowth.data.zoning --city austin
```
Then re-run lot_finder to populate zoning_category in lot_opportunities.

### 4. TCAD appraisal data — for parcel-level lot valuation backtest
Downloads at traviscad.org/publicinformation → EARS exports (2023-2025, 70-148MB each)
Fixed-width format documented in TP_Legacy8.0.32-AppraisalExportLayout.xlsx
Key fields: STATE_CD (C1=vacant lot), land_value by year, property address
Goal: build parcel-level time series of land value for vacant lots → backtest lot_finder

### 5. Re-run H3 classifier after Austin permit data aggregation
Austin permits are in city_permits (189k rows with lat/lon).
Aggregate to H3 cells and update h3_features.permit_count, then re-run:
```
PYTHONPATH=src D:\envs\urbangrowth\python.exe -m urbangrowth.modeling.h3_classifier
PYTHONPATH=src D:\envs\urbangrowth\python.exe -m urbangrowth.dashboards.city_growth_map
```

### 6. Run dot_tips signals
TxDOT data has awarded_date=NULL — either fix scraper or use fiscal_year as proxy:
```
PYTHONPATH=src D:\envs\urbangrowth\python.exe -m urbangrowth.signals.dot_signals
```

## Key File Locations
- Model output: D:\urbangrowth_data\processed\signal_tables\
- H3 predictions: D:\urbangrowth_data\processed\signals\h3_pred.parquet
- Lot opportunities: D:\urbangrowth_data\processed\signals\lot_opportunities_{city}.parquet
- Lot backtest: D:\urbangrowth_data\processed\signals\lot_backtest_{city}.parquet
- City maps: D:\urbangrowth_data\processed\maps\
- Raw FERC data: D:\urbangrowth_data\raw\ferc_queue\
- Raw TxDOT data: D:\urbangrowth_data\raw\dot_tips\tx\
- Raw Austin permits: D:\urbangrowth_data\raw\city_permits\austin\
- Census ACS: D:\urbangrowth_data\raw\census_acs\{phoenix,austin}\
- Signals code: src/urbangrowth/signals/
- Model code: src/urbangrowth/modeling/cross_section.py
- H3 classifier: src/urbangrowth/modeling/h3_classifier.py
- Lot finder: src/urbangrowth/modeling/lot_finder.py  ← NEW
- City map: src/urbangrowth/dashboards/city_growth_map.py
- Lot map: src/urbangrowth/dashboards/lot_map.py  ← NEW
- DB schema: src/urbangrowth/db/schema.sql

## Fixes Applied This Session (2026-05-10)
- data/zoning.py: fixed Austin resource ID from 5rzy-nm5e → q3y3-ungd (in both main and worktree)
- modeling/lot_finder.py: NEW — empty lot opportunity finder with walk-forward backtest
  - Combines h3_features (built_pct), h3_predictions (investment_score), city_permits (permit_velocity)
  - Adaptive vacancy threshold (data-relative, handles cities with different built_pct scales)
  - Backtest: IC=0.66-0.83 Phoenix, IC=0.28-0.99 Austin over 2018-2023
  - Output: lot_opportunities table (2120 Phoenix + 1689 Austin cells)
- dashboards/lot_map.py: NEW — Folium interactive H3 vacant lot opportunity map

## Fixes Applied Previous Session (2026-05-09)
- db/loaders.py: added load_dotenv() so POSTGRES_PASSWORD is picked up — backtest now gets real returns
- ferc_queue.py: added _SSL_SKIP_HOSTS + legacy TLS adapter; added .xls extension support for PJM
- ferc_queue.py: NYISO dynamic URL discovery from interconnections page (dynamic xlsx link)
- city_permits.py: fixed Austin normalizer (permit_number not permitnum), PostGIS-optional upsert, added lat/lon cols
- modeling/h3_classifier.py: NEW — GBM sub-city investment classifier (AUC=0.91)
- dashboards/city_growth_map.py: NEW — Folium interactive H3 investment score map

## Check DB
```
PYTHONPATH=src D:\envs\urbangrowth\python.exe check_db.py
```
