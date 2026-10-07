"""P0-02 read-only Carto adapter; not a GUI, network solver or 3D checker."""
# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import math
from pathlib import Path
from typing import Any, Iterable

import fiona
import numpy as np
from pyproj import CRS
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window, transform as window_transform
from shapely.geometry import Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry

from .contracts import PlanningZone

ADAPTER_VERSION = "p0-02-0.2.0"
UNKNOWN, LAND, WATER, UNCERTAIN = 0, 1, 2, 3
FORM_MAP = {"Normal": "SURFACE", "Elevated": "ELEVATED", "Tunnel": "TUNNEL"}


class AdapterError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise AdapterError(code, message)


def metric_crs(value: Any) -> CRS:
    require(value is not None, "MISSING_CRS", "explicit CRS is required")
    try:
        crs = CRS.from_user_input(value)
    except (ValueError, TypeError) as exc:
        raise AdapterError("INVALID_CRS", str(exc)) from exc
    require(crs.is_projected and len(crs.axis_info) >= 2,
            "NON_METRIC_CRS", "analysis requires a projected metre CRS")
    require(all(math.isclose(axis.unit_conversion_factor, 1.0, rel_tol=0, abs_tol=1e-12)
                for axis in crs.axis_info[:2]), "NON_METRIC_CRS", "convert horizontal units explicitly")
    return crs


def pixel_area(transform: Any) -> float:
    values = tuple(transform)[:6]
    require(all(math.isfinite(v) for v in values), "INVALID_TRANSFORM", "finite affine required")
    area = abs(transform.a * transform.e - transform.b * transform.d)
    require(area > 0, "INVALID_TRANSFORM", "singular affine")
    return area


def nodata_pixels(values: np.ndarray, nodata: float | None) -> np.ndarray:
    if nodata is None:
        return np.zeros(values.shape, dtype=bool)
    if math.isnan(nodata):
        return np.isnan(values)
    # Compare against the value encoded in the source dtype, not a decimal string.
    if np.issubdtype(values.dtype, np.integer):
        limits = np.iinfo(values.dtype)
        if not limits.min <= nodata <= limits.max or nodata != int(nodata):
            return np.zeros(values.shape, dtype=bool)
    with np.errstate(over="ignore", invalid="ignore"):
        encoded = np.asarray(nodata, dtype=values.dtype).item()
    return values == encoded


def valid_pixels(values: np.ndarray, mask: np.ndarray, nodata: float | None) -> np.ndarray:
    require(values.shape == mask.shape, "MASK_SHAPE", "band and mask must align")
    return (mask > 0) & np.isfinite(values) & ~nodata_pixels(values, nodata)


@dataclass(frozen=True)
class DepthSemantics:
    """Explicit source assertions; defaults keep missing Depth values unknown."""
    carto_nodata_is_land: bool = False
    complete_coverage_confirmed: bool = False

    def __post_init__(self) -> None:
        require(type(self.carto_nodata_is_land) is bool and
                type(self.complete_coverage_confirmed) is bool, "INVALID_SEMANTICS", "booleans required")
        require(not self.carto_nodata_is_land or self.complete_coverage_confirmed,
                "UNCONFIRMED_COVERAGE", "Carto land interpretation requires confirmed coverage")


def classify_depth(values: np.ndarray, mask: np.ndarray, coverage: np.ndarray,
                   nodata: float | None, semantics: DepthSemantics = DepthSemantics(),
                   near_zero_m: float = 0.0) -> np.ndarray:
    require(values.shape == coverage.shape, "MASK_SHAPE", "coverage must align")
    require(math.isfinite(near_zero_m) and near_zero_m >= 0,
            "INVALID_THRESHOLD", "near-zero band must be finite and nonnegative")
    valid = valid_pixels(values, mask, nodata) & coverage
    result = np.full(values.shape, UNKNOWN, dtype=np.uint8)
    result[valid & (values > near_zero_m)] = WATER
    result[valid & (np.abs(values) <= near_zero_m)] = UNCERTAIN
    if semantics.carto_nodata_is_land:
        result[coverage & nodata_pixels(values, nodata)] = LAND
    return result


def footprint(dataset: Any) -> Polygon:
    t = dataset.transform
    return Polygon([t * (0, 0), t * (dataset.width, 0),
                    t * (dataset.width, dataset.height), t * (0, dataset.height)])


def grid_windows(width: int, height: int, size: int = 256) -> Iterable[Window]:
    require(size > 0, "INVALID_WINDOW", "positive window size required")
    for row in range(0, height, size):
        for col in range(0, width, size):
            yield Window(col, row, min(size, width - col), min(size, height - row))


