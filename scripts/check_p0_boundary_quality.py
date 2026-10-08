"""Run: conda run -n csl2-planner --no-capture-output python -s scripts/check_p0_boundary_quality.py
Expected: ALL P0-02 BOUNDARY QUALITY CHECKS PASSED; default is self-contained.
Real --sample-dir retains source records; degraded WKB report is local only.
No source repair, game writes or candidate physical acceptance.
"""
# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_p0_inputs import (CartoChecks, ProjectionChecks, MappingChecks, VersionBindingChecks, WaterInputChecks)
import fiona
import shapely
from shapely.geometry import Polygon, LineString, box, mapping
import rasterio
from prototypes.p0.carto_adapter import AdapterError, file_hash, shapefile_components
from prototypes.p0.carto_quality import review_geometry
from prototypes.p0.contracts import (Corridor, ExistingObject, LandUse, Mode, PlanningZone, PolicyStatus, Snapshot, TunnelRules)
from prototypes.p0.boundary_quality import (BoundaryRecord, BoundarySnapshot, boundary_snapshot_report,
    decide_with_boundary_quality, load_boundary_snapshot)


class BoundaryQualityChecks(unittest.TestCase):
    def record(self, geometry=None, id="7", candidate=None):
        invalid = Polygon([(0,0),(4,4),(4,0),(0,4),(0,0)]) if geometry is None else geometry
        return BoundaryRecord(id, invalid.wkb, "Elevated", candidate, "POLYGON_CANDIDATE" if candidate else None)

    def fixture(self, records=None, *, allow=False, mode=Mode.ELEVATED, asset_modes=None, corridor=None):
        records = (self.record(),) if records is None else records
        boundaries = BoundarySnapshot("bound-r1", "metric-source", "Network_Boundary.shp", "EPSG:32631", records,
                                      len(records), (("Network_Boundary.shp","a"*64),), box(-100,-100,100,100), True)
        zone = PlanningZone("zone-a", box(-100,-100,100,100), LandUse.RESIDENTIAL, (100,100), allow)
        snapshot = Snapshot("input-r1", "metric-source", (zone,), (), True)
        corridor = corridor or LineString([(-2,2),(6,2)])
        candidate = Corridor("road-a", "metric-source", corridor, 0.25, mode,
                             frozenset({Mode.SURFACE, Mode.ELEVATED, Mode.TUNNEL}) if asset_modes is None else asset_modes, 999.0)
        rules = TunnelRules("rules-r1", 0.5, 1.0, 1.0, 3.0)
        return candidate, snapshot, rules, boundaries

    def decide(self, values):
        c, s, r, b = values
        return decide_with_boundary_quality(c,s,r,b,expected_revision=s.revision,expected_boundary_revision=b.revision)

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(ValueError) as ctx:
            function(*args, **kwargs)
        self.assertEqual(getattr(ctx.exception,"code",None),code)

    def test_invalid_source_wkb_is_retained_byte_for_byte(self):
        record = self.record(); before = record.source_geometry_wkb
        self.assertTrue(record.degraded); self.assertEqual(record.quality,"INVALID_GEOMETRY")
        self.assertEqual(record.source_geometry_wkb,before); self.assertEqual(record.source_guard.bounds,(0,0,4,4))

    def test_candidate_does_not_shrink_conservative_guard(self):
        record = self.record(candidate=box(1,1,2,2).wkb)
        self.assertEqual(record.source_guard.bounds,(0,0,4,4)); self.assertTrue(record.degraded)

    def test_mixed_dimension_candidate_never_replaces_source(self):
        invalid=Polygon([(0,0),(4,0),(4,4),(2,2),(4,4),(0,4),(0,0)])
        review=review_geometry(invalid)
        record=BoundaryRecord("7",invalid.wkb,"Normal",bytes.fromhex(review["candidate_wkb_hex"]),review["status"])
        self.assertTrue(record.degraded); self.assertEqual(record.source_guard.bounds,(0,0,4,4))
        self.assertEqual(record.candidate_wkb,bytes.fromhex(review["candidate_wkb_hex"]))

    def test_invalid_crossing_is_data_insufficient_and_tunnel_only(self):
        result=self.decide(self.fixture()); self.assertEqual(result.status,PolicyStatus.DATA_INSUFFICIENT)
        self.assertEqual(result.permitted_modes,frozenset({Mode.TUNNEL})); self.assertTrue(result.lower_only)

    def test_roof_height_does_not_allow_elevated_bypass(self):
        result=self.decide(self.fixture(mode=Mode.ELEVATED))
        self.assertNotIn(Mode.ELEVATED,result.permitted_modes); self.assertNotIn(Mode.SURFACE,result.permitted_modes)

    def test_demolition_permission_cannot_clear_quality_defect(self):
        result=self.decide(self.fixture(allow=True)); self.assertEqual(result.status,PolicyStatus.DATA_INSUFFICIENT)
        self.assertEqual(result.demolition_candidate_ids,()); self.assertEqual(len(result.mandatory_object_ids),1)

    def test_asset_without_tunnel_has_no_quality_fallback(self):
        result=self.decide(self.fixture(asset_modes=frozenset({Mode.SURFACE,Mode.ELEVATED})))
        self.assertEqual(result.status,PolicyStatus.NO_FEASIBLE_SOLUTION); self.assertEqual(result.permitted_modes,frozenset())

    def test_distant_invalid_object_is_not_global_geometry_rejection(self):
        result=self.decide(self.fixture(corridor=LineString([(-10,-10),(-5,-10)])))
        self.assertEqual(result.status,PolicyStatus.POLICY_CLEAR); self.assertEqual(result.mandatory_object_ids,())

    def test_corridor_width_not_just_centreline_hits_guard(self):
        values=self.fixture(corridor=LineString([(-2,4.6),(6,4.6)]))
        self.assertEqual(self.decide(values).status,PolicyStatus.DATA_INSUFFICIENT)

    def test_unlocatable_null_object_is_not_dropped(self):
        result=self.decide(self.fixture(records=(BoundaryRecord("null",None),)))
        self.assertEqual(result.reason,"UNLOCATABLE_BOUNDARY_RETAINED"); self.assertEqual(result.permitted_modes,frozenset())

    def test_empty_geometry_requires_unlocatable_review(self):
        result=self.decide(self.fixture(records=(BoundaryRecord("empty",Polygon().wkb),)))
        self.assertEqual(result.status,PolicyStatus.DATA_INSUFFICIENT)

    def test_nonfinite_geometry_cannot_clear_space(self):
        geometry=LineString([(0,0),(float("inf"),1)])
        result=self.decide(self.fixture(records=(BoundaryRecord("nonfinite",geometry.wkb),)))
        self.assertEqual(result.reason,"UNLOCATABLE_BOUNDARY_RETAINED")

    def test_z_geometry_is_preserved_as_degraded(self):
        geometry=Polygon([(0,0,1),(4,0,1),(4,4,1),(0,4,1),(0,0,1)])
        record=BoundaryRecord("z",geometry.wkb); self.assertEqual(record.quality,"Z_GEOMETRY_UNVERIFIED")
        self.assertEqual(self.decide(self.fixture(records=(record,))).status,PolicyStatus.DATA_INSUFFICIENT)

    def test_actual_valid_source_not_omitted_by_empty_domain_snapshot(self):
        result=self.decide(self.fixture(records=(self.record(geometry=box(0,0,4,4)),)))
        self.assertEqual(result.status,PolicyStatus.DATA_INSUFFICIENT); self.assertEqual(len(result.mandatory_object_ids),1)

    def test_hard_protection_not_overridden_by_quality_layer(self):
        c,s,r,b=self.fixture();s=replace(s,protected_areas=(box(-1,1,5,3),))
        result=self.decide((c,s,r,b)); self.assertEqual(result.status,PolicyStatus.NO_FEASIBLE_SOLUTION)
        self.assertEqual(result.reason,"HARD_PROTECTION_CONFLICT")

    def test_stale_source_revision_is_rejected(self):
        c,s,r,b=self.fixture()
        result=decide_with_boundary_quality(c,s,r,b,expected_revision=s.revision,expected_boundary_revision="old")
        self.assertEqual(result.status,PolicyStatus.STALE_INPUT)

    def test_coordinate_space_mismatch_is_rejected(self):
        c,s,r,b=self.fixture(); b=replace(b,space_id="different")
        self.error("COORDINATE_SPACE_MISMATCH",decide_with_boundary_quality,c,s,r,b,
                   expected_revision=s.revision,expected_boundary_revision=b.revision)

    def test_unknown_coverage_is_data_insufficient(self):
        c,s,r,b=self.fixture();b=replace(b,coverage_confirmed=False)
        self.assertEqual(self.decide((c,s,r,b)).reason,"BOUNDARY_COVERAGE_UNKNOWN")

    def test_roi_outside_coverage_does_not_clear_obstacles(self):
        c,s,r,b=self.fixture(corridor=LineString([(101,101),(110,110)]))
        self.assertEqual(self.decide((c,s,r,b)).reason,"BOUNDARY_COVERAGE_UNKNOWN")

    def test_source_record_count_detects_drop(self):
        *_,b=self.fixture(); self.error("SOURCE_RECORDS_DROPPED",replace,b,records=())

    def test_duplicate_source_ids_are_rejected(self):
        *_,b=self.fixture()
        self.error("DUPLICATE_SOURCE_RECORD",replace,b,records=(b.records[0],b.records[0]),declared_source_count=2)

    def test_source_identity_collision_cannot_override_footprint(self):
        c,s,r,b=self.fixture();s=replace(s,objects=(ExistingObject(b.object_id(b.records[0]),box(50,50,60,60)),))
        self.error("SOURCE_OBJECT_COLLISION",decide_with_boundary_quality,c,s,r,b,
                   expected_revision=s.revision,expected_boundary_revision=b.revision)

    def test_report_preserves_candidate_and_source_without_acceptance(self):
        record=self.record(candidate=box(0,0,4,4).wkb); *_,b=self.fixture(records=(record,))
        retained=boundary_snapshot_report(b,include_degraded_sources=True)["degraded_records"][0]
        self.assertEqual(retained["source_wkb_hex"],record.source_geometry_wkb.hex())
        self.assertFalse(retained["physical_envelope_accepted"]); self.assertFalse(retained["automatic_demolition_allowed"])

    def test_loader_retains_invalid_and_valid_shapefile_records(self):
        with TemporaryDirectory() as directory:
            file=Path(directory)/"Network_Boundary.shp"
            with fiona.open(file,"w",driver="ESRI Shapefile",crs="EPSG:32631",schema={"geometry":"Polygon","properties":{"Form":"str"}}) as dst:
                for geometry in (box(0,0,4,4),shapely.from_wkb(self.record().source_geometry_wkb)):
                    dst.write({"geometry":mapping(geometry),"properties":{"Form":"Normal"}})
            before={p.name:file_hash(p) for p in shapefile_components(file)}
            snapshot=load_boundary_snapshot(file,revision="r1",space_id="metric-source",valid_coverage=box(-10,-10,10,10),coverage_confirmed=True)
            self.assertEqual(len(snapshot.records),2); self.assertEqual(sum(r.degraded for r in snapshot.records),1)
            self.assertEqual(before,{p.name:file_hash(p) for p in shapefile_components(file)})


