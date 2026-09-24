"""租户可见性掩码、账单隔离与跨进程并发占位。"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest

from capacity import CapacityService
from capacity.errors import CapacityError

from tests._world import WorldTest, t


class VisibilityTests(WorldTest):
    def test_tenant_sees_only_own_details(self):
        self.declare(self.A, "a1", self.box(50))
        self.declare(self.B, "b1", self.box(50, "general"))
        self.svc.plan_receiving(self.A, "a1", "temp-1", 0, 4, t(8), t(20))
        self.svc.plan_receiving(self.B, "b1", "temp-1", 2, 4, t(8), t(20))

        view_a = self.svc.space_ledger(self.A, "temp-1", t(10))
        self.assertEqual(view_a["occupancy"], 2)  # 知道有 2 处占用
        for item in view_a["items"]:
            if item["tenant_id"] == "***":
                self.assertEqual(item["cargo_id"], "***")
                self.assertIsNone(item["x"])
            else:
                self.assertEqual(item["tenant_id"], "ten-A")

        view_admin = self.svc.space_ledger(self.admin, "temp-1", t(10))
        cargo_ids = {i["cargo_id"] for i in view_admin["items"]}
        self.assertEqual(cargo_ids, {"a1", "b1"})  # 管理员可追查全部细节

    def test_billing_isolated_per_tenant(self):
        self.declare(self.A, "a1", self.box(50))
        hold = self.svc.plan_receiving(self.A, "a1", "temp-1", 0, 4, t(8), t(20))
        self.svc.mark_arrived(self.A, hold)
        from capacity.service import StepSpec
        mv = self.svc.create_move(self.A, [StepSpec("a1", "stall-A", 5, 5)],
                                  start_at=t(10), leave_at=t(19), idem_key="bill-1")
        self.svc.approve_move(self.admin, mv, charge_per_step=7)
        self.svc.begin_step(self.A, mv, 1)
        self.svc.complete_step(self.A, mv, 1, charge=9)

        bills_a = self.svc.ledger_entries(self.A)
        self.assertTrue(all(b["tenant_id"] == "ten-A" for b in bills_a))
        self.assertEqual({b["amount"] for b in bills_a}, {7.0, 9.0})
        # B 看不到 A 的账
        self.assertEqual(self.svc.ledger_entries(self.B), [])
        # 管理员能按租户过滤
        self.assertEqual(len(self.svc.ledger_entries(self.admin, "ten-A")), 2)


class ConcurrencyTests(unittest.TestCase):
    """两个连接/线程抢同一货位：BEGIN IMMEDIATE 串行化，容量不被穿透。"""

    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        self.path = self.tmp.name
        admin = __import__("capacity", fromlist=["Actor"]).Actor("admin-1", "admin")
        bootstrap = CapacityService(self.path)
        from capacity.service import SpaceSpec
        bootstrap.register_tenant(admin, "ten-A", "A")
        bootstrap.register_tenant(admin, "ten-B", "B")
        bootstrap.register_space(admin, SpaceSpec(
            "temp", "temp_area", 0, 0, 4, 4, 4, 1000))
        bootstrap.close()
        self.A = __import__("capacity", fromlist=["Actor"]).Actor("uA", "tenant", "ten-A")
        self.B = __import__("capacity", fromlist=["Actor"]).Actor("uB", "tenant", "ten-B")

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_parallel_booking_same_spot_one_wins(self):
        results: list[Exception | str] = []

        def book(actor, cargo, tenant, out_slot):
            svc = CapacityService(self.path, clock=lambda: t(7))
            try:
                from capacity import CargoSpec
                svc.declare_cargo(actor, cargo, CargoSpec(2, 2, 1, 50, "general"),
                                  tenant_id=tenant)
                hold = svc.plan_receiving(actor, cargo, "temp", 0, 0, t(8), t(20))
                results.append(hold)
            except Exception as exc:  # noqa: BLE001
                results.append(exc)
            finally:
                svc.close()

        threads = [
            threading.Thread(target=book, args=(self.A, "c-a", "ten-A", 0)),
            threading.Thread(target=book, args=(self.B, "c-b", "ten-B", 1)),
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        self.assertEqual(len(results), 2)
        holds = [r for r in results if isinstance(r, str)]
        errors = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(holds), 1, results)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], CapacityError)

        # 库内只有一个该位置的有效占位
        check = CapacityService(self.path)
        rows = check.db.execute(
            "select count(*) c from holds where status='reserved'").fetchone()
        self.assertEqual(rows["c"], 1)
        check.close()

    def test_reopen_database_state_persists(self):
        svc1 = CapacityService(self.path, clock=lambda: t(7))
        from capacity import CargoSpec
        svc1.declare_cargo(self.A, "c1", CargoSpec(1, 1, 1, 20, "fruit"))
        hold = svc1.plan_receiving(self.A, "c1", "temp", 0, 0, t(8), t(20))
        svc1.close()

        svc2 = CapacityService(self.path, clock=lambda: t(7))
        view = svc2.space_ledger(
            __import__("capacity", fromlist=["Actor"]).Actor("admin-1", "admin"),
            "temp", t(10))
        self.assertEqual(view["items"][0]["hold_id"], hold)
        svc2.close()


if __name__ == "__main__":
    unittest.main()
