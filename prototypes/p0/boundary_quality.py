"""P0-02 source-boundary retention and conservative screening; no physical acceptance."""
# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import fiona
import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import Polygon, MultiPolygon, shape
from shapely.geometry.base import BaseGeometry

from .carto_adapter import AdapterError, file_hash, metric_crs, require, shapefile_components
from .carto_quality import review_geometry
from .contracts import (Corridor, ExistingObject, Mode, PolicyDecision, PolicyStatus,
                        Snapshot, TunnelRules, decide_conflicts)

BOUNDARY_QUALITY_VERSION = "p0-02-boundary-quality-0.1.0"


def source_geometry_quality(wkb: bytes | None) -> tuple[str, BaseGeometry | None]:
    if wkb is None:
        return "NULL_GEOMETRY", None
    require(isinstance(wkb, bytes), "INVALID_SOURCE_RECORD", "immutable source WKB required")
    try:
        geometry = shapely.from_wkb(wkb)
    except (shapely.errors.GEOSException, ValueError) as exc:
        raise AdapterError("INVALID_SOURCE_WKB", str(exc)) from exc
    if geometry.is_empty:
        return "EMPTY_GEOMETRY", geometry
    if not np.isfinite(shapely.get_coordinates(geometry, include_z=geometry.has_z)).all():
        return "NONFINITE_GEOMETRY", geometry
    if geometry.has_z:
        return "Z_GEOMETRY_UNVERIFIED", geometry
    if not isinstance(geometry, (Polygon, MultiPolygon)):
        return "UNSUPPORTED_FOOTPRINT", geometry
    if not geometry.is_valid:
        return "INVALID_GEOMETRY", geometry
    return "VALID_PLANAR_SOURCE", geometry


@dataclass(frozen=True, slots=True)
class BoundaryRecord:
    source_record_id: str
    source_geometry_wkb: bytes | None
    source_form: str | None = None
    candidate_wkb: bytes | None = None
    candidate_status: str | None = None
    quality: str = field(init=False)
    source_guard: BaseGeometry | None = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        require(isinstance(self.source_record_id, str) and bool(self.source_record_id.strip()),
                "INVALID_SOURCE_RECORD", "source ID required")
        quality, geometry = source_geometry_quality(self.source_geometry_wkb)
        require(self.source_form is None or isinstance(self.source_form, str), "INVALID_SOURCE_RECORD", "source form must be text/unknown")
        require(self.candidate_wkb is None or isinstance(self.candidate_wkb, bytes), "INVALID_CANDIDATE", "immutable candidate WKB required")
        require(self.candidate_status is None or isinstance(self.candidate_status, str), "INVALID_CANDIDATE", "candidate status must be text/unknown")
        guard = None
        if geometry is not None and not geometry.is_empty and np.isfinite(shapely.get_coordinates(geometry)).all():
            guard = geometry if quality == "VALID_PLANAR_SOURCE" else geometry.envelope
        object.__setattr__(self, "quality", quality)
        object.__setattr__(self, "source_guard", guard)

    @property
    def degraded(self) -> bool:
        return self.quality != "VALID_PLANAR_SOURCE"


