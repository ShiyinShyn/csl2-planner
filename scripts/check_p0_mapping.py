"""Run: conda run -n csl2-planner --no-capture-output python -s scripts/check_p0_mapping.py
Expected: ALL P0-02 MAPPING CHECKS PASSED; synthetic defaults need no private data.
Real checks require explicit --sample-dir, --source-assertions, --settings; reports
must stay outside the source directory. No uploads, downloads or source writes.
"""
# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_p0_carto import CartoChecks  # reuse dependency checks and 47 core cases
from pyproj import Transformer
from prototypes.p0.carto_adapter import AdapterError, validate_sample
from prototypes.p0.carto_quality import assertions_from_mapping
from prototypes.p0.carto_projection import audit_projection_context
from prototypes.p0.carto_mapping import GameMappingPreview, preview_from_grid, audit_mapping_roundtrip, audit_map_tile_preview, source_manifest_digest, verify_version_bound_mapping
from prototypes.p0.carto_quality import assess_readiness
from dataclasses import replace
from tempfile import TemporaryDirectory
from unittest.mock import patch
from hashlib import sha256
import struct


class ProjectionChecks(unittest.TestCase):
    def fixture(self):
        x, y = Transformer.from_crs("EPSG:4326", "EPSG:32631", always_xy=True).transform(0, 0)
        terrain = {"crs": "EPSG:32631", "shape": [4, 4], "transform": [1, 0, x - 2, 0, -1, y + 2]}
        settings = {"projection": "WGS84", "x": 0, "y": 0,
                    "scope": "reported_current_ui_defaults", "evidence": "synthetic fixture"}
        return terrain, dict(terrain), settings

    def error(self, code, terrain, depth, settings):
        with self.assertRaises(AdapterError) as ctx:
            audit_projection_context(terrain, depth, [], settings)
        self.assertEqual(ctx.exception.code, code)

    def test_zero_origin_match_is_only_a_candidate(self):
        terrain, depth, settings = self.fixture()
        result = audit_projection_context(terrain, depth, [], settings)
        self.assertTrue(result["origin_candidate_matches_zero_reference"])
        self.assertFalse(result["game_mapping_confirmed"])

    def test_origin_difference_does_not_get_silently_shifted(self):
        terrain, depth, settings = self.fixture()
        terrain["transform"] = list(terrain["transform"])
        terrain["transform"][2] += 100
        result = audit_projection_context(terrain, depth, [], settings)
        self.assertFalse(result["origin_candidate_matches_zero_reference"])
        self.assertAlmostEqual(result["origin_reference_residual_m"], 100)

    def test_missing_crs_does_not_receive_guessed_epsg(self):
        terrain, depth, settings = self.fixture()
        terrain["crs"] = None
        self.error("MISSING_CRS", terrain, depth, settings)

    def test_geographic_crs_is_not_treated_as_metres(self):
        terrain, depth, settings = self.fixture()
        terrain["crs"] = "EPSG:4326"
        self.error("NON_METRIC_CRS", terrain, depth, settings)

    def test_different_source_crs_is_rejected(self):
        terrain, depth, settings = self.fixture()
        depth["crs"] = "EPSG:32632"
        self.error("CRS_MISMATCH", terrain, depth, settings)

    def test_ui_scope_cannot_become_historical_proof(self):
        terrain, depth, settings = self.fixture()
        result = audit_projection_context(terrain, depth, [], settings)
        self.assertFalse(result["historical_settings_confirmed"])
        self.assertFalse(result["game_axis_orientation_confirmed"])

    def test_missing_settings_evidence_is_rejected(self):
        terrain, depth, settings = self.fixture()
        settings["evidence"] = ""
        self.error("MISSING_SETTINGS_EVIDENCE", terrain, depth, settings)

    def test_vector_crs_mismatch_is_reported_without_relabelling(self):
        terrain, depth, settings = self.fixture()
        result = audit_projection_context(terrain, depth, [{"crs": "EPSG:32632"}], settings)
        self.assertFalse(result["all_vector_crs_match"])
        self.assertFalse(result["game_mapping_confirmed"])


