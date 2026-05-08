"""Urban Growth Research Platform — Streamlit Dashboard.

Launch:  streamlit run dashboards/streamlit_app.py

Pages are auto-discovered from the pages/ subdirectory.
This file is the landing / status page.
"""
from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Urban Growth Research Platform",
    page_icon="🏗",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("Urban Growth Research Platform")
st.caption("Construction / infrastructure alt-data signal research · Phoenix & Austin")

# ── Status cards ─────────────────────────────────────────────────────────────
from data import (
    load_universe_df, load_signal_sources, load_returns_coverage,
    h3_available_months, _proc_dir,
)

st.markdown("### Platform Status")
c1, c2, c3, c4 = st.columns(4)

universe = load_universe_df()
c1.metric("Universe", f"{len(universe)} tickers", help="Rows in universe.yaml")

sources = load_signal_sources()
c2.metric("Signal sources", len(sources), help="Distinct sources in signal_features")

ret_cov = load_returns_coverage()
c3.metric("Return months", len(ret_cov), help="Months with ≥1 return in DB")

phx_months = h3_available_months("phoenix")
c4.metric("H3 months (PHX)", len(phx_months), help="Months with H3 LC parquets")

st.divider()

# ── Universe table ────────────────────────────────────────────────────────────
st.markdown("### Universe")
if not universe.empty:
    display_cols = [c for c in ["symbol", "name", "sector", "market_cap_tier",
                                 "geographic_concentration"] if c in universe.columns]
    st.dataframe(universe[display_cols] if display_cols else universe,
                 use_container_width=True, height=320)
else:
    st.info("Universe not loaded — check that `urbangrowth` package is importable.")

st.divider()

# ── Quick-start guide ─────────────────────────────────────────────────────────
st.markdown("### Quick start")
st.code("""# 1. Set up environment (first time only)
conda env create -f environment.yml
conda activate urbangrowth

# 2. Init database
ug doctor

# 3. Ingest data
ug ingest markets --start 2015-01-01
ug ingest census-bps --start 2015-01
ug ingest fred
ug ingest ferc-queue

# 4. Build signals
ug signals build --start 2015-01 --end 2025-12

# 5. Validate signals
ug model ic-test --signal all
ug model decay --signal all
ug model cross-section --horizon 1

# 6. Fit + backtest
ug model fit --model ridge --horizon 1
ug model backtest --model ridge --horizon 1
""", language="bash")
