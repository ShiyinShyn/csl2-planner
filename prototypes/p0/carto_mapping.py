"""Source-derived preview mapping; historical game mapping remains unverified."""
# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping
from pathlib import Path

import fiona
from .carto_adapter import metric_crs, require

MAPPING_VERSION = "p0-02-mapping-preview-0.2.0"


def coordinate(values: tuple[float, ...], size: int) -> None:
    require(isinstance(values, tuple) and len(values) == size and
            all(type(v) in (int, float) and math.isfinite(v) for v in values),
            "INVALID_COORDINATE", "finite numeric coordinate tuple required")


@dataclass(frozen=True)
class GameMappingPreview:
    """x/z -> source easting/northing; y -> height. This is a guarded hypothesis."""
    origin_m: tuple[float, float]
    half_extent_m: tuple[float, float]
    crs: str
    evidence: str

    def __post_init__(self) -> None:
        coordinate(self.origin_m, 2)
        coordinate(self.half_extent_m, 2)
        metric_crs(self.crs)
        require(all(v > 0 for v in self.half_extent_m), "INVALID_EXTENT", "positive source extent required")
        require(isinstance(self.evidence, str) and bool(self.evidence.strip()),
                "MISSING_MAPPING_EVIDENCE", "source-derived mapping evidence required")

    def forward(self, game_xyz_m: tuple[float, float, float], *, for_export: bool = False) -> tuple[float, float, float]:
        require(type(for_export) is bool, "INVALID_EXPORT_FLAG", "actual boolean required")
        require(not for_export, "UNCONFIRMED_GAME_MAPPING", "preview cannot certify game placement/export")
        coordinate(game_xyz_m, 3)
        x, height, z = game_xyz_m
        require(abs(x) <= self.half_extent_m[0] and abs(z) <= self.half_extent_m[1],
                "OUTSIDE_SOURCE_COVERAGE", "do not extrapolate a provisional mapping")
        return self.origin_m[0] + x, self.origin_m[1] + z, height

    def inverse(self, source_xyh_m: tuple[float, float, float], *, for_export: bool = False) -> tuple[float, float, float]:
        require(type(for_export) is bool, "INVALID_EXPORT_FLAG", "actual boolean required")
        require(not for_export, "UNCONFIRMED_GAME_MAPPING", "preview cannot certify game placement/export")
        coordinate(source_xyh_m, 3)
        east, north, height = source_xyh_m
        x, z = east - self.origin_m[0], north - self.origin_m[1]
        require(abs(x) <= self.half_extent_m[0] + 1e-9 and abs(z) <= self.half_extent_m[1] + 1e-9,
                "OUTSIDE_SOURCE_COVERAGE", "source point is outside preview footprint")
        return x, height, z


def preview_from_grid(terrain: Mapping[str, Any], depth: Mapping[str, Any]) -> GameMappingPreview:
    crs = metric_crs(terrain.get("crs"))
    require(metric_crs(depth.get("crs")) == crs, "CRS_MISMATCH", "matched explicit source CRSs required")
    centres = []
    extents = []
    for item in (terrain, depth):
        rows, columns = item["shape"]
        a, b, c, d, e, f = item["transform"]
        require(rows > 0 and columns > 0 and all(math.isfinite(v) for v in (a, b, c, d, e, f)),
                "INVALID_GRID", "positive grid and finite affine required")
        require(a > 0 and e < 0 and b == 0 and d == 0,
                "UNSUPPORTED_GRID_ORIENTATION", "preview only covers north-up, unrotated source grids")
        centres.append((c + a * columns / 2, f + e * rows / 2))
        extents.append((a * columns / 2, -e * rows / 2))
    require(all(math.isclose(centres[0][i], centres[1][i], abs_tol=1e-6, rel_tol=0) and
                math.isclose(extents[0][i], extents[1][i], abs_tol=1e-6, rel_tol=0) for i in (0, 1)),
            "SOURCE_FOOTPRINT_MISMATCH", "DEM/Depth must have the same centre and footprint")
    return GameMappingPreview(centres[0], extents[0], crs.to_string(),
        "Author AreaSystem.CollectBoundariesJob uses center.Shift(m_Position.xzy), "
        "Coord.Shift preserves x/y/z addition, then Transform.Apply and Shapefile XY/Z writing; "
        "RasterSystem reverses source rows and uses resolution.x/z. Main source checked 2026-10-07; "
        "actual sample export version/real game control points unknown; grid centre is a provisional origin.")


