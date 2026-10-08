# Run: conda run -n csl2-planner --no-capture-output python -s scripts/check_p0_jobs.py
# Expected: [SUCCESS] P0-04-A/B/C checks passed (N cases); synthetic only.
# SPDX-License-Identifier: GPL-3.0-only
"""Self-contained C0 contract, lifecycle and acceptance checks; no Qt or real data."""
from __future__ import annotations

import dataclasses
import enum
import importlib.util
import inspect
import io
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, Callable

for package in ("pyproj", "shapely"):
    if importlib.util.find_spec(package) is None:
        print("[ENVIRONMENT REQUIRED] Use the csl2-planner environment described in ENVIRONMENT.md.")
        raise SystemExit(2)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from prototypes.p0.contracts import ContractError
    from prototypes.p0.job_contracts import (
        CheckState, DataState, Diagnostic, EnvelopeViewData, GeometryType, JobEvent,
        JobIdentity, JobSpec, JobState, MapLayer, ModuleId, ProfileSample, ProfileViewData,
        ProjectSummary, ProgressUnit, ResourceFormat, ResourcePurpose, ResourceRef,
        ResultManifest, Severity, SpaceRef, VersionKey,
    )
    from prototypes.p0.job_lifecycle import (
        CancelStatus, FakeStage, FakeWorker, JobLifecycle, run_fake_job,
    )
    from prototypes.p0 import job_acceptance, job_contracts, job_edits, job_fixtures, job_lifecycle
    from prototypes.p0.job_acceptance import AcceptanceState, assess_result_acceptance
    from prototypes.p0.job_edits import (
        EditAction, EditActor, EditCommand, EditState, EditTarget, LockEntry, LockManifest,
        ObjectKind, UndoContract, UndoStack, assess_edit, assess_undo,
    )
except (ImportError, OSError) as exc:
    print(f"[ENVIRONMENT REQUIRED] C0 imports failed: {type(exc).__name__}; see ENVIRONMENT.md.")
    raise SystemExit(2)


HASH = "a" * 64


def versions(*, boundary: str | None = "boundary-r1") -> VersionKey:
    return VersionKey("input-r1", "parameters-r1", "synthetic-model-v0", "synthetic-catalog-r1",
                      "synthetic-rules-r1", boundary)


def space(*, vertical: str | None = "synthetic-game-height-v1") -> SpaceRef:
    return SpaceRef("synthetic-metric-local", "EPSG:32631", vertical)


def resource(name: str = "dem.tif", *, purpose: ResourcePurpose = ResourcePurpose.INPUT,
             fmt: ResourceFormat = ResourceFormat.GEOTIFF) -> ResourceRef:
    return ResourceRef(name.replace(".", "-"), name, purpose, fmt, HASH, 128)


def identity() -> JobIdentity:
    return JobIdentity("synthetic-project-1", "synthetic-scenario-1", "synthetic-job-1")


def diagnostic(code: str = "DATA_GAP", severity: Severity = Severity.WARNING) -> Diagnostic:
    return Diagnostic(code, "synthetic diagnostic", severity)


def job(*, boundary: str | None = "boundary-r1", uses_boundary: bool = False) -> JobSpec:
    return JobSpec(identity(), ModuleId.M1, versions(boundary=boundary), space(),
                   (resource(),), "synthetic-cancel-1", uses_boundary)


def result() -> ResultManifest:
    return ResultManifest(identity(), ModuleId.M1, versions(), space(), JobState.SUCCEEDED,
                          (resource("result.json", purpose=ResourcePurpose.ARTIFACT, fmt=ResourceFormat.JSON),))


def profile() -> ProfileViewData:
    return ProfileViewData("align-1", versions(), space(),
                           (ProfileSample(0, 1), ProfileSample(10, 2)), DataState.READY)


def envelope() -> EnvelopeViewData:
    return EnvelopeViewData("env-1", "align-1", versions(), space(),
                            resource("envelope.gpkg", purpose=ResourcePurpose.ARTIFACT,
                                     fmt=ResourceFormat.GEOPACKAGE),
                            DataState.READY, source_layer="envelopes")


def summary() -> ProjectSummary:
    view = envelope()
    return ProjectSummary("synthetic-project-1", "synthetic-scenario-1", versions(), space(),
                          tuple((module, None) for module in ModuleId), (view.resource,),
                          profiles=(profile(),), envelopes=(view,))


