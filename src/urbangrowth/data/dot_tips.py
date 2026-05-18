"""State DOT Transportation Improvement Program (TIP) ingestion.

Scrapers for Texas (TxDOT UTP), California (Caltrans STIP/CTIPS), and
Arizona (ADOT STIP). All scrapers normalise raw data to the dot_tips
DB table schema and upsert via loaders.upsert_df.

Each scraper caches raw downloads by date; re-runs within the same day
skip the download. Partial success is supported: if one state scraper
fails the others continue and a warning is emitted.

Usage:
    python -m urbangrowth.data.dot_tips            # all states
    python -m urbangrowth.data.dot_tips TX,CA      # specific states
"""
from __future__ import annotations

import io
import json
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import structlog
from dotenv import load_dotenv
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from urbangrowth.config import data_path
from urbangrowth.db import loaders

load_dotenv()

log = structlog.get_logger(__name__)

_TODAY = date.today().isoformat()

# ---------------------------------------------------------------------------
# Project-type normalisation
# ---------------------------------------------------------------------------

# Evaluated in order: first match wins. More specific types precede generic ones.
_TYPE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("bridge", ["bridge", "culvert", "overpass", "underpass", "viaduct"]),
    ("transit", [
        "transit", "bus", "rail", "commuter", "pedestrian", "bike",
        "bicycle", "ped ", "light rail", "multimodal", "multi-modal",
    ]),
    ("safety", [
        "safety", "signal", "lighting", "guardrail", "pavement marking",
        "rumble strip", "intersection improvement", "railroad crossing",
    ]),
    ("highway", [
        "highway", "freeway", "interchange", "road", "route", "lane",
        "corridor", "pavement", "overlay", "widening", "resurfacing",
        "rehabilitation", "reconstruction", "construction",
    ]),
]


def normalize_project_type(raw: str) -> str:
    """Map a raw work-type string to highway | bridge | transit | safety | other.

    Matching is case-insensitive keyword search evaluated in the order:
    bridge → transit → safety → highway, so more specific types win.
    Returns "other" for empty/unrecognised values.
    """
    if not raw or not isinstance(raw, str):
        return "other"
    lower = raw.lower()
    for ptype, keywords in _TYPE_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return ptype
    return "other"


# ---------------------------------------------------------------------------
# Shared HTTP helper with tenacity retry
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; UrbanGrowthResearchPlatform/1.0; "
        "+https://github.com/urbangrowth)"
    )
}

