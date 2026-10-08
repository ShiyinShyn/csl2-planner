"""P0-02 explicit CRS/unit normalization and water-face alternative input.
Source files remain read-only. Unknown coverage and absent water depth stay explicit.
"""
# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping

import fiona
import numpy as np
from pyproj import CRS, Transformer, network
from pyproj.exceptions import CRSError, ProjError
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.warp import calculate_default_transform
from rasterio.windows import transform as window_transform
import shapely
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.ops import transform as transform_geometry

from .carto_adapter import (AdapterError, LAND, UNKNOWN, WATER, file_hash, grid_windows,
                            metric_crs, pixel_area, require, shapefile_components, valid_pixels)


from contextlib import contextmanager
from functools import wraps


@contextmanager
def offline_projection_task():
    """Task-local reversible PROJ/GDAL setting; no machine/environment-file changes."""
    previous = network.is_network_enabled()
    network.set_network_enabled(False)
    try:
        with rasterio.Env(GDAL_PAM_ENABLED="NO", PROJ_NETWORK="OFF"):
            yield
    finally:
        network.set_network_enabled(previous)


def offline_task(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with offline_projection_task():
            return function(*args, **kwargs)
    return wrapped


INPUT_ADAPTER_VERSION = "p0-02-inputs-0.1.0"
VERTICAL_FACTORS = {"m": 1.0, "ft": 0.3048, "us-ft": 1200 / 3937}


def explicit_crs(value: Any) -> CRS:
    require(value is not None, "MISSING_CRS", "supply a documented source CRS; do not guess EPSG")
    try:
        crs = CRS.from_user_input(value)
    except (CRSError, ValueError, TypeError) as exc:
        raise AdapterError("INVALID_CRS", str(exc)) from exc
    require(len(crs.axis_info) == 2 and (crs.is_projected or crs.is_geographic),
            "UNSUPPORTED_CRS_DIMENSION", "this adapter requires a two-dimensional projected/geographic CRS")
    return crs


def polygon_input(value: Any, name: str) -> Polygon | MultiPolygon:
    require(isinstance(value, (Polygon, MultiPolygon)) and not value.is_empty and value.is_valid and not value.has_z,
            "INVALID_WATER_GEOMETRY", name + " must be a valid nonempty planar polygon; no silent repair")
    require(np.isfinite(shapely.get_coordinates(value)).all(), "INVALID_WATER_GEOMETRY", name + " must be finite")
    return value


@offline_task
def normalize_polygon(value: Polygon | MultiPolygon, source_crs: Any, target_crs: Any) -> tuple[Any, dict[str, Any]]:
    source = explicit_crs(source_crs)
    target = metric_crs(explicit_crs(target_crs))
    polygon_input(value, "source geometry")
    record = {"source_crs": source.to_string(), "target_crs": target.to_string(),
              "axis_handling": "always_xy", "geometry_repaired": False,
              "source_geometry_sha256": sha256(value.wkb).hexdigest()}
    if source == target:
        record.update({"transformed": False, "densification_source_units": None, "operation": "identity"})
        return value, record
    require(source.geodetic_crs.equals(target.geodetic_crs, ignore_axis_order=True),
            "DATUM_OPERATION_REQUIRED", "cross-datum transformation needs an explicit separately verified operation")
    require(not network.is_network_enabled(), "ONLINE_CRS_CONTEXT", "offline CRS transformation required; no implicit grid downloads")
    if source.is_geographic:
        require(all(math.isclose(axis.unit_conversion_factor, math.pi / 180, rel_tol=0, abs_tol=1e-12)
                    for axis in source.axis_info), "UNSUPPORTED_ANGULAR_UNIT", "geographic source must use declared degrees")
        xmin, ymin, xmax, ymax = value.bounds
        require(-180 <= xmin <= xmax <= 180 and -90 <= ymin <= ymax <= 90,
                "INVALID_TRANSFORM_DOMAIN", "coordinates exceed declared longitude/latitude ranges")
        require(xmax - xmin <= 180, "ANTIMERIDIAN_SPLIT_REQUIRED", "split antimeridian geometry explicitly")
        segment = 0.001
    else:
        factor = source.axis_info[0].unit_conversion_factor
        require(math.isfinite(factor) and factor > 0, "INVALID_HORIZONTAL_UNIT", "documented source unit required")
        segment = 100.0 / factor
    dense = shapely.segmentize(value, segment)
    require(len(shapely.get_coordinates(dense)) <= 500000, "GEOMETRY_RESOURCE_LIMIT", "split large geometry before normalization")
    try:
        transformer = Transformer.from_crs(source, target, always_xy=True, allow_ballpark=False, only_best=True)
        result = transform_geometry(lambda x, y: transformer.transform(x, y, errcheck=True), dense)
    except ProjError as exc:
        raise AdapterError("CRS_TRANSFORM_UNAVAILABLE", str(exc)) from exc
    polygon_input(result, "normalized geometry")
    record.update({"transformed": True, "densification_source_units": segment,
                   "operation": transformer.description, "operation_accuracy_m": transformer.accuracy,
                   "area_basis": "target projected plane; not geodesic/source precision"})
    return result, record


@dataclass(frozen=True)
class TargetGrid:
    width: int
    height: int
    crs: Any
    transform: Affine

    def __post_init__(self) -> None:
        require(type(self.width) is int and type(self.height) is int and self.width > 0 and self.height > 0,
                "INVALID_GRID", "positive integer dimensions required")
        require(self.width * self.height <= 100000000, "GRID_RESOURCE_LIMIT", "P0 prototype limit is 100 million cells")
        metric_crs(explicit_crs(self.crs))
        require(isinstance(self.transform, Affine), "INVALID_TRANSFORM", "explicit affine required")
        pixel_area(self.transform)

    def footprint(self) -> Polygon:
        t = self.transform
        return Polygon([t * (0, 0), t * (self.width, 0), t * (self.width, self.height), t * (0, self.height)])

    def metadata(self) -> dict[str, Any]:
        return {"crs": CRS.from_user_input(self.crs).to_string(), "width": self.width, "height": self.height,
                "transform": list(self.transform)[:6], "pixel_area_m2": pixel_area(self.transform)}


@offline_task
def target_grid(dataset: Any, target_crs: Any = None, resolution_m: float | None = None) -> TargetGrid:
    source = explicit_crs(dataset.crs)
    if target_crs is None:
        require(source.is_projected and all(math.isclose(axis.unit_conversion_factor, 1.0, abs_tol=1e-12, rel_tol=0)
                for axis in source.axis_info), "TARGET_METRIC_CRS_REQUIRED", "choose an explicit metre target for geographic/feet DEM")
        target = source
    else:
        target = metric_crs(explicit_crs(target_crs))
    if resolution_m is not None:
        require(type(resolution_m) in (int, float) and math.isfinite(resolution_m) and resolution_m > 0,
                "INVALID_RESOLUTION", "positive finite metre resolution required")
    if source == target and resolution_m is None:
        return TargetGrid(dataset.width, dataset.height, target, dataset.transform)
    require(source.geodetic_crs.equals(target.geodetic_crs, ignore_axis_order=True),
            "DATUM_OPERATION_REQUIRED", "cross-datum raster normalization needs an explicit verified operation")
    require(not network.is_network_enabled(), "ONLINE_CRS_CONTEXT", "offline raster transformation required")
    try:
        transform, width, height = calculate_default_transform(source, target, dataset.width, dataset.height,
                                                               *dataset.bounds, resolution=resolution_m)
    except rasterio.errors.RasterioError as exc:
        raise AdapterError("CRS_TRANSFORM_UNAVAILABLE", str(exc)) from exc
    return TargetGrid(int(width), int(height), target, transform)


@dataclass(frozen=True)
class WaterFaces:
    geometries: tuple[Polygon | MultiPolygon, ...]
    source_crs: Any
    valid_coverage: Polygon | MultiPolygon
    coverage_crs: Any
    complete_coverage_confirmed: bool = False
    evidence: str = ""

    def __post_init__(self) -> None:
        require(isinstance(self.geometries, tuple), "INVALID_WATER_INPUT", "immutable geometry tuple required")
        explicit_crs(self.source_crs)
        explicit_crs(self.coverage_crs)
        for geometry in self.geometries:
            polygon_input(geometry, "water feature")
        polygon_input(self.valid_coverage, "declared coverage")
        require(type(self.complete_coverage_confirmed) is bool, "INVALID_COVERAGE_ASSERTION", "actual boolean required")
        require(isinstance(self.evidence, str) and bool(self.evidence.strip()), "MISSING_WATER_EVIDENCE", "record source role/coverage evidence")


def read_water_file(path: Path, *, source_crs: Any = None) -> tuple[tuple[Any, ...], CRS, dict[str, Any]]:
    require(path.is_file(), "MISSING_WATER_FILE", "water-face input not found")
    source_files = shapefile_components(path) if path.suffix.lower() == ".shp" else [path]
    before = {item.name: file_hash(item) for item in source_files}
    with fiona.open(path, "r") as layer:
        if layer.driver == "GeoJSON":
            require(source_crs is not None, "EXPLICIT_GEOJSON_CRS_REQUIRED", "declare GeoJSON coordinate semantics, including game coordinates")
            crs = explicit_crs(source_crs)
        else:
            stored = layer.crs_wkt or layer.crs or None
            crs = explicit_crs(source_crs if source_crs is not None else stored)
            if stored and source_crs is not None:
                require(explicit_crs(stored) == crs, "CONFLICTING_SOURCE_CRS", "declared and stored CRS disagree; no relabelling")
        values = []
        for feature in layer:
            require(feature["geometry"] is not None, "INVALID_WATER_GEOMETRY", "null source feature must be diagnosed, not dropped")
            values.append(polygon_input(shape(feature["geometry"]), "source water feature"))
        metadata = {"source_file": path.name, "driver": layer.driver, "feature_count": len(values),
                    "declared_source_crs": crs.to_string(), "stored_crs": str(layer.crs),
                    "geojson_requires_explicit_semantics": layer.driver == "GeoJSON", "source_hashes": before}
    require(before == {item.name: file_hash(item) for item in source_files}, "SOURCE_CHANGED", "water source changed while reading")
    return tuple(values), crs, metadata


@dataclass(frozen=True)
class PreparedWater:
    water_geometry: Any
    coverage_geometry: Any
    complete_coverage_confirmed: bool
    records: tuple[dict[str, Any], ...]
    evidence: str

    def classes(self, grid: TargetGrid, window: Any) -> np.ndarray:
        size = (int(window.height), int(window.width))
        affine = window_transform(window, grid.transform)
        covered = geometry_mask([mapping(self.coverage_geometry)], out_shape=size, transform=affine,
                                invert=True, all_touched=False)
        water = np.zeros(size, dtype=bool) if self.water_geometry.is_empty else geometry_mask(
            [mapping(self.water_geometry)], out_shape=size, transform=affine, invert=True, all_touched=False)
        values = np.full(size, UNKNOWN, dtype="uint8")
        if self.complete_coverage_confirmed:
            values[covered & ~water] = LAND
        values[covered & water] = WATER
        return values


def prepare_water(values: WaterFaces, grid: TargetGrid) -> PreparedWater:
    normalized, records = [], []
    for geometry in values.geometries:
        result, record = normalize_polygon(geometry, values.source_crs, grid.crs)
        normalized.append(result); records.append(record)
    coverage, record = normalize_polygon(values.valid_coverage, values.coverage_crs, grid.crs)
    record["role"] = "valid_coverage"
    records.append(record)
    water = shapely.union_all(normalized)
    require(water.is_empty or (water.is_valid and isinstance(water, (Polygon, MultiPolygon))),
            "INVALID_NORMALIZED_WATER", "normalized water union must remain planar and valid")
    clipped = water.intersection(coverage)
    surface_parts, non_area_types = [], []
    def collect(value):
        if value.is_empty:
            return
        if isinstance(value, Polygon):
            surface_parts.append(value)
        elif isinstance(value, MultiPolygon) or value.geom_type == "GeometryCollection":
            for part in value.geoms:
                collect(part)
        else:
            non_area_types.append(value.geom_type)
    collect(clipped)
    normalized_water = shapely.union_all(surface_parts)
    records.append({"role": "coverage_clip", "outside_coverage_area_m2": max(0.0, float(water.area - clipped.area)),
                    "zero_area_clip_types": non_area_types, "source_geometry_repaired": False})
    return PreparedWater(normalized_water, coverage, values.complete_coverage_confirmed, tuple(records), values.evidence)


@offline_task
def analyze_water_faces(dataset: Any, water: WaterFaces, *, grid: TargetGrid | None = None,
                        vertical_unit: str | None = None, vertical_reference: str | None = None,
                        output_dir: Path | None = None, source_paths: tuple[Path, ...] = ()) -> dict[str, Any]:
    """Stream DEM/windows and return explicit water/land/unknown + depth capability."""
    require(vertical_unit in VERTICAL_FACTORS, "MISSING_VERTICAL_UNIT", "declare m, ft or us-ft; horizontal CRS does not prove elevation units")
    require(vertical_reference is None or (isinstance(vertical_reference, str) and bool(vertical_reference.strip())),
            "INVALID_VERTICAL_REFERENCE", "explicit nonempty reference or unknown required")
    require(dataset.count == 1, "UNSUPPORTED_BANDS", "explicit single DEM band required")
    source_crs = explicit_crs(dataset.crs)
    grid = target_grid(dataset) if grid is None else grid
    require(source_crs.geodetic_crs.equals(CRS.from_user_input(grid.crs).geodetic_crs, ignore_axis_order=True),
            "DATUM_OPERATION_REQUIRED", "cross-datum raster normalization needs an explicit verified operation")
    prepared = prepare_water(water, grid)
    before = {str(path.resolve()): file_hash(path) for path in source_paths}
    if output_dir is None:
        return _run_windows(dataset, grid, prepared, vertical_unit, vertical_reference, None, before, source_paths)
    destination = output_dir.resolve()
    require(not destination.exists(), "OUTPUT_EXISTS", "create a new derived package; never overwrite previous outputs")
    for source in source_paths:
        parent = source.resolve().parent
        require(destination != source.resolve() and destination != parent and parent not in destination.parents,
                "OUTPUT_IN_SOURCE_DIRECTORY", "derived package must stay outside raw source directories")
    destination.parent.mkdir(parents=True, exist_ok=True)
    intended_parent = destination.parent.resolve()
    with TemporaryDirectory(prefix=destination.name + "_staging_", dir=intended_parent) as directory:
        staging = Path(directory).resolve()
        require(staging.parent == intended_parent and staging != intended_parent and destination.parent == intended_parent,
                "INVALID_OUTPUT_PATH", "verified staging and final paths must stay within named output parent")
        report = _run_windows(dataset, grid, prepared, vertical_unit, vertical_reference, staging, before, source_paths)
        # All GDAL/Fiona handles are closed; same-parent directory rename is atomic.
        require(not destination.exists() and staging.parent == intended_parent, "OUTPUT_EXISTS", "output changed during normalization")
        staging.rename(destination)
    return report


def _run_windows(dataset: Any, grid: TargetGrid, water: PreparedWater, unit: str,
                 reference: str | None, directory: Path | None, source_hashes: dict[str, str],
                 source_paths: tuple[Path, ...]) -> dict[str, Any]:
    from contextlib import ExitStack
    require(not network.is_network_enabled(), "ONLINE_CRS_CONTEXT", "offline normalization required")
    totals = np.zeros(3, dtype="int64")
    terrain_valid, minimum, maximum = 0, math.inf, -math.inf
    with ExitStack() as stack:
        stack.enter_context(rasterio.Env(GDAL_PAM_ENABLED="NO"))
        dem = stack.enter_context(WarpedVRT(dataset, crs=grid.crs, transform=grid.transform,
                                  width=grid.width, height=grid.height, dtype="float64",
                                  src_nodata=dataset.nodata, nodata=np.nan, resampling=Resampling.bilinear))
        terrain_output = mask_output = None
        if directory is not None:
            profile = {"driver": "GTiff", "width": grid.width, "height": grid.height,
                       "count": 1, "crs": grid.crs, "transform": grid.transform,
                       "tiled": True, "blockxsize": 256, "blockysize": 256, "compress": "deflate"}
            terrain_output = stack.enter_context(rasterio.open(directory / "terrain-normalized.tif", "w", dtype="float64", nodata=np.nan, **profile))
            terrain_output.set_band_unit(1, "m")
            terrain_output.update_tags(vertical_reference=reference or "unknown", adapter_version=INPUT_ADAPTER_VERSION)
            mask_output = stack.enter_context(rasterio.open(directory / "land-water-mask.tif", "w", dtype="uint8", **profile))
            mask_output.update_tags(class_0="unknown", class_1="land", class_2="water", classification="pixel centre, all_touched=False")
        for window in grid_windows(grid.width, grid.height):
            values = dem.read(1, window=window)
            valid = valid_pixels(values, dem.read_masks(1, window=window), dem.nodata)
            values = np.where(valid, values * VERTICAL_FACTORS[unit], np.nan)
            classes = water.classes(grid, window)
            totals += np.bincount(classes.ravel(), minlength=3)
            terrain_valid += int(valid.sum())
            if valid.any():
                minimum = min(minimum, float(values[valid].min())); maximum = max(maximum, float(values[valid].max()))
            if terrain_output is not None:
                terrain_output.write(values, 1, window=window); mask_output.write(classes, 1, window=window)
    cells = grid.width * grid.height
    require(int(totals.sum()) == cells, "AREA_CONSERVATION", "water/land/unknown must partition target ROI")
    require(source_hashes == {str(path.resolve()): file_hash(path) for path in source_paths}, "SOURCE_CHANGED", "raw inputs changed during normalization")
    diagnostics = []
    if totals[UNKNOWN]: diagnostics.append("WATER_COVERAGE_UNKNOWN")
    if terrain_valid != cells: diagnostics.append("TERRAIN_MISSING")
    if reference is None: diagnostics.append("VERTICAL_REFERENCE_UNCONFIRMED")
    area = pixel_area(grid.transform)
    report = {"adapter_version": INPUT_ADAPTER_VERSION, "grid": grid.metadata(),
              "normalization_ready": not diagnostics, "blocking_diagnostics": diagnostics,
              "counts": {"water": int(totals[WATER]), "land": int(totals[LAND]), "unknown": int(totals[UNKNOWN])},
              "area_m2": {"water": float(totals[WATER]*area), "land": float(totals[LAND]*area),
                          "unknown": float(totals[UNKNOWN]*area), "roi": float(cells*area)},
              "pixel_conservation_passed": True, "terrain_valid_pixels": terrain_valid,
              "terrain_missing_pixels": cells - terrain_valid,
              "terrain_min_m": minimum if terrain_valid else None, "terrain_max_m": maximum if terrain_valid else None,
              "source_vertical_unit": unit, "vertical_factor_to_m": VERTICAL_FACTORS[unit],
              "vertical_reference": reference or "unknown", "vertical_datum_transformed": False,
              "resampling": "bilinear DEM; polygon classification at pixel centres",
              "geometry_transform_records": list(water.records), "coverage_evidence": water.evidence,
              "water_depth_available": False, "water_depth_feasibility_status": "资料不足",
              "source_hashes": source_hashes, "source_unchanged": True,
              "game_mapping_transfer_automatically_confirmed": False,
              "limitations": ["metre plane area, no geodesic precision claim", "reprojection does not add source detail",
                              "unit scaling is not geoid/vertical datum conversion", "no inferred depth or physical feasibility"]}
    if directory is not None:
        with fiona.open(directory / "water-extent.gpkg", "w", driver="GPKG", layer="water_extent",
                        crs_wkt=CRS.from_user_input(grid.crs).to_wkt(),
                        schema={"geometry": "MultiPolygon", "properties": {"role": "str"}}) as output:
            if not water.water_geometry.is_empty:
                geometry = MultiPolygon([water.water_geometry]) if isinstance(water.water_geometry, Polygon) else water.water_geometry
                output.write({"geometry": mapping(geometry), "properties": {"role": "water_extent"}})
        report["artifacts"] = ["terrain-normalized.tif", "land-water-mask.tif", "water-extent.gpkg", "manifest.json"]
        report["artifact_sha256"] = {path.name: file_hash(path) for path in directory.iterdir() if path.is_file()}
        (directory / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return report
