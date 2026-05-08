"""OpenStreetMap feature ingestion via osmnx.

Pulls drive network, building footprints, and commercial/industrial POIs
for Phoenix and Austin. Computes road density per H3 hex (resolution 8).
Saves layers as GeoPackage. Upserts road_density into h3_features table.

Idempotent: if the GeoPackage already exists, the osmnx download is skipped
and layers are read back from disk.
"""
from __future__ import annotations

import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
import structlog
from dotenv import load_dotenv
from shapely.geometry import Polygon

from urbangrowth.config import data_path, get_cities, get_pipeline
from urbangrowth.db import loaders

load_dotenv()

log = structlog.get_logger(__name__)

# osmnx import — configure before importing so settings take effect immediately
try:
    import osmnx as ox

    ox.settings.log_console = False
    ox.settings.use_cache = True
except ImportError as exc:
    raise ImportError("Install osmnx: conda install -c conda-forge osmnx") from exc

try:
    import h3
except ImportError as exc:
    raise ImportError("Install h3-py: conda install -c conda-forge h3-py") from exc

# H3 resolution 8 hex area in km²
_H3_RES8_AREA_KM2 = 0.7373

# city_id mapping (matches the cities table seed in schema.sql)
_CITY_IDS: dict[str, int] = {"phoenix": 1, "austin": 2}


# ---------------------------------------------------------------------------
# Public API: fetchers
# ---------------------------------------------------------------------------


def fetch_drive_network(bbox: list[float], utm_crs: str) -> gpd.GeoDataFrame:
    """Download the drive network for *bbox* and return edges in UTM CRS.

    Parameters
    ----------
    bbox:
        [west, south, east, north] in EPSG:4326.
    utm_crs:
        Target UTM CRS string (e.g. ``"EPSG:32612"``).

    Returns
    -------
    GeoDataFrame
        Edge table in *utm_crs* with an extra ``length_m`` column (metres).
    """
    import time

    west, south, east, north = bbox[0], bbox[1], bbox[2], bbox[3]

    log.info("osm_downloading_network", bbox=bbox)
    t0 = time.perf_counter()
    G = ox.graph_from_bbox(
        north, south, east, west, network_type="drive", retain_all=False
    )
    nodes, edges = ox.graph_to_gdfs(G)
    elapsed = time.perf_counter() - t0

    log.info(
        "osm_network_downloaded",
        nodes=len(nodes),
        edges=len(edges),
        elapsed_s=round(elapsed, 1),
    )

    # Reproject to UTM for metre-accurate lengths
    G_proj = ox.project_graph(G, to_crs=utm_crs)
    edges_utm = ox.graph_to_gdfs(G_proj, nodes=False)
    edges_utm = edges_utm.copy()
    edges_utm["length_m"] = edges_utm["length"]   # already in metres after projection

    return edges_utm


def fetch_buildings(bbox: list[float]) -> gpd.GeoDataFrame:
    """Download building footprints for *bbox*.

    Returns a GeoDataFrame (EPSG:4326) with columns:
    ``osm_id``, ``building_type``, ``geometry`` (Polygon / MultiPolygon only).
    """
    west, south, east, north = bbox[0], bbox[1], bbox[2], bbox[3]

    log.info("osm_downloading_buildings", bbox=bbox)
    buildings = ox.geometries_from_bbox(
        north, south, east, west, tags={"building": True}
    )

    buildings = buildings[
        buildings.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
    ].copy()

    buildings = buildings.reset_index()

    # osmid may be part of a MultiIndex; normalise
    if "osmid" in buildings.columns:
        osm_id_col = buildings["osmid"]
    else:
        osm_id_col = buildings.index

    out = gpd.GeoDataFrame(
        {
            "osm_id": osm_id_col.astype(str).values,
            "building_type": buildings["building"].astype(str).values
            if "building" in buildings.columns
            else pd.NA,
            "geometry": buildings["geometry"].values,
        },
        crs="EPSG:4326",
    )
    log.info("osm_buildings_fetched", count=len(out))
    return out


def fetch_pois(bbox: list[float]) -> gpd.GeoDataFrame:
    """Download commercial / industrial POIs for *bbox*.

    Returns a GeoDataFrame (EPSG:4326) with columns:
    ``osm_id``, ``geometry`` (Point only).
    """
    west, south, east, north = bbox[0], bbox[1], bbox[2], bbox[3]

    log.info("osm_downloading_pois", bbox=bbox)
    pois = ox.geometries_from_bbox(
        north,
        south,
        east,
        west,
        tags={
            "landuse": ["commercial", "industrial", "retail", "warehouse"],
            "amenity": ["marketplace", "bank", "restaurant", "fast_food", "fuel"],
            "shop": True,
        },
    )

    pois = pois[pois.geometry.geom_type == "Point"].reset_index()

    osm_id_col = (
        pois["osmid"].astype(str)
        if "osmid" in pois.columns
        else pd.Series(range(len(pois)), dtype=str)
    )

    out = gpd.GeoDataFrame(
        {
            "osm_id": osm_id_col.values,
            "geometry": pois["geometry"].values,
        },
        crs="EPSG:4326",
    )
    log.info("osm_pois_fetched", count=len(out))
    return out


