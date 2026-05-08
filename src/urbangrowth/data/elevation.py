"""USGS 3DEP Digital Elevation Model (DEM) download and terrain derivation.

Downloads 1m (with 1/3 arc-second fallback) DEM tiles for city bounding boxes
via the USGS TNM API. Mosaics, clips, and reprojects tiles to city UTM CRS.
Derives slope, aspect, and hillshade rasters. Idempotent.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import requests
import rioxarray as rxr
import structlog
import xarray as xr
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

from urbangrowth.config import data_path, get_cities, get_pipeline

load_dotenv()

log = structlog.get_logger(__name__)

_TNM_URL = "https://tnmapi.cr.usgs.gov/api/products"
_DATASET_1M = "Digital Elevation Model (DEM) 1 meter"
_DATASET_10M = "Digital Elevation Model (DEM) 1/3 arc-second"

# Hillshade parameters
_SUN_ZENITH_DEG = 45.0
_SUN_AZIMUTH_DEG = 315.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_dem_tiles(bbox: list[float], dataset: str) -> list[dict]:
    """Query USGS TNM API for DEM product URLs covering bbox.

    Parameters
    ----------
    bbox:
        [west, south, east, north] in EPSG:4326.
    dataset:
        TNM dataset name string (e.g. ``_DATASET_1M`` or ``_DATASET_10M``).

    Returns
    -------
    list[dict]
        Product dicts with keys including ``title``, ``downloadURL``, ``fileSize``.
    """
    west, south, east, north = bbox
    params = {
        "datasets": dataset,
        "bbox": f"{west},{south},{east},{north}",
        "outputFormat": "JSON",
        "max": 100,
    }
    log.info("tnm_query", dataset=dataset, bbox=bbox)
    resp = requests.get(_TNM_URL, params=params, timeout=60)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    log.info("tnm_tiles_found", dataset=dataset, count=len(items))
    return items


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30))
def download_tile(url: str, dest_dir: Path) -> Path:
    """Download a single DEM tile by URL if not already present.

    Parameters
    ----------
    url:
        Direct download URL for the tile file.
    dest_dir:
        Directory to save the tile into.

    Returns
    -------
    Path
        Local path of the downloaded (or pre-existing) tile.
    """
    filename = url.split("/")[-1].split("?")[0]  # strip query params if any
    dest = dest_dir / filename
    if dest.exists():
        log.debug("skip_existing_tile", file=str(dest))
        return dest

    log.info("downloading_tile", url=url, dest=str(dest))
    dest_dir.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, stream=True, timeout=600)
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            fh.write(chunk)
    log.info("tile_saved", file=str(dest), size_mb=round(dest.stat().st_size / 1e6, 2))
    return dest


def mosaic_and_clip(tile_paths: list[Path], bbox: list[float]) -> xr.DataArray:
    """Open, mosaic, and clip DEM tiles to the city bounding box.

    Parameters
    ----------
    tile_paths:
        Local paths to downloaded DEM GeoTIFF files.
    bbox:
        [west, south, east, north] in EPSG:4326.

    Returns
    -------
    xr.DataArray
        Mosaicked and clipped DEM in its native CRS (EPSG:4326).
    """
    west, south, east, north = bbox
    log.info("opening_tiles", count=len(tile_paths))
    tiles = [rxr.open_rasterio(p, masked=True) for p in tile_paths]

    if len(tiles) == 1:
        merged = tiles[0]
    else:
        merged = rxr.merge_arrays(tiles)

    log.info("mosaic_complete", shape=merged.shape)
    clipped = merged.rio.clip_box(minx=west, miny=south, maxx=east, maxy=north)
    log.info("clip_complete", shape=clipped.shape)
    return clipped


def derive_terrain_products(dem_utm: xr.DataArray, out_dir: Path) -> dict[str, Path]:
    """Derive and save slope, aspect, and hillshade from a UTM-projected DEM.

    All four products (elevation, slope, aspect, hillshade) are written as
    GeoTIFFs in the same UTM CRS as ``dem_utm``.  If all four already exist,
    the function returns immediately (idempotent).

    Parameters
    ----------
    dem_utm:
        DEM DataArray reprojected to a UTM CRS (cell sizes in metres).
    out_dir:
        Directory where output TIFFs are written.

    Returns
    -------
    dict[str, Path]
        Mapping of product name → absolute output path.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "elevation": out_dir / "elevation.tif",
        "slope": out_dir / "slope.tif",
        "aspect": out_dir / "aspect.tif",
        "hillshade": out_dir / "hillshade.tif",
    }

    if all(p.exists() for p in paths.values()):
        log.info("terrain_products_already_exist", directory=str(out_dir))
        return paths

    # --- elevation ---------------------------------------------------------
    log.info("saving_elevation", path=str(paths["elevation"]))
    dem_utm.rio.to_raster(paths["elevation"])

    # --- gradient computation ----------------------------------------------
    elev_arr = dem_utm.values[0].astype(np.float64)  # (rows, cols)
    cell_x = abs(float(dem_utm.rio.resolution()[0]))   # metres (east-west)
    cell_y = abs(float(dem_utm.rio.resolution()[1]))   # metres (north-south)
    log.info("computing_gradients", cell_x_m=cell_x, cell_y_m=cell_y)

    # numpy.gradient: first arg = row spacing (y), second = col spacing (x)
    dz_dy, dz_dx = np.gradient(elev_arr, cell_y, cell_x)

    # --- slope (degrees) ---------------------------------------------------
    slope_rad = np.arctan(np.sqrt(dz_dx**2 + dz_dy**2))
    slope_deg = np.degrees(slope_rad)

    slope_da = _array_like(dem_utm, slope_deg, dtype=np.float32)
    slope_da.rio.to_raster(paths["slope"])
    log.info("slope_saved", path=str(paths["slope"]))

    # --- aspect (degrees clockwise from north, [0, 360)) -------------------
    aspect_deg = (90.0 - np.degrees(np.arctan2(-dz_dy, dz_dx))) % 360.0

    aspect_da = _array_like(dem_utm, aspect_deg, dtype=np.float32)
    aspect_da.rio.to_raster(paths["aspect"])
    log.info("aspect_saved", path=str(paths["aspect"]))

    # --- hillshade (uint8 [0, 255]) ----------------------------------------
    zenith_rad = math.radians(_SUN_ZENITH_DEG)
    azimuth_rad = math.radians(_SUN_AZIMUTH_DEG)

    intensity = (
        math.cos(zenith_rad) * np.cos(slope_rad)
        + math.sin(zenith_rad) * np.sin(slope_rad) * np.cos(azimuth_rad - np.radians(aspect_deg))
    )
    # Clamp to [0, 1] then scale to [0, 255]
    intensity = np.clip(intensity, 0.0, 1.0)
    hillshade_u8 = (intensity * 255).astype(np.uint8)

    hillshade_da = _array_like(dem_utm, hillshade_u8, dtype=np.uint8)
    hillshade_da.rio.to_raster(paths["hillshade"])
    log.info("hillshade_saved", path=str(paths["hillshade"]))

    return paths


