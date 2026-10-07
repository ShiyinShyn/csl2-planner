"""P0-02 checks: synthetic default; real Carto only when explicitly requested.
Run from repository root:
  conda run -n csl2-planner --no-capture-output python -s scripts/check_p0_carto.py
  conda run -n csl2-planner --no-capture-output python -s scripts/check_p0_carto.py --sample-dir GIS-files-example --report outputs/p0/p0-02-carto-validation.json
For source gates and local derived candidates add --review-geometries; declarations
can be supplied via --source-assertions (explicit owner evidence, no defaults).
Expected: ALL P0-02 CHECKS PASSED; failures exit nonzero. Data gaps are reported,
not converted to planning-ready or G0 acceptance. No downloads or GIS writes.
"""
# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MISSING = [name for name in ("numpy", "rasterio", "fiona", "pyproj", "shapely")
           if importlib.util.find_spec(name) is None]
if MISSING:
    print("FAIL: missing " + ", ".join(MISSING) + "; see ENVIRONMENT.md; use csl2-planner environment")
    raise SystemExit(1)

import fiona
import numpy as np
from pyproj import CRS
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import Affine, from_origin
import shapely
from shapely.geometry import LineString, Point, Polygon, box, mapping
import pyproj

from prototypes.p0.carto_adapter import (AdapterError, DepthSemantics, LAND, UNKNOWN, WATER,
    UNCERTAIN, align_depth, associate_geometry, classify_depth, metric_crs, normalize_network,
    pixel_area, scan_raster, scan_vector, shapefile_components, valid_pixels, validate_sample)
from prototypes.p0.contracts import LandUse, PlanningZone
from prototypes.p0.carto_quality import SourceAssertions, assertions_from_mapping, assess_readiness, review_geometry