# ---------------------------------------------------------------------------
# Road density computation
# ---------------------------------------------------------------------------


def compute_road_density(
    edges_utm: gpd.GeoDataFrame,
    resolution: int,
) -> pd.DataFrame:
    """Compute road density (km/km²) per H3 hex using edge midpoints.

    Parameters
    ----------
    edges_utm:
        Edge GeoDataFrame in any projected CRS with a ``length_m`` column.
    resolution:
        H3 resolution (8 for city-scale).

    Returns
    -------
    DataFrame with columns: ``h3_index``, ``total_length_m``, ``road_density``.
    """
    # Reproject to WGS-84 to get lat/lng for H3 lookups
    edges_4326 = edges_utm.to_crs("EPSG:4326").copy()

    # Midpoint of each edge
    mid = edges_4326.geometry.interpolate(0.5, normalized=True)
    edges_4326["mid_lat"] = mid.y
    edges_4326["mid_lng"] = mid.x

    edges_4326["h3_index"] = edges_4326.apply(
        lambda r: h3.geo_to_h3(r.mid_lat, r.mid_lng, resolution), axis=1
    )

    density_df = (
        edges_4326.groupby("h3_index")["length_m"]
        .sum()
        .reset_index()
        .rename(columns={"length_m": "total_length_m"})
    )
    density_df["road_density"] = density_df["total_length_m"] / 1000 / _H3_RES8_AREA_KM2

    log.info(
        "road_density_computed",
        hexes=len(density_df),
        mean_density=round(float(density_df["road_density"].mean()), 3),
        max_density=round(float(density_df["road_density"].max()), 3),
        total_road_km=round(float(density_df["total_length_m"].sum() / 1000), 1),
    )
    return density_df


# ---------------------------------------------------------------------------
# GeoPackage helpers
# ---------------------------------------------------------------------------


