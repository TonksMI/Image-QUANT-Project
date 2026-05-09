"""H3 cell investment classifier.

Trains a GradientBoostingClassifier to predict which H3 cells will see
>= 5 percentage-point growth in built_pct over the next 12 months.

Outputs a probability map (investment_score) per H3 cell saved to the
h3_predictions table and to a parquet file for visualization.

Usage:
    python -m urbangrowth.modeling.h3_classifier
    python -m urbangrowth.modeling.h3_classifier --horizon 12 --threshold 0.05
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import h3
import numpy as np
import pandas as pd
import sqlalchemy as sa
import structlog
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import classification_report, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from urbangrowth.config import data_path
from urbangrowth.db.loaders import _engine

load_dotenv()
log = structlog.get_logger(__name__)

# Known city centers (lat, lng)
_CITY_CENTERS: dict[int, tuple[float, float]] = {
    1: (33.4484, -112.0740),  # phoenix
    2: (30.2672, -97.7431),   # austin
}

_TRANSITION_KEYS = ["veg_to_built", "bare_to_built", "built_stable", "new_built", "built_loss"]
_FEATURE_COLS = ["built_pct", "veg_pct"] + _TRANSITION_KEYS + [
    "permit_count", "permit_valuation_norm", "dist_to_center_km",
]

MODEL_VERSION = "gbm_v1"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


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

    df["dist_to_center_km"] = [
        _haversine(ll, c) for ll, c in zip(latlngs, centers)
    ]
    return df


# ---------------------------------------------------------------------------
# Training set construction
# ---------------------------------------------------------------------------


def build_training_set(
    df: pd.DataFrame,
    horizon_months: int = 12,
    threshold: float = 0.05,
) -> pd.DataFrame:
    """Join each cell's features at time t with its built_pct at t + horizon."""
    df = df.copy()
    df["date_future"] = df["date"].apply(lambda d: d + relativedelta(months=horizon_months))

    future = df[["h3_index", "city_id", "date", "built_pct"]].rename(
        columns={"date": "date_future", "built_pct": "built_pct_future"}
    )
    merged = df.merge(future, on=["h3_index", "city_id", "date_future"], how="inner")
    merged["target"] = ((merged["built_pct_future"] - merged["built_pct"]) >= threshold).astype(int)

    log.info(
        "training_set_built",
        rows=len(merged),
        positive_rate=round(merged["target"].mean(), 4),
        horizon=horizon_months,
        threshold=threshold,
    )
    return merged


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    df = _expand_transitions(df)
    df = _add_distance(df)
    # Normalise permit_valuation by cells that have any permits to avoid scale dominance
    max_val = df["permit_valuation"].replace(0, np.nan).quantile(0.99)
    df["permit_valuation_norm"] = df["permit_valuation"] / (max_val if max_val else 1.0)
    df["permit_valuation_norm"] = df["permit_valuation_norm"].clip(0, 1).fillna(0)
    df["permit_count"] = df["permit_count"].fillna(0)
    return df


# ---------------------------------------------------------------------------
# Model training
# ---------------------------------------------------------------------------


def train(
    df: pd.DataFrame,
    horizon_months: int = 12,
    threshold: float = 0.05,
    test_size: float = 0.2,
    random_state: int = 42,
) -> tuple[Pipeline, pd.DataFrame]:
    df = engineer_features(df)
    training = build_training_set(df, horizon_months=horizon_months, threshold=threshold)

    X = training[_FEATURE_COLS].fillna(0)
    y = training["target"]

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=test_size, random_state=random_state, stratify=y
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
    log.info("training_model", train_rows=len(X_train), test_rows=len(X_test))
    pipe.fit(X_train, y_train)

    y_prob = pipe.predict_proba(X_test)[:, 1]
    auc = roc_auc_score(y_test, y_prob)
    report = classification_report(y_test, pipe.predict(X_test), output_dict=True)
    log.info(
        "model_trained",
        auc=round(auc, 4),
        f1_pos=round(report.get("1", {}).get("f1-score", 0), 4),
        accuracy=round(report.get("accuracy", 0), 4),
    )

    # Feature importance
    imp = pd.Series(
        pipe.named_steps["clf"].feature_importances_,
        index=_FEATURE_COLS,
    ).sort_values(ascending=False)
    log.info("feature_importance", importances=imp.to_dict())

    return pipe, training


# ---------------------------------------------------------------------------
# Inference & persistence
# ---------------------------------------------------------------------------


def predict_latest(pipe: Pipeline, df: pd.DataFrame) -> pd.DataFrame:
    """Score all cells at the most recent date per city."""
    df = engineer_features(df)
    latest = (
        df.groupby("city_id")["date"].max().reset_index().rename(columns={"date": "max_date"})
    )
    current = df.merge(latest, on="city_id").query("date == max_date")
    X = current[_FEATURE_COLS].fillna(0)
    current = current.copy()
    current["investment_score"] = pipe.predict_proba(X)[:, 1]
    log.info(
        "predictions_made",
        rows=len(current),
        mean_score=round(current["investment_score"].mean(), 4),
        top10_threshold=round(current["investment_score"].quantile(0.9), 4),
    )
    return current


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

    out_path = data_path("processed/signals/h3_pred.parquet")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows.to_parquet(out_path, index=False)
    log.info("predictions_saved_parquet", path=str(out_path))
    return out_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run(horizon_months: int = 12, threshold: float = 0.05) -> None:
    df = _load_h3_features()
    pipe, _ = train(df, horizon_months=horizon_months, threshold=threshold)
    preds = predict_latest(pipe, df)
    save_predictions(preds)


def main() -> None:
    parser = argparse.ArgumentParser(description="H3 cell investment classifier")
    parser.add_argument("--horizon", type=int, default=12, help="Forward horizon in months")
    parser.add_argument("--threshold", type=float, default=0.05, help="Min built_pct increase to be positive")
    args = parser.parse_args()
    run(horizon_months=args.horizon, threshold=args.threshold)


if __name__ == "__main__":
    main()
