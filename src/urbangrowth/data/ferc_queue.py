"""FERC Interconnection Queue ingestion — all 7 major ISOs.

Downloads each ISO's public interconnection queue file (Excel/CSV), normalises
the diverse column layouts to a common schema, and upserts a monthly snapshot
into the ferc_queue PostgreSQL table.

Supported ISOs: PJM, MISO, ERCOT, CAISO, SPP, NYISO, ISO-NE.

Usage (CLI):
    python -m urbangrowth.data.ferc_queue
    python -m urbangrowth.data.ferc_queue --snapshot-date 2025-04-01
"""
from __future__ import annotations

import argparse
from abc import ABC, abstractmethod
from datetime import date, datetime
from pathlib import Path
from typing import Optional

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

# ---------------------------------------------------------------------------
# Normalisation maps
# ---------------------------------------------------------------------------

FUEL_MAP: dict[str, str] = {
    "solar": "solar",
    "pv": "solar",
    "photovoltaic": "solar",
    "wind": "wind",
    "offshore wind": "wind_offshore",
    "off-shore wind": "wind_offshore",
    "battery": "battery",
    "storage": "battery",
    "bess": "battery",
    "energy storage": "battery",
    "natural gas": "natural_gas",
    "gas": "natural_gas",
    "ng": "natural_gas",
    "combined cycle": "natural_gas",
    "combustion turbine": "natural_gas",
    "nuclear": "nuclear",
    "hydro": "hydro",
    "hydroelectric": "hydro",
    "coal": "coal",
    "biomass": "biomass",
    "geothermal": "geothermal",
}

STATUS_MAP: dict[str, str] = {
    "active": "active",
    "in queue": "active",
    "under study": "active",
    "suspended": "suspended",
    "withdrawn": "withdrawn",
    "cancelled": "withdrawn",
    "in service": "operational",
    "operational": "operational",
    "commercial": "operational",
    "approved": "operational",
}

# Common output columns (maps 1-to-1 with ferc_queue table + iso column)
_OUTPUT_COLS = [
    "snapshot_date",
    "project_id",
    "iso",
    "region",        # alias of iso kept for backward-compat with signal queries
    "project_name",
    "state",
    "county",
    "fuel_type",
    "mw_capacity",
    "status",
    "queue_date",
    "expected_inservice_date",
    "withdrawn",
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; UrbanGrowthResearchBot/1.0; "
        "+https://github.com/urbangrowth/research)"
    )
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_col(
    columns: list[str],
    keywords: list[str],
    exclude: Optional[list[str]] = None,
) -> Optional[str]:
    """Return the first column whose lowercased name contains any keyword.

    Optionally skip columns whose name also contains any of *exclude*.
    """
    exclude = [e.lower() for e in (exclude or [])]
    for col in columns:
        lower = col.lower()
        if any(kw.lower() in lower for kw in keywords):
            if not any(ex in lower for ex in exclude):
                return col
    return None


def _normalise_fuel(raw: object) -> str:
    """Map a raw fuel/technology string to the canonical FUEL_MAP value."""
    if pd.isna(raw) or str(raw).strip() == "":
        return "other"
    text = str(raw).lower().strip()
    # Longest-match first (so "offshore wind" beats "wind")
    for key in sorted(FUEL_MAP, key=len, reverse=True):
        if key in text:
            return FUEL_MAP[key]
    return "other"


def _normalise_status(raw: object) -> str:
    """Map a raw status string to the canonical STATUS_MAP value."""
    if pd.isna(raw) or str(raw).strip() == "":
        return "unknown"
    text = str(raw).lower().strip()
    for key in sorted(STATUS_MAP, key=len, reverse=True):
        if key in text:
            return STATUS_MAP[key]
    return "unknown"