def raster_metadata(dataset: Any) -> dict[str, Any]:
    crs = metric_crs(dataset.crs)
    require(dataset.count == 1, "UNSUPPORTED_BANDS", "select an explicit single-band source")
    return {"driver": dataset.driver, "shape": list(dataset.shape),
            "dtype": dataset.dtypes[0], "crs": crs.to_string(),
            "horizontal_units": [axis.unit_name for axis in crs.axis_info[:2]],
            "transform": list(dataset.transform)[:6], "bounds": list(dataset.bounds),
            "pixel_area_m2": pixel_area(dataset.transform), "nodata": dataset.nodata,
            "band_units": list(dataset.units), "vertical_datum": "unknown",
            "game_coordinate_mapping": "unknown"}


def scan_raster(dataset: Any) -> dict[str, Any]:
    info = raster_metadata(dataset)
    valid_count, finite_count, zero_count, negative_count = 0, 0, 0, 0
    minimum, maximum = math.inf, -math.inf
    for window in grid_windows(dataset.width, dataset.height):
        values = dataset.read(1, window=window)
        valid = valid_pixels(values, dataset.read_masks(1, window=window), dataset.nodata)
        finite_count += int(np.count_nonzero(np.isfinite(values)))
        valid_count += int(valid.sum())
        zero_count += int(np.count_nonzero(valid & (values == 0)))
        negative_count += int(np.count_nonzero(valid & (values < 0)))
        if valid.any():
            minimum = min(minimum, float(values[valid].min()))
            maximum = max(maximum, float(values[valid].max()))
    info.update({"scan": "all pixels in bounded 256x256 windows", "valid_pixels": valid_count,
                 "finite_pixels": finite_count, "valid_zero_pixels": zero_count,
                 "valid_negative_pixels": negative_count,
                 "valid_min": minimum if valid_count else None,
                 "valid_max": maximum if valid_count else None})
    return info


def align_depth(terrain: Any, depth: Any,
                semantics: DepthSemantics = DepthSemantics()) -> dict[str, Any]:
    terrain_info, depth_info = raster_metadata(terrain), raster_metadata(depth)
    require(metric_crs(terrain.crs) == metric_crs(depth.crs),
            "CRS_TRANSFORM_REQUIRED", "this prototype requires explicitly matching source CRSs")
    # A declared land rule must not reinterpret arbitrary explicit masks as dry land.
    if semantics.carto_nodata_is_land:
        require(all(flag.name in {"nodata", "all_valid"} for flag in depth.mask_flag_enums[0]),
                "UNSUPPORTED_SOURCE_MASK", "confirm explicit mask semantics separately")
    totals = np.zeros(4, dtype=np.int64)
    terrain_missing, depth_outside = 0, 0
    source_footprint = mapping(footprint(depth))
    dst_nodata = depth.nodata if depth.nodata is not None else np.nan
    with WarpedVRT(depth, crs=terrain.crs, transform=terrain.transform,
                   width=terrain.width, height=terrain.height, resampling=Resampling.nearest,
                   src_nodata=depth.nodata, nodata=dst_nodata, dtype="float32") as aligned:
        for window in grid_windows(terrain.width, terrain.height):
            values = aligned.read(1, window=window)
            mask = aligned.read_masks(1, window=window)
            coverage = geometry_mask([source_footprint], out_shape=values.shape,
                                     transform=window_transform(window, terrain.transform),
                                     invert=True, all_touched=False)
            classes = classify_depth(values, mask, coverage, dst_nodata, semantics)
            totals += np.bincount(classes.ravel(), minlength=4)
            terrain_values = terrain.read(1, window=window)
            terrain_valid = valid_pixels(terrain_values, terrain.read_masks(1, window=window), terrain.nodata)
            terrain_missing += int(np.count_nonzero(~terrain_valid))
            depth_outside += int(np.count_nonzero(~coverage))
    expected = terrain.width * terrain.height
    require(int(totals.sum()) == expected, "AREA_CONSERVATION", "classification must partition target grid")
    area = terrain_info["pixel_area_m2"]
    return {"target": terrain_info, "source": depth_info, "resampling": "nearest",
            "scope": "complete DEM footprint; nearest-neighbour Depth on DEM grid",
            "assertions": {"carto_nodata_is_land": semantics.carto_nodata_is_land,
                           "complete_coverage_confirmed": semantics.complete_coverage_confirmed},
            "counts": {"water": int(totals[WATER]), "land": int(totals[LAND]),
                       "unknown": int(totals[UNKNOWN] + totals[UNCERTAIN]),
                       "near_zero_uncertain": int(totals[UNCERTAIN])},
            "area_m2": {"water": float(totals[WATER] * area), "land": float(totals[LAND] * area),
                        "unknown": float((totals[UNKNOWN] + totals[UNCERTAIN]) * area),
                        "roi": float(expected * area)},
            "terrain_missing_pixels": terrain_missing, "depth_outside_pixels": depth_outside,
            "pixel_conservation_passed": True,
            "classification_complete": int(totals[UNKNOWN] + totals[UNCERTAIN]) == 0 and terrain_missing == 0,
            "limitations": ["NoData remains unknown without source/coverage confirmation",
                            "nearest target-grid area is not native-grid water area",
                            "vertical units/datum and game mapping are not confirmed",
                            "water polygons and general reprojection are not implemented"]}


