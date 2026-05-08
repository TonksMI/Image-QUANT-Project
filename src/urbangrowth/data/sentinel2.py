"""Sentinel-2 L2A STAC interface for Microsoft Planetary Computer.

Provides two functions used by the composites pipeline:

  query_scenes(city, start_date, end_date, max_cloud_pct=60)
      Queries the PC STAC catalogue and returns signed pystac.Item objects.
      No data is downloaded — items are lazy handles to COG assets.

  load_stack(items, bands, bbox_4326, utm_epsg, resolution, chunksize)
      Returns a Dask-backed xr.DataArray (time × band × y × x) via stackstac.
      Only the requested COG windows are fetched when .compute() is called.

rescale=False keeps raw DN values so that:
  - SCL values are exact integers 0-11 (needed for cloud masking)
  - Spectral indices (NDVI, NDBI, NDWI) cancel the scale factor in their ratios

CLI: ug ingest sentinel2 --city phoenix  →  connectivity check only.
     ug composite generate --city phoenix  →  actual composite building.
"""
from __future__ import annotations

import numpy as np
import planetary_computer
import pystac_client
import stackstac
import structlog
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

from urbangrowth.config import get_cities

load_dotenv()
log = structlog.get_logger(__name__)

_STAC_URL   = "https://planetarycomputer.microsoft.com/api/stac/v1"
_COLLECTION = "sentinel-2-l2a"

SPECTRAL_BANDS = ["B02", "B03", "B04", "B08", "B11", "B12"]
SCL_BAND = "SCL"


# ---------------------------------------------------------------------------
# STAC catalogue
# ---------------------------------------------------------------------------


def _catalog() -> pystac_client.Client:
    return pystac_client.Client.open(
        _STAC_URL,
        modifier=planetary_computer.sign_inplace,
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30))
def query_scenes(
    city: str,
    start_date: str,
    end_date: str,
    max_cloud_pct: int = 60,
) -> list:
    """Return signed pystac.Item objects for *city* in [start_date, end_date].

    Parameters
    ----------
    city:
        City key from cities.yaml (e.g. ``"phoenix"``).
    start_date, end_date:
        ISO 8601 date strings, e.g. ``"2022-03-01"`` / ``"2022-03-31"``.
    max_cloud_pct:
        Maximum scene-level cloud cover threshold.
    """
    cities = get_cities()
    if city not in cities:
        raise ValueError(f"Unknown city '{city}'. Options: {list(cities)}")

    bbox = cities[city]["bbox"]  # [west, south, east, north]

    catalog = _catalog()
    search = catalog.search(
        collections=[_COLLECTION],
        bbox=bbox,
        datetime=f"{start_date}/{end_date}",
        query={"eo:cloud_cover": {"lt": max_cloud_pct}},
    )
    items = list(search.item_collection())

    log.info(
        "scenes_queried",
        city=city,
        start=start_date,
        end=end_date,
        max_cloud_pct=max_cloud_pct,
        count=len(items),
    )
    return items


# ---------------------------------------------------------------------------
# Lazy COG loader
# ---------------------------------------------------------------------------


def load_stack(
    items: list,
    bands: list[str],
    bbox_4326: list[float],
    utm_epsg: int,
    resolution: int = 10,
    chunksize: int = 2048,
):
    """Build a lazy Dask-backed (time × band × y × x) DataArray via stackstac.

    COG windows are fetched only when the caller calls ``.compute()``.

    Parameters
    ----------
    items:
        Signed pystac.Item objects from :func:`query_scenes`.
    bands:
        Asset keys, e.g. ``["B02", "B03", "B04", "B08", "B11", "B12", "SCL"]``.
    bbox_4326:
        ``[west, south, east, north]`` in EPSG:4326 (matches cities.yaml bbox order).
    utm_epsg:
        Target UTM EPSG code as int (e.g. ``32612`` for Phoenix).
    resolution:
        Pixel size in target CRS units (metres). Default 10 m.
    chunksize:
        Dask spatial chunk size in pixels. 2048 ≈ 80 MB per band per chunk at float32.
    """
    stack = stackstac.stack(
        items,
        assets=bands,
        epsg=utm_epsg,
        resolution=resolution,
        bounds_latlon=tuple(bbox_4326),
        chunksize=chunksize,
        fill_value=np.nan,
        rescale=False,
    )

    log.info(
        "stack_built",
        bands=bands,
        shape=list(stack.shape),
        utm_epsg=utm_epsg,
        resolution_m=resolution,
    )
    return stack


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run(city: str = "phoenix") -> None:
    """Validate Planetary Computer STAC connectivity for *city*.

    Use ``ug composite generate`` to build monthly composites.
    """
    items = query_scenes(city, "2022-01-01", "2022-01-31", max_cloud_pct=20)
    log.info(
        "sentinel2_connectivity_ok",
        city=city,
        sample_scenes=len(items),
        hint="Run 'ug composite generate' to build monthly composites.",
    )