def _safe_date(val: object) -> Optional[date]:
    """Coerce various date representations to a Python date, or None."""
    if pd.isna(val) if not isinstance(val, (list, dict)) else False:
        return None
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.date()
    if isinstance(val, date):
        return val
    text = str(val).strip()
    if not text or text.lower() in ("nan", "nat", "none", "n/a", "tbd", "-"):
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",  # CAISO: "2003-11-18 08:00:00"
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y/%m/%d",
        "%d-%b-%Y",
        "%b-%Y",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _safe_float(val: object) -> Optional[float]:
    """Coerce to float, returning None on failure."""
    if pd.isna(val) if not isinstance(val, (list, dict)) else False:
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


_SSL_SKIP_HOSTS: set[str] = {"www.spp.org", "spp.org"}


def _make_session(verify: bool = True) -> requests.Session:
    """Build a requests Session; for legacy-TLS hosts use a permissive adapter."""
    session = requests.Session()
    if not verify:
        import ssl
        import urllib3
        from requests.adapters import HTTPAdapter
        from urllib3.util.ssl_ import create_urllib3_context

        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        ctx.options |= 0x4  # OP_LEGACY_SERVER_CONNECT

        class _LegacyAdapter(HTTPAdapter):
            def init_poolmanager(self, *args, **kwargs):
                kwargs["ssl_context"] = ctx
                super().init_poolmanager(*args, **kwargs)

        session.mount("https://", _LegacyAdapter())
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _get(url: str, stream: bool = False) -> requests.Response:
    """GET with retry logic and standard headers."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    verify = host not in _SSL_SKIP_HOSTS
    session = _make_session(verify=verify)
    resp = session.get(url, headers=_HEADERS, timeout=120, stream=stream, verify=verify)
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Abstract base scraper
# ---------------------------------------------------------------------------


class ISOScraper(ABC):
    """Base class for all ISO interconnection queue scrapers."""

    iso_name: str  # must be set by each subclass

    def __init__(self, snapshot_date: date, dest_dir: Path) -> None:
        self.snapshot_date = snapshot_date
        self.dest_dir = dest_dir

    @abstractmethod
    def get_download_url(self) -> str:
        """Return the URL of the queue file to download."""
        ...

    @abstractmethod
    def parse(self, path: Path) -> pd.DataFrame:
        """Parse the downloaded file and return a DataFrame with _OUTPUT_COLS."""
        ...

    # ------------------------------------------------------------------
    # Concrete helpers
    # ------------------------------------------------------------------

    def _cache_path(self, ext: str = "xlsx") -> Path:
        return self.dest_dir / f"{self.iso_name}_{self.snapshot_date}.{ext}"

    def download(self) -> Path:
        """Download the queue file; use cache if already present."""
        url = self.get_download_url()
        # Infer extension from URL
        ext = "xlsx"
        url_lower = url.split("?")[0].lower()
        if url_lower.endswith(".csv"):
            ext = "csv"
        elif url_lower.endswith(".zip"):
            ext = "zip"
        elif url_lower.endswith(".xls"):
            ext = "xls"

        dest = self._cache_path(ext)
        if dest.exists():
            log.info("cache_hit", iso=self.iso_name, path=str(dest))
            return dest

        log.info("downloading", iso=self.iso_name, url=url)
        try:
            resp = _get(url)
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                log.warning(
                    "download_404",
                    iso=self.iso_name,
                    url=url,
                    hint="URL may be stale — check ISO website directly",
                )
            else:
                log.error("download_error", iso=self.iso_name, url=url, error=str(exc))
            raise
        except requests.exceptions.RequestException as exc:
            log.error("download_error", iso=self.iso_name, url=url, error=str(exc))
            raise

        dest.write_bytes(resp.content)
        log.info("downloaded", iso=self.iso_name, bytes=len(resp.content), path=str(dest))
        return dest

    def fetch(self) -> pd.DataFrame:
        """Full pipeline: download → parse → normalise fuel/status."""
        path = self.download()
        df = self.parse(path)
        if df.empty:
            return df
        df["fuel_type"] = df["fuel_type"].apply(_normalise_fuel)
        df["status"] = df["status"].apply(_normalise_status)
        df["withdrawn"] = df["status"] == "withdrawn"
        # Ensure all output columns exist
        for col in _OUTPUT_COLS:
            if col not in df.columns:
                df[col] = None
        return df[_OUTPUT_COLS]

    # ------------------------------------------------------------------
    # Shared DataFrame builder
    # ------------------------------------------------------------------

    def _build_row(
        self,
        project_id: object,
        project_name: object,
        state: object,
        county: object,
        fuel_type: object,
        mw_capacity: object,
        status: object,
        queue_date: object,
        expected_inservice_date: object,
    ) -> dict:
        return {
            "snapshot_date": self.snapshot_date,
            "project_id": f"{self.iso_name}_{str(project_id).strip()}",
            "iso": self.iso_name,
            "region": self.iso_name,   # kept for backward-compat with signal queries
            "project_name": str(project_name).strip() if pd.notna(project_name) else "",
            "state": str(state).strip().upper()[:2] if pd.notna(state) else "",
            "county": str(county).strip() if pd.notna(county) else "",
            "fuel_type": fuel_type,
            "mw_capacity": _safe_float(mw_capacity),
            "status": status,
            "queue_date": _safe_date(queue_date),
            "expected_inservice_date": _safe_date(expected_inservice_date),
            "withdrawn": False,  # overwritten in fetch()
        }


# ---------------------------------------------------------------------------
# ISO implementations
# ---------------------------------------------------------------------------


class PJMScraper(ISOScraper):
    """PJM — https://www.pjm.com/planning/project-connect"""

    iso_name = "pjm"

    def get_download_url(self) -> str:
        return "https://pjm.com/pub/planning/iq_queues/xcl_queue.xls"

    def parse(self, path: Path) -> pd.DataFrame:
        # PJM publishes a multi-sheet workbook; find the "queue" sheet
        engine = "xlrd" if path.suffix.lower() == ".xls" else "openpyxl"
        xl = pd.ExcelFile(path, engine=engine)
        sheet = next(
            (s for s in xl.sheet_names if "queue" in s.lower()),
            xl.sheet_names[0],
        )
        log.debug("pjm_sheet", sheet=sheet)

        # PJM often has a header row several rows in — find it
        raw = pd.read_excel(xl, sheet_name=sheet, header=None, dtype=str)
        # Locate the header row: the row with the most non-null, non-empty values
        header_row = int(raw.notna().sum(axis=1).idxmax())
        df = pd.read_excel(xl, sheet_name=sheet, header=header_row, dtype=str)
        df.columns = [str(c).strip() for c in df.columns]

        cols = list(df.columns)
        id_col = _find_col(cols, ["queue position", "queue pos", "project id", "queue #", "inris"])
        name_col = _find_col(cols, ["project name", "name"])
        state_col = _find_col(cols, ["state"], exclude=["county", "substati"])
        county_col = _find_col(cols, ["county"])
        mw_col = _find_col(cols, ["mw", "capacity", "size"], exclude=["proposed", "current"])
        fuel_col = _find_col(cols, ["fuel", "type", "technology", "resource"])
        status_col = _find_col(cols, ["status"])
        qdate_col = _find_col(cols, ["queue date", "application date", "entered", "received"])
        inservice_col = _find_col(cols, ["in-service", "in service", "service date", "cod", "completion"])

        rows = []
        for _, row in df.iterrows():
            pid = row[id_col] if id_col else ""
            if not pid or str(pid).strip() in ("", "nan"):
                continue
            rows.append(
                self._build_row(
                    project_id=pid,
                    project_name=row[name_col] if name_col else "",
                    state=row[state_col] if state_col else "",
                    county=row[county_col] if county_col else "",
                    fuel_type=row[fuel_col] if fuel_col else "",
                    mw_capacity=row[mw_col] if mw_col else None,
                    status=row[status_col] if status_col else "",
                    queue_date=row[qdate_col] if qdate_col else None,
                    expected_inservice_date=row[inservice_col] if inservice_col else None,
                )
            )
        return pd.DataFrame(rows)


