"""容量引擎的纯规则测试：时间片、叠放、承重、相邻禁忌、消防通道。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from capacity import engine
from capacity.model import (
    ADJACENCY,
    AREA_CONFLICT,
    FLOOR_LOAD,
    HEIGHT_LIMIT,
    LEVEL_LOAD,
    STACK_RULE,
)

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 24, 8, tzinfo=TZ)
T1 = datetime(2026, 9, 24, 20, tzinfo=TZ)


def ground(space_id="s1", owner=None, kind="temp_area", **kw):
    sp = {"space_id": space_id, "kind": kind, "owner_id": owner,
          "x": 0.0, "y": 0.0, "w": 10.0, "d": 10.0,
          "net_height": 4.0, "floor_load": 500.0, "level_load": 0.0,
          "status": "open"}
    sp.update(kw)
    return sp


def placement(hold_id="h", *, space_id="s1", tenant="t1", cargo="c1",
              x=0.0, y=0.0, level=0, start=T0, leave=T1,
              length=1.0, width=1.0, height=1.0, weight=50.0,
              category="fruit", stackable=False, max_layers=1, bear_load=0.0):
    return engine.Placement(hold_id, space_id, tenant, cargo, x, y, level, start, leave,
                            length, width, height, weight, category, stackable,
                            max_layers, bear_load)


def evaluate(candidate, spaces=None, others=(), taboos=(), overrides=()):
    spaces = spaces or {"s1": ground()}
    taboo_map = {(a, b): g for a, b, g in taboos}
    return engine.evaluate(candidate, spaces, list(others), taboo_map,
                           grant_overrides=overrides)


class GeometryBasics(unittest.TestCase):
    def test_clean_placement_passes(self):
        self.assertEqual(evaluate(placement()), [])

    def test_outside_space_rejected(self):
        self.assertIn("GEOMETRY", evaluate(placement(x=9.5, length=1.0)))

    def test_bad_interval_rejected(self):
        self.assertEqual(
            evaluate(placement(start=T1, leave=T0)), ["BAD_INTERVAL"])

    def test_other_tenant_fixed_stall(self):
        sp = ground(kind="fixed_stall", owner="owner-x")
        self.assertEqual(evaluate(placement(tenant="intruder"), {"s1": sp}),
                         ["UNAUTHORIZED_SPACE"])

    def test_closed_area(self):
        sp = ground(status="closed",
                    closed_from=datetime(2026, 9, 24, 6, tzinfo=TZ),
                    closed_until=datetime(2026, 9, 24, 18, tzinfo=TZ))
        self.assertEqual(evaluate(placement(), {"s1": sp}), ["AREA_CLOSED"])

    def test_fire_lane_overlap(self):
        spaces = {"s1": ground(), "lane": ground("lane", kind="fire_lane",
                                                 x=0, y=0, w=2, d=2, floor_load=0)}
        p = placement(x=1.5, y=1.5)
        self.assertEqual(evaluate(p, spaces), ["FIRE_LANE_BLOCKED"])
        # 紧急放行可覆盖
        self.assertEqual(evaluate(p, spaces, overrides=("fire_lane",)), [])


class TimeSliceTests(unittest.TestCase):
    def test_same_spot_disjoint_windows_both_fit(self):
        a = placement("a", start=T0, leave=T0 + timedelta(hours=2))
        b = placement("b", start=T0 + timedelta(hours=2), leave=T1)
        self.assertEqual(evaluate(b, others=[a]), [])

    def test_same_spot_overlapping_windows_conflicts(self):
        a = placement("a", cargo="c1", start=T0, leave=T0 + timedelta(hours=3))
        b = placement("b", cargo="c2", start=T0 + timedelta(hours=2), leave=T1)
        self.assertEqual(evaluate(b, others=[a]), [AREA_CONFLICT])

    def test_touching_edges_do_not_conflict(self):
        a = placement("a", cargo="c1", x=0)
        b = placement("b", cargo="c2", x=1.0)
        self.assertEqual(evaluate(b, others=[a]), [])


class StackingTests(unittest.TestCase):
    def test_two_layer_stack_ok(self):
        low = placement("low", stackable=True, max_layers=2, bear_load=200)
        up = placement("up", cargo="c2", y=0.0000001, level=1,
                       stackable=True, max_layers=2, bear_load=200)
        self.assertEqual(evaluate(up, others=[low]), [])

    def test_third_layer_exceeds_max_layers(self):
        low = placement("low", stackable=True, max_layers=2, bear_load=500)
        mid = placement("mid", cargo="c2", y=1e-7, level=1,
                        stackable=True, max_layers=2, bear_load=500)
        top = placement("top", cargo="c3", y=2e-7, level=2,
                        stackable=True, max_layers=3, bear_load=500)
        # low/mid 允许 2 层，第三层越界
        self.assertIn(STACK_RULE, evaluate(top, others=[low, mid]))

    def test_non_stackable_on_top(self):
        low = placement("low", stackable=True, max_layers=2, bear_load=500)
        up = placement("up", cargo="c2", level=1, stackable=False)
        self.assertEqual(evaluate(up, others=[low]), [STACK_RULE])

    def test_level_one_without_support_is_floating(self):
        self.assertEqual(evaluate(placement(level=1, stackable=True)), [STACK_RULE])

    def test_gap_in_levels_is_floating(self):
        low = placement("low", stackable=True, max_layers=3, bear_load=500)
        top = placement("top", cargo="c2", level=2, stackable=True, max_layers=3)
        self.assertIn(STACK_RULE, evaluate(top, others=[low]))

    def test_upper_must_sit_within_lower_footprint(self):
        low = placement("low", length=1, width=1, stackable=True,
                        max_layers=2, bear_load=500)
        up = placement("up", cargo="c2", x=0.5, level=1, length=1, width=1,
                       stackable=True, max_layers=2)
        self.assertEqual(evaluate(up, others=[low]), [STACK_RULE])

    def test_bear_load_exceeded(self):
        low = placement("low", stackable=True, max_layers=2, bear_load=40)
        up = placement("up", cargo="c2", level=1, weight=100,
                       stackable=True, max_layers=2)
        self.assertEqual(evaluate(up, others=[low]), [STACK_RULE])

    def test_net_height_exceeded(self):
        low = placement("low", height=3.5, stackable=True, max_layers=2, bear_load=500)
        up = placement("up", cargo="c2", level=1, height=1.0,
                       stackable=True, max_layers=2)
        self.assertEqual(evaluate(up, others=[low]), [HEIGHT_LIMIT])


class LoadTests(unittest.TestCase):
    def test_floor_load_pressure(self):
        # 1000kg / 1m2 > 500kg/m2
        p = placement(weight=1000)
        self.assertEqual(evaluate(p), [FLOOR_LOAD])

    def test_shelf_level_total_load(self):
        sp = ground(kind="shelf_level", floor_load=10_000, level_load=300)
        p = placement(weight=400)
        self.assertEqual(evaluate(p, {"s1": sp}), [LEVEL_LOAD])

    def test_floor_load_includes_stack_weight(self):
        low = placement("low", weight=450, stackable=True, max_layers=2, bear_load=500)
        up = placement("up", cargo="c2", level=1, weight=100,
                       stackable=True, max_layers=2)
        # 合计 550kg/m2 > 500
        self.assertEqual(evaluate(up, others=[low]), [FLOOR_LOAD])


class AdjacencyTests(unittest.TestCase):
    def test_taboo_categories_too_close_across_spaces(self):
        # 两块相邻空间，水果与化学品仅一墙之隔（净距 0）
        s1 = ground("s1", x=0, w=5)
        s2 = ground("s2", x=5, w=5)
        spaces = {"s1": s1, "s2": s2}
        fruit = placement("a", space_id="s1", x=4.0, category="fruit")
        chem = placement("b", space_id="s2", cargo="c2", x=5.0,
                         category="chemical")
        taboo = (("chemical", "fruit"), 2.0)
        self.assertEqual(
            engine.evaluate(chem, spaces, [fruit], {( "chemical", "fruit"): 2.0}),
            [ADJACENCY])

    def test_taboo_satisfied_with_gap(self):
        s1 = ground("s1", x=0, w=5)
        s2 = ground("s2", x=5, w=5)
        spaces = {"s1": s1, "s2": s2}
        fruit = placement("a", space_id="s1", x=0.0, category="fruit")
        chem = placement("b", space_id="s2", cargo="c2", x=8.0,
                         category="chemical")
        self.assertEqual(
            engine.evaluate(chem, spaces, [fruit], {("chemical", "fruit"): 2.0}),
            [])


if __name__ == "__main__":
    unittest.main()
