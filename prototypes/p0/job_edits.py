# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contracts import require
from .job_acceptance import RESOURCE_FIELDS, VERSION_FIELDS
from .job_contracts import (
    ProjectSummary, ResourceRef, VersionKey, _enum, _id, _items, _resources,
)

MAX_EDIT_TARGETS = 1024
MAX_LOCK_ENTRIES = 4096
MAX_UNDO_ENTRIES = 256


class EditActor(str, Enum):
    USER = "user"
    ALGORITHM = "algorithm"


class EditAction(str, Enum):
    ADD = "add"
    MODIFY = "modify"
    REMOVE = "remove"
    LOCK = "lock"
    UNLOCK = "unlock"


class ObjectKind(str, Enum):
    GEOMETRY = "geometry"
    CONFIGURATION = "configuration"


class EditState(str, Enum):
    ACCEPTABLE = "acceptable"
    REJECTED_STALE = "rejected_stale"
    REJECTED_LOCKED = "rejected_locked"
    REJECTED_INVALID = "rejected_invalid"


LOCK_ACTIONS = frozenset({EditAction.LOCK, EditAction.UNLOCK})


@dataclass(frozen=True, slots=True)
class EditTarget:
    object_id: str
    kind: ObjectKind
    zone_id: str

    def __post_init__(self) -> None:
        _id(self.object_id, "object_id")
        _enum(self.kind, ObjectKind, "kind")
        _id(self.zone_id, "zone_id")

    @property
    def key(self) -> tuple[str, ObjectKind]:
        return (self.object_id, self.kind)


@dataclass(frozen=True, slots=True)
class LockEntry:
    target: EditTarget
    locked_revision: str
    invalid_retained: bool = False

    def __post_init__(self) -> None:
        require(isinstance(self.target, EditTarget), "INVALID_LOCK_ENTRY", "typed target required")
        _id(self.locked_revision, "locked_revision")
        require(type(self.invalid_retained) is bool, "INVALID_LOCK_ENTRY", "explicit boolean required")


@dataclass(frozen=True, slots=True)
class LockManifest:
    revision: str
    entries: tuple[LockEntry, ...] = ()

    def __post_init__(self) -> None:
        _id(self.revision, "lock revision")
        _items(self.entries, LockEntry, "entries", limit=MAX_LOCK_ENTRIES)
        require(len({entry.target.key for entry in self.entries}) == len(self.entries),
                "DUPLICATE_LOCK", "one lock per object and kind")


@dataclass(frozen=True, slots=True)
class EditCommand:
    command_id: str
    project_id: str
    scenario_id: str
    actor: EditActor
    action: EditAction
    scope_zone_id: str
    targets: tuple[EditTarget, ...]
    expected_versions: VersionKey
    expected_lock_revision: str
    resources: tuple[ResourceRef, ...] = ()

    def __post_init__(self) -> None:
        for name in ("command_id", "project_id", "scenario_id", "scope_zone_id", "expected_lock_revision"):
            _id(getattr(self, name), name)
        _enum(self.actor, EditActor, "actor")
        _enum(self.action, EditAction, "action")
        _items(self.targets, EditTarget, "targets", limit=MAX_EDIT_TARGETS)
        require(bool(self.targets), "EMPTY_WRITE_SET", "an edit must declare its write set")
        require(len({target.key for target in self.targets}) == len(self.targets),
                "DUPLICATE_TARGET", "write set targets must be unique")
        require(isinstance(self.expected_versions, VersionKey),
                "INVALID_EDIT_CONTEXT", "typed expected versions required")
        _resources(self.resources)
        if self.action in LOCK_ACTIONS:
            require(self.actor == EditActor.USER, "ALGORITHM_CANNOT_CHANGE_LOCKS",
                    "only the user may lock or unlock")
            require(not self.resources, "LOCK_COMMAND_HAS_RESOURCES", "lock commands carry no resources")
        if self.action == EditAction.REMOVE:
            require(not self.resources, "REMOVE_COMMAND_HAS_RESOURCES", "removal carries no resources")