@dataclass(frozen=True, slots=True)
class BoundarySnapshot:
    revision: str
    space_id: str
    layer_name: str
    crs: Any
    records: tuple[BoundaryRecord, ...]
    declared_source_count: int
    source_files: tuple[tuple[str, str], ...]
    valid_coverage: Polygon | MultiPolygon
    coverage_confirmed: bool
    _index: Any = field(init=False, repr=False, compare=False)
    _located: tuple[BoundaryRecord, ...] = field(init=False, repr=False, compare=False)
    _unlocated: tuple[BoundaryRecord, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        require(all(isinstance(value, str) and bool(value.strip()) for value in (self.revision, self.space_id, self.layer_name)),
                "INVALID_BOUNDARY_SNAPSHOT", "revision/space/layer identifiers required")
        metric_crs(self.crs)
        require(isinstance(self.records, tuple) and all(isinstance(record, BoundaryRecord) for record in self.records),
                "INVALID_BOUNDARY_SNAPSHOT", "typed immutable records required")
        require(type(self.declared_source_count) is int and self.declared_source_count == len(self.records),
                "SOURCE_RECORDS_DROPPED", "retain every original boundary record")
        require(len({record.source_record_id for record in self.records}) == len(self.records),
                "DUPLICATE_SOURCE_RECORD", "source IDs unique within snapshot")
        require(isinstance(self.source_files, tuple) and bool(self.source_files) and
                all(isinstance(item, tuple) and len(item) == 2 for item in self.source_files),
                "INVALID_BOUNDARY_SNAPSHOT", "immutable source component identities required")
        require(len({name for name, _ in self.source_files}) == len(self.source_files),
                "INVALID_BOUNDARY_SNAPSHOT", "unique source component names required")
        for name, digest in self.source_files:
            require(isinstance(name, str) and bool(name) and isinstance(digest, str) and len(digest) == 64 and
                    all(c in "0123456789abcdefABCDEF" for c in digest), "INVALID_SOURCE_HASH", "SHA-256 source provenance required")
        require(isinstance(self.valid_coverage, (Polygon, MultiPolygon)) and not self.valid_coverage.is_empty and
                self.valid_coverage.is_valid and not self.valid_coverage.has_z and np.isfinite(shapely.get_coordinates(self.valid_coverage)).all(),
                "INVALID_COVERAGE", "explicit valid metre-plane coverage required")
        require(type(self.coverage_confirmed) is bool, "INVALID_COVERAGE_ASSERTION", "actual boolean required")
        located = tuple(record for record in self.records if record.source_guard is not None)
        object.__setattr__(self, "_located", located)
        object.__setattr__(self, "_unlocated", tuple(record for record in self.records if record.source_guard is None))
        object.__setattr__(self, "_index", STRtree([record.source_guard for record in located]))

    def object_id(self, record: BoundaryRecord) -> str:
        identity = sha256((self.layer_name + "\0" + self.revision).encode("utf-8")).hexdigest()[:16]
        return "boundary:" + identity + ":" + record.source_record_id

    def affected_records(self, envelope: BaseGeometry, buffer_m: float) -> tuple[BoundaryRecord, ...]:
        # Bounding-box query is broad phase only; precise check still includes full guard/control buffer.
        indexes = self._index.query(envelope.buffer(buffer_m).envelope)
        located = tuple(self._located[int(index)] for index in sorted(indexes)
                        if envelope.intersects(self._located[int(index)].source_guard.buffer(buffer_m)) or
                        envelope.intersects(self._located[int(index)].source_guard))
        return located + self._unlocated


def load_boundary_snapshot(path: Path, *, revision: str, space_id: str,
                           valid_coverage: Polygon | MultiPolygon, coverage_confirmed: bool) -> BoundarySnapshot:
    components = shapefile_components(path)
    before = tuple((item.name, file_hash(item)) for item in components)
    records = []
    with fiona.open(path, "r") as source:
        crs = metric_crs(source.crs_wkt or source.crs or None)
        declared = len(source)
        for feature in source:
            geometry = None if feature["geometry"] is None else shape(feature["geometry"])
            wkb = None if geometry is None else geometry.wkb
            status, candidate = None, None
            if (geometry is not None and not geometry.is_empty and not geometry.is_valid and not geometry.has_z and
                    isinstance(geometry, (Polygon, MultiPolygon)) and np.isfinite(shapely.get_coordinates(geometry)).all()):
                review = review_geometry(geometry)
                status, candidate = review["status"], bytes.fromhex(review["candidate_wkb_hex"])
            form = feature["properties"].get("Form")
            records.append(BoundaryRecord(str(feature.id), wkb, None if form is None else str(form), candidate, status))
    require(before == tuple((item.name, file_hash(item)) for item in components), "SOURCE_CHANGED", "boundary files changed during import")
    return BoundarySnapshot(revision, space_id, path.name, crs.to_string(), tuple(records), declared,
                            before, valid_coverage, coverage_confirmed)


def boundary_snapshot_report(snapshot: BoundarySnapshot, *, include_degraded_sources: bool = False) -> dict[str, Any]:
    require(type(include_degraded_sources) is bool, "INVALID_REPORT_FLAG", "actual boolean required")
    qualities = Counter(record.quality for record in snapshot.records)
    candidates = Counter(record.candidate_status for record in snapshot.records if record.candidate_status)
    retained = []
    for record in snapshot.records:
        if not record.degraded:
            continue
        guard = record.source_guard
        item = {"source_record_id": record.source_record_id, "object_id": snapshot.object_id(record),
                "source_quality": record.quality, "source_form": record.source_form,
                "source_wkb_sha256": None if record.source_geometry_wkb is None else sha256(record.source_geometry_wkb).hexdigest(),
                "conservative_bounds_m": None if guard is None else list(guard.bounds),
                "location_known": guard is not None, "candidate_status": record.candidate_status,
                "candidate_accepted": False, "physical_envelope_accepted": False,
                "automatic_demolition_allowed": False, "review_required": True}
        if include_degraded_sources:
            item["source_wkb_hex"] = None if record.source_geometry_wkb is None else record.source_geometry_wkb.hex()
            item["candidate_wkb_hex"] = None if record.candidate_wkb is None else record.candidate_wkb.hex()
        retained.append(item)
    return {"quality_version": BOUNDARY_QUALITY_VERSION, "revision": snapshot.revision, "space_id": snapshot.space_id,
            "source_layer": snapshot.layer_name, "crs": str(snapshot.crs), "source_count": snapshot.declared_source_count,
            "retained_count": len(snapshot.records), "records_dropped": 0,
            "quality_counts": dict(qualities), "candidate_counts": dict(candidates),
            "degraded_retained_count": len(retained), "unlocatable_count": sum(not item["location_known"] for item in retained),
            "source_files": dict(snapshot.source_files), "degraded_records": retained,
            "quality_exit": "degraded-retained; screening only; physical envelopes not accepted",
            "all_planar_sources_physical_envelopes_verified": False}


def decide_with_boundary_quality(corridor: Corridor, snapshot: Snapshot, rules: TunnelRules,
                                boundaries: BoundarySnapshot, *, expected_revision: str,
                                expected_boundary_revision: str) -> PolicyDecision:
    """Core wrapper; valid source records and degraded guards cannot vanish at import."""
    if expected_revision != snapshot.revision or expected_boundary_revision != boundaries.revision:
        return PolicyDecision(PolicyStatus.STALE_INPUT, "BOUNDARY_OR_INPUT_REVISION_CHANGED", snapshot.revision, frozenset())
    require(boundaries.space_id == snapshot.space_id == corridor.space_id,
            "COORDINATE_SPACE_MISMATCH", "one explicit metre space required for core/quality inputs")
    envelope = corridor.alignment.buffer(corridor.envelope_radius_m)
    relevant = boundaries.affected_records(envelope, rules.control_buffer_m)
    degraded = tuple(record for record in relevant if record.degraded)
    objects = list(snapshot.objects)
    identifiers = {obj.object_id for obj in objects}
    for record in relevant:
        guard = record.source_guard
        if guard is None or not isinstance(guard, (Polygon, MultiPolygon)) or guard.area <= 0:
            continue
        identity = boundaries.object_id(record)
        require(identity not in identifiers, "SOURCE_OBJECT_COLLISION", "quality source ID must not overwrite known object")
        identifiers.add(identity)
        objects.append(ExistingObject(identity, guard))  # Never invent ground or foundation from Form/Elevation.
    combined = replace(snapshot, objects=tuple(objects))
    base = decide_conflicts(corridor, combined, rules, expected_revision=expected_revision)
    retained_ids = tuple(sorted(boundaries.object_id(record) for record in degraded))
    demolition = tuple(identity for identity in base.demolition_candidate_ids if identity not in retained_ids)
    mandatory = tuple(sorted(set(base.mandatory_object_ids) | set(retained_ids)))
    if base.status == PolicyStatus.NO_FEASIBLE_SOLUTION:
        return replace(base, demolition_candidate_ids=demolition, mandatory_object_ids=mandatory)
    if not boundaries.coverage_confirmed or not boundaries.valid_coverage.covers(envelope):
        return replace(base, status=PolicyStatus.DATA_INSUFFICIENT, reason="BOUNDARY_COVERAGE_UNKNOWN",
                       permitted_modes=frozenset(), demolition_candidate_ids=demolition, mandatory_object_ids=mandatory)
    if any(record.source_guard is None for record in degraded):
        return replace(base, status=PolicyStatus.DATA_INSUFFICIENT, reason="UNLOCATABLE_BOUNDARY_RETAINED",
                       permitted_modes=frozenset(), demolition_candidate_ids=demolition, mandatory_object_ids=mandatory)
    if degraded:
        modes = frozenset({Mode.TUNNEL}) if Mode.TUNNEL in corridor.asset_modes else frozenset()
        return replace(base, status=PolicyStatus.DATA_INSUFFICIENT if modes else PolicyStatus.NO_FEASIBLE_SOLUTION,
                       reason="DEGRADED_BOUNDARY_REVIEW_REQUIRED" if modes else "TUNNEL_ASSET_UNSUPPORTED",
                       permitted_modes=modes, lower_only=True, demolition_candidate_ids=demolition,
                       mandatory_object_ids=mandatory, centre_z_upper_bounds_m=())
    return base  # No affected degraded record is not a physical/3D clearance certificate.