class MISOScraper(ISOScraper):
    """MISO — https://www.misoenergy.org/planning/generator-interconnection"""

    iso_name = "miso"

    def get_download_url(self) -> str:
        return (
            "https://www.misoenergy.org/siteassets/planningbps/"
            "generator-interconnection/active-generator-interconnection-queue.xlsx"
        )

    def parse(self, path: Path) -> pd.DataFrame:
        df = pd.read_excel(path, dtype=str, engine="openpyxl")
        df.columns = [str(c).strip() for c in df.columns]
        # Drop fully-empty rows
        df = df.dropna(how="all")

        cols = list(df.columns)
        id_col = _find_col(cols, ["project no", "project number", "queue no", "id"])
        name_col = _find_col(cols, ["interconnect name", "project name", "name"])
        state_col = _find_col(cols, ["state"])
        county_col = _find_col(cols, ["county"])
        mw_col = _find_col(cols, ["mw", "capacity"])
        fuel_col = _find_col(cols, ["technology", "fuel", "type", "resource"])
        status_col = _find_col(cols, ["status"])
        qdate_col = _find_col(cols, ["queue date", "application date", "date entered", "entered"])
        inservice_col = _find_col(cols, ["in-service", "in service", "cod", "service date"])

        rows = []
        for _, row in df.iterrows():
            pid = row[id_col] if id_col else ""
            if not pid or str(pid).strip() in ("", "nan"):
                continue
            rows.append(
                self._build_row(
                    project_id=pid,
                    project_name=row[name_col] if name_col else "",
                    state=row[state_col] if state_col else "",
                    county=row[county_col] if county_col else "",
                    fuel_type=row[fuel_col] if fuel_col else "",
                    mw_capacity=row[mw_col] if mw_col else None,
                    status=row[status_col] if status_col else "",
                    queue_date=row[qdate_col] if qdate_col else None,
                    expected_inservice_date=row[inservice_col] if inservice_col else None,
                )
            )
        return pd.DataFrame(rows)