def run(city: str = "phoenix") -> None:
    """Download DEM tiles, mosaic, reproject, and derive terrain products.

    This is the CLI / pipeline entry point.  It is fully idempotent: existing
    tile files and derived rasters are never re-downloaded or recomputed.
    """
    cities = get_cities()
    if city not in cities:
        raise ValueError(f"Unknown city '{city}'. Available: {list(cities)}")

    city_cfg = cities[city]
    bbox: list[float] = city_cfg["bbox"]
    utm_crs: str = city_cfg["utm_crs"]

    pipeline = get_pipeline()
    raw_subdir = pipeline["raw_data_subdirs"]["elevation"]
    dest_dir: Path = data_path(raw_subdir) / city
    dest_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 1. Discover tiles (1m preferred, 10m fallback)
    # ------------------------------------------------------------------
    products = search_dem_tiles(bbox, _DATASET_1M)
    used_dataset = _DATASET_1M
    if not products:
        log.warning("no_1m_tiles_found_falling_back", city=city, fallback=_DATASET_10M)
        products = search_dem_tiles(bbox, _DATASET_10M)
        used_dataset = _DATASET_10M

    if not products:
        log.error("no_dem_tiles_found", city=city, bbox=bbox)
        raise RuntimeError(f"No DEM tiles found for city '{city}' bbox {bbox}")

    log.info(
        "dem_tiles_discovered",
        city=city,
        dataset=used_dataset,
        count=len(products),
    )

    # ------------------------------------------------------------------
    # 2. Download tiles (idempotent)
    # ------------------------------------------------------------------
    tile_paths: list[Path] = []
    for product in products:
        url = product["downloadURL"]
        path = download_tile(url, dest_dir)
        tile_paths.append(path)

    log.info("all_tiles_ready", city=city, count=len(tile_paths))

    # ------------------------------------------------------------------
    # 3. Check if derived rasters already exist
    # ------------------------------------------------------------------
    out_dir = dest_dir  # same directory as tiles
    output_files = [
        out_dir / "elevation.tif",
        out_dir / "slope.tif",
        out_dir / "aspect.tif",
        out_dir / "hillshade.tif",
    ]
    if all(p.exists() for p in output_files):
        log.info("all_rasters_already_exist_skipping_derivation", city=city)
        # Still log summary stats from existing elevation raster
        _log_summary_stats(city, out_dir)
        return

    # ------------------------------------------------------------------
    # 4. Mosaic + clip
    # ------------------------------------------------------------------
    dem_wgs84 = mosaic_and_clip(tile_paths, bbox)

    # ------------------------------------------------------------------
    # 5. Reproject to UTM
    # ------------------------------------------------------------------
    log.info("reprojecting_to_utm", city=city, crs=utm_crs)
    dem_utm = dem_wgs84.rio.reproject(utm_crs)
    log.info(
        "reproject_complete",
        city=city,
        shape=dem_utm.shape,
        crs=utm_crs,
        resolution_m=abs(float(dem_utm.rio.resolution()[0])),
    )

    # ------------------------------------------------------------------
    # 6. Derive terrain products
    # ------------------------------------------------------------------
    raster_paths = derive_terrain_products(dem_utm, out_dir)
    log.info("terrain_products_complete", city=city, paths={k: str(v) for k, v in raster_paths.items()})

    # ------------------------------------------------------------------
    # 7. Summary statistics
    # ------------------------------------------------------------------
    _log_summary_stats(city, out_dir)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _array_like(
    template: xr.DataArray,
    data: np.ndarray,
    dtype: type,
) -> xr.DataArray:
    """Wrap *data* in a DataArray with the same spatial metadata as *template*."""
    da = xr.DataArray(
        data[np.newaxis, :, :].astype(dtype),  # add band dim
        dims=template.dims,
        coords=template.coords,
        attrs=template.attrs,
    )
    da = da.rio.write_crs(template.rio.crs)
    da = da.rio.write_transform(template.rio.transform())
    return da


