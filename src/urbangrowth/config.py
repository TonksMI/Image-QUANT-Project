"""Config loader — reads YAML files from config/ and returns dicts.

Usage:
    from urbangrowth.config import get_universe, get_cities, get_pipeline
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

_CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


@lru_cache(maxsize=None)
def _load(name: str) -> dict:
    path = _CONFIG_DIR / f"{name}.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_universe() -> list[dict]:
    return _load("universe")["universe"]


def get_cities() -> dict:
    return _load("cities")["cities"]


def get_data_sources() -> dict:
    return _load("data_sources")


def get_pipeline() -> dict:
    cfg = _load("pipeline")
    # Allow env var override of data_root
    override = os.environ.get("URBANGROWTH_DATA_ROOT")
    if override:
        cfg = {**cfg, "data_root": override}
    return cfg


def data_path(*parts: str) -> Path:
    """Return an absolute Path under data_root, creating it if needed."""
    root = Path(get_pipeline()["data_root"])
    p = root.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p
