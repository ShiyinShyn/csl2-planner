# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PureWindowsPath
from typing import Any

from pyproj import CRS
from pyproj.exceptions import CRSError

from .contracts import ContractError, finite_number, identifier, require

JOB_SCHEMA_VERSION = "p0-04-c0-0.1.0"
MAX_REFERENCES = 4096
MAX_PROFILE_SAMPLES = 100_000
MAX_TEXT_LENGTH = 2048
MAX_INTEGER = 2**63 - 1


class ModuleId(str, Enum):
    M1 = "M1"
    M2 = "M2"
    M3 = "M3"
    M4 = "M4"
    M5 = "M5"


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CheckState(str, Enum):
    NOT_CHECKED = "not_checked"
    PASSED = "passed"
    FAILED = "failed"
    DATA_INSUFFICIENT = "data_insufficient"


class DataState(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    DATA_INSUFFICIENT = "data_insufficient"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ResourcePurpose(str, Enum):
    INPUT = "input"
    ARTIFACT = "artifact"


class ResourceFormat(str, Enum):
    GEOTIFF = "geotiff"
    GEOPACKAGE = "geopackage"
    GEOJSON = "geojson"
    JSON = "json"


class GeometryType(str, Enum):
    RASTER = "raster"
    POINT = "point"
    LINE = "line"
    POLYGON = "polygon"


class ProgressUnit(str, Enum):
    ITEMS = "items"
    STEPS = "steps"


TERMINAL_STATES = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED})
VECTOR_FORMATS = frozenset({ResourceFormat.GEOPACKAGE, ResourceFormat.GEOJSON})
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    | {f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³"}
)


def _id(value: Any, name: str) -> None:
    identifier(value, name)
    require(len(value) <= 128 and value == value.strip() and
            not any(ord(character) < 32 for character in value), "INVALID_ID", name)


def _enum(value: Any, enum_type: type[Enum], name: str) -> None:
    require(isinstance(value, enum_type), "INVALID_ENUM", name)


def _integer(value: Any, name: str) -> None:
    require(type(value) is int and 0 <= value <= MAX_INTEGER, "INVALID_INTEGER", name)


def _number(value: Any, name: str, *, minimum: float | None = None) -> None:
    try:
        finite_number(value, name, minimum=minimum)
    except OverflowError as exc:
        raise ContractError("INVALID_NUMBER", f"{name} exceeds finite numeric range") from exc


def _items(values: Any, item_type: type, name: str, *, limit: int = MAX_REFERENCES) -> None:
    require(isinstance(values, tuple) and len(values) <= limit and
            all(isinstance(value, item_type) for value in values), "INVALID_COLLECTION", name)


def _relative_path(value: Any) -> None:
    require(isinstance(value, str) and 0 < len(value) <= 1024,
            "INVALID_RESOURCE_PATH", "bounded project-relative path required")
    parts = value.split("/")
    require(not PureWindowsPath(value).drive and not value.startswith("/") and
            not any(character in '<>:"\\|?*' or ord(character) < 32 for character in value),
            "INVALID_RESOURCE_PATH", "absolute, drive, stream and Windows-special paths are forbidden")
    require(all(part not in ("", ".", "..") and not part.endswith((".", " ")) and
                part.split(".")[0].rstrip(" ").upper() not in WINDOWS_RESERVED_NAMES for part in parts),
            "INVALID_RESOURCE_PATH", "canonical forward-slash relative path required")


def _resources(values: Any, *, purpose: ResourcePurpose | None = None) -> None:
    _items(values, ResourceRef, "resources")
    require(len({value.resource_id for value in values}) == len(values),
            "DUPLICATE_RESOURCE", "resource IDs must be unique")
    require(len({value.relative_path.casefold() for value in values}) == len(values),
            "DUPLICATE_RESOURCE_PATH", "resource paths must be unique on Windows")
    if purpose is not None:
        require(all(value.purpose == purpose for value in values),
                "INVALID_RESOURCE_PURPOSE", "resource purpose does not match its port")


def _diagnostics(values: Any) -> None:
    _items(values, Diagnostic, "diagnostics")


def _view_header(versions: Any, space: Any, state: Any, diagnostics: Any) -> None:
    require(isinstance(versions, VersionKey) and isinstance(space, SpaceRef),
            "INVALID_VIEW_CONTEXT", "typed version and space references required")
    _enum(state, DataState, "data_state")
    _diagnostics(diagnostics)
    if state != DataState.READY:
        require(bool(diagnostics), "MISSING_DIAGNOSTIC", "degraded or insufficient data needs a reason")


