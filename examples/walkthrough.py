"""端到端业务场景演示（可直接运行：python3 examples/walkthrough.py）。

故事线：
  1. 建档：固定档口 / 临时区 / 消防通道 / 货架层位、相邻禁忌；
  2. 精品果档在途货预订；整柜想遮消防通道被拒；
  3. 车辆延误只改未搬入计划；
  4. 巡场发现实际堆放遮通道且区域台账属另一商户 -> 违规单 -> 强制清退；
  5. 输出可执行腾挪顺序（上层先移）并逐步执行，逐步留痕；
  6. 紧急放行双人批准、到期回收；
  7. 搬运中崩溃 -> resume 幂等续跑，不重复释放、不重复计费；
  8. 租户视图掩码。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capacity import Actor, CapacityService, CargoSpec  # noqa: E402
from capacity.errors import CapacityError  # noqa: E402
from capacity.service import SpaceSpec, StepSpec  # noqa: E402

TZ = timezone(timedelta(hours=8))
def t(h, day=24):  # noqa: E306
    return datetime(2026, 9, day, h, tzinfo=TZ)


def main() -> None:
    admin = Actor("admin-1", "admin")
    security = Actor("sec-1", "security")
    fruit = Actor("u-fruit", "tenant", "ten-fruit")   # 精品果档
    veg = Actor("u-veg", "tenant", "ten-veg")         # 蔬菜档（台账占用方）

    svc = CapacityService(clock=lambda: t(7))

    # 1) 建档 ----------------------------------------------------------
    svc.register_tenant(admin, "ten-fruit", "精品果档")
    svc.register_tenant(admin, "ten-veg", "蔬菜档")
    svc.register_space(admin, SpaceSpec("stall-F", "fixed_stall", 0, 0, 10, 10, 4, 500,
                                        owner_id="ten-fruit"))
    svc.register_space(admin, SpaceSpec("temp-1", "temp_area", 20, 0, 14, 10, 4, 300))
    svc.register_space(admin, SpaceSpec("temp-2", "temp_area", 40, 0, 14, 10, 4, 300))
    svc.register_space(admin, SpaceSpec("lane-1", "fire_lane", 10, 0, 12, 3, 3, 0))
    svc.add_adjacency_taboo(admin, "fruit", "chemical", 2.0)

    # 2) 接货判断 -------------------------------------------------------
    svc.declare_cargo(fruit, "apple-1", CargoSpec(1, 1, 1, 80, "fruit"))
    options = svc.suggest_receiving(fruit, "apple-1", t(8), t(20))
    print("推荐货位（固定档口优先）：", options[0])
    hold_apple = svc.plan_receiving(fruit, "apple-1", options[0]["space_id"],
                                    options[0]["local_x"], options[0]["local_y"], t(8), t(20))

    svc.declare_cargo(veg, "container-1", CargoSpec(12, 2.5, 2.6, 2000, "general"))
    try:
        svc.plan_receiving(veg, "container-1", "temp-1", 0, 0, t(8), t(20))
    except CapacityError as exc:
        print("遮消防通道的接货被拒：", exc.reasons)
    hold_cab = svc.plan_receiving(veg, "container-1", "temp-1", 0, 3, t(8), t(20))
    svc.mark_arrived(veg, hold_cab)

    # 3) 车辆延误：未搬入计划可改；已搬入不可改 -----------------------------
    svc.declare_cargo(fruit, "pear-1", CargoSpec(1, 1, 1, 60, "fruit"))
    hold_pear = svc.plan_receiving(fruit, "pear-1", "stall-F", 2, 0, t(9), t(12))
    svc.adjust_plan(fruit, hold_pear, start_at=t(11), leave_at=t(16))
    print("延误改期成功：", hold_pear)

    # 4) 巡场：实际柜子越过边界遮 lane-1 ----------------------------------
    vids = svc.observe_occupancy(
        security, "temp-1", t(10),
        [{"cargo_id": "container-1", "tenant_id": "ten-veg",
          "x": 0, "y": 0, "length": 12, "width": 2.5}])
    print("违规单：", vids)
    viol = next(v for v in svc.violations(security) if v["kind"] == "FIRE_LANE_BLOCKED")
    order = svc.order_clearance(admin, viol["violation_id"])

    # 5) 腾挪顺序并执行 --------------------------------------------------
    plan = svc.plan_clearance_relocation(admin, order, ["temp-2"], leave_at=t(22))
    detail = svc.get_relocation_plan(admin, plan)
    print("腾挪顺序：", [(s["step_no"], s["cargo_id"], s["to_space"]) for s in detail["steps"]])
    for s in detail["steps"]:
        svc.execute_relocation_step(security, plan, s["step_no"])
    print("清退完成，temp-1 占用：", svc.space_ledger(admin, "temp-1", t(12))["occupancy"])

    # 6) 紧急放行双人批准 ------------------------------------------------
    grant = svc.request_emergency_grant(veg, "temp-1", "fire_lane", "抢险物资临时占道", t(14))
    try:
        svc.approve_emergency_grant(veg, grant)
    except Exception as exc:  # noqa: BLE001
        print("申请人自批被拒：", type(exc).__name__)
    svc.approve_emergency_grant(security, grant)
    print("紧急放行生效：", grant)
    print("到期回收：", svc.reclaim_expired_grants(t(15)))

    # 7) 搬运崩溃恢复 ----------------------------------------------------
    svc.mark_arrived(fruit, hold_apple)
    svc.declare_cargo(fruit, "apple-2", CargoSpec(1, 1, 1, 70, "fruit"))
    mv = svc.create_move(fruit, [StepSpec("apple-1", "temp-2", 0, 7)],
                         start_at=t(13), leave_at=t(19), idem_key="walkthrough-move")
    svc.approve_move(admin, mv, charge_per_step=5)
    svc.begin_step(fruit, mv, 1)
    # —— 此刻进程崩溃，重启后管理员判定 resume ——
    svc.decide_recovery(admin, mv, "resume", "重启后未见落地痕迹")
    first = svc.complete_step(fruit, mv, 1, charge=10)
    replay = svc.complete_step(fruit, mv, 1, charge=10)
    print("完成回执 / 重放回执：", first["idempotent_replay"], replay["idempotent_replay"])

    # 8) 租户视图掩码 ----------------------------------------------------
    view = svc.space_ledger(fruit, "temp-2", t(14))
    print("果档看 temp-2：", [(i["cargo_id"], i["tenant_id"]) for i in view["items"]])

    step_events = [e["event_type"] for e in svc.events(mv)]
    print("搬运全程事件：", step_events)
    print("账单条数（不重复计费）：", len(svc.ledger_entries(admin, "ten-fruit")))


if __name__ == "__main__":
    main()
