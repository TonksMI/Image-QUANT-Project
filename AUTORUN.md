# Urban Growth — Unattended Pipeline Instructions
# For use with: claude --dangerously-skip-permissions

## Environment
- Python: D:\envs\urbangrowth\python.exe
- Working dir: C:\Users\17ton\urbangrowth
- PYTHONPATH: src
- DB: PostgreSQL urbangrowth@localhost:5432 (see .env)
- Data root: D:\urbangrowth_data

## Current State (as of 2026-05-09)
- DB signal_features: 72,682 rows (FERC + census_bps + FRED + city_growth + usaspending)
- ferc_queue: 303 rows (CAISO only — PJM/SPP/MISO/ERCOT/NYISO URLs all stale/blocked)
- dot_tips: 510 rows (TX only — CA/AZ sources returning 404)
- city_permits: 189,863 rows (Austin only — Phoenix API returns 404)
- h3_predictions: 7,122 rows (Phoenix + Austin, as_of 2026-05-08, model=gbm_v1)
- Ridge H=1: Sharpe=0.106, CAGR=2.76%, alpha=7.59%, hit_rate=58.6%
- Ridge H=2: Sharpe=0.266, CAGR=8.44%, alpha=15.49%, hit_rate=62.3% ← best horizon
- Ridge H=3: Sharpe=0.238, alpha=11.7% (from before sector_tag fix; re-run pending)
- H3 GBM classifier: AUC=0.91, accuracy=89.6%, F1_pos=0.59 (top features: veg_pct, bare_to_built)

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

## Generating city investment maps
```
PYTHONPATH=src D:\envs\urbangrowth\python.exe -m urbangrowth.dashboards.city_growth_map
```
Output: D:\urbangrowth_data\processed\maps\city_growth_map_{phoenix,austin}.html

## Pending Tasks (run in order)

### 1. Re-run Ridge H=3 backtest with sector_tag fix
```
PYTHONPATH=src D:\envs\urbangrowth\python.exe -c "
import os; os.chdir('C:/Users/17ton/urbangrowth')
from urbangrowth.modeling.backtest import run
run(model_type='ridge', horizon=3)
"
```

### 2. Fix FERC ISOs — stale URLs need manual lookup
- PJM: https://pjm.com/pub/planning/iq_queues/xcl_queue.xls → 404, check https://pjm.com/planning/project-connect
- SPP: https://www.spp.org/documents/36282/ → 404, check https://www.spp.org/markets-operations/
- MISO: 403 blocked, needs cookie/browser session
- ERCOT: check https://www.ercot.com/gridinfo/resource for current GIS report link
- NYISO: check https://www.nyiso.com/interconnections

After fixing any ISO URL, re-run:
```
PYTHONPATH=src D:\envs\urbangrowth\python.exe -m urbangrowth.data.ferc_queue
PYTHONPATH=src D:\envs\urbangrowth\python.exe -m urbangrowth.signals.ferc_signals
```

### 3. Phoenix city permits — API returning 404
Phoenix open data resource ID dvuu-gqtg gives 404. Check:
  https://www.phoenixopendata.com/dataset/development-services-permits

### 4. Improve H3 classifier with permit data
Now that Austin permits are ingested (189k rows), re-run the classifier to leverage permit signals.
The current model has 0 importance for permit_count/permit_valuation because h3_features.permit_count
was mostly 0 at training time. After updating h3_features with fresh permit data, re-run:
```
PYTHONPATH=src D:\envs\urbangrowth\python.exe -m urbangrowth.modeling.h3_classifier
PYTHONPATH=src D:\envs\urbangrowth\python.exe -m urbangrowth.dashboards.city_growth_map
```

### 5. Run dot_tips signals
```
PYTHONPATH=src D:\envs\urbangrowth\python.exe -m urbangrowth.signals.dot_signals
```

## Key File Locations
- Model output: D:\urbangrowth_data\processed\signal_tables\
- H3 predictions: D:\urbangrowth_data\processed\signals\h3_pred.parquet
- City maps: D:\urbangrowth_data\processed\maps\
- Raw FERC data: D:\urbangrowth_data\raw\ferc_queue\
- Raw TxDOT data: D:\urbangrowth_data\raw\dot_tips\tx\
- Raw Austin permits: D:\urbangrowth_data\raw\city_permits\austin\
- Census ACS: D:\urbangrowth_data\raw\census_acs\{phoenix,austin}\
- Signals code: src/urbangrowth/signals/
- Model code: src/urbangrowth/modeling/cross_section.py
- H3 classifier: src/urbangrowth/modeling/h3_classifier.py
- City map: src/urbangrowth/dashboards/city_growth_map.py
- DB schema: src/urbangrowth/db/schema.sql

## Fixes Applied This Session (2026-05-09)
- db/loaders.py: added load_dotenv() so POSTGRES_PASSWORD is picked up — backtest now gets real returns
- ferc_queue.py: added _SSL_SKIP_HOSTS + legacy TLS adapter; added .xls extension support for PJM
- city_permits.py: fixed Austin normalizer (permit_number not permitnum), PostGIS-optional upsert, added lat/lon cols
- modeling/h3_classifier.py: NEW — GBM sub-city investment classifier (AUC=0.91)
- dashboards/city_growth_map.py: NEW — Folium interactive H3 investment score map

## Check DB
```
PYTHONPATH=src D:\envs\urbangrowth\python.exe check_db.py
```
