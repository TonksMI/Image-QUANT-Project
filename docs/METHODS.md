# Methods

Technical documentation for the Urban Growth Research Platform signal pipeline,
modeling methodology, and backtest assumptions.

---

## 1. Data Sources

### 1.1 Census Building Permits Survey (BPS)

**Provenance:** US Census Bureau, released monthly with approximately 6-week lag.
**Geographic coverage:** National, 9 Census divisions, ~400 MSAs.
**Variables:** Residential units authorized (1-unit, 2-4 unit, 5+ unit), total construction value.
**Download:** `https://www.census.gov/construction/bps/` — monthly CSV files.
**Implementation lag applied:** 2 months (6-week publication lag + 2-week processing buffer).

Signal construction: YoY percent change in 1-unit permits at national and MSA level.
24-month rolling z-score applied. Ticker weights from `config/universe.yaml`
`homebuilder_sensitivity` and `geographic_concentration` fields.

### 1.2 FERC Interconnection Queue

**Provenance:** Federal Energy Regulatory Commission. Project-level queue snapshots
published approximately weekly on ferc.gov.
**Variables:** Project MW capacity, fuel type (solar, wind, gas, storage), ISO/RTO region,
queue status (active, withdrawn, online).
**Implementation lag:** 1 month.
**Structural break warning:** PJM's 2022 queue reform and MISO cluster studies introduced
level-shifts in active MW counts. Pre-2020 data should be treated with caution.

Signal construction: Net new active MW additions per quarter by ISO and fuel type.
Relevant for energy-infrastructure contractors (PRIM, PWR, MYR, IEA). Zero weight
for homebuilders and pure-materials names.

### 1.3 USASpending Federal Construction Contracts

**Provenance:** USASpending.gov API v2. Daily updates.
**NAICS filter:** 2383 (structural steel erection), 2370 (highway/street construction),
2371 (water/sewer construction), 2361 (industrial building construction).
**Variables:** Award amount, recipient name, recipient DUNS/UEI, place of performance.
**Implementation lag:** 1 month.
**Known limitation:** Subsidiary and JV matching is incomplete. Awards to subsidiaries
(e.g., a Jacobs subsidiary) may not be attributed to the parent ticker.

Signal construction: Monthly award dollar volume per ticker, 3-month moving sum,
12-month rolling z-score.

### 1.4 FRED Macro Series

**Provenance:** Federal Reserve Bank of St. Louis FRED API.
**Series list:** Defined in `config/fred_series.yaml`. Key series:
- `TLNRESCONS`: Total non-residential construction spending (monthly, seasonally adjusted)
- `PRRESCONS`: Private residential construction spending
- `MORTGAGE30US`: 30-year fixed mortgage rate (weekly, last-of-month)
- `T10Y2Y`: 10Y-2Y Treasury spread
- Additional: lumber prices, employment in construction, housing starts

**Implementation lag:** 1 month for monthly series; same-week for weekly series.

Signal construction: YoY change, 6-month z-score, level. Sector applicability from
`config/fred_series.yaml` `sector_applicability` field.

### 1.5 State DOT Transportation Improvement Programs

**Provenance:** Texas DOT, Caltrans, ADOT — quarterly TIP publications.
**Variables:** Project dollar value, county, project type (highway, bridge, transit).
**Implementation lag:** 1 quarter.
**Reliability:** Scraped from PDF/Excel; format is unstable quarter-to-quarter.
Use with caution; validate before extending history.

Signal construction: State-level quarterly award total, mapped to ticker via
`geographic_concentration` weights for infrastructure-heavy names.

### 1.6 Sentinel-2 L2A Satellite Imagery

**Provenance:** ESA Copernicus programme. Accessed via Microsoft Planetary Computer
STAC API. License: CC BY-SA 3.0 IGO (attribution required).
**Spatial resolution:** 10 m × 10 m.
**Revisit frequency:** ~5 days (combined with Sentinel-2B).
**Cloud masking:** Scene Classification Layer (SCL) mask applied; pixels classified
as cloud, shadow, or cirrus are excluded.

**Composite generation:** For each calendar month, all available L2A scenes within
the city bounding box are cloud-masked. Monthly composite = median of valid pixels.
Output bands: B02 (blue), B03 (green), B04 (red), B08 (NIR).

**Land-cover segmentation:** DynamicWorld v1 probability outputs used where available.
Custom Prithvi foundation model (IBM/NASA) fine-tuned on 100 manually labeled Phoenix
tiles for improved separation of built vs bare-soil in desert urban areas.