def _edges_to_4326(edges_utm: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Return edges in EPSG:4326 with osm_id, highway, length_m columns."""
    e = edges_utm.to_crs("EPSG:4326").copy()
    e = e.reset_index()

    # Edge index is (u, v, key) MultiIndex after graph_to_gdfs
    # The osmid column on edges is usually a list; stringify it.
    if "osmid" in e.columns:
        e["osm_id"] = e["osmid"].apply(
            lambda x: str(x[0]) if isinstance(x, list) else str(x)
        )
    else:
        e["osm_id"] = e.index.astype(str)

    highway_col = "highway" if "highway" in e.columns else None

    keep_cols = ["osm_id"]
    if highway_col:
        keep_cols.append(highway_col)
    keep_cols += ["length_m", "geometry"]

    # Ensure highway column exists
    if "highway" not in e.columns:
        e["highway"] = None
    keep_cols = ["osm_id", "highway", "length_m", "geometry"]

    # highway values can be lists; stringify
    e["highway"] = e["highway"].apply(
        lambda x: x[0] if isinstance(x, list) else x
    )

    return gpd.GeoDataFrame(e[keep_cols], crs="EPSG:4326")


def _density_to_geodataframe(density_df: pd.DataFrame) -> gpd.GeoDataFrame:
    """Attach H3 polygon geometries to the density DataFrame."""
    def _h3_polygon(h3_index: str) -> Polygon:
        boundary = h3.h3_to_geo_boundary(h3_index, geo_json=True)
        # geo_json=True returns [lng, lat] pairs — Shapely expects (x, y) = (lng, lat)
        return Polygon(boundary)

    polygons = density_df["h3_index"].apply(_h3_polygon)
    gdf = gpd.GeoDataFrame(density_df.copy(), geometry=polygons, crs="EPSG:4326")
    return gdf


# ---------------------------------------------------------------------------
# GeoPackage save / load
# ---------------------------------------------------------------------------


def _save_gpkg(
    out: Path,
    edges_utm: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
    pois: gpd.GeoDataFrame,
    density_gdf: gpd.GeoDataFrame,
) -> None:
    """Write all four layers to a GeoPackage file."""
    roads_4326 = _edges_to_4326(edges_utm)

    roads_4326.to_file(out, layer="roads", driver="GPKG")
    buildings.to_file(out, layer="buildings", driver="GPKG")
    pois.to_file(out, layer="pois", driver="GPKG")
    density_gdf.to_file(out, layer="h3_road_density", driver="GPKG")

    log.info(
        "gpkg_saved",
        path=str(out),
        roads=len(roads_4326),
        buildings=len(buildings),
        pois=len(pois),
        h3_hexes=len(density_gdf),
    )


def _load_gpkg(out: Path) -> tuple[
    gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame
]:
    """Read back all four layers from an existing GeoPackage."""
    log.info("gpkg_loading_cached", path=str(out))
    roads = gpd.read_file(out, layer="roads")
    buildings = gpd.read_file(out, layer="buildings")
    pois = gpd.read_file(out, layer="pois")
    density_gdf = gpd.read_file(out, layer="h3_road_density")
    return roads, buildings, pois, density_gdf


# ---------------------------------------------------------------------------
# DB upsert
# ---------------------------------------------------------------------------


def _upsert_road_density(
    density_df: pd.DataFrame,
    city: str,
    as_of: datetime.date,
) -> None:
    """Upsert h3_index / city_id / date / road_density rows into h3_features."""
    city_id = _CITY_IDS[city]

    upsert = pd.DataFrame(
        {
            "h3_index": density_df["h3_index"],
            "city_id": city_id,
            "date": as_of,
            "road_density": density_df["road_density"].round(4),
        }
    )
    loaders.upsert_df(upsert, "h3_features", pk_cols=["h3_index", "city_id", "date"])


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def ingest_city(city: str) -> None:
    """Ingest all OSM layers for *city* and upsert road density to DB.

    Downloads drive network, buildings, and POIs; saves as GeoPackage;
    computes road density per H3 hex; upserts into ``h3_features``.
    Idempotent: if the GeoPackage already exists the download is skipped.
    """
    cities = get_cities()
    if city not in cities:
        raise ValueError(f"Unknown city '{city}'. Available: {list(cities)}")

    city_cfg = cities[city]
    bbox: list[float] = city_cfg["bbox"]
    utm_crs: str = city_cfg["utm_crs"]
    resolution: int = city_cfg.get("h3_resolution_city", 8)

    pipeline = get_pipeline()
    dest_dir: Path = data_path(pipeline["raw_data_subdirs"]["osm"]) / city
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{city}_osm.gpkg"

    if out.exists():
        log.info("osm_gpkg_exists_loading_cache", city=city, path=str(out))
        roads_gdf, buildings, pois, density_gdf = _load_gpkg(out)

        # Reconstruct density_df from the cached GeoDataFrame
        density_df = density_gdf[["h3_index", "total_length_m", "road_density"]].copy()

    else:
        # ------------------------------------------------------------------
        # 1. Download
        # ------------------------------------------------------------------
        edges_utm = fetch_drive_network(bbox, utm_crs)
        buildings = fetch_buildings(bbox)
        pois = fetch_pois(bbox)

        log.info(
            "osm_download_complete",
            city=city,
            edges=len(edges_utm),
            buildings=len(buildings),
            pois=len(pois),
        )

        # ------------------------------------------------------------------
        # 2. Compute road density
        # ------------------------------------------------------------------
        density_df = compute_road_density(edges_utm, resolution)

        # ------------------------------------------------------------------
        # 3. Build H3 GeoDataFrame and save GeoPackage
        # ------------------------------------------------------------------
        density_gdf = _density_to_geodataframe(density_df)
        _save_gpkg(out, edges_utm, buildings, pois, density_gdf)

    # ------------------------------------------------------------------
    # 4. Upsert h3_features (always re-upsert — idempotent)
    # ------------------------------------------------------------------
    as_of = datetime.date.today()
    _upsert_road_density(density_df, city, as_of)


def run(city: str = "phoenix") -> None:
    """CLI / pipeline entry point.

    Ingest OSM data for *city* and emit a structured summary log.
    """
    ingest_city(city)

    # Read back density from the saved GeoPackage to report summary stats
    pipeline = get_pipeline()
    dest_dir: Path = data_path(pipeline["raw_data_subdirs"]["osm"]) / city
    out = dest_dir / f"{city}_osm.gpkg"
    density_gdf = gpd.read_file(out, layer="h3_road_density")

    h3_hexes = len(density_gdf)
    total_road_km = round(float(density_gdf["total_length_m"].sum() / 1000), 1)
    mean_density = round(float(density_gdf["road_density"].mean()), 3)

    log.info(
        "osm_run_complete",
        city=city,
        h3_hexes_with_roads=h3_hexes,
        total_road_km=total_road_km,
        mean_density=mean_density,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Ingest OSM drive network, buildings, and POIs for a city."
    )
    parser.add_argument(
        "city",
        nargs="?",
        default="phoenix",
        choices=list(get_cities().keys()),
        help="City to ingest (default: phoenix)",
    )
    args = parser.parse_args()
    run(city=args.city)