def audit_mapping_roundtrip(terrain: Mapping[str, Any], depth: Mapping[str, Any]) -> dict[str, Any]:
    preview = preview_from_grid(terrain, depth)
    half_x, half_z = preview.half_extent_m
    points = [(x, height, z) for height in (-10.0, 0.0, 100.0)
              for x in (-half_x, 0.0, half_x) for z in (-half_z, 0.0, half_z)]
    maximum = 0.0
    for point in points:
        restored = preview.inverse(preview.forward(point))
        maximum = max(maximum, max(abs(a-b) for a, b in zip(point, restored)))
    rows, columns = terrain["shape"]
    a, _, c, _, e, f = terrain["transform"]
    centres = [(c + a * (col + 0.5), f + e * (row + 0.5), 0.0)
               for row in (0, rows - 1) for col in (0, columns - 1)]
    recovered_game = [preview.inverse(point) for point in centres]
    return {"mapping_version": MAPPING_VERSION, "evidence_level": "source-derived preview",
            "formula": {"forward": "E=E0+game_x; N=N0+game_z; H=game_y",
                        "inverse": "game_x=E-E0; game_y=H; game_z=N-N0"},
            "source_crs": preview.crs, "origin_m": list(preview.origin_m),
            "half_extent_m": list(preview.half_extent_m), "evidence": preview.evidence,
            "roundtrip_points": len(points), "maximum_roundtrip_error_m": maximum,
            "roundtrip_tolerance_m": 1e-8, "numerical_roundtrip_passed": maximum <= 1e-8,
            "corner_pixel_centres_game_preview": [list(point) for point in recovered_game],
            "raster_row_game_z_direction": "decreases as output row increases",
            "raster_column_game_x_direction": "increases as output column increases",
            "height_independent_of_horizontal_translation": True,
            "game_mapping_confirmed": False, "real_game_control_points_verified": False,
            "historical_export_version": "unknown", "allow_game_placement_export": False,
            "scope": "same projected input CRS; no implicit CRS transformation or source repair",
            "sources": ["https://github.com/taipei-native/Carto/blob/main/Systems/AreaSystem.cs",
                        "https://github.com/taipei-native/Carto/blob/main/Geodata/Coord.cs",
                        "https://github.com/taipei-native/Carto/blob/main/Systems/RasterSystem.cs",
                        "https://github.com/taipei-native/Carto/blob/main/IO/Shapefile.cs",
                        "https://github.com/taipei-native/Carto/blob/main/IO/Options.cs"]}


def audit_map_tile_preview(path: Path, preview: GameMappingPreview) -> dict[str, Any]:
    """Map tiles are dataset consistency checks, not independent game control points."""
    records, maximum, outside = 0, 0.0, 0
    with fiona.open(path, "r") as source:
        require(metric_crs(source.crs_wkt) == metric_crs(preview.crs), "CRS_MISMATCH", "tile CRS must match preview")
        for feature in source:
            if str(feature["properties"].get("Object")) != "MapTile" or feature["geometry"] is None:
                continue
            records += 1
            coordinates = feature["geometry"]["coordinates"]
            rings = coordinates if feature["geometry"]["type"] == "Polygon" else [ring for polygon in coordinates for ring in polygon]
            for ring in rings:
                for value in ring:
                    point = (float(value[0]), float(value[1]), 0.0)
                    try:
                        game = preview.inverse(point)
                        restored = preview.forward(game)
                    except ValueError as exc:
                        if getattr(exc, "code", None) == "OUTSIDE_SOURCE_COVERAGE":
                            outside += 1
                            continue
                        raise
                    maximum = max(maximum, abs(restored[0]-point[0]), abs(restored[1]-point[1]))
    return {"source_file": path.name, "map_tile_records": records,
            "vertices_outside_preview_coverage": outside, "maximum_roundtrip_error_m": maximum,
            "evidence_scope": "roundtrip consistency within exported MapTile geometry",
            "independent_game_control_points": False}


