# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contracts import identifier, require
from .job_contracts import (
    CheckState, JobEvent, JobSpec, JobState, ProjectSummary, ResultManifest,
    Severity, SpaceRef, VersionKey,
)
from .job_lifecycle import JobLifecycle


class AcceptanceState(str, Enum):
    ACCEPTABLE = "acceptable"
    REJECTED_STALE = "rejected_stale"
    REJECTED_INVALID = "rejected_invalid"


@dataclass(frozen=True, slots=True)
class AcceptanceIssue:
    state: AcceptanceState
    code: str
    field: str

    def __post_init__(self) -> None:
        require(isinstance(self.state, AcceptanceState) and self.state != AcceptanceState.ACCEPTABLE,
                "INVALID_ACCEPTANCE_ISSUE", "issue must classify a rejection")
        for name in ("code", "field"):
            value = getattr(self, name)
            identifier(value, name)
            require(len(value) <= 128, "INVALID_ACCEPTANCE_ISSUE", "bounded code and field required")


@dataclass(frozen=True, slots=True)
class ResultAcceptance:
    state: AcceptanceState
    current_project: ProjectSummary
    registered_job: JobSpec
    lifecycle_spec: JobSpec
    terminal_event: JobEvent | None
    cancellation_requested: bool
    result: ResultManifest
    issues: tuple[AcceptanceIssue, ...]

    def __post_init__(self) -> None:
        require(isinstance(self.current_project, ProjectSummary) and isinstance(self.registered_job, JobSpec) and
                isinstance(self.lifecycle_spec, JobSpec) and isinstance(self.result, ResultManifest),
                "INVALID_ACCEPTANCE_CONTEXT", "typed immutable context required")
        require(self.terminal_event is None or isinstance(self.terminal_event, JobEvent),
                "INVALID_ACCEPTANCE_CONTEXT", "typed terminal event required")
        require(type(self.cancellation_requested) is bool and isinstance(self.issues, tuple) and
                all(isinstance(issue, AcceptanceIssue) for issue in self.issues),
                "INVALID_ACCEPTANCE_CONTEXT", "immutable issues and explicit cancellation required")
        expected = AcceptanceState.ACCEPTABLE
        if any(issue.state == AcceptanceState.REJECTED_INVALID for issue in self.issues):
            expected = AcceptanceState.REJECTED_INVALID
        elif self.issues:
            expected = AcceptanceState.REJECTED_STALE
        require(isinstance(self.state, AcceptanceState) and self.state == expected,
                "INVALID_ACCEPTANCE_STATE", "decision must agree with all rejection reasons")

    @property
    def acceptable(self) -> bool:
        return self.state == AcceptanceState.ACCEPTABLE


VERSION_FIELDS = (
    "input_revision", "parameter_revision", "boundary_revision", "schema_version",
    "model_version", "catalog_version", "rule_version",
)
SPACE_FIELDS = ("space_id", "crs", "vertical_reference", "horizontal_unit", "elevation_unit")
RESOURCE_FIELDS = ("relative_path", "purpose", "format", "sha256", "size_bytes")


