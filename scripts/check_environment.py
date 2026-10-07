"""Run: python scripts/check_environment.py --synthetic-only
Expected: ALL CHECKS PASSED (nonzero exit and one-line summary on failure)."""
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 ShiyinShyn
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import importlib.util

_REQUIRED = ("click", "fiona", "geopandas", "networkx", "numpy", "pandas",
             "pyogrio", "pyproj", "rasterio", "rich", "scipy", "shapely", "yaml")
_MISSING = [name for name in _REQUIRED if importlib.util.find_spec(name) is None]
if _MISSING:
    print("FAIL: missing " + ", ".join(_MISSING)
          + "; run conda env create -f environment.yml and conda activate csl2-planner")
    raise SystemExit(1)

import click
from click.testing import CliRunner
import fiona
import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import pyogrio
import pyproj
import rasterio
from rasterio.io import MemoryFile
from rasterio.mask import mask
from rasterio.transform import from_origin
from rich.console import Console
import scipy
from scipy.optimize import linprog
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree
import shapely
from shapely.geometry import Point, box, mapping
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = Console(force_terminal=False)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main(*, include_local_data: bool = True) -> None:
    require(sys.version_info[:2] == (3, 11), "Expected Python 3.11")
    CONSOLE.print(f"Python: {sys.version.split()[0]} | {sys.executable}")
    for name in (
        "geopandas", "shapely", "rasterio", "pyproj", "pyogrio", "fiona",
        "numpy", "pandas", "scipy", "networkx", "pyyaml", "click", "rich",
    ):
        CONSOLE.print(f"{name}: {importlib.metadata.version(name)}")
    CONSOLE.print(
        f"Native: GEOS={shapely.geos_version_string}, "
        f"PROJ={pyproj.proj_version_str}, GDAL rasterio={rasterio.__gdal_version__}, "
        f"fiona={fiona.__gdal_version__}, pyogrio={pyogrio.__gdal_version_string__}"
    )

    config = yaml.safe_load((ROOT / "environment.yml").read_text(encoding="utf-8-sig"))
    require(config["name"] == "csl2-planner", "Invalid environment name")
    require(config["channels"] == ["conda-forge", "nodefaults"], "Unexpected channels")
    require(yaml.safe_load("assignment: {method: all_or_nothing}")["assignment"]["method"]
            == "all_or_nothing", "YAML parsing failed")

    polygon = box(0, 0, 2, 2)
    require(polygon.intersection(box(1, 1, 3, 3)).area == 1, "GEOS overlay failed")
    require(polygon.buffer(1).area > polygon.area, "GEOS buffer failed")
    frame = gpd.GeoDataFrame({"zone_id": [1]}, geometry=[polygon], crs="EPSG:3857")
    require(len(frame.sindex.query(Point(1, 1))) == 1, "Spatial index failed")
    require(frame.to_crs("EPSG:4326").crs.to_epsg() == 4326, "CRS transform failed")
    transformer = pyproj.Transformer.from_crs(4326, 3857, always_xy=True)
    require(np.allclose(transformer.transform(0, 0), (0, 0)), "PROJ transform failed")
    with TemporaryDirectory(prefix="csl2-smoke-") as directory:
        target = Path(directory) / "zones.gpkg"
        frame.to_file(target, layer="zones", engine="pyogrio", driver="GPKG")
        for engine in ("pyogrio", "fiona"):
            loaded = gpd.read_file(target, layer="zones", engine=engine)
            require(len(loaded) == 1 and loaded.geometry.iloc[0].equals(polygon),
                    f"Vector IO failed: {engine}")
        target_fiona = Path(directory) / "zones-fiona.gpkg"
        frame.to_file(target_fiona, layer="zones", engine="fiona", driver="GPKG")
        require(len(gpd.read_file(target_fiona, engine="pyogrio")) == 1,
                "Fiona write / pyogrio read failed")
    CONSOLE.print("PASS: geometry, spatial indexing, CRS and both vector IO engines")

    dem = np.arange(16, dtype="float32").reshape(4, 4)
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", height=4, width=4, count=1, dtype=dem.dtype,
                         crs="EPSG:3857", transform=from_origin(0, 4, 1, 1),
                         nodata=-9999) as dataset:
            dataset.write(dem, 1)
        with memory.open() as dataset:
            require(np.array_equal(dataset.read(1), dem), "GeoTIFF round-trip failed")
            clipped, _ = mask(dataset, [mapping(box(1, 1, 3, 3))], crop=True)
            require(clipped.shape == (1, 2, 2), "Vector/raster clipping failed")
    dy, dx = np.gradient(dem, 1.0, 1.0)
    require(np.isfinite(np.hypot(dx, dy)).all(), "DEM gradient calculation failed")
    CONSOLE.print("PASS: GeoTIFF, DEM arrays/gradients and vector/raster clipping")

    graph = nx.DiGraph()
    graph.add_weighted_edges_from([(0, 1, 2.0), (1, 2, 3.0), (0, 2, 10.0)])
    require(nx.shortest_path(graph, 0, 2, weight="weight") == [0, 1, 2],
            "NetworkX directed routing failed")
    matrix = csr_matrix(nx.to_scipy_sparse_array(graph, nodelist=[0, 1, 2],
                                                weight="weight", dtype=float))
    matrix.indices = matrix.indices.astype(np.int32)
    matrix.indptr = matrix.indptr.astype(np.int32)
    require(dijkstra(matrix, directed=True, indices=0)[2] == 5.0,
            "SciPy sparse routing failed")
    _, nearest = cKDTree([[0, 0], [10, 10]]).query([1, 1])
    require(nearest == 0, "Road-node snapping index failed")
    result = linprog([1.0, 2.0], A_eq=[[1.0, 1.0]], b_eq=[100.0],
                     bounds=[(0, None), (0, None)], method="highs")
    require(result.success and np.allclose(result.x, [100, 0]), "Optimization failed")
    od = pd.DataFrame({"origin": [0], "destination": [2], "trips": [100.0]})
    require(od["trips"].sum() == 100, "OD table failed")
    CONSOLE.print("PASS: directed/sparse routing, node snapping, optimization and OD tables")

    @click.command()
    def cli() -> None:
        click.echo("ok")

    cli_result = CliRunner().invoke(cli)
    require(cli_result.exit_code == 0 and cli_result.output.strip() == "ok", "CLI failed")
    records = [json.loads(path.read_text(encoding="utf-8"))
               for path in (Path(sys.prefix) / "conda-meta").glob("*.json")]
    require(bool(records), "No Conda package records found")
    non_forge = [record["name"] for record in records
                 if "conda-forge" not in str(record.get("channel", ""))]
    require(not non_forge, f"Non-conda-forge packages: {non_forge}")
    CONSOLE.print(f"PASS: CLI/configuration and conda-forge-only audit ({len(records)} packages)")

    examples = ROOT / "GIS-files-example"
    if not include_local_data or not examples.is_dir():
        CONSOLE.print("SKIP: optional local Carto samples (not distributed in this repository)")
        CONSOLE.print("ALL CHECKS PASSED")
        return
    for path in sorted((examples / "GeoTIFF").glob("*.tif")):
        with rasterio.open(path) as dataset:
            sample = dataset.read(1, window=rasterio.windows.Window(0, 0, 16, 16),
                                  masked=True)
            require(sample.size > 0 and dataset.count >= 1, f"Cannot read {path.name}")
            CONSOLE.print(f"Carto raster: {path.name}; shape={dataset.shape}; "
                          f"crs={dataset.crs}; pixel_size={dataset.res}")
            if dataset.crs is None:
                CONSOLE.print("WARNING: missing CRS; do not assume WGS84 or reproject blindly")
    for suffix in ("Area_Boundary", "Network_Centerline"):
        for path in sorted((examples / "Shapefile").glob(f"*_{suffix}.shp")):
            info = pyogrio.read_info(path)
            sample = gpd.read_file(path, engine="pyogrio", rows=5)
            with fiona.open(path) as source:
                require(len(source) == info["features"], "Vector engine count mismatch")
            require(len(sample) > 0, f"No features readable in {path.name}")
            CONSOLE.print(f"Carto vector: {path.name}; features={info['features']}; "
                          f"crs={info['crs']}; geometry={info['geometry_type']}")
            if sample.crs is None:
                CONSOLE.print("WARNING: missing CRS; preserve game coordinates and confirm units")
    CONSOLE.print("ALL CHECKS PASSED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic-only", action="store_true",
                        help="Run offline checks without reading local Carto samples")
    arguments = parser.parse_args()
    try:
        main(include_local_data=not arguments.synthetic_only)
    except Exception as error:
        print(f"FAIL: {type(error).__name__}: {error}")
        raise SystemExit(1) from None
