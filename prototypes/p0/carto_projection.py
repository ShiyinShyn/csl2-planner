"""Compare reported Carto UI context and exported metric grids; no auto CRS repair."""
# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import math
from typing import Any, Mapping
from pyproj import Transformer

from .carto_adapter import metric_crs, require

PROJECTION_REVIEW_VERSION = "p0-02-projection-0.1.0"


def audit_projection_context(terrain: Mapping[str, Any], depth: Mapping[str, Any],
                             vectors: list[Mapping[str, Any]], settings: Mapping[str, Any],
                             *, origin_tolerance_m: float = 0.001) -> dict[str, Any]:
    """A raster centre is an origin candidate, never an authenticated game origin."""
    require(isinstance(settings, Mapping) and settings.get("projection") == "WGS84",
            "UNSUPPORTED_SETTINGS", "this checkpoint only audits explicitly reported WGS84 defaults")
    require(all(type(settings.get(key)) in (int, float) and math.isfinite(settings[key])
                for key in ("x", "y")), "INVALID_SETTINGS", "finite numeric X/Y required")
    require(settings["x"] == 0 and settings["y"] == 0,
            "UNSUPPORTED_SETTINGS", "nonzero settings need their own source mapping validation")
    require(isinstance(settings.get("scope"), str) and bool(settings["scope"].strip()) and
            isinstance(settings.get("evidence"), str) and bool(settings["evidence"].strip()),
            "MISSING_SETTINGS_EVIDENCE", "record the UI report and its historical scope")
    require(math.isfinite(origin_tolerance_m) and origin_tolerance_m > 0,
            "INVALID_TOLERANCE", "explicit positive metre tolerance required")
    crs = metric_crs(terrain.get("crs"))
    require(metric_crs(depth.get("crs")) == crs, "CRS_MISMATCH", "source grids must share explicit CRS")
    vector_matches = all(metric_crs(item.get("crs")) == crs for item in vectors)
    rows, columns = terrain["shape"]
    a, b, c, d, e, f = terrain["transform"]
    require(rows > 0 and columns > 0 and all(math.isfinite(v) for v in (a, b, c, d, e, f)),
            "INVALID_GRID", "positive grid and finite affine required")
    centre = (a * columns / 2 + b * rows / 2 + c,
              d * columns / 2 + e * rows / 2 + f)
    zero_reference = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(0, 0, errcheck=True)
    lon_lat = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(*centre, errcheck=True)
    require(all(math.isfinite(v) for v in (*centre, *zero_reference, *lon_lat)),
            "INVALID_TRANSFORM_RESULT", "nonfinite projection result")
    residual = math.hypot(centre[0] - zero_reference[0], centre[1] - zero_reference[1])
    return {"review_version": PROJECTION_REVIEW_VERSION, "reported_settings": dict(settings),
            "documented_zero_wgs84_meaning": "projection disabled in current author manual",
            "file_crs": crs.to_string(), "all_vector_crs_match": vector_matches,
            "grid_centre_projected_m": list(centre), "grid_centre_wgs84_degrees": list(lon_lat),
            "zero_wgs84_reference_projected_m": list(zero_reference),
            "origin_reference_residual_m": residual, "origin_tolerance_m": origin_tolerance_m,
            "origin_candidate_matches_zero_reference": residual <= origin_tolerance_m,
            "game_mapping_confirmed": False,
            "historical_settings_confirmed": False, "game_axis_orientation_confirmed": False,
            "scope": "exported-grid reference consistency; no game control points or CRS relabelling",
            "diagnostics": ["CURRENT_UI_DEFAULTS_ARE_NOT_HISTORICAL_EXPORT_PROOF",
                            "ZERO_WGS84_UI_AND_UTM_FILE_REQUIRE_EXPORT_PATH_RECONCILIATION",
                            "GRID_CENTRE_MATCH_IS_AN_ORIGIN_CANDIDATE_ONLY"],
            "source_facts": {"wiki": "https://github.com/taipei-native/Carto/wiki",
                             "options": "https://github.com/taipei-native/Carto/blob/main/IO/Options.cs",
                             "options_note": "GetTMCoord/GetTMProjection can select an internal UTM origin for WGS84 inputs",
                             "checked_date": "2026-10-07", "sample_export_version": "unknown"}}
