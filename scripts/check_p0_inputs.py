"""Run: conda run -n csl2-planner --no-capture-output python -s scripts/check_p0_inputs.py
Expected: ALL P0-02 INPUT CHECKS PASSED; default fixtures are self-contained/offline.
Optional --sample-dir uses 3 bounded DEM/Depth windows to derive water-face fixtures;
--profile imports an explicitly declared external water-face/DEM pair. Raw sources
are read-only; derived packages and reports must be outside raw directories.
"""
# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_p0_mapping import CartoChecks, ProjectionChecks, MappingChecks, VersionBindingChecks
import fiona
import numpy as np
from pyproj import Transformer
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import Affine, from_origin
from rasterio.windows import Window, bounds as window_bounds, transform as window_transform
import shapely
from shapely.geometry import Polygon, MultiPolygon, box, mapping
from shapely.ops import transform as transform_geometry

from prototypes.p0.carto_adapter import AdapterError, file_hash, valid_pixels
from prototypes.p0.gis_inputs import (TargetGrid, WaterFaces, analyze_water_faces, explicit_crs,
    normalize_polygon, prepare_water, read_water_file, target_grid)

FEET_CRS = "+proj=tmerc +lat_0=0 +lon_0=3 +k=0.9996 +x_0=500000 +y_0=0 +datum=WGS84 +units=us-ft +no_defs"