class CartoChecks(unittest.TestCase):
    def raster(self, data, *, transform=None, nodata=-9999.0, crs="EPSG:32631"):
        memory = MemoryFile()
        self.addCleanup(memory.close)
        ds = memory.open(driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
                         dtype=data.dtype, crs=crs, transform=transform or from_origin(0, 4, 1, 1),
                         nodata=nodata)
        ds.write(data, 1)
        ds.close()
        reader = memory.open()
        self.addCleanup(reader.close)
        return reader

    def error(self, code, function, *args):
        with self.assertRaises(AdapterError) as ctx:
            function(*args)
        self.assertEqual(ctx.exception.code, code)

    def test_projected_metre_crs(self):
        self.assertEqual(metric_crs("EPSG:32631").to_epsg(), 32631)

    def test_missing_crs(self):
        self.error("MISSING_CRS", metric_crs, None)

    def test_geographic_crs_is_rejected(self):
        self.error("NON_METRIC_CRS", metric_crs, "EPSG:4326")

    def test_feet_crs_requires_explicit_conversion(self):
        self.error("NON_METRIC_CRS", metric_crs, "EPSG:2263")

    def test_affine_area_includes_rotation(self):
        self.assertEqual(pixel_area(Affine(2, 1, 0, 1, -3, 0)), 7)

    def test_singular_transform_is_rejected(self):
        self.error("INVALID_TRANSFORM", pixel_area, Affine(1, 1, 0, 1, 1, 0))

    def test_finite_sentinel_zero_negative_and_nan(self):
        values = np.array([[1.70141e38, 0, -5, np.nan, np.inf, 2]], dtype="float32")
        np.testing.assert_array_equal(valid_pixels(values, np.ones_like(values), 1.70141e38),
                                      [[False, True, True, False, False, True]])

    def test_explicit_band_mask(self):
        values = np.array([[2, 3]], dtype="float32")
        np.testing.assert_array_equal(valid_pixels(values, np.array([[255, 0]]), None), [[True, False]])

    def test_mask_shape_is_rejected(self):
        self.error("MASK_SHAPE", valid_pixels, np.zeros((1, 2)), np.zeros((2, 2)), None)

    def test_unknown_nodata_is_not_dry_land(self):
        values = np.array([[-9999, 2, 0, -1]], dtype="float32")
        result = classify_depth(values, np.ones_like(values), np.ones_like(values, dtype=bool), -9999)
        np.testing.assert_array_equal(result, [[UNKNOWN, WATER, UNCERTAIN, UNKNOWN]])

    def test_confirmed_carto_nodata_land_requires_coverage(self):
        values = np.array([[-9999, -9999, np.nan]], dtype="float32")
        result = classify_depth(values, np.ones_like(values), np.array([[True, False, True]]),
                                -9999, DepthSemantics(True, True))
        np.testing.assert_array_equal(result, [[LAND, UNKNOWN, UNKNOWN]])

    def test_unconfirmed_coverage_is_rejected(self):
        self.error("UNCONFIRMED_COVERAGE", DepthSemantics, True, False)

    def test_string_boolean_is_rejected(self):
        self.error("INVALID_SEMANTICS", DepthSemantics, "true", True)

    def test_near_zero_band_retains_uncertainty(self):
        values = np.array([[0.001, 1.0, -0.001]], dtype="float32")
        result = classify_depth(values, np.ones_like(values), np.ones_like(values, dtype=bool),
                                None, near_zero_m=0.01)
        np.testing.assert_array_equal(result, [[UNCERTAIN, WATER, UNCERTAIN]])

    def test_invalid_threshold(self):
        self.error("INVALID_THRESHOLD", classify_depth, np.ones((1, 1)), np.ones((1, 1)),
                   np.ones((1, 1), dtype=bool), None, DepthSemantics(), -1.0)

    def test_scan_respects_valid_mask(self):
        source = self.raster(np.array([[1, -9999], [0, -2]], dtype="float32"))
        result = scan_raster(source)
        self.assertEqual((result["valid_pixels"], result["valid_zero_pixels"],
                          result["valid_negative_pixels"]), (3, 1, 1))

    def test_different_resolution_area_conservation(self):
        terrain = self.raster(np.ones((4, 4), dtype="float32"))
        depth = self.raster(np.array([[2, -9999], [-9999, 2]], dtype="float32"),
                            transform=from_origin(0, 4, 2, 2))
        result = align_depth(terrain, depth)
        self.assertEqual(result["counts"], {"water": 8, "land": 0, "unknown": 8, "near_zero_uncertain": 0})
        self.assertEqual(sum(result["area_m2"][key] for key in ("water", "land", "unknown")), 16)

    def test_partial_coverage_never_becomes_land(self):
        terrain = self.raster(np.ones((4, 4), dtype="float32"))
        depth = self.raster(np.full((2, 2), -9999, dtype="float32"))
        result = align_depth(terrain, depth, DepthSemantics(True, True))
        self.assertEqual(result["depth_outside_pixels"], 12)
        self.assertEqual(result["counts"]["land"], 4)
        self.assertEqual(result["counts"]["unknown"], 12)

    def test_shifted_grid_is_spatially_aligned(self):
        terrain = self.raster(np.ones((4, 4), dtype="float32"))
        depth = self.raster(np.full((2, 2), 2, dtype="float32"), transform=from_origin(1, 3, 1, 1))
        result = align_depth(terrain, depth)
        self.assertEqual((result["counts"]["water"], result["depth_outside_pixels"]), (4, 12))

    def test_mismatched_crs_requires_conversion(self):
        terrain = self.raster(np.ones((2, 2), dtype="float32"))
        depth = self.raster(np.ones((2, 2), dtype="float32"), crs="EPSG:32632")
        self.error("CRS_TRANSFORM_REQUIRED", align_depth, terrain, depth)

    def test_terrain_missing_is_separate_from_water(self):
        terrain = self.raster(np.array([[-9999, 1], [1, 1]], dtype="float32"))
        depth = self.raster(np.full((2, 2), 2, dtype="float32"))
        result = align_depth(terrain, depth)
        self.assertEqual((result["terrain_missing_pixels"], result["counts"]["water"]), (1, 4))
        self.assertFalse(result["classification_complete"])

    def test_elevated_and_tunnel_are_separate_forms(self):
        self.assertEqual(normalize_network({"Form": "Elevated"})["form"], "ELEVATED")
        self.assertEqual(normalize_network({"Form": "Tunnel"})["form"], "TUNNEL")

    def test_unknown_and_compound_form_are_not_surface(self):
        for value in (None, "Mystery", "Normal,Tunnel"):
            self.assertIsNone(normalize_network({"Form": value})["form"])

    def test_excluded_modes_stay_background(self):
        for obj in ("Pathway", "Taxiway", "Runway", "Tram"):
            value = normalize_network({"Object": obj, "Form": "Normal"})
            self.assertTrue(value["excluded_from_computation"])
            self.assertFalse(value["road_candidate"])

    def test_shared_tram_lane_keeps_road_candidate(self):
        value = normalize_network({"Object": "Road", "Category": "Small, Tram", "Form": "Normal"})
        self.assertTrue(value["road_candidate"])
        self.assertFalse(value["excluded_from_computation"])

    def test_road_freight_is_allowed_but_tram_track_is_background(self):
        self.assertTrue(normalize_network({"Object": "Road", "Category": "Cargo"})["road_candidate"])
        self.assertTrue(normalize_network({"Object": "Track", "Category": "Tram"})["excluded_from_computation"])

    def test_import_editable_does_not_create_permission(self):
        result = normalize_network({"Object": "Road", "Form": "Normal", "editable": True})
        self.assertNotIn("editable", result)
        self.assertFalse(result["topology_verified"])

    def zones(self):
        return (PlanningZone("a", box(0, 0, 2, 2), LandUse.RESIDENTIAL, (100, 100), True),
                PlanningZone("b", box(2, 0, 4, 2), LandUse.COMMERCIAL, (100, 100), False))

    def test_cross_zone_uses_strictest_demolition_permission(self):
        result = associate_geometry(box(1, 0.5, 3, 1.5), self.zones())
        self.assertEqual(result["zone_ids"], ("a", "b"))
        self.assertFalse(result["allow_demolition"])
        self.assertIsNone(result["foundation_bottom_z_m"])

    def test_unmatched_and_boundary_point_are_conservative(self):
        self.assertFalse(associate_geometry(box(9, 9, 10, 10), self.zones())["allow_demolition"])
        self.assertEqual(associate_geometry(Point(2, 1), self.zones())["zone_ids"], ("a", "b"))

    def test_z_geometry_is_rejected(self):
        self.error("INVALID_GEOMETRY", associate_geometry, LineString([(0, 0, 1), (1, 1, 2)]), ())

    def test_shapefile_missing_components(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.shp"
            path.write_bytes(b"")
            self.error("MISSING_SHAPEFILE_COMPONENT", shapefile_components, path)

    def test_invalid_geometry_is_quarantined_with_source_record_id(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "Network_Boundary.shp"
            invalid = Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])
            with fiona.open(path, "w", driver="ESRI Shapefile", crs="EPSG:32631", encoding="UTF-8",
                            schema={"geometry": "Polygon", "properties": {"Object": "str"}}) as dst:
                dst.write({"geometry": mapping(invalid), "properties": {"Object": "Road"}})
            result = scan_vector(path)
            self.assertEqual(result["counts"]["invalid_geometry"], 1)
            self.assertEqual(result["quarantined_features"], [{"source_record_id": "0", "code": "invalid_geometry"}])

    def test_real_driver_synthetic_shapefile_and_geometry_audit(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "Building_Boundary.shp"
            with fiona.open(path, "w", driver="ESRI Shapefile", crs="EPSG:32631", encoding="UTF-8",
                            schema={"geometry": "Polygon", "properties": {"Object": "str"}}) as dst:
                dst.write({"geometry": mapping(box(0, 0, 1, 1)), "properties": {"Object": "Building"}})
            before = {p.name: p.read_bytes() for p in shapefile_components(path)}
            result = scan_vector(path)
            self.assertEqual(result["counts"]["pending_association"], 1)
            self.assertEqual(result["counts"]["valid_planar_geometry"], 1)
            self.assertEqual(before, {p.name: p.read_bytes() for p in shapefile_components(path)})


    def ready_fixture(self):
        actual = {"terrain": {"valid_pixels": 16}, "depth": {"valid_pixels": 8},
                  "alignment": {"pixel_conservation_passed": True, "terrain_missing_pixels": 0,
                                "depth_outside_pixels": 0, "counts": {"unknown": 0}}, "vectors": []}
        assertions = SourceAssertions(carto_source_confirmed=True, complete_depth_coverage_confirmed=True,
                                      terrain_unit="m", terrain_vertical_reference="game_origin",
                                      game_mapping_confirmed=True, evidence="explicit synthetic fixture")
        return actual, assertions

    def test_readiness_can_be_true_for_verified_inputs(self):
        actual, assertions = self.ready_fixture()
        result = assess_readiness(actual, assertions)
        self.assertTrue(result["planning_ready"])
        self.assertFalse(result["capabilities"]["three_dimensional_feasibility"])

    def test_default_source_assertions_do_not_invent_confirmation(self):
        actual, _ = self.ready_fixture()
        result = assess_readiness(actual, SourceAssertions())
        self.assertFalse(result["planning_ready"])
        self.assertIn("DEPTH_COVERAGE_UNCONFIRMED", {item["code"] for item in result["blocking_diagnostics"]})

    def test_optional_invalid_boundary_does_not_block_required_inputs(self):
        actual, assertions = self.ready_fixture()
        actual["vectors"] = [{"file": "Network_Boundary.shp", "counts": {"invalid_geometry": 220}}]
        result = assess_readiness(actual, assertions)
        self.assertTrue(result["planning_ready"])
        self.assertEqual(result["optional_diagnostics"][0]["count"], 220)
        self.assertFalse(result["optional_diagnostics"][0]["physical_envelope_accepted"])

    def test_missing_real_zone_is_a_downstream_dependency(self):
        actual, assertions = self.ready_fixture()
        result = assess_readiness(actual, assertions)
        self.assertTrue(result["planning_ready"])
        self.assertEqual(result["downstream_diagnostics"][0]["code"], "ACTUAL_PLANNING_ZONE_PENDING")

    def test_assertion_cannot_override_unknown_pixels(self):
        actual, assertions = self.ready_fixture()
        actual["alignment"]["counts"]["unknown"] = 1
        self.assertFalse(assess_readiness(actual, assertions)["planning_ready"])

    def test_assertion_cannot_override_missing_terrain(self):
        actual, assertions = self.ready_fixture()
        actual["alignment"]["terrain_missing_pixels"] = 1
        self.assertFalse(assess_readiness(actual, assertions)["planning_ready"])

    def test_missing_vertical_unit_blocks_readiness(self):
        actual, _ = self.ready_fixture()
        assertions = SourceAssertions(carto_source_confirmed=True, complete_depth_coverage_confirmed=True,
                                      terrain_vertical_reference="game_origin", game_mapping_confirmed=True,
                                      evidence="explicit synthetic fixture without unit")
        result = assess_readiness(actual, assertions)
        self.assertIn("TERRAIN_UNIT_UNCONFIRMED", {item["code"] for item in result["blocking_diagnostics"]})

    def test_source_declaration_requires_evidence(self):
        self.error("MISSING_ASSERTION_EVIDENCE", SourceAssertions, True)

    def test_source_declaration_rejects_string_boolean(self):
        self.error("INVALID_SOURCE_ASSERTION", SourceAssertions, "true")

    def test_unknown_source_declaration_fields_are_rejected(self):
        self.error("UNKNOWN_SOURCE_ASSERTION", assertions_from_mapping, {"editable": True})

    def test_source_declaration_is_loaded_without_boolean_coercion(self):
        value = assertions_from_mapping({"carto_source_confirmed": True, "evidence": "fixture"})
        self.assertTrue(value.carto_source_confirmed)
        self.assertFalse(value.complete_depth_coverage_confirmed)

    def test_geometry_review_keeps_source_and_requires_physical_review(self):
        invalid = Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])
        before = invalid.wkb
        result = review_geometry(invalid)
        self.assertEqual(result["status"], "POLYGON_CANDIDATE")
        self.assertTrue(result["candidate_valid"])
        self.assertFalse(result["physical_envelope_accepted"])
        self.assertEqual(before, invalid.wkb)

    def test_collapsed_geometry_is_retained_for_review(self):
        invalid = Polygon([(0, 0), (1, 1), (1, 2), (1, 1), (0, 0)])
        result = review_geometry(invalid)
        self.assertEqual(result["status"], "COLLAPSED_REVIEW")
        self.assertNotEqual(result["candidate_wkb_hex"], "")
        self.assertFalse(result["physical_envelope_accepted"])

    def test_mixed_polygon_and_line_components_are_not_discarded(self):
        invalid = Polygon([(0, 0), (2, 0), (2, 2), (1, 1), (2, 2), (0, 2), (0, 0)])
        result = review_geometry(invalid)
        self.assertEqual(result["candidate_type"], "GeometryCollection")
        self.assertEqual(result["status"], "MIXED_DIMENSION_REVIEW")
        self.assertTrue(result["review_required"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-dir", type=Path, help="explicit local Carto sample; read only")
    parser.add_argument("--report", type=Path, help="write aggregate JSON outside source directory")
    parser.add_argument("--source-assertions", type=Path, help="explicit source declaration JSON; no guessed values")
    parser.add_argument("--review-geometries", action="store_true", help="generate local review candidates for invalid boundaries")
    args = parser.parse_args()
    assertions = SourceAssertions()
    if args.source_assertions:
        try:
            assertions = assertions_from_mapping(json.loads(args.source_assertions.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    if args.sample_dir and args.report:
        source, target = args.sample_dir.resolve(), args.report.resolve()
        if target == source or source in target.parents:
            parser.error("report must be outside the raw sample directory")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CartoChecks)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    report = {"schema_version": "p0-02-report-0.2.0",
              "timestamp": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
              "environment": {"python": platform.python_version(), "rasterio": rasterio.__version__,
                              "gdal": rasterio.__gdal_version__, "fiona": fiona.__version__,
                              "shapely": shapely.__version__, "pyproj": pyproj.__version__,
                              "numpy": np.__version__},
              "synthetic": {"run": result.testsRun, "failures": len(result.failures),
                            "errors": len(result.errors), "skipped": len(result.skipped)},
              "actual": None, "actual_status": "not_run", "planning_ready": None}
    success = result.wasSuccessful()
    if success and args.sample_dir:
        try:
            report["actual"] = validate_sample(args.sample_dir.resolve(), source_assertions=assertions,
                                               geometry_review=args.review_geometries)
            report["planning_ready"] = report["actual"]["planning_ready"]
            report["actual_status"] = "read_only_validation_completed"
        except (AdapterError, OSError, ValueError, fiona.errors.FionaError, rasterio.errors.RasterioError) as exc:
            report["actual_status"] = "failed"
            report["actual_error"] = str(exc)
            success = False
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_name(args.report.name + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(args.report)
    print(f"P0-02 synthetic={result.testsRun}; failures={len(result.failures)}; errors={len(result.errors)}; skipped={len(result.skipped)}")
    if report["actual"]:
        actual = report["actual"]
        print("Carto: valid terrain=" + str(actual["terrain"]["valid_pixels"]) +
              "; valid depth=" + str(actual["depth"]["valid_pixels"]) +
              "; vector layers=" + str(len(actual["vectors"])) + "; sources unchanged=" + str(actual["source_unchanged"]))
        print("Planning inputs ready=" + str(actual["planning_ready"]) +
              "; blockers=" + str(len(actual["readiness"]["blocking_diagnostics"])) +
              "; optional degraded layers=" + str(len(actual["readiness"]["optional_diagnostics"])))
        for review in actual["geometry_reviews"]:
            print("Geometry review candidates=" + str(review["candidate_count"]) +
                  "; status=" + str(review["status_counts"]) + "; physical acceptance=False")
    if not success:
        print("FAIL: " + report.get("actual_error", "synthetic checks failed"))
    print("ALL P0-02 CHECKS PASSED" if success else "P0-02 CHECKS FAILED")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
