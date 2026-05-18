"""Typer CLI — entry point: ug <command> [options]

Quick reference:
    ug doctor                                              # verify env before anything else
    ug ingest census-bps
    ug ingest markets
    ug ingest sentinel2 --city phoenix                     # STAC connectivity check
    ug composite generate --city phoenix --start 2018-01   # build monthly COG composites
    ug segment run --city phoenix --model dynamic_world    # land cover segmentation
    ug process h3 --city phoenix                           # aggregate to H3 hex grid
    ug process change --city phoenix                       # compute transition matrices
    ug features build --city phoenix                       # assemble full feature panel
    ug signals build --start 2015-01 --end 2025-12         # build all signal families
    ug signals permit
    ug model ic-test --signal permit --horizon 3
    ug model fit --model ridge --horizon 1
    ug model backtest --model ridge --horizon 1
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import structlog
import typer
from dotenv import load_dotenv

load_dotenv()

app = typer.Typer(
    name="ug",
    help="Urban Growth Research Platform",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
ingest_app     = typer.Typer(help="Data ingestion",                      no_args_is_help=True)
composite_app  = typer.Typer(help="Sentinel-2 composite generation",     no_args_is_help=True)
segment_app    = typer.Typer(help="Land cover segmentation",             no_args_is_help=True)
process_app    = typer.Typer(help="Processing pipeline",                 no_args_is_help=True)
features_app   = typer.Typer(help="H3 feature table assembly",           no_args_is_help=True)
signals_app    = typer.Typer(help="Signal generation",                   no_args_is_help=True)
model_app      = typer.Typer(help="Modeling and backtests",              no_args_is_help=True)

app.add_typer(ingest_app,    name="ingest")
app.add_typer(composite_app, name="composite")
app.add_typer(segment_app,   name="segment")
app.add_typer(process_app,   name="process")
app.add_typer(features_app,  name="features")
app.add_typer(signals_app,   name="signals")
app.add_typer(model_app,     name="model")

log = structlog.get_logger(__name__)

# ── DOCTOR ───────────────────────────────────────────────────────────────────

_OK   = "[green]  ✓ [/green]"
_WARN = "[yellow]  ⚠ [/yellow]"
_FAIL = "[red]  ✗ [/red]"


def _check(label: str, ok: bool, detail: str, warn_only: bool = False) -> bool:
    symbol = _OK if ok else (_WARN if warn_only else _FAIL)
    typer.echo(typer.style(
        f"{'✓' if ok else ('⚠' if warn_only else '✗')}  {label}: {detail}",
        fg=typer.colors.GREEN if ok else (typer.colors.YELLOW if warn_only else typer.colors.RED),
    ))
    return ok or warn_only


@app.command("doctor")
def doctor() -> None:
    """Verify GPU sm_120, PostgreSQL, API keys, and disk space before running any pipeline."""
    from rich.console import Console
    from rich.rule import Rule

    console = Console()
    console.print(Rule("[bold cyan]Urban Growth Research Platform — Doctor[/bold cyan]"))

    failures: list[str] = []
    warnings: list[str] = []

    # ── 1. NVIDIA GPU ─────────────────────────────────────────────────────
    try:
        result = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,driver_version,compute_cap",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
            for line in lines:
                name, driver, cc = [x.strip() for x in line.split(",")]
                typer.echo(typer.style(
                    f"✓  GPU: {name}  driver {driver}  compute {cc}",
                    fg=typer.colors.GREEN,
                ))
        else:
            failures.append("NVIDIA GPU not detected — install NVIDIA driver 570+")
            typer.echo(typer.style(f"✗  NVIDIA GPU: not detected", fg=typer.colors.RED))
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        failures.append(f"nvidia-smi failed: {e}")
        typer.echo(typer.style(f"✗  nvidia-smi not found — is NVIDIA driver installed?", fg=typer.colors.RED))

    # ── 2. CUDA runtime version ───────────────────────────────────────────
    try:
        result = subprocess.run(
            ["nvidia-smi"], capture_output=True, text=True, timeout=10,
        )
        import re
        m = re.search(r"CUDA Version:\s+([\d.]+)", result.stdout)
        cuda_ver = m.group(1) if m else "unknown"
        major, minor = (int(x) for x in cuda_ver.split(".")[:2]) if cuda_ver != "unknown" else (0, 0)
        ok = major > 12 or (major == 12 and minor >= 8)
        msg = f"CUDA {cuda_ver} {'(>= 12.8 ✓)' if ok else '(< 12.8 — upgrade driver for RTX 5070)'}"
        if ok:
            typer.echo(typer.style(f"✓  {msg}", fg=typer.colors.GREEN))
        else:
            warnings.append(msg)
            typer.echo(typer.style(f"⚠  {msg}", fg=typer.colors.YELLOW))
    except Exception as e:
        warnings.append(f"CUDA version check failed: {e}")

    # ── 3. PyTorch CUDA + sm_120 ──────────────────────────────────────────
    try:
        import torch
        torch_ver = torch.__version__
        cuda_available = torch.cuda.is_available()
        if not cuda_available:
            failures.append("torch.cuda.is_available() == False — reinstall PyTorch nightly cu128")
            typer.echo(typer.style("✗  PyTorch: CUDA not available", fg=typer.colors.RED))
        else:
            cc = torch.cuda.get_device_capability()
            dev_name = torch.cuda.get_device_name(0)
            cuda_build = torch.version.cuda
            sm_tag = f"sm_{cc[0]}{cc[1]:02d}"
            is_sm120 = cc == (12, 0)
            msg = f"PyTorch {torch_ver} | CUDA build {cuda_build} | {dev_name} | {sm_tag}"
            if is_sm120:
                typer.echo(typer.style(f"✓  {msg} ← Blackwell confirmed", fg=typer.colors.GREEN))
            else:
                typer.echo(typer.style(f"⚠  {msg} (expected sm_120 for RTX 5070)", fg=typer.colors.YELLOW))
                warnings.append(f"Compute capability {cc} — expected (12,0) for Blackwell RTX 5070")
    except ImportError:
        failures.append("PyTorch not installed — run 00_setup_env.sh")
        typer.echo(typer.style("✗  PyTorch: not installed", fg=typer.colors.RED))
    except Exception as e:
        failures.append(f"PyTorch check error: {e}")
        typer.echo(typer.style(f"✗  PyTorch: {e}", fg=typer.colors.RED))

    # ── 4. PostgreSQL connection ──────────────────────────────────────────
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            port=int(os.environ.get("POSTGRES_PORT", 5432)),
            dbname=os.environ.get("POSTGRES_DB", "urbangrowth"),
            user=os.environ.get("POSTGRES_USER", "urbangrowth"),
            password=os.environ.get("POSTGRES_PASSWORD", ""),
            connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute("SELECT version();")
        pg_ver = cur.fetchone()[0].split(",")[0]

        # ── 5. PostGIS extension ──────────────────────────────────────────
        try:
            cur.execute("SELECT PostGIS_version();")
            postgis_ver = cur.fetchone()[0]
            typer.echo(typer.style(
                f"✓  PostgreSQL: {pg_ver} | PostGIS {postgis_ver}",
                fg=typer.colors.GREEN,
            ))
        except Exception:
            conn.rollback()
            warnings.append("PostGIS extension not installed — run 01_init_postgis.sh")
            typer.echo(typer.style(
                f"⚠  PostgreSQL connected but PostGIS missing — run 01_init_postgis.sh",
                fg=typer.colors.YELLOW,
            ))

        # ── 6. h3 extension (optional) ────────────────────────────────────
        cur.execute(
            "SELECT extname FROM pg_extension WHERE extname = 'h3';"
        )
        h3_row = cur.fetchone()
        if h3_row:
            typer.echo(typer.style("✓  h3 PostgreSQL extension installed", fg=typer.colors.GREEN))
        else:
            typer.echo(typer.style(
                "⚠  h3 extension not in PostgreSQL (optional — h3-py handles all H3 ops in Python)",
                fg=typer.colors.YELLOW,
            ))
            warnings.append("h3 PostgreSQL extension absent (optional)")

        # ── 7. Table count ────────────────────────────────────────────────
        cur.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';"
        )
        n_tables = cur.fetchone()[0]
        typer.echo(typer.style(f"✓  Schema: {n_tables} tables in public schema", fg=typer.colors.GREEN))
        conn.close()

    except Exception as e:
        failures.append(f"PostgreSQL: {e}")
        typer.echo(typer.style(f"✗  PostgreSQL: {e}", fg=typer.colors.RED))
        typer.echo(typer.style("   → Run 01_init_postgis.sh to create the database", fg=typer.colors.RED))

    # ── 8. Disk space ─────────────────────────────────────────────────────
    data_root = os.environ.get("URBANGROWTH_DATA_ROOT", "C:/urbangrowth_data")
    root_path = Path(data_root)
    if root_path.exists():
        total, used, free = shutil.disk_usage(root_path)
        free_tb = free / (1024 ** 4)
        free_gb = free / (1024 ** 3)
        ok = free_gb > 100
        label = f"{free_tb:.1f} TB" if free_tb >= 1 else f"{free_gb:.0f} GB"
        msg = f"Disk: {label} free at {data_root}"
        if ok:
            typer.echo(typer.style(f"✓  {msg}", fg=typer.colors.GREEN))
        else:
            warnings.append(f"{msg} — Sentinel-2 data needs 500GB+")
            typer.echo(typer.style(f"⚠  {msg} — recommend 500GB+ free for satellite data", fg=typer.colors.YELLOW))
    else:
        warnings.append(f"Data root {data_root} does not exist — run 00_setup_env.sh")
        typer.echo(typer.style(f"⚠  Data root not found: {data_root}", fg=typer.colors.YELLOW))

    # ── 9. API keys ───────────────────────────────────────────────────────
    for key_name in ("CENSUS_API_KEY", "FRED_API_KEY"):
        val = os.environ.get(key_name, "")
        if val and len(val) > 8:
            typer.echo(typer.style(f"✓  {key_name} set ({val[:4]}...)", fg=typer.colors.GREEN))
        else:
            failures.append(f"{key_name} not set — add to .env")
            typer.echo(typer.style(f"✗  {key_name} not set — add to .env", fg=typer.colors.RED))

    # ── 10. h3-py Python package ──────────────────────────────────────────
    try:
        import h3
        typer.echo(typer.style(f"✓  h3-py {h3.__version__} installed", fg=typer.colors.GREEN))
    except ImportError:
        warnings.append("h3-py not installed — conda install h3-py")
        typer.echo(typer.style("⚠  h3-py not installed", fg=typer.colors.YELLOW))

    # ── Summary ───────────────────────────────────────────────────────────
    console.print(Rule())
    if failures:
        typer.echo(typer.style(
            f"FAILED: {len(failures)} check(s) failed, {len(warnings)} warning(s)",
            fg=typer.colors.RED, bold=True,
        ))
        for f in failures:
            typer.echo(typer.style(f"  ✗ {f}", fg=typer.colors.RED))
        raise typer.Exit(code=1)
    elif warnings:
        typer.echo(typer.style(
            f"PASSED with {len(warnings)} warning(s) — pipeline can proceed",
            fg=typer.colors.YELLOW, bold=True,
        ))
    else:
        typer.echo(typer.style("All checks passed — ready to ingest data", fg=typer.colors.GREEN, bold=True))


# ── INGEST ───────────────────────────────────────────────────────────────────

@ingest_app.command("census-bps")
def ingest_census_bps(
    start: str = typer.Option("2010-01", help="Start month YYYY-MM"),
    end: str = typer.Option("2025-12", help="End month YYYY-MM"),
) -> None:
    """Download Census Building Permits Survey data."""
    from urbangrowth.data.census_bps import run
    run(start=start, end=end)


@ingest_app.command("ferc-queue")
def ingest_ferc(
    snapshot_date: Optional[str] = typer.Option(
        None,
        "--snapshot-date",
        help="Snapshot date YYYY-MM-DD; defaults to today",
    ),
) -> None:
    """Download and parse FERC interconnection queue."""
    from urbangrowth.data.ferc_queue import run
    run(snapshot_date=snapshot_date)


@ingest_app.command("usaspending")
def ingest_usaspending(
    start: str = typer.Option("2015-01", help="Start month YYYY-MM"),
    end: str = typer.Option("2025-12", help="End month YYYY-MM"),
) -> None:
    """Fetch construction contract awards from USASpending."""
    from urbangrowth.data.usaspending import run
    run(start=start, end=end)


@ingest_app.command("dot-tips")
def ingest_dot_tips(
    states: str = typer.Option(
        "TX,CA,AZ",
        "--states",
        help="Comma-separated state codes to scrape (TX,CA,AZ)",
    ),
) -> None:
    """Scrape state DOT Transportation Improvement Programs."""
    from urbangrowth.data.dot_tips import run
    run(states=states)


@ingest_app.command("fred")
def ingest_fred(
    series_config: str = typer.Option(
        "config/fred_series.yaml",
        "--series-config",
        help="Path to fred_series.yaml manifest (relative to repo root or absolute)",
    ),
    start: str = typer.Option("2010-01-01", help="History start date (ISO format)"),
    end: Optional[str] = typer.Option(None, help="End date (ISO format); defaults to pipeline dates.end"),
) -> None:
    """Fetch FRED macro and construction series."""
    from urbangrowth.data.fred import run
    run(series_config=series_config, start=start, end=end)


@ingest_app.command("sentinel2")
def ingest_sentinel2(
    city: str = typer.Option("phoenix"),
    start: Optional[str] = typer.Option(None),
    end: Optional[str] = typer.Option(None),
) -> None:
    """Download Sentinel-2 scenes for a city."""
    from urbangrowth.data.sentinel2 import run
    run(city=city)


@ingest_app.command("elevation")
def ingest_elevation(city: str = typer.Option("phoenix")) -> None:
    """Download USGS 3DEP DEM tiles."""
    from urbangrowth.data.elevation import run
    run(city=city)


@ingest_app.command("zoning")
def ingest_zoning(
    city: str = typer.Option("phoenix"),
    snapshot_date: Optional[str] = typer.Option(
        None,
        "--snapshot-date",
        help="Snapshot date YYYY-MM-DD; defaults to today",
    ),
) -> None:
    """Download zoning layers."""
    from urbangrowth.data.zoning import run
    run(city=city, snapshot_date=snapshot_date)


@ingest_app.command("city-permits")
def ingest_city_permits(
    city: str = typer.Option("phoenix"),
    since: str = typer.Option("2015-01-01", "--since", help="Earliest issue date YYYY-MM-DD"),
) -> None:
    """Download city-level building permits."""
    from urbangrowth.data.city_permits import run
    run(city=city, since=since)


@ingest_app.command("census-acs")
def ingest_census_acs(
    city: str = typer.Option("phoenix"),
    year: int = typer.Option(2022, "--year", help="ACS 5-year vintage year"),
) -> None:
    """Download Census ACS demographics + TIGER block-group boundaries."""
    from urbangrowth.data.census_acs import run
    run(city=city, year=year)


@ingest_app.command("parcels")
def ingest_parcels(city: str = typer.Option("phoenix")) -> None:
    """Download county assessor parcel data."""
    from urbangrowth.data.parcels import run
    run(city=city)


@ingest_app.command("osm")
def ingest_osm(city: str = typer.Option("phoenix")) -> None:
    """Download OpenStreetMap features."""
    from urbangrowth.data.osm import run
    run(city=city)


@ingest_app.command("markets")
def ingest_markets(
    start: Optional[str] = typer.Option(None, help="Start date YYYY-MM-DD; defaults to pipeline history_start"),
    end:   Optional[str] = typer.Option(None, help="End date YYYY-MM-DD; defaults to pipeline dates.end"),
) -> None:
    """Download daily OHLCV + returns for universe and benchmarks."""
    from urbangrowth.data.markets import run
    run(start=start, end=end)


# ── PROCESS ──────────────────────────────────────────────────────────────────

@composite_app.command("generate")
def composite_generate(
    city: str = typer.Option("phoenix", help="City key (phoenix | austin)"),
    start: str = typer.Option("2018-01", "--start", help="Start month YYYY-MM"),
    end:   str = typer.Option("2025-12", "--end",   help="End month YYYY-MM"),
    max_cloud_pct: int = typer.Option(60, "--max-cloud-pct", help="Scene cloud cover ceiling"),
) -> None:
    """Build monthly cloud-masked median composites (resumable).

    Skips months whose output COG already exists.
    Uses Planetary Computer STAC COGs — no raw scene downloads required.
    """
    from urbangrowth.processing.composites import run
    run(city=city, start=start, end=end, max_cloud_pct=max_cloud_pct)


@process_app.command("composites")
def process_composites(
    city: str = typer.Option("phoenix"),
    start: str = typer.Option("2018-01", "--start", help="Start month YYYY-MM"),
    end:   str = typer.Option("2025-12", "--end",   help="End month YYYY-MM"),
) -> None:
    """Build cloud-masked monthly Sentinel-2 composites (alias for composite generate)."""
    from urbangrowth.processing.composites import run
    run(city=city, start=start, end=end)


@segment_app.command("run")
def segment_run(
    city: str = typer.Option("phoenix", help="City key (phoenix | austin)"),
    start: str = typer.Option("2018-01", "--start", help="Start month YYYY-MM"),
    end:   str = typer.Option("2025-12", "--end",   help="End month YYYY-MM"),
    model: str = typer.Option("dynamic_world", "--model", help="dynamic_world | prithvi"),
    device: str = typer.Option("cuda", "--device", help="cuda | cpu"),
) -> None:
    """Segment monthly composites into 9 DynamicWorld land cover classes (resumable).

    Outputs per month: {YYYY-MM}_class.tif (uint8) and {YYYY-MM}_prob.tif (float16×9).
    Skips months whose class raster already exists.
    """
    from urbangrowth.processing.segmentation import run
    run(city=city, start=start, end=end, model_name=model, device=device)


@process_app.command("segment")
def process_segment(city: str = typer.Option("phoenix")) -> None:
    """Run land cover segmentation on composites (alias for segment run)."""
    from urbangrowth.processing.segmentation import run
    run(city=city)


@process_app.command("change")
def process_change(
    city: str = typer.Option("phoenix"),
    start: str = typer.Option("2018-01", "--start", help="Start month YYYY-MM"),
    end:   str = typer.Option("2025-12", "--end",   help="End month YYYY-MM"),
) -> None:
    """Compute per-H3-cell land cover transition matrices (resumable)."""
    from urbangrowth.processing.change import run
    run(city=city, start=start, end=end)


@process_app.command("h3")
def process_h3(
    city: str = typer.Option("phoenix"),
    start: str = typer.Option("2018-01", "--start", help="Start month YYYY-MM"),
    end:   str = typer.Option("2025-12", "--end",   help="End month YYYY-MM"),
) -> None:
    """Aggregate land cover rasters to H3 hex grid (resumable)."""
    from urbangrowth.processing.h3_aggregate import run
    run(city=city, start=start, end=end)


@process_app.command("features")
def process_features(
    city: str = typer.Option("phoenix"),
    start: str = typer.Option("2018-01", "--start", help="Start month YYYY-MM"),
    end:   str = typer.Option("2025-12", "--end",   help="End month YYYY-MM"),
) -> None:
    """Build final H3 feature panel tables (alias for features build)."""
    from urbangrowth.processing.features import run
    run(city=city, start=start, end=end)


# ── FEATURES ─────────────────────────────────────────────────────────────────

@features_app.command("build")
def features_build(
    city: str = typer.Option("phoenix", help="City key (phoenix | austin)"),
    start: str = typer.Option("2018-01", "--start", help="Start month YYYY-MM"),
    end:   str = typer.Option("2025-12", "--end",   help="End month YYYY-MM"),
    resolution: int = typer.Option(8, "--resolution", help="H3 resolution"),
) -> None:
    """Assemble per-H3-cell feature panel for the full date range (resumable).

    Requires h3_aggregate and change parquets to exist first:

        ug process h3     --city phoenix --start 2018-01 --end 2025-12
        ug process change --city phoenix --start 2018-01 --end 2025-12
        ug features build --city phoenix --start 2018-01 --end 2025-12

    Outputs monthly parquets + a full panel parquet, and upserts to DB.
    """
    from urbangrowth.processing.features import run
    run(city=city, start=start, end=end, resolution=resolution)


# ── SIGNALS ──────────────────────────────────────────────────────────────────

@signals_app.command("build")
def sig_build(
    start: str = typer.Option("2015-01", "--start", help="Start month YYYY-MM"),
    end:   str = typer.Option("2025-12", "--end",   help="End month YYYY-MM"),
) -> None:
    """Build all signal families and write to signal_features table (resumable).

    Runs permit → FERC → USASpending → FRED → DOT TIPs → city growth in sequence.
    Each family is independent; a failure in one does not block others.
    """
    from urbangrowth.signals.build import run
    run(start=start, end=end)


@signals_app.command("permit")
def sig_permit(
    start: str = typer.Option("2015-01", "--start", help="Start month YYYY-MM"),
    end:   str = typer.Option("2025-12", "--end",   help="End month YYYY-MM"),
) -> None:
    """Build national + state-weighted permit signal table."""
    from urbangrowth.signals.permit_signals import run
    run(start=start, end=end)


@signals_app.command("ferc")
def sig_ferc(
    start: str = typer.Option("2015-01", "--start", help="Start month YYYY-MM"),
    end:   str = typer.Option("2025-12", "--end",   help="End month YYYY-MM"),
) -> None:
    """Build FERC interconnection queue signal table."""
    from urbangrowth.signals.ferc_signals import run
    run(start=start, end=end)


@signals_app.command("usaspending")
def sig_usaspending(
    start: str = typer.Option("2015-01", "--start", help="Start month YYYY-MM"),
    end:   str = typer.Option("2025-12", "--end",   help="End month YYYY-MM"),
) -> None:
    """Build USASpending federal award signal table."""
    from urbangrowth.signals.usaspending_signals import run
    run(start=start, end=end)


@signals_app.command("fred")
def sig_fred(
    start: str = typer.Option("2015-01", "--start", help="Start month YYYY-MM"),
    end:   str = typer.Option("2025-12", "--end",   help="End month YYYY-MM"),
) -> None:
    """Build FRED macro construction signal table."""
    from urbangrowth.signals.fred_signals import run
    run(start=start, end=end)


@signals_app.command("dot-tips")
def sig_dot_tips(
    start: str = typer.Option("2015-01", "--start", help="Start month YYYY-MM"),
    end:   str = typer.Option("2025-12", "--end",   help="End month YYYY-MM"),
) -> None:
    """Build state DOT TIP award signal table."""
    from urbangrowth.signals.dot_tips_signals import run
    run(start=start, end=end)


@signals_app.command("city-growth")
def sig_city_growth(
    start: str = typer.Option("2018-01", "--start", help="Start month YYYY-MM"),
    end:   str = typer.Option("2025-12", "--end",   help="End month YYYY-MM"),
) -> None:
    """Build city-level satellite urban growth signal table."""
    from urbangrowth.signals.city_growth_signals import run
    run(start=start, end=end)


# ── MODEL ────────────────────────────────────────────────────────────────────

@model_app.command("ic-test")
def model_ic(
    signal: str = typer.Option(..., help="Signal name"),
    horizon: int = typer.Option(None, help="Horizon in months; omit to run 1/2/3"),
) -> None:
    """Run IC test for a signal."""
    from urbangrowth.modeling.ic_test import run
    run(signal_name=signal, horizon=horizon if horizon else None)


@model_app.command("decay")
def model_decay(signal: str = typer.Option(...)) -> None:
    """Compute IC decay profile (lag 1-12 months)."""
    from urbangrowth.modeling.decay_test import run
    run(signal_name=signal)


@model_app.command("cross-section")
def model_cross_section(horizon: int = typer.Option(1)) -> None:
    """Run Fama-MacBeth cross-sectional regression."""
    from urbangrowth.modeling.cross_section import run
    run(horizon=horizon)


@model_app.command("fit")
def model_fit(
    model: str  = typer.Option("ridge",  "--model",   help="ridge | elasticnet | lgbm"),
    horizon: int = typer.Option(1,        "--horizon", help="Forward return horizon in months"),
    min_train: int = typer.Option(24,     "--min-train", help="Minimum training months before first prediction"),
) -> None:
    """Build walk-forward model scores (expanding window, retrain monthly).

    Saves processed/signals/model_scores_{model}_h{horizon}.parquet.
    Run this before 'ug model backtest'.
    """
    from urbangrowth.modeling.cross_section import run_cross_sectional_model
    run_cross_sectional_model(model_type=model, horizon=horizon, min_train_months=min_train)


@model_app.command("backtest")
def model_backtest(
    model:     str   = typer.Option("ridge", "--model",     help="ridge | elasticnet | lgbm"),
    horizon:   int   = typer.Option(1,       "--horizon",   help="Forward return horizon in months"),
    long_pct:  float = typer.Option(0.2,     "--long-pct",  help="Fraction of universe to go long"),
    short_pct: float = typer.Option(0.2,     "--short-pct", help="Fraction of universe to go short"),
) -> None:
    """Run L/S backtest: top-quintile long, bottom-quintile short, 10 bps/side TC.

    Loads pre-computed walk-forward scores (runs 'ug model fit' automatically if missing).
    """
    from urbangrowth.modeling.backtest import run
    run(model_type=model, horizon=horizon)


if __name__ == "__main__":
    app()