@dataclass(frozen=True, slots=True)
class UndoContract:
    undo_id: str
    ordinal: int
    command: EditCommand
    applied_versions: VersionKey
    applied_lock_revision: str

    def __post_init__(self) -> None:
        _id(self.undo_id, "undo_id")
        require(type(self.ordinal) is int and self.ordinal >= 0, "INVALID_UNDO_ENTRY", "ordinal required")
        require(isinstance(self.command, EditCommand) and isinstance(self.applied_versions, VersionKey),
                "INVALID_UNDO_ENTRY", "typed command and versions required")
        _id(self.applied_lock_revision, "applied_lock_revision")


@dataclass(frozen=True, slots=True)
class UndoStack:
    entries: tuple[UndoContract, ...] = ()

    def __post_init__(self) -> None:
        _items(self.entries, UndoContract, "entries", limit=MAX_UNDO_ENTRIES)
        require([entry.ordinal for entry in self.entries] == list(range(len(self.entries))),
                "INVALID_UNDO_STACK", "ordinals must be contiguous from zero")
        require(len({entry.undo_id for entry in self.entries}) == len(self.entries),
                "INVALID_UNDO_STACK", "undo IDs must be unique")
        require(len({(entry.command.project_id, entry.command.scenario_id) for entry in self.entries}) <= 1,
                "INVALID_UNDO_STACK", "one project scenario per stack")


@dataclass(frozen=True, slots=True)
class EditIssue:
    state: EditState
    code: str
    field: str

    def __post_init__(self) -> None:
        require(isinstance(self.state, EditState) and self.state != EditState.ACCEPTABLE,
                "INVALID_EDIT_ISSUE", "issue must classify a rejection")
        _id(self.code, "code")
        require(isinstance(self.field, str) and 0 < len(self.field) <= 256,
                "INVALID_EDIT_ISSUE", "bounded field required")


@dataclass(frozen=True, slots=True)
class EditAssessment:
    state: EditState
    project: ProjectSummary
    locks: LockManifest
    command: EditCommand
    issues: tuple[EditIssue, ...]

    def __post_init__(self) -> None:
        require(isinstance(self.project, ProjectSummary) and isinstance(self.locks, LockManifest) and
                isinstance(self.command, EditCommand), "INVALID_EDIT_CONTEXT", "typed immutable context required")
        require(isinstance(self.issues, tuple) and all(isinstance(issue, EditIssue) for issue in self.issues),
                "INVALID_EDIT_CONTEXT", "immutable issues required")
        require(isinstance(self.state, EditState) and self.state == _decide(self.issues),
                "INVALID_EDIT_STATE", "decision must agree with all rejection reasons")

    @property
    def acceptable(self) -> bool:
        return self.state == EditState.ACCEPTABLE


def _decide(issues: tuple[EditIssue, ...]) -> EditState:
    states = {issue.state for issue in issues}
    for state in (EditState.REJECTED_INVALID, EditState.REJECTED_LOCKED, EditState.REJECTED_STALE):
        if state in states:
            return state
    return EditState.ACCEPTABLE


def _compare_versions(issues: list[EditIssue], name: str, actual: VersionKey, expected: VersionKey) -> None:
    for field in VERSION_FIELDS:
        if getattr(actual, field) != getattr(expected, field):
            issues.append(EditIssue(EditState.REJECTED_STALE, "VERSION_CHANGED", f"{name}.{field}"))


def _check_targets(issues: list[EditIssue], command: EditCommand, action: EditAction,
                   locks: LockManifest) -> None:
    locked = {entry.target.key for entry in locks.entries}
    for index, target in enumerate(command.targets):
        field = f"targets[{index}]"
        if target.zone_id != command.scope_zone_id:
            issues.append(EditIssue(EditState.REJECTED_INVALID, "TARGET_OUT_OF_SCOPE", f"{field}.zone_id"))
        if action == EditAction.LOCK:
            if target.key in locked:
                issues.append(EditIssue(EditState.REJECTED_INVALID, "ALREADY_LOCKED", field))
        elif action == EditAction.UNLOCK:
            if target.key not in locked:
                issues.append(EditIssue(EditState.REJECTED_INVALID, "NOT_LOCKED", field))
        elif target.key in locked:
            issues.append(EditIssue(EditState.REJECTED_LOCKED, "TARGET_LOCKED", field))