class ERCOTScraper(ISOScraper):
    """ERCOT — GIS Report (date-stamped URL, tries current month then previous)."""

    iso_name = "ercot"

    def _month_url(self, dt: datetime) -> str:
        return (
            f"https://www.ercot.com/files/docs/"
            f"{dt.year}/{dt.month:02d}/01/"
            f"GIS_Report_{dt.strftime('%B_%Y')}.xlsx"
        )

    def get_download_url(self) -> str:
        return self._month_url(datetime.now())

    def download(self) -> Path:
        """Override: fall back to previous month on 404."""
        now = datetime.now()
        for delta in (0, -1, -2):
            # compute target month
            month_offset = now.month + delta
            year = now.year
            while month_offset < 1:
                month_offset += 12
                year -= 1
            target = now.replace(year=year, month=month_offset, day=1)
            url = self._month_url(target)
            ext = "xlsx"
            dest = self.dest_dir / f"{self.iso_name}_{self.snapshot_date}.{ext}"
            if dest.exists():
                log.info("cache_hit", iso=self.iso_name, path=str(dest))
                return dest
            log.info("downloading", iso=self.iso_name, url=url)
            try:
                resp = _get(url)
                dest.write_bytes(resp.content)
                log.info("downloaded", iso=self.iso_name, bytes=len(resp.content), path=str(dest))
                return dest
            except requests.exceptions.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    log.warning("download_404", iso=self.iso_name, url=url, hint="Trying previous month")
                    continue
                raise
        raise FileNotFoundError(
            f"ERCOT GIS report not found for current or previous 2 months. "
            "Check https://www.ercot.com/gridinfo/resource directly."
        )

    def parse(self, path: Path) -> pd.DataFrame:
        df = pd.read_excel(path, dtype=str, engine="openpyxl")
        df.columns = [str(c).strip() for c in df.columns]
        df = df.dropna(how="all")

        cols = list(df.columns)
        id_col = _find_col(cols, ["inr", "gen no", "project no", "id", "number"])
        name_col = _find_col(cols, ["project name", "name"])
        state_col = _find_col(cols, ["state"])
        county_col = _find_col(cols, ["county"])
        mw_col = _find_col(cols, ["total mw", "approved mw", "mw", "capacity"])
        fuel_col = _find_col(cols, ["fuel type", "fuel", "technology", "resource"])
        status_col = _find_col(cols, ["status"])
        qdate_col = _find_col(cols, ["queue application date", "application date", "queue date", "entered"])
        inservice_col = _find_col(cols, ["in-service", "in service", "cod", "commercial"])

        rows = []
        for _, row in df.iterrows():
            pid = row[id_col] if id_col else ""
            if not pid or str(pid).strip() in ("", "nan"):
                continue
            rows.append(
                self._build_row(
                    project_id=pid,
                    project_name=row[name_col] if name_col else "",
                    state="TX",  # ERCOT is Texas-only
                    county=row[county_col] if county_col else "",
                    fuel_type=row[fuel_col] if fuel_col else "",
                    mw_capacity=row[mw_col] if mw_col else None,
                    status=row[status_col] if status_col else "",
                    queue_date=row[qdate_col] if qdate_col else None,
                    expected_inservice_date=row[inservice_col] if inservice_col else None,
                )
            )
        return pd.DataFrame(rows)


