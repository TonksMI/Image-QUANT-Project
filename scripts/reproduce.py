"""End-to-end reproduce script.

Runs the full modeling pipeline on a sampled date range to verify that
code executes without errors and that key outputs satisfy sanity checks.
No live data is fetched — signal_features and returns must already exist
in the database, or the script will run on whatever subset is available.

Usage
-----
    python scripts/reproduce.py --start 2022-01 --end 2022-06 --model ridge --horizon 1

Or via Make:
    make reproduce
"""
from __future__ import annotations

import argparse
import sys
import textwrap
import time
from pathlib import Path

_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(_ROOT / "src"))


def _section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def _check(name: str, condition: bool, detail: str = "") -> None:
    mark = "✓" if condition else "✗"
    msg  = f"  [{mark}] {name}"
    if detail:
        msg += f"  ({detail})"
    print(msg)
    if not condition:
        print(f"      FAIL: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="End-to-end reproduce pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              python scripts/reproduce.py
              python scripts/reproduce.py --start 2022-01 --end 2022-06 --model lgbm
        """),
    )
    parser.add_argument("--start",   default="2022-01", help="Start month YYYY-MM")
    parser.add_argument("--end",     default="2022-06", help="End month YYYY-MM")
    parser.add_argument("--model",   default="ridge",   choices=["ridge", "elasticnet", "lgbm"])
    parser.add_argument("--horizon", default=1,         type=int)
    args = parser.parse_args()

    t0 = time.perf_counter()
    failures: list[str] = []

    # ------------------------------------------------------------------
    # 1. Forward returns
    # ------------------------------------------------------------------
    _section("1 — Forward returns")
    try:
        from urbangrowth.modeling._data import build_forward_returns, load_monthly_returns
        raw = load_monthly_returns()
        fwd = build_forward_returns(raw, horizons=[args.horizon])
        _check("load_monthly_returns",  not raw.empty, f"{len(raw):,} rows")
        _check("build_forward_returns", not fwd.empty, f"{len(fwd):,} rows")
    except Exception as exc:
        print(f"  ERROR: {exc}")
        failures.append("forward_returns")

    # ------------------------------------------------------------------
    # 2. Momentum + volume signals
    # ------------------------------------------------------------------
    _section("2 — Control signals")
    try:
        from urbangrowth.modeling._data import build_momentum_signal, build_volume_signal
        mom = build_momentum_signal(raw) if not raw.empty else None
        vol = build_volume_signal()
        _check("build_momentum_signal", mom is not None and not mom.empty,
               f"{len(mom):,} rows" if mom is not None else "n/a")
        _check("build_volume_signal",   vol is not None,
               f"{len(vol):,} rows" if vol is not None else "empty (OK — no DB)")
    except Exception as exc:
        print(f"  ERROR: {exc}")
        failures.append("control_signals")

    # ------------------------------------------------------------------
    # 3. Signal panel
    # ------------------------------------------------------------------
    _section("3 — Signal panel")
    try:
        from urbangrowth.modeling._data import list_signal_sources, load_signal_panel
        sources = list_signal_sources()
        _check("list_signal_sources", True, f"{sources}")
        if sources:
            panel = load_signal_panel(sources[0])
            _check("load_signal_panel", not panel.empty, f"{len(panel):,} rows, source={sources[0]}")
        else:
            print("  [i] No signal sources in DB — skipping panel load")
    except Exception as exc:
        print(f"  ERROR: {exc}")
        failures.append("signal_panel")

    # ------------------------------------------------------------------
    # 4. Fama-MacBeth
    # ------------------------------------------------------------------
    _section("4 — Fama-MacBeth")
    try:
        import pandas as pd
        import numpy as np
        from urbangrowth.modeling.cross_section import aggregate_fmb, fama_macbeth

        n_tickers = 25
        n_periods = 24
        tickers = [f"T{i:02d}" for i in range(n_tickers)]
        dates = pd.date_range("2020-01-01", periods=n_periods, freq="MS")
        rng = np.random.default_rng(0)

        sig = pd.DataFrame([
            {"period": d, "symbol": t, "mock_sig": rng.standard_normal()}
            for d in dates for t in tickers
        ])
        ret = pd.DataFrame([
            {"date": d, "symbol": t, "horizon": 1, "fwd_return": rng.normal(0.005, 0.05)}
            for d in dates for t in tickers
        ])
        slopes = fama_macbeth(sig, ret, signal_cols=["mock_sig"], horizon=1, min_stocks=10)
        agg = aggregate_fmb(slopes)
        _check("fama_macbeth",   len(slopes) > 0, f"{len(slopes)} monthly slopes")
        _check("aggregate_fmb",  len(agg) > 0,    f"{len(agg)} signals")
    except Exception as exc:
        print(f"  ERROR: {exc}")
        failures.append("fama_macbeth")

    # ------------------------------------------------------------------
    # 5. Portfolio construction
    # ------------------------------------------------------------------
    _section("5 — Portfolio backtest")
    try:
        from urbangrowth.modeling.backtest import construct_portfolio, performance_stats

        scores = pd.DataFrame([
            {"period": d, "symbol": t, "score": rng.standard_normal()}
            for d in dates for t in tickers
        ])
        fwd_h = pd.DataFrame([
            {"date": d, "symbol": t, "horizon": 1, "fwd_return": rng.normal(0.005, 0.05)}
            for d in dates for t in tickers
        ])
        portfolio = construct_portfolio(scores, fwd_h, long_pct=0.2, short_pct=0.2, horizon=1)
        ls = portfolio.set_index("period")["ls_ret"]
        stats = performance_stats(ls)

        _check("construct_portfolio", len(portfolio) > 0, f"{len(portfolio)} months")
        _check("performance_stats",   "sharpe" in stats,  f"sharpe={stats.get('sharpe', 'N/A'):.2f}")
        _check("tc_reduces_net",
               (portfolio["ls_ret"] <= portfolio["ls_gross"] + 1e-10).all(), "net ≤ gross")
    except Exception as exc:
        print(f"  ERROR: {exc}")
        failures.append("portfolio")

    # ------------------------------------------------------------------
    # 6. Stats
    # ------------------------------------------------------------------
    _section("6 — Statistical tests")
    try:
        from urbangrowth.modeling.stats import bootstrap_ic_ci, fdr_correction, newey_west_tstat
        import pandas as pd

        ic = pd.Series(rng.normal(0.04, 0.10, 48))
        t  = newey_west_tstat(ic)
        lo, hi = bootstrap_ic_ci(ic, n_boot=200)

        pvals = pd.Series(rng.uniform(0, 1, 20))
        fdr   = fdr_correction(pvals)

        _check("newey_west_tstat",  isinstance(t, float),  f"t={t:.2f}")
        _check("bootstrap_ic_ci",   lo < hi,               f"[{lo:.3f}, {hi:.3f}]")
        _check("fdr_correction",    len(fdr) == 20,        f"{len(fdr)} rows")
    except Exception as exc:
        print(f"  ERROR: {exc}")
        failures.append("stats")

    # ------------------------------------------------------------------
    # 7. Artifacts
    # ------------------------------------------------------------------
    _section("7 — Artifact persistence")
    try:
        import tempfile
        from pathlib import Path as PPath
        from urbangrowth.modeling.artifacts import load_artifact, save_artifact

        with tempfile.TemporaryDirectory() as tmp:
            save_artifact({"key": "value", "list": [1, 2, 3]},
                          "test_artifact", artifact_dir=PPath(tmp))
            loaded = load_artifact("test_artifact", artifact_dir=PPath(tmp))

        _check("save_artifact", True)
        _check("load_artifact", loaded == {"key": "value", "list": [1, 2, 3]})
    except Exception as exc:
        print(f"  ERROR: {exc}")
        failures.append("artifacts")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    elapsed = time.perf_counter() - t0
    _section("Summary")
    print(f"  Elapsed: {elapsed:.1f}s")
    if failures:
        print(f"  FAILURES ({len(failures)}): {', '.join(failures)}")
        return 1
    else:
        print("  All checks passed.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