**H3 aggregation:** Raster output resampled to H3 resolution 8 hexagonal grid
(cell area ≈ 0.74 km²). Per-cell built fraction = fraction of 10m pixels classified
as built within the H3 hex boundary.

**Transition matrices:** Month-over-month change in per-cell land-cover class.
Cells transitioning from non-built to built are "new construction events."

### 1.7 Yahoo Finance OHLCV

**Provenance:** Yahoo Finance via `yfinance` library. Non-commercial, delayed data.
**Variables:** Adjusted close price, volume. Monthly OHLCV.
**Implementation lag:** Same-day for EOD data.
**License restriction:** Must not be used for commercial redistribution.

Monthly return = `adjusted_close(t) / adjusted_close(t-1) - 1`.

---

## 2. Universe Construction

The ~50-stock universe is defined in `config/universe.yaml`. Inclusion criteria:
- Primary listing on US exchange (NYSE or NASDAQ)
- SIC/NAICS code in: homebuilding (1521), building materials (5031–5039),
  specialty trade contractors (1711–1799), heavy construction (1611–1629),
  electrical contractors (1731), engineering/construction (8711)
- Minimum $500M market cap as of 2020-01-01
- Sufficient price history from 2015

Universe is static (no survivorship bias mitigation in current version). SPY, XHB,
and PAVE are loaded as benchmark series only, not included in the cross-section.

---

## 3. Signal Processing

### 3.1 Cross-Sectional Z-Scoring

Each (period, feature_name) group is standardized independently:

```
z(t, i) = (x(t, i) - mean_j[x(t, j)]) / std_j[x(t, j)]
```

Groups with a single valid observation produce NaN (std → NaN after replace(0→NaN)).
This removes time-series level variation; only cross-sectional rank information is retained.

### 3.2 Rolling Z-Score

Time-series z-score applied before cross-sectional z-scoring where appropriate:

```
z_roll(t) = (x(t) - mean(x[t-w:t])) / std(x[t-w:t])
```

Window `w=24` months. `min_periods = max(4, w//2)`.

### 3.3 Ticker Applicability Weights

National signals have a single value per period. To create cross-sectional dispersion,
each ticker's signal value is multiplied by a sector sensitivity weight:

```
signal(t, i) = national_value(t) × sensitivity(i)
```

Weights from `config/universe.yaml` per-ticker `homebuilder_sensitivity`,
`energy_infra_sensitivity`, `materials_sensitivity`, `geographic_concentration`.
Range: [0.0, 1.0]. Tickers with weight=0 receive NaN for that signal family.

**Important:** This modeled dispersion is not observed heterogeneity. The IC
tests measure whether sector-weighted national signals predict cross-sectional
return variation, not firm-specific sensitivity to construction activity.

### 3.4 Implementation Lag Enforcement

All signals are joined to the return table on the *lagged* period:

```
signal at period t → predicts return at period t+h
```

Census BPS period `t` refers to permits issued *through month t*, released at ~t+6weeks.
We apply a 2-month lag to ensure no look-ahead. FERC and USASpending: 1-month lag.
Satellite composites: 1-month lag (composite available early in month t+1).

---

## 4. IC Methodology

### 4.1 Spearman Rank IC

For each (signal, horizon, month t):

```
IC(t) = SpearmanR(signal_rank(t), fwd_return_rank(t+h))
```

Computed only for months with ≥10 valid ticker observations.

Summary statistics: IC mean, IC std, IC IR = mean/std, Newey-West t-stat (see §4.2),
95% bootstrap confidence interval (see §4.3), number of months.

### 4.2 Newey-West HAC t-Statistic

IC series exhibit autocorrelation (signals are persistent). OLS standard errors
are inconsistent. We use Newey-West HAC with automatic lag selection:

```
lags = 4 × (T / 100)^(2/9)
```

where T = number of valid IC months. Implemented via `statsmodels.OLS.fit(cov_type="HAC")`.

### 4.3 Bootstrap Confidence Interval

1000 bootstrap resamples (with replacement) of the IC time series.
95% CI = [2.5th percentile, 97.5th percentile] of bootstrap IC means.
Standard bootstrap (not block bootstrap) since residual autocorrelation in IC
series is small after NW correction.

### 4.4 BH-FDR Correction

All (signal, horizon) tests pooled. Benjamini-Hochberg (1995) correction at α=0.05.
Raw p-values computed from NW t-statistics using two-sided t-distribution CDF.
Rejected null = FDR-adjusted p < 0.05.

---

## 5. Cross-Sectional Model

### 5.1 Feature Matrix