class MappingChecks(unittest.TestCase):
    def fixture(self):
        terrain = {"crs": "EPSG:32631", "shape": [4, 4], "transform": [1, 0, 98, 0, -1, 202]}
        depth = {"crs": "EPSG:32631", "shape": [2, 2], "transform": [2, 0, 98, 0, -2, 202]}
        return terrain, depth, preview_from_grid(terrain, depth)

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(AdapterError) as ctx:
            function(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)

    def test_game_axes_preserve_x_z_plane_and_y_height(self):
        _, _, preview = self.fixture()
        self.assertEqual(preview.forward((1, 80, -1)), (101, 199, 80))

    def test_inverse_reconstructs_game_axis_order(self):
        _, _, preview = self.fixture()
        self.assertEqual(preview.inverse((99, 202, -10)), (-1, -10, 2))

    def test_horizontal_translation_does_not_change_height(self):
        _, _, preview = self.fixture()
        self.assertEqual(preview.forward((-2, 700, 2))[2], 700)

    def test_centre_and_boundary_roundtrip(self):
        terrain, depth, _ = self.fixture()
        result = audit_mapping_roundtrip(terrain, depth)
        self.assertEqual(result["roundtrip_points"], 27)
        self.assertTrue(result["numerical_roundtrip_passed"])
        self.assertFalse(result["game_mapping_confirmed"])

    def test_row_increase_decreases_game_z(self):
        terrain, depth, _ = self.fixture()
        points = audit_mapping_roundtrip(terrain, depth)["corner_pixel_centres_game_preview"]
        self.assertGreater(points[0][2], points[2][2])
        self.assertLess(points[0][0], points[1][0])

    def test_half_pixel_offset_is_explicit(self):
        terrain, depth, _ = self.fixture()
        points = audit_mapping_roundtrip(terrain, depth)["corner_pixel_centres_game_preview"]
        self.assertEqual(points[0], [-1.5, 0.0, 1.5])

    def test_outside_game_coordinate_is_rejected(self):
        _, _, preview = self.fixture()
        self.error("OUTSIDE_SOURCE_COVERAGE", preview.forward, (3, 0, 0))

    def test_nan_coordinate_is_rejected(self):
        _, _, preview = self.fixture()
        self.error("INVALID_COORDINATE", preview.forward, (float("nan"), 0, 0))

    def test_rotated_grid_is_not_silently_reinterpreted(self):
        terrain, depth, _ = self.fixture()
        terrain["transform"][1] = 0.1
        self.error("UNSUPPORTED_GRID_ORIENTATION", preview_from_grid, terrain, depth)

    def test_shifted_depth_origin_is_rejected(self):
        terrain, depth, _ = self.fixture()
        depth["transform"][2] += 1
        self.error("SOURCE_FOOTPRINT_MISMATCH", preview_from_grid, terrain, depth)

    def test_source_evidence_is_required(self):
        self.error("MISSING_MAPPING_EVIDENCE", GameMappingPreview, (0, 0), (1, 1), "EPSG:32631", "")

    def test_preview_is_blocked_at_game_export_boundary(self):
        _, _, preview = self.fixture()
        self.error("UNCONFIRMED_GAME_MAPPING", preview.forward, (0, 0, 0), for_export=True)
        self.error("UNCONFIRMED_GAME_MAPPING", preview.inverse, (100, 200, 0), for_export=True)



