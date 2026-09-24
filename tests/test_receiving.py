"""接货判断：安全边界、容量、预订/到场、调整边界。"""
from __future__ import annotations

from capacity.errors import CapacityError, StateError

from tests._world import WorldTest, t


class ReceivingTests(WorldTest):
    def test_fire_lane_blocks_booking(self):
        self.declare(self.B, "container", self.box(2000, "general",
                                                   length=12, width=2.5, height=2.6))
        # temp-1 原点与 lane-1 重叠
        with self.assertRaises(CapacityError) as ctx:
            self.svc.plan_receiving(self.B, "container", "temp-1", 0, 0, t(8), t(20))
        self.assertEqual(ctx.exception.reasons, ["FIRE_LANE_BLOCKED"])

    def test_inner_spot_books_and_arrives(self):
        self.declare(self.B, "container", self.box(2000, "general",
                                                   length=12, width=2.5, height=2.6))
        hold = self.svc.plan_receiving(self.B, "container", "temp-1", 0, 3, t(8), t(20))
        self.svc.mark_arrived(self.B, hold)
        ledger = self.svc.space_ledger(self.admin, "temp-1", t(10))
        self.assertEqual(ledger["occupancy"], 1)
        self.assertEqual(ledger["items"][0]["status"], "placed")

    def test_concurrent_area_claim_cannot_oversell(self):
        """两家同订一块只能放一箱的位置，时间重叠必须拒第二家。"""
        self.declare(self.A, "a1", self.box(200, length=3, width=3))
        self.declare(self.B, "b1", self.box(200, length=3, width=3, category="general"))
        self.svc.plan_receiving(self.A, "a1", "temp-1", 0, 3, t(8), t(20))
        with self.assertRaises(CapacityError) as ctx:
            self.svc.plan_receiving(self.B, "b1", "temp-1", 0, 3, t(9), t(18))
        self.assertIn("AREA_CONFLICT", ctx.exception.reasons)

    def test_other_tenant_fixed_stall_denied(self):
        self.declare(self.B, "b1", self.box(50, "general"))
        with self.assertRaises(CapacityError) as ctx:
            self.svc.plan_receiving(self.B, "b1", "stall-A", 0, 0, t(8), t(20))
        self.assertEqual(ctx.exception.reasons, ["UNAUTHORIZED_SPACE"])

    def test_closed_area_blocks_booking(self):
        self.svc.close_area(self.admin, "temp-1", t(8), t(18), "管线检修")
        self.declare(self.B, "b1", self.box(50, "general"))
        with self.assertRaises(CapacityError) as ctx:
            self.svc.plan_receiving(self.B, "b1", "temp-1", 0, 3, t(9), t(17))
        self.assertEqual(ctx.exception.reasons, ["AREA_CLOSED"])
        # 封闭窗口之外仍可预订
        self.svc.plan_receiving(self.B, "b1", "temp-1", 0, 3, t(19), t(22))

    def test_only_reserved_plan_can_be_adjusted(self):
        self.declare(self.A, "pear", self.box(80))
        hold = self.svc.plan_receiving(self.A, "pear", "stall-A", 2, 0, t(9), t(12))
        # 车辆延误 -> 改时间（未搬入，允许）
        self.svc.adjust_plan(self.A, hold, start_at=t(11), leave_at=t(16))
        self.svc.mark_arrived(self.A, hold)
        # 销售加快想再改 -> 已搬入，拒绝，只能腾挪
        with self.assertRaises(StateError):
            self.svc.adjust_plan(self.A, hold, leave_at=t(20))

    def test_adjustment_must_keep_capacity(self):
        self.declare(self.A, "a1", self.box(80, length=2, width=2))
        self.declare(self.A, "a2", self.box(80, length=2, width=2, category="general"))
        h1 = self.svc.plan_receiving(self.A, "a1", "stall-A", 0, 0, t(8), t(20))
        self.svc.plan_receiving(self.A, "a2", "stall-A", 2, 0, t(8), t(20))
        with self.assertRaises(CapacityError):
            self.svc.adjust_plan(self.A, h1, local_x=2.0)

    def test_expired_reservation_releases_capacity(self):
        self.declare(self.A, "a1", self.box(80, length=2, width=2))
        hold = self.svc.plan_receiving(self.A, "a1", "stall-A", 0, 0, t(8), t(10))
        result = self.svc.sweep_expiry(t(11))
        self.assertIn(hold, result["expired_reserved"])
        # 过期后同位置可被新预订使用
        self.declare(self.A, "a2", self.box(80, length=2, width=2, category="general"))
        self.svc.plan_receiving(self.A, "a2", "stall-A", 0, 0, t(11), t(15))

    def test_overstay_keeps_physical_capacity_and_opens_violation(self):
        self.declare(self.A, "a1", self.box(80, length=2, width=2))
        hold = self.svc.plan_receiving(self.A, "a1", "stall-A", 0, 0, t(8), t(10))
        self.svc.mark_arrived(self.A, hold)
        result = self.svc.sweep_expiry(t(11))
        self.assertIn(hold, result["overstay"])
        # 货还物理在场，容量继续被占
        self.declare(self.A, "a2", self.box(80, length=2, width=2, category="general"))
        with self.assertRaises(CapacityError):
            self.svc.plan_receiving(self.A, "a2", "stall-A", 0, 0, t(11), t(15))
        kinds = {v["kind"] for v in self.svc.violations(self.sec)}
        self.assertIn("OVERSTAY", kinds)

    def test_suggest_receiving_prefers_own_stall(self):
        self.declare(self.A, "a1", self.box(50))
        options = self.svc.suggest_receiving(self.A, "a1", t(8), t(20))
        self.assertTrue(options)
        self.assertEqual(options[0]["space_id"], "stall-A")

    def test_idempotent_booking(self):
        self.declare(self.A, "a1", self.box(50))
        h1 = self.svc.plan_receiving(self.A, "a1", "stall-A", 0, 0, t(8), t(20),
                                     idem_key="idem-001")
        h2 = self.svc.plan_receiving(self.A, "a1", "stall-A", 0, 0, t(8), t(20),
                                     idem_key="idem-001")
        self.assertEqual(h1, h2)

    def test_tenant_cannot_touch_others_cargo(self):
        self.declare(self.A, "a1", self.box(50))
        hold = self.svc.plan_receiving(self.A, "a1", "stall-A", 0, 0, t(8), t(20))
        from capacity.errors import AuthError
        with self.assertRaises(AuthError):
            self.svc.cancel_reservation(self.B, hold)
