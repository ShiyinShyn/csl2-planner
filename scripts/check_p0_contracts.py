# Run: conda run -n csl2-planner --no-capture-output python -s scripts/check_p0_contracts.py
# Expected: [SUCCESS] P0 contract checks passed (N cases); no downloads or game access.
# SPDX-License-Identifier: GPL-3.0-only
"""Synthetic P0 contracts, not a model/performance/3D-routing test."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

if importlib.util.find_spec("shapely") is None:
    print("[ENVIRONMENT REQUIRED] Use the csl2-planner environment described in ENVIRONMENT.md.")
    raise SystemExit(2)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shapely.geometry import LineString, Polygon, box
from prototypes.p0.contracts import (
    ContractError, Corridor, DefaultCatalog, ExistingObject, LandUse, Mode, PlanningZone,
    PolicyStatus, ScenarioValues, Snapshot, TunnelRules, change_land_use,
    decide_conflicts, resolve_scenario, zone_from_mapping, zone_to_mapping,
)


def values(rate: float) -> ScenarioValues:
    return ScenarioValues((("synthetic_trip_rate", rate),))


def zone(*, use: LandUse = LandUse.RESIDENTIAL, demolish: bool = False,
         zone_id: str = "z1", geometry: Polygon | None = None,
         overrides: ScenarioValues | None = None) -> PlanningZone:
    return PlanningZone(zone_id, box(0, 0, 100, 100) if geometry is None else geometry,
                        use, (120.0, 180.0), demolish, overrides)


def corridor(mode: Mode = Mode.SURFACE, **updates: object) -> Corridor:
    base = Corridor("road-1", "synthetic-local-metres", LineString([(0, 50), (100, 50)]),
                    2.0, mode, frozenset(Mode), nominal_z_m=100.0)
    return replace(base, **updates)


def obj(**updates: object) -> ExistingObject:
    return replace(ExistingObject("b1", box(40, 40, 60, 60), 100.0, 95.0), **updates)


def snapshot(*, zones: tuple[PlanningZone, ...] | None = None,
             objects: tuple[ExistingObject, ...] | None = None, **updates: object) -> Snapshot:
    base = Snapshot("r1", "synthetic-local-metres", (zone(),) if zones is None else zones,
                    (obj(),) if objects is None else objects, True)
    return replace(base, **updates)


RULES = TunnelRules("synthetic-rules-v1", 0.0, 3.0, 2.0, 4.0)


class ZoneContracts(unittest.TestCase):
    def setUp(self) -> None:
        # Synthetic values, not proposed game defaults.
        self.defaults = DefaultCatalog("synthetic-defaults-v1", tuple((use, values(1.0)) for use in LandUse))

    def test_one_zone_identity_round_trip(self) -> None:
        payload = zone_to_mapping(zone())
        self.assertEqual(zone_from_mapping(payload).zone_id, "z1")
        self.assertEqual(set(k for k in payload if k.endswith('_id')), {"zone_id"})
        self.assertTrue(zone_from_mapping(payload).geometry.equals(zone().geometry))

    def test_default_permission_is_false_and_immutable(self) -> None:
        original = zone()
        self.assertFalse(original.allow_demolition)
        with self.assertRaises(FrozenInstanceError):
            original.allow_demolition = True

    def test_invalid_polygon_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            zone(geometry=Polygon([(0, 0), (1, 1), (0, 1), (1, 0), (0, 0)]))

    def test_3d_zone_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            zone(geometry=Polygon([(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 0, 1)]))

    def test_string_permission_is_rejected(self) -> None:
        payload = zone_to_mapping(zone())
        payload['allow_demolition'] = 'false'
        with self.assertRaises(ContractError):
            zone_from_mapping(payload)

    def test_nonpositive_block_size_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            replace(zone(), block_size_m=(0.0, 20.0))

    def test_residential_override_is_allowed(self) -> None:
        self.assertEqual(dict(resolve_scenario(zone(overrides=values(2.0)), self.defaults).entries),
                         {'synthetic_trip_rate': 2.0})

    def test_mixed_override_is_allowed(self) -> None:
        self.assertEqual(resolve_scenario(zone(use=LandUse.MIXED, overrides=values(2.0)), self.defaults), values(2.0))

    def test_all_nonresidential_overrides_are_forbidden(self) -> None:
        for use in set(LandUse) - {LandUse.RESIDENTIAL, LandUse.MIXED}:
            with self.subTest(use=use), self.assertRaises(ContractError) as caught:
                zone(use=use, overrides=values(2.0))
            self.assertEqual(caught.exception.code, 'SCENARIO_LOCKED')

    def test_nonresidential_user_defaults_are_forbidden(self) -> None:
        with self.assertRaises(ContractError):
            DefaultCatalog('user-v1', ((LandUse.COMMERCIAL, values(2.0)),), user_defaults=True)

    def test_default_then_user_then_zone_precedence(self) -> None:
        user = DefaultCatalog('user-v1', ((LandUse.RESIDENTIAL, values(2.0)),), user_defaults=True)
        self.assertEqual(resolve_scenario(zone(), self.defaults, user), values(2.0))
        self.assertEqual(resolve_scenario(zone(overrides=values(3.0)), self.defaults, user), values(3.0))
        self.assertEqual(resolve_scenario(zone(use=LandUse.COMMERCIAL), self.defaults, user), values(1.0))

    def test_unknown_parameter_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            resolve_scenario(zone(overrides=ScenarioValues((('unknown', 1.0),))), self.defaults)

    def test_nonfinite_or_boolean_parameter_is_rejected(self) -> None:
        for value in (float('nan'), float('inf'), -1.0, True):
            with self.subTest(value=value), self.assertRaises(ContractError):
                values(value)

    def test_duplicate_parameter_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            ScenarioValues((('x', 1.0), ('x', 2.0)))

    def test_illegal_import_cannot_grant_editability(self) -> None:
        payload = zone_to_mapping(zone(use=LandUse.INDUSTRIAL))
        payload.update(scenario_overrides={'synthetic_trip_rate': 9.0}, editable=True)
        with self.assertRaises(ContractError):
            zone_from_mapping(payload)
        del payload['editable']
        with self.assertRaises(ContractError) as caught:
            zone_from_mapping(payload)
        self.assertEqual(caught.exception.code, 'SCENARIO_LOCKED')

    def test_use_change_clears_residential_override(self) -> None:
        updated = change_land_use(zone(overrides=values(3.0)), LandUse.COMMERCIAL)
        self.assertIsNone(updated.scenario_overrides)
        self.assertEqual(resolve_scenario(updated, self.defaults), values(1.0))

    def test_nonresidential_geometry_and_demolition_remain_editable(self) -> None:
        original = zone(use=LandUse.INDUSTRIAL)
        updated = replace(original, block_size_m=(250.0, 350.0), allow_demolition=True)
        self.assertTrue(updated.allow_demolition)
        self.assertEqual(original.block_size_m, (120.0, 180.0))

    def test_duplicate_zones_are_rejected(self) -> None:
        with self.assertRaises(ContractError):
            snapshot(zones=(zone(), zone()))

    def test_overlapping_zones_are_rejected(self) -> None:
        with self.assertRaises(ContractError):
            snapshot(zones=(zone(), zone(zone_id='z2', geometry=box(50, 0, 150, 100))))


class ConflictContracts(unittest.TestCase):
    def decide(self, road: Corridor | None = None, data: Snapshot | None = None,
               rules: TunnelRules = RULES, expected: str = 'r1'):
        return decide_conflicts(corridor() if road is None else road,
                                snapshot() if data is None else data, rules, expected_revision=expected)

    def assert_tunnel_only(self, decision) -> None:
        self.assertEqual(decision.permitted_modes, frozenset({Mode.TUNNEL}))
        self.assertTrue(decision.lower_only)

    def test_clear_policy_is_not_full_geometry_pass(self) -> None:
        decision = self.decide(data=snapshot(objects=()))
        self.assertEqual(decision.status, PolicyStatus.POLICY_CLEAR)
        self.assertEqual(decision.permitted_modes, frozenset(Mode))

    def test_surface_conflict_requires_tunnel(self) -> None:
        decision = self.decide()
        self.assertEqual(decision.status, PolicyStatus.TUNNEL_REQUIRED)
        self.assert_tunnel_only(decision)

    def test_over_roof_elevated_route_still_requires_tunnel(self) -> None:
        self.assert_tunnel_only(self.decide(corridor(Mode.ELEVATED, nominal_z_m=500.0)))

    def test_existing_tunnel_request_remains_tunnel_only(self) -> None:
        self.assert_tunnel_only(self.decide(corridor(Mode.TUNNEL)))

    def test_allowed_demolition_proposes_but_does_not_delete(self) -> None:
        data = snapshot(zones=(zone(demolish=True),))
        decision = self.decide(data=data)
        self.assertEqual(decision.status, PolicyStatus.POLICY_CLEAR)
        self.assertEqual(decision.demolition_candidate_ids, ('b1',))
        self.assertEqual(len(data.objects), 1)

    def test_cross_zone_uses_most_restrictive_permission(self) -> None:
        zones = (zone(demolish=True, geometry=box(0, 0, 50, 100)),
                 zone(zone_id='z2', geometry=box(50, 0, 100, 100)))
        self.assert_tunnel_only(self.decide(data=snapshot(zones=zones)))

    def test_all_cross_zone_permissions_must_allow_demolition(self) -> None:
        zones = (zone(demolish=True, geometry=box(0, 0, 50, 100)),
                 zone(demolish=True, zone_id='z2', geometry=box(50, 0, 100, 100)))
        self.assertEqual(self.decide(data=snapshot(zones=zones)).demolition_candidate_ids, ('b1',))

    def test_unmatched_building_does_not_gain_permission(self) -> None:
        decision = self.decide(data=snapshot(zones=()))
        self.assertEqual(decision.status, PolicyStatus.DATA_INSUFFICIENT)
        self.assertEqual(decision.unresolved_association_ids, ('b1',))
        self.assert_tunnel_only(decision)

    def test_partially_covered_building_is_not_demolishable(self) -> None:
        decision = self.decide(data=snapshot(zones=(zone(demolish=True, geometry=box(0, 0, 50, 100)),)))
        self.assertEqual(decision.status, PolicyStatus.DATA_INSUFFICIENT)
        self.assert_tunnel_only(decision)

    def test_missing_foundation_is_data_insufficient(self) -> None:
        decision = self.decide(data=snapshot(objects=(obj(foundation_bottom_z_m=None),)))
        self.assertEqual(decision.status, PolicyStatus.DATA_INSUFFICIENT)
        self.assert_tunnel_only(decision)

    def test_non_tunnel_asset_has_no_legal_fallback(self) -> None:
        decision = self.decide(corridor(asset_modes=frozenset({Mode.SURFACE, Mode.ELEVATED})))
        self.assertEqual(decision.status, PolicyStatus.NO_FEASIBLE_SOLUTION)
        self.assertFalse(decision.permitted_modes)

    def test_tunnel_centre_bound_uses_outer_height(self) -> None:
        self.assertEqual(self.decide().centre_z_upper_bounds_m, (('b1', 91.0),))

    def test_empty_depth_range_has_no_legal_fallback(self) -> None:
        decision = self.decide(corridor(minimum_z_m=95.0))
        self.assertEqual(decision.status, PolicyStatus.NO_FEASIBLE_SOLUTION)
        self.assertFalse(decision.permitted_modes)

    def test_exact_depth_boundary_does_not_claim_full_feasibility(self) -> None:
        self.assertEqual(self.decide(corridor(minimum_z_m=91.0)).status, PolicyStatus.TUNNEL_REQUIRED)

    def test_asset_envelope_not_just_centre_line_is_checked(self) -> None:
        self.assert_tunnel_only(self.decide(data=snapshot(objects=(obj(footprint=box(40, 51, 60, 60)),))))

    def test_object_control_buffer_is_checked(self) -> None:
        data = snapshot(objects=(obj(footprint=box(40, 53, 60, 60)),))
        self.assertEqual(self.decide(data=data).status, PolicyStatus.POLICY_CLEAR)
        self.assert_tunnel_only(self.decide(data=data, rules=replace(RULES, control_buffer_m=2.0)))

    def test_tunnel_has_no_hard_protection_exemption(self) -> None:
        decision = self.decide(corridor(Mode.TUNNEL), snapshot(protected_areas=(box(40, 40, 60, 60),)))
        self.assertEqual(decision.reason, 'HARD_PROTECTION_CONFLICT')
        self.assertFalse(decision.permitted_modes)

    def test_missing_existing_layer_is_not_proof_of_no_obstacles(self) -> None:
        self.assertEqual(self.decide(data=snapshot(objects=(), existing_data_complete=False)).status,
                         PolicyStatus.DATA_INSUFFICIENT)

    def test_old_revision_is_not_publishable(self) -> None:
        decision = self.decide(expected='r0')
        self.assertEqual(decision.status, PolicyStatus.STALE_INPUT)
        self.assertFalse(decision.permitted_modes)

    def test_coordinate_space_mismatch_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            self.decide(corridor(space_id='different-space'))

    def test_updated_permissions_require_new_snapshot(self) -> None:
        data = snapshot(zones=(zone(demolish=True),), revision='r2')
        self.assertEqual(self.decide(data=data).status, PolicyStatus.STALE_INPUT)
        self.assertEqual(self.decide(data=data, expected='r2').demolition_candidate_ids, ('b1',))

    def test_invalid_foundation_order_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            obj(foundation_bottom_z_m=101.0)

    def test_zero_asset_width_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            corridor(envelope_radius_m=0)

    def test_duplicate_existing_ids_are_rejected(self) -> None:
        with self.assertRaises(ContractError):
            snapshot(objects=(obj(), obj()))


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TestResult()
    suite.run(result)
    for test, error in result.failures + result.errors:
        last_line = error.strip().splitlines()[-1]
        print(f"[FAIL] {test.id()}: {last_line[:220]}")
    if result.skipped:
        print(f"[SKIPPED] {len(result.skipped)} cases")
    if result.wasSuccessful() and not result.skipped:
        print(f"[SUCCESS] P0 contract checks passed ({result.testsRun} cases).")
        print("[SCOPE] Synthetic zone/parameter/conflict contracts only; no 3D routing, GUI, or performance validation.")
        return 0
    print(f"[FAILED] {len(result.failures)} failures, {len(result.errors)} errors in {result.testsRun} cases.")
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
