"""Ticker → signal weight mappings derived from universe.yaml.

All signal generation modules call this to determine which tickers to score
and at what geographic weight.  No DB access — pure YAML config.

Key functions
-------------
tickers_for_signal(signal_name)   — symbols whose primary_signals include the key
geographic_weights(symbol)        — {state_code: normalized_weight}
sector_for_symbol(symbol)         — sector_tag string
tickers_with_city(city)           — symbols with city_growth_{city} signal
city_primary_state(city)          — 'AZ' for phoenix, 'TX' for austin
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from urbangrowth.config import get_universe

_NON_STATE_KEYS = frozenset({"nationwide", "worldwide", "diversified"})

# Sector sensitivity to national permit level — creates cross-sectional
# variance from a common national time series (higher = more permit-driven)
PERMIT_SENSITIVITY: dict[str, float] = {
    "homebuilder":       1.00,
    "reit_residential":  0.60,
    "materials":         0.70,
    "equipment":         0.45,
    "regional_bank":     0.45,
    "steel":             0.40,
    "reit_industrial":   0.30,
    "civil_construction":0.30,
    "electrical_infra":  0.20,
    "control":           0.00,
}

# FERC queue ISO/RTO region → primary US states (for geographic weighting)
ISO_TO_STATES: dict[str, list[str]] = {
    "ERCOT":  ["TX"],
    "CAISO":  ["CA"],
    "PJM":    ["PA", "OH", "IL", "IN", "MI", "MD", "VA", "WV", "NJ", "DE"],
    "MISO":   ["MN", "WI", "IA", "IL", "IN", "MI", "MO", "ND", "SD"],
    "SPP":    ["KS", "OK", "NE", "SD", "TX"],
    "NYISO":  ["NY"],
    "NEISO":  ["CT", "ME", "MA", "NH", "RI", "VT"],
    "SERC":   ["TN", "KY", "GA", "SC", "NC", "MS", "AL"],
    "WECC":   ["WA", "OR", "ID", "MT", "WY", "UT", "NV", "AZ", "CO", "NM"],
}

# Sector → NAICS code prefixes for USASpending sector-level fallback
SECTOR_NAICS: dict[str, list[str]] = {
    "electrical_infra":   ["2383", "2370"],
    "civil_construction": ["2371", "2372", "2379", "2369"],
    "materials":          ["3312", "3313", "3316", "3271", "3272", "3273"],
    "equipment":          ["3331", "3332", "3339"],
    "steel":              ["3312", "3313", "3314", "3315"],
    "homebuilder":        ["2361"],
    "reit_industrial":    ["2362", "2369"],
    "reit_residential":   ["2361"],
    "regional_bank":      [],
    "control":            [],
}

# FRED series → sectors for which the series is relevant
FRED_SECTOR_SERIES: dict[str, list[str]] = {
    "TLNRESCONS":  ["materials", "equipment", "steel", "civil_construction",
                    "reit_industrial", "electrical_infra"],
    "PNRESCONS":   ["materials", "equipment", "steel", "civil_construction",
                    "reit_industrial", "reit_residential"],
    "MNFCTRCONS":  ["materials", "equipment", "steel"],
    "PWRCONS":     ["electrical_infra"],
    "HOUST":       ["homebuilder", "reit_residential", "regional_bank",
                    "materials", "equipment"],
    "MORTGAGE30US":["homebuilder", "reit_residential", "regional_bank"],
    "T10Y2Y":      ["homebuilder", "reit_residential", "regional_bank",
                    "materials", "civil_construction", "electrical_infra"],
}


@lru_cache(maxsize=None)
def _universe() -> list[dict]:
    return get_universe()


def all_symbols(include_controls: bool = False) -> list[str]:
    return [
        t["symbol"] for t in _universe()
        if include_controls or t.get("sector_tag") != "control"
    ]


def tickers_for_signal(signal_name: str) -> list[str]:
    """Symbols whose primary_signals list includes signal_name."""
    return [
        t["symbol"] for t in _universe()
        if signal_name in t.get("primary_signals", [])
    ]


def sector_tickers(sector_tag: str) -> list[str]:
    return [t["symbol"] for t in _universe() if t.get("sector_tag") == sector_tag]


def sector_for_symbol(symbol: str) -> Optional[str]:
    for t in _universe():
        if t["symbol"] == symbol:
            return t.get("sector_tag")
    return None


def geographic_weights(symbol: str) -> dict[str, float]:
    """Return {state_code: normalized_weight} from geographic_concentration.

    Non-geographic keys (nationwide, worldwide, diversified) are dropped.
    Weights are normalized so the state weights sum to 1.0.
    Returns {} for tickers with no state-level concentration data.
    """
    for t in _universe():
        if t["symbol"] == symbol:
            conc = t.get("geographic_concentration", {})
            states = {k: float(v) for k, v in conc.items() if k not in _NON_STATE_KEYS}
            if not states:
                return {}
            total = sum(states.values())
            return {k: v / total for k, v in states.items()}
    return {}


def tickers_with_city(city: str) -> list[str]:
    return tickers_for_signal(f"city_growth_{city}")


def city_primary_state(city: str) -> Optional[str]:
    return {"phoenix": "AZ", "austin": "TX"}.get(city)


def iso_state_weights(iso: str) -> dict[str, float]:
    """Return equal-weight {state: weight} for an ISO/RTO region."""
    states = ISO_TO_STATES.get(iso.upper(), [])
    if not states:
        return {}
    w = 1.0 / len(states)
    return {s: w for s in states}