class CAISOScraper(ISOScraper):
    """CAISO — Generator Interconnection and Interoperability Queue."""

    iso_name = "caiso"

    def get_download_url(self) -> str:
        # URL updated 2026-05; old path 404'd after CAISO website restructure
        return "https://www.caiso.com/documents/publicqueuereport.xlsx"

    def parse(self, path: Path) -> pd.DataFrame:
        # CAISO workbook: "Grid GenerationQueue" sheet; header is on row 3 (0-indexed)
        xl = pd.ExcelFile(path, engine="openpyxl")
        sheet = xl.sheet_names[0]
        for s in xl.sheet_names:
            if "queue" in s.lower():
                sheet = s
                break

        # header=3 skips the 3 preamble/merged-header rows above the real column row
        df = pd.read_excel(xl, sheet_name=sheet, header=3, dtype=str)
        df.columns = [str(c).strip().replace("\n", " ") for c in df.columns]
        df = df.dropna(how="all")

        cols = list(df.columns)
        id_col   = _find_col(cols, ["queue position", "application no", "queue no", "project id"])
        name_col = _find_col(cols, ["project name", "applicant name", "name"])
        state_col   = _find_col(cols, ["state"])
        county_col  = _find_col(cols, ["county", "jurisdiction"])
        mw_col      = _find_col(cols, ["net mws to grid", "net mw", "mw-1", "mw", "capacity"])
        fuel_col    = _find_col(cols, ["fuel-1", "type-1", "technology", "fuel", "resource"])
        status_col  = _find_col(cols, ["application status", "status"])
        qdate_col   = _find_col(cols, ["queue date", "application date", "received", "entered"])
        inservice_col = _find_col(cols, ["current  on-line date", "current on-line date",
                                          "proposed  on-line date", "proposed on-line date",
                                          "in-service", "in service", "on-line", "cod"])

        rows = []
        for _, row in df.iterrows():
            pid = row[id_col] if id_col else ""
            if not pid or str(pid).strip() in ("", "nan"):
                continue
            # CAISO is primarily CA
            state = row[state_col] if state_col else "CA"
            rows.append(
                self._build_row(
                    project_id=pid,
                    project_name=row[name_col] if name_col else "",
                    state=state,
                    county=row[county_col] if county_col else "",
                    fuel_type=row[fuel_col] if fuel_col else "",
                    mw_capacity=row[mw_col] if mw_col else None,
                    status=row[status_col] if status_col else "",
                    queue_date=row[qdate_col] if qdate_col else None,
                    expected_inservice_date=row[inservice_col] if inservice_col else None,
                )
            )
        return pd.DataFrame(rows)


