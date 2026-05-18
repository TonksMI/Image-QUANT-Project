"""Cross-sectional return model.

Fama-MacBeth two-pass regression (Phase 9):
  1. Each month: cross-sectional OLS of returns on signal scores
  2. Time-series: average monthly slope coefficients, compute Newey-West t-stats

Value-beyond-price-volume:
  - Augmented regression: fwd_return ~ momentum + volume + alt_signal
  - alt_signal t-stat (NW HAC) after controlling for price/volume
  - BH-FDR correction across all signal × horizon tests

Walk-forward cross-sectional model (Phase 10):
  - Wide panel: (ticker, period) × all features + sector dummies + momentum/volume controls
  - Model options: Ridge | ElasticNet | LightGBM
  - Expanding window, retrain monthly, predict next-month cross-sectional rank
  - Scores saved to processed/signals/model_scores_{model}_h{horizon}.parquet

CLI:
  ug model cross-section --horizon 1          # Fama-MacBeth
  ug model fit --model ridge --horizon 1      # walk-forward model
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import statsmodels.api as sm
import structlog

from urbangrowth.config import data_path, get_pipeline
from urbangrowth.modeling._data import (
    build_aligned_panel,
    build_forward_returns,
    build_momentum_signal,
    build_volume_signal,
    list_signal_sources,
    load_monthly_returns,
    load_signal_panel,
)
from urbangrowth.modeling.stats import fdr_correction, newey_west_tstat

log = structlog.get_logger(__name__)

_MIN_CROSS_SECTION = 20


# ---------------------------------------------------------------------------
# Fama-MacBeth
# ---------------------------------------------------------------------------


def fama_macbeth(
    signals: pd.DataFrame,
    returns: pd.DataFrame,
    signal_cols: list[str],
    horizon: int = 1,
    min_stocks: int = _MIN_CROSS_SECTION,
) -> pd.DataFrame:
    """Run Fama-MacBeth cross-sectional regression.

    Parameters
    ----------
    signals : wide panel with columns (period|date, symbol) + signal_cols
    returns : long DataFrame (symbol, date|period, horizon, fwd_return)
    signal_cols : regressors for the cross-sectional OLS each month
    horizon : forward return horizon
    min_stocks : skip months with fewer observations

    Returns
    -------
    DataFrame: one row per month, one column per signal_col + r_squared
    """
    # Normalise column names
    sig = signals.copy()
    if "period" in sig.columns:
        sig = sig.rename(columns={"period": "date"})

    ret = returns.copy()
    if "period" in ret.columns:
        ret = ret.rename(columns={"period": "date"})
    if "ticker" in ret.columns:
        ret = ret.rename(columns={"ticker": "symbol"})

    fwd = ret[ret["horizon"] == horizon][["date", "symbol", "fwd_return"]]
    merged = sig.merge(fwd, on=["date", "symbol"])

    monthly_slopes: list[dict] = []
    for date, grp in merged.groupby("date"):
        grp = grp.dropna(subset=signal_cols + ["fwd_return"])
        if len(grp) < min_stocks:
            continue
        y = grp["fwd_return"].values
        X = sm.add_constant(grp[signal_cols].values, has_constant="add")
        try:
            res = sm.OLS(y, X).fit()
        except Exception:
            continue
        row = {"date": date, "r_squared": float(res.rsquared)}
        for i, col in enumerate(signal_cols):
            row[col] = float(res.params[i + 1])  # skip intercept
        monthly_slopes.append(row)

    return pd.DataFrame(monthly_slopes)


def aggregate_fmb(monthly_slopes: pd.DataFrame) -> pd.DataFrame:
    """Aggregate Fama-MacBeth monthly slopes to mean, NW t-stat, p-value.

    Returns one row per signal column.
    """
    results = []
    for col in monthly_slopes.columns:
        if col in ("date", "r_squared"):
            continue
        series = monthly_slopes[col].dropna()
        if len(series) < 12:
            continue
        mean_ = float(series.mean())
        std_  = float(series.std())
        nw_t  = newey_west_tstat(series)
        import scipy.stats as sc
        p_val = float(sc.t.sf(abs(nw_t), df=len(series) - 1) * 2)
        results.append({
            "signal":      col,
            "mean_slope":  mean_,
            "std_slope":   std_,
            "nw_tstat":    nw_t,
            "p_value":     p_val,
            "n_months":    len(series),
        })
    return pd.DataFrame(results)


# ---------------------------------------------------------------------------
# Value beyond price/volume
# ---------------------------------------------------------------------------


def value_beyond_price_volume(
    signal_panel: pd.DataFrame,
    fwd_returns: pd.DataFrame,
    momentum: pd.DataFrame,
    volume: pd.DataFrame,
    horizons: tuple[int, ...] = (1, 2, 3),
) -> pd.DataFrame:
    """Test each alt signal for incremental predictive power vs momentum + volume.

    For each (feature_name, horizon):
        1. Build aligned panel: fwd_return, momentum_12m, volume_norm, feature
        2. Fama-MacBeth: fwd_return ~ momentum + volume + feature
        3. Extract feature slope t-stat (NW HAC)
    Then apply BH-FDR across all tests.

    Returns DataFrame: source, feature_name, horizon, mean_slope, nw_tstat,
                       p_value, p_adjusted, fdr_rejected, mean_slope_mom, mean_slope_vol
    """
    if "source" not in signal_panel.columns:
        signal_panel = signal_panel.copy()
        signal_panel["source"] = "unknown"

    rows = []
    for src, src_grp in signal_panel.groupby("source"):
        for feat in src_grp["feature_name"].unique():
            for h in horizons:
                panel = build_aligned_panel(src_grp, fwd_returns, momentum, volume, feat, h)
                if panel is None:
                    continue

                # Build wide signals panel
                sig_wide = panel[["period", "symbol", feat, "momentum_12m", "volume_norm"]].rename(
                    columns={"period": "date"}
                )
                ret_long = panel[["period", "symbol", "fwd_return"]].copy()
                ret_long["horizon"] = h
                ret_long = ret_long.rename(columns={"period": "date"})

                signal_cols = [c for c in ["momentum_12m", "volume_norm", feat]
                               if c in sig_wide.columns and sig_wide[c].notna().any()]
                if feat not in signal_cols:
                    continue

                slopes = fama_macbeth(sig_wide, ret_long, signal_cols, horizon=h)
                if slopes.empty or feat not in slopes.columns:
                    continue

                agg = aggregate_fmb(slopes)
                if agg.empty or "signal" not in agg.columns:
                    continue
                agg_dict = agg.set_index("signal").to_dict(orient="index")
                if feat not in agg_dict:
                    continue

                r = agg_dict[feat]
                rows.append({
                    "source":          src,
                    "feature_name":    feat,
                    "horizon":         h,
                    "mean_slope":      r["mean_slope"],
                    "nw_tstat":        r["nw_tstat"],
                    "p_value_raw":     r["p_value"],
                    "mean_slope_mom":  agg_dict.get("momentum_12m", {}).get("mean_slope", np.nan),
                    "mean_slope_vol":  agg_dict.get("volume_norm",  {}).get("mean_slope", np.nan),
                })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    p_series = pd.Series(df["p_value_raw"].values, index=df.index)
    fdr_df   = fdr_correction(p_series, alpha=0.05)
    df["p_adjusted"]  = fdr_df["p_adjusted"].values
    df["fdr_rejected"]= fdr_df["rejected"].values

    return df.sort_values(["source", "feature_name", "horizon"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Walk-forward cross-sectional model
# ---------------------------------------------------------------------------


def build_model_panel(
    signal_panel: pd.DataFrame,
    momentum: pd.DataFrame,
    volume: pd.DataFrame,
) -> pd.DataFrame:
    """Assemble wide (period, symbol) feature matrix for cross-sectional models.

    Pivots long signal_panel to wide, then merges momentum + volume controls and
    one-hot sector dummies.  Missing values stay as NaN; callers should impute
    (fill_value=0 is safe because signals are already z-scored, so 0 = mean).

    Returns DataFrame: period, symbol, <feat_1>, …, momentum_12m, volume_norm,
    sector_<s>, … (one-hot, drop_first=True).
    """
    wide = (
        signal_panel
        .pivot_table(
            index=["period", "symbol"],
            columns="feature_name",
            values="feature_value",
            aggfunc="first",
        )
        .reset_index()
    )
    wide.columns.name = None

    mom = (
        momentum[momentum["feature_name"] == "momentum_12m"]
        [["symbol", "period", "feature_value"]]
        .rename(columns={"feature_value": "momentum_12m"})
    )
    wide = wide.merge(mom, on=["symbol", "period"], how="left")

    vol = (
        volume[volume["feature_name"] == "volume_norm"]
        [["symbol", "period", "feature_value"]]
        .rename(columns={"feature_value": "volume_norm"})
    )
    wide = wide.merge(vol, on=["symbol", "period"], how="left")

    try:
        from urbangrowth.signals.ticker_mapping import _universe
        sector_map = {t["symbol"]: t.get("sector_tag", "unknown") for t in _universe()}
    except Exception:
        sector_map = {}
    wide["_sector"] = wide["symbol"].map(sector_map).fillna("unknown")
    dummies = pd.get_dummies(wide["_sector"], prefix="sector", drop_first=True, dtype=float)
    wide = pd.concat([wide.drop(columns="_sector"), dummies], axis=1)

    return wide


def walk_forward_predict(
    panel: pd.DataFrame,
    fwd_returns: pd.DataFrame,
    horizon: int = 1,
    model_type: str = "ridge",
    min_train_months: int = 24,
) -> pd.DataFrame:
    """Expanding-window walk-forward cross-sectional prediction.

    For each month t (after min_train_months of history):
      1. Train on all rows with period < t; target = fwd_return at horizon h
      2. Predict score for the period-t cross-section
    Missing features are imputed with 0 (= cross-sectional mean for z-scored signals).

    Returns DataFrame: period, symbol, score
    """
    import warnings as _warn
    _warn.filterwarnings("ignore")
    try:
        from sklearn.impute import SimpleImputer
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        log.error("sklearn_not_installed", error=str(exc))
        return pd.DataFrame()

    id_cols      = ["period", "symbol"]
    feature_cols = [c for c in panel.columns if c not in id_cols]

    fwd_h = (
        fwd_returns[fwd_returns["horizon"] == horizon][["symbol", "date", "fwd_return"]]
        .rename(columns={"date": "period"})
    )
    data = (
        panel.merge(fwd_h, on=["symbol", "period"], how="inner")
        .sort_values("period")
        .reset_index(drop=True)
    )

    periods = sorted(data["period"].unique())
    if len(periods) < min_train_months + 1:
        log.warning("walk_forward_too_few_periods",
                    n_periods=len(periods), required=min_train_months + 1)
        return pd.DataFrame()

    imputer    = SimpleImputer(strategy="constant", fill_value=0.0)
    all_scores: list[pd.DataFrame] = []

    def _clean(arr: np.ndarray) -> np.ndarray:
        out = arr.copy()
        out[~np.isfinite(out)] = 0.0
        return out

    # Embargo: exclude the last `horizon` months from training at each step
    # to prevent overlapping forward-return targets leaking future information.
    # For h=1 this is unchanged; for h=2+ it progressively excludes recent obs.
    for t in periods[min_train_months:]:
        embargo_cutoff = t - pd.DateOffset(months=horizon)
        train = data[data["period"] <= embargo_cutoff].copy()
        test  = data[data["period"] == t].copy().reset_index(drop=True)
        if len(train) < 30 or len(test) < 3:
            continue

        X_train = train[feature_cols].values.astype(float)
        y_train = train["fwd_return"].values.astype(float)
        X_test  = test[feature_cols].values.astype(float)

        finite  = np.isfinite(y_train)
        X_train, y_train = X_train[finite], y_train[finite]
        if len(X_train) < 10:
            continue

        X_tr = imputer.fit_transform(X_train)
        X_te = imputer.transform(X_test)

        try:
            if model_type == "ridge":
                from sklearn.linear_model import Ridge
                sc  = StandardScaler()
                mdl = Ridge(alpha=1.0)
                mdl.fit(_clean(sc.fit_transform(X_tr)), y_train)
                y_hat = mdl.predict(_clean(sc.transform(X_te)))

            elif model_type == "elasticnet":
                from sklearn.linear_model import SGDRegressor
                sc  = StandardScaler()
                # SGDRegressor with elasticnet penalty avoids coordinate descent BLAS crash on Windows
                mdl = SGDRegressor(
                    loss="squared_error", penalty="elasticnet",
                    alpha=0.01, l1_ratio=0.5, max_iter=1000,
                    tol=1e-3, random_state=42,
                )
                mdl.fit(_clean(sc.fit_transform(X_tr)), y_train)
                y_hat = mdl.predict(_clean(sc.transform(X_te)))

            elif model_type == "lgbm":
                try:
                    import lightgbm as lgb
                except ImportError as exc:
                    log.error("lightgbm_not_installed", error=str(exc))
                    return pd.DataFrame()
                mdl = lgb.LGBMRegressor(
                    n_estimators=100, num_leaves=15, learning_rate=0.05,
                    subsample=0.8, colsample_bytree=0.8, random_state=42,
                    verbose=-1,
                )
                mdl.fit(X_tr, y_train)
                y_hat = mdl.predict(X_te)

            else:
                raise ValueError(f"Unknown model_type: {model_type!r}")

        except Exception as exc:
            log.warning("walk_forward_fit_failed", period=str(t), error=str(exc))
            continue

        scored = test[["period", "symbol"]].copy()
        scored["score"] = y_hat.astype(float)
        all_scores.append(scored)

    return pd.concat(all_scores, ignore_index=True) if all_scores else pd.DataFrame()


def run_cross_sectional_model(
    model_type: str = "ridge",
    horizon: int = 1,
    min_train_months: int = 24,
) -> pd.DataFrame:
    """Build feature panel, run walk-forward prediction, save scores parquet.

    Output: processed/signals/model_scores_{model_type}_h{horizon}.parquet
    """
    returns = load_monthly_returns()
    if returns.empty:
        log.warning("model_no_returns")
        return pd.DataFrame()

    fwd_returns = build_forward_returns(returns, [horizon])
    momentum    = build_momentum_signal(returns)
    volume      = build_volume_signal()

    sources = list_signal_sources()
    if not sources:
        log.warning("model_no_sources")
        return pd.DataFrame()

    all_signals = pd.concat(
        [load_signal_panel(src) for src in sources], ignore_index=True
    )
    if all_signals.empty:
        log.warning("model_no_signals")
        return pd.DataFrame()

    panel = build_model_panel(all_signals, momentum, volume)
    log.info("model_panel_built", rows=len(panel), n_features=len(panel.columns) - 2)

    scores = walk_forward_predict(
        panel, fwd_returns,
        horizon=horizon, model_type=model_type, min_train_months=min_train_months,
    )
    if scores.empty:
        log.warning("model_no_scores", model=model_type, horizon=horizon)
        return pd.DataFrame()

    out_dir  = data_path(get_pipeline()["processed_data_subdirs"].get("signal_tables", "processed/signals"))
    out_path = out_dir / f"model_scores_{model_type}_h{horizon}.parquet"
    scores.to_parquet(out_path, index=False)
    log.info("model_scores_saved", model=model_type, horizon=horizon,
             n=len(scores), path=str(out_path))
    return scores


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run(horizon: int = 1) -> None:
    """Run Fama-MacBeth + value-beyond-PV test for all signal sources."""
    returns    = load_monthly_returns()
    if returns.empty:
        log.warning("cross_section_no_returns")
        return

    fwd_returns = build_forward_returns(returns, [1, 2, 3])
    momentum    = build_momentum_signal(returns)
    volume      = build_volume_signal()

    sources = list_signal_sources()
    if not sources:
        log.warning("cross_section_no_sources")
        return

    all_signals = pd.concat(
        [load_signal_panel(src) for src in sources],
        ignore_index=True,
    )
    if all_signals.empty:
        log.warning("cross_section_no_signals")
        return

    df = value_beyond_price_volume(
        all_signals, fwd_returns, momentum, volume, horizons=(horizon,)
    )
    if df.empty:
        log.warning("cross_section_empty_result")
        return

    out_dir  = data_path(get_pipeline()["processed_data_subdirs"].get("signal_tables", "processed/signals"))
    out_path = out_dir / f"cross_section_h{horizon}.parquet"
    df.to_parquet(out_path, index=False)
    log.info("cross_section_done", rows=len(df), path=str(out_path))

    sig_signals = df[df["fdr_rejected"]].sort_values("nw_tstat", ascending=False)
    log.info("cross_section_significant",
             n_significant=len(sig_signals),
             results=sig_signals[["source", "feature_name", "nw_tstat", "p_adjusted"]].head(10).to_dict("records"))
