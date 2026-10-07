"""Run: conda run -n csl2-planner --no-capture-output python -s scripts/check_p0_projection.py
Expected: ALL P0-02 PROJECTION CHECKS PASSED; synthetic defaults need no private data.
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-dir", type=Path)
    parser.add_argument("--source-assertions", type=Path)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.sample_dir and not (args.source_assertions and args.settings):
        parser.error("real sample requires explicit source declarations and reported UI settings")
    if args.sample_dir and args.report:
        source, target = args.sample_dir.resolve(), args.report.resolve()
        if target == source or source in target.parents:
            parser.error("report must be outside the raw source directory")
    suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(CartoChecks),
                               unittest.defaultTestLoader.loadTestsFromTestCase(ProjectionChecks)])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    report = {"schema_version": "p0-02-projection-report-0.1.0",
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
            report["actual"] = actual
            report["actual_status"] = "projection_context_review_completed"
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
    print("P0-02 projection checks=" + str(result.testsRun) + "; failures=" + str(len(result.failures)) +
          "; errors=" + str(len(result.errors)) + "; skipped=" + str(len(result.skipped)))
    if report["actual"]:
        actual = report["actual"]
        print("Planning ready=" + str(actual["planning_ready"]) + "; blockers=" +
              str([item["code"] for item in actual["readiness"]["blocking_diagnostics"]]))
        print("Origin candidate residual_m=" + str(actual["projection_audit"]["origin_reference_residual_m"]) +
              "; game_mapping_confirmed=False; sources unchanged=" + str(actual["source_unchanged"]))
    if not success:
        print("FAIL: " + report.get("error", "synthetic validation failed"))
    print("ALL P0-02 PROJECTION CHECKS PASSED" if success else "P0-02 PROJECTION CHECKS FAILED")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