class SPPScraper(ISOScraper):
    """SPP — Generator Interconnection Status Report."""

    iso_name = "spp"

    def get_download_url(self) -> str:
        return (
            "https://www.spp.org/documents/36282/"
            "generator%20interconnection%20status%20report.xlsx"
        )

    def parse(self, path: Path) -> pd.DataFrame:
        df = pd.read_excel(path, dtype=str, engine="openpyxl")
        df.columns = [str(c).strip() for c in df.columns]
        df = df.dropna(how="all")

        cols = list(df.columns)
        id_col = _find_col(cols, ["queue id", "project id", "id", "number"])
        name_col = _find_col(cols, ["project name", "name"])
        state_col = _find_col(cols, ["state"])
        county_col = _find_col(cols, ["county"])
        mw_col = _find_col(cols, ["mw capacity", "mw", "capacity"])
        fuel_col = _find_col(cols, ["fuel type", "fuel", "technology", "type"])
        status_col = _find_col(cols, ["status"])
        qdate_col = _find_col(cols, ["date entered", "queue date", "application date", "entered"])
        inservice_col = _find_col(cols, ["in-service", "in service", "cod", "service date"])

        rows = []
        for _, row in df.iterrows():
            pid = row[id_col] if id_col else ""
            if not pid or str(pid).strip() in ("", "nan"):
                continue
            rows.append(
                self._build_row(
                    project_id=pid,
                    project_name=row[name_col] if name_col else "",
                    state=row[state_col] if state_col else "",
                    county=row[county_col] if county_col else "",
                    fuel_type=row[fuel_col] if fuel_col else "",
                    mw_capacity=row[mw_col] if mw_col else None,
                    status=row[status_col] if status_col else "",
                    queue_date=row[qdate_col] if qdate_col else None,
                    expected_inservice_date=row[inservice_col] if inservice_col else None,
                )
            )
        return pd.DataFrame(rows)


