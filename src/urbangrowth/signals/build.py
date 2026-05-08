"""Signal build orchestrator — runs all six signal families in sequence.

Called by:  ug signals build --start 2015-01 --end 2025-12
"""
from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

_FAMILIES = [
    ("permit",      "urbangrowth.signals.permit_signals"),
    ("ferc",        "urbangrowth.signals.ferc_signals"),
    ("usaspending", "urbangrowth.signals.usaspending_signals"),
    ("fred",        "urbangrowth.signals.fred_signals"),
    ("dot_tips",    "urbangrowth.signals.dot_tips_signals"),
    ("city_growth", "urbangrowth.signals.city_growth_signals"),
]


def run(start: str = "2015-01", end: str = "2025-12") -> None:
    """Build all signal families and write to signal_features table."""
    import importlib

    log.info("signal_build_all_start", start=start, end=end, families=len(_FAMILIES))
    failed: list[str] = []

    for name, module_path in _FAMILIES:
        log.info("signal_build_family_start", family=name)
        try:
            mod = importlib.import_module(module_path)
            mod.run(start=start, end=end)
            log.info("signal_build_family_done", family=name)
        except Exception as exc:
            log.error("signal_build_family_failed", family=name, error=str(exc))
            failed.append(name)

    if failed:
        log.warning("signal_build_complete_with_failures", failed=failed)
    else:
        log.info("signal_build_all_done", start=start, end=end)
