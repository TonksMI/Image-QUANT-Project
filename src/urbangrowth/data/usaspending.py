"""USASpending federal contract awards ingestion.

Pulls construction/infrastructure contract awards (NAICS 236*, 237*, 238*)
from api.usaspending.gov.  No API key required.

Idempotent: checks local parquet cache before querying the API.
Monthly cache files: raw/usaspending/awards_YYYY-MM.parquet
DB target table   : usaspending_awards  (see db/schema.sql)
"""
from __future__ import annotations

import time
from calendar import monthrange
from pathlib import Path
from typing import Iterator

import pandas as pd
import requests
import structlog
import yaml
from tenacity import (
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from urbangrowth.config import data_path
from urbangrowth.db import loaders

log = structlog.get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_BASE_URL = "https://api.usaspending.gov/api/v2"
_ENDPOINT = "/search/spending_by_award/"
_RATE_LIMIT_SLEEP = 0.10  # seconds between requests (~10 req/s)

NAICS_PREFIXES: list[str] = ["23"]  # 2-digit construction sector; USASpending requires 2, 4, or 6 digit codes

# All contract award type codes
_CONTRACT_CODES: list[str] = ["A", "B", "C", "D"]

# Fields requested from the API
_FIELDS: list[str] = [
    "Award ID",
    "Recipient Name",
    "recipient_uei",
    "Award Amount",
    "Action Date",
    "NAICS Code",
    "NAICS Description",
    "Place of Performance State Code",
    "Place of Performance Zip5",
]

# Column rename map: API field name → DataFrame column name
_COL_MAP: dict[str, str] = {
    "Award ID": "award_id",
    "Recipient Name": "recipient_name",
    "Award Amount": "amount",
    "Action Date": "award_date",
    "NAICS Code": "naics",
    "Place of Performance State Code": "state",
    "Place of Performance Zip5": "performance_zip",
}

# Default config path (relative to repo root)
_CONFIG_DIR = Path(__file__).parent.parent.parent.parent / "config"

# ── Config helpers ─────────────────────────────────────────────────────────────


def _default_config_path() -> Path:
    """Locate config/recipient_to_ticker.yaml from the installed package tree."""
    return _CONFIG_DIR / "recipient_to_ticker.yaml"


def load_recipient_mapping(config_path: Path | None = None) -> dict[str, list[str]]:
    """Load config/recipient_to_ticker.yaml.

    Returns {ticker: [name_fragment, ...]} with all fragments lowercased.
    """
    path = config_path or _default_config_path()
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    raw: dict[str, list[str]] = data.get("mapping", {})
    return {ticker: [frag.lower() for frag in frags] for ticker, frags in raw.items()}


def load_exclusions(config_path: Path | None = None) -> list[str]:
    """Load the exclusion list from config/recipient_to_ticker.yaml.

    Returns a list of lowercase name substrings that must NOT match any ticker.
    """
    path = config_path or _default_config_path()
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [e.lower() for e in data.get("exclusions", [])]


# ── Recipient matching ─────────────────────────────────────────────────────────


def match_recipient(
    name: str,
    mapping: dict[str, list[str]],
    exclusions: list[str],
) -> str | None:
    """Attempt to map a recipient name to a ticker symbol.

    Steps:
    1. Check exclusions — if any exclusion fragment is a substring of ``name``,
       return None (prevents false positives).
    2. Scan manual mapping — first ticker whose fragment list contains a
       case-insensitive substring match wins.
    3. Return the matched ticker, or None if nothing matched.
    """
    lower_name = name.lower()

    # Step 1: exclusions take priority
    for excl in exclusions:
        if excl in lower_name:
            return None

    # Step 2: manual mapping
    for ticker, fragments in mapping.items():
        for frag in fragments:
            if frag in lower_name:
                return ticker

    return None


# ── API helpers ────────────────────────────────────────────────────────────────


def _build_naics_list(prefixes: list[str]) -> list[str]:
    """Return 3-digit NAICS prefix codes; USASpending API does prefix matching."""
    return list(prefixes)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code >= 500
    return False


@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def search_awards_page(
    naics_list: list[str],
    start_date: str,
    end_date: str,
    page: int,
    limit: int = 100,
) -> dict:
    """POST a single page request to /api/v2/search/spending_by_award/.

    Returns the parsed JSON dict with keys ``results`` and ``page_metadata``.
    Retries up to 5 times on HTTP errors using exponential back-off.
    """
    payload = {
        "filters": {
            "award_type_codes": _CONTRACT_CODES,
            "naics_codes": {"require": naics_list},
            "time_period": [{"start_date": start_date, "end_date": end_date}],
        },
        "fields": _FIELDS,
        "sort": "Award ID",
        "order": "desc",
        "limit": limit,
        "page": page,
    }

    url = f"{_BASE_URL}{_ENDPOINT}"
    log.debug("usaspending_request", url=url, page=page, naics_count=len(naics_list))

    resp = requests.post(url, json=payload, timeout=60)
    if not resp.ok:
        log.error("usaspending_api_error", status=resp.status_code, body=resp.text[:500])
    resp.raise_for_status()
    time.sleep(_RATE_LIMIT_SLEEP)
    return resp.json()


# ── Pagination & DataFrame builder ────────────────────────────────────────────


def _iter_pages(
    naics_list: list[str],
    start_date: str,
    end_date: str,
    limit: int = 100,
) -> Iterator[list[dict]]:
    """Yield successive pages of award results until pagination is exhausted."""
    page = 1
    while True:
        data = search_awards_page(naics_list, start_date, end_date, page, limit)
        results = data.get("results", [])
        if not results:
            break
        yield results
        meta = data.get("page_metadata", {})
        if not meta.get("hasNext", False):
            break
        page = meta.get("page", page) + 1


def _records_to_df(
    records: list[dict],
    mapping: dict[str, list[str]],
    exclusions: list[str],
) -> pd.DataFrame:
    """Convert a list of raw API result dicts to a typed DataFrame.

    Applies recipient matching and maps columns to the DB schema names.
    """
    if not records:
        return pd.DataFrame(
            columns=[
                "award_id",
                "award_date",
                "naics",
                "recipient_name",
                "recipient_ticker_guess",
                "amount",
                "state",
                "performance_zip",
            ]
        )

    df = pd.DataFrame(records)

    # Rename columns that have a direct mapping; drop the rest
    df = df.rename(columns=_COL_MAP)

    # Ensure all target columns exist (some may be missing in sparse responses)
    for col in ["award_id", "recipient_name", "amount", "award_date", "naics",
                "state", "performance_zip"]:
        if col not in df.columns:
            df[col] = None

    # Match recipient → ticker
    df["recipient_ticker_guess"] = df["recipient_name"].apply(
        lambda n: match_recipient(str(n), mapping, exclusions) if pd.notna(n) else None
    )

    # Type coercions
    _dates = pd.to_datetime(df["award_date"], errors="coerce")
    df["award_date"] = [d.date() if pd.notna(d) else None for d in _dates]
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["naics"] = df["naics"].astype(str).where(df["naics"].notna())

    # Trim state to 2 chars (API sometimes returns full name)
    df["state"] = df["state"].apply(
        lambda s: str(s)[:2].upper() if pd.notna(s) and s else None
    )

    return df[[
        "award_id",
        "award_date",
        "naics",
        "recipient_name",
        "recipient_ticker_guess",
        "amount",
        "state",
        "performance_zip",
    ]]


def fetch_all_awards(
    naics_prefixes: list[str],
    start: str,
    end: str,
) -> pd.DataFrame:
    """Paginate through all awards for the given NAICS prefixes and date range.

    Args:
        naics_prefixes: List of 3-digit NAICS prefixes, e.g. ["236", "237", "238"].
        start: ISO date string "YYYY-MM-DD".
        end:   ISO date string "YYYY-MM-DD".

    Returns:
        DataFrame with columns matching ``usaspending_awards`` schema.
        Logs a warning for each unmatched recipient with amount > $10 million.
    """
    naics_list = _build_naics_list(naics_prefixes)
    mapping = load_recipient_mapping()
    exclusions = load_exclusions()

    all_records: list[dict] = []
    for page_records in _iter_pages(naics_list, start, end):
        all_records.extend(page_records)
        log.debug("usaspending_page_fetched", cumulative_records=len(all_records))

    log.info("usaspending_all_pages_fetched", total_records=len(all_records),
             start=start, end=end)

    df = _records_to_df(all_records, mapping, exclusions)

    # Warn about large unmatched recipients
    unmatched_large = df[
        (df["recipient_ticker_guess"].isna()) &
        (df["amount"].fillna(0) > 10_000_000)
    ]
    for _, row in unmatched_large.iterrows():
        log.warning(
            "unmatched_large_recipient",
            recipient=row["recipient_name"],
            amount=row["amount"],
            award_id=row["award_id"],
        )

    return df


# ── Monthly iteration helpers ──────────────────────────────────────────────────


def _month_range(start: str, end: str) -> Iterator[tuple[int, int]]:
    """Yield (year, month) tuples from start to end inclusive.

    Args:
        start: "YYYY-MM" string.
        end:   "YYYY-MM" string.
    """
    sy, sm = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    y, m = sy, sm
    while (y, m) <= (ey, em):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def _month_cache_path(year: int, month: int) -> Path:
    """Return the parquet cache path for a given month."""
    return data_path("raw/usaspending") / f"awards_{year:04d}-{month:02d}.parquet"


# ── CLI entry point ────────────────────────────────────────────────────────────


def run(start: str = "2015-01", end: str = "2025-12") -> None:
    """Fetch and store federal construction contract awards, month by month.

    Idempotent: skips any month whose parquet cache already exists.
    Upserts each month into the ``usaspending_awards`` DB table.

    Args:
        start: First month to ingest, "YYYY-MM".
        end:   Last month to ingest, "YYYY-MM".
    """
    total_awards = 0
    total_matched = 0
    total_unmatched_large = 0

    for year, month in _month_range(start, end):
        cache_path = _month_cache_path(year, month)

        import datetime as _dt
        month_start = _dt.date(year, month, 1)

        # Idempotency check
        if cache_path.exists():
            log.info("usaspending_cache_hit", year=year, month=month, path=str(cache_path))
            df = pd.read_parquet(cache_path)
        else:
            # Compute ISO date bounds for the month
            last_day = monthrange(year, month)[1]
            start_date = f"{year:04d}-{month:02d}-01"
            end_date = f"{year:04d}-{month:02d}-{last_day:02d}"

            log.info("usaspending_fetch_month", year=year, month=month,
                     start_date=start_date, end_date=end_date)

            df = fetch_all_awards(NAICS_PREFIXES, start_date, end_date)

            # Persist raw cache
            df.to_parquet(cache_path, index=False)
            log.info("usaspending_cached", year=year, month=month,
                     rows=len(df), path=str(cache_path))

        if df.empty:
            log.info("usaspending_empty_month", year=year, month=month)
            continue

        # Backfill NULL award_dates: USASpending API often omits Action Date.
        # Fall back to the month's first day so signals can use the date.
        if "award_date" in df.columns:
            df["award_date"] = [
                (v if (v is not None and not (isinstance(v, float) and pd.isna(v)))
                 else month_start)
                for v in df["award_date"]
            ]

        df = df.drop_duplicates(subset=["award_id"])

        # Upsert to DB
        loaders.upsert_df(df, "usaspending_awards", ["award_id"])

        # Accumulate summary stats
        n = len(df)
        n_matched = int(df["recipient_ticker_guess"].notna().sum())
        n_unmatched_large = int(
            ((df["recipient_ticker_guess"].isna()) & (df["amount"].fillna(0) > 10_000_000)).sum()
        )

        total_awards += n
        total_matched += n_matched
        total_unmatched_large += n_unmatched_large

        log.info(
            "usaspending_month_complete",
            year=year,
            month=month,
            awards=n,
            matched=n_matched,
            unmatched_large=n_unmatched_large,
        )

    log.info(
        "usaspending_run_complete",
        start=start,
        end=end,
        total_awards=total_awards,
        total_matched=total_matched,
        total_unmatched_large=total_unmatched_large,
    )
