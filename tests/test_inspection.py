"""巡场比对、越权追查、强制清退与可执行腾挪顺序。"""
from __future__ import annotations

from capacity.errors import AuthError, StateError

from tests._world import WorldTest, t


class InspectionTests(WorldTest):
    def _arrive(self, actor, cargo_id, where, x, y, spec=None):
        self.declare(actor, cargo_id, spec or self.box(80, length=2, width=2))
        hold = self.svc.plan_receiving(actor, cargo_id, where, x, y, t(8), t(20))
        self.svc.mark_arrived(actor, hold)
        return hold

    def test_observed_container_blocking_fire_lane(self):
        # 台账：柜子合法放在 y=3
        self.declare(self.B, "container", self.box(2000, "general",
                                                   length=12, width=2.5, height=2.6))
        hold = self.svc.plan_receiving(self.B, "container", "temp-1", 0, 3, t(8), t(20))
        self.svc.mark_arrived(self.B, hold)
        # 巡场：实际堆在 y=0（遮 lane-1）
        vids = self.svc.observe_occupancy(
            self.sec, "temp-1", t(10),
            [{"cargo_id": "container", "tenant_id": "ten-B",
              "x": 0, "y": 0, "length": 12, "width": 2.5}])
        self.assertTrue(vids)
        viol = self.svc.violations(self.sec)
        self.assertEqual({v["kind"] for v in viol}, {"FIRE_LANE_BLOCKED"})

    def test_off_book_cargo_is_unauthorized(self):
        vids = self.svc.observe_occupancy(
            self.sec, "temp-1", t(10),
            [{"tenant_id": "ten-B", "x": 1, "y": 4, "length": 2, "width": 2}])
        self.assertEqual(len(vids), 1)
        self.assertEqual(self.svc.violations(self.sec)[0]["kind"], "UNAUTHORIZED_OCCUPANCY")

    def test_tenant_mismatch_recorded(self):
        hold = self._arrive(self.A, "a1", "stall-A", 0, 0)
        vids = self.svc.observe_occupancy(
            self.sec, "stall-A", t(10),
            [{"cargo_id": "a1", "tenant_id": "ten-B",
              "x": 0, "y": 0, "length": 2, "width": 2}])
        kinds = {v["kind"] for v in self.svc.violations(self.sec)}
        self.assertIn("UNAUTHORIZED_OCCUPANCY", kinds)

    def test_ledger_mismatch_when_snapshot_missing(self):
        self._arrive(self.A, "a1", "stall-A", 0, 0)
        vids = self.svc.observe_occupancy(self.sec, "stall-A", t(10), [])
        kinds = {v["kind"] for v in self.svc.violations(self.sec)}
        self.assertIn("LEDGER_MISMATCH", kinds)

    def test_repeated_patrol_does_not_duplicate_violation(self):
        first = self.svc.observe_occupancy(
            self.sec, "temp-1", t(10),
            [{"tenant_id": "ten-B", "x": 1, "y": 4, "length": 2, "width": 2}])
        second = self.svc.observe_occupancy(
            self.sec, "temp-1", t(11),
            [{"tenant_id": "ten-B", "x": 1, "y": 4, "length": 2, "width": 2}])
        self.assertTrue(first)
        self.assertEqual(second, [])

    def test_tenant_cannot_run_patrol(self):
        with self.assertRaises(AuthError):
            self.svc.observe_occupancy(self.A, "temp-1", t(10), [])

    def test_full_clearance_flow_with_upper_first_ordering(self):
        # 违规柜（底层）合法台账位置 y=3，一箱货叠压在柜顶；
        # 腾挪顺序必须：先移走上层遮挡，再移走违规柜
        self.declare(self.B, "container", self.box(2000, "general",
                                                   length=4, width=2.5, height=2.6,
                                                   stackable=True, max_layers=2, bear_load=500))
        hold = self.svc.plan_receiving(self.B, "container", "temp-1", 0, 3, t(8), t(20))
        self.svc.mark_arrived(self.B, hold)
        self.declare(self.B, "box-top", self.box(30, "general",
                                                 stackable=True, max_layers=2, bear_load=100,
                                                 length=1, width=1, height=1))
        # 上层箱落在柜子顶面：level 1
        htop = self.svc.plan_receiving(self.B, "box-top", "temp-1", 0, 3, t(8), t(20), level=1)
        self.svc.mark_arrived(self.B, htop)
        # 巡场：柜子实际越过边界遮通道（箱子随柜一起违规遮 lane 不做要求）
        vids = self.svc.observe_occupancy(
            self.sec, "temp-1", t(10),
            [{"cargo_id": "container", "tenant_id": "ten-B",
              "x": 0, "y": 0, "length": 4, "width": 2.5}])
        vid = next(v for v in self.svc.violations(self.sec)
                   if v["kind"] == "FIRE_LANE_BLOCKED")["violation_id"]
        order = self.svc.order_clearance(self.admin, vid)
        plan = self.svc.plan_clearance_relocation(self.admin, order, ["temp-2"])
        detail = self.svc.get_relocation_plan(self.admin, plan)
        cargo_seq = [s["cargo_id"] for s in detail["steps"]]
        self.assertEqual(cargo_seq[0], "box-top")       # 上层先移
        self.assertEqual(cargo_seq[-1], "container")    # 违规柜最后移
        # 必须严格按顺序执行
        with self.assertRaises(StateError):
            self.svc.execute_relocation_step(self.sec, plan, 2)
        for step_no in range(1, len(cargo_seq) + 1):
            self.svc.execute_relocation_step(self.sec, plan, step_no)
        # 崩溃后重发最后一步：幂等，不重复落地/计费
        self.svc.execute_relocation_step(self.sec, plan, len(cargo_seq))
        bills = [b for b in self.svc.ledger_entries(self.admin, "ten-B")
                 if b["kind"] == "move_charge"]
        self.assertEqual(len(bills), len(cargo_seq))
        final = self.svc.get_relocation_plan(self.admin, plan)
        self.assertEqual(final["status"], "done")
        # 原空间已清空，每步都有留痕
        self.assertEqual(self.svc.space_ledger(self.admin, "temp-1", t(12))["occupancy"], 0)
        step_events = [e for e in self.svc.events(plan)
                       if e["event_type"] == "relocation.step_executed"]
        self.assertEqual(len(step_events), len(cargo_seq))

    def test_clearance_requires_staff(self):
        with self.assertRaises(AuthError):
            self.svc.plan_clearance_relocation(self.A, "order-x")

    def test_closure_relocation_empties_area_high_level_first(self):
        self.declare(self.A, "low", self.box(80, stackable=True, max_layers=2, bear_load=200))
        hl = self.svc.plan_receiving(self.A, "low", "stall-A", 0, 0, t(8), t(20))
        self.svc.mark_arrived(self.A, hl)
        self.declare(self.A, "up", self.box(60, stackable=True, max_layers=2))
        hu = self.svc.plan_receiving(self.A, "up", "stall-A", 0, 0, t(8), t(20), level=1)
        self.svc.mark_arrived(self.A, hu)
        self.svc.close_area(self.admin, "stall-A", t(12), t(20), "夜间封闭")
        plan = self.svc.plan_closure_relocation(self.admin, "stall-A", t(19), ["temp-2"])
        detail = self.svc.get_relocation_plan(self.admin, plan)
        self.assertEqual(detail["steps"][0]["cargo_id"], "up")
        for i in range(1, len(detail["steps"]) + 1):
            self.svc.execute_relocation_step(self.sec, plan, i)
        self.assertEqual(self.svc.space_ledger(self.admin, "stall-A", t(13))["occupancy"], 0)