# Fixed publisher source identity; this mapping supports only the confirmed version.
PINNED_COMMIT = "8ab791a6fa1c4c45f887c66fcb576056228017b8"
PINNED_MANAGED_DLL_SHA256 = "243df799b5d5f879e7f44714e6fd15cce9da7cb9fa2aec58e16343edfa0610d2"
PINNED_SOURCE_HASHES = {
    "Systems/AreaSystem.cs": "aee9d875a8859b28bf03a95de7a82e6f601ee1621342dd1cb7abcccab8ba5f84",
    "IO/Options.cs": "c8fb682870e6075d3ecc2984a2061976c69dd71b52e74162dcc1f456a685c9b9",
    "Geodata/Coord.cs": "2e3253417ad9ae28d1d68aaf170f5f5f2dcceb5e6e7117a91b1b34d53c3b9a97",
    "IO/GeoTiff.cs": "f1e86af33dc133050df50b7af75b4fd8844156b2ef254b38ed5dd08aebde289f",
    "Systems/RasterSystem.cs": "6340af1c26629f702ca0983706fe33400e8305d21d39f727454a20bda446723b",
    "IO/TagManager.cs": "0a108d2e2bfe0e22562deecf57899942c388467e7b1370c444a098352e2bb26c"
}


def source_manifest_digest(source_hashes: Mapping[str, str]) -> str:
    from hashlib import sha256
    import json
    require(isinstance(source_hashes, Mapping) and bool(source_hashes), "INVALID_SOURCE_MANIFEST", "source snapshot required")
    normalized = {}
    for source, digest in source_hashes.items():
        require(isinstance(source, str) and isinstance(digest, str), "INVALID_SOURCE_MANIFEST", "text source/hash required")
        source = source.replace("\\", "/")
        require(not source.startswith("/") and ":" not in source and ".." not in source.split("/"),
                "INVALID_SOURCE_MANIFEST", "source paths must remain relative")
        require(source not in normalized and len(digest) == 64 and all(c in "0123456789abcdefABCDEF" for c in digest),
                "INVALID_SOURCE_MANIFEST", "unique paths and SHA-256 required")
        normalized[source] = digest.lower()
    data = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return sha256(data).hexdigest()


def read_native_grid_reference(path: Path) -> dict[str, list[float]]:
    """Read only classic TIFF 33550/33922 double arrays; no source writes."""
    import struct
    tags = {}
    with path.open("rb") as stream:
        header = stream.read(8)
        require(len(header) == 8 and header[:2] in (b"II", b"MM"), "INVALID_TIFF_REFERENCE", "classic TIFF header required")
        endian = "<" if header[:2] == b"II" else ">"
        require(struct.unpack(endian + "H", header[2:4])[0] == 42, "UNSUPPORTED_TIFF_REFERENCE", "BigTIFF/reference variants need separate validation")
        size = path.stat().st_size
        offset = struct.unpack(endian + "I", header[4:])[0]
        require(8 <= offset <= size - 2, "INVALID_TIFF_REFERENCE", "IFD offset outside file")
        stream.seek(offset)
        count_bytes = stream.read(2)
        count = struct.unpack(endian + "H", count_bytes)[0]
        require(count <= 16384 and offset + 2 + count * 12 <= size, "INVALID_TIFF_REFERENCE", "truncated/oversized IFD")
        entries = [stream.read(12) for _ in range(count)]
        for entry in entries:
            tag, kind, length, data_offset = struct.unpack(endian + "HHII", entry)
            if tag not in (33550, 33922):
                continue
            required_length = 3 if tag == 33550 else 6
            require(str(tag) not in tags and kind == 12 and length == required_length and
                    data_offset + 8 * length <= size, "INVALID_TIFF_REFERENCE", "unique expected double-array tag required")
            stream.seek(data_offset)
            values = list(struct.unpack(endian + "d" * length, stream.read(8 * length)))
            require(all(math.isfinite(v) for v in values), "INVALID_TIFF_REFERENCE", "finite reference values required")
            tags[str(tag)] = values
    require(set(tags) == {"33550", "33922"}, "MISSING_TIFF_REFERENCE", "pixel scale and centre tiepoint required")
    return tags