class VersionBindingChecks(unittest.TestCase):
    def fixture(self, directory):
        from prototypes.p0.carto_mapping import PINNED_COMMIT, PINNED_MANAGED_DLL_SHA256
        root = Path(directory)
        sample, code = root / "sample", root / "code"
        sample.mkdir(); code.mkdir()
        hashes = {}
        for name, shape, scale in (("Test_Elevation.tif", [4, 4], 1.0), ("Test_Depth.tif", [2, 2], 2.0)):
            centre = [shape[1]/2, shape[0]/2, 0, 100.0, 200.0, 0]
            payload_offset = 8 + 2 + 24 + 4
            header = b"II" + struct.pack("<HI", 42, 8) + struct.pack("<H", 2)
            entries = struct.pack("<HHII", 33550, 12, 3, payload_offset) + struct.pack("<HHII", 33922, 12, 6, payload_offset + 24)
            body = header + entries + struct.pack("<I", 0) + struct.pack("<3d", scale, scale, 1) + struct.pack("<6d", *centre)
            (sample / name).write_bytes(body)
            hashes[name] = sha256(body).hexdigest()
        source_body = b"explicit synthetic publisher-source fixture"
        (code / "Fixture__Source.cs").write_bytes(source_body)
        mocked_sources = {"Fixture/Source.cs": sha256(source_body).hexdigest()}
        terrain = {"crs": "EPSG:32631", "shape": [4, 4], "transform": [1, 0, 98, 0, -1, 202], "dtype": "float32"}
        depth = {"crs": "EPSG:32631", "shape": [2, 2], "transform": [2, 0, 98, 0, -2, 202], "dtype": "float32"}
        actual = {"terrain": terrain, "depth": depth, "source_hashes": hashes,
                  "projection_audit": {"origin_candidate_matches_zero_reference": True, "all_vector_crs_match": True}}
        declaration = {"carto_version": "1.0.17", "commit": PINNED_COMMIT,
                       "managed_dll_sha256": PINNED_MANAGED_DLL_SHA256,
                       "source_manifest_sha256": source_manifest_digest(hashes),
                       "export_matches_installation_confirmed": True, "evidence": "explicit synthetic fixture, not actual export proof"}
        return actual, declaration, code, sample, mocked_sources

    def checked(self, actual, declaration, code, sample, sources):
        with patch("prototypes.p0.carto_mapping.PINNED_SOURCE_HASHES", sources):
            return verify_version_bound_mapping(actual, declaration, code, sample)

    def assert_code(self, expected, function, *args):
        with self.assertRaises(AdapterError) as ctx:
            function(*args)
        self.assertEqual(ctx.exception.code, expected)

    def test_bound_version_grid_can_verify_without_claiming_game_runtime(self):
        with TemporaryDirectory() as directory:
            data = self.fixture(directory)
            result = self.checked(*data)
            self.assertTrue(result["game_mapping_confirmed"])
            self.assertFalse(result["real_game_control_points_verified"])
            self.assertFalse(result["allow_game_placement_export"])

    def test_current_installation_alone_does_not_confirm_export(self):
        with TemporaryDirectory() as directory:
            actual, declaration, code, sample, sources = self.fixture(directory)
            declaration["export_matches_installation_confirmed"] = False
            self.assert_code("EXPORT_VERSION_UNCONFIRMED", self.checked, actual, declaration, code, sample, sources)

    def test_string_true_cannot_confirm_historical_export(self):
        with TemporaryDirectory() as directory:
            actual, declaration, code, sample, sources = self.fixture(directory)
            declaration["export_matches_installation_confirmed"] = "true"
            self.assert_code("EXPORT_VERSION_UNCONFIRMED", self.checked, actual, declaration, code, sample, sources)

    def test_changed_snapshot_invalidates_prior_version_confirmation(self):
        with TemporaryDirectory() as directory:
            actual, declaration, code, sample, sources = self.fixture(directory)
            actual["source_hashes"]["New.shp"] = "a" * 64
            self.assert_code("STALE_VERSION_DECLARATION", self.checked, actual, declaration, code, sample, sources)

    def test_different_export_version_is_not_silently_supported(self):
        with TemporaryDirectory() as directory:
            actual, declaration, code, sample, sources = self.fixture(directory)
            declaration["carto_version"] = "1.0.18"
            self.assert_code("UNSUPPORTED_EXPORT_VERSION", self.checked, actual, declaration, code, sample, sources)

    def test_tampered_pinned_source_cannot_verify_mapping(self):
        with TemporaryDirectory() as directory:
            actual, declaration, code, sample, sources = self.fixture(directory)
            (code / "Fixture__Source.cs").write_text("changed", encoding="utf-8")
            self.assert_code("PINNED_SOURCE_CHANGED", self.checked, actual, declaration, code, sample, sources)

    def test_changed_native_reference_is_not_overridden_by_declaration(self):
        with TemporaryDirectory() as directory:
            actual, declaration, code, sample, sources = self.fixture(directory)
            file = sample / "Test_Depth.tif"
            body = bytearray(file.read_bytes())
            body[-24:-16] = struct.pack("<d", 101.0)
            file.write_bytes(body)
            actual["source_hashes"]["Test_Depth.tif"] = sha256(body).hexdigest()
            declaration["source_manifest_sha256"] = source_manifest_digest(actual["source_hashes"])
            self.assert_code("NATIVE_REFERENCE_MISMATCH", self.checked, actual, declaration, code, sample, sources)

    def test_norm16_cannot_inherit_float32_unit_contract(self):
        with TemporaryDirectory() as directory:
            actual, declaration, code, sample, sources = self.fixture(directory)
            actual["terrain"]["dtype"] = "uint16"
            self.assert_code("UNSUPPORTED_NATIVE_FORMAT", self.checked, actual, declaration, code, sample, sources)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-dir", type=Path)
    parser.add_argument("--source-assertions", type=Path)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--version-evidence", type=Path, help="explicit export version binding to source snapshot")
    parser.add_argument("--pinned-source-dir", type=Path, help="local verified fixed-commit publisher source evidence")
    args = parser.parse_args()
    if bool(args.version_evidence) != bool(args.pinned_source_dir) or (args.version_evidence and not args.sample_dir):
        parser.error("version binding requires sample-dir, version-evidence and pinned-source-dir together")
    if args.sample_dir and not (args.source_assertions and args.settings):
        parser.error("real sample requires explicit source declarations and reported UI settings")
    if args.sample_dir and args.report:
        source, target = args.sample_dir.resolve(), args.report.resolve()
        if target == source or source in target.parents:
            parser.error("report must be outside the raw source directory")
    suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(CartoChecks),
                               unittest.defaultTestLoader.loadTestsFromTestCase(ProjectionChecks),
                               unittest.defaultTestLoader.loadTestsFromTestCase(MappingChecks),
                               unittest.defaultTestLoader.loadTestsFromTestCase(VersionBindingChecks)])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    report = {"schema_version": "p0-02-mapping-report-0.2.0",
              "timestamp": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
              "synthetic": {"run": result.testsRun, "failures": len(result.failures),
                            "errors": len(result.errors), "skipped": len(result.skipped)},
              "actual": None, "actual_status": "not_run", "planning_ready": None}
    success = result.wasSuccessful()
    if success and args.sample_dir:
        try:
            assertions = assertions_from_mapping(json.loads(args.source_assertions.read_text(encoding="utf-8")))
            settings = json.loads(args.settings.read_text(encoding="utf-8"))
            actual = validate_sample(args.sample_dir.resolve(), source_assertions=assertions, geometry_review=True)
            actual["projection_audit"] = audit_projection_context(actual["terrain"], actual["depth"],
                                                                  actual["vectors"], settings)
            actual["mapping_preview"] = audit_mapping_roundtrip(actual["terrain"], actual["depth"])
            preview = preview_from_grid(actual["terrain"], actual["depth"])
            tile_paths = list(args.sample_dir.resolve().rglob("*_Area_Boundary.shp"))
            actual["map_tile_preview"] = [audit_map_tile_preview(path, preview) for path in tile_paths]
            if args.version_evidence:
                version_declaration = json.loads(args.version_evidence.read_text(encoding="utf-8"))
                actual["version_bound_mapping"] = verify_version_bound_mapping(actual, version_declaration,
                    args.pinned_source_dir.resolve(), args.sample_dir.resolve())
                verified_assertions = replace(assertions, game_mapping_confirmed=True,
                    evidence=assertions.evidence + "; fixed-version source/native-reference contract verified")
                actual["readiness"] = assess_readiness(actual, verified_assertions)
                actual["carto_version"] = actual["version_bound_mapping"]["carto_version"]
                actual["carto_version_source"] = "user-confirmed original export; installed DLL product identity"
                actual["planning_ready"] = actual["readiness"]["planning_ready"]
                actual["remaining"] = [item["code"] for item in actual["readiness"]["blocking_diagnostics"]]
                actual["readiness"]["documented_source_facts"] = {
                    **actual["readiness"]["documented_source_facts"], "dataset_version_verified": True}
            report["actual"] = actual
            report["actual_status"] = "mapping_preview_review_completed"
            report["planning_ready"] = actual["planning_ready"]
        except (OSError, ValueError) as exc:
            report["actual_status"] = "failed"
            report["error"] = str(exc)
            success = False
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_name(args.report.name + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(args.report)
    print("P0-02 mapping preview checks=" + str(result.testsRun) + "; failures=" + str(len(result.failures)) +
          "; errors=" + str(len(result.errors)) + "; skipped=" + str(len(result.skipped)))
    if report["actual"]:
        actual = report["actual"]
        print("Planning ready=" + str(actual["planning_ready"]) + "; blockers=" +
              str([item["code"] for item in actual["readiness"]["blocking_diagnostics"]]))
        print("Origin candidate residual_m=" + str(actual["projection_audit"]["origin_reference_residual_m"]) +
              "; game_mapping_confirmed=" + str(actual.get("version_bound_mapping", {}).get("game_mapping_confirmed", False)) +
              "; sources unchanged=" + str(actual["source_unchanged"]))
        print("Mapping preview roundtrip points=" + str(actual["mapping_preview"]["roundtrip_points"]) +
              "; max_error_m=" + str(actual["mapping_preview"]["maximum_roundtrip_error_m"]) +
              "; real_game_control_points_verified=False")
        for tile in actual["map_tile_preview"]:
            print("MapTile records=" + str(tile["map_tile_records"]) + "; vertices_outside=" +
                  str(tile["vertices_outside_preview_coverage"]) + "; max_error_m=" + str(tile["maximum_roundtrip_error_m"]))
    if not success:
        print("FAIL: " + report.get("error", "synthetic validation failed"))
    print("ALL P0-02 MAPPING CHECKS PASSED" if success else "P0-02 MAPPING CHECKS FAILED")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
