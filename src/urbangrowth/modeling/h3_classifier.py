"""H3 cell investment classifier.

Trains a GradientBoostingClassifier to predict which H3 cells will see
above-median built_pct growth relative to their city peers over the next
12 months.

Fixes vs. original:
  - Relative target: top-N% of changers per city/date (handles Phoenix
    where absolute built_pct values are near-zero due to satellite
    classification issues — an absolute 5pp threshold is never reachable).
  - Temporal train/test split: train on signal_date < cutoff, test on
    signal_date >= cutoff. Prevents look-ahead bias from spatial
    autocorrelation between neighboring cells.
  - predict_latest uses the latest date with non-null built_pct, not the
    absolute latest date (which often has NULL satellite data).

Usage:
    python -m urbangrowth.modeling.h3_classifier
    python -m urbangrowth.modeling.h3_classifier --horizon 12 --top-pct 0.25
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import h3
import numpy as np
import pandas as pd
import sqlalchemy as sa
import structlog
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import classification_report, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from urbangrowth.config import data_path
from urbangrowth.db.loaders import _engine

load_dotenv()
log = structlog.get_logger(__name__)

_CITY_CENTERS: dict[int, tuple[float, float]] = {
    1: (33.4484, -112.0740),  # phoenix
    2: (30.2672, -97.7431),   # austin
}

_TRANSITION_KEYS = ["veg_to_built", "bare_to_built", "built_stable", "new_built", "built_loss"]
_FEATURE_COLS = ["built_pct", "veg_pct"] + _TRANSITION_KEYS + [
    "permit_count", "permit_valuation_norm", "dist_to_center_km",
]

# Train on signal_dates before this cutoff; test on >= cutoff.
# Ensures no future spatial neighbors leak into training.
_TRAIN_CUTOFF = pd.Timestamp("2022-01-01")

MODEL_VERSION = "gbm_v1"


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_h3_features() -> pd.DataFrame:
    engine = _engine()
    log.info("loading_h3_features")
    with engine.connect() as conn:
        df = pd.read_sql(
            sa.text("""
                SELECT h3_index, city_id, date,
                       built_pct, veg_pct, transitions_jsonb,
                       permit_count, permit_valuation
                FROM h3_features
                ORDER BY city_id, date, h3_index
            """),
            conn,
        )
    df["date"] = pd.to_datetime(df["date"])
    log.info("h3_features_loaded", rows=len(df))
    return df


def _expand_transitions(df: pd.DataFrame) -> pd.DataFrame:
    def _parse(v) -> dict:
        if v is None:
            return {k: 0.0 for k in _TRANSITION_KEYS}
        if isinstance(v, str):
            v = json.loads(v)
        return {k: float(v.get(k, 0.0)) for k in _TRANSITION_KEYS}

    parsed = df["transitions_jsonb"].apply(_parse).apply(pd.Series)
    return pd.concat([df.drop(columns=["transitions_jsonb"]), parsed], axis=1)


def _add_distance(df: pd.DataFrame) -> pd.DataFrame:
    centers = df["city_id"].map(_CITY_CENTERS)
    latlngs = df["h3_index"].map(lambda idx: h3.cell_to_latlng(idx))

    def _haversine(cell_ll, center_ll) -> float:
        if cell_ll is None or center_ll is None:
            return np.nan
        lat1, lon1 = np.radians(cell_ll[0]), np.radians(cell_ll[1])
        lat2, lon2 = np.radians(center_ll[0]), np.radians(center_ll[1])
        dlat, dlon = lat2 - lat1, lon2 - lon1
        a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
        return 6371.0 * 2 * np.arcsin(np.sqrt(a))

    df = df.copy()
    df["dist_to_center_km"] = [
        _haversine(ll, c) for ll, c in zip(latlngs, centers)
    ]
    return df


# ── Training set construction ─────────────────────────────────────────────────

def build_training_set(
    df: pd.DataFrame,
    horizon_months: int = 12,
    top_pct: float = 0.25,
) -> pd.DataFrame:
    """Pair each cell's features at t with its built_pct change at t+horizon.

    Target: whether this cell's built_pct change is in the top `top_pct`
    fraction of all changers for the same city and signal date.  This is
    scale-invariant — it works even when absolute built_pct values are tiny
    (e.g. Phoenix where satellite classification gives near-zero values).
    """
    df = df.copy()
    df["date_future"] = df["date"].apply(
        lambda d: d + relativedelta(months=horizon_months)
    )

    future = df[["h3_index", "city_id", "date", "built_pct"]].rename(
        columns={"date": "date_future", "built_pct": "built_pct_future"}
    )
    merged = df.merge(future, on=["h3_index", "city_id", "date_future"], how="inner")
    merged["built_pct_change"] = merged["built_pct_future"] - merged["built_pct"]

    # Relative target: top top_pct of changers within the same city+date bucket
    def _label(group: pd.DataFrame) -> pd.Series:
        thresh = group["built_pct_change"].quantile(1.0 - top_pct)
        return (group["built_pct_change"] >= thresh).astype(int)

    merged["target"] = merged.groupby(
        ["city_id", "date"], group_keys=False
    ).apply(_label)

    log.info(
        "training_set_built",
        rows=len(merged),
        positive_rate=round(float(merged["target"].mean()), 4),
        horizon=horizon_months,
        top_pct=top_pct,
    )
    return merged


# ── Feature engineering ───────────────────────────────────────────────────────

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    df = _expand_transitions(df)
    df = _add_distance(df)
    max_val = df["permit_valuation"].replace(0, np.nan).quantile(0.99)
    df["permit_valuation_norm"] = df["permit_valuation"] / (max_val if max_val else 1.0)
    df["permit_valuation_norm"] = df["permit_valuation_norm"].clip(0, 1).fillna(0)
    df["permit_count"] = df["permit_count"].fillna(0)
    return df


# ── Model training ────────────────────────────────────────────────────────────

def train(
    df: pd.DataFrame,
    horizon_months: int = 12,
    top_pct: float = 0.25,
    random_state: int = 42,
) -> tuple[Pipeline, pd.DataFrame]:
    df = engineer_features(df)
    training = build_training_set(df, horizon_months=horizon_months, top_pct=top_pct)

    # Temporal split: train on earlier signal dates, test on later ones.
    # This prevents look-ahead from spatially correlated neighboring cells.
    train_mask = training["date"] < _TRAIN_CUTOFF
    test_mask  = training["date"] >= _TRAIN_CUTOFF

    if train_mask.sum() < 100 or test_mask.sum() < 20:
        log.warning(
            "small_split",
            train=int(train_mask.sum()),
            test=int(test_mask.sum()),
            cutoff=str(_TRAIN_CUTOFF.date()),
        )

    X_train = training.loc[train_mask, _FEATURE_COLS].fillna(0)
    y_train = training.loc[train_mask, "target"]
    X_test  = training.loc[test_mask,  _FEATURE_COLS].fillna(0)
    y_test  = training.loc[test_mask,  "target"]

    log.info(
        "temporal_split",
        train_rows=len(X_train),
        test_rows=len(X_test),
        cutoff=str(_TRAIN_CUTOFF.date()),
        train_pos_rate=round(float(y_train.mean()), 4),
        test_pos_rate=round(float(y_test.mean()), 4),
    )

    pipe = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", GradientBoostingClassifier(
            n_estimators=200,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            random_state=random_state,
        )),
    ])
    pipe.fit(X_train, y_train)

    if len(X_test) >= 10:
        y_prob = pipe.predict_proba(X_test)[:, 1]
        auc = roc_auc_score(y_test, y_prob)
        report = classification_report(y_test, pipe.predict(X_test), output_dict=True)
        log.info(
            "model_trained",
            auc=round(auc, 4),
            f1_pos=round(report.get("1", {}).get("f1-score", 0), 4),
            accuracy=round(report.get("accuracy", 0), 4),
        )

    imp = pd.Series(
        pipe.named_steps["clf"].feature_importances_,
        index=_FEATURE_COLS,
    ).sort_values(ascending=False)
    log.info("feature_importance", importances=imp.round(4).to_dict())

    return pipe, training


# ── Inference ─────────────────────────────────────────────────────────────────

def predict_latest(pipe: Pipeline, df: pd.DataFrame) -> pd.DataFrame:
    """Score all cells at the most recent date with non-null built_pct per city.

    The absolute latest date in h3_features often has NULL satellite data
    (the pipeline ingests a placeholder row before imagery is processed).
    Using that date would feed all-zero built_pct into the GBM, causing a
    distribution shift vs. the training data.
    """
    df = engineer_features(df)

    # Find the latest date per city where built_pct is actually populated
    has_data = df[df["built_pct"].notna() & (df["built_pct"] > 0)]
    latest = (
        has_data.groupby("city_id")["date"]
        .max()
        .reset_index()
        .rename(columns={"date": "max_date"})
    )
    if latest.empty:
        log.warning("no_non_null_built_pct_for_inference")
        return pd.DataFrame()

    current = df.merge(latest, on="city_id").query("date == max_date").copy()
    log.info(
        "inference_dates",
        dates=current.groupby("city_id")["date"].first().to_dict(),
    )

    X = current[_FEATURE_COLS].fillna(0)
    current["investment_score"] = pipe.predict_proba(X)[:, 1]
    log.info(
        "predictions_made",
        rows=len(current),
        mean_score=round(float(current["investment_score"].mean()), 4),
        top10_threshold=round(float(current["investment_score"].quantile(0.9)), 4),
    )
    return current


# ── Persistence ───────────────────────────────────────────────────────────────

def _ensure_predictions_table(engine: sa.Engine) -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS h3_predictions (
        h3_index        TEXT        NOT NULL,
        city_id         INTEGER     NOT NULL,
        as_of_date      DATE        NOT NULL,
        investment_score FLOAT      NOT NULL,
        model_version   TEXT        NOT NULL DEFAULT 'gbm_v1',
        created_at      TIMESTAMP   NOT NULL DEFAULT NOW(),
        PRIMARY KEY (h3_index, city_id, as_of_date, model_version)
    );
    """
    with engine.begin() as conn:
        conn.execute(sa.text(ddl))


