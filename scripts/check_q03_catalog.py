# Run: conda run -n csl2-planner --no-capture-output python -s scripts/check_q03_catalog.py
# Expected: self-tests pass; catalog summary, exit 0 iff no ERROR; approval NONE.
# SPDX-License-Identifier: GPL-3.0-only
"""Offline Q-03 draft checks, not source verification or P default approval.

Default: synthetic self-tests plus the repository draft. --self-test-only needs
no repository catalog. --catalog PATH checks a different draft against this
repository. Sources are never read, imported, executed, or requested over HTTP.
GAP counts include recorded gaps, unknown source/baseline versions and unavailable
or merely located sources. UNVERIFIED source bindings are NOTE, not approval.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit
from unittest.mock import patch

SCHEMA = "q03-draft-0.1"
TOP = "draft_schema_version catalog_version status consumer_baseline_refs sources parameters inventory_groups"
BASELINE = "path version sections notes"
SOURCE = "source_id source_kind locator source_version retrieval_status version_verification notes"
LOCATOR = "kind target section"
VERIFICATION = "status game_build model_version asset_scope method evidence_locator"
PARAMETER = "parameter_id name parameter_domain definition scope quantity value_status value range source_refs assumptions constraints usage_limit rule_refs gaps consumer_tasks"
SCOPE = "land_use_types business_subtypes component mode_scope purpose_scope notes"
QUANTITY = "unit_status unit_id quantity_basis time_basis notes"
TIME = "status clock period_id duration_seconds notes"
REFERENCE = "source_id role claim_locator notes"
GROUP = "group_id name definition source_refs rule_refs gaps consumer_tasks"
GAP = "field reason next_evidence"
CONSTRAINT = "kind source_refs"
UNITS = frozenset("person job seat passenger_trip vehicle_trip person_per_m2 job_per_m2 passenger_trip_per_person_per_period passenger_trip_per_job_per_period passenger_trip_per_period vehicle_trip_per_period passenger_trip_per_vehicle_trip kg_per_period kg_per_vehicle_trip game_resource_unit_per_period game_resource_unit_per_vehicle_trip s min m inverse_m ratio percent km_per_km2 km".split())
DIVISORS = frozenset("passenger_trip_per_vehicle_trip kg_per_vehicle_trip game_resource_unit_per_vehicle_trip".split())
LAND_USES = frozenset("residential residential_commercial commercial industrial office public_service transport protection other".split())
MODES = frozenset("car_passenger bus_passenger metro_passenger rail_passenger sea_passenger road_freight NOT_APPLICABLE".split())
CONSUMERS = frozenset("P0-03 P0-05 Q-03.1 Q-07.0".split())
SOURCE_KINDS = frozenset("PRODUCT_RULE PROJECT_PLAN LOCAL_CODE LOCAL_GUIDE GAME_OFFICIAL ENGINEERING_REFERENCE TEST_FIXTURE THEORY_REFERENCE".split())
STATES = frozenset("UNKNOWN EVIDENCED_CANDIDATE THEORETICAL_ASSUMPTION REFERENCE_ONLY SYNTHETIC_ONLY".split())
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z", re.ASCII)


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    record_id: str
    field_path: str
    message: str


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[Issue, ...]
    catalog: Mapping[str, Any] | None
    source_count: int = 0
    parameter_count: int = 0
    group_count: int = 0
    unknown_count: int = 0

    @property
    def error_count(self) -> int:
        return sum(i.severity == "ERROR" for i in self.issues)

    @property
    def gap_count(self) -> int:
        return sum(i.severity == "GAP" for i in self.issues)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


class Checker:
    """Accumulate independent issues before exposing any immutable records."""

    def __init__(self, repository: Path) -> None:
        self.root = repository.resolve()
        self.issues: list[Issue] = []
        self.sources: dict[str, dict[str, Any]] = {}
        self.record = "catalog"

    def issue(self, code: str, path: str, message: str, severity: str = "ERROR") -> None:
        self.issues.append(Issue(severity, code, self.record, path, message))

    def obj(self, value: Any, keys: str, path: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.issue("Q03_SCHEMA", path, "Expected an object.")
            return {}
        expected = set(keys.split())
        for key in sorted(expected - value.keys()):
            self.issue("Q03_SCHEMA", f"{path}.{key}", "Required field is missing.")
        # Do not echo untrusted values or private absolute paths in diagnostics.
        if value.keys() - expected:
            self.issue("Q03_SCHEMA", path, "Unknown field(s); schema is closed.")
        return value

    def text(self, value: Any, path: str, nullable: bool = False) -> bool:
        if nullable and value is None:
            return True
        if not isinstance(value, str) or not value.strip():
            self.issue("Q03_SCHEMA", path, "Expected a nonempty string.")
            return False
        return True

    def choice(self, value: Any, allowed: Any, path: str, code: str = "Q03_SCHEMA") -> bool:
        if not isinstance(value, str) or value not in allowed:
            self.issue(code, path, "Unknown or mistyped enumeration value.")
            return False
        return True

    def array(self, value: Any, path: str, nonempty: bool = False) -> list[Any]:
        if not isinstance(value, list):
            self.issue("Q03_SCHEMA", path, "Expected an array.")
            return []
        if nonempty and not value:
            self.issue("Q03_SCHEMA", path, "Array must not be empty.")
        return value

    def strings(self, value: Any, path: str, nonempty: bool = False,
                allowed: Any = None, code: str = "Q03_SCHEMA") -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for index, item in enumerate(self.array(value, path, nonempty)):
            loc = f"{path}[{index}]"
            if not self.text(item, loc):
                continue
            if item in seen:
                self.issue(code, loc, "Duplicate array identifier.")
            seen.add(item)
            result.append(item)
            if allowed is not None:
                self.choice(item, allowed, loc, code)
        return result

    def identifier(self, value: Any, path: str, seen: set[str]) -> None:
        if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
            self.issue("Q03_SCHEMA", path, "ID must be stable ASCII, 1..80 characters.")
        elif value in seen:
            self.issue("Q03_DUPLICATE_ID", path, "Duplicate ID in its namespace.")
        else:
            seen.add(value)

    def number(self, value: Any, path: str, nullable: bool = False) -> bool:
        if nullable and value is None:
            return True
        valid = type(value) in (int, float)
        if valid:
            try:
                valid = math.isfinite(value)
            except (OverflowError, ValueError):
                valid = False
        if not valid:
            self.issue("Q03_NUMBER", path, "Expected a finite JSON number, not bool/string.")
        return valid

    def local_file(self, value: Any, path: str) -> None:
        if not self.text(value, path):
            return
        if ("\\" in value or ":" in value or value.startswith("/")
                or any(part in ("", ".", "..") for part in value.split("/"))
                or any(ord(char) < 32 for char in value)):
            self.issue("Q03_PATH", path, "Expected a clean repository-relative file path.")
            return
        try:
            target = (self.root / value).resolve()
            if not target.is_relative_to(self.root):
                self.issue("Q03_PATH", path, "Resolved path escapes repository.")
            elif not target.is_file():
                self.issue("Q03_PATH", path, "Local source file is missing or not a file.")
        except (OSError, RuntimeError, ValueError):
            self.issue("Q03_PATH", path, "Local path could not be safely resolved.")

    def https_url(self, value: Any, path: str) -> None:
        if not self.text(value, path):
            return
        try:
            url = urlsplit(value)
            valid = (url.scheme == "https" and bool(url.hostname)
                     and url.username is None and url.password is None
                     and not any(c.isspace() or ord(c) < 32 or c == "\\" for c in value))
            _ = url.port  # Invalid port is a string-validation error, never a request.
        except ValueError:
            valid = False
        if not valid:
            self.issue("Q03_PATH", path, "Expected HTTPS URL with host and no user information.")

    def baseline(self, value: Any, path: str) -> tuple[str, Any] | None:
        obj = self.obj(value, BASELINE, path)
        self.local_file(obj.get("path"), f"{path}.path")
        self.text(obj.get("version"), f"{path}.version", nullable=True)
        self.strings(obj.get("sections"), f"{path}.sections", True)
        self.text(obj.get("notes"), f"{path}.notes")
        if obj.get("version") is None:
            self.issue("Q03_SOURCE_STATE", f"{path}.version", "Baseline version is unknown.", "GAP")
        if isinstance(obj.get("path"), str) and isinstance(obj.get("version"), (str, type(None))):
            return obj["path"], obj.get("version")
        return None

    def source(self, value: Any, path: str, seen: set[str]) -> None:
        obj = self.obj(value, SOURCE, path)
        source_id = obj.get("source_id")
        self.identifier(source_id, f"{path}.source_id", seen)
        self.record = source_id if isinstance(source_id, str) and ID_PATTERN.fullmatch(source_id) else path
        if isinstance(source_id, str) and source_id not in self.sources:
            self.sources[source_id] = obj
        self.choice(obj.get("source_kind"), SOURCE_KINDS, f"{path}.source_kind")
        self.text(obj.get("source_version"), f"{path}.source_version", True)
        self.text(obj.get("notes"), f"{path}.notes")
        locator = self.obj(obj.get("locator"), LOCATOR, f"{path}.locator")
        kind = locator.get("kind")
        self.choice(kind, ("LOCAL_FILE", "HTTPS_URL"), f"{path}.locator.kind")
        self.text(locator.get("section"), f"{path}.locator.section")
        if kind == "LOCAL_FILE":
            self.local_file(locator.get("target"), f"{path}.locator.target")
        elif kind == "HTTPS_URL":
            self.https_url(locator.get("target"), f"{path}.locator.target")
        status = obj.get("retrieval_status")
        self.choice(status, ("LOCATED_ONLY", "LOCAL_READ", "EXTERNAL_READ", "UNAVAILABLE"),
                    f"{path}.retrieval_status", "Q03_SOURCE_STATE")
        if ((status == "LOCAL_READ" and kind != "LOCAL_FILE")
                or (status == "EXTERNAL_READ" and kind != "HTTPS_URL")):
            self.issue("Q03_SOURCE_STATE", f"{path}.retrieval_status", "Read status contradicts locator kind.")
        if status in ("LOCATED_ONLY", "UNAVAILABLE"):
            self.issue("Q03_SOURCE_STATE", f"{path}.retrieval_status", "Source has not been read.", "GAP")
        if obj.get("source_version") is None:
            self.issue("Q03_SOURCE_STATE", f"{path}.source_version", "Source version is unknown.", "GAP")
        verification = self.obj(obj.get("version_verification"), VERIFICATION, f"{path}.version_verification")
        vstatus = verification.get("status")
        self.choice(vstatus, ("UNVERIFIED", "VERSION_BOUND_VERIFIED"),
                    f"{path}.version_verification.status", "Q03_SOURCE_STATE")
        for key in VERIFICATION.split()[1:]:
            self.text(verification.get(key), f"{path}.version_verification.{key}", True)
        if vstatus == "UNVERIFIED":
            if any(verification.get(key) is not None for key in VERIFICATION.split()[1:]):
                self.issue("Q03_SOURCE_STATE", f"{path}.version_verification", "UNVERIFIED fields must all be null.")
            self.issue("Q03_SOURCE_STATE", f"{path}.version_verification", "Version binding is unverified.", "NOTE")
        elif vstatus == "VERSION_BOUND_VERIFIED":
            if (status not in ("LOCAL_READ", "EXTERNAL_READ")
                    or not all(isinstance(verification.get(k), str) and verification[k].strip()
                               for k in ("game_build", "method", "evidence_locator"))
                    or not any(isinstance(verification.get(k), str) and verification[k].strip()
                               for k in ("model_version", "asset_scope"))):
                self.issue("Q03_SOURCE_STATE", f"{path}.version_verification", "Incomplete version-bound verification.")
        self.record = "catalog"

    def references(self, value: Any, path: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        linked: list[tuple[dict[str, Any], dict[str, Any]]] = []
        seen: set[tuple[str, str, str]] = set()
        for index, item in enumerate(self.array(value, path, True)):
            loc = f"{path}[{index}]"
            ref = self.obj(item, REFERENCE, loc)
            for key in REFERENCE.split():
                self.text(ref.get(key), f"{loc}.{key}")
            self.choice(ref.get("role"), ("RULE", "DEFINITION", "VALUE", "ASSUMPTION", "TEST"), f"{loc}.role")
            identifier = ref.get("source_id")
            source = self.sources.get(identifier) if isinstance(identifier, str) else None
            if source is None:
                self.issue("Q03_SOURCE_REF", f"{loc}.source_id", "Source reference is dangling.")
            else:
                linked.append((ref, source))
            identity = tuple(ref.get(k) for k in ("source_id", "role", "claim_locator"))
            if all(isinstance(k, str) for k in identity):
                if identity in seen:
                    self.issue("Q03_SOURCE_REF", loc, "Duplicate source/role/claim reference.")
                seen.add(identity)
        return linked

    def gaps(self, value: Any, record: dict[str, Any], path: str, group: bool = False) -> set[str]:
        fields: set[str] = set()
        for index, item in enumerate(self.array(value, path, group)):
            loc = f"{path}[{index}]"
            gap = self.obj(item, GAP, loc)
            for key in GAP.split():
                self.text(gap.get(key), f"{loc}.{key}")
            field = gap.get("field")
            if isinstance(field, str):
                target: Any = record
                if field == "INVENTORY_SPLIT":
                    valid = group
                else:
                    valid = True
                    for part in field.split("."):
                        if not isinstance(target, dict) or part not in target:
                            valid = False
                            break
                        target = target[part]
                if not valid:
                    self.issue("Q03_SCHEMA", f"{loc}.field", "Gap must name a field of this record or group INVENTORY_SPLIT.")
                well_formed = (set(gap) == set(GAP.split())
                               and all(isinstance(gap.get(k), str) and gap[k].strip() for k in GAP.split()))
                if valid and well_formed:
                    fields.add(field)
                    self.issue("Q03_VALUE_STATE", loc, "Recorded evidence/definition gap.", "GAP")
        if group and "INVENTORY_SPLIT" not in fields:
            self.issue("Q03_SCHEMA", path, "Inventory group requires INVENTORY_SPLIT gap.")
        return fields

    def require_gap(self, fields: set[str], field: str, path: str, code: str) -> None:
        if field not in fields:
            self.issue(code, path, f"Missing explicit gap for {field}.")

    def scope(self, value: Any, fields: set[str], path: str) -> dict[str, Any]:
        scope = self.obj(value, SCOPE, path)
        for key, allowed in (("land_use_types", LAND_USES), ("business_subtypes", None),
                             ("mode_scope", MODES), ("purpose_scope", None)):
            items = self.strings(scope.get(key), f"{path}.{key}", allowed=allowed, code="Q03_SCOPE")
            if not items and key != "business_subtypes":
                self.require_gap(fields, f"scope.{key}", f"{path}.{key}", "Q03_SCOPE")
            if key == "mode_scope" and "NOT_APPLICABLE" in items and len(items) != 1:
                self.issue("Q03_SCOPE", f"{path}.{key}", "NOT_APPLICABLE must appear alone.")
        self.choice(scope.get("component"), ("WHOLE", "RESIDENTIAL", "COMMERCIAL", "NOT_APPLICABLE", "UNRESOLVED"),
                    f"{path}.component", "Q03_SCOPE")
        if scope.get("component") == "UNRESOLVED":
            self.require_gap(fields, "scope.component", f"{path}.component", "Q03_SCOPE")
        self.text(scope.get("notes"), f"{path}.notes")
        return scope

    def quantity(self, value: Any, fields: set[str], state: Any, path: str) -> dict[str, Any]:
        quantity = self.obj(value, QUANTITY, path)
        unit_status, unit = quantity.get("unit_status"), quantity.get("unit_id")
        self.choice(unit_status, ("KNOWN", "UNRESOLVED"), f"{path}.unit_status", "Q03_UNIT")
        if unit_status == "KNOWN":
            self.choice(unit, UNITS, f"{path}.unit_id", "Q03_UNIT")
        elif unit_status == "UNRESOLVED":
            if unit is not None:
                self.issue("Q03_UNIT", f"{path}.unit_id", "Unresolved unit must be null.")
            self.require_gap(fields, "quantity.unit_id", f"{path}.unit_id", "Q03_UNIT")
        self.text(quantity.get("quantity_basis"), f"{path}.quantity_basis")
        self.text(quantity.get("notes"), f"{path}.notes")
        time = self.obj(quantity.get("time_basis"), TIME, f"{path}.time_basis")
        status = time.get("status")
        self.choice(status, ("KNOWN", "UNRESOLVED", "NOT_APPLICABLE"), f"{path}.time_basis.status", "Q03_TIME")
        self.text(time.get("notes"), f"{path}.time_basis.notes")
        if status == "KNOWN":
            self.choice(time.get("clock"), ("SIMULATION", "REAL_WORLD"), f"{path}.time_basis.clock", "Q03_TIME")
            self.text(time.get("period_id"), f"{path}.time_basis.period_id")
            duration = time.get("duration_seconds")
            if self.number(duration, f"{path}.time_basis.duration_seconds") and duration <= 0:
                self.issue("Q03_TIME", f"{path}.time_basis.duration_seconds", "Time duration must be strictly positive.")
        elif status in ("UNRESOLVED", "NOT_APPLICABLE"):
            if any(time.get(k) is not None for k in ("clock", "period_id", "duration_seconds")):
                self.issue("Q03_TIME", f"{path}.time_basis", "Unresolved/inapplicable time fields must be null.")
            if status == "UNRESOLVED":
                self.require_gap(fields, "quantity.time_basis", f"{path}.time_basis", "Q03_TIME")
        if isinstance(unit, str) and unit.endswith("_per_period"):
            if status == "NOT_APPLICABLE":
                self.issue("Q03_TIME", f"{path}.time_basis", "Per-period unit requires a time basis.")
            if status == "UNRESOLVED" and state not in ("UNKNOWN", "REFERENCE_ONLY", "SYNTHETIC_ONLY"):
                self.issue("Q03_TIME", f"{path}.time_basis", "Consumable candidate needs resolved time.")
        return quantity

    def parameter(self, value: Any, path: str, seen: set[str]) -> None:
        obj = self.obj(value, PARAMETER, path)
        identifier = obj.get("parameter_id")
        self.identifier(identifier, f"{path}.parameter_id", seen)
        self.record = identifier if isinstance(identifier, str) and ID_PATTERN.fullmatch(identifier) else path
        for key in ("name", "definition", "usage_limit"):
            self.text(obj.get(key), f"{path}.{key}")
        self.choice(obj.get("parameter_domain"), ("DEMAND", "SERVICE", "GEOMETRY", "REFERENCE", "TEST"),
                    f"{path}.parameter_domain")
        self.strings(obj.get("rule_refs"), f"{path}.rule_refs", True)
        self.strings(obj.get("consumer_tasks"), f"{path}.consumer_tasks", True, CONSUMERS)
        fields = self.gaps(obj.get("gaps"), obj, f"{path}.gaps")
        self.scope(obj.get("scope"), fields, f"{path}.scope")
        state = obj.get("value_status")
        self.choice(state, STATES, f"{path}.value_status", "Q03_VALUE_STATE")
        quantity = self.quantity(obj.get("quantity"), fields, state, f"{path}.quantity")
        refs = self.references(obj.get("source_refs"), f"{path}.source_refs")
        assumptions = self.strings(obj.get("assumptions"), f"{path}.assumptions")
        number = obj.get("value")
        value_ok = self.number(number, f"{path}.value", True)
        bounds: list[tuple[str, int | float]] = []
        if number is not None and value_ok:
            bounds.append((f"{path}.value", number))
        range_value = obj.get("range")
        if range_value is not None:
            interval = self.obj(range_value, "lower upper", f"{path}.range")
            lower, upper = interval.get("lower"), interval.get("upper")
            lower_ok = self.number(lower, f"{path}.range.lower")
            upper_ok = self.number(upper, f"{path}.range.upper")
            for key, valid in (("lower", lower_ok), ("upper", upper_ok)):
                if valid:
                    bounds.append((f"{path}.range.{key}", interval[key]))
            if lower_ok and upper_ok:
                if lower > upper:
                    self.issue("Q03_VALUE_STATE", f"{path}.range", "Range lower exceeds upper.")
                elif number is not None and value_ok and not lower <= number <= upper:
                    self.issue("Q03_VALUE_STATE", f"{path}.value", "Point value lies outside its range.")
        kinds: set[str] = set()
        for index, item in enumerate(self.array(obj.get("constraints"), f"{path}.constraints")):
            loc = f"{path}.constraints[{index}]"
            constraint = self.obj(item, CONSTRAINT, loc)
            kind = constraint.get("kind")
            if self.choice(kind, ("NONNEGATIVE", "STRICTLY_POSITIVE", "UNIT_INTERVAL", "PERCENT_INTERVAL"),
                           f"{loc}.kind", "Q03_CONSTRAINT"):
                if kind in kinds:
                    self.issue("Q03_CONSTRAINT", f"{loc}.kind", "Duplicate constraint kind.")
                kinds.add(kind)
                for location, candidate in bounds:
                    valid = ((kind == "NONNEGATIVE" and candidate >= 0)
                             or (kind == "STRICTLY_POSITIVE" and candidate > 0)
                             or (kind == "UNIT_INTERVAL" and 0 <= candidate <= 1)
                             or (kind == "PERCENT_INTERVAL" and 0 <= candidate <= 100))
                    if not valid:
                        self.issue("Q03_CONSTRAINT", location, "Value/range violates explicit constraint.")
            constraint_refs = self.references(constraint.get("source_refs"), f"{loc}.source_refs")
            if not any(ref.get("role") in ("RULE", "DEFINITION", "ASSUMPTION") for ref, _ in constraint_refs):
                self.issue("Q03_CONSTRAINT", f"{loc}.source_refs", "Constraint needs definition/dimension evidence, not only a value.")
        unit = quantity.get("unit_id")
        if isinstance(unit, str) and unit in DIVISORS and bounds and "STRICTLY_POSITIVE" not in kinds:
            self.issue("Q03_CONSTRAINT", f"{path}.constraints", "Numeric divisor requires explicit STRICTLY_POSITIVE.")
        if state == "UNKNOWN":
            if number is not None or range_value is not None:
                self.issue("Q03_VALUE_STATE", path, "UNKNOWN requires value=null and range=null.")
            self.require_gap(fields, "value", f"{path}.gaps", "Q03_VALUE_STATE")
        elif isinstance(state, str) and state in STATES:
            if number is None:
                self.issue("Q03_VALUE_STATE", f"{path}.value", "This status requires a point value.")
            role = "ASSUMPTION" if state == "THEORETICAL_ASSUMPTION" else "TEST" if state == "SYNTHETIC_ONLY" else "VALUE"
            evidenced = any(ref.get("role") == role
                            and source.get("retrieval_status") in ("LOCAL_READ", "EXTERNAL_READ")
                            and ((source.get("source_kind") == "TEST_FIXTURE") == (state == "SYNTHETIC_ONLY"))
                            for ref, source in refs)
            if not evidenced:
                self.issue("Q03_VALUE_STATE", f"{path}.source_refs", "Status lacks read, correctly typed supporting evidence.")
            if state == "THEORETICAL_ASSUMPTION" and not assumptions:
                self.issue("Q03_VALUE_STATE", f"{path}.assumptions", "Theoretical value needs explicit model premises.")
            if state in ("EVIDENCED_CANDIDATE", "THEORETICAL_ASSUMPTION"):
                time = quantity.get("time_basis")
                if (quantity.get("unit_status") != "KNOWN" or not isinstance(time, dict)
                        or time.get("status") not in ("KNOWN", "NOT_APPLICABLE")
                        or any(f.startswith(("scope.", "quantity.")) or f in ("scope", "quantity") for f in fields)):
                    self.issue("Q03_VALUE_STATE", path, "Candidate/assumption has unresolved scope or quantity.")
            if state == "REFERENCE_ONLY" and obj.get("parameter_domain") != "REFERENCE":
                self.issue("Q03_VALUE_STATE", f"{path}.parameter_domain", "Reference-only value requires REFERENCE domain.")
            if state == "SYNTHETIC_ONLY" and obj.get("parameter_domain") != "TEST":
                self.issue("Q03_VALUE_STATE", f"{path}.parameter_domain", "Synthetic-only value requires TEST domain.")
            self.issue("Q03_VALUE_STATE", path, "Recorded value is not approved for production.", "NOTE")
        self.record = "catalog"

    def group(self, value: Any, path: str, seen: set[str]) -> None:
        obj = self.obj(value, GROUP, path)
        identifier = obj.get("group_id")
        self.identifier(identifier, f"{path}.group_id", seen)
        self.record = identifier if isinstance(identifier, str) and ID_PATTERN.fullmatch(identifier) else path
        for key in ("name", "definition"):
            self.text(obj.get(key), f"{path}.{key}")
        self.references(obj.get("source_refs"), f"{path}.source_refs")
        self.strings(obj.get("rule_refs"), f"{path}.rule_refs", True)
        self.strings(obj.get("consumer_tasks"), f"{path}.consumer_tasks", True, CONSUMERS)
        self.gaps(obj.get("gaps"), obj, f"{path}.gaps", True)
        self.record = "catalog"


def validate_catalog(value: Any, repository: Path) -> ValidationReport:
    """Return immutable records only after all structural/semantic checks pass."""
    checker = Checker(repository)
    obj = checker.obj(value, TOP, "$")
    checker.choice(obj.get("draft_schema_version"), (SCHEMA,), "$.draft_schema_version")
    checker.text(obj.get("catalog_version"), "$.catalog_version")
    checker.choice(obj.get("status"), ("DRAFT",), "$.status")
    baseline_ids: set[tuple[str, Any]] = set()
    for index, item in enumerate(checker.array(obj.get("consumer_baseline_refs"), "$.consumer_baseline_refs", True)):
        loc = f"$.consumer_baseline_refs[{index}]"
        identity = checker.baseline(item, loc)
        if identity is not None:
            if identity in baseline_ids:
                checker.issue("Q03_SCHEMA", loc, "Duplicate baseline path/version.")
            baseline_ids.add(identity)
    source_ids: set[str] = set()
    sources = checker.array(obj.get("sources"), "$.sources", True)
    for index, item in enumerate(sources):
        checker.source(item, f"$.sources[{index}]", source_ids)
    record_ids: set[str] = set()
    parameters = checker.array(obj.get("parameters"), "$.parameters")
    groups = checker.array(obj.get("inventory_groups"), "$.inventory_groups")
    if not parameters and not groups:
        checker.issue("Q03_SCHEMA", "$", "Parameters and inventory groups cannot both be empty.")
    for index, item in enumerate(parameters):
        checker.parameter(item, f"$.parameters[{index}]", record_ids)
    for index, item in enumerate(groups):
        checker.group(item, f"$.inventory_groups[{index}]", record_ids)
    errors = any(i.severity == "ERROR" for i in checker.issues)
    unknown = sum(isinstance(p, dict) and p.get("value_status") == "UNKNOWN" for p in parameters)
    return ValidationReport(tuple(checker.issues), None if errors else _freeze(obj),
                            len(sources), len(parameters), len(groups), unknown)


class CatalogReadError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogReadError("Q03_JSON_DUPLICATE_KEY", "Duplicate JSON object key.")
        result[key] = value
    return result


def _constant(_: str) -> Any:
    raise CatalogReadError("Q03_NUMBER", "NaN/Infinity are forbidden JSON constants.")


def _float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise CatalogReadError("Q03_NUMBER", "JSON floating-point number overflows.")
    return number


def _integer(text: str) -> int:
    try:
        number = int(text)
        finite = math.isfinite(number)
    except (ValueError, OverflowError):
        finite = False
    if not finite:
        raise CatalogReadError("Q03_NUMBER", "JSON integer exceeds finite validation range.")
    return number


def parse_catalog(text: str, repository: Path) -> ValidationReport:
    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant,
                           parse_float=_float, parse_int=_integer)
    except CatalogReadError as exc:
        return ValidationReport((Issue("ERROR", exc.code, "catalog", "$", str(exc)),), None)
    except (ValueError, RecursionError):
        return ValidationReport((Issue("ERROR", "Q03_SCHEMA", "catalog", "$", "Invalid or excessively nested JSON."),), None)
    return validate_catalog(value, repository)


def load_catalog(path: Path, repository: Path) -> ValidationReport:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        return ValidationReport((Issue("ERROR", "Q03_PATH", "catalog", "$", "Catalog is missing, unreadable, or not UTF-8."),), None)
    return parse_catalog(text, repository)


def _fixture() -> dict[str, Any]:
    """All values and source metadata below are synthetic, never game defaults."""
    def ref(role: str) -> dict[str, str]:
        return {"source_id": "S-1", "role": role, "claim_locator": "synthetic section 1", "notes": "Synthetic fixture only."}
    return {
        "draft_schema_version": SCHEMA, "catalog_version": "synthetic-1", "status": "DRAFT",
        "consumer_baseline_refs": [{"path": "source.txt", "version": "synthetic-1", "sections": ["1"], "notes": "Synthetic baseline."}],
        "sources": [{"source_id": "S-1", "source_kind": "THEORY_REFERENCE",
                     "locator": {"kind": "LOCAL_FILE", "target": "source.txt", "section": "1"},
                     "source_version": "synthetic-1", "retrieval_status": "LOCAL_READ",
                     "version_verification": {"status": "UNVERIFIED", "game_build": None, "model_version": None,
                                              "asset_scope": None, "method": None, "evidence_locator": None},
                     "notes": "Synthetic source, not an actual game claim."}],
        "parameters": [{"parameter_id": "P-1", "name": "synthetic_occupancy", "parameter_domain": "DEMAND",
                        "definition": "Synthetic ratio of passenger trips to vehicle trips.",
                        "scope": {"land_use_types": ["residential"], "business_subtypes": [], "component": "NOT_APPLICABLE",
                                  "mode_scope": ["car_passenger"], "purpose_scope": ["synthetic-commute"], "notes": "Synthetic scope."},
                        "quantity": {"unit_status": "KNOWN", "unit_id": "passenger_trip_per_vehicle_trip",
                                     "quantity_basis": "Synthetic passenger trips / vehicle trips.",
                                     "time_basis": {"status": "UNRESOLVED", "clock": None, "period_id": None,
                                                    "duration_seconds": None, "notes": "Unknown synthetic time applicability."},
                                     "notes": "Synthetic units."},
                        "value_status": "UNKNOWN", "value": None, "range": None, "source_refs": [ref("DEFINITION")],
                        "assumptions": [], "constraints": [{"kind": "STRICTLY_POSITIVE", "source_refs": [ref("DEFINITION")]}],
                        "usage_limit": "Synthetic fixture only; never approve production defaults.", "rule_refs": ["synthetic rule"],
                        "gaps": [{"field": "value", "reason": "Unknown synthetic value.", "next_evidence": "Synthetic evidence."},
                                 {"field": "quantity.time_basis", "reason": "Unknown time.", "next_evidence": "Synthetic time evidence."}],
                        "consumer_tasks": ["P0-03"]}],
        "inventory_groups": [],
    }


def _ready() -> dict[str, Any]:
    data = _fixture()
    parameter = data["parameters"][0]
    parameter.update(value_status="EVIDENCED_CANDIDATE", value=1.5, gaps=[])
    parameter["source_refs"][0]["role"] = "VALUE"
    parameter["quantity"]["time_basis"].update(status="NOT_APPLICABLE", notes="Synthetic ratio without a time denominator.")
    return data


def _group() -> dict[str, Any]:
    parameter = _fixture()["parameters"][0]
    return {"group_id": "G-1", "name": "synthetic_inventory", "definition": "Synthetic unsplit quantities.",
            "source_refs": parameter["source_refs"], "rule_refs": ["synthetic rule"],
            "gaps": [{"field": "INVENTORY_SPLIT", "reason": "Not split.", "next_evidence": "Define individual quantities."}],
            "consumer_tasks": ["P0-03"]}


def self_tests() -> tuple[int, int]:
    """Independent expectations; no dependencies on the real catalog or sources."""
    passed = failed = 0
    with tempfile.TemporaryDirectory(prefix="q03-fixtures-") as temporary:
        root = Path(temporary) / "repository"
        root.mkdir()
        (root / "source.txt").write_text("Synthetic fixture; must never be read or executed.\n", encoding="utf-8")
        outside = Path(temporary) / "outside.txt"
        outside.write_text("Synthetic outside file.\n", encoding="utf-8")

        def expect(name: str, report: ValidationReport, codes: tuple[str, ...] = ()) -> None:
            nonlocal passed, failed
            found = {issue.code for issue in report.issues if issue.severity == "ERROR"}
            valid = (not found and report.catalog is not None) if not codes else (set(codes) <= found and report.catalog is None)
            if valid:
                passed += 1
            else:
                failed += 1
                print(f"[FAIL] {name}: expected {','.join(codes) or 'valid'}, got {','.join(sorted(found)) or 'valid'}")

        def check(name: str, data: Any, codes: tuple[str, ...] = ()) -> None:
            try:
                expect(name, parse_catalog(json.dumps(data), root), codes)
            except Exception as exc:
                nonlocal failed
                failed += 1
                print(f"[FAIL] {name}: unexpected {type(exc).__name__}")

        def change(name: str, path: str, value: Any, code: str, ready: bool = False) -> None:
            data = _ready() if ready else _fixture()
            node: Any = data
            parts = path.split(".")
            for part in parts[:-1]:
                node = node[int(part)] if isinstance(node, list) else node[part]
            key: Any = int(parts[-1]) if isinstance(node, list) else parts[-1]
            node[key] = value
            check(name, data, (code,))

        check("legal UNKNOWN", _fixture())
        check("legal candidate remains unapproved", _ready())
        data = _fixture()
        data.update(parameters=[], inventory_groups=[_group()])
        check("legal group-only catalog", data)
        data["inventory_groups"][0]["gaps"].append({"field": "value", "reason": "Unknown.", "next_evidence": "Split first."})
        check("group gap cannot name absent value field", data, ("Q03_SCHEMA",))
        for state, domain, role, kind in (("REFERENCE_ONLY", "REFERENCE", "VALUE", "THEORY_REFERENCE"),
                                           ("SYNTHETIC_ONLY", "TEST", "TEST", "TEST_FIXTURE"),
                                           ("THEORETICAL_ASSUMPTION", "DEMAND", "ASSUMPTION", "THEORY_REFERENCE")):
            data = _ready()
            data["parameters"][0].update(value_status=state, parameter_domain=domain, assumptions=["Synthetic model premise."])
            data["parameters"][0]["source_refs"][0]["role"] = role
            data["sources"][0]["source_kind"] = kind
            check(f"legal {state}", data)
        data = _fixture()
        data["sources"][0]["notes"] = "Background mentions walking, cycling, taxi, tram, internal air and non-road freight."
        check("excluded modes in source notes are harmless", data)
        data["parameters"][0]["scope"].update(business_subtypes=["airport"], land_use_types=["transport"])
        data["parameters"][0]["rule_refs"].append("R1 D4 airport deduplication")
        check("UNKNOWN airport, no mapping generated", data)
        data = _ready()
        data["parameters"][0].update(value=-5, constraints=[])
        data["parameters"][0]["quantity"]["unit_id"] = "m"
        check("signed elevation has no global nonnegative bound", data)
        data["parameters"][0].update(value=0, constraints=[{"kind": "NONNEGATIVE", "source_refs": _fixture()["parameters"][0]["source_refs"]}])
        check("evidenced zero allowed when definition allows", data)
        data = _ready()
        data["parameters"][0]["quantity"].update(unit_id="passenger_trip_per_person_per_period",
            time_basis={"status": "KNOWN", "clock": "SIMULATION", "period_id": "synthetic-period", "duration_seconds": 60, "notes": "Synthetic simulation period."})
        check("known simulation period", data)
        data = _ready()
        data["sources"][0]["version_verification"].update(status="VERSION_BOUND_VERIFIED", game_build="synthetic-build",
            model_version="synthetic-model", method="Synthetic method.", evidence_locator="Synthetic locator.")
        check("complete synthetic version binding still not approval", data)
        data = _fixture()
        data["sources"][0].update(retrieval_status="UNAVAILABLE", locator={"kind": "HTTPS_URL", "target": "https://example.invalid/source", "section": "1"})
        check("unavailable external source remains valid GAP", data)

        cases = [
            ("unknown schema", "draft_schema_version", "future", "Q03_SCHEMA", False),
            ("approved status", "status", "APPROVED", "Q03_SCHEMA", False),
            ("empty sources", "sources", [], "Q03_SCHEMA", False),
            ("empty baseline", "consumer_baseline_refs", [], "Q03_SCHEMA", False),
            ("dangling source", "parameters.0.source_refs.0.source_id", "S-MISSING", "Q03_SOURCE_REF", False),
            ("bool numeric", "parameters.0.value", True, "Q03_NUMBER", True),
            ("string numeric", "parameters.0.value", "1.5", "Q03_NUMBER", True),
            ("numeric array", "parameters.0.value", [1.5], "Q03_NUMBER", True),
            ("numeric null candidate", "parameters.0.value", None, "Q03_VALUE_STATE", True),
            ("UNKNOWN zero", "parameters.0.value", 0, "Q03_VALUE_STATE", False),
            ("UNKNOWN number", "parameters.0.value", 2, "Q03_VALUE_STATE", False),
            ("UNKNOWN range", "parameters.0.range", {"lower": 1, "upper": 2}, "Q03_VALUE_STATE", False),
            ("rule is not value evidence", "parameters.0.source_refs.0.role", "RULE", "Q03_VALUE_STATE", True),
            ("definition is not value evidence", "parameters.0.source_refs.0.role", "DEFINITION", "Q03_VALUE_STATE", True),
            ("located evidence", "sources.0.retrieval_status", "LOCATED_ONLY", "Q03_VALUE_STATE", True),
            ("unavailable evidence", "sources.0.retrieval_status", "UNAVAILABLE", "Q03_VALUE_STATE", True),
            ("test promoted to candidate", "sources.0.source_kind", "TEST_FIXTURE", "Q03_VALUE_STATE", True),
            ("reference wrong domain", "parameters.0.value_status", "REFERENCE_ONLY", "Q03_VALUE_STATE", True),
            ("synthetic wrong domain/evidence", "parameters.0.value_status", "SYNTHETIC_ONLY", "Q03_VALUE_STATE", True),
            ("unresolved unit not null", "parameters.0.quantity.unit_status", "UNRESOLVED", "Q03_UNIT", False),
            ("unknown unit", "parameters.0.quantity.unit_id", "cars_per_day", "Q03_UNIT", False),
            ("unresolved time lacks gap", "parameters.0.quantity.time_basis.status", "UNRESOLVED", "Q03_TIME", True),
            ("per-period time inapplicable", "parameters.0.quantity.unit_id", "kg_per_period", "Q03_TIME", True),
            ("unresolved time carries clock", "parameters.0.quantity.time_basis.clock", "SIMULATION", "Q03_TIME", False),
            ("zero occupancy", "parameters.0.value", 0, "Q03_CONSTRAINT", True),
            ("negative occupancy", "parameters.0.value", -1, "Q03_CONSTRAINT", True),
            ("missing divisor constraint", "parameters.0.constraints", [], "Q03_CONSTRAINT", True),
            ("constraint only cites VALUE", "parameters.0.constraints.0.source_refs.0.role", "VALUE", "Q03_CONSTRAINT", True),
            ("range reversed", "parameters.0.range", {"lower": 3, "upper": 2}, "Q03_VALUE_STATE", True),
            ("value outside range", "parameters.0.range", {"lower": 2, "upper": 3}, "Q03_VALUE_STATE", True),
            ("range zero occupancy", "parameters.0.range", {"lower": 0, "upper": 2}, "Q03_CONSTRAINT", True),
            ("range string bound", "parameters.0.range", {"lower": "1", "upper": 2}, "Q03_NUMBER", True),
            ("range bool bound", "parameters.0.range", {"lower": True, "upper": 2}, "Q03_NUMBER", True),
            ("missing gap for empty land use", "parameters.0.scope.land_use_types", [], "Q03_SCOPE", False),
            ("unknown land use", "parameters.0.scope.land_use_types", ["airport"], "Q03_SCOPE", False),
            ("unresolved component lacks gap", "parameters.0.scope.component", "UNRESOLVED", "Q03_SCOPE", False),
            ("NOT_APPLICABLE mixed", "parameters.0.scope.mode_scope", ["NOT_APPLICABLE", "car_passenger"], "Q03_SCOPE", False),
            ("invalid consumer", "parameters.0.consumer_tasks", ["P0-04"], "Q03_SCHEMA", False),
            ("duplicate consumer", "parameters.0.consumer_tasks", ["P0-03", "P0-03"], "Q03_SCHEMA", False),
            ("external read on local", "sources.0.retrieval_status", "EXTERNAL_READ", "Q03_SOURCE_STATE", False),
            ("incomplete verified binding", "sources.0.version_verification.status", "VERSION_BOUND_VERIFIED", "Q03_SOURCE_STATE", False),
            ("unverified carrying build", "sources.0.version_verification.game_build", "synthetic-build", "Q03_SOURCE_STATE", False),
        ]
        for name, path, value, code, ready in cases:
            change(name, path, value, code, ready)
        for mode in ("walking", "cycling", "taxi", "tram", "internal_air", "rail_freight", "sea_freight"):
            change(f"excluded mode {mode}", "parameters.0.scope.mode_scope", [mode], "Q03_SCOPE")
        for target in ("/outside.txt", "C:/outside.txt", "C:outside.txt", "//host/share/file", "\\\\host\\share\\file",
                       "../outside.txt", "sub/../../outside.txt", "sub\\file", "sub//file", "missing.txt", "source.txt:stream", "."):
            change("unsafe/missing source path", "sources.0.locator.target", target, "Q03_PATH")
        for url in ("http://example.invalid/", "https:///missing", "https://user:pass@example.invalid/", "https://example.invalid:bad/", "https://bad host/"):
            data = _fixture()
            data["sources"][0].update(locator={"kind": "HTTPS_URL", "target": url, "section": "1"}, retrieval_status="UNAVAILABLE")
            check("invalid URL", data, ("Q03_PATH",))
        data = _fixture()
        data["sources"][0]["locator"].update(kind="HTTPS_URL", target="https://example.invalid/")
        check("local read on URL", data, ("Q03_SOURCE_STATE",))
        data = _ready()
        data["parameters"][0].update(value_status="THEORETICAL_ASSUMPTION", assumptions=[])
        data["parameters"][0]["source_refs"][0]["role"] = "ASSUMPTION"
        check("theory without premises", data, ("Q03_VALUE_STATE",))
        for duration in (0, -1, True, "60"):
            data = _ready()
            data["parameters"][0]["quantity"]["time_basis"].update(status="KNOWN", clock="REAL_WORLD", period_id="fixture", duration_seconds=duration)
            check("bad known time duration", data, ("Q03_NUMBER" if type(duration) is not int else "Q03_TIME",))
        for unit in ("kg_per_vehicle_trip", "game_resource_unit_per_vehicle_trip"):
            data = _ready()
            data["parameters"][0]["quantity"]["unit_id"] = unit
            data["parameters"][0]["value"] = 0
            check("zero payload", data, ("Q03_CONSTRAINT",))
        for kind, value in (("UNIT_INTERVAL", 1.5), ("PERCENT_INTERVAL", 101), ("NONNEGATIVE", -1)):
            data = _ready()
            data["parameters"][0]["quantity"]["unit_id"] = "ratio"
            data["parameters"][0]["constraints"][0]["kind"] = kind
            data["parameters"][0]["value"] = value
            check("violated explicit interval/bound", data, ("Q03_CONSTRAINT",))
        data = _fixture()
        data.update(parameters=[], inventory_groups=[])
        check("both record arrays empty", data, ("Q03_SCHEMA",))
        data = _fixture()
        data["parameters"][0]["gaps"] = []
        check("UNKNOWN lacks explicit gaps", data, ("Q03_VALUE_STATE", "Q03_TIME"))
        data = _fixture()
        data["inventory_groups"] = [_group()]
        data["inventory_groups"][0]["gaps"] = []
        check("group lacks split gap", data, ("Q03_SCHEMA",))
        for section in ("sources", "parameters", "inventory_groups"):
            data = _fixture()
            if section == "inventory_groups":
                data[section] = [_group()]
            data[section].append(json.loads(json.dumps(data[section][0])))
            check("duplicate record ID", data, ("Q03_DUPLICATE_ID",))
        data = _fixture()
        data["inventory_groups"] = [_group()]
        data["inventory_groups"][0]["group_id"] = "P-1"
        check("cross parameter/group ID collision", data, ("Q03_DUPLICATE_ID",))
        data = _fixture()
        data["consumer_baseline_refs"] *= 2
        check("duplicate baseline identity", data, ("Q03_SCHEMA",))
        for section, key in ((None, "approved"), ("parameters", "editable"), ("inventory_groups", "value")):
            data = _fixture()
            if section == "inventory_groups":
                data[section] = [_group()]
            target = data if section is None else data[section][0]
            target[key] = True
            check("unknown/approval key", data, ("Q03_SCHEMA",))
        for path in ("sources.0.locator", "sources.0.version_verification", "parameters.0.scope", "parameters.0.quantity", "parameters.0.quantity.time_basis", "parameters.0.source_refs.0", "parameters.0.gaps.0", "parameters.0.constraints.0"):
            data = _fixture()
            node: Any = data
            for part in path.split("."):
                node = node[int(part)] if isinstance(node, list) else node[part]
            node["unknown_key"] = True
            check("nested closed schema", data, ("Q03_SCHEMA",))
        for key in PARAMETER.split():
            data = _fixture()
            del data["parameters"][0][key]
            check("missing parameter field", data, ("Q03_SCHEMA",))
        for key in SOURCE.split():
            data = _fixture()
            del data["sources"][0][key]
            check("missing source field", data, ("Q03_SCHEMA",))
        data = _fixture()
        data["parameters"][0]["gaps"][0]["reason"] = ""
        malformed = validate_catalog(data, root)
        expect("malformed gap is rejected", malformed, ("Q03_SCHEMA",))
        if malformed.gap_count == 1:
            passed += 1
        else:
            failed += 1
            print("[FAIL] malformed gap must not count as a legal GAP")
        data = _ready()
        data["parameters"].append(json.loads(json.dumps(data["parameters"][0])))
        data["parameters"][1].update(parameter_id="P-2")
        data["parameters"][1]["quantity"]["quantity_basis"] = "Different synthetic statistical denominator."
        distinct = validate_catalog(data, root)
        expect("same unit does not merge distinct bases", distinct)
        if distinct.catalog is not None and len(distinct.catalog["parameters"]) == 2:
            passed += 1
        else:
            failed += 1
            print("[FAIL] distinct statistical bases must not be converted/merged")
        # Shape corruption tests must reject cleanly, not crash on unhashable values.
        for path in ("sources.0.source_kind", "sources.0.source_id", "parameters.0.value_status", "parameters.0.quantity.unit_id",
                     "parameters.0.scope.component", "parameters.0.source_refs.0.source_id", "parameters.0.constraints.0.kind"):
            for invalid in ([], {}, False):
                change("mistyped scalar", path, invalid, "Q03_SCHEMA" if path.endswith("source_id") else
                       "Q03_VALUE_STATE" if path.endswith("value_status") else "Q03_UNIT" if path.endswith("unit_id") else
                       "Q03_SCOPE" if path.endswith("component") else "Q03_CONSTRAINT" if path.endswith("constraints.0.kind") else "Q03_SCHEMA")
        for text, code in (("{\"a\":1,\"a\":2}", "Q03_JSON_DUPLICATE_KEY"),
                           ("{\"nested\":{\"a\":1,\"a\":2}}", "Q03_JSON_DUPLICATE_KEY"),
                           ("NaN", "Q03_NUMBER"), ("Infinity", "Q03_NUMBER"), ("-Infinity", "Q03_NUMBER"),
                           ("1e999", "Q03_NUMBER"), ("1" + "0" * 400, "Q03_NUMBER"), ("not JSON", "Q03_SCHEMA")):
            expect("strict JSON parser", parse_catalog(text, root), (code,))
        expect("missing catalog", load_catalog(root / "missing.json", root), ("Q03_PATH",))
        data = _fixture()
        original_resolve = Path.resolve
        with patch.object(Path, "resolve", lambda self, *a, **kw: outside if self == root / "source.txt" else original_resolve(self, *a, **kw)):
            expect("simulated out-of-repository reparse resolution", validate_catalog(data, root), ("Q03_PATH",))
        with patch.object(Path, "read_text", side_effect=AssertionError("source body must not be read")):
            expect("source existence only, no body reads", validate_catalog(_fixture(), root))
        data = _ready()
        report = validate_catalog(data, root)
        try:
            report.catalog["parameters"][0]["value"] = 999
        except TypeError:
            passed += 1
        else:
            failed += 1
            print("[FAIL] nested records must be immutable")
        data["parameters"][0]["value"] = 999
        if report.catalog["parameters"][0]["value"] == 1.5:
            passed += 1
        else:
            failed += 1
            print("[FAIL] report must not alias mutable input")
        data = _ready()
        data["parameters"][0]["value"] = True
        data["parameters"].append(_fixture()["parameters"][0])
        data["parameters"][1]["parameter_id"] = "P-2"
        data["parameters"][1]["source_refs"][0]["source_id"] = "MISSING"
        check("all records checked; no partial catalog returned", data, ("Q03_NUMBER", "Q03_SOURCE_REF"))
    status = "SUCCESS" if not failed else "FAILURE"
    print(f"[{status}] self-tests: {passed} passed, {failed} failed; synthetic only, no skipped cases.")
    return passed, failed


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--self-test-only", action="store_true", help="Only run independent synthetic fixtures.")
    group.add_argument("--catalog", type=Path, default=repository / "prototypes/q03/draft_catalog.json", help="Optional draft JSON path.")
    arguments = parser.parse_args()
    _, failed = self_tests()
    if arguments.self_test_only:
        print("\u751f\u4ea7\u9ed8\u8ba4\u6279\u51c6\uff1a\u65e0")
        return 1 if failed else 0
    report = load_catalog(arguments.catalog, repository)
    status = "REJECTED" if report.error_count else "STRUCTURE OK"
    print(f"[{status}] Q-03 draft (structural consistency only; not source-truth verification).")
    print(f"Sources={report.source_count}; Parameters={report.parameter_count}; InventoryGroups={report.group_count}; UNKNOWN={report.unknown_count}")
    print(f"ERROR={report.error_count}; GAP={report.gap_count}; NOTE={sum(i.severity == 'NOTE' for i in report.issues)}")
    errors = [issue for issue in report.issues if issue.severity == "ERROR"]
    for issue in errors[:20]:
        print(f"[ERROR] {issue.code} {issue.record_id} {issue.field_path}: {issue.message}")
    if len(errors) > 20:
        print(f"[NOTE] {len(errors) - 20} additional errors; inspect ValidationReport.issues programmatically.")
    print("\u751f\u4ea7\u9ed8\u8ba4\u6279\u51c6\uff1a\u65e0")
    return 1 if failed else 2 if report.error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