_RETRY_KWARGS: dict[str, Any] = dict(
    retry=retry_if_exception_type((requests.RequestException, IOError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)


_SSL_SKIP_HOSTS = {"ftp.txdot.gov"}  # SSL cert chain fails on Windows for this host


@retry(**_RETRY_KWARGS)
def _http_get_with_retry(url: str, timeout: int = 120) -> requests.Response:
    from urllib.parse import urlparse
    host = urlparse(url).netloc
    verify = host not in _SSL_SKIP_HOSTS
    if not verify:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    resp = requests.get(url, headers=_HEADERS, timeout=timeout, verify=verify)
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Column resolution helper
# ---------------------------------------------------------------------------


def _find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """Return the first candidate column name that exists in *df* (case-insensitive)."""
    cols_lower = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand in df.columns:
            return cand
        if cand.lower() in cols_lower:
            return cols_lower[cand.lower()]
    return None


def _clean_money(series: pd.Series) -> pd.Series:
    """Strip currency symbols/commas and coerce to float."""
    return pd.to_numeric(
        series.astype(str).str.replace(r"[\$,]", "", regex=True),
        errors="coerce",
    ).round(2)


# ---------------------------------------------------------------------------
# Abstract base scraper
# ---------------------------------------------------------------------------


class DOTScraper(ABC):
    """Abstract base for state DOT TIP scrapers."""

    state: str  # "TX" | "CA" | "AZ"

    def __init__(self, dest_dir: Path) -> None:
        self.dest_dir = dest_dir
        self.dest_dir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def fetch(self) -> pd.DataFrame:
        """Return a DataFrame normalised to the dot_tips schema columns."""

    def _http_get(self, url: str, suffix: str = ".xlsx") -> Path:
        """Download *url* to a date-stamped cache file; skip if already exists.

        File name: {STATE}_tip_{today}{suffix}
        Returns the local Path.
        """
        fname = f"{self.state}_tip_{_TODAY}{suffix}"
        dest = self.dest_dir / fname
        if dest.exists():
            log.info("cache_hit", state=self.state, file=str(dest))
            return dest
        log.info("downloading", state=self.state, url=url)
        resp = _http_get_with_retry(url)
        dest.write_bytes(resp.content)
        log.info("saved", state=self.state, file=str(dest), bytes=len(resp.content))
        return dest

    @staticmethod
    def _base_columns() -> list[str]:
        return [
            "state", "project_id", "fiscal_year", "status",
            "project_type", "total_cost", "county", "awarded_date",
        ]


# ---------------------------------------------------------------------------
# Texas — TxDOT Unified Transportation Program (UTP)
# ---------------------------------------------------------------------------

_TXDOT_SOURCES = [
    # TxDOT FTP — official UTP Excel files (current + prior year fallback)
    "https://ftp.txdot.gov/pub/txdot/get-involved/tpp/utp/2026utp-searchable.xlsx",
    "https://ftp.txdot.gov/pub/txdot/get-involved/tpp/utp/2025UTP_Searchable.xlsx",
    "https://ftp.txdot.gov/pub/txdot/get-involved/tpp/utp/2024UTP_Searchable.xlsx",
    # Legacy ArcGIS Open Data direct CSV (may be stale)
    "https://opendata.arcgis.com/datasets/de9cb3e86a0e4b29b4d3b4c0c9b9c3a2_0.csv",
]

_TX_COL_MAP = {
    "project_id":  ["Project ID (CSJ)", "project_id", "proj_id", "PROJ_ID",
                    "projectid", "CSJ", "csj", "control_section_job"],
    "county":      ["County", "county", "COUNTY", "county_name", "dist_name"],
    "work_type":   ["UTP Action", "work_type", "WORK_TYPE", "work_program",
                    "project_type", "type_of_work", "wrktype"],
    "total_cost":  ["Est. Construction Cost", "total_cost", "TOTAL_COST",
                    "total_project_cost", "estimated_cost", "proj_cost"],
    "fiscal_year": ["Est. Let Date Range", "fiscal_year", "FISCAL_YEAR",
                    "fy", "FY", "let_fy", "funding_year"],
    "status":      ["UTP Action", "status", "STATUS", "proj_status",
                    "let_status", "construction_status"],
    "awarded_date": ["awarded_date", "let_date", "LET_DATE", "award_date",
                     "contract_date"],
}


def _parse_ckan_json(raw: bytes) -> pd.DataFrame:
    data = json.loads(raw)
    if not data.get("success"):
        raise ValueError("CKAN API returned success=false")
    return pd.DataFrame(data["result"]["records"])


def _parse_arcgis_json(raw: bytes) -> pd.DataFrame:
    data = json.loads(raw)
    features = data.get("features", [])
    if not features:
        raise ValueError("ArcGIS FeatureServer returned 0 features")
    return pd.DataFrame([f["attributes"] for f in features])


class TxDOTScraper(DOTScraper):
    """TxDOT Unified Transportation Program scraper (Texas)."""

    state = "TX"

    def fetch(self) -> pd.DataFrame:
        """Try each TxDOT UTP source in order; return the first successful parse."""
        last_exc: Exception | None = None

        for url in _TXDOT_SOURCES:
            try:
                log.info("txdot_try_source", url=url)
                resp = _http_get_with_retry(url)
                raw = resp.content
                content_type = resp.headers.get("Content-Type", "")

                if url.endswith(".xlsx") or url.endswith(".xls") or "spreadsheet" in content_type:
                    xl = pd.ExcelFile(io.BytesIO(raw), engine="openpyxl")
                    # Pick the first sheet whose name contains "project" or "UTP"
                    # (skips intro/metadata sheets)
                    data_sheet = next(
                        (s for s in xl.sheet_names
                         if any(kw in s.lower() for kw in ("project", "utp", "tip", "data"))),
                        xl.sheet_names[-1],
                    )
                    df = xl.parse(data_sheet)
                elif url.endswith(".csv") or "text/csv" in content_type:
                    df = pd.read_csv(io.BytesIO(raw), low_memory=False)
                elif "datastore_search" in url:
                    df = _parse_ckan_json(raw)
                elif "FeatureServer" in url:
                    df = _parse_arcgis_json(raw)
                else:
                    # Guess from content
                    try:
                        df = pd.read_csv(io.BytesIO(raw), low_memory=False)
                    except Exception:
                        try:
                            df = _parse_ckan_json(raw)
                        except Exception:
                            df = _parse_arcgis_json(raw)

                if df.empty:
                    log.warning("txdot_empty_response", url=url)
                    continue

                # Persist raw bytes for manual inspection
                if url.endswith(".xlsx") or url.endswith(".xls"):
                    suffix = ".xlsx"
                elif "FeatureServer" in url or "datastore_search" in url:
                    suffix = ".json"
                else:
                    suffix = ".csv"
                raw_path = self.dest_dir / f"{self.state}_tip_{_TODAY}_raw{suffix}"
                if not raw_path.exists():
                    raw_path.write_bytes(raw)

                return self._normalise(df)

            except Exception as exc:
                log.warning(
                    "txdot_source_failed",
                    url=url,
                    error=str(exc),
                    hint=(
                        "TxDOT UTP URL failed. "
                        "Check https://www.txdot.gov/inside-txdot/division/"
                        "transportation-planning/unified-transportation-program.html "
                        "for the current download URL."
                    ),
                )
                last_exc = exc

        raise RuntimeError(
            "All TxDOT UTP sources failed. "
            "Manual download: https://www.txdot.gov/inside-txdot/division/"
            "transportation-planning/unified-transportation-program.html"
        ) from last_exc

    def _normalise(self, df: pd.DataFrame) -> pd.DataFrame:
        pid_col = _find_col(df, _TX_COL_MAP["project_id"])
        if pid_col is None:
            log.warning("txdot_no_project_id_col", columns=list(df.columns))
            pid_col = df.columns[0]
        project_ids = df[pid_col].astype(str).str.strip()
        mask = project_ids.notna() & (project_ids != "") & (project_ids != "nan")
        df_filtered = df[mask].copy()

        out = pd.DataFrame(index=df_filtered.index)
        out["state"] = "TX"
        out["project_id"] = project_ids[mask].values

        fy_col = _find_col(df_filtered, _TX_COL_MAP["fiscal_year"])
        if fy_col:
            # Handle "FY 2026-2029" style strings — extract the first year
            raw_fy = df_filtered[fy_col].astype(str).str.extract(r"(\d{4})")[0]
            out["fiscal_year"] = pd.to_numeric(raw_fy, errors="coerce").astype("Int16").values
        else:
            out["fiscal_year"] = pd.array([pd.NA] * len(df_filtered), dtype="Int16")

        status_col = _find_col(df_filtered, _TX_COL_MAP["status"])
        out["status"] = (
            df_filtered[status_col].astype(str).str.strip().values
            if status_col else pd.NA
        )

        wt_col = _find_col(df_filtered, _TX_COL_MAP["work_type"])
        out["project_type"] = (
            df_filtered[wt_col].fillna("").apply(normalize_project_type).values
            if wt_col else "other"
        )

        cost_col = _find_col(df_filtered, _TX_COL_MAP["total_cost"])
        out["total_cost"] = (
            _clean_money(df_filtered[cost_col]).values if cost_col else pd.NA
        )

        county_col = _find_col(df_filtered, _TX_COL_MAP["county"])
        out["county"] = (
            df_filtered[county_col].astype(str).str.strip().str.title().values
            if county_col else pd.NA
        )

        aw_col = _find_col(df_filtered, _TX_COL_MAP["awarded_date"])
        out["awarded_date"] = (
            pd.to_datetime(df_filtered[aw_col], errors="coerce").dt.date.values
            if aw_col else None
        )

        out = out.drop_duplicates(subset=["state", "project_id"])
        log.info("txdot_normalised", rows=len(out))
        return out[self._base_columns()]


# ---------------------------------------------------------------------------
# California — Caltrans STIP / CTIPS
# ---------------------------------------------------------------------------

_CA_SOURCES = [
    # CA Open Data CKAN API (STIP dataset)
    "https://data.ca.gov/api/3/action/datastore_search?resource_id=stip&limit=10000",
    # Alternate CA Open Data slug
    "https://data.ca.gov/api/3/action/datastore_search?resource_id=ca-stip&limit=10000",
    # Caltrans CTIPS public query
    "https://ctips.dot.ca.gov/ctips/api/projects?format=json&limit=10000",
    # Direct Caltrans STIP Excel (URL changes per programming cycle)
    "https://dot.ca.gov/-/media/dot-media/programs/financial-programming/documents/stip/stip-a-tag.xlsx",
]

_CA_COL_MAP = {
    "project_id":   ["project_id", "proj_id", "ea", "EA", "expenditure_auth",
                     "project_number", "ppno", "PPNO"],
    "county":       ["county", "COUNTY", "county_name", "dist_co", "co_name"],
    "program_type": ["program_type", "project_type", "program", "category",
                     "work_description", "type_of_work", "work_type"],
    "total_cost":   ["total_cost", "total_project_cost", "programmed_amount",
                     "program_amount", "total_programmed", "amount"],
    "fiscal_year":  ["fiscal_year", "fy", "FY", "fiscal_yr", "program_year"],
    "status":       ["status", "STATUS", "project_status", "phase"],
    "awarded_date": ["awarded_date", "award_date", "let_date", "approval_date"],
}


class CaltransScraper(DOTScraper):
    """Caltrans STIP scraper (California)."""

    state = "CA"

    def fetch(self) -> pd.DataFrame:
        """Try each Caltrans/CA Open Data source; return the first successful parse."""
        last_exc: Exception | None = None

        for url in _CA_SOURCES:
            try:
                log.info("caltrans_try_source", url=url)
                resp = _http_get_with_retry(url)
                raw = resp.content
                content_type = resp.headers.get("Content-Type", "")

                if "api/3/action" in url or "json" in content_type or url.endswith(".json"):
                    data = json.loads(raw)
                    if isinstance(data, dict) and "result" in data:
                        if not data.get("success", True):
                            raise ValueError("CKAN API returned success=false")
                        df = pd.DataFrame(data["result"].get("records", []))
                    elif isinstance(data, list):
                        df = pd.DataFrame(data)
                    elif isinstance(data, dict) and "projects" in data:
                        df = pd.DataFrame(data["projects"])
                    else:
                        df = pd.json_normalize(data)
                elif url.endswith(".xlsx") or "spreadsheet" in content_type:
                    df = pd.read_excel(io.BytesIO(raw), engine="openpyxl")
                elif url.endswith(".csv") or "text/csv" in content_type:
                    df = pd.read_csv(io.BytesIO(raw), low_memory=False)
                else:
                    try:
                        df = pd.read_csv(io.BytesIO(raw), low_memory=False)
                    except Exception:
                        try:
                            df = pd.DataFrame(json.loads(raw))
                        except Exception:
                            df = pd.read_excel(io.BytesIO(raw), engine="openpyxl")

                if df.empty:
                    log.warning("caltrans_empty_response", url=url)
                    continue

                suffix = ".json" if "api" in url else ".xlsx"
                raw_path = self.dest_dir / f"{self.state}_tip_{_TODAY}_raw{suffix}"
                if not raw_path.exists():
                    raw_path.write_bytes(raw)

                return self._normalise(df)

            except Exception as exc:
                log.warning(
                    "caltrans_source_failed",
                    url=url,
                    error=str(exc),
                    hint=(
                        "Caltrans STIP URL failed. "
                        "Check https://dot.ca.gov/programs/financial-programming/stip "
                        "or https://data.ca.gov/dataset/stip for the current URL."
                    ),
                )
                last_exc = exc

        raise RuntimeError(
            "All Caltrans STIP sources failed. "
            "Manual download: https://dot.ca.gov/programs/financial-programming/stip"
        ) from last_exc

    def _normalise(self, df: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame()
        out["state"] = "CA"

        pid_col = _find_col(df, _CA_COL_MAP["project_id"])
        if pid_col is None:
            log.warning("caltrans_no_project_id_col", columns=list(df.columns))
            pid_col = df.columns[0]
        out["project_id"] = df[pid_col].astype(str).str.strip()
        mask = out["project_id"].notna() & (out["project_id"] != "") & (out["project_id"] != "nan")
        out = out[mask].copy()

        fy_col = _find_col(df, _CA_COL_MAP["fiscal_year"])
        if fy_col:
            # CA fiscal years often appear as "2024-25" — extract the start year
            fy_series = (
                df.loc[mask, fy_col].astype(str)
                .str.extract(r"(\d{4})", expand=False)
                .pipe(pd.to_numeric, errors="coerce")
                .astype("Int16")
            )
            out["fiscal_year"] = fy_series
        else:
            out["fiscal_year"] = pd.array([pd.NA] * mask.sum(), dtype="Int16")

        status_col = _find_col(df, _CA_COL_MAP["status"])
        out["status"] = (
            df.loc[mask, status_col].astype(str).str.strip()
            if status_col else pd.NA
        )

        pt_col = _find_col(df, _CA_COL_MAP["program_type"])
        out["project_type"] = (
            df.loc[mask, pt_col].fillna("").apply(normalize_project_type)
            if pt_col else "other"
        )

        cost_col = _find_col(df, _CA_COL_MAP["total_cost"])
        out["total_cost"] = (
            _clean_money(df.loc[mask, cost_col]) if cost_col else pd.NA
        )

        county_col = _find_col(df, _CA_COL_MAP["county"])
        out["county"] = (
            df.loc[mask, county_col].astype(str).str.strip().str.title()
            if county_col else pd.NA
        )

        aw_col = _find_col(df, _CA_COL_MAP["awarded_date"])
        out["awarded_date"] = (
            pd.to_datetime(df.loc[mask, aw_col], errors="coerce").dt.date
            if aw_col else None
        )

        out = out.drop_duplicates(subset=["state", "project_id"])
        log.info("caltrans_normalised", rows=len(out))
        return out[self._base_columns()]


# ---------------------------------------------------------------------------
# Arizona — ADOT STIP
# ---------------------------------------------------------------------------

_ADOT_STIP_TEMPLATE = (
    "https://azdot.gov/sites/default/files/media/files/adot-stip-{year}.xlsx"
)

_AZ_COL_MAP = {
    "project_id":    ["project_id", "proj_id", "tracs_no", "tracs", "project_no",
                      "project_number", "project_num", "TIP_ID", "tip_id"],
    "county":        ["county", "COUNTY", "county_name", "co_name"],
    "work_type":     ["work_type", "WORK_TYPE", "project_type", "type_of_work",
                      "description", "scope", "work_description"],
    "federal_share": ["federal_share", "federal", "fed_share", "federal_funds",
                      "federal_amount"],
    "state_share":   ["state_share", "state_funds", "state_amount", "non_federal"],
    "total_cost":    ["total_cost", "total", "total_project_cost", "project_cost",
                      "estimated_cost", "total_funds"],
    "fiscal_year":   ["fiscal_year", "fy", "FY", "fiscal_yr", "program_year", "year"],
    "status":        ["status", "STATUS", "project_status", "phase"],
    "awarded_date":  ["awarded_date", "award_date", "let_date", "approval_date"],
}


class ADOTScraper(DOTScraper):
    """ADOT STIP scraper (Arizona)."""

    state = "AZ"

    def fetch(self) -> pd.DataFrame:
        """Try current year, then previous year, then next year Excel URLs."""
        current_year = date.today().year
        years_to_try = [current_year, current_year - 1, current_year + 1]
        last_exc: Exception | None = None

        for year in years_to_try:
            url = _ADOT_STIP_TEMPLATE.format(year=year)
            try:
                log.info("adot_try_source", url=url, year=year)
                resp = _http_get_with_retry(url)
                raw = resp.content

                raw_path = self.dest_dir / f"{self.state}_tip_{_TODAY}_{year}.xlsx"
                if not raw_path.exists():
                    raw_path.write_bytes(raw)

                df = self._parse_adot_excel(raw)
                if df.empty:
                    log.warning("adot_empty_excel", year=year)
                    continue

                return self._normalise(df)

            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else "?"
                log.warning(
                    "adot_url_failed",
                    url=url,
                    status=code,
                    error=str(exc),
                    hint=(
                        f"ADOT STIP URL failed ({code}). "
                        "Check https://azdot.gov/programs/transportation-improvement-program "
                        "for the current URL."
                    ),
                )
                last_exc = exc
            except Exception as exc:
                log.warning(
                    "adot_source_error",
                    url=url,
                    error=str(exc),
                    hint=(
                        "ADOT STIP download failed. "
                        "Check https://azdot.gov/programs/transportation-improvement-program"
                    ),
                )
                last_exc = exc

        raise RuntimeError(
            "All ADOT STIP Excel URLs failed. "
            "Manual download: https://azdot.gov/programs/transportation-improvement-program"
        ) from last_exc

    def _parse_adot_excel(self, raw: bytes) -> pd.DataFrame:
        """Parse the ADOT STIP Excel workbook; prefers the data-richest sheet."""
        xl = pd.ExcelFile(io.BytesIO(raw), engine="openpyxl")
        best: pd.DataFrame = pd.DataFrame()

        for sheet in xl.sheet_names:
            try:
                df = xl.parse(sheet, header=0)
                if df.shape[1] < 3:
                    continue  # skip cover/notes sheets
                pid_col = _find_col(df, _AZ_COL_MAP["project_id"])
                if pid_col and len(df) > len(best):
                    best = df
            except Exception as exc:
                log.debug("adot_sheet_skip", sheet=sheet, error=str(exc))

        if best.empty and xl.sheet_names:
            best = xl.parse(xl.sheet_names[0], header=0)

        return best

    def _normalise(self, df: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame()
        out["state"] = "AZ"

        pid_col = _find_col(df, _AZ_COL_MAP["project_id"])
        if pid_col is None:
            log.warning("adot_no_project_id_col", columns=list(df.columns))
            pid_col = df.columns[0]
        out["project_id"] = df[pid_col].astype(str).str.strip()
        mask = out["project_id"].notna() & (out["project_id"] != "") & (out["project_id"] != "nan")
        out = out[mask].copy()

        fy_col = _find_col(df, _AZ_COL_MAP["fiscal_year"])
        out["fiscal_year"] = (
            pd.to_numeric(df.loc[mask, fy_col], errors="coerce").astype("Int16")
            if fy_col else pd.array([pd.NA] * mask.sum(), dtype="Int16")
        )

        status_col = _find_col(df, _AZ_COL_MAP["status"])
        out["status"] = (
            df.loc[mask, status_col].astype(str).str.strip()
            if status_col else pd.NA
        )

        wt_col = _find_col(df, _AZ_COL_MAP["work_type"])
        out["project_type"] = (
            df.loc[mask, wt_col].fillna("").apply(normalize_project_type)
            if wt_col else "other"
        )

        # Prefer explicit total_cost; fall back to federal + state shares
        cost_col = _find_col(df, _AZ_COL_MAP["total_cost"])
        if cost_col:
            out["total_cost"] = _clean_money(df.loc[mask, cost_col])
        else:
            fed_col = _find_col(df, _AZ_COL_MAP["federal_share"])
            st_col = _find_col(df, _AZ_COL_MAP["state_share"])
            if fed_col and st_col:
                fed = _clean_money(df.loc[mask, fed_col]).fillna(0)
                st = _clean_money(df.loc[mask, st_col]).fillna(0)
                out["total_cost"] = (fed + st).round(2)
            else:
                out["total_cost"] = pd.NA

        county_col = _find_col(df, _AZ_COL_MAP["county"])
        out["county"] = (
            df.loc[mask, county_col].astype(str).str.strip().str.title()
            if county_col else pd.NA
        )

        aw_col = _find_col(df, _AZ_COL_MAP["awarded_date"])
        out["awarded_date"] = (
            pd.to_datetime(df.loc[mask, aw_col], errors="coerce").dt.date
            if aw_col else None
        )

        out = out.drop_duplicates(subset=["state", "project_id"])
        log.info("adot_normalised", rows=len(out))
        return out[self._base_columns()]


# ---------------------------------------------------------------------------
# Registry and CLI entry point
# ---------------------------------------------------------------------------

_SCRAPERS: dict[str, type[DOTScraper]] = {
    "TX": TxDOTScraper,
    "CA": CaltransScraper,
    "AZ": ADOTScraper,
}


def run(states: str = "TX,CA,AZ") -> None:
    """CLI entry point for the DOT TIP ingestion pipeline.

    Parameters
    ----------
    states:
        Comma-separated state codes to scrape (default: "TX,CA,AZ").

    For each requested state:
    - Checks for a date-stamped parquet at raw/dot_tips/{state}/tip_{today}.parquet.
    - If absent, runs the scraper and writes the parquet.
    - Upserts into the dot_tips DB table on (state, project_id).
    - On failure, logs a warning and continues to the next state.
    """
    requested = [s.strip().upper() for s in states.split(",") if s.strip()]
    unknown = [s for s in requested if s not in _SCRAPERS]
    if unknown:
        log.warning("unknown_states", states=unknown, valid=list(_SCRAPERS))

    results: dict[str, int] = {}
    errors: dict[str, str] = {}

    for state_code in requested:
        if state_code not in _SCRAPERS:
            continue

        cache_dir = data_path("raw/dot_tips", state_code.lower())
        parquet_path = cache_dir / f"tip_{_TODAY}.parquet"

        try:
            if parquet_path.exists():
                log.info("cache_hit_parquet", state=state_code, file=str(parquet_path))
                df = pd.read_parquet(parquet_path)
            else:
                scraper = _SCRAPERS[state_code](dest_dir=cache_dir)
                log.info("scraper_start", state=state_code)
                df = scraper.fetch()
                df.to_parquet(parquet_path, index=False)
                log.info(
                    "parquet_saved",
                    state=state_code,
                    file=str(parquet_path),
                    rows=len(df),
                )

            n = loaders.upsert_df(df, "dot_tips", ["state", "project_id"])
            results[state_code] = n
            log.info("state_complete", state=state_code, upserted=n)

        except Exception as exc:
            errors[state_code] = str(exc)
            log.warning(
                "state_scraper_failed",
                state=state_code,
                error=str(exc),
                hint=(
                    f"Scraper for {state_code} failed — continuing with remaining states. "
                    "Check network connectivity and the source URLs above."
                ),
            )

    log.info(
        "dot_tips_run_complete",
        succeeded=list(results.keys()),
        failed=list(errors.keys()),
        total_upserted=sum(results.values()),
    )
    if errors:
        log.warning("dot_tips_partial_failure", errors=errors)


if __name__ == "__main__":
    import sys

    _states_arg = sys.argv[1] if len(sys.argv) > 1 else "TX,CA,AZ"
    run(_states_arg)