class JobContractChecks(unittest.TestCase):
    def assertCode(self, code: str, function: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        with self.assertRaises(ContractError) as context:
            function(*args, **kwargs)
        self.assertEqual(context.exception.code, code)

    def test_valid_job_is_typed_and_immutable(self) -> None:
        value = job(uses_boundary=True)
        self.assertEqual(value.versions.boundary_revision, "boundary-r1")
        with self.assertRaises(FrozenInstanceError):
            setattr(value, "cancel_token", "other")
        with self.assertRaises(FrozenInstanceError):
            setattr(value.versions, "input_revision", "other")

    def test_boundary_job_requires_independent_revision(self) -> None:
        self.assertCode("MISSING_BOUNDARY_REVISION", job, boundary=None, uses_boundary=True)
        self.assertIsNone(job(boundary=None).versions.boundary_revision)

    def test_all_revision_keys_affect_equality(self) -> None:
        original = versions()
        for field in ("input_revision", "parameter_revision", "model_version", "catalog_version",
                      "rule_version", "boundary_revision"):
            with self.subTest(field=field):
                self.assertNotEqual(original, replace(original, **{field: "changed"}))

    def test_unsupported_schema_is_rejected(self) -> None:
        self.assertCode("UNSUPPORTED_JOB_SCHEMA", replace, versions(), schema_version="unknown")

    def test_raw_enum_and_untyped_context_are_rejected(self) -> None:
        self.assertCode("INVALID_ENUM", replace, job(), module_id="M1")
        self.assertCode("INVALID_JOB_CONTEXT", replace, job(), identity={"job_id": "job-1"})
        self.assertCode("INVALID_BOUNDARY_BINDING", replace, job(), uses_boundary_data=1)

    def test_identifiers_are_bounded_and_explicit(self) -> None:
        for value in ("", " padded ", "line\nbreak", "x" * 129, 42):
            with self.subTest(value=repr(value)[:30]):
                self.assertCode("INVALID_ID", JobIdentity, value, "scenario", "job")

    def test_space_must_be_projected_metres(self) -> None:
        self.assertCode("NON_METRIC_CRS", SpaceRef, "geographic", "EPSG:4326", None)
        self.assertCode("NON_METRIC_CRS", SpaceRef, "feet", "EPSG:2263", None)

    def test_crs_parse_error_has_stable_code(self) -> None:
        self.assertCode("INVALID_CRS", SpaceRef, "unknown", "not-a-crs", None)

    def test_units_are_not_silently_converted(self) -> None:
        self.assertCode("INVALID_SPACE_UNIT", replace, space(), horizontal_unit="ft")
        self.assertCode("INVALID_SPACE_UNIT", replace, space(), elevation_unit="ft")
        self.assertCode("INVALID_VIEW_UNIT", replace, profile(), station_unit="km")

    def test_unknown_vertical_reference_remains_unknown(self) -> None:
        self.assertIsNone(space(vertical=None).vertical_reference)
        self.assertCode("INVALID_ID", replace, space(), vertical_reference="")

    def test_resource_path_is_relative_and_windows_safe(self) -> None:
        for path in ("../dem.tif", "/tmp/dem.tif", "C:/dem.tif", "C:dem.tif", "//host/share/a",
                     "data\\dem.tif", "data//dem.tif", "data/./dem.tif", "data/../dem.tif",
                     "CON.json", "data/AUX.txt", "data/a.", "data/a ", "a:b", "data/*.tif",
                     "data/a\x00.json"):
            with self.subTest(path=path):
                self.assertCode("INVALID_RESOURCE_PATH", ResourceRef, "r", path,
                                ResourcePurpose.INPUT, ResourceFormat.JSON, HASH, 1)
        self.assertEqual(resource("data/dem.tif").relative_path, "data/dem.tif")

    def test_resource_hash_and_size_are_bounded(self) -> None:
        for digest in ("A" * 64, "z" * 64, "a" * 63, None):
            with self.subTest(digest=repr(digest)[:15]):
                self.assertCode("INVALID_RESOURCE_HASH", replace, resource(), sha256=digest)
        for size in (-1, True, 1.5, 2**63):
            with self.subTest(size=size):
                self.assertCode("INVALID_INTEGER", replace, resource(), size_bytes=size)

    def test_resources_are_immutable_bounded_and_unique(self) -> None:
        self.assertCode("INVALID_COLLECTION", replace, job(), input_resources=[resource()])
        self.assertCode("INVALID_COLLECTION", replace, job(), input_resources=(resource(),) * 4097)
        self.assertCode("DUPLICATE_RESOURCE", replace, job(), input_resources=(resource(), resource()))
        alias = replace(resource(), resource_id="alias", relative_path="DEM.TIF")
        self.assertCode("DUPLICATE_RESOURCE_PATH", replace, job(), input_resources=(resource(), alias))

    def test_input_and_artifact_ports_keep_resource_purpose(self) -> None:
        self.assertCode("INVALID_RESOURCE_PURPOSE", replace, job(),
                        input_resources=(resource(purpose=ResourcePurpose.ARTIFACT),))
        self.assertCode("INVALID_RESOURCE_PURPOSE", replace, result(), artifacts=(resource(),))

    def test_event_progress_allows_unknown_total(self) -> None:
        event = JobEvent(identity(), versions(), 1, JobState.RUNNING, "load", 3, None, ProgressUnit.STEPS)
        self.assertIsNone(event.total)
        self.assertEqual(event.completed, 3)
        self.assertCode("INVALID_ID", replace, event, stage_id=None)

    def test_event_progress_rejects_inconsistent_counts(self) -> None:
        event = JobEvent(identity(), versions(), 1, JobState.RUNNING, "load", 3, 5, ProgressUnit.ITEMS)
        self.assertCode("INVALID_PROGRESS", replace, event, total=2)
        self.assertCode("INVALID_INTEGER", replace, event, completed=True)
        self.assertCode("INVALID_PROGRESS", replace, event, completed=None)
        self.assertCode("INVALID_PROGRESS", replace, event, state=JobState.QUEUED)
        self.assertCode("INVALID_INTEGER", replace, event, sequence=-1)

    def test_event_failure_has_error_code_without_fake_success(self) -> None:
        failed = JobEvent(identity(), versions(), 2, JobState.FAILED, error_code="INPUT_INVALID")
        self.assertEqual(failed.error_code, "INPUT_INVALID")
        self.assertCode("INVALID_ID", JobEvent, identity(), versions(), 2, JobState.FAILED)
        self.assertCode("INVALID_JOB_ERROR", replace, failed, state=JobState.SUCCEEDED)

    def test_stage_only_and_cancelled_events_are_representable(self) -> None:
        stage = JobEvent(identity(), versions(), 2, JobState.RUNNING, stage_id="prepare")
        cancelled = JobEvent(identity(), versions(), 3, JobState.CANCELLED)
        self.assertIsNone(stage.completed)
        self.assertEqual(cancelled.state, JobState.CANCELLED)

    def test_result_separates_completion_from_validation(self) -> None:
        value = result()
        self.assertEqual(value.validation_state, CheckState.NOT_CHECKED)
        self.assertFalse(hasattr(value, "accepted"))
        self.assertEqual(replace(value, validation_state=CheckState.PASSED).job_state, JobState.SUCCEEDED)
        insufficient = replace(value, validation_state=CheckState.DATA_INSUFFICIENT,
                               diagnostics=(diagnostic(),))
        self.assertEqual(insufficient.job_state, JobState.SUCCEEDED)

    def test_result_requires_terminal_state_and_reasons(self) -> None:
        self.assertCode("NON_TERMINAL_RESULT", replace, result(), job_state=JobState.RUNNING)
        self.assertCode("MISSING_DIAGNOSTIC", replace, result(), validation_state=CheckState.FAILED)
        self.assertCode("INVALID_RESULT_VALIDATION", replace, result(),
                        job_state=JobState.CANCELLED, validation_state=CheckState.PASSED)

    def test_failed_result_needs_error_diagnostic(self) -> None:
        self.assertCode("MISSING_JOB_ERROR", replace, result(), job_state=JobState.FAILED,
                        diagnostics=(diagnostic(),))
        failed = replace(result(), job_state=JobState.FAILED,
                         diagnostics=(diagnostic("FAILED", Severity.ERROR),))
        self.assertEqual(failed.job_state, JobState.FAILED)

    def test_diagnostic_is_typed_and_bounded(self) -> None:
        self.assertCode("INVALID_DIAGNOSTIC", replace, diagnostic(), message="")
        self.assertCode("INVALID_DIAGNOSTIC", replace, diagnostic(), message="x" * 2049)
        self.assertCode("INVALID_ENUM", replace, diagnostic(), severity="warning")

    def test_map_layers_keep_resource_format_and_source_layer(self) -> None:
        layer = MapLayer("terrain", versions(), space(), resource(), GeometryType.RASTER)
        self.assertEqual(layer.data_state, DataState.READY)
        self.assertCode("INVALID_VIEW_RESOURCE", replace, layer,
                        resource=resource("layer.json", fmt=ResourceFormat.JSON))
        vector = MapLayer("roads", versions(), space(), envelope().resource, GeometryType.POLYGON,
                          source_layer="envelopes")
        self.assertCode("INVALID_ID", replace, vector, source_layer=None)
        geojson = replace(vector, resource=resource("roads.geojson", fmt=ResourceFormat.GEOJSON),
                          source_layer=None)
        self.assertCode("INVALID_SOURCE_LAYER", replace, geojson, source_layer="roads")

    def test_degraded_views_preserve_diagnostics(self) -> None:
        layer = MapLayer("terrain", versions(), space(), resource(), GeometryType.RASTER,
                         DataState.DEGRADED, (diagnostic("SOURCE_DEGRADED"),))
        self.assertEqual(layer.diagnostics[0].code, "SOURCE_DEGRADED")
        self.assertCode("MISSING_DIAGNOSTIC", replace, layer, diagnostics=())

    def test_profile_missing_elevation_is_not_zero_or_ready(self) -> None:
        samples = (ProfileSample(0, None), ProfileSample(10, None))
        view = ProfileViewData("align-1", versions(), space(), samples,
                               DataState.DATA_INSUFFICIENT, (diagnostic(),))
        self.assertIsNone(view.samples[0].elevation_m)
        self.assertCode("INVALID_VIEW_READINESS", replace, view, data_state=DataState.READY)
        self.assertCode("INVALID_VIEW_READINESS", replace, view, data_state=DataState.DEGRADED)

    def test_profile_missing_vertical_reference_stays_insufficient(self) -> None:
        view = replace(profile(), space=space(vertical=None), data_state=DataState.DATA_INSUFFICIENT,
                       diagnostics=(diagnostic("NO_VERTICAL_REFERENCE"),))
        self.assertIsNone(view.space.vertical_reference)
        self.assertCode("INVALID_VIEW_READINESS", replace, profile(), space=space(vertical=None))

    def test_profile_empty_and_single_point_are_insufficient(self) -> None:
        for samples in ((), (ProfileSample(0, 1),)):
            with self.subTest(count=len(samples)):
                self.assertCode("INVALID_VIEW_READINESS", replace, profile(), samples=samples)
                view = replace(profile(), samples=samples, data_state=DataState.DATA_INSUFFICIENT,
                               diagnostics=(diagnostic("MISSING_SAMPLES"),))
                self.assertEqual(len(view.samples), len(samples))

    def test_profile_stations_must_increase_and_be_finite(self) -> None:
        for samples in ((ProfileSample(10, 1), ProfileSample(10, 2)),
                        (ProfileSample(10, 1), ProfileSample(0, 2))):
            with self.subTest(samples=samples):
                self.assertCode("INVALID_PROFILE_ORDER", replace, profile(), samples=samples)
        for value in (float("nan"), float("inf"), True, 10**400):
            with self.subTest(value=repr(value)[:15]):
                self.assertCode("INVALID_NUMBER", ProfileSample, value, 1)
                self.assertCode("INVALID_NUMBER", ProfileSample, 0, value)
        self.assertCode("INVALID_NUMBER", ProfileSample, -1, 0)
        self.assertEqual(ProfileSample(0, -10).elevation_m, -10)

    def test_envelope_does_not_compute_or_approve_safety(self) -> None:
        view = envelope()
        self.assertEqual(view.validation_state, CheckState.NOT_CHECKED)
        degraded = replace(view, data_state=DataState.DEGRADED, diagnostics=(diagnostic(),))
        self.assertCode("INVALID_VIEW_VALIDATION", replace, degraded, validation_state=CheckState.PASSED)

    def test_envelope_missing_data_or_reference_is_insufficient(self) -> None:
        self.assertCode("INVALID_VIEW_READINESS", replace, envelope(), space=space(vertical=None))
        missing = replace(envelope(), resource=None, source_layer=None,
                          data_state=DataState.DATA_INSUFFICIENT, diagnostics=(diagnostic(),))
        self.assertIsNone(missing.resource)
        self.assertCode("INVALID_VIEW_READINESS", replace, missing, data_state=DataState.READY)
        self.assertCode("INVALID_SOURCE_LAYER", replace, missing, source_layer="orphan")

    def test_project_summary_contains_five_module_states(self) -> None:
        value = summary()
        self.assertEqual(len(value.module_states), 5)
        self.assertTrue(all(state is None for _, state in value.module_states))
        self.assertCode("INVALID_MODULE_STATES", replace, value, module_states=value.module_states[:4])
        self.assertCode("INVALID_MODULE_STATES", replace, value,
                        module_states=((ModuleId.M1, None),) * 5)

    def test_project_view_versions_and_space_must_match(self) -> None:
        value = summary()
        for field in ("input_revision", "parameter_revision", "model_version", "catalog_version",
                      "rule_version", "boundary_revision"):
            with self.subTest(field=field):
                changed = replace(profile(), versions=replace(versions(), **{field: "changed"}))
                self.assertCode("VIEW_CONTEXT_MISMATCH", replace, value, profiles=(changed,))
        changed = replace(profile(), space=replace(space(), space_id="other-space"))
        self.assertCode("VIEW_CONTEXT_MISMATCH", replace, value, profiles=(changed,))
        changed = replace(profile(), space=replace(space(), vertical_reference="other-reference"))
        self.assertCode("VIEW_CONTEXT_MISMATCH", replace, value, profiles=(changed,))

    def test_project_views_require_exact_registered_resource(self) -> None:
        self.assertCode("UNREGISTERED_VIEW_RESOURCE", replace, summary(), resources=())
        changed = replace(envelope().resource, sha256="b" * 64)
        self.assertCode("UNREGISTERED_VIEW_RESOURCE", replace, summary(), resources=(changed,))

    def test_project_views_are_unique_and_aligned(self) -> None:
        self.assertCode("DUPLICATE_VIEW", replace, summary(), profiles=(profile(), profile()))
        self.assertCode("UNKNOWN_VIEW_ALIGNMENT", replace, summary(), profiles=())


class JobLifecycleChecks(unittest.TestCase):
    def event(self, lifecycle: JobLifecycle, state: JobState, **updates: Any) -> JobEvent:
        fields = dict(identity=lifecycle.spec.identity, versions=lifecycle.spec.versions,
                      sequence=len(lifecycle.events), state=state)
        fields.update(updates)
        return JobEvent(**fields)

    def queued(self) -> JobLifecycle:
        lifecycle = JobLifecycle(job())
        lifecycle.accept(self.event(lifecycle, JobState.QUEUED))
        return lifecycle

    def running(self) -> JobLifecycle:
        lifecycle = self.queued()
        lifecycle.accept(self.event(lifecycle, JobState.RUNNING))
        return lifecycle

    def assertRejected(self, lifecycle: JobLifecycle, event: JobEvent, code: str) -> None:
        before = lifecycle.events
        with self.assertRaises(ContractError) as context:
            lifecycle.accept(event)
        self.assertEqual(context.exception.code, code)
        self.assertEqual(lifecycle.events, before)

    def cancel(self, lifecycle: JobLifecycle) -> CancelStatus:
        return lifecycle.request_cancel(lifecycle.spec.identity, lifecycle.spec.cancel_token)

    def assertClosedRun(self, lifecycle: JobLifecycle, worker: FakeWorker, state: JobState,
                        **kwargs: Any) -> Any:
        run = run_fake_job(lifecycle, worker, **kwargs)
        self.assertEqual(run.manifest.job_state, state)
        self.assertEqual(run.events[-1].state, state)
        self.assertEqual(run.events, lifecycle.events)
        self.assertEqual([event.sequence for event in run.events], list(range(len(run.events))))
        self.assertEqual(sum(event.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)
                             for event in run.events), 1)
        self.assertEqual(worker.cleanup_count, 1)
        self.assertTrue(worker.resource_closed)
        self.assertEqual(run.manifest.identity, lifecycle.spec.identity)
        self.assertEqual(run.manifest.versions, lifecycle.spec.versions)
        self.assertEqual(run.manifest.space, lifecycle.spec.space)
        self.assertEqual(run.manifest.validation_state, CheckState.NOT_CHECKED)
        return run

    def test_success_has_contiguous_events_and_one_terminal(self) -> None:
        lifecycle, worker = JobLifecycle(job()), FakeWorker()
        run = self.assertClosedRun(lifecycle, worker, JobState.SUCCEEDED)
        self.assertEqual([event.state for event in run.events],
                         [JobState.QUEUED] + [JobState.RUNNING] * 4 + [JobState.SUCCEEDED])
        self.assertEqual(worker.steps_completed, 3)
        self.assertEqual(run.manifest.artifacts, ())

    def test_first_event_requires_queued_sequence_zero(self) -> None:
        lifecycle = JobLifecycle(job())
        for state in (JobState.RUNNING, JobState.SUCCEEDED, JobState.CANCELLED, JobState.FAILED):
            with self.subTest(state=state):
                event = self.event(lifecycle, state, error_code="INIT_FAILED" if state == JobState.FAILED else None)
                self.assertRejected(lifecycle, event, "INVALID_JOB_TRANSITION")
        self.assertRejected(lifecycle, self.event(lifecycle, JobState.QUEUED, sequence=1), "JOB_EVENT_SEQUENCE")
        lifecycle.accept(self.event(lifecycle, JobState.QUEUED))
        self.assertEqual(lifecycle.state, JobState.QUEUED)

    def test_queued_cannot_report_success_or_repeat_queued(self) -> None:
        lifecycle = self.queued()
        for state in (JobState.SUCCEEDED, JobState.QUEUED):
            with self.subTest(state=state):
                self.assertRejected(lifecycle, self.event(lifecycle, state), "INVALID_JOB_TRANSITION")
        lifecycle.accept(self.event(lifecycle, JobState.FAILED, error_code="START_FAILED"))
        self.assertEqual(lifecycle.state, JobState.FAILED)

    def test_duplicate_gap_and_reordered_sequences_leave_history_unchanged(self) -> None:
        lifecycle = self.queued()
        for sequence in (0, 2, 5):
            with self.subTest(sequence=sequence):
                self.assertRejected(lifecycle, self.event(lifecycle, JobState.RUNNING, sequence=sequence),
                                    "JOB_EVENT_SEQUENCE")
        lifecycle.accept(self.event(lifecycle, JobState.RUNNING))
        self.assertEqual(lifecycle.events[-1].sequence, 1)

    def test_event_identity_changes_are_rejected(self) -> None:
        lifecycle = self.running()
        for field in ("project_id", "scenario_id", "job_id"):
            with self.subTest(field=field):
                changed = replace(lifecycle.spec.identity, **{field: "other"})
                self.assertRejected(lifecycle, self.event(lifecycle, JobState.RUNNING, identity=changed),
                                    "JOB_EVENT_CONTEXT_MISMATCH")

    def test_each_version_change_is_rejected(self) -> None:
        lifecycle = self.running()
        for field in ("input_revision", "parameter_revision", "boundary_revision",
                      "model_version", "catalog_version", "rule_version"):
            with self.subTest(field=field):
                changed = replace(lifecycle.spec.versions, **{field: "other"})
                self.assertRejected(lifecycle, self.event(lifecycle, JobState.RUNNING, versions=changed),
                                    "JOB_EVENT_CONTEXT_MISMATCH")

    def test_terminal_history_is_closed_for_every_terminal_state(self) -> None:
        for state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
            with self.subTest(state=state):
                lifecycle = self.running()
                if state == JobState.CANCELLED:
                    self.cancel(lifecycle)
                lifecycle.accept(self.event(lifecycle, state, error_code="FAULT" if state == JobState.FAILED else None))
                self.assertRejected(lifecycle, self.event(lifecycle, JobState.RUNNING), "JOB_ALREADY_TERMINAL")

    def test_raw_event_is_rejected_without_changing_state(self) -> None:
        lifecycle = self.queued()
        before = lifecycle.events
        with self.assertRaises(ContractError) as context:
            lifecycle.accept({"state": "running"})
        self.assertEqual(context.exception.code, "INVALID_JOB_EVENT")
        self.assertEqual(lifecycle.events, before)

    def test_progress_cannot_decrease_and_rejection_does_not_advance_sequence(self) -> None:
        lifecycle = self.running()
        lifecycle.accept(self.event(lifecycle, JobState.RUNNING, stage_id="load", completed=3,
                                    total=5, progress_unit=ProgressUnit.STEPS))
        self.assertRejected(lifecycle, self.event(lifecycle, JobState.RUNNING, stage_id="load", completed=2,
                                                 total=5, progress_unit=ProgressUnit.STEPS), "JOB_PROGRESS_REGRESSION")
        lifecycle.accept(self.event(lifecycle, JobState.RUNNING, stage_id="load", completed=4,
                                    total=5, progress_unit=ProgressUnit.STEPS))
        self.assertEqual(lifecycle.events[-1].sequence, 3)

    def test_confirmed_total_cannot_change_or_disappear(self) -> None:
        lifecycle = self.running()
        lifecycle.accept(self.event(lifecycle, JobState.RUNNING, stage_id="load", completed=1,
                                    total=5, progress_unit=ProgressUnit.STEPS))
        for total in (None, 4, 6):
            with self.subTest(total=total):
                self.assertRejected(lifecycle, self.event(lifecycle, JobState.RUNNING, stage_id="load", completed=2,
                                                         total=total, progress_unit=ProgressUnit.STEPS),
                                    "JOB_PROGRESS_TOTAL_CHANGED")
        lifecycle.accept(self.event(lifecycle, JobState.RUNNING, stage_id="load", completed=2,
                                    total=5, progress_unit=ProgressUnit.STEPS))

    def test_counter_unit_cannot_change(self) -> None:
        lifecycle = self.running()
        lifecycle.accept(self.event(lifecycle, JobState.RUNNING, stage_id="load", completed=1,
                                    progress_unit=ProgressUnit.STEPS))
        self.assertRejected(lifecycle, self.event(lifecycle, JobState.RUNNING, stage_id="load", completed=2,
                                                 progress_unit=ProgressUnit.ITEMS), "JOB_PROGRESS_UNIT_CHANGED")

    def test_unknown_total_can_be_confirmed_without_fake_percentage(self) -> None:
        lifecycle = self.running()
        lifecycle.accept(self.event(lifecycle, JobState.RUNNING, stage_id="load"))
        lifecycle.accept(self.event(lifecycle, JobState.RUNNING, stage_id="load", completed=1,
                                    progress_unit=ProgressUnit.STEPS))
        lifecycle.accept(self.event(lifecycle, JobState.RUNNING, stage_id="load", completed=2,
                                    total=3, progress_unit=ProgressUnit.STEPS))
        self.assertIsNone(lifecycle.events[2].completed)
        self.assertIsNone(lifecycle.events[3].total)
        self.assertEqual(lifecycle.events[4].total, 3)

    def test_stage_switch_allows_new_counter_but_retains_old_stage_progress(self) -> None:
        lifecycle = self.running()
        lifecycle.accept(self.event(lifecycle, JobState.RUNNING, stage_id="load", completed=2,
                                    total=3, progress_unit=ProgressUnit.STEPS))
        lifecycle.accept(self.event(lifecycle, JobState.RUNNING, stage_id="solve", completed=0,
                                    total=8, progress_unit=ProgressUnit.ITEMS))
        lifecycle.accept(self.event(lifecycle, JobState.RUNNING, stage_id="load"))
        self.assertRejected(lifecycle, self.event(lifecycle, JobState.RUNNING, stage_id="load", completed=1,
                                                 total=3, progress_unit=ProgressUnit.STEPS), "JOB_PROGRESS_REGRESSION")

    def test_cancel_request_is_idempotent_and_does_not_publish_terminal(self) -> None:
        lifecycle = self.queued()
        before = lifecycle.events
        self.assertEqual(self.cancel(lifecycle), CancelStatus.REQUESTED)
        self.assertEqual(self.cancel(lifecycle), CancelStatus.ALREADY_REQUESTED)
        self.assertTrue(lifecycle.cancellation_requested)
        self.assertEqual(lifecycle.events, before)
        self.assertEqual(lifecycle.state, JobState.QUEUED)

    def test_wrong_cancel_identity_and_token_do_not_request_cancel(self) -> None:
        lifecycle = self.running()
        for target, token, code in (
            (replace(lifecycle.spec.identity, job_id="other"), lifecycle.spec.cancel_token, "JOB_CANCEL_CONTEXT_MISMATCH"),
            (lifecycle.spec.identity, "other-token", "INVALID_CANCEL_TOKEN"),
            (lifecycle.spec.identity, None, "INVALID_CANCEL_TOKEN"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(ContractError) as context:
                    lifecycle.request_cancel(target, token)
                self.assertEqual(context.exception.code, code)
                self.assertFalse(lifecycle.cancellation_requested)

    def test_cancelled_without_matching_request_is_rejected(self) -> None:
        lifecycle = self.running()
        self.assertRejected(lifecycle, self.event(lifecycle, JobState.CANCELLED), "JOB_CANCELLATION_NOT_REQUESTED")

    def test_success_with_pending_cancellation_is_rejected(self) -> None:
        lifecycle = self.running()
        self.cancel(lifecycle)
        self.assertRejected(lifecycle, self.event(lifecycle, JobState.SUCCEEDED), "JOB_CANCELLATION_PENDING")
        lifecycle.accept(self.event(lifecycle, JobState.CANCELLED))

    def test_cancel_after_terminal_returns_finished_without_rewriting_history(self) -> None:
        for state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
            with self.subTest(state=state):
                lifecycle = self.running()
                if state == JobState.CANCELLED:
                    self.cancel(lifecycle)
                lifecycle.accept(self.event(lifecycle, state, error_code="FAULT" if state == JobState.FAILED else None))
                before = lifecycle.events
                self.assertEqual(self.cancel(lifecycle), CancelStatus.JOB_FINISHED)
                self.assertEqual(lifecycle.events, before)

    def test_request_before_enqueue_prevents_worker_start(self) -> None:
        lifecycle, worker = JobLifecycle(job()), FakeWorker()
        self.cancel(lifecycle)
        run = self.assertClosedRun(lifecycle, worker, JobState.CANCELLED)
        self.assertEqual([event.state for event in run.events], [JobState.QUEUED, JobState.CANCELLED])
        self.assertFalse(worker.started)
        self.assertEqual(worker.steps_completed, 0)

    def test_cancellation_at_each_checkpoint_is_confirmed_after_cleanup(self) -> None:
        for target, expected_steps in (("before_start", 0), ("after_prepare", 0),
                                       ("after_progress", 1), ("before_result", 3)):
            with self.subTest(target=target):
                lifecycle, worker = JobLifecycle(job()), FakeWorker()
                observations = []

                def checkpoint(name: str, current: JobLifecycle) -> None:
                    if name == target:
                        self.assertEqual(self.cancel(current), CancelStatus.REQUESTED)
                    if name == "before_result":
                        observations.append((worker.cleanup_count, worker.resource_closed, current.state))

                run = self.assertClosedRun(lifecycle, worker, JobState.CANCELLED, checkpoint=checkpoint)
                self.assertEqual(worker.steps_completed, expected_steps)
                self.assertEqual(len(observations), 1)
                self.assertEqual(observations[0][:2], (1, True))
                self.assertNotIn(observations[0][2], (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED))
                self.assertIn("JOB_CANCELLED", {item.code for item in run.manifest.diagnostics})

    def test_fake_worker_stage_switch_and_unknown_total(self) -> None:
        worker = FakeWorker((FakeStage("load", 2), FakeStage("solve", 2, known_total=False)))
        run = self.assertClosedRun(JobLifecycle(job()), worker, JobState.SUCCEEDED)
        progress = [event for event in run.events if event.completed is not None]
        self.assertEqual([(event.stage_id, event.completed, event.total) for event in progress],
                         [("load", 1, 2), ("load", 2, 2), ("solve", 1, None), ("solve", 2, None)])

    def test_initialization_failure_releases_acquired_resource_and_retains_reason(self) -> None:
        worker = FakeWorker(fail_initialization=True)
        run = self.assertClosedRun(JobLifecycle(job()), worker, JobState.FAILED)
        self.assertEqual(worker.steps_completed, 0)
        self.assertEqual(run.events[-1].error_code, "WORKER_INITIALIZATION_FAILED")
        self.assertIn("SYNTHETIC_INITIALIZATION_FAILED", {item.code for item in run.manifest.diagnostics})

    def test_resource_acquisition_error_is_failed_not_success(self) -> None:
        def acquire() -> Any:
            raise OSError("synthetic resource unavailable")

        worker = FakeWorker(resource_factory=acquire)
        run = self.assertClosedRun(JobLifecycle(job()), worker, JobState.FAILED)
        self.assertEqual(run.events[-1].error_code, "WORKER_INITIALIZATION_FAILED")
        self.assertEqual(worker.steps_completed, 0)

    def test_execution_failure_releases_resource_and_retains_reason(self) -> None:
        worker = FakeWorker(fail_at_step=2)
        run = self.assertClosedRun(JobLifecycle(job()), worker, JobState.FAILED)
        self.assertEqual(worker.steps_completed, 1)
        self.assertEqual(run.events[-1].error_code, "WORKER_EXECUTION_FAILED")
        self.assertIn("SYNTHETIC_EXECUTION_FAILED", {item.code for item in run.manifest.diagnostics})

    def test_checkpoint_faults_are_failed_and_cleanup_runs_once(self) -> None:
        for target in ("before_start", "after_prepare", "after_progress", "before_result"):
            with self.subTest(target=target):
                def checkpoint(name: str, current: JobLifecycle) -> None:
                    if name == target:
                        raise ValueError("synthetic checkpoint failure")

                worker = FakeWorker()
                run = self.assertClosedRun(JobLifecycle(job()), worker, JobState.FAILED, checkpoint=checkpoint)
                self.assertEqual(run.events[-1].error_code, "WORKER_CHECKPOINT_FAILED")

    def test_cleanup_failure_blocks_success(self) -> None:
        worker = FakeWorker(fail_cleanup=True)
        run = self.assertClosedRun(JobLifecycle(job()), worker, JobState.FAILED)
        self.assertEqual(run.events[-1].error_code, "WORKER_CLEANUP_FAILED")
        self.assertIn("SYNTHETIC_CLEANUP_FAILED", {item.code for item in run.manifest.diagnostics})

    def test_cleanup_failure_retains_execution_failure(self) -> None:
        worker = FakeWorker(fail_at_step=2, fail_cleanup=True)
        run = self.assertClosedRun(JobLifecycle(job()), worker, JobState.FAILED)
        codes = {item.code for item in run.manifest.diagnostics}
        self.assertTrue({"WORKER_EXECUTION_FAILED", "SYNTHETIC_EXECUTION_FAILED",
                         "WORKER_CLEANUP_FAILED", "SYNTHETIC_CLEANUP_FAILED"}.issubset(codes))
        self.assertEqual(run.events[-1].error_code, "WORKER_CLEANUP_FAILED")

    def test_cleanup_failure_blocks_cancelled_terminal(self) -> None:
        lifecycle, worker = JobLifecycle(job()), FakeWorker(fail_cleanup=True)
        self.cancel(lifecycle)
        run = self.assertClosedRun(lifecycle, worker, JobState.FAILED)
        self.assertTrue(lifecycle.cancellation_requested)
        self.assertEqual(run.events[-1].error_code, "WORKER_CLEANUP_FAILED")
        self.assertNotIn("JOB_CANCELLED", {item.code for item in run.manifest.diagnostics})

    def test_real_close_error_is_reported_without_claiming_resource_released(self) -> None:
        class CloseFailure(io.BytesIO):
            def close(self) -> None:
                raise OSError("synthetic close failure")

        stream = CloseFailure()
        try:
            worker = FakeWorker(resource_factory=lambda: stream)
            run = run_fake_job(JobLifecycle(job()), worker)
            self.assertEqual(run.manifest.job_state, JobState.FAILED)
            self.assertEqual(run.events[-1].error_code, "WORKER_CLEANUP_FAILED")
            self.assertFalse(worker.resource_closed)
            self.assertEqual(worker.cleanup_count, 1)
        finally:
            io.BytesIO.close(stream)

    def test_terminal_success_is_published_after_resource_cleanup(self) -> None:
        lifecycle, worker = JobLifecycle(job()), FakeWorker()
        observations = []

        def checkpoint(name: str, current: JobLifecycle) -> None:
            if name == "before_result":
                observations.append((worker.cleanup_count, worker.resource_closed, current.state))

        self.assertClosedRun(lifecycle, worker, JobState.SUCCEEDED, checkpoint=checkpoint)
        self.assertEqual(observations, [(1, True, JobState.RUNNING)])

    def test_interrupt_releases_resource_without_fake_terminal(self) -> None:
        lifecycle, worker = JobLifecycle(job()), FakeWorker()

        def checkpoint(name: str, current: JobLifecycle) -> None:
            if name == "after_prepare":
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            run_fake_job(lifecycle, worker, checkpoint=checkpoint)
        self.assertEqual(worker.cleanup_count, 1)
        self.assertTrue(worker.resource_closed)
        self.assertEqual(lifecycle.state, JobState.RUNNING)

    def test_rerun_is_rejected_without_touching_existing_history_or_resources(self) -> None:
        lifecycle, worker = JobLifecycle(job()), FakeWorker()
        self.assertClosedRun(lifecycle, worker, JobState.SUCCEEDED)
        before = lifecycle.events
        with self.assertRaises(ContractError) as context:
            run_fake_job(lifecycle, worker)
        self.assertEqual(context.exception.code, "FAKE_RUN_ALREADY_STARTED")
        self.assertEqual(lifecycle.events, before)
        self.assertEqual(worker.cleanup_count, 1)

    def test_synthetic_plan_bounds_are_checked_before_run(self) -> None:
        for kwargs in ({"stages": ()}, {"stages": (FakeStage("same", 1),) * 2},
                       {"stages": (FakeStage("a", 10_000), FakeStage("b", 1))},
                       {"fail_at_step": 0}, {"fail_at_step": 4},
                       {"fail_initialization": 1}, {"fail_cleanup": "yes"}, {"resource_factory": 42}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ContractError) as context:
                    FakeWorker(**kwargs)
                self.assertEqual(context.exception.code, "INVALID_FAKE_PLAN")
        for steps in (0, -1, True, 10_001):
            with self.subTest(steps=steps):
                with self.assertRaises(ContractError) as context:
                    FakeStage("stage", steps)
                self.assertEqual(context.exception.code, "INVALID_FAKE_PLAN")

    def test_failed_and_cancelled_runs_leave_old_project_snapshot_unchanged(self) -> None:
        old_project = summary()
        before = repr(old_project)
        self.assertClosedRun(JobLifecycle(job()), FakeWorker(fail_at_step=1), JobState.FAILED)
        lifecycle = JobLifecycle(job())
        self.cancel(lifecycle)
        self.assertClosedRun(lifecycle, FakeWorker(), JobState.CANCELLED)
        self.assertEqual(repr(old_project), before)
        self.assertTrue(all(state is None for _, state in old_project.module_states))


class JobAcceptanceChecks(unittest.TestCase):
    def fixture(self) -> tuple[ProjectSummary, JobSpec, JobLifecycle, ResultManifest]:
        spec = job()
        project = ProjectSummary(spec.identity.project_id, spec.identity.scenario_id,
                                 spec.versions, spec.space, tuple((module, None) for module in ModuleId),
                                 spec.input_resources)
        lifecycle = JobLifecycle(spec)
        run = run_fake_job(lifecycle, FakeWorker())
        manifest = replace(run.manifest, validation_state=CheckState.PASSED,
                           artifacts=(resource("result.json", purpose=ResourcePurpose.ARTIFACT,
                                               fmt=ResourceFormat.JSON),))
        return project, spec, lifecycle, manifest

    def assertRejected(self, values: tuple, code: str, state: AcceptanceState) -> Any:
        decision = assess_result_acceptance(*values)
        self.assertFalse(decision.acceptable)
        self.assertEqual(decision.state, state)
        self.assertIn(code, {issue.code for issue in decision.issues})
        return decision

    def test_current_successful_checked_result_is_acceptable(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        decision = assess_result_acceptance(project, spec, lifecycle, manifest)
        self.assertTrue(decision.acceptable)
        self.assertEqual(decision.state, AcceptanceState.ACCEPTABLE)
        self.assertEqual(decision.issues, ())
        self.assertEqual(decision.current_project, project)
        self.assertEqual(decision.registered_job, spec)
        self.assertEqual(decision.lifecycle_spec, spec)
        self.assertEqual(decision.terminal_event, lifecycle.events[-1])
        self.assertEqual(decision.result, manifest)
        self.assertFalse(decision.cancellation_requested)

    def test_each_current_revision_change_rejects_old_result(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        for field in ("input_revision", "parameter_revision", "boundary_revision",
                      "model_version", "catalog_version", "rule_version"):
            with self.subTest(field=field):
                current = replace(project, versions=replace(project.versions, **{field: "changed"}))
                decision = self.assertRejected((current, spec, lifecycle, manifest),
                                               "VERSION_CHANGED", AcceptanceState.REJECTED_STALE)
                self.assertIn(f"result.versions.{field}", {issue.field for issue in decision.issues})

    def test_new_registered_job_rejects_old_result_at_same_version(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        registered = replace(spec, identity=replace(spec.identity, job_id="new-job"), cancel_token="new-token")
        decision = self.assertRejected((project, registered, lifecycle, manifest),
                                       "JOB_REPLACED", AcceptanceState.REJECTED_STALE)
        self.assertIn("result.identity.job_id", {issue.field for issue in decision.issues})

    def test_new_job_can_replace_registration_and_pass_without_changing_input(self) -> None:
        project, spec, _, manifest = self.fixture()
        registered = replace(spec, identity=replace(spec.identity, job_id="new-job"), cancel_token="new-token")
        lifecycle = JobLifecycle(registered)
        run_fake_job(lifecycle, FakeWorker())
        candidate = replace(manifest, identity=registered.identity)
        self.assertTrue(assess_result_acceptance(project, registered, lifecycle, candidate).acceptable)

    def test_project_and_scenario_identity_mismatch_are_invalid(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        for field in ("project_id", "scenario_id"):
            for target in ("project", "registered", "result"):
                with self.subTest(field=field, target=target):
                    current = replace(project, **{field: "other"}) if target == "project" else project
                    registered = replace(spec, identity=replace(spec.identity, **{field: "other"})) if target == "registered" else spec
                    candidate = replace(manifest, identity=replace(manifest.identity, **{field: "other"})) if target == "result" else manifest
                    self.assertRejected((current, registered, lifecycle, candidate),
                                        "PROJECT_IDENTITY_MISMATCH", AcceptanceState.REJECTED_INVALID)

    def test_wrong_result_job_id_is_stale(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        candidate = replace(manifest, identity=replace(manifest.identity, job_id="other-job"))
        self.assertRejected((project, spec, lifecycle, candidate), "JOB_REPLACED", AcceptanceState.REJECTED_STALE)

    def test_wrong_result_or_lifecycle_module_is_invalid(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        self.assertRejected((project, spec, lifecycle, replace(manifest, module_id=ModuleId.M2)),
                            "JOB_MODULE_MISMATCH", AcceptanceState.REJECTED_INVALID)
        other = JobLifecycle(replace(spec, module_id=ModuleId.M2))
        run_fake_job(other, FakeWorker())
        self.assertRejected((project, spec, other, manifest), "JOB_MODULE_MISMATCH", AcceptanceState.REJECTED_INVALID)

    def test_changed_registered_spec_is_rejected_even_with_same_job_id(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        registered = replace(spec, cancel_token="different-token")
        self.assertRejected((project, registered, lifecycle, manifest),
                            "LIFECYCLE_SPEC_CHANGED", AcceptanceState.REJECTED_STALE)

    def test_result_versions_must_match_current_and_registered_spec(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        for field in ("input_revision", "parameter_revision", "boundary_revision",
                      "model_version", "catalog_version", "rule_version"):
            with self.subTest(field=field):
                candidate = replace(manifest, versions=replace(manifest.versions, **{field: "other"}))
                decision = self.assertRejected((project, spec, lifecycle, candidate),
                                               "VERSION_CHANGED", AcceptanceState.REJECTED_STALE)
                self.assertIn(f"result_binding.versions.{field}", {issue.field for issue in decision.issues})

    def test_missing_boundary_binding_does_not_match_present_revision(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        candidate = replace(manifest, versions=replace(manifest.versions, boundary_revision=None))
        self.assertRejected((project, spec, lifecycle, candidate), "VERSION_CHANGED", AcceptanceState.REJECTED_STALE)

    def test_space_and_vertical_reference_changes_are_stale(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        for updates in ({"space_id": "other-space"}, {"crs": "EPSG:32632"},
                        {"vertical_reference": "other-height-reference"}, {"vertical_reference": None}):
            with self.subTest(updates=updates):
                current = replace(project, space=replace(project.space, **updates))
                self.assertRejected((current, spec, lifecycle, manifest), "SPACE_CHANGED", AcceptanceState.REJECTED_STALE)
                candidate = replace(manifest, space=replace(manifest.space, **updates))
                self.assertRejected((project, spec, lifecycle, candidate), "SPACE_CHANGED", AcceptanceState.REJECTED_STALE)

    def test_lifecycle_must_have_successful_terminal(self) -> None:
        project, spec, _, manifest = self.fixture()
        for state in (None, JobState.QUEUED, JobState.RUNNING):
            with self.subTest(state=state):
                lifecycle = JobLifecycle(spec)
                if state is not None:
                    lifecycle.accept(JobEvent(spec.identity, spec.versions, 0, JobState.QUEUED))
                if state == JobState.RUNNING:
                    lifecycle.accept(JobEvent(spec.identity, spec.versions, 1, JobState.RUNNING))
                decision = self.assertRejected((project, spec, lifecycle, manifest),
                                               "LIFECYCLE_NOT_SUCCESSFUL", AcceptanceState.REJECTED_INVALID)
                self.assertIsNone(decision.terminal_event)

    def test_failed_lifecycle_cannot_accept_claimed_successful_manifest(self) -> None:
        project, spec, _, manifest = self.fixture()
        lifecycle = JobLifecycle(spec)
        run_fake_job(lifecycle, FakeWorker(fail_at_step=1))
        decision = self.assertRejected((project, spec, lifecycle, manifest),
                                       "LIFECYCLE_NOT_SUCCESSFUL", AcceptanceState.REJECTED_INVALID)
        self.assertIn("TERMINAL_STATE_MISMATCH", {issue.code for issue in decision.issues})

    def test_failed_and_cancelled_results_are_rejected(self) -> None:
        project, spec, _, _ = self.fixture()
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                lifecycle = JobLifecycle(spec)
                if cancel:
                    lifecycle.request_cancel(spec.identity, spec.cancel_token)
                run = run_fake_job(lifecycle, FakeWorker(fail_at_step=None if cancel else 1))
                decision = self.assertRejected((project, spec, lifecycle, run.manifest),
                                               "RESULT_JOB_NOT_SUCCESSFUL", AcceptanceState.REJECTED_INVALID)
                self.assertEqual(decision.cancellation_requested, cancel)
                if cancel:
                    self.assertIn("JOB_CANCELLATION_PENDING", {issue.code for issue in decision.issues})

    def test_manifest_terminal_must_agree_with_lifecycle(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        candidate = replace(manifest, job_state=JobState.CANCELLED, validation_state=CheckState.NOT_CHECKED)
        self.assertRejected((project, spec, lifecycle, candidate),
                            "TERMINAL_STATE_MISMATCH", AcceptanceState.REJECTED_INVALID)

    def test_unchecked_failed_and_insufficient_checks_are_rejected(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        for state in (CheckState.NOT_CHECKED, CheckState.FAILED, CheckState.DATA_INSUFFICIENT):
            with self.subTest(state=state):
                candidate = replace(manifest, validation_state=state, diagnostics=(diagnostic(),))
                self.assertRejected((project, spec, lifecycle, candidate),
                                    "RESULT_CHECKS_NOT_PASSED", AcceptanceState.REJECTED_INVALID)

    def test_error_diagnostic_overrides_passed_checks(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        candidate = replace(manifest, diagnostics=(diagnostic("CONSTRAINT_FAILED", Severity.ERROR),))
        self.assertRejected((project, spec, lifecycle, candidate),
                            "RESULT_ERROR_DIAGNOSTIC", AcceptanceState.REJECTED_INVALID)

    def test_warning_does_not_override_explicit_passed_checks(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        candidate = replace(manifest, diagnostics=(diagnostic("THEORETICAL_ASSUMPTION"),))
        self.assertTrue(assess_result_acceptance(project, spec, lifecycle, candidate).acceptable)

    def test_missing_input_is_stale_even_if_revision_is_unchanged(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        self.assertRejected((replace(project, resources=()), spec, lifecycle, manifest),
                            "INPUT_RESOURCE_MISSING", AcceptanceState.REJECTED_STALE)

    def test_input_descriptor_changes_are_stale_without_revision_change(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        for field, value in (("relative_path", "data/moved.tif"), ("purpose", ResourcePurpose.ARTIFACT),
                             ("format", ResourceFormat.JSON), ("sha256", "b" * 64), ("size_bytes", 129)):
            with self.subTest(field=field):
                changed = replace(project.resources[0], **{field: value})
                current = replace(project, resources=(changed,))
                decision = self.assertRejected((current, spec, lifecycle, manifest),
                                               "INPUT_RESOURCE_CHANGED", AcceptanceState.REJECTED_STALE)
                self.assertIn(f"registered_job.input_resources[0].{field}",
                              {issue.field for issue in decision.issues})

    def test_artifact_id_cannot_replace_different_content(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        artifact = manifest.artifacts[0]
        previous = replace(artifact, relative_path="previous.json", sha256="b" * 64)
        current = replace(project, resources=project.resources + (previous,))
        self.assertRejected((current, spec, lifecycle, manifest), "ARTIFACT_ID_CONFLICT", AcceptanceState.REJECTED_INVALID)

    def test_artifact_path_collision_is_case_insensitive_on_windows(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        artifact = manifest.artifacts[0]
        previous = replace(artifact, resource_id="other-artifact", relative_path="RESULT.JSON", sha256="b" * 64)
        current = replace(project, resources=project.resources + (previous,))
        self.assertRejected((current, spec, lifecycle, manifest), "ARTIFACT_PATH_CONFLICT", AcceptanceState.REJECTED_INVALID)

    def test_artifact_cannot_replace_current_input(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        candidate = replace(manifest, artifacts=(replace(spec.input_resources[0], purpose=ResourcePurpose.ARTIFACT),))
        decision = self.assertRejected((project, spec, lifecycle, candidate),
                                       "ARTIFACT_ID_CONFLICT", AcceptanceState.REJECTED_INVALID)
        self.assertIn("ARTIFACT_PATH_CONFLICT", {issue.code for issue in decision.issues})

    def test_identical_existing_artifact_reference_is_allowed(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        current = replace(project, resources=project.resources + manifest.artifacts)
        self.assertTrue(assess_result_acceptance(current, spec, lifecycle, manifest).acceptable)

    def test_resource_index_order_does_not_change_input_binding(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        other = resource("other.json", fmt=ResourceFormat.JSON)
        current = replace(project, resources=(other,) + project.resources)
        self.assertTrue(assess_result_acceptance(current, spec, lifecycle, manifest).acceptable)

    def test_pure_assessment_preserves_project_lifecycle_and_result(self) -> None:
        values = self.fixture()
        project, spec, lifecycle, manifest = values
        before = (repr(project), repr(spec), lifecycle.events, lifecycle.cancellation_requested, repr(manifest))
        assess_result_acceptance(*values)
        assess_result_acceptance(replace(project, versions=replace(project.versions, input_revision="new-edit")),
                                 spec, lifecycle, manifest)
        after = (repr(project), repr(spec), lifecycle.events, lifecycle.cancellation_requested, repr(manifest))
        self.assertEqual(after, before)

    def test_decision_and_rejection_records_are_immutable(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        current = replace(project, versions=replace(project.versions, parameter_revision="new-parameters"))
        decision = assess_result_acceptance(current, spec, lifecycle, manifest)
        with self.assertRaises(FrozenInstanceError):
            setattr(decision, "state", AcceptanceState.ACCEPTABLE)
        with self.assertRaises(FrozenInstanceError):
            setattr(decision.issues[0], "code", "IGNORED")
        with self.assertRaises(ContractError) as context:
            replace(decision, state=AcceptanceState.ACCEPTABLE)
        self.assertEqual(context.exception.code, "INVALID_ACCEPTANCE_STATE")

    def test_reassessment_after_edit_invalidates_old_acceptable_decision(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        old = assess_result_acceptance(project, spec, lifecycle, manifest)
        current = replace(project, versions=replace(project.versions, input_revision="manual-edit-r2"))
        new = assess_result_acceptance(current, spec, lifecycle, manifest)
        self.assertTrue(old.acceptable)
        self.assertEqual(old.current_project, project)
        self.assertFalse(new.acceptable)
        self.assertEqual(new.current_project, current)

    def test_rejection_order_is_deterministic_and_invalid_takes_precedence(self) -> None:
        project, spec, lifecycle, manifest = self.fixture()
        current = replace(project, versions=replace(project.versions, input_revision="new"), resources=())
        candidate = replace(manifest, validation_state=CheckState.NOT_CHECKED)
        first = assess_result_acceptance(current, spec, lifecycle, candidate)
        second = assess_result_acceptance(current, spec, lifecycle, candidate)
        self.assertEqual(first, second)
        self.assertEqual(first.state, AcceptanceState.REJECTED_INVALID)
        self.assertEqual([(issue.code, issue.field) for issue in first.issues], [
            ("VERSION_CHANGED", "registered_job.versions.input_revision"),
            ("VERSION_CHANGED", "result.versions.input_revision"),
            ("INPUT_RESOURCE_MISSING", "registered_job.input_resources[0]"),
            ("RESULT_CHECKS_NOT_PASSED", "result.validation_state"),
        ])

    def test_raw_context_is_rejected_with_stable_error_code(self) -> None:
        values = self.fixture()
        for index in range(4):
            with self.subTest(index=index):
                invalid = list(values)
                invalid[index] = {}
                with self.assertRaises(ContractError) as context:
                    assess_result_acceptance(*invalid)
                self.assertEqual(context.exception.code, "INVALID_ACCEPTANCE_CONTEXT")


def edit_target(object_id: str = "road-1", kind: ObjectKind = ObjectKind.GEOMETRY,
                zone_id: str = "zone-1") -> EditTarget:
    return EditTarget(object_id, kind, zone_id)


def edit_command(**updates: Any) -> EditCommand:
    fields: dict[str, Any] = dict(
        command_id="cmd-1", project_id="synthetic-project-1", scenario_id="synthetic-scenario-1",
        actor=EditActor.USER, action=EditAction.MODIFY, scope_zone_id="zone-1",
        targets=(edit_target(),), expected_versions=versions(), expected_lock_revision="locks-r1")
    fields.update(updates)
    return EditCommand(**fields)


def edit_project() -> ProjectSummary:
    return ProjectSummary("synthetic-project-1", "synthetic-scenario-1", versions(), space(),
                          tuple((module, None) for module in ModuleId), (resource(),))


def lock_manifest(*entries: LockEntry, revision: str = "locks-r1") -> LockManifest:
    return LockManifest(revision, tuple(entries))


def lock_entry(target: EditTarget | None = None, *, invalid: bool = False) -> LockEntry:
    return LockEntry(target or edit_target(), "locked-r1", invalid)


class JobEditChecks(unittest.TestCase):
    def assertCode(self, code: str, function: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        with self.assertRaises(ContractError) as context:
            function(*args, **kwargs)
        self.assertEqual(context.exception.code, code)

    def assertEdit(self, state: EditState, code: str | None, *args: Any, **kwargs: Any) -> Any:
        decision = assess_edit(*args, **kwargs)
        self.assertEqual(decision.state, state)
        if code is not None:
            self.assertIn(code, {issue.code for issue in decision.issues})
        return decision

    def test_unlocked_modify_is_acceptable(self) -> None:
        decision = self.assertEdit(EditState.ACCEPTABLE, None, edit_project(), lock_manifest(), edit_command())
        self.assertTrue(decision.acceptable)
        self.assertEqual(decision.issues, ())

    def test_locked_target_blocks_every_actor_and_edit_action(self) -> None:
        locks = lock_manifest(lock_entry())
        for actor in (EditActor.USER, EditActor.ALGORITHM):
            for action in (EditAction.ADD, EditAction.MODIFY, EditAction.REMOVE):
                with self.subTest(actor=actor, action=action):
                    self.assertEdit(EditState.REJECTED_LOCKED, "TARGET_LOCKED", edit_project(), locks,
                                    edit_command(actor=actor, action=action))

    def test_invalid_retained_lock_still_blocks_writes(self) -> None:
        locks = lock_manifest(lock_entry(invalid=True))
        self.assertEdit(EditState.REJECTED_LOCKED, "TARGET_LOCKED", edit_project(), locks, edit_command())

    def test_lock_is_per_object_and_kind(self) -> None:
        locks = lock_manifest(lock_entry(edit_target(kind=ObjectKind.CONFIGURATION)))
        self.assertEdit(EditState.ACCEPTABLE, None, edit_project(), locks, edit_command())
        other = lock_manifest(lock_entry(edit_target("road-2")))
        self.assertEdit(EditState.ACCEPTABLE, None, edit_project(), other, edit_command())

    def test_one_locked_target_rejects_the_whole_write_set(self) -> None:
        locks = lock_manifest(lock_entry(edit_target("road-2")))
        command = edit_command(targets=(edit_target(), edit_target("road-2")))
        decision = self.assertEdit(EditState.REJECTED_LOCKED, "TARGET_LOCKED", edit_project(), locks, command)
        self.assertEqual([issue.field for issue in decision.issues], ["targets[1]"])

    def test_algorithm_cannot_lock_or_unlock(self) -> None:
        for action in (EditAction.LOCK, EditAction.UNLOCK):
            with self.subTest(action=action):
                self.assertCode("ALGORITHM_CANNOT_CHANGE_LOCKS", edit_command,
                                actor=EditActor.ALGORITHM, action=action)

    def test_user_must_unlock_before_editing(self) -> None:
        locks = lock_manifest(lock_entry())
        self.assertEdit(EditState.REJECTED_LOCKED, "TARGET_LOCKED", edit_project(), locks, edit_command())
        self.assertEdit(EditState.ACCEPTABLE, None, edit_project(), locks,
                        edit_command(action=EditAction.UNLOCK))
        self.assertEdit(EditState.ACCEPTABLE, None, edit_project(), lock_manifest(revision="locks-r1"),
                        edit_command())

    def test_lock_and_unlock_preconditions(self) -> None:
        self.assertEdit(EditState.ACCEPTABLE, None, edit_project(), lock_manifest(),
                        edit_command(action=EditAction.LOCK))
        self.assertEdit(EditState.REJECTED_INVALID, "ALREADY_LOCKED", edit_project(),
                        lock_manifest(lock_entry()), edit_command(action=EditAction.LOCK))
        self.assertEdit(EditState.REJECTED_INVALID, "NOT_LOCKED", edit_project(), lock_manifest(),
                        edit_command(action=EditAction.UNLOCK))

    def test_lock_manifest_rejects_duplicate_entries(self) -> None:
        self.assertCode("DUPLICATE_LOCK", lock_manifest, lock_entry(), lock_entry())

    def test_each_version_drift_makes_command_stale(self) -> None:
        for field in ("input_revision", "parameter_revision", "boundary_revision",
                      "model_version", "catalog_version", "rule_version"):
            with self.subTest(field=field):
                project = replace(edit_project(), versions=replace(versions(), **{field: "changed"}))
                decision = self.assertEdit(EditState.REJECTED_STALE, "VERSION_CHANGED", project,
                                           lock_manifest(), edit_command())
                self.assertIn(f"expected_versions.{field}", {issue.field for issue in decision.issues})

    def test_lock_manifest_revision_drift_makes_command_stale(self) -> None:
        self.assertEdit(EditState.REJECTED_STALE, "LOCK_MANIFEST_CHANGED", edit_project(),
                        lock_manifest(revision="locks-r2"), edit_command())

    def test_project_identity_mismatch_is_invalid(self) -> None:
        for field in ("project_id", "scenario_id"):
            with self.subTest(field=field):
                self.assertEdit(EditState.REJECTED_INVALID, "PROJECT_IDENTITY_MISMATCH", edit_project(),
                                lock_manifest(), edit_command(**{field: "other"}))

    def test_target_outside_declared_zone_is_rejected(self) -> None:
        command = edit_command(targets=(edit_target(zone_id="zone-2"),))
        self.assertEdit(EditState.REJECTED_INVALID, "TARGET_OUT_OF_SCOPE", edit_project(), lock_manifest(), command)

    def test_write_set_must_be_declared_unique_and_bounded(self) -> None:
        self.assertCode("EMPTY_WRITE_SET", edit_command, targets=())
        self.assertCode("DUPLICATE_TARGET", edit_command, targets=(edit_target(), edit_target()))
        many = tuple(edit_target(f"road-{index}") for index in range(1025))
        self.assertCode("INVALID_COLLECTION", edit_command, targets=many)
        self.assertCode("INVALID_COLLECTION", edit_command, targets=[edit_target()])

    def test_lock_and_remove_commands_carry_no_resources(self) -> None:
        extra = (resource("extra.json", fmt=ResourceFormat.JSON),)
        self.assertCode("LOCK_COMMAND_HAS_RESOURCES", edit_command, action=EditAction.LOCK, resources=extra)
        self.assertCode("REMOVE_COMMAND_HAS_RESOURCES", edit_command, action=EditAction.REMOVE, resources=extra)

    def test_raw_enums_and_untyped_context_are_rejected(self) -> None:
        self.assertCode("INVALID_ENUM", edit_command, actor="user")
        self.assertCode("INVALID_ENUM", edit_command, action="modify")
        self.assertCode("INVALID_EDIT_CONTEXT", edit_command, expected_versions={})
        self.assertCode("INVALID_EDIT_CONTEXT", assess_edit, {}, lock_manifest(), edit_command())

    def test_declared_resource_must_match_observed_description(self) -> None:
        asset = resource("assets/new.json", fmt=ResourceFormat.JSON)
        command = edit_command(resources=(asset,))
        self.assertEdit(EditState.ACCEPTABLE, None, edit_project(), lock_manifest(), command, (asset,))
        self.assertEdit(EditState.REJECTED_INVALID, "RESOURCE_MISSING", edit_project(), lock_manifest(), command)
        for name, value in (("sha256", "b" * 64), ("size_bytes", 129), ("relative_path", "assets/moved.json")):
            with self.subTest(name=name):
                changed = replace(asset, **{name: value})
                self.assertEdit(EditState.REJECTED_INVALID, "RESOURCE_CHANGED", edit_project(),
                                lock_manifest(), command, (changed,))

    def test_declared_resource_cannot_replace_registered_one(self) -> None:
        project = edit_project()
        for updates in ({"sha256": "b" * 64}, {"resource_id": "other-id", "sha256": "b" * 64}):
            with self.subTest(updates=updates):
                clash = replace(project.resources[0], **updates)
                self.assertEdit(EditState.REJECTED_INVALID, "RESOURCE_CONFLICT", project, lock_manifest(),
                                edit_command(resources=(clash,)), (clash,))

    def test_path_traversal_resource_cannot_be_constructed(self) -> None:
        self.assertCode("INVALID_RESOURCE_PATH", ResourceRef, "r", "../outside.json",
                        ResourcePurpose.ARTIFACT, ResourceFormat.JSON, "a" * 64, 1)

    def test_issues_are_ordered_and_invalid_outranks_locked_and_stale(self) -> None:
        project = replace(edit_project(), versions=replace(versions(), input_revision="new"))
        command = edit_command(targets=(edit_target(zone_id="zone-2"),))
        decision = assess_edit(project, lock_manifest(lock_entry(edit_target(zone_id="zone-2"))), command)
        self.assertEqual(decision.state, EditState.REJECTED_INVALID)
        self.assertEqual([issue.code for issue in decision.issues],
                         ["VERSION_CHANGED", "TARGET_OUT_OF_SCOPE", "TARGET_LOCKED"])
        with self.assertRaises(ContractError) as context:
            replace(decision, state=EditState.ACCEPTABLE)
        self.assertEqual(context.exception.code, "INVALID_EDIT_STATE")

    def undo_fixture(self, **updates: Any) -> tuple[UndoStack, UndoContract]:
        command = edit_command(**updates)
        entry = UndoContract("undo-1", 0, command, versions(), "locks-r1")
        return UndoStack((entry,)), entry

    def test_latest_matching_entry_can_be_undone(self) -> None:
        stack, entry = self.undo_fixture()
        decision = assess_undo(edit_project(), lock_manifest(), stack, entry)
        self.assertTrue(decision.acceptable)

    def test_only_latest_entry_can_be_undone(self) -> None:
        first = UndoContract("undo-1", 0, edit_command(), versions(), "locks-r1")
        second = UndoContract("undo-2", 1, edit_command(command_id="cmd-2"), versions(), "locks-r1")
        decision = assess_undo(edit_project(), lock_manifest(), UndoStack((first, second)), first)
        self.assertEqual(decision.state, EditState.REJECTED_INVALID)
        self.assertIn("UNDO_NOT_LATEST", {issue.code for issue in decision.issues})
        self.assertTrue(assess_undo(edit_project(), lock_manifest(), UndoStack((first, second)), second).acceptable)

    def test_entry_missing_from_stack_is_rejected(self) -> None:
        _, entry = self.undo_fixture()
        decision = assess_undo(edit_project(), lock_manifest(), UndoStack(), entry)
        self.assertIn("UNDO_NOT_LATEST", {issue.code for issue in decision.issues})

    def test_undo_after_newer_edit_is_stale(self) -> None:
        stack, entry = self.undo_fixture()
        project = replace(edit_project(), versions=replace(versions(), input_revision="newer-edit"))
        self.assertEqual(assess_undo(project, lock_manifest(), stack, entry).state, EditState.REJECTED_STALE)
        self.assertEqual(assess_undo(edit_project(), lock_manifest(revision="locks-r2"), stack, entry).state,
                         EditState.REJECTED_STALE)

    def test_undo_cannot_bypass_a_lock_added_after_the_edit(self) -> None:
        stack, entry = self.undo_fixture()
        decision = assess_undo(edit_project(), lock_manifest(lock_entry()), stack, entry)
        self.assertEqual(decision.state, EditState.REJECTED_LOCKED)

    def test_undoing_a_lock_command_requires_the_lock_to_exist(self) -> None:
        stack, entry = self.undo_fixture(action=EditAction.LOCK)
        self.assertEqual(assess_undo(edit_project(), lock_manifest(), stack, entry).state, EditState.REJECTED_INVALID)
        self.assertTrue(assess_undo(edit_project(), lock_manifest(lock_entry()), stack, entry).acceptable)

    def test_undo_stack_is_validated(self) -> None:
        entry = UndoContract("undo-1", 0, edit_command(), versions(), "locks-r1")
        self.assertCode("INVALID_UNDO_STACK", UndoStack, (replace(entry, ordinal=1),))
        self.assertCode("INVALID_UNDO_STACK", UndoStack, (entry, replace(entry, ordinal=1)))
        other = replace(entry, undo_id="undo-2", ordinal=1, command=edit_command(project_id="other"))
        self.assertCode("INVALID_UNDO_STACK", UndoStack, (entry, other))
        many = tuple(UndoContract(f"undo-{index}", index, edit_command(command_id=f"cmd-{index}"),
                                  versions(), "locks-r1") for index in range(257))
        self.assertCode("INVALID_COLLECTION", UndoStack, many)

    def test_accepted_edit_makes_previous_result_stale(self) -> None:
        project, spec, lifecycle, manifest = JobAcceptanceChecks().fixture()
        self.assertTrue(assess_result_acceptance(project, spec, lifecycle, manifest).acceptable)
        decision = assess_edit(project, lock_manifest(), edit_command())
        self.assertTrue(decision.acceptable)
        edited = replace(project, versions=replace(project.versions, input_revision="after-edit"))
        stale = assess_result_acceptance(edited, spec, lifecycle, manifest)
        self.assertEqual(stale.state, AcceptanceState.REJECTED_STALE)

    def test_assessment_is_pure_and_records_are_immutable(self) -> None:
        project, locks, command = edit_project(), lock_manifest(lock_entry()), edit_command()
        before = (repr(project), repr(locks), repr(command))
        decision = assess_edit(project, locks, command)
        self.assertEqual((repr(project), repr(locks), repr(command)), before)
        self.assertEqual(decision, assess_edit(project, locks, command))
        for target, name in ((decision, "state"), (command, "actor"), (locks, "revision"), (lock_entry(), "invalid_retained")):
            with self.subTest(name=name), self.assertRaises(FrozenInstanceError):
                setattr(target, name, None)


CONTRACT_SCHEMA = "p0-04-c0-0.1.0"
DATACLASS_FIELDS: dict[str, tuple[str, ...]] = {
    "Diagnostic": ("code", "message", "severity", "object_id"),
    "EnvelopeViewData": ("envelope_id", "alignment_id", "versions", "space", "resource", "data_state",
                         "validation_state", "diagnostics", "source_layer"),
    "JobEvent": ("identity", "versions", "sequence", "state", "stage_id", "completed", "total",
                 "progress_unit", "error_code"),
    "JobIdentity": ("project_id", "scenario_id", "job_id"),
    "JobSpec": ("identity", "module_id", "versions", "space", "input_resources", "cancel_token",
                "uses_boundary_data"),
    "MapLayer": ("layer_id", "versions", "space", "resource", "geometry_type", "data_state",
                 "diagnostics", "source_layer"),
    "ProfileSample": ("station_m", "elevation_m"),
    "ProfileViewData": ("alignment_id", "versions", "space", "samples", "data_state", "diagnostics",
                        "station_unit", "elevation_unit"),
    "ProjectSummary": ("project_id", "scenario_id", "versions", "space", "module_states", "resources",
                       "map_layers", "profiles", "envelopes"),
    "ResourceRef": ("resource_id", "relative_path", "purpose", "format", "sha256", "size_bytes"),
    "ResultManifest": ("identity", "module_id", "versions", "space", "job_state", "artifacts",
                       "validation_state", "diagnostics"),
    "SpaceRef": ("space_id", "crs", "vertical_reference", "horizontal_unit", "elevation_unit"),
    "VersionKey": ("input_revision", "parameter_revision", "model_version", "catalog_version",
                   "rule_version", "boundary_revision", "schema_version"),
    "FakeJobRun": ("events", "manifest"),
    "FakeStage": ("stage_id", "steps", "known_total"),
    "AcceptanceIssue": ("state", "code", "field"),
    "ResultAcceptance": ("state", "current_project", "registered_job", "lifecycle_spec", "terminal_event",
                         "cancellation_requested", "result", "issues"),
    "EditAssessment": ("state", "project", "locks", "command", "issues"),
    "EditCommand": ("command_id", "project_id", "scenario_id", "actor", "action", "scope_zone_id",
                    "targets", "expected_versions", "expected_lock_revision", "resources"),
    "EditIssue": ("state", "code", "field"),
    "EditTarget": ("object_id", "kind", "zone_id"),
    "LockEntry": ("target", "locked_revision", "invalid_retained"),
    "LockManifest": ("revision", "entries"),
    "UndoContract": ("undo_id", "ordinal", "command", "applied_versions", "applied_lock_revision"),
    "UndoStack": ("entries",),
}
ENUM_VALUES: dict[str, tuple[str, ...]] = {
    "CheckState": ("not_checked", "passed", "failed", "data_insufficient"),
    "DataState": ("ready", "degraded", "data_insufficient"),
    "GeometryType": ("raster", "point", "line", "polygon"),
    "JobState": ("queued", "running", "succeeded", "failed", "cancelled"),
    "ModuleId": ("M1", "M2", "M3", "M4", "M5"),
    "ProgressUnit": ("items", "steps"),
    "ResourceFormat": ("geotiff", "geopackage", "geojson", "json"),
    "ResourcePurpose": ("input", "artifact"),
    "Severity": ("info", "warning", "error"),
    "CancelStatus": ("requested", "already_requested", "job_finished"),
    "AcceptanceState": ("acceptable", "rejected_stale", "rejected_invalid"),
    "EditAction": ("add", "modify", "remove", "lock", "unlock"),
    "EditActor": ("user", "algorithm"),
    "EditState": ("acceptable", "rejected_stale", "rejected_locked", "rejected_invalid"),
    "ObjectKind": ("geometry", "configuration"),
}
CONTRACT_MODULES = (job_contracts, job_lifecycle, job_acceptance, job_edits)


class JobHandoffChecks(unittest.TestCase):
    def public_types(self) -> dict[str, type]:
        found: dict[str, type] = {}
        for module in CONTRACT_MODULES:
            for name, obj in vars(module).items():
                if inspect.isclass(obj) and obj.__module__ == module.__name__:
                    found[name] = obj
        return found

    def test_schema_version_is_pinned(self) -> None:
        self.assertEqual(job_contracts.JOB_SCHEMA_VERSION, CONTRACT_SCHEMA)
        self.assertEqual(versions().schema_version, CONTRACT_SCHEMA)

    def test_dataclass_field_names_and_order_are_pinned(self) -> None:
        actual = {name: tuple(field.name for field in dataclasses.fields(obj))
                  for name, obj in self.public_types().items()
                  if dataclasses.is_dataclass(obj) and not issubclass(obj, enum.Enum)}
        self.assertEqual(actual, DATACLASS_FIELDS)

    def test_enum_values_and_order_are_pinned(self) -> None:
        actual = {name: tuple(item.value for item in obj)
                  for name, obj in self.public_types().items() if issubclass(obj, enum.Enum)}
        self.assertEqual(actual, ENUM_VALUES)

    def test_every_contract_dataclass_is_frozen_with_slots(self) -> None:
        for name in DATACLASS_FIELDS:
            with self.subTest(name=name):
                obj = self.public_types()[name]
                self.assertTrue(obj.__dataclass_params__.frozen)
                self.assertTrue(hasattr(obj, "__slots__"))

    def test_fixture_origin_is_synthetic_and_names_are_marked(self) -> None:
        self.assertEqual(job_fixtures.FIXTURE_ORIGIN, "synthetic")
        project, spec, lifecycle, manifest = job_fixtures.build_succeeded_run()
        for text in (project.project_id, project.scenario_id, spec.identity.job_id, spec.cancel_token,
                     project.versions.model_version, project.space.space_id):
            self.assertIn("synthetic", text)
        for item in project.resources + manifest.artifacts:
            self.assertTrue(item.resource_id.startswith("synthetic-"))
            self.assertEqual(item.sha256, job_fixtures.FIXTURE_HASH)

    def test_fixture_package_builds_a_consistent_accepted_run(self) -> None:
        project, spec, lifecycle, manifest = job_fixtures.build_succeeded_run()
        self.assertEqual(lifecycle.state, JobState.SUCCEEDED)
        self.assertEqual(manifest.versions, project.versions)
        decision = assess_result_acceptance(project, spec, lifecycle, manifest)
        self.assertTrue(decision.acceptable)

    def test_fixture_builders_are_deterministic_and_independent(self) -> None:
        self.assertEqual(job_fixtures.build_job(), job_fixtures.build_job())
        first, second = job_fixtures.build_succeeded_run(), job_fixtures.build_succeeded_run()
        self.assertIsNot(first[2], second[2])
        self.assertEqual(first[2].events, second[2].events)

    def test_fixture_boundary_variant_is_available(self) -> None:
        self.assertTrue(job_fixtures.build_job(uses_boundary=True).uses_boundary_data)
        self.assertIsNone(job_fixtures.build_versions(boundary=None).boundary_revision)

    def test_fixture_module_reads_no_files(self) -> None:
        source = Path(job_fixtures.__file__).read_text(encoding="utf-8")
        for token in ("open(", "Path(", "read_text", "write_text", "os.", "shutil", "subprocess"):
            self.assertNotIn(token, source)

    def test_fixture_module_states_unverified_scope(self) -> None:
        doc = job_fixtures.__doc__ or ""
        for phrase in ("NOT verified", "Qt rendering", "Q consumer feedback", "transactional project"):
            self.assertIn(phrase, doc)

    def test_acceptable_result_carries_no_policy_conclusion(self) -> None:
        project, spec, lifecycle, manifest = job_fixtures.build_succeeded_run()
        decision = assess_result_acceptance(project, spec, lifecycle, manifest)
        names = {field.name for field in dataclasses.fields(type(decision))}
        self.assertFalse(any("polic" in name or "clear" in name or "d7" in name.casefold() for name in names))
        self.assertEqual(manifest.job_state, JobState.SUCCEEDED)
        self.assertEqual(manifest.validation_state, CheckState.PASSED)

    def test_successful_job_with_failed_checks_is_never_acceptable(self) -> None:
        project, spec, lifecycle, manifest = job_fixtures.build_succeeded_run()
        failed = replace(manifest, validation_state=CheckState.FAILED, diagnostics=(diagnostic("CONSTRAINT_FAILED"),))
        self.assertEqual(failed.job_state, JobState.SUCCEEDED)
        decision = assess_result_acceptance(project, spec, lifecycle, failed)
        self.assertFalse(decision.acceptable)
        self.assertIn("RESULT_CHECKS_NOT_PASSED", {issue.code for issue in decision.issues})

    def test_passed_checks_require_a_successful_job(self) -> None:
        _, _, _, manifest = job_fixtures.build_succeeded_run()
        with self.assertRaises(ContractError) as context:
            replace(manifest, job_state=JobState.FAILED, diagnostics=(diagnostic("X", Severity.ERROR),))
        self.assertEqual(context.exception.code, "INVALID_RESULT_VALIDATION")


class SummaryResult(unittest.TestResult):
    def addFailure(self, test: unittest.TestCase, err: tuple) -> None:
        super().addFailure(test, err)
        self._report(test, err)

    def addError(self, test: unittest.TestCase, err: tuple) -> None:
        super().addError(test, err)
        self._report(test, err)

    def addSubTest(self, test: unittest.TestCase, subtest: unittest.TestCase, err: tuple | None) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._report(subtest, err)

    def _report(self, test: unittest.TestCase, err: tuple) -> None:
        if len(self.failures) + len(self.errors) <= 20:
            detail = " ".join(str(err[1]).split())[:180]
            print(f"[FAIL] {test.id()}: {err[0].__name__}: {detail}")


def main() -> int:
    suites = tuple(unittest.defaultTestLoader.loadTestsFromTestCase(checks)
                   for checks in (JobContractChecks, JobLifecycleChecks, JobAcceptanceChecks,
                                  JobEditChecks, JobHandoffChecks))
    counts = tuple(suite.countTestCases() for suite in suites)
    outcome = SummaryResult()
    unittest.TestSuite(suites).run(outcome)
    if outcome.wasSuccessful() and not outcome.skipped and outcome.testsRun == sum(counts):
        print(f"[SUCCESS] P0-04-A/B/C/D/E checks passed ({outcome.testsRun} cases: "
              f"A={counts[0]}, B={counts[1]}, C={counts[2]}, D={counts[3]}, E={counts[4]}); synthetic only.")
        return 0
    print(f"[FAILED] P0-04-A/B/C/D/E: {outcome.testsRun} cases; {len(outcome.failures)} failures, "
          f"{len(outcome.errors)} errors, {len(outcome.skipped)} skipped.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
