# SPDX-License-Identifier: GPL-3.0-only
"""C0 synthetic fixtures for consumers (Q shell, views, project, packaging).

Handoff status. Verified here, synthetically only: DTO validation, the
synchronous fake-worker lifecycle, result acceptance, edit/lock/undo
assessment and the frozen field/enum snapshot. NOT verified: real background
process communication, on-disk file existence or hashes, transactional project
writes, local dependency impact, Qt rendering, and any Q consumer feedback.
Nothing here touches files or real P0-02 data.
"""
from __future__ import annotations

from .contracts import require
from .job_contracts import (
    CheckState, JobSpec, JobState, ModuleId, ProjectSummary, ResourceFormat,
    ResourcePurpose, ResourceRef, ResultManifest, SpaceRef, VersionKey, JobIdentity,
)
from .job_lifecycle import FakeWorker, JobLifecycle, run_fake_job

FIXTURE_ORIGIN = "synthetic"
FIXTURE_HASH = "a" * 64


def build_versions(*, boundary: str | None = "synthetic-boundary-r1") -> VersionKey:
    return VersionKey("synthetic-input-r1", "synthetic-parameters-r1", "synthetic-model-v0",
                      "synthetic-catalog-r1", "synthetic-rules-r1", boundary)


def build_space(*, vertical: str | None = "synthetic-height-v1") -> SpaceRef:
    return SpaceRef("synthetic-metric-local", "EPSG:32631", vertical)


def build_resource(name: str = "synthetic/dem.tif", *, purpose: ResourcePurpose = ResourcePurpose.INPUT,
                   fmt: ResourceFormat = ResourceFormat.GEOTIFF) -> ResourceRef:
    return ResourceRef(f"synthetic-{name.replace('/', '-').replace('.', '-')}", name, purpose, fmt,
                       FIXTURE_HASH, 128)


def build_identity() -> JobIdentity:
    return JobIdentity("synthetic-project-1", "synthetic-scenario-1", "synthetic-job-1")


def build_job(*, uses_boundary: bool = False) -> JobSpec:
    return JobSpec(build_identity(), ModuleId.M1, build_versions(), build_space(),
                   (build_resource(),), "synthetic-cancel-1", uses_boundary)


def build_project(spec: JobSpec | None = None) -> ProjectSummary:
    spec = spec or build_job()
    return ProjectSummary(spec.identity.project_id, spec.identity.scenario_id, spec.versions, spec.space,
                          tuple((module, None) for module in ModuleId), spec.input_resources)


def build_succeeded_run() -> tuple[ProjectSummary, JobSpec, JobLifecycle, ResultManifest]:
    spec = build_job()
    lifecycle = JobLifecycle(spec)
    run = run_fake_job(lifecycle, FakeWorker())
    require(run.manifest.job_state == JobState.SUCCEEDED, "FIXTURE_RUN_FAILED", "synthetic run must succeed")
    artifact = build_resource("synthetic/result.json", purpose=ResourcePurpose.ARTIFACT,
                              fmt=ResourceFormat.JSON)
    manifest = ResultManifest(spec.identity, spec.module_id, spec.versions, spec.space, JobState.SUCCEEDED,
                              (artifact,), CheckState.PASSED)
    return build_project(spec), spec, lifecycle, manifest
