# SPDX-License-Identifier: GPL-3.0-only
"""Executable P0 contracts: planar policy checks, not a 3D route solver."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from math import isfinite
from typing import Any, Mapping

from shapely.geometry import LineString, MultiPolygon, Polygon, mapping, shape
from shapely.ops import unary_union


class ContractError(ValueError):
    """Invalid input or forbidden update, with a stable reason code."""
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise ContractError(code, message)


def finite_number(value: Any, name: str, *, minimum: float | None = None) -> None:
    require(isinstance(value, (int, float)) and not isinstance(value, bool)
            and isfinite(value), "INVALID_NUMBER", f"{name} must be finite")
    if minimum is not None:
        require(value >= minimum, "INVALID_NUMBER", f"{name} is below {minimum}")


def identifier(value: Any, name: str) -> None:
    require(isinstance(value, str) and bool(value.strip()), "INVALID_ID", name)


def polygon(value: Any, name: str) -> None:
    require(isinstance(value, (Polygon, MultiPolygon)), "INVALID_GEOMETRY", name)
    require(not value.is_empty and value.is_valid and not value.has_z
            and all(isfinite(x) for x in value.bounds),
            "INVALID_GEOMETRY", f"{name} must be a valid nonempty 2D polygon")


class LandUse(str, Enum):
    RESIDENTIAL = "residential"
    MIXED = "residential_commercial"
    COMMERCIAL = "commercial"
    INDUSTRIAL = "industrial"
    OFFICE = "office"
    PUBLIC = "public_service"
    TRANSPORT = "transport"
    PROTECTION = "protection"
    OTHER = "other"


EDITABLE_SCENARIOS = frozenset({LandUse.RESIDENTIAL, LandUse.MIXED})


@dataclass(frozen=True, slots=True)
class ScenarioValues:
    """Immutable parameters; physical schema/defaults are not frozen by P0."""
    entries: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        require(isinstance(self.entries, tuple), "INVALID_PARAMETERS", "immutable entries required")
        names: set[str] = set()
        for entry in self.entries:
            require(isinstance(entry, tuple) and len(entry) == 2,
                    "INVALID_PARAMETERS", "parameter pairs required")
            name, value = entry
            identifier(name, "parameter name")
            require(name not in names, "DUPLICATE_PARAMETER", name)
            finite_number(value, name, minimum=0)
            names.add(name)

    def overlay(self, overrides: ScenarioValues) -> ScenarioValues:
        values = dict(self.entries)
        require(set(dict(overrides.entries)) <= set(values),
                "UNKNOWN_PARAMETER", "override keys must belong to default schema")
        values.update(overrides.entries)
        return ScenarioValues(tuple(sorted(values.items())))


@dataclass(frozen=True, slots=True)
class DefaultCatalog:
    version: str
    entries: tuple[tuple[LandUse, ScenarioValues], ...]
    user_defaults: bool = False

    def __post_init__(self) -> None:
        identifier(self.version, "default version")
        require(type(self.user_defaults) is bool and isinstance(self.entries, tuple),
                "INVALID_DEFAULTS", "immutable catalog required")
        seen: set[LandUse] = set()
        for entry in self.entries:
            require(isinstance(entry, tuple) and len(entry) == 2,
                    "INVALID_DEFAULTS", "default pairs required")
            use, values = entry
            require(isinstance(use, LandUse) and isinstance(values, ScenarioValues),
                    "INVALID_DEFAULTS", "typed defaults required")
            require(use not in seen, "DUPLICATE_DEFAULT", use.value)
            require(not self.user_defaults or use in EDITABLE_SCENARIOS,
                    "SCENARIO_LOCKED", "non-residential user defaults forbidden")
            seen.add(use)

    def get(self, use: LandUse) -> ScenarioValues | None:
        return next((values for kind, values in self.entries if kind == use), None)


@dataclass(frozen=True, slots=True)
class PlanningZone:
    """Only zone entity: land-use zone and TAZ are the same object."""
    zone_id: str
    geometry: Polygon | MultiPolygon
    land_use: LandUse
    block_size_m: tuple[float, float]
    allow_demolition: bool = False
    scenario_overrides: ScenarioValues | None = None

    def __post_init__(self) -> None:
        identifier(self.zone_id, "zone_id")
        polygon(self.geometry, "zone geometry")
        require(isinstance(self.land_use, LandUse), "INVALID_LAND_USE", "typed land use required")
        require(type(self.allow_demolition) is bool, "INVALID_PERMISSION", "boolean required")
        require(isinstance(self.block_size_m, tuple) and len(self.block_size_m) == 2,
                "INVALID_BLOCK_SIZE", "two immutable net block dimensions required")
        for size in self.block_size_m:
            finite_number(size, "block_size_m")
            require(size > 0, "INVALID_BLOCK_SIZE", "positive net block dimensions required")
        if self.scenario_overrides is not None:
            require(isinstance(self.scenario_overrides, ScenarioValues),
                    "INVALID_PARAMETERS", "typed overrides required")
            require(self.land_use in EDITABLE_SCENARIOS,
                    "SCENARIO_LOCKED", "non-residential scenario parameters read-only")


def resolve_scenario(zone: PlanningZone, system: DefaultCatalog,
                     user: DefaultCatalog | None = None) -> ScenarioValues:
    require(not system.user_defaults, "INVALID_DEFAULTS", "system catalog required")
    base = system.get(zone.land_use)
    require(base is not None, "MISSING_DEFAULTS", zone.land_use.value)
    assert base is not None
    if user is not None:
        require(user.user_defaults, "INVALID_DEFAULTS", "user catalog required")
        inherited = user.get(zone.land_use)
        if inherited is not None:
            base = base.overlay(inherited)
    if zone.scenario_overrides is not None:
        require(zone.land_use in EDITABLE_SCENARIOS, "SCENARIO_LOCKED", "forbidden override")
        base = base.overlay(zone.scenario_overrides)
    return base


def change_land_use(zone: PlanningZone, use: LandUse) -> PlanningZone:
    return replace(zone, land_use=use,
                   scenario_overrides=zone.scenario_overrides if use in EDITABLE_SCENARIOS else None)


def zone_from_mapping(payload: Mapping[str, Any]) -> PlanningZone:
    """Strict import: input fields cannot grant editing or demolition rights."""
    allowed = {"zone_id", "geometry", "land_use", "block_size_m",
               "allow_demolition", "scenario_overrides"}
    required = {"zone_id", "geometry", "land_use", "block_size_m"}
    require(isinstance(payload, Mapping), "INVALID_INPUT", "zone object required")
    require(set(payload) <= allowed and required <= set(payload),
            "INVALID_SCHEMA", "unknown or missing zone fields")
    try:
        values = payload.get("scenario_overrides")
        require(values is None or isinstance(values, Mapping),
                "INVALID_PARAMETERS", "parameter object required")
        return PlanningZone(
            zone_id=payload["zone_id"], geometry=shape(payload["geometry"]),
            land_use=LandUse(payload["land_use"]),
            block_size_m=tuple(payload["block_size_m"]),
            allow_demolition=payload.get("allow_demolition", False),
            scenario_overrides=None if values is None else ScenarioValues(tuple(values.items())),
        )
    except ContractError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ContractError("INVALID_INPUT", "invalid zone payload") from exc


def zone_to_mapping(zone: PlanningZone) -> dict[str, Any]:
    """Save one zone identity, never a second TAZ identity; revalidate permissions."""
    checked = replace(zone)
    return {"zone_id": checked.zone_id, "geometry": mapping(checked.geometry),
            "land_use": checked.land_use.value, "block_size_m": list(checked.block_size_m),
            "allow_demolition": checked.allow_demolition,
            "scenario_overrides": None if checked.scenario_overrides is None
            else dict(checked.scenario_overrides.entries)}


class Mode(str, Enum):
    SURFACE = "surface"
    ELEVATED = "elevated"
    TUNNEL = "tunnel"


class PolicyStatus(str, Enum):
    POLICY_CLEAR = "policy_clear"  # Not a full geometry/engineering pass.
    TUNNEL_REQUIRED = "tunnel_required"  # Not a solved profile.
    DATA_INSUFFICIENT = "data_insufficient"
    NO_FEASIBLE_SOLUTION = "no_feasible_solution"
    STALE_INPUT = "stale_input"


@dataclass(frozen=True, slots=True)
class ExistingObject:
    object_id: str
    footprint: Polygon | MultiPolygon
    ground_z_m: float | None = None
    foundation_bottom_z_m: float | None = None

    def __post_init__(self) -> None:
        identifier(self.object_id, "object_id")
        polygon(self.footprint, "existing footprint")
        for value in (self.ground_z_m, self.foundation_bottom_z_m):
            if value is not None:
                finite_number(value, "existing elevation")
        if self.ground_z_m is not None and self.foundation_bottom_z_m is not None:
            require(self.foundation_bottom_z_m <= self.ground_z_m,
                    "INVALID_ELEVATION", "foundation bottom cannot be above ground")


@dataclass(frozen=True, slots=True)
class Corridor:
    corridor_id: str
    space_id: str
    alignment: LineString
    envelope_radius_m: float
    requested_mode: Mode
    asset_modes: frozenset[Mode]
    nominal_z_m: float | None = None
    minimum_z_m: float | None = None

    def __post_init__(self) -> None:
        identifier(self.corridor_id, "corridor_id")
        identifier(self.space_id, "space_id")
        require(isinstance(self.alignment, LineString), "INVALID_GEOMETRY", "line required")
        require(not self.alignment.is_empty and self.alignment.is_valid
                and not self.alignment.has_z and self.alignment.length > 0
                and all(isfinite(v) for xy in self.alignment.coords for v in xy),
                "INVALID_GEOMETRY", "finite nonzero planar alignment required")
        finite_number(self.envelope_radius_m, "envelope_radius_m")
        require(self.envelope_radius_m > 0, "INVALID_ENVELOPE", "positive asset envelope required")
        require(isinstance(self.requested_mode, Mode) and isinstance(self.asset_modes, frozenset)
                and bool(self.asset_modes) and all(isinstance(m, Mode) for m in self.asset_modes),
                "INVALID_ASSET", "typed nonempty asset modes required")
        for value in (self.nominal_z_m, self.minimum_z_m):
            if value is not None:
                finite_number(value, "candidate elevation")


@dataclass(frozen=True, slots=True)
class Snapshot:
    revision: str
    space_id: str
    zones: tuple[PlanningZone, ...]
    objects: tuple[ExistingObject, ...]
    existing_data_complete: bool
    protected_areas: tuple[Polygon | MultiPolygon, ...] = ()

    def __post_init__(self) -> None:
        identifier(self.revision, "revision")
        identifier(self.space_id, "space_id")
        require(type(self.existing_data_complete) is bool and isinstance(self.zones, tuple)
                and isinstance(self.objects, tuple) and isinstance(self.protected_areas, tuple),
                "INVALID_SNAPSHOT", "immutable collections and explicit coverage required")
        require(all(isinstance(z, PlanningZone) for z in self.zones)
                and all(isinstance(o, ExistingObject) for o in self.objects),
                "INVALID_SNAPSHOT", "typed zones and existing objects required")
        require(len({z.zone_id for z in self.zones}) == len(self.zones),
                "DUPLICATE_ZONE", "zone IDs must be unique")
        require(len({o.object_id for o in self.objects}) == len(self.objects),
                "DUPLICATE_OBJECT", "object IDs must be unique")
        for index, zone in enumerate(self.zones):
            for other in self.zones[index + 1:]:
                require(zone.geometry.intersection(other.geometry).area == 0,
                        "OVERLAPPING_ZONES", "zone interiors must not overlap")
        for area in self.protected_areas:
            polygon(area, "protected area")


@dataclass(frozen=True, slots=True)
class TunnelRules:
    version: str
    control_buffer_m: float
    minimum_cover_m: float
    foundation_clearance_m: float
    outer_height_m: float

    def __post_init__(self) -> None:
        identifier(self.version, "rule version")
        for value in (self.control_buffer_m, self.minimum_cover_m, self.foundation_clearance_m):
            finite_number(value, "tunnel rule distance", minimum=0)
        finite_number(self.outer_height_m, "outer_height_m")
        require(self.outer_height_m > 0, "INVALID_TUNNEL_RULE", "positive outer height required")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    status: PolicyStatus
    reason: str
    input_revision: str
    permitted_modes: frozenset[Mode]
    mandatory_object_ids: tuple[str, ...] = ()
    demolition_candidate_ids: tuple[str, ...] = ()
    unresolved_association_ids: tuple[str, ...] = ()
    centre_z_upper_bounds_m: tuple[tuple[str, float], ...] = ()
    lower_only: bool = False


def decide_conflicts(corridor: Corridor, snapshot: Snapshot, rules: TunnelRules,
                     *, expected_revision: str) -> PolicyDecision:
    """Admissible modes/local bounds only; never a 3D-feasibility claim."""
    if expected_revision != snapshot.revision:
        return PolicyDecision(PolicyStatus.STALE_INPUT, "INPUT_REVISION_CHANGED",
                              snapshot.revision, frozenset())
    require(corridor.space_id == snapshot.space_id,
            "COORDINATE_SPACE_MISMATCH", "explicit common metre-based space required")
    envelope = corridor.alignment.buffer(corridor.envelope_radius_m)
    if any(envelope.intersects(area) for area in snapshot.protected_areas):
        return PolicyDecision(PolicyStatus.NO_FEASIBLE_SOLUTION, "HARD_PROTECTION_CONFLICT",
                              snapshot.revision, frozenset())

    mandatory: list[ExistingObject] = []
    demolition: list[str] = []
    unresolved: list[str] = []
    for obj in snapshot.objects:
        if not envelope.intersects(obj.footprint.buffer(rules.control_buffer_m)):
            continue
        zones = tuple(z for z in snapshot.zones if z.geometry.intersects(obj.footprint))
        fully_resolved = bool(zones) and unary_union([z.geometry for z in zones]).covers(obj.footprint)
        if fully_resolved and all(z.allow_demolition for z in zones):
            demolition.append(obj.object_id)
        else:
            mandatory.append(obj)
            if not fully_resolved:
                unresolved.append(obj.object_id)

    modes = frozenset({Mode.TUNNEL}) if mandatory else corridor.asset_modes
    base = dict(input_revision=snapshot.revision, permitted_modes=modes,
                mandatory_object_ids=tuple(sorted(o.object_id for o in mandatory)),
                demolition_candidate_ids=tuple(sorted(demolition)),
                unresolved_association_ids=tuple(sorted(unresolved)), lower_only=bool(mandatory))
    if mandatory and Mode.TUNNEL not in corridor.asset_modes:
        base['permitted_modes'] = frozenset()
        return PolicyDecision(PolicyStatus.NO_FEASIBLE_SOLUTION, "TUNNEL_ASSET_UNSUPPORTED", **base)
    if not snapshot.existing_data_complete or unresolved:
        return PolicyDecision(PolicyStatus.DATA_INSUFFICIENT, "EXISTING_COVERAGE_OR_ZONES_UNKNOWN", **base)
    if not mandatory:
        if corridor.requested_mode not in corridor.asset_modes:
            base['permitted_modes'] = frozenset()
            return PolicyDecision(PolicyStatus.NO_FEASIBLE_SOLUTION, "REQUESTED_MODE_UNSUPPORTED", **base)
        return PolicyDecision(PolicyStatus.POLICY_CLEAR, "NO_NON_DEMOLISHABLE_CONFLICT", **base)
    if any(o.ground_z_m is None or o.foundation_bottom_z_m is None for o in mandatory):
        return PolicyDecision(PolicyStatus.DATA_INSUFFICIENT, "UNDERGROUND_ENVELOPE_MISSING", **base)

    ceilings: list[tuple[str, float]] = []
    for obj in mandatory:
        assert obj.ground_z_m is not None and obj.foundation_bottom_z_m is not None
        top_limit = min(obj.ground_z_m - rules.minimum_cover_m,
                        obj.foundation_bottom_z_m - rules.foundation_clearance_m)
        ceilings.append((obj.object_id, top_limit - rules.outer_height_m / 2))
    base['centre_z_upper_bounds_m'] = tuple(sorted(ceilings))
    if corridor.minimum_z_m is not None and any(z < corridor.minimum_z_m for _, z in ceilings):
        base['permitted_modes'] = frozenset()
        return PolicyDecision(PolicyStatus.NO_FEASIBLE_SOLUTION, "UNDERGROUND_DEPTH_RANGE_EMPTY", **base)
    return PolicyDecision(PolicyStatus.TUNNEL_REQUIRED, "LOWER_ONLY_NO_OVERPASS", **base)