def source_tokens(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(sorted({part.strip() for part in str(value).split(",") if part.strip()}))


def normalize_network(properties: dict[str, Any]) -> dict[str, Any]:
    forms = source_tokens(properties.get("Form"))
    normalized_form = FORM_MAP.get(forms[0]) if len(forms) == 1 else None
    objects = source_tokens(properties.get("Object"))
    categories = source_tokens(properties.get("Category"))
    # Tram lanes do not remove the road itself; road freight remains allowed.
    excluded = bool(set(objects) & {"Pathway", "Taxiway", "Runway", "Tram"}) or (
        "Track" in objects and bool(set(categories) & {"Tram", "Cargo"}))
    return {"source_object": objects, "source_category": categories, "source_form": forms,
            "form": normalized_form, "direction_source": properties.get("Direction"),
            "lanes_source": properties.get("Lane"), "speed_source": properties.get("Limit"),
            "elevation_source": properties.get("Elevation"),
            "road_candidate": objects == ("Road",) and not excluded,
            "excluded_from_computation": excluded,
            "topology_verified": False, "vertical_geometry_verified": False,
            "diagnostics": ([] if normalized_form else ["UNKNOWN_OR_AMBIGUOUS_FORM"])}


def associate_geometry(geometry: BaseGeometry, zones: tuple[PlanningZone, ...]) -> dict[str, Any]:
    require(not geometry.is_empty and geometry.is_valid and not geometry.has_z,
            "INVALID_GEOMETRY", "valid nonempty planar geometry required")
    require(all(isinstance(zone, PlanningZone) for zone in zones),
            "INVALID_ZONES", "use the canonical PlanningZone contract")
    matches = tuple(zone for zone in zones if geometry.intersects(zone.geometry))
    return {"zone_ids": tuple(sorted(zone.zone_id for zone in matches)),
            "allow_demolition": bool(matches) and all(zone.allow_demolition for zone in matches),
            "association_status": "matched" if matches else "待关联",
            "foundation_bottom_z_m": None, "ground_z_m": None}


def file_hash(path: Path) -> str:
    result = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def shapefile_components(path: Path) -> list[Path]:
    required = [path.with_suffix(suffix) for suffix in (".shp", ".shx", ".dbf")]
    require(all(p.is_file() for p in required), "MISSING_SHAPEFILE_COMPONENT", "shp/shx/dbf set required")
    return sorted(p for p in path.parent.glob(path.stem + ".*") if p.is_file())


def scan_vector(path: Path, zones: tuple[PlanningZone, ...] = ()) -> dict[str, Any]:
    shapefile_components(path)
    cpg = path.with_suffix(".cpg")
    encoding = cpg.read_text(encoding="utf-8").strip() if cpg.exists() else None
    counters: Counter[str] = Counter()
    issues: list[dict[str, str]] = []
    values: dict[str, Counter[str]] = {key: Counter() for key in ("Object", "Category", "Form", "Direction")}
    with fiona.open(path, mode="r", encoding=encoding) as source:
        crs = metric_crs(source.crs_wkt or source.crs or None)
        fields = dict(source.schema["properties"])
        for feature in source:
            counters["records_read"] += 1
            props = dict(feature["properties"])
            for key, counts in values.items():
                for token in source_tokens(props.get(key)):
                    counts[token] += 1
            if feature["geometry"] is None:
                counters["null_geometry"] += 1
                issues.append({"source_record_id": str(feature.id), "code": "null_geometry"})
                continue
            geom = shape(feature["geometry"])
            if geom.is_empty:
                counters["empty_geometry"] += 1
                issues.append({"source_record_id": str(feature.id), "code": "empty_geometry"})
                continue
            if not geom.is_valid:
                counters["invalid_geometry"] += 1
                issues.append({"source_record_id": str(feature.id), "code": "invalid_geometry"})
                continue
            if geom.has_z:
                counters["z_geometry_requires_adapter"] += 1
                issues.append({"source_record_id": str(feature.id), "code": "z_geometry_requires_adapter"})
                continue
            counters["valid_planar_geometry"] += 1
            if "Network" in path.stem:
                normalized = normalize_network(props)
                counters["form_" + (normalized["form"] or "unknown")] += 1
                if normalized["excluded_from_computation"]:
                    counters["excluded_background"] += 1
            if "Building" in path.stem or "POI" in path.stem:
                association = associate_geometry(geom, zones)
                counters["associated" if association["zone_ids"] else "pending_association"] += 1
                counters["foundation_unknown"] += 1
        return {"file": path.name, "driver": source.driver, "declared_encoding": encoding or "unknown",
                "crs": crs.to_string(), "schema_geometry": source.schema["geometry"], "fields": fields,
                "declared_records": len(source), "counts": dict(sorted(counters.items())),
                "categorical_counts": {key: dict(sorted(counts.items())) for key, counts in values.items() if counts},
                "repairs_written": False, "topology_verified": False,
                "stable_game_ids_verified": False, "property_encoding_validation": "Fiona decode only; not strict DBF byte audit", "quarantined_features": issues, "geometry_scan_complete": True}


def validate_sample(sample_dir: Path, *, source_assertions: Any = None,
                    geometry_review: bool = False) -> dict[str, Any]:
    from .carto_quality import SourceAssertions, assess_readiness, review_boundary_layer
    assertions = SourceAssertions() if source_assertions is None else source_assertions
    require(isinstance(assertions, SourceAssertions), "INVALID_SOURCE_ASSERTION", "typed source assertions required")
    require(type(geometry_review) is bool, "INVALID_REVIEW_FLAG", "boolean review flag required")
    require(sample_dir.is_dir(), "MISSING_SAMPLE", "provide an existing local Carto sample")
    rasters = sorted(sample_dir.rglob("*.tif"))
    terrain_paths = [p for p in rasters if p.stem.endswith("_Elevation")]
    depth_paths = [p for p in rasters if p.stem.endswith("_Depth")]
    require(len(terrain_paths) == 1 and len(depth_paths) == 1,
            "AMBIGUOUS_INPUT", "this prototype needs one explicit Elevation/Depth pair")
    vectors = sorted(sample_dir.rglob("*.shp"))
    inputs = set(rasters)
    for path in rasters:
        inputs.update(p for p in path.parent.glob(path.name + ".*") if p.is_file())
    for path in vectors:
        inputs.update(shapefile_components(path))
    original_file_set = {str(path.relative_to(sample_dir)) for path in sample_dir.rglob('*') if path.is_file()}
    before = {str(path.relative_to(sample_dir)): file_hash(path) for path in sorted(inputs)}
    semantics = DepthSemantics(assertions.carto_source_confirmed and assertions.complete_depth_coverage_confirmed,
                              assertions.complete_depth_coverage_confirmed)
    with rasterio.Env(GDAL_PAM_ENABLED="NO"):
        with rasterio.open(terrain_paths[0], "r") as terrain, rasterio.open(depth_paths[0], "r") as depth:
            terrain_result, depth_result = scan_raster(terrain), scan_raster(depth)
            alignment = align_depth(terrain, depth, semantics)
        vector_results = [scan_vector(path) for path in vectors]
        reviews = [review_boundary_layer(path) for path in vectors if "Network_Boundary" in path.stem] if geometry_review else []
    after = {str(path.relative_to(sample_dir)): file_hash(path) for path in sorted(inputs)}
    require(before == after, "SOURCE_CHANGED", "read-only source hashes changed")
    final_file_set = {str(path.relative_to(sample_dir)) for path in sample_dir.rglob('*') if path.is_file()}
    require(original_file_set == final_file_set, "SOURCE_CHANGED", "source file inventory changed")
    actual = {"adapter_version": ADAPTER_VERSION, "source_hashes": before, "source_unchanged": True,
              "terrain": terrain_result, "depth": depth_result, "alignment": alignment,
              "vectors": vector_results, "geometry_reviews": reviews,
              "zone_source": "none: actual zone generation belongs to M1 or explicit zone import",
              "game_version": "unknown", "carto_version": "unknown"}
    actual["readiness"] = assess_readiness(actual, assertions)
    actual["planning_ready"] = actual["readiness"]["planning_ready"]
    actual["remaining"] = [item["code"] for item in actual["readiness"]["blocking_diagnostics"]]
    return actual