def save_predictions(preds: pd.DataFrame) -> Path:
    engine = _engine()
    _ensure_predictions_table(engine)

    rows = preds[["h3_index", "city_id", "date", "investment_score"]].copy()
    rows = rows.rename(columns={"date": "as_of_date"})
    rows["model_version"] = MODEL_VERSION

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    with engine.begin() as conn:
        for _, r in rows.iterrows():
            stmt = pg_insert(sa.table(
                "h3_predictions",
                sa.column("h3_index"),
                sa.column("city_id"),
                sa.column("as_of_date"),
                sa.column("investment_score"),
                sa.column("model_version"),
            )).values(
                h3_index=r["h3_index"],
                city_id=int(r["city_id"]),
                as_of_date=r["as_of_date"],
                investment_score=float(r["investment_score"]),
                model_version=MODEL_VERSION,
            ).on_conflict_do_update(
                index_elements=["h3_index", "city_id", "as_of_date", "model_version"],
                set_={"investment_score": float(r["investment_score"])},
            )
            conn.execute(stmt)

    log.info("predictions_saved_db", rows=len(rows))

    out_path = data_path("processed/signals") / "h3_pred.parquet"
    rows.to_parquet(out_path, index=False)
    log.info("predictions_saved_parquet", path=str(out_path))
    return out_path


# ── Entry point ───────────────────────────────────────────────────────────────

def run(horizon_months: int = 12, top_pct: float = 0.25) -> None:
    df = _load_h3_features()
    pipe, _ = train(df, horizon_months=horizon_months, top_pct=top_pct)
    preds = predict_latest(pipe, df)
    if not preds.empty:
        save_predictions(preds)


def main() -> None:
    parser = argparse.ArgumentParser(description="H3 cell investment classifier")
    parser.add_argument("--horizon", type=int, default=12,
                        help="Forward horizon in months (default: 12)")
    parser.add_argument("--top-pct", type=float, default=0.25,
                        help="Top fraction of changers to label positive (default: 0.25)")
    args = parser.parse_args()
    run(horizon_months=args.horizon, top_pct=args.top_pct)


if __name__ == "__main__":
    main()
