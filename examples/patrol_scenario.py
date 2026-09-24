"""巡场场景演示：整柜遮挡消防通道 → 追查 → 紧急放行 → 到期回收 → 腾挪。

运行：python3 examples/patrol_scenario.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capacity_governance import CapacityService, Store

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 24, 8, 0, tzinfo=TZ)


def main() -> None:
    svc = CapacityService(Store(), clock=lambda: T0)
    svc.register_tenant("admin", "市场管理处", is_admin=True)
    svc.register_tenant("sec", "安全负责人", is_admin=True)
    svc.register_tenant("ops", "运营主管", is_admin=True)
    svc.register_tenant("fruit", "精品果档口")
    svc.register_tenant("veg", "蔬菜档口")

    svc.define_space(space_id="A1", kind="fixed", name="精品果档",
                     area_m2=20, max_weight_kg=1000, max_height_m=2.0,
                     owner_tenant_id="fruit")
    svc.define_space(space_id="T1", kind="temp", name="临时一区",
                     area_m2=30, max_weight_kg=2000, max_height_m=2.5)
    svc.define_space(space_id="F1", kind="temp", name="消防通道",
                     area_m2=40, max_weight_kg=2000, max_height_m=3.0,
                     is_fire_lane=True)

    print("== 1. 巡场发现：整柜遮挡消防通道，台账显示区域被精品果档口占用 ==")
    record = svc.report_encroachment(
        actor="sec", space_id="F1",
        description="整柜遮挡消防通道；台账显示该区域被精品果档口占用",
        ref="container-88")
    print(f"  越权占位已登记追查：{record}")

    print("== 2. 精品果档口在途货需要接货：接货判断给出可用空间 ==")
    svc.declare_lot(lot_id="lot-101", tenant_id="fruit", category="fruit",
                    length_m=3, width_m=2, height_m=1.2, weight_kg=400,
                    expected_departure=T0 + timedelta(hours=10))
    options = svc.find_options(
        lot_id="lot-101", starts=T0, ends=T0 + timedelta(hours=10),
        actor="fruit")
    for o in options:
        print(f"  可用：{o['name']} 余量 {o['free_area_m2']}m² / "
              f"{o['free_weight_kg']}kg")
    svc.plan_move(task_id="m-101", lot_id="lot-101", to_space_id="A1",
                  actor="fruit", planned_in_at=T0,
                  planned_out_at=T0 + timedelta(hours=10))
    print("  已冻结 A1 容量并批准接货（自有档口自动批准）")

    print("== 3. 紧急放行：两人批准，期限覆盖在场窗口 ==")
    svc.declare_lot(lot_id="lot-88", tenant_id="fruit", category="container",
                    length_m=3, width_m=2, height_m=2.2, weight_kg=600)
    svc.request_permit(
        permit_id="p-88", space_id="F1", reason="整柜临时周转，等待卸货位",
        requested_by="fruit",
        valid_from=T0 - timedelta(minutes=30),
        valid_until=T0 + timedelta(hours=6))
    svc.approve_permit("p-88", "sec")
    svc.approve_permit("p-88", "ops")
    svc.plan_move(task_id="m-88", lot_id="lot-88", to_space_id="F1",
                  actor="admin", permit_id="p-88",
                  planned_in_at=T0, planned_out_at=T0 + timedelta(hours=5))
    svc.start_move("m-88")
    svc.complete_move("m-88", at=T0 + timedelta(minutes=40))
    print("  p-88 已由 sec/ops 两人批准，整柜限时占用消防通道")

    print("== 4. 期限到达：许可回收，在场货物按强制清退结算 ==")
    result = svc.sweep(at=T0 + timedelta(hours=7))
    print(f"  回收许可：{result['permits_expired']}")
    charges = svc.list_charges("admin")
    for c in charges:
        print(f"  计费：任务 {c['task_id']} {c['kind']} ¥{c['amount']}")

    print("== 5. 消防通道需要立即净空：生成可执行腾挪顺序 ==")
    svc.declare_lot(lot_id="lot-102", tenant_id="fruit", category="fruit",
                    length_m=2, width_m=2, height_m=1.0, weight_kg=200)
    # 另放一批货在 T1 模拟封闭区域腾挪（8 小时后才离场，需改投而非加速离场）
    svc.plan_move(task_id="m-103", lot_id="lot-102", to_space_id="T1",
                  actor="admin",
                  planned_in_at=T0, planned_out_at=T0 + timedelta(hours=8))
    svc.start_move("m-103")
    svc.complete_move("m-103", at=T0 + timedelta(minutes=20))
    svc.close_space("T1", "admin")
    steps = svc.build_evacuation_plan("T1", "admin")
    for i, s in enumerate(steps, 1):
        print(f"  步骤{i}: {s['type']} 货物 {s['lot_id']} "
              f"{s['from_space_id']} → {s['to_space_id']}")
    for s in steps:
        svc.execute_evacuation_step(s, "admin")
    print(f"  T1 当前占用：{svc.space_view('T1', 'admin')['used_area_m2']}m²")

    print("== 6. 货物轨迹留痕（跨区域每一步）==")
    for e in svc.trajectory("lot-102", "admin"):
        print(f"  {e['occurred_at']} {e['event_type']} by {e['actor']}")

    print("== 7. 租户视角隔离：蔬菜档口看不到精品果的货位细节 ==")
    view = svc.space_view("A1", "veg")
    print(f"  A1 占用者（脱敏）：{view['occupants']}")


if __name__ == "__main__":
    main()