class NYISOScraper(ISOScraper):
    """NYISO — Interconnection Queue (dynamically resolved from the interconnections page)."""

    iso_name = "nyiso"
    _INDEX_URL = "https://www.nyiso.com/interconnections"
    _FOLDER_ID = "1407078"

    def get_download_url(self) -> str:
        import re as _re
        try:
            resp = requests.get(
                self._INDEX_URL,
                headers=_HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            m = _re.search(
                rf"/documents/\d+/{self._FOLDER_ID}/NYISO-Interconnection-Queue[^\"'\s]+\.xlsx[^\"'\s]*",
                resp.text,
            )
            if m:
                url = "https://www.nyiso.com" + m.group(0)
                log.info("nyiso_queue_url_found", url=url)
                return url
        except Exception as exc:
            log.warning("nyiso_index_fetch_failed", error=str(exc))
        # Fallback to last-known URL
        return (
            "https://www.nyiso.com/documents/20142/1407078/"
            "NYISO-Interconnection-Queue-03-31-2026.xlsx/"
            "ff0e2005-e8d3-e75d-3e81-fa7027a52685?t=1776108425656"
        )

    def parse(self, path: Path) -> pd.DataFrame:
        xl = pd.ExcelFile(path, engine="openpyxl")
        sheet = xl.sheet_names[0]
        for s in xl.sheet_names:
            if "queue" in s.lower():
                sheet = s
                break

        df = pd.read_excel(xl, sheet_name=sheet, dtype=str)
        df.columns = [str(c).strip() for c in df.columns]
        df = df.dropna(how="all")

        cols = list(df.columns)
        id_col = _find_col(cols, ["queue pos", "queue id", "case no", "project id", "id"])
        name_col = _find_col(cols, ["project name", "name"])
        state_col = _find_col(cols, ["state"])
        # NYISO uses zone rather than county
        county_col = _find_col(cols, ["zone", "county"])
        mw_col = _find_col(cols, ["sp (mw)", "wp (mw)", "mw", "capacity"])
        fuel_col = _find_col(cols, ["type/ fuel", "type", "fuel", "technology", "resource"])
        status_col = _find_col(cols, ["status", "s"])
        qdate_col = _find_col(cols, ["date of ir", "application date", "queue date", "received", "entered"])
        inservice_col = _find_col(cols, ["proposed in-service", "proposed cod", "in-service", "in service", "cod", "proposed"])

        rows = []
        for _, row in df.iterrows():
            pid = row[id_col] if id_col else ""
            if not pid or str(pid).strip() in ("", "nan"):
                continue
            state = row[state_col] if state_col else "NY"
            rows.append(
                self._build_row(
                    project_id=pid,
                    project_name=row[name_col] if name_col else "",
                    state=state,
                    county=row[county_col] if county_col else "",
                    fuel_type=row[fuel_col] if fuel_col else "",
                    mw_capacity=row[mw_col] if mw_col else None,
                    status=row[status_col] if status_col else "",
                    queue_date=row[qdate_col] if qdate_col else None,
                    expected_inservice_date=row[inservice_col] if inservice_col else None,
                )
            )
        return pd.DataFrame(rows)


class ISONEScraper(ISOScraper):
    """ISO-NE — Queue Status Report (dated URL; falls back gracefully)."""

    iso_name = "isone"

    def get_download_url(self) -> str:
        # ISO-NE uses dated URLs; this may need updating periodically.
        # The code handles 404 gracefully and logs a hint.
        now = datetime.now()
        year = now.year
        month = now.month
        return (
            f"https://www.iso-ne.com/static-assets/documents/"
            f"{year}/{month:02d}/"
            f"{year}_{month:02d}_queue_status_report.xlsx"
        )

    def download(self) -> Path:
        """Try current and recent months, fall back gracefully."""
        now = datetime.now()
        for delta in range(0, 6):
            month_offset = now.month - delta
            year = now.year
            while month_offset < 1:
                month_offset += 12
                year -= 1
            url = (
                f"https://www.iso-ne.com/static-assets/documents/"
                f"{year}/{month_offset:02d}/"
                f"{year}_{month_offset:02d}_queue_status_report.xlsx"
            )
            dest = self.dest_dir / f"{self.iso_name}_{self.snapshot_date}.xlsx"
            if dest.exists():
                log.info("cache_hit", iso=self.iso_name, path=str(dest))
                return dest
            log.info("downloading", iso=self.iso_name, url=url)
            try:
                resp = _get(url)
                dest.write_bytes(resp.content)
                log.info("downloaded", iso=self.iso_name, bytes=len(resp.content), path=str(dest))
                return dest
            except requests.exceptions.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    log.warning(
                        "download_404",
                        iso=self.iso_name,
                        url=url,
                        hint="Trying previous month",
                    )
                    continue
                log.warning(
                    "download_error",
                    iso=self.iso_name,
                    url=url,
                    error=str(exc),
                    hint=(
                        "ISO-NE URL may be stale — check "
                        "https://www.iso-ne.com/isoexpress/web/reports/operations directly"
                    ),
                )
                raise
        raise FileNotFoundError(
            "ISO-NE queue report not found for the past 6 months. "
            "Check https://www.iso-ne.com/isoexpress/ directly."
        )

    def parse(self, path: Path) -> pd.DataFrame:
        xl = pd.ExcelFile(path, engine="openpyxl")
        sheet = xl.sheet_names[0]
        for s in xl.sheet_names:
            if "queue" in s.lower():
                sheet = s
                break

        df = pd.read_excel(xl, sheet_name=sheet, dtype=str)
        df.columns = [str(c).strip() for c in df.columns]
        df = df.dropna(how="all")

        cols = list(df.columns)
        id_col = _find_col(cols, ["queue id", "project no", "id", "number"])
        name_col = _find_col(cols, ["project name", "name"])
        state_col = _find_col(cols, ["state"])
        county_col = _find_col(cols, ["county"])
        mw_col = _find_col(cols, ["mw", "capacity"])
        fuel_col = _find_col(cols, ["technology", "fuel", "type", "resource"])
        status_col = _find_col(cols, ["status"])
        qdate_col = _find_col(cols, ["application date", "queue date", "received", "entered"])
        inservice_col = _find_col(cols, ["in-service", "in service", "cod", "commercial"])

        rows = []
        for _, row in df.iterrows():
            pid = row[id_col] if id_col else ""
            if not pid or str(pid).strip() in ("", "nan"):
                continue
            rows.append(
                self._build_row(
                    project_id=pid,
                    project_name=row[name_col] if name_col else "",
                    state=row[state_col] if state_col else "",
                    county=row[county_col] if county_col else "",
                    fuel_type=row[fuel_col] if fuel_col else "",
                    mw_capacity=row[mw_col] if mw_col else None,
                    status=row[status_col] if status_col else "",
                    queue_date=row[qdate_col] if qdate_col else None,
                    expected_inservice_date=row[inservice_col] if inservice_col else None,
                )
            )
        return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Scraper registry
# ---------------------------------------------------------------------------

_SCRAPERS: list[type[ISOScraper]] = [
    PJMScraper,
    MISOScraper,
    ERCOTScraper,
    CAISOScraper,
    SPPScraper,
    NYISOScraper,
    ISONEScraper,
]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def snapshot_all_isos(snapshot_date: date, dest_dir: Path) -> pd.DataFrame:
    """Run all 7 ISO scrapers, concatenate results.

    Each ISO is wrapped in try/except so a single failure does not abort the
    entire run.  Per-ISO success/failure is logged.
    """
    frames: list[pd.DataFrame] = []
    dest_dir.mkdir(parents=True, exist_ok=True)

    for scraper_cls in _SCRAPERS:
        scraper = scraper_cls(snapshot_date=snapshot_date, dest_dir=dest_dir)
        try:
            df = scraper.fetch()
            log.info(
                "iso_success",
                iso=scraper.iso_name,
                rows=len(df),
            )
            if not df.empty:
                frames.append(df)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "iso_failed",
                iso=scraper.iso_name,
                error=str(exc),
                hint=(
                    f"Check the ISO website directly for updated queue URLs. "
                    f"ISO: {scraper.iso_name.upper()}"
                ),
            )

    if not frames:
        log.warning("no_data_from_any_iso")
        return pd.DataFrame(columns=_OUTPUT_COLS)

    combined = pd.concat(frames, ignore_index=True)
    log.info("combined", total_rows=len(combined), isos=len(frames))
    return combined


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run(snapshot_date: str | None = None) -> None:
    """Download and upsert all ISO interconnection queues.

    snapshot_date: ISO 8601 date string (YYYY-MM-DD); defaults to today.

    Cache: raw/ferc_queue/combined_{snapshot_date}.parquet
    DB:    ferc_queue table, PRIMARY KEY (snapshot_date, project_id)
    """
    snap = (
        date.fromisoformat(snapshot_date) if snapshot_date else date.today()
    )

    dest_dir: Path = data_path("raw/ferc_queue")
    combined_cache = dest_dir / f"combined_{snap}.parquet"

    if combined_cache.exists():
        log.info("combined_cache_hit", path=str(combined_cache), snapshot_date=str(snap))
        df = pd.read_parquet(combined_cache)
    else:
        df = snapshot_all_isos(snap, dest_dir)
        if df.empty:
            log.warning("ferc_queue_empty", snapshot_date=str(snap))
            return
        # Coerce types before saving
        df["snapshot_date"] = snap
        df["mw_capacity"] = pd.to_numeric(df["mw_capacity"], errors="coerce")
        df["withdrawn"] = df["withdrawn"].fillna(False).astype(bool)
        df.to_parquet(combined_cache, index=False)
        log.info("combined_cached", path=str(combined_cache), rows=len(df))

    # Upsert to DB — project_id already prefixed with ISO name in _build_row
    try:
        n = loaders.upsert_df(df, "ferc_queue", ["snapshot_date", "project_id"])
        log.info("ferc_queue_upserted", rows=n, snapshot_date=str(snap))
    except Exception as exc:  # noqa: BLE001
        log.error("db_upsert_failed", error=str(exc))
        raise


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest FERC interconnection queues.")
    parser.add_argument(
        "--snapshot-date",
        default=None,
        help="Snapshot date in YYYY-MM-DD format (default: today)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(snapshot_date=args.snapshot_date)
