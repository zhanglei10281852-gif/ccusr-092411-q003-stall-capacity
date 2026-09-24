"""跨区域搬运：分步留痕、顺序约束、崩溃恢复、幂等计费。"""
from __future__ import annotations

from capacity.errors import StateError
from capacity.service import StepSpec

from tests._world import WorldTest, t


class MoveLifecycleTests(WorldTest):
    def _place(self, actor, cargo_id, where="stall-A", x=3, y=3, **spec_kw):
        self.declare(actor, cargo_id, self.box(**spec_kw))
        hold = self.svc.plan_receiving(actor, cargo_id, where, x, y, t(8), t(20))
        self.svc.mark_arrived(actor, hold)
        return hold

    def test_two_step_move_executes_in_order(self):
        hold = self._place(self.A, "p1", length=1, width=1)
        mv = self.svc.create_move(
            self.A,
            [StepSpec("p1", "temp-2", 0, 3),
             StepSpec("p1", "shelf-1", 0, 0)],
            start_at=t(10), leave_at=t(19), idem_key="mv-1")
        self.svc.approve_move(self.admin, mv, charge_per_step=5)
        # 未完成第一步不能跳步
        with self.assertRaises(StateError):
            self.svc.begin_step(self.A, mv, 2)
        self.svc.begin_step(self.A, mv, 1)
        self.svc.complete_step(self.A, mv, 1, charge=10)
        self.svc.begin_step(self.A, mv, 2)
        self.svc.complete_step(self.A, mv, 2, charge=10)
        # 源位最终在 shelf-1，中间 temp-2 已释放
        ledger = self.svc.space_ledger(self.admin, "temp-2", t(15))
        self.assertEqual(ledger["occupancy"], 0)
        shelf = self.svc.space_ledger(self.admin, "shelf-1", t(15))
        self.assertEqual(shelf["items"][0]["cargo_id"], "p1")
        # 每一步都有事件留痕
        types = [e["event_type"] for e in self.svc.events(mv)]
        self.assertEqual(types.count("move.started"), 1)
        self.assertEqual(types.count("move.step_recorded"), 3)  # 第2步begin + 2次done
        self.assertIn("move.completed", types)

    def test_approve_locks_capacity_atomically(self):
        self._place(self.A, "p1", length=1, width=1)
        self._place(self.B, "b1", where="temp-2", x=0, y=8, category="general",
                    length=1, width=1)
        mv = self.svc.create_move(
            self.A, [StepSpec("p1", "temp-2", 0, 3)],
            start_at=t(10), leave_at=t(19), idem_key="mv-2")
        self.svc.approve_move(self.admin, mv)
        # 目标位已被 A 的 reserved 锁定：B 的新在途货想订同一位置/时段被拒
        self.declare(self.B, "b2", self.box(50, "general"))
        from capacity.errors import CapacityError
        with self.assertRaises(CapacityError):
            self.svc.plan_receiving(self.B, "b2", "temp-2", 0, 3, t(11), t(18))
        # 但锁定窗口之外仍可订
        self.svc.plan_receiving(self.B, "b2", "temp-2", 0, 3, t(19), t(22))

    def test_cancel_planned_move_releases_locks(self):
        self._place(self.A, "p1", length=1, width=1)
        mv = self.svc.create_move(
            self.A, [StepSpec("p1", "temp-2", 0, 3)],
            start_at=t(10), leave_at=t(19), idem_key="mv-3")
        self.svc.approve_move(self.admin, mv)
        self.svc.cancel_move(self.A, mv)
        # 锁定释放，同位置可再订
        self.declare(self.B, "b2", self.box(50, "general"))
        self.svc.plan_receiving(self.B, "b2", "temp-2", 0, 3, t(11), t(18))

    def test_cancel_moving_requires_recovery_decision(self):
        self._place(self.A, "p1", length=1, width=1)
        mv = self.svc.create_move(
            self.A, [StepSpec("p1", "temp-2", 0, 3)],
            start_at=t(10), leave_at=t(19), idem_key="mv-4")
        self.svc.approve_move(self.admin, mv)
        self.svc.begin_step(self.A, mv, 1)
        with self.assertRaises(StateError):
            self.svc.cancel_move(self.A, mv)

    def test_recovery_resume_is_idempotent(self):
        self._place(self.A, "p1", length=1, width=1)
        mv = self.svc.create_move(
            self.A, [StepSpec("p1", "temp-2", 0, 3)],
            start_at=t(10), leave_at=t(19), idem_key="mv-5")
        self.svc.approve_move(self.admin, mv, charge_per_step=5)
        self.svc.begin_step(self.A, mv, 1)
        # 模拟崩溃重启：begin 已落库，complete 未知
        self.svc.decide_recovery(self.admin, mv, "resume", "重启后未见落地痕迹")
        r1 = self.svc.complete_step(self.A, mv, 1, charge=10)
        r2 = self.svc.complete_step(self.A, mv, 1, charge=10)
        self.assertFalse(r1["idempotent_replay"])
        self.assertTrue(r2["idempotent_replay"])
        bills = [b for b in self.svc.ledger_entries(self.admin, "ten-A")
                 if b["kind"] == "move_charge"]
        self.assertEqual(len(bills), 1)  # 绝不重复计费
        self.assertEqual(bills[0]["amount"], 10)
        # 源位只释放一次
        releases = [e for e in self.svc.events() if e["event_type"] == "space.released"]
        self.assertEqual(len(releases), 1)

    def test_recovery_revert_before_begin(self):
        self._place(self.A, "p1", length=1, width=1)
        mv = self.svc.create_move(
            self.A, [StepSpec("p1", "temp-2", 0, 3)],
            start_at=t(10), leave_at=t(19), idem_key="mv-6")
        self.svc.approve_move(self.admin, mv)
        # 崩溃发生在开工前：approved 状态可自动撤销，目标 reserved 全部回收
        self.svc.decide_recovery(self.admin, mv, "revert", "车辆故障，任务未开工")
        stall = self.svc.space_ledger(self.admin, "stall-A", t(12))
        self.assertEqual(stall["items"][0]["cargo_id"], "p1")
        self.assertEqual(self.svc.space_ledger(self.admin, "temp-2", t(12))["occupancy"], 0)

    def test_recovery_revert_refused_after_begin(self):
        self._place(self.A, "p1", length=1, width=1)
        mv = self.svc.create_move(
            self.A, [StepSpec("p1", "temp-2", 0, 3)],
            start_at=t(10), leave_at=t(19), idem_key="mv-7")
        self.svc.approve_move(self.admin, mv)
        self.svc.begin_step(self.A, mv, 1)
        with self.assertRaises(StateError):
            self.svc.decide_recovery(self.admin, mv, "revert", "不应允许")

    def test_manual_freezes_until_resolved(self):
        self._place(self.A, "p1", length=1, width=1)
        mv = self.svc.create_move(
            self.A, [StepSpec("p1", "temp-2", 0, 3)],
            start_at=t(10), leave_at=t(19), idem_key="mv-9")
        self.svc.approve_move(self.admin, mv)
        self.svc.begin_step(self.A, mv, 1)
        self.svc.decide_recovery(self.admin, mv, "manual", "摄像头离线，位置不明")
        with self.assertRaises(StateError):
            self.svc.complete_step(self.A, mv, 1)
        # 管理员现场确认货已到位 -> 人工了结
        self.svc.resolve_manual(self.admin, mv, "complete_current")
        self.assertEqual(
            self.svc.space_ledger(self.admin, "temp-2", t(12))["items"][0]["cargo_id"], "p1")
        self.assertEqual(
            self.svc.space_ledger(self.admin, "stall-A", t(12))["occupancy"], 0)

    def test_create_move_is_idempotent(self):
        self._place(self.A, "p1", length=1, width=1)
        spec = [StepSpec("p1", "temp-2", 0, 3)]
        m1 = self.svc.create_move(self.A, spec, start_at=t(10), leave_at=t(19), idem_key="dup")
        m2 = self.svc.create_move(self.A, spec, start_at=t(10), leave_at=t(19), idem_key="dup")
        self.assertEqual(m1, m2)

    def test_cannot_bundle_tenants_in_one_move(self):
        self._place(self.A, "p1", where="stall-A", length=1, width=1)
        self._place(self.B, "p2", where="temp-2", length=1, width=1, category="general")
        from capacity.errors import AuthError
        with self.assertRaises(AuthError):
            self.svc.create_move(
                self.A, [StepSpec("p1", "temp-2", 0, 3), StepSpec("p2", "temp-2", 2, 3)],
                start_at=t(10), leave_at=t(19))