def assess_result_acceptance(current_project: ProjectSummary, registered_job: JobSpec,
                             lifecycle: JobLifecycle, result: ResultManifest) -> ResultAcceptance:
    require(isinstance(current_project, ProjectSummary) and isinstance(registered_job, JobSpec) and
            isinstance(lifecycle, JobLifecycle) and isinstance(result, ResultManifest),
            "INVALID_ACCEPTANCE_CONTEXT", "typed project, registered job, lifecycle and result required")
    issues: list[AcceptanceIssue] = []

    def reject(state: AcceptanceState, code: str, field: str) -> None:
        issues.append(AcceptanceIssue(state, code, field))

    stale = AcceptanceState.REJECTED_STALE
    invalid = AcceptanceState.REJECTED_INVALID
    for name, identity in (("registered_job", registered_job.identity),
                           ("lifecycle", lifecycle.spec.identity), ("result", result.identity)):
        for field in ("project_id", "scenario_id"):
            if getattr(identity, field) != getattr(current_project, field):
                reject(invalid, "PROJECT_IDENTITY_MISMATCH", f"{name}.identity.{field}")
    for name, identity in (("lifecycle", lifecycle.spec.identity), ("result", result.identity)):
        if identity.job_id != registered_job.identity.job_id:
            reject(stale, "JOB_REPLACED", f"{name}.identity.job_id")
    for name, module_id in (("lifecycle", lifecycle.spec.module_id), ("result", result.module_id)):
        if module_id != registered_job.module_id:
            reject(invalid, "JOB_MODULE_MISMATCH", f"{name}.module_id")
    if lifecycle.spec != registered_job:
        reject(stale, "LIFECYCLE_SPEC_CHANGED", "lifecycle.spec")

    terminal_event = lifecycle.events[-1] if lifecycle.state in (
        JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED) else None
    if lifecycle.state != JobState.SUCCEEDED:
        reject(invalid, "LIFECYCLE_NOT_SUCCESSFUL", "lifecycle.state")
    if result.job_state != JobState.SUCCEEDED:
        reject(invalid, "RESULT_JOB_NOT_SUCCESSFUL", "result.job_state")
    if terminal_event is not None and terminal_event.state != result.job_state:
        reject(invalid, "TERMINAL_STATE_MISMATCH", "result.job_state")
    if lifecycle.cancellation_requested:
        reject(invalid, "JOB_CANCELLATION_PENDING", "lifecycle.cancellation_requested")

    def compare_context(name: str, versions: VersionKey, space: SpaceRef,
                        expected_versions: VersionKey, expected_space: SpaceRef) -> None:
        for field in VERSION_FIELDS:
            if getattr(versions, field) != getattr(expected_versions, field):
                reject(stale, "VERSION_CHANGED", f"{name}.versions.{field}")
        for field in SPACE_FIELDS:
            if getattr(space, field) != getattr(expected_space, field):
                reject(stale, "SPACE_CHANGED", f"{name}.space.{field}")

    compare_context("registered_job", registered_job.versions, registered_job.space,
                    current_project.versions, current_project.space)
    compare_context("result", result.versions, result.space, current_project.versions, current_project.space)
    compare_context("lifecycle", lifecycle.spec.versions, lifecycle.spec.space,
                    registered_job.versions, registered_job.space)
    compare_context("result_binding", result.versions, result.space, registered_job.versions, registered_job.space)

    resources = {resource.resource_id: resource for resource in current_project.resources}
    paths = {resource.relative_path.casefold(): resource for resource in current_project.resources}
    for index, resource in enumerate(registered_job.input_resources):
        field_prefix = f"registered_job.input_resources[{index}]"
        current = resources.get(resource.resource_id)
        if current is None:
            reject(stale, "INPUT_RESOURCE_MISSING", field_prefix)
        else:
            for field in RESOURCE_FIELDS:
                if getattr(resource, field) != getattr(current, field):
                    reject(stale, "INPUT_RESOURCE_CHANGED", f"{field_prefix}.{field}")

    if result.validation_state != CheckState.PASSED:
        reject(invalid, "RESULT_CHECKS_NOT_PASSED", "result.validation_state")
    for index, diagnostic in enumerate(result.diagnostics):
        if diagnostic.severity == Severity.ERROR:
            reject(invalid, "RESULT_ERROR_DIAGNOSTIC", f"result.diagnostics[{index}]")
    for index, artifact in enumerate(result.artifacts):
        by_id = resources.get(artifact.resource_id)
        by_path = paths.get(artifact.relative_path.casefold())
        if by_id is not None and by_id != artifact:
            reject(invalid, "ARTIFACT_ID_CONFLICT", f"result.artifacts[{index}].resource_id")
        if by_path is not None and by_path != artifact:
            reject(invalid, "ARTIFACT_PATH_CONFLICT", f"result.artifacts[{index}].relative_path")

    state = AcceptanceState.ACCEPTABLE
    if any(issue.state == invalid for issue in issues):
        state = invalid
    elif issues:
        state = stale
    return ResultAcceptance(state, current_project, registered_job, lifecycle.spec, terminal_event,
                            lifecycle.cancellation_requested, result, tuple(issues))