def _vector_resource(resource: Any, source_layer: Any) -> None:
    require(isinstance(resource, ResourceRef) and resource.format in VECTOR_FORMATS,
            "INVALID_VIEW_RESOURCE", "vector resource required")
    if resource.format == ResourceFormat.GEOPACKAGE:
        _id(source_layer, "source_layer")
    else:
        require(source_layer is None, "INVALID_SOURCE_LAYER", "GeoJSON has no named sublayer")


@dataclass(frozen=True, slots=True)
class VersionKey:
    input_revision: str
    parameter_revision: str
    model_version: str
    catalog_version: str
    rule_version: str
    boundary_revision: str | None = None
    schema_version: str = JOB_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("input_revision", "parameter_revision", "model_version", "catalog_version", "rule_version"):
            _id(getattr(self, name), name)
        require(self.schema_version == JOB_SCHEMA_VERSION,
                "UNSUPPORTED_JOB_SCHEMA", "explicit C0 schema version required")
        if self.boundary_revision is not None:
            _id(self.boundary_revision, "boundary_revision")


@dataclass(frozen=True, slots=True)
class SpaceRef:
    space_id: str
    crs: str
    vertical_reference: str | None
    horizontal_unit: str = "m"
    elevation_unit: str = "m"

    def __post_init__(self) -> None:
        _id(self.space_id, "space_id")
        require(isinstance(self.crs, str) and 0 < len(self.crs) <= 16_384,
                "INVALID_CRS", "explicit bounded CRS text required")
        require(self.horizontal_unit == "m" and self.elevation_unit == "m",
                "INVALID_SPACE_UNIT", "normalize units to metres before constructing a C0 space")
        try:
            crs = CRS.from_user_input(self.crs)
        except (CRSError, ValueError, TypeError) as exc:
            raise ContractError("INVALID_CRS", "unreadable CRS") from exc
        require(crs.is_projected and len(crs.axis_info) >= 2 and
                all(abs(axis.unit_conversion_factor - 1.0) <= 1e-12 for axis in crs.axis_info[:2]),
                "NON_METRIC_CRS", "projected metre CRS required")
        if self.vertical_reference is not None:
            _id(self.vertical_reference, "vertical_reference")


@dataclass(frozen=True, slots=True)
class ResourceRef:
    resource_id: str
    relative_path: str
    purpose: ResourcePurpose
    format: ResourceFormat
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        _id(self.resource_id, "resource_id")
        _relative_path(self.relative_path)
        _enum(self.purpose, ResourcePurpose, "purpose")
        _enum(self.format, ResourceFormat, "format")
        require(isinstance(self.sha256, str) and len(self.sha256) == 64 and
                all(character in "0123456789abcdef" for character in self.sha256),
                "INVALID_RESOURCE_HASH", "canonical lowercase SHA-256 required")
        _integer(self.size_bytes, "size_bytes")


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    message: str
    severity: Severity
    object_id: str | None = None

    def __post_init__(self) -> None:
        _id(self.code, "diagnostic code")
        require(isinstance(self.message, str) and bool(self.message.strip()) and
                len(self.message) <= MAX_TEXT_LENGTH, "INVALID_DIAGNOSTIC", "bounded reason required")
        _enum(self.severity, Severity, "severity")
        if self.object_id is not None:
            _id(self.object_id, "object_id")


