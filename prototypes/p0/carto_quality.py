"""P0-02 source assertions, capability gates and geometry review candidates.
No candidate is automatically accepted as a physical network envelope.
"""
# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

import fiona
from shapely import make_valid, to_wkb
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from .carto_adapter import AdapterError, require

QUALITY_VERSION = "p0-02-quality-0.1.0"
DOCUMENTED_CARTO_FACTS = {
    "source": "https://github.com/taipei-native/Carto/wiki",
    "manual_version": "1.0.17", "checked_date": "2026-10-07",
    "depth_unit": "m", "nonpositive_depth_export": "NoData",
    "terrain_reference": "game_origin_not_sea_level",
    "dataset_version_verified": False,
    "coverage_and_projection_options_verified": False,
}


@dataclass(frozen=True)
class SourceAssertions:
    """Explicit source declarations. A document fact does not confirm this file."""
    carto_source_confirmed: bool = False
    complete_depth_coverage_confirmed: bool = False
    terrain_unit: str | None = None
    terrain_vertical_reference: str | None = None
    game_mapping_confirmed: bool = False
    evidence: str = ""

    def __post_init__(self) -> None:
        flags = (self.carto_source_confirmed, self.complete_depth_coverage_confirmed,
                 self.game_mapping_confirmed)
        require(all(type(value) is bool for value in flags),
                "INVALID_SOURCE_ASSERTION", "confirmation fields must be actual booleans")
        require(self.terrain_unit in (None, "m"), "UNSUPPORTED_VERTICAL_UNIT",
                "normalize terrain units explicitly; only declared metres are supported")
        require(self.terrain_vertical_reference is None or
                (isinstance(self.terrain_vertical_reference, str) and bool(self.terrain_vertical_reference.strip())),
                "INVALID_VERTICAL_REFERENCE", "use an explicit nonempty vertical reference")
        require(isinstance(self.evidence, str), "INVALID_SOURCE_ASSERTION", "evidence must be text")
        if any(flags) or self.terrain_unit or self.terrain_vertical_reference:
            require(bool(self.evidence.strip()), "MISSING_ASSERTION_EVIDENCE", "record the declaration source")
        require(not self.complete_depth_coverage_confirmed or self.carto_source_confirmed,
                "SOURCE_UNCONFIRMED", "Carto NoData land rule requires confirmed source identity")


def assertions_from_mapping(value: Mapping[str, Any]) -> SourceAssertions:
    require(isinstance(value, Mapping), "INVALID_SOURCE_ASSERTION", "assertions must be an object")
    allowed = set(SourceAssertions.__dataclass_fields__)
    require(set(value) <= allowed, "UNKNOWN_SOURCE_ASSERTION", "unknown declaration fields")
    return SourceAssertions(**dict(value))


def assess_readiness(actual: Mapping[str, Any], assertions: SourceAssertions) -> dict[str, Any]:
    """Input sufficiency gate; this is neither G0 nor physical/3D acceptance."""
    require(isinstance(assertions, SourceAssertions), "INVALID_SOURCE_ASSERTION", "typed assertions required")
    terrain, depth, alignment = actual["terrain"], actual["depth"], actual["alignment"]
    conditions = {
        "RASTER_EMPTY": terrain["valid_pixels"] > 0 and depth["valid_pixels"] > 0,
        "GRID_UNVERIFIED": alignment["pixel_conservation_passed"] is True,
        "TERRAIN_MISSING": alignment["terrain_missing_pixels"] == 0,
        "DEPTH_OUTSIDE_ROI": alignment["depth_outside_pixels"] == 0,
        "CARTO_SOURCE_UNCONFIRMED": assertions.carto_source_confirmed,
        "DEPTH_COVERAGE_UNCONFIRMED": assertions.complete_depth_coverage_confirmed,
        "LAND_WATER_UNKNOWN": alignment["counts"]["unknown"] == 0,
        "TERRAIN_UNIT_UNCONFIRMED": assertions.terrain_unit == "m",
        "VERTICAL_REFERENCE_UNCONFIRMED": assertions.terrain_vertical_reference is not None,
        "GAME_MAPPING_UNCONFIRMED": assertions.game_mapping_confirmed,
    }
    blocking = [{"code": code, "severity": "blocking"} for code, passed in conditions.items() if not passed]
    optional = []
    for layer in actual.get("vectors", ()):
        counts = layer.get("counts", {})
        issues = sum(counts.get(key, 0) for key in
                     ("null_geometry", "empty_geometry", "invalid_geometry", "z_geometry_requires_adapter"))
        if issues:
            optional.append({"code": "OPTIONAL_LAYER_QUARANTINE", "layer": layer["file"],
                             "count": issues, "severity": "degraded",
                             "physical_envelope_accepted": False})
    downstream = [{"code": "ACTUAL_PLANNING_ZONE_PENDING", "owner": "M1 / explicit PlanningZone input"}]
    return {"scope": "required Carto DEM/Depth input contracts before M1; no G0 or 3D acceptance",
            "planning_ready": not blocking, "blocking_diagnostics": blocking,
            "optional_diagnostics": optional, "downstream_diagnostics": downstream,
            "source_assertions": asdict(assertions), "documented_source_facts": DOCUMENTED_CARTO_FACTS,
            "capabilities": {"raster_io": conditions["RASTER_EMPTY"],
                             "grid_partition": conditions["GRID_UNVERIFIED"],
                             "complete_land_water_semantics": conditions["LAND_WATER_UNKNOWN"] and
                                 conditions["DEPTH_COVERAGE_UNCONFIRMED"] and conditions["CARTO_SOURCE_UNCONFIRMED"],
                             "physical_network_envelope": False, "three_dimensional_feasibility": False},
            "future_functions": ["general reprojection", "water-face input", "water polygon export",
                                 "actual PlanningZone integration", "full network topology"]}