For each period t, the feature matrix X(t) has shape [n_tickers × n_features]:
- All signal z-scores from all six signal families (NaN-filled with 0)
- `momentum_12m`: 12-month trailing compound return, 1-month implementation lag
- `volume_norm`: 3m/12m average daily volume ratio, 1-month implementation lag
- Sector one-hot dummies from `config/universe.yaml` sector field

NaN imputation: `SimpleImputer(strategy="constant", fill_value=0.0)`. For z-scored
features, 0 = cross-section mean; this is appropriate neutral imputation.

### 5.2 Walk-Forward Expanding Window

For each period t starting at `min_train_months=24`:

1. Assemble training set: all (feature, fwd_return) pairs with period < t
2. Fit model on training data
3. Predict scores for all tickers at period t
4. Step to t+1

No walk-forward validation is used for hyperparameter tuning (parameters are fixed
at values recommended for small-cross-section settings).

### 5.3 Model Configurations

**Ridge regression:**
```python
StandardScaler() → Ridge(alpha=1.0)
```
L2 regularization prevents overfitting when n_tickers < n_features.
Features must be scaled before Ridge.

**ElasticNet:**
```python
StandardScaler() → ElasticNet(alpha=0.1, l1_ratio=0.5, max_iter=2000)
```
Performs feature selection via L1 penalty while retaining Ridge stability.

**LightGBM:**
```python
LGBMRegressor(n_estimators=100, num_leaves=15, learning_rate=0.05,
              subsample=0.8, colsample_bytree=0.8)
```
`num_leaves=15` is deliberately shallow to prevent overfitting in 50-ticker
cross-sections. SHAP values computed for feature importance.

---

## 6. Backtest

### 6.1 Portfolio Construction

Each month t:
1. Sort tickers by walk-forward score (descending)
2. Long: top 20% by count (equal-weight)
3. Short: bottom 20% by count (equal-weight)
4. Compute period return:
   ```
   long_ret(t)  = mean(fwd_return(t, long_tickers))
   short_ret(t) = mean(fwd_return(t, short_tickers))
   ls_gross(t)  = long_ret(t) - short_ret(t)
   ```

### 6.2 Transaction Cost Model

One-way TC = 10 bps per dollar rebalanced (round-trip = 20 bps).

```
long_delta  = Σ |w_long(t,i) - w_long(t-1,i)|  over all i
short_delta = Σ |w_short(t,i) - w_short(t-1,i)| over all i
tc(t) = (10 / 10000) × (long_delta + short_delta)
ls_net(t) = ls_gross(t) - tc(t)
```

`long_delta` and `short_delta` measure the total weight reallocation in each leg.
On 100% turnover of the long leg, `long_delta = 2.0` (full exit + full new entry),
giving `tc = 20 bps` — correctly accounting for both entry and exit.

**Caveat:** 10 bps is a point estimate appropriate for large-cap liquid names. Actual
market impact for concentrated positions in small-cap construction names may be
5–20× higher.

### 6.3 Performance Statistics

- **CAGR**: `(1 + r̄)^12 - 1` where `r̄` is geometric mean
  Implementation: `(1 + monthly).prod()^(12/T) - 1`
- **Annualized Sharpe**: `CAGR / (σ_monthly × √12)` using population std
- **Max drawdown**: `min_t[(cum_ret(t) / cummax_ret(t)) - 1]`
- **Hit rate**: fraction of months with positive return
- **Turnover**: `(long_delta + short_delta) / 2` (one-way fraction of NAV)

### 6.4 Factor Exposure

Fama-French 3-factor + Momentum (Carhart 4-factor) regression on monthly L/S returns:

```
ls_ret(t) = α + β_mkt × MKT-RF(t) + β_smb × SMB(t) + β_hml × HML(t)
          + β_mom × UMD(t) + ε(t)
```

FF factors downloaded via `pandas_datareader` (`famafrench` reader, "F-F_Research_Data_Factors").
OLS with HAC standard errors (maxlags=3). α is annualized as `α × 12`.

---

## 7. Reproducibility

All artifacts are saved with SHA-256 hashes:
- Model weights (`.pkl`) under `data/urbangrowth/artifacts/`
- Manifest JSON with hash, metadata, and timestamp

Run `make reproduce` to execute the full pipeline on the 2022-01 to 2022-06 sample.
Run `make test` to execute unit and integration tests.
Run `make coverage` to verify ≥70% code coverage on `src/urbangrowth/`.

Pin all package versions in `environment.yml`. The `[tool.pytest.ini_options]` section
in `pyproject.toml` sets `testpaths = ["tests"]` and `asyncio_mode = "auto"`.