class WaterInputChecks(unittest.TestCase):
    def terrain(self, data=None, *, transform=None, crs="EPSG:32631", nodata=-9999):
        values = np.ones((4, 4), dtype="float32") if data is None else data
        memory = MemoryFile()
        self.addCleanup(memory.close)
        with memory.open(driver="GTiff", width=values.shape[1], height=values.shape[0], count=1,
                         dtype=values.dtype, crs=crs, transform=transform or from_origin(0, 4, 1, 1), nodata=nodata) as dst:
            dst.write(values, 1)
        dataset = memory.open()
        self.addCleanup(dataset.close)
        return dataset

    def water(self, geometries=None, *, coverage=None, complete=True, crs="EPSG:32631"):
        return WaterFaces((box(0, 0, 2, 4),) if geometries is None else tuple(geometries), crs,
                          box(0, 0, 4, 4) if coverage is None else coverage, crs, complete, "explicit synthetic source/coverage")

    def analyze(self, terrain, water, **kwargs):
        return analyze_water_faces(terrain, water, vertical_unit=kwargs.pop("vertical_unit", "m"),
                                   vertical_reference=kwargs.pop("vertical_reference", "game_origin"), **kwargs)

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(AdapterError) as ctx:
            function(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)

    def test_complete_water_faces_partition_roi(self):
        result = self.analyze(self.terrain(), self.water())
        self.assertEqual(result["counts"], {"water": 8, "land": 8, "unknown": 0})
        self.assertTrue(result["normalization_ready"])
        self.assertEqual(result["area_m2"]["roi"], 16)

    def test_partial_coverage_remains_unknown(self):
        result = self.analyze(self.terrain(), self.water((box(0, 0, 1, 4),), coverage=box(0, 0, 2, 4)))
        self.assertEqual(result["counts"], {"water": 4, "land": 4, "unknown": 8})
        self.assertFalse(result["normalization_ready"])

    def test_unconfirmed_nonwater_is_not_land(self):
        result = self.analyze(self.terrain(), self.water(complete=False))
        self.assertEqual(result["counts"], {"water": 8, "land": 0, "unknown": 8})

    def test_empty_confirmed_layer_means_known_no_water(self):
        result = self.analyze(self.terrain(), self.water(()))
        self.assertEqual(result["counts"], {"water": 0, "land": 16, "unknown": 0})

    def test_empty_unconfirmed_layer_stays_unknown(self):
        result = self.analyze(self.terrain(), self.water((), complete=False))
        self.assertEqual(result["counts"]["unknown"], 16)

    def test_water_faces_do_not_invent_depth(self):
        result = self.analyze(self.terrain(), self.water())
        self.assertFalse(result["water_depth_available"])
        self.assertEqual(result["water_depth_feasibility_status"], "资料不足")

    def test_missing_source_crs_is_rejected(self):
        self.error("MISSING_CRS", WaterFaces, (box(0, 0, 1, 1),), None, box(0, 0, 4, 4), "EPSG:32631", True, "fixture")

    def test_missing_coverage_crs_is_rejected(self):
        self.error("MISSING_CRS", WaterFaces, (), "EPSG:32631", box(0, 0, 4, 4), None, True, "fixture")

    def test_geographic_target_is_not_metres(self):
        self.error("NON_METRIC_CRS", normalize_polygon, box(0, 0, 1, 1), "EPSG:32631", "EPSG:4326")

    def test_invalid_water_polygon_is_not_silently_repaired(self):
        invalid = Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])
        before = invalid.wkb
        self.error("INVALID_WATER_GEOMETRY", self.water, (invalid,))
        self.assertEqual(before, invalid.wkb)

    def test_z_polygon_is_not_flattened(self):
        geometry = Polygon([(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 0, 1)])
        self.error("INVALID_WATER_GEOMETRY", self.water, (geometry,))

    def test_missing_vertical_unit_is_rejected(self):
        self.error("MISSING_VERTICAL_UNIT", analyze_water_faces, self.terrain(), self.water(), vertical_reference="game_origin")

    def test_vertical_feet_are_scaled_to_metres(self):
        result = self.analyze(self.terrain(np.full((4, 4), 100, dtype="float32")), self.water(), vertical_unit="ft")
        self.assertAlmostEqual(result["terrain_min_m"], 30.48)
        self.assertFalse(result["vertical_datum_transformed"])

    def test_zero_negative_heights_remain_valid(self):
        values = np.ones((4, 4), dtype="float32")
        values[0, 0] = 0; values[1, 1] = -2
        result = self.analyze(self.terrain(values), self.water())
        self.assertEqual(result["terrain_valid_pixels"], 16)
        self.assertEqual(result["terrain_min_m"], -2)

    def test_dem_nodata_is_separate_from_water_class(self):
        values = np.ones((4, 4), dtype="float32"); values[0, 0] = -9999
        result = self.analyze(self.terrain(values), self.water())
        self.assertEqual(result["terrain_missing_pixels"], 1)
        self.assertEqual(result["counts"]["unknown"], 0)
        self.assertFalse(result["normalization_ready"])

    def test_missing_vertical_reference_is_reported(self):
        result = self.analyze(self.terrain(), self.water(), vertical_reference=None)
        self.assertIn("VERTICAL_REFERENCE_UNCONFIRMED", result["blocking_diagnostics"])

    def test_geographic_water_transforms_with_explicit_xy(self):
        inverse = Transformer.from_crs("EPSG:32631", "EPSG:4326", always_xy=True)
        water = transform_geometry(inverse.transform, box(0, 0, 2, 4))
        coverage = transform_geometry(inverse.transform, box(0, 0, 4, 4))
        result = self.analyze(self.terrain(), WaterFaces((water,), "EPSG:4326", coverage, "EPSG:4326", True, "synthetic WGS84 x/y"))
        self.assertEqual(result["counts"], {"water": 8, "land": 8, "unknown": 0})
        self.assertTrue(result["geometry_transform_records"][0]["transformed"])

    def test_horizontal_us_feet_are_converted_by_crs(self):
        inverse = Transformer.from_crs("EPSG:32631", FEET_CRS, always_xy=True)
        water = transform_geometry(inverse.transform, box(0, 0, 2, 4))
        coverage = transform_geometry(inverse.transform, box(0, 0, 4, 4))
        result = self.analyze(self.terrain(), WaterFaces((water,), FEET_CRS, coverage, FEET_CRS, True, "synthetic feet CRS"))
        self.assertEqual(result["counts"]["water"], 8)

    def test_geographic_dem_needs_explicit_target(self):
        dataset = self.terrain(crs="EPSG:4326", transform=from_origin(0, 0.0004, 0.0001, 0.0001))
        self.error("TARGET_METRIC_CRS_REQUIRED", target_grid, dataset)

    def test_geographic_dem_can_use_declared_metric_target(self):
        dataset = self.terrain(np.full((4, 4), 5, dtype="float32"), crs="EPSG:4326", transform=from_origin(0, 0.0004, 0.0001, 0.0001))
        grid = target_grid(dataset, "EPSG:32631")
        water = WaterFaces((), "EPSG:4326", box(0, 0, 0.0004, 0.0004), "EPSG:4326", True, "synthetic geographic DEM")
        result = self.analyze(dataset, water, grid=grid)
        self.assertGreater(result["terrain_valid_pixels"], 0)
        self.assertAlmostEqual(result["terrain_min_m"], 5)
        self.assertEqual(result["grid"]["crs"], "EPSG:32631")

    def test_cross_datum_needs_separate_operation(self):
        self.error("DATUM_OPERATION_REQUIRED", normalize_polygon, box(-1, 50, 0, 51), "EPSG:4277", "EPSG:32631")

    def test_declared_resolution_is_not_inferred_precision(self):
        grid = target_grid(self.terrain(), "EPSG:32631", 2)
        self.assertEqual((grid.width, grid.height), (2, 2))
        self.assertEqual(grid.transform.a, 2)

    def test_geojson_crs_cannot_be_silently_assumed(self):
        with TemporaryDirectory() as directory:
            file = Path(directory) / "water.geojson"
            file.write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
            self.error("EXPLICIT_GEOJSON_CRS_REQUIRED", read_water_file, file)

    def test_geojson_reads_declared_game_plane_without_source_rewrite(self):
        with TemporaryDirectory() as directory:
            file = Path(directory) / "water.geojson"
            file.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": mapping(box(0, 0, 2, 4))}]}), encoding="utf-8")
            before = file.read_bytes()
            values, crs, metadata = read_water_file(file, source_crs="EPSG:32631")
            self.assertEqual(crs.to_epsg(), 32631)
            self.assertEqual(len(values), 1)
            self.assertEqual(file.read_bytes(), before)

    def test_null_water_feature_is_not_dropped(self):
        with TemporaryDirectory() as directory:
            file = Path(directory) / "water.geojson"
            file.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": None}]}), encoding="utf-8")
            self.error("INVALID_WATER_GEOMETRY", read_water_file, file, source_crs="EPSG:32631")

    def test_string_coverage_boolean_is_rejected(self):
        self.error("INVALID_COVERAGE_ASSERTION", self.water, complete="true")

    def test_coverage_evidence_is_required(self):
        self.error("MISSING_WATER_EVIDENCE", WaterFaces, (), "EPSG:32631", box(0, 0, 4, 4), "EPSG:32631", True, "")

    def test_overlapping_water_faces_are_counted_once(self):
        result = self.analyze(self.terrain(), self.water((box(0, 0, 2, 4), box(1, 0, 3, 4))))
        self.assertEqual(result["counts"]["water"], 12)

    def test_outside_coverage_is_clipped_with_trace(self):
        result = self.analyze(self.terrain(), self.water((box(0, 0, 4, 4),), coverage=box(0, 0, 2, 4)))
        self.assertEqual(result["counts"], {"water": 8, "land": 0, "unknown": 8})
        self.assertEqual(result["geometry_transform_records"][-1]["outside_coverage_area_m2"], 8)

    def test_antimeridian_geometry_requires_explicit_split(self):
        self.error("ANTIMERIDIAN_SPLIT_REQUIRED", normalize_polygon, box(-170, -1, 170, 1), "EPSG:4326", "EPSG:32631")

    def test_derived_package_contains_real_readable_gis_files(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "normalized"
            result = self.analyze(self.terrain(), self.water(), output_dir=output)
            self.assertTrue(result["normalization_ready"])
            with rasterio.open(output / "terrain-normalized.tif") as dataset:
                self.assertEqual(dataset.units, ("m",)); self.assertEqual(dataset.crs.to_epsg(), 32631)
            with rasterio.open(output / "land-water-mask.tif") as dataset:
                self.assertEqual(int(np.count_nonzero(dataset.read(1) == 2)), 8)
            with fiona.open(output / "water-extent.gpkg", layer="water_extent") as dataset:
                self.assertEqual(len(dataset), 1)
            self.assertTrue((output / "manifest.json").is_file())

    def test_existing_output_is_never_overwritten(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "normalized"; output.mkdir()
            (output / "keep.txt").write_text("keep", encoding="utf-8")
            self.error("OUTPUT_EXISTS", self.analyze, self.terrain(), self.water(), output_dir=output)
            self.assertEqual((output / "keep.txt").read_text(), "keep")

    def test_derived_output_cannot_be_created_inside_raw_source(self):
        with TemporaryDirectory() as directory:
            raw = Path(directory) / "source.geojson"; raw.write_text("{}", encoding="utf-8")
            self.error("OUTPUT_IN_SOURCE_DIRECTORY", self.analyze, self.terrain(), self.water(),
                       output_dir=Path(directory) / "derived", source_paths=(raw,))

    def test_invalid_target_resolution_is_rejected(self):
        self.error("INVALID_RESOLUTION", target_grid, self.terrain(), "EPSG:32631", 0)

    def test_grid_area_uses_affine_determinant(self):
        grid = TargetGrid(4, 4, "EPSG:32631", Affine(1, 0.2, 0, 0, -1, 4))
        self.assertEqual(grid.metadata()["pixel_area_m2"], 1)


    def test_task_restores_existing_proj_network_flag(self):
        from pyproj import network
        previous = network.is_network_enabled()
        try:
            network.set_network_enabled(True)
            result = self.analyze(self.terrain(), self.water())
            self.assertTrue(result["normalization_ready"])
            self.assertTrue(network.is_network_enabled())
        finally:
            network.set_network_enabled(previous)


def rectilinear_water_fixture(mask, transform):
    pieces = []
    for row, values in enumerate(mask):
        changes = np.flatnonzero(np.diff(np.r_[False, values, False].astype("int8")))
        for start, end in changes.reshape(-1, 2):
            pieces.append(Polygon([transform * (int(start), row), transform * (int(end), row),
                                   transform * (int(end), row + 1), transform * (int(start), row + 1)]))
    water = shapely.union_all(pieces)
    return () if water.is_empty else (water,)


def real_window_checks(sample_dir, output_parent):
    from prototypes.p0.carto_adapter import shapefile_components
    rasters = sorted(sample_dir.rglob("*.tif"))
    terrain_files = [p for p in rasters if p.stem.endswith("_Elevation")]
    depth_files = [p for p in rasters if p.stem.endswith("_Depth")]
    if len(terrain_files) != 1 or len(depth_files) != 1:
        raise AdapterError("AMBIGUOUS_INPUT", "explicit single local Elevation/Depth pair required")
    source_files = set(rasters)
    for p in rasters: source_files.update(item for item in p.parent.glob(p.name + ".*") if item.is_file())
    for p in sample_dir.rglob("*.shp"): source_files.update(shapefile_components(p))
    before = {str(path.relative_to(sample_dir)): file_hash(path) for path in sorted(source_files)}
    checks = []
    with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(terrain_files[0]) as terrain, rasterio.open(depth_files[0]) as depth:
        if terrain.crs != depth.crs or terrain.width != depth.width * 2 or terrain.height != depth.height * 2:
            raise AdapterError("UNSUPPORTED_WINDOW_FIXTURE", "this integration fixture needs the verified aligned 2:1 source grids")
        starts = [(0, 0), (terrain.width//2 - 256, terrain.height//2 - 256), (terrain.width - 512, terrain.height - 512)]
        for index, (column, row) in enumerate(starts):
            window = Window(column, row, 512, 512)
            depth_window = Window(column//2, row//2, 256, 256)
            values = terrain.read(1, window=window)
            elevation_transform = window_transform(window, terrain.transform)
            depth_transform = window_transform(depth_window, depth.transform)
            depth_values = depth.read(1, window=depth_window)
            positive = valid_pixels(depth_values, depth.read_masks(1, window=depth_window), depth.nodata) & (depth_values > 0)
            geometries = rectilinear_water_fixture(positive, depth_transform)
            coverage = box(*window_bounds(window, terrain.transform))
            expected_water = int(positive.sum()) * 4
            with MemoryFile() as memory:
                with memory.open(driver="GTiff", width=512, height=512, count=1, crs=terrain.crs,
                                 transform=elevation_transform, dtype=values.dtype, nodata=terrain.nodata) as writer:
                    writer.write(values, 1)
                with memory.open() as source:
                    for coordinate_case in ("source-metre", "wgs84-water-faces"):
                        if coordinate_case == "source-metre":
                            faces = WaterFaces(geometries, terrain.crs, coverage, terrain.crs, True,
                                               "derived fixture from confirmed Carto positive Depth cells; selected ROI only")
                        else:
                            inverse = Transformer.from_crs(terrain.crs, "EPSG:4326", always_xy=True)
                            faces = WaterFaces(tuple(transform_geometry(inverse.transform, geom) for geom in geometries),
                                               "EPSG:4326", transform_geometry(inverse.transform, coverage), "EPSG:4326", True,
                                               "same derived fixture expressed in WGS84; not independently supplied real water dataset")
                        output = output_parent / f"roi-{index}-{coordinate_case}"
                        result = analyze_water_faces(source, faces, vertical_unit="m", vertical_reference="game_origin", output_dir=output)
                        if not result["normalization_ready"] or result["counts"]["water"] != expected_water or result["counts"]["unknown"]:
                            raise AdapterError("DERIVED_WINDOW_MISMATCH", "water-face branch differs from source Depth cell-centre reference")
                        checks.append({"roi_index": index, "source_dem_window": [column, row, 512, 512],
                                       "coordinate_case": coordinate_case, "derived_package": output.name,
                                       "expected_water_pixels": expected_water, "counts": result["counts"], "area_m2": result["area_m2"],
                                       "normalization_ready": result["normalization_ready"], "water_depth_available": False,
                                       "fixture_source": "positive Depth cells, row-run rectangles; no source geometry repair"})
    after = {str(path.relative_to(sample_dir)): file_hash(path) for path in sorted(source_files)}
    if before != after: raise AdapterError("SOURCE_CHANGED", "original sample changed")
    return {"scope": "3 bounded 512x512 DEM windows / 256x256 Depth windows, two water-face CRSs each",
            "independent_external_water_file_tested": False, "checks": checks, "source_hashes": before,
            "source_unchanged": True, "derived_package_count": len(checks)}


def run_profile(profile_path, output_dir):
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    allowed = {"terrain_path", "water_faces_path", "water_source_crs", "coverage_geometry", "coverage_crs",
               "complete_coverage_confirmed", "coverage_evidence", "target_crs", "target_resolution_m",
               "terrain_unit", "terrain_vertical_reference"}
    if not isinstance(profile, dict) or set(profile) - allowed:
        raise AdapterError("INVALID_IMPORT_PROFILE", "unknown/invalid import profile fields")
    terrain_path = (profile_path.parent / profile["terrain_path"]).resolve()
    water_path = (profile_path.parent / profile["water_faces_path"]).resolve()
    geometries, source_crs, source_metadata = read_water_file(water_path, source_crs=profile.get("water_source_crs"))
    water = WaterFaces(geometries, source_crs, shapely.geometry.shape(profile["coverage_geometry"]),
                       profile.get("coverage_crs"), profile.get("complete_coverage_confirmed", False),
                       profile.get("coverage_evidence", ""))
    sources = tuple([terrain_path, water_path] if water_path.suffix.lower() != ".shp" else
                    [terrain_path, *__import__("prototypes.p0.carto_adapter", fromlist=["shapefile_components"]).shapefile_components(water_path)])
    with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(terrain_path, "r") as terrain:
        grid = target_grid(terrain, profile.get("target_crs"), profile.get("target_resolution_m"))
        result = analyze_water_faces(terrain, water, grid=grid, vertical_unit=profile.get("terrain_unit"),
                                    vertical_reference=profile.get("terrain_vertical_reference"), output_dir=output_dir, source_paths=sources)
    return {"scope": "explicit external water-face import profile", "source_water_metadata": source_metadata, "result": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--sample-dir", type=Path)
    inputs.add_argument("--profile", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if (args.sample_dir or args.profile) and not (args.output_dir and args.report):
        parser.error("explicit real/profile checks require separate output-dir and report")
    if args.sample_dir:
        source = args.sample_dir.resolve()
        for target in (args.output_dir.resolve(), args.report.resolve()):
            if target == source or source in target.parents:
                parser.error("derived files/report must be outside raw sample directory")
    if args.report and args.report.exists(): parser.error("create a new report; do not overwrite an earlier run")
    suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in
                               (CartoChecks, ProjectionChecks, MappingChecks, VersionBindingChecks, WaterInputChecks)])
    capture = io.StringIO()
    result = unittest.TextTestRunner(stream=capture, verbosity=1).run(suite)
    report = {"schema_version": "p0-02-input-report-0.1.0", "timestamp": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
              "synthetic": {"run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped)},
              "test_failures": [{"case": str(case), "summary": error.strip().splitlines()[-1][:500]} for case, error in result.failures + result.errors],
              "actual": None, "actual_status": "not_run"}
    success = result.wasSuccessful()
    if success and (args.sample_dir or args.profile):
        try:
            if args.sample_dir:
                if args.output_dir.exists(): raise AdapterError("OUTPUT_EXISTS", "choose a new derived output parent")
                report["actual"] = real_window_checks(args.sample_dir.resolve(), args.output_dir.resolve())
            else:
                report["actual"] = run_profile(args.profile.resolve(), args.output_dir.resolve())
            report["actual_status"] = "water_face_normalization_completed"
        except (AdapterError, OSError, ValueError, KeyError, rasterio.errors.RasterioError, fiona.errors.FionaError) as exc:
            report["actual_status"] = "failed"; report["actual_error"] = str(exc); success = False
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temp = args.report.with_name(args.report.name + ".tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"); temp.replace(args.report)
    print(f"P0-02 inputs: synthetic={result.testsRun}; failures={len(result.failures)}; errors={len(result.errors)}; skipped={len(result.skipped)}")
    for failure in report["test_failures"]: print("FAIL: " + failure["case"] + ": " + failure["summary"])
    if report["actual"] and args.sample_dir:
        actual = report["actual"]
        print("Actual derived-window checks=" + str(len(actual["checks"])) + "; GIS packages=" + str(actual["derived_package_count"]) +
              "; raw sources unchanged=" + str(actual["source_unchanged"]))
        for check in actual["checks"]:
            print("ROI " + str(check["roi_index"]) + " " + check["coordinate_case"] + " water=" +
                  str(check["counts"]["water"]) + " unknown=" + str(check["counts"]["unknown"]))
    if not success: print("FAIL: " + report.get("actual_error", "synthetic cases failed"))
    print("ALL P0-02 INPUT CHECKS PASSED" if success else "P0-02 INPUT CHECKS FAILED")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