def review_geometry(geometry: BaseGeometry) -> dict[str, Any]:
    """Keep full make_valid output, including collapsed and mixed components."""
    require(not geometry.is_empty and not geometry.has_z and geometry.geom_type in ("Polygon", "MultiPolygon"),
            "UNSUPPORTED_REVIEW_GEOMETRY", "review requires a nonempty planar polygon footprint")
    original_wkb = to_wkb(geometry)
    candidate = geometry if geometry.is_valid else make_valid(geometry, method="linework", keep_collapsed=True)
    polygonal = candidate.geom_type in ("Polygon", "MultiPolygon")
    if candidate.is_empty:
        status = "EMPTY_REVIEW"
    elif not candidate.is_valid:
        status = "INVALID_REVIEW"
    elif polygonal:
        status = "POLYGON_CANDIDATE"
    elif candidate.geom_type == "GeometryCollection":
        status = "MIXED_DIMENSION_REVIEW"
    else:
        status = "COLLAPSED_REVIEW"
    candidate_wkb = to_wkb(candidate)
    return {"status": status, "source_type": geometry.geom_type, "candidate_type": candidate.geom_type,
            "source_valid": bool(geometry.is_valid), "candidate_valid": bool(candidate.is_valid),
            "source_algebraic_area_m2": float(geometry.area), "candidate_area_m2": float(candidate.area),
            "area_difference_m2": float(candidate.area - geometry.area),
            "area_difference_is_physical_error": False,
            "source_wkb_sha256": sha256(original_wkb).hexdigest(),
            "candidate_wkb_sha256": sha256(candidate_wkb).hexdigest(),
            "candidate_wkb_hex": candidate_wkb.hex(), "source_unchanged": to_wkb(geometry) == original_wkb,
            "review_required": not geometry.is_valid, "physical_envelope_accepted": False}


def review_boundary_layer(path: Path) -> dict[str, Any]:
    """Produce local WKB candidates for invalid features; never write to source."""
    candidates = []
    with fiona.open(path, "r") as source:
        for feature in source:
            if feature["geometry"] is None:
                continue
            geometry = shape(feature["geometry"])
            if geometry.is_valid and not geometry.is_empty:
                continue
            try:
                item = review_geometry(geometry)
            except AdapterError as exc:
                item = {"status": "UNSUPPORTED_REVIEW", "error": str(exc),
                        "physical_envelope_accepted": False, "review_required": True}
            item["source_record_id"] = str(feature.id)
            candidates.append(item)
    counts: dict[str, int] = {}
    for item in candidates:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"quality_version": QUALITY_VERSION, "source_file": path.name,
            "method": "Shapely make_valid(linework, keep_collapsed=True)",
            "candidate_count": len(candidates), "status_counts": counts,
            "candidates": candidates, "physical_envelope_accepted": False,
            "source_repaired_in_place": False,
            "note": "Local derived WKB candidates retain full geometry; semantic envelope review is still required"}