@dataclass(frozen=True, slots=True)
class JobIdentity:
    project_id: str
    scenario_id: str
    job_id: str

    def __post_init__(self) -> None:
        for name in ("project_id", "scenario_id", "job_id"):
            _id(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class JobSpec:
    identity: JobIdentity
    module_id: ModuleId
    versions: VersionKey
    space: SpaceRef
    input_resources: tuple[ResourceRef, ...]
    cancel_token: str
    uses_boundary_data: bool = False

    def __post_init__(self) -> None:
        require(isinstance(self.identity, JobIdentity) and isinstance(self.versions, VersionKey) and
                isinstance(self.space, SpaceRef), "INVALID_JOB_CONTEXT", "typed identity/version/space required")
        _enum(self.module_id, ModuleId, "module_id")
        _resources(self.input_resources, purpose=ResourcePurpose.INPUT)
        _id(self.cancel_token, "cancel_token")
        require(type(self.uses_boundary_data) is bool, "INVALID_BOUNDARY_BINDING", "explicit boolean required")
        require(not self.uses_boundary_data or self.versions.boundary_revision is not None,
                "MISSING_BOUNDARY_REVISION", "boundary-dependent job needs its independent revision")


@dataclass(frozen=True, slots=True)
class JobEvent:
    identity: JobIdentity
    versions: VersionKey
    sequence: int
    state: JobState
    stage_id: str | None = None
    completed: int | None = None
    total: int | None = None
    progress_unit: ProgressUnit | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        require(isinstance(self.identity, JobIdentity) and isinstance(self.versions, VersionKey),
                "INVALID_JOB_CONTEXT", "typed identity/version required")
        _integer(self.sequence, "sequence")
        _enum(self.state, JobState, "state")
        if self.stage_id is not None:
            _id(self.stage_id, "stage_id")
        if self.completed is None:
            require(self.total is None and self.progress_unit is None,
                    "INVALID_PROGRESS", "progress fields must be absent together")
        else:
            _integer(self.completed, "completed")
            _id(self.stage_id, "stage_id")
            _enum(self.progress_unit, ProgressUnit, "progress_unit")
            require(self.state != JobState.QUEUED, "INVALID_PROGRESS", "queued job cannot claim work completed")
            if self.total is not None:
                _integer(self.total, "total")
                require(self.completed <= self.total, "INVALID_PROGRESS", "completed exceeds total")
        if self.state == JobState.FAILED:
            _id(self.error_code, "error_code")
        else:
            require(self.error_code is None, "INVALID_JOB_ERROR", "only a failed event carries an error code")


@dataclass(frozen=True, slots=True)
class ResultManifest:
    identity: JobIdentity
    module_id: ModuleId
    versions: VersionKey
    space: SpaceRef
    job_state: JobState
    artifacts: tuple[ResourceRef, ...]
    validation_state: CheckState = CheckState.NOT_CHECKED
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        require(isinstance(self.identity, JobIdentity) and isinstance(self.versions, VersionKey) and
                isinstance(self.space, SpaceRef), "INVALID_JOB_CONTEXT", "typed identity/version/space required")
        _enum(self.module_id, ModuleId, "module_id")
        _enum(self.job_state, JobState, "job_state")
        require(self.job_state in TERMINAL_STATES, "NON_TERMINAL_RESULT", "final manifest needs a terminal state")
        _resources(self.artifacts, purpose=ResourcePurpose.ARTIFACT)
        _enum(self.validation_state, CheckState, "validation_state")
        _diagnostics(self.diagnostics)
        require(self.validation_state != CheckState.PASSED or self.job_state == JobState.SUCCEEDED,
                "INVALID_RESULT_VALIDATION", "unsuccessful computation cannot claim checks passed")
        if self.validation_state in (CheckState.FAILED, CheckState.DATA_INSUFFICIENT):
            require(bool(self.diagnostics), "MISSING_DIAGNOSTIC", "validation failure or missing data needs a reason")
        if self.job_state == JobState.FAILED:
            require(any(item.severity == Severity.ERROR for item in self.diagnostics),
                    "MISSING_JOB_ERROR", "failed computation requires an error diagnostic")
        # Artifact presence and passed checks do not authorize publication to a current project.


@dataclass(frozen=True, slots=True)
class MapLayer:
    layer_id: str
    versions: VersionKey
    space: SpaceRef
    resource: ResourceRef
    geometry_type: GeometryType
    data_state: DataState = DataState.READY
    diagnostics: tuple[Diagnostic, ...] = ()
    source_layer: str | None = None

    def __post_init__(self) -> None:
        _id(self.layer_id, "layer_id")
        _view_header(self.versions, self.space, self.data_state, self.diagnostics)
        _enum(self.geometry_type, GeometryType, "geometry_type")
        if self.geometry_type == GeometryType.RASTER:
            require(isinstance(self.resource, ResourceRef) and self.resource.format == ResourceFormat.GEOTIFF and
                    self.source_layer is None, "INVALID_VIEW_RESOURCE", "raster needs a GeoTIFF without sublayer")
        else:
            _vector_resource(self.resource, self.source_layer)


@dataclass(frozen=True, slots=True)
class ProfileSample:
    station_m: float
    elevation_m: float | None

    def __post_init__(self) -> None:
        _number(self.station_m, "station_m", minimum=0)
        if self.elevation_m is not None:
            _number(self.elevation_m, "elevation_m")


@dataclass(frozen=True, slots=True)
class ProfileViewData:
    alignment_id: str
    versions: VersionKey
    space: SpaceRef
    samples: tuple[ProfileSample, ...]
    data_state: DataState
    diagnostics: tuple[Diagnostic, ...] = ()
    station_unit: str = "m"
    elevation_unit: str = "m"

    def __post_init__(self) -> None:
        _id(self.alignment_id, "alignment_id")
        _view_header(self.versions, self.space, self.data_state, self.diagnostics)
        require(self.station_unit == "m" and self.elevation_unit == "m",
                "INVALID_VIEW_UNIT", "profile distances and elevations are metres")
        _items(self.samples, ProfileSample, "samples", limit=MAX_PROFILE_SAMPLES)
        require(all(first.station_m < second.station_m for first, second in zip(self.samples, self.samples[1:])),
                "INVALID_PROFILE_ORDER", "stations must be strictly increasing")
        missing = len(self.samples) < 2 or self.space.vertical_reference is None or any(
            sample.elevation_m is None for sample in self.samples)
        require(not missing or self.data_state == DataState.DATA_INSUFFICIENT,
                "INVALID_VIEW_READINESS", "missing profile data/reference cannot be ready or merely degraded")


@dataclass(frozen=True, slots=True)
class EnvelopeViewData:
    envelope_id: str
    alignment_id: str
    versions: VersionKey
    space: SpaceRef
    resource: ResourceRef | None
    data_state: DataState
    validation_state: CheckState = CheckState.NOT_CHECKED
    diagnostics: tuple[Diagnostic, ...] = ()
    source_layer: str | None = None

    def __post_init__(self) -> None:
        _id(self.envelope_id, "envelope_id")
        _id(self.alignment_id, "alignment_id")
        _view_header(self.versions, self.space, self.data_state, self.diagnostics)
        _enum(self.validation_state, CheckState, "validation_state")
        if self.resource is None:
            require(self.source_layer is None, "INVALID_SOURCE_LAYER", "no sublayer without a resource")
        else:
            _vector_resource(self.resource, self.source_layer)
        missing = self.resource is None or self.space.vertical_reference is None
        require(not missing or self.data_state == DataState.DATA_INSUFFICIENT,
                "INVALID_VIEW_READINESS", "missing envelope data/reference must remain insufficient")
        require(self.validation_state != CheckState.PASSED or self.data_state == DataState.READY,
                "INVALID_VIEW_VALIDATION", "degraded or insufficient envelope cannot claim passed checks")
        if self.validation_state in (CheckState.FAILED, CheckState.DATA_INSUFFICIENT):
            require(bool(self.diagnostics), "MISSING_DIAGNOSTIC", "constraint result needs a reason")


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    project_id: str
    scenario_id: str
    versions: VersionKey
    space: SpaceRef
    module_states: tuple[tuple[ModuleId, JobState | None], ...]
    resources: tuple[ResourceRef, ...] = ()
    map_layers: tuple[MapLayer, ...] = ()
    profiles: tuple[ProfileViewData, ...] = ()
    envelopes: tuple[EnvelopeViewData, ...] = ()

    def __post_init__(self) -> None:
        _id(self.project_id, "project_id")
        _id(self.scenario_id, "scenario_id")
        require(isinstance(self.versions, VersionKey) and isinstance(self.space, SpaceRef),
                "INVALID_PROJECT_CONTEXT", "typed version/space required")
        require(isinstance(self.module_states, tuple) and len(self.module_states) == len(ModuleId) and
                all(isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], ModuleId) and
                    (item[1] is None or isinstance(item[1], JobState)) for item in self.module_states),
                "INVALID_MODULE_STATES", "explicit state for each of M1-M5; None means no job")
        require({module for module, _ in self.module_states} == set(ModuleId),
                "INVALID_MODULE_STATES", "one entry per module required")
        _resources(self.resources)
        for values, item_type, id_field in ((self.map_layers, MapLayer, "layer_id"),
                                           (self.profiles, ProfileViewData, "alignment_id"),
                                           (self.envelopes, EnvelopeViewData, "envelope_id")):
            _items(values, item_type, id_field)
            require(len({getattr(item, id_field) for item in values}) == len(values),
                    "DUPLICATE_VIEW", id_field)
            require(all(item.versions == self.versions and item.space == self.space for item in values),
                    "VIEW_CONTEXT_MISMATCH", "view must match project version and space")
        resources = {item.resource_id: item for item in self.resources}
        for view in (*self.map_layers, *self.envelopes):
            if view.resource is not None:
                require(resources.get(view.resource.resource_id) == view.resource,
                        "UNREGISTERED_VIEW_RESOURCE", "view resource must match the project resource index")
        profiles = {item.alignment_id: item for item in self.profiles}
        for envelope in self.envelopes:
            require(envelope.alignment_id in profiles,
                    "UNKNOWN_VIEW_ALIGNMENT", "envelope needs its same-version profile")
