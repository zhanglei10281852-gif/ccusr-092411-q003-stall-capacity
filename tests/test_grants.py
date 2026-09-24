"""紧急放行：双人批准、适用范围、到期回收。"""
from __future__ import annotations

from capacity.errors import AuthError, CapacityError, StateError

from tests._world import WorldTest, t


class EmergencyGrantTests(WorldTest):
    def test_two_person_approval_flow(self):
        grant = self.svc.request_emergency_grant(
            self.B, "temp-1", "fire_lane", "应急抢险物资临时占道", t(14))
        # 申请人自己不能批
        with self.assertRaises(AuthError):
            self.svc.approve_emergency_grant(self.B, grant)
        # 租户也不能当第二批准人
        with self.assertRaises(AuthError):
            self.svc.approve_emergency_grant(self.A, grant)
        # 第二人（安保）批准后生效
        self.svc.approve_emergency_grant(self.sec, grant)
        self.declare(self.B, "rescue", self.box(100, "general", length=2, width=2))
        hold = self.svc.plan_receiving(self.B, "rescue", "temp-1", 0, 0, t(12), t(13),
                                       grant_id=grant)
        self.svc.mark_arrived(self.B, hold)
        ledger = self.svc.space_ledger(self.admin, "temp-1", t(12))
        self.assertTrue(any(i["cargo_id"] == "rescue" for i in ledger["items"]))

    def test_grant_scope_space_and_tenant(self):
        grant = self.svc.request_emergency_grant(
            self.B, "temp-1", "fire_lane", "x", t(14))
        self.svc.approve_emergency_grant(self.sec, grant)
        self.declare(self.A, "a1", self.box(50))
        # 放行属于 ten-B，A 不能用
        with self.assertRaises(AuthError):
            self.svc.plan_receiving(self.A, "a1", "temp-1", 0, 0, t(12), t(13),
                                    grant_id=grant)
        # 放行只对 temp-1，不能用在 stall-A
        self.declare(self.B, "b2", self.box(50, "general"))
        with self.assertRaises(AuthError):
            self.svc.plan_receiving(self.B, "b2", "stall-A", 0, 0, t(12), t(13),
                                    grant_id=grant)

    def test_expired_grant_cannot_be_used(self):
        grant = self.svc.request_emergency_grant(
            self.B, "temp-1", "fire_lane", "x", t(14))
        self.svc.approve_emergency_grant(self.sec, grant)
        self.declare(self.B, "b1", self.box(50, "general"))
        with self.assertRaises(StateError):
            self.svc.plan_receiving(self.B, "b1", "temp-1", 0, 0, t(15), t(16),
                                    grant_id=grant)

    def test_reclaim_cancels_unarrived_hold(self):
        grant = self.svc.request_emergency_grant(
            self.B, "temp-1", "fire_lane", "x", t(14))
        self.svc.approve_emergency_grant(self.sec, grant)
        self.declare(self.B, "b1", self.box(50, "general", length=2, width=2))
        hold = self.svc.plan_receiving(self.B, "b1", "temp-1", 0, 0, t(12), t(13),
                                       grant_id=grant)
        reclaimed = self.svc.reclaim_expired_grants(t(15))
        self.assertEqual(reclaimed, [grant])
        row = self.svc.db.execute("select status from holds where hold_id=?",
                                  (hold,)).fetchone()
        self.assertEqual(row["status"], "cancelled")
        # 容量立刻回来
        self.svc.plan_receiving(self.B, "b1", "temp-1", 0, 3, t(15), t(18))

    def test_reclaim_placed_hold_opens_violation(self):
        grant = self.svc.request_emergency_grant(
            self.B, "temp-1", "fire_lane", "x", t(14))
        self.svc.approve_emergency_grant(self.sec, grant)
        self.declare(self.B, "b1", self.box(100, "general", length=2, width=2))
        hold = self.svc.plan_receiving(self.B, "b1", "temp-1", 0, 0, t(12), t(13),
                                       grant_id=grant)
        self.svc.mark_arrived(self.B, hold)
        self.svc.reclaim_expired_grants(t(15))
        kinds = {v["kind"] for v in self.svc.violations(self.sec)}
        self.assertIn("FIRE_LANE_BLOCKED", kinds)

    def test_reject_flow(self):
        grant = self.svc.request_emergency_grant(
            self.B, "temp-1", "fire_lane", "x", t(14))
        self.svc.reject_emergency_grant(self.sec, grant)
        self.declare(self.B, "b1", self.box(50, "general"))
        with self.assertRaises(StateError):
            self.svc.plan_receiving(self.B, "b1", "temp-1", 0, 0, t(12), t(13),
                                    grant_id=grant)