def _log_summary_stats(city: str, out_dir: Path) -> None:
    """Open saved rasters and log summary statistics."""
    elev_path = out_dir / "elevation.tif"
    slope_path = out_dir / "slope.tif"
    aspect_path = out_dir / "aspect.tif"

    if not elev_path.exists() or not slope_path.exists() or not aspect_path.exists():
        log.warning("cannot_compute_stats_missing_rasters", city=city)
        return

    elev_da = rxr.open_rasterio(elev_path, masked=True)
    slope_da = rxr.open_rasterio(slope_path, masked=True)
    aspect_da = rxr.open_rasterio(aspect_path, masked=True)

    elev_arr = elev_da.values[0]
    slope_arr = slope_da.values[0]
    aspect_arr = aspect_da.values[0]

    # Valid (non-NaN) masks
    elev_valid = ~np.isnan(elev_arr)
    slope_valid = ~np.isnan(slope_arr)
    aspect_valid = ~np.isnan(aspect_arr)

    mean_elev = float(np.nanmean(elev_arr))
    min_elev = float(np.nanmin(elev_arr))
    max_elev = float(np.nanmax(elev_arr))
    mean_slope = float(np.nanmean(slope_arr))

    total_slope_cells = int(np.sum(slope_valid))
    steep_cells = int(np.sum(slope_arr[slope_valid] > 15.0))
    frac_steep = steep_cells / total_slope_cells if total_slope_cells > 0 else float("nan")

    # Aspect distribution (valid cells only)
    asp = aspect_arr[aspect_valid]
    total_asp = asp.size
    if total_asp > 0:
        north_facing = float(np.sum((asp >= 315.0) | (asp < 45.0)) / total_asp)
        south_facing = float(np.sum((asp >= 135.0) & (asp < 225.0)) / total_asp)
    else:
        north_facing = float("nan")
        south_facing = float("nan")

    log.info(
        "elevation_summary",
        city=city,
        mean_elevation_m=round(mean_elev, 2),
        min_elevation_m=round(min_elev, 2),
        max_elevation_m=round(max_elev, 2),
        mean_slope_deg=round(mean_slope, 3),
        frac_slope_gt15deg=round(frac_steep, 4),
        pct_north_facing=round(north_facing * 100, 2),
        pct_south_facing=round(south_facing * 100, 2),
    )