def verify_version_bound_mapping(actual: Mapping[str, Any], declaration: Mapping[str, Any],
                                 source_dir: Path, sample_dir: Path) -> dict[str, Any]:
    from hashlib import sha256
    from .carto_adapter import file_hash
    require(isinstance(declaration, Mapping), "INVALID_VERSION_DECLARATION", "version declaration required")
    require(declaration.get("export_matches_installation_confirmed") is True and
            isinstance(declaration.get("evidence"), str) and bool(declaration["evidence"].strip()),
            "EXPORT_VERSION_UNCONFIRMED", "explicit historical export confirmation with evidence required")
    require(declaration.get("carto_version") == "1.0.17" and declaration.get("commit") == PINNED_COMMIT,
            "UNSUPPORTED_EXPORT_VERSION", "this source contract covers only the fixed 1.0.17 commit")
    require(str(declaration.get("managed_dll_sha256", "")).lower() == PINNED_MANAGED_DLL_SHA256,
            "INSTALLATION_IDENTITY_MISMATCH", "installed managed DLL identity changed")
    snapshot = source_manifest_digest(actual["source_hashes"])
    require(declaration.get("source_manifest_sha256") == snapshot, "STALE_VERSION_DECLARATION", "confirmation does not match this snapshot")
    verified_sources = {}
    for relative, expected in PINNED_SOURCE_HASHES.items():
        path = source_dir / relative.replace("/", "__")
        require(path.is_file(), "PINNED_SOURCE_MISSING", relative)
        observed = sha256(path.read_bytes()).hexdigest()
        require(observed == expected, "PINNED_SOURCE_CHANGED", relative)
        verified_sources[relative] = observed
    preview = preview_from_grid(actual["terrain"], actual["depth"])
    projection = actual["projection_audit"]
    require(preview.crs == "EPSG:32631" and projection["origin_candidate_matches_zero_reference"] is True and
            projection["all_vector_crs_match"] is True, "UNSUPPORTED_MAPPING_CONTEXT", "current 0/0 UTM31 sample contract only")
    native_references = {}
    root = sample_dir.resolve()
    for role, suffix in (("terrain", "_Elevation.tif"), ("depth", "_Depth.tif")):
        metadata = actual[role]
        require(metadata["dtype"] == "float32", "UNSUPPORTED_NATIVE_FORMAT", "this unit/mapping contract requires original Float32")
        candidates = [p for p in actual["source_hashes"] if p.replace("\\", "/").endswith(suffix)]
        require(len(candidates) == 1, "AMBIGUOUS_MAPPING_SOURCE", role)
        source = (root / candidates[0].replace("\\", "/")).resolve()
        require(root in source.parents, "INVALID_MAPPING_SOURCE", "source must remain in declared snapshot")
        reference = read_native_grid_reference(source)
        require(file_hash(source).lower() == actual["source_hashes"][candidates[0]].lower(), "SOURCE_CHANGED", "source changed during reference verification")
        rows, columns = metadata["shape"]
        a, _, _, _, e, _ = metadata["transform"]
        expected_tie = [columns / 2, rows / 2, 0, preview.origin_m[0], preview.origin_m[1], 0]
        expected_scale = [a, -e, 1]
        require(all(math.isclose(a, b, abs_tol=1e-6, rel_tol=0) for a, b in zip(reference["33922"], expected_tie)) and
                all(math.isclose(a, b, abs_tol=1e-6, rel_tol=0) for a, b in zip(reference["33550"], expected_scale)),
                "NATIVE_REFERENCE_MISMATCH", "native centre/scale differs from versioned exporter contract")
        native_references[role] = reference
    numerical = audit_mapping_roundtrip(actual["terrain"], actual["depth"])
    require(numerical["numerical_roundtrip_passed"], "MAPPING_ROUNDTRIP_FAILED", "mapping calculation did not pass")
    return {"carto_version": "1.0.17", "commit": PINNED_COMMIT, "source_manifest_sha256": snapshot,
            "export_version_identity_source": "explicit user confirmation plus installed DLL product version",
            "verified_source_hashes": verified_sources, "native_grid_references": native_references,
            "game_mapping_confirmed": True, "verification_basis": "version-bound exporter/source-grid contract",
            "formula": numerical["formula"], "origin_m": list(preview.origin_m),
            "half_extent_m": list(preview.half_extent_m), "roundtrip_points": numerical["roundtrip_points"],
            "maximum_roundtrip_error_m": numerical["maximum_roundtrip_error_m"],
            "real_game_control_points_verified": False, "game_runtime_calibration_performed": False,
            "allow_game_placement_export": False,
            "scope": "this unchanged 1.0.17 Float32 UTM31 snapshot; required input contract only",
            "limitations": ["no live game calibration or physical envelope acceptance",
                            "preview API still blocks placement export", "other versions/grids require separate validation"]}