def inspect_actual(sample_dir):
    boundaries_files=list(sample_dir.rglob("*_Network_Boundary.shp"));terrain_files=list(sample_dir.rglob("*_Elevation.tif"))
    if len(boundaries_files)!=1 or len(terrain_files)!=1:
        raise AdapterError("AMBIGUOUS_SOURCE","explicit single boundary/terrain pair required")
    all_inputs=set(sample_dir.rglob("*.tif"))
    for p in list(all_inputs): all_inputs.update(item for item in p.parent.glob(p.name+".*") if item.is_file())
    for p in sample_dir.rglob("*.shp"): all_inputs.update(shapefile_components(p))
    before={str(p.relative_to(sample_dir)):file_hash(p) for p in sorted(all_inputs)}
    with rasterio.Env(GDAL_PAM_ENABLED="NO"),rasterio.open(terrain_files[0],"r") as terrain:
        coverage=box(*terrain.bounds);space="EPSG32631-carto-1.0.17-snapshot";crs=terrain.crs
    boundaries=load_boundary_snapshot(boundaries_files[0],revision="source-"+before[str(boundaries_files[0].relative_to(sample_dir))],
        space_id=space,valid_coverage=coverage,coverage_confirmed=True)
    if str(boundaries.crs)!=str(crs): raise AdapterError("CRS_MISMATCH","same source space required")
    report=boundary_snapshot_report(boundaries,include_degraded_sources=True)
    rules=TunnelRules("synthetic-screening-only",1.0,1.0,1.0,3.0)
    zone=PlanningZone("whole-roi-synthetic",coverage,LandUse.RESIDENTIAL,(100,100),True)
    snapshot=Snapshot("screening-r1",space,(zone,),(),True)  # Deliberately permissive synthetic context, not an actual TAZ.
    tests=[]
    for record in boundaries.records:
        if not record.degraded: continue
        guard=record.source_guard
        if guard is None: raise AdapterError("UNLOCATABLE_ACTUAL_BOUNDARY","source could not be located")
        x0,y0,x1,y1=guard.bounds
        corridor=Corridor("screening-"+record.source_record_id,space,LineString([(x0-2,(y0+y1)/2),(x1+2,(y0+y1)/2)]),0.5,
                          Mode.ELEVATED,frozenset({Mode.SURFACE,Mode.ELEVATED,Mode.TUNNEL}),999.0)
        result=decide_with_boundary_quality(corridor,snapshot,rules,boundaries,
                                           expected_revision=snapshot.revision,expected_boundary_revision=boundaries.revision)
        oid=boundaries.object_id(record)
        if (result.status==PolicyStatus.POLICY_CLEAR or Mode.ELEVATED in result.permitted_modes or Mode.SURFACE in result.permitted_modes or
                oid in result.demolition_candidate_ids or oid not in result.mandatory_object_ids):
            raise AdapterError("QUALITY_BYPASS","degraded record escaped core gate")
        tests.append({"source_record_id":record.source_record_id,"policy_status":result.status.value,"reason":result.reason,
                      "permitted_modes":sorted(mode.value for mode in result.permitted_modes),"retained_as_mandatory":True,"demolition_candidate":False})
    after={str(p.relative_to(sample_dir)):file_hash(p) for p in sorted(all_inputs)}
    if before!=after: raise AdapterError("SOURCE_CHANGED","sample changed")
    return {"boundary_retention":report,"core_screening_cases":tests,"core_screening_count":len(tests),
            "synthetic_candidate_context":True,"actual_planning_zone_association_verified":False,
            "source_hashes":before,"source_unchanged":True,"physical_envelopes_accepted":False,
            "scope":"all original records retained; synthetic probes exercise real degraded source bounds"}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--sample-dir",type=Path);parser.add_argument("--report",type=Path)
    args=parser.parse_args()
    if args.sample_dir and not args.report: parser.error("actual check requires local report")
    if args.report and args.report.exists(): parser.error("choose a new report")
    if args.sample_dir and args.report:
        source,target=args.sample_dir.resolve(),args.report.resolve()
        if target==source or source in target.parents: parser.error("report must be outside source")
    suite=unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in
        (CartoChecks,ProjectionChecks,MappingChecks,VersionBindingChecks,WaterInputChecks,BoundaryQualityChecks)])
    result=unittest.TextTestRunner(stream=io.StringIO(),verbosity=1).run(suite)
    report={"schema_version":"p0-02-boundary-quality-report-0.1.0","timestamp":datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
            "synthetic":{"run":result.testsRun,"failures":len(result.failures),"errors":len(result.errors),"skipped":len(result.skipped)},
            "test_failures":[{"case":str(case),"summary":error.strip().splitlines()[-1][:500]} for case,error in result.failures+result.errors],
            "actual":None,"actual_status":"not_run"}
    success=result.wasSuccessful()
    if success and args.sample_dir:
        try:
            report["actual"]=inspect_actual(args.sample_dir.resolve());report["actual_status"]="boundary_quality_exit_verified"
        except (AdapterError,OSError,ValueError,fiona.errors.FionaError,rasterio.errors.RasterioError) as exc:
            report["actual_status"]="failed";report["actual_error"]=str(exc);success=False
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True);temp=args.report.with_name(args.report.name+".tmp")
        temp.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8");temp.replace(args.report)
    print("P0-02 boundary quality: synthetic="+str(result.testsRun)+"; failures="+str(len(result.failures))+"; errors="+str(len(result.errors))+"; skipped="+str(len(result.skipped)))
    for failure in report["test_failures"]: print("FAIL: "+failure["case"]+": "+failure["summary"])
    if report["actual"]:
        actual=report["actual"];retention=actual["boundary_retention"]
        print("Boundary records="+str(retention["source_count"])+"; retained="+str(retention["retained_count"])+"; dropped="+str(retention["records_dropped"]))
        print("Degraded retained="+str(retention["degraded_retained_count"])+"; unlocatable="+str(retention["unlocatable_count"])+"; core probes="+str(actual["core_screening_count"]))
        print("Candidates="+str(retention["candidate_counts"])+"; physical acceptance=False; raw sources unchanged="+str(actual["source_unchanged"]))
    if not success: print("FAIL: "+report.get("actual_error","synthetic checks failed"))
    print("ALL P0-02 BOUNDARY QUALITY CHECKS PASSED" if success else "P0-02 BOUNDARY QUALITY CHECKS FAILED")
    return 0 if success else 1


if __name__=="__main__":
    raise SystemExit(main())