def _check_context(issues: list[EditIssue], project: ProjectSummary, command: EditCommand) -> None:
    for field in ("project_id", "scenario_id"):
        if getattr(command, field) != getattr(project, field):
            issues.append(EditIssue(EditState.REJECTED_INVALID, "PROJECT_IDENTITY_MISMATCH", field))


def _check_resources(issues: list[EditIssue], project: ProjectSummary, command: EditCommand,
                     observed: tuple[ResourceRef, ...]) -> None:
    registered = {item.resource_id: item for item in project.resources}
    paths = {item.relative_path.casefold(): item for item in project.resources}
    seen = {item.resource_id: item for item in observed}
    for index, resource in enumerate(command.resources):
        field = f"resources[{index}]"
        for current in (registered.get(resource.resource_id), paths.get(resource.relative_path.casefold())):
            if current is not None and current != resource:
                issues.append(EditIssue(EditState.REJECTED_INVALID, "RESOURCE_CONFLICT", field))
                break
        actual = seen.get(resource.resource_id)
        if actual is None:
            issues.append(EditIssue(EditState.REJECTED_INVALID, "RESOURCE_MISSING", field))
            continue
        for name in RESOURCE_FIELDS:
            if getattr(actual, name) != getattr(resource, name):
                issues.append(EditIssue(EditState.REJECTED_INVALID, "RESOURCE_CHANGED", f"{field}.{name}"))


def assess_edit(project: ProjectSummary, locks: LockManifest, command: EditCommand,
                observed_resources: tuple[ResourceRef, ...] = ()) -> EditAssessment:
    require(isinstance(project, ProjectSummary) and isinstance(locks, LockManifest) and
            isinstance(command, EditCommand), "INVALID_EDIT_CONTEXT",
            "typed project, lock manifest and command required")
    _resources(observed_resources)
    issues: list[EditIssue] = []
    _check_context(issues, project, command)
    _compare_versions(issues, "expected_versions", command.expected_versions, project.versions)
    if command.expected_lock_revision != locks.revision:
        issues.append(EditIssue(EditState.REJECTED_STALE, "LOCK_MANIFEST_CHANGED", "expected_lock_revision"))
    _check_targets(issues, command, command.action, locks)
    _check_resources(issues, project, command, observed_resources)
    result = tuple(issues)
    return EditAssessment(_decide(result), project, locks, command, result)


INVERSE_ACTIONS = {EditAction.LOCK: EditAction.UNLOCK, EditAction.UNLOCK: EditAction.LOCK}


def assess_undo(project: ProjectSummary, locks: LockManifest, stack: UndoStack,
                entry: UndoContract) -> EditAssessment:
    require(isinstance(project, ProjectSummary) and isinstance(locks, LockManifest) and
            isinstance(stack, UndoStack) and isinstance(entry, UndoContract),
            "INVALID_EDIT_CONTEXT", "typed project, lock manifest, stack and entry required")
    command = entry.command
    issues: list[EditIssue] = []
    _check_context(issues, project, command)
    if not stack.entries or stack.entries[-1] != entry:
        issues.append(EditIssue(EditState.REJECTED_INVALID, "UNDO_NOT_LATEST", "entry"))
    _compare_versions(issues, "applied_versions", entry.applied_versions, project.versions)
    if entry.applied_lock_revision != locks.revision:
        issues.append(EditIssue(EditState.REJECTED_STALE, "LOCK_MANIFEST_CHANGED", "applied_lock_revision"))
    _check_targets(issues, command, INVERSE_ACTIONS.get(command.action, command.action), locks)
    result = tuple(issues)
    return EditAssessment(_decide(result), project, locks, command, result)
