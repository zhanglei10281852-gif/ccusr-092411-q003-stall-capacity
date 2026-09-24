"""容量治理应用服务。

线程/并发约定：每个写方法在自己的 BEGIN IMMEDIATE 事务内完成
"读占用 -> 引擎重算 -> 写凭证/事件"，配合各表唯一约束与幂等键，
并发占位不可能同时穿透容量，重试/重启也不会重复释放或重复计费。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from . import engine
from .errors import AuthError, CapacityError, DomainError, NotFoundError, StateError
from .geometry import Rect, intersects as rect_intersects
from .model import Actor, CargoSpec
from .storage import connect, dumps, parse_ts

FAR_FUTURE = datetime(9999, 12, 30, tzinfo=timezone.utc)


@dataclass(frozen=True)
class SpaceSpec:
    space_id: str
    kind: str  # fixed_stall/temp_area/shelf_level/fire_lane
    x: float
    y: float
    w: float
    d: float
    net_height: float
    floor_load: float
    level_load: float = 0.0
    owner_id: str | None = None
    parent_id: str | None = None
    level_no: int | None = None
    attrs: dict[str, Any] | None = None


@dataclass(frozen=True)
class StepSpec:
    """一个搬运步：货物 from_hold -> 目标空间位置（局部坐标，米）。"""

    cargo_id: str
    to_space: str
    to_x: float
    to_y: float
    to_level: int = 0
    from_hold: str | None = None
    start_at: datetime | None = None
    leave_at: datetime | None = None


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class CapacityService:
    def __init__(self, db_path: str | Path = ":memory:", clock=None):
        self.db = connect(db_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self) -> datetime:
        return self.clock()

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------------- 事务

    @contextmanager
    def _tx(self):
        connection = self.db
        connection.execute("begin immediate")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _event(self, conn: sqlite3.Connection, event_type: str, aggregate_id: str,
               actor: Actor | None, payload: dict[str, Any], at: datetime | None = None) -> str:
        event_id = _uid("evt")
        conn.execute(
            "insert into event_log values (?, ?, ?, ?, ?, ?)",
            (event_id, event_type, aggregate_id, (at or self.now()).isoformat(),
             actor.actor_id if actor else None, dumps(payload)),
        )
        return event_id

    # ---------------------------------------------------------------- 主数据

    def register_tenant(self, actor: Actor, tenant_id: str, name: str) -> None:
        self._require_staff(actor)
        with self._tx() as conn:
            conn.execute("insert or ignore into tenants values (?, ?, ?)",
                         (tenant_id, name, self.now().isoformat()))
            self._event(conn, "space.registered", tenant_id, actor, {"name": name, "kind": "tenant"})

    def register_space(self, actor: Actor, spec: SpaceSpec) -> None:
        self._require_staff(actor)
        if spec.kind not in {"fixed_stall", "temp_area", "shelf_level", "fire_lane"}:
            raise DomainError(f"未知空间类型：{spec.kind}")
        with self._tx() as conn:
            try:
                conn.execute(
                    "insert into spaces(space_id,kind,owner_id,parent_id,level_no,x,y,w,d,"
                    "net_height,floor_load,level_load,status,attrs) "
                    "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (spec.space_id, spec.kind, spec.owner_id, spec.parent_id, spec.level_no,
                     spec.x, spec.y, spec.w, spec.d, spec.net_height, spec.floor_load,
                     spec.level_load, "open", dumps(spec.attrs or {})),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError(f"空间已存在：{spec.space_id}") from exc
            self._event(conn, "space.registered", spec.space_id, actor,
                        {"kind": spec.kind, "w": spec.w, "d": spec.d})

    def add_adjacency_taboo(self, actor: Actor, category_a: str, category_b: str, min_gap: float) -> None:
        self._require_staff(actor)
        a, b = sorted((category_a, category_b))
        with self._tx() as conn:
            conn.execute(
                "insert into adjacency_taboos values (?, ?, ?) "
                "on conflict(category_a,category_b) do update set min_gap=excluded.min_gap",
                (a, b, min_gap),
            )

    def declare_cargo(self, actor: Actor, cargo_id: str, spec: CargoSpec, *,
                      tenant_id: str | None = None, in_transit: bool = True) -> None:
        owner = tenant_id or actor.tenant_id
        if owner is None:
            raise AuthError("无法识别货物归属租户")
        if actor.tenant_id is not None and actor.tenant_id != owner:
            raise AuthError("不能替其他商户申报货物")
        with self._tx() as conn:
            try:
                conn.execute(
                    "insert into cargo values (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cargo_id, owner, spec.category, spec.length, spec.width, spec.height,
                     spec.weight, int(spec.stackable), spec.max_layers, spec.bear_load,
                     int(in_transit), self.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError(f"货物已申报：{cargo_id}") from exc
            self._event(conn, "cargo.declared", cargo_id, actor,
                        {"tenant_id": owner, "category": spec.category, "in_transit": in_transit})

    def close_area(self, actor: Actor, space_id: str, closed_from: datetime,
                   closed_until: datetime, reason: str) -> None:
        self._require_staff(actor)
        if closed_until <= closed_from:
            raise DomainError("封闭结束时间必须晚于开始时间")
        with self._tx() as conn:
            sp = self._get_space(conn, space_id)
            attrs = json.loads(sp["attrs"])
            attrs.update(closed_from=closed_from.isoformat(), closed_until=closed_until.isoformat())
            conn.execute("update spaces set status='closed', closed_reason=?, attrs=? where space_id=?",
                         (reason, dumps(attrs), space_id))
            self._event(conn, "space.closed", space_id, actor,
                        {"from": closed_from.isoformat(), "until": closed_until.isoformat(), "reason": reason})

    def reopen_area(self, actor: Actor, space_id: str) -> None:
        self._require_staff(actor)
        with self._tx() as conn:
            sp = self._get_space(conn, space_id)
            attrs = json.loads(sp["attrs"])
            attrs.pop("closed_from", None)
            attrs.pop("closed_until", None)
            conn.execute("update spaces set status='open', closed_reason=null, attrs=? where space_id=?",
                         (dumps(attrs), space_id))
            self._event(conn, "space.reopened", space_id, actor, {})

    # ---------------------------------------------------------------- 接货/占位

    def plan_receiving(self, actor: Actor, cargo_id: str, space_id: str,
                       local_x: float, local_y: float, start_at: datetime, leave_at: datetime,
                       *, level: int = 0, idem_key: str | None = None,
                       grant_id: str | None = None) -> str:
        """为（多为在途）货物预订货位，写 reserved 占位凭证。返回 hold_id。幂等。"""
        with self._tx() as conn:
            idem = idem_key or _uid("idem-hold")
            existing = conn.execute("select hold_id from holds where idem_key=?", (idem,)).fetchone()
            if existing:
                return existing["hold_id"]
            cargo = self._get_cargo(conn, cargo_id)
            self._assert_cargo_access(actor, cargo["tenant_id"])
            sp = self._get_space(conn, space_id)
            overrides = self._grant_overrides(conn, grant_id, cargo["tenant_id"], space_id, start_at)
            return self._create_hold(
                conn, actor, cargo, sp, local_x, local_y, level,
                start_at, leave_at, idem, overrides, grant_id)

    def _create_hold(self, conn, actor, cargo, sp, local_x, local_y, level,
                     start_at, leave_at, idem_key, overrides, grant_id) -> str:
        candidate = self._candidate(cargo, sp, local_x, local_y, level, start_at, leave_at,
                                    hold_id="(pending)", tenant_id=cargo["tenant_id"])
        reasons = engine.evaluate(candidate, self._load_spaces(conn), self._load_placements(conn),
                                  self._load_taboos(conn), grant_overrides=overrides)
        if reasons:
            raise CapacityError(reasons)
        hold_id = _uid("hold")
        conn.execute(
            "insert into holds(hold_id,space_id,cargo_id,tenant_id,x,y,stack_level,start_at,leave_at,"
            "status,idem_key,grant_id,created_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (hold_id, sp["space_id"], cargo["cargo_id"], cargo["tenant_id"],
             candidate.x, candidate.y, level, start_at.isoformat(), leave_at.isoformat(),
             "reserved", idem_key, grant_id, self.now().isoformat()),
        )
        self._event(conn, "space.held", hold_id, actor, {
            "space_id": sp["space_id"], "cargo_id": cargo["cargo_id"],
            "start": start_at.isoformat(), "leave": leave_at.isoformat(), "status": "reserved"})
        return hold_id

    def adjust_plan(self, actor: Actor, hold_id: str, *, start_at: datetime | None = None,
                    leave_at: datetime | None = None, space_id: str | None = None,
                    local_x: float | None = None, local_y: float | None = None,
                    level: int | None = None) -> str:
        """销售加快/车辆延误/区域封闭后调整计划。只有尚未搬入的 reserved 凭证可改。"""
        with self._tx() as conn:
            hold = self._get_hold(conn, hold_id)
            self._assert_cargo_access(actor, hold["tenant_id"])
            if hold["status"] != "reserved":
                raise StateError(f"货物已搬入（{hold['status']}），只能腾挪不能改预订；hold={hold_id}")
            cargo = self._get_cargo(conn, hold["cargo_id"])
            old_sp = self._get_space(conn, hold["space_id"])
            new_sid = space_id or hold["space_id"]
            sp = self._get_space(conn, new_sid)
            same_space = new_sid == hold["space_id"]
            nx = local_x if local_x is not None else (hold["x"] - old_sp["x"] if same_space else 0.0)
            ny = local_y if local_y is not None else (hold["y"] - old_sp["y"] if same_space else 0.0)
            new_start = start_at or parse_ts(hold["start_at"])
            new_leave = leave_at or parse_ts(hold["leave_at"])
            new_level = hold["stack_level"] if level is None else level

            candidate = self._candidate(cargo, sp, nx, ny, new_level, new_start, new_leave,
                                        hold_id=hold_id, tenant_id=hold["tenant_id"])
            overrides = self._grant_overrides(conn, hold["grant_id"], hold["tenant_id"],
                                              sp["space_id"], new_start)
            others = [p for p in self._load_placements(conn) if p.hold_id != hold_id]
            reasons = engine.evaluate(candidate, self._load_spaces(conn), others,
                                      self._load_taboos(conn), grant_overrides=overrides)
            if reasons:
                raise CapacityError(reasons)
            conn.execute(
                "update holds set space_id=?,x=?,y=?,stack_level=?,start_at=?,leave_at=? where hold_id=?",
                (sp["space_id"], candidate.x, candidate.y, new_level,
                 new_start.isoformat(), new_leave.isoformat(), hold_id))
            self._event(conn, "plan.adjusted", hold_id, actor, {
                "space_id": sp["space_id"], "start": new_start.isoformat(),
                "leave": new_leave.isoformat()})
            return hold_id

    def cancel_reservation(self, actor: Actor, hold_id: str) -> None:
        with self._tx() as conn:
            hold = self._get_hold(conn, hold_id)
            self._assert_cargo_access(actor, hold["tenant_id"])
            if hold["status"] != "reserved":
                raise StateError("仅未搬入的预订可取消")
            self._cancel_hold(conn, actor, hold_id)

    def _cancel_hold(self, conn, actor, hold_id: str) -> None:
        conn.execute("update holds set status='cancelled' where hold_id=?", (hold_id,))
        self._event(conn, "space.released", hold_id, actor, {"reason": "cancelled"})

    def mark_arrived(self, actor: Actor, hold_id: str, at: datetime | None = None) -> None:
        """车辆到场、货物实际搬入：reserved -> placed，到场瞬间再验一次容量。"""
        at = at or self.now()
        with self._tx() as conn:
            hold = self._get_hold(conn, hold_id)
            self._assert_cargo_access(actor, hold["tenant_id"])
            if hold["status"] != "reserved":
                raise StateError(f"预订状态为 {hold['status']}，不能确认到场")
            cargo = self._get_cargo(conn, hold["cargo_id"])
            candidate = self._placement_from_hold(hold, cargo, hold_id)
            others = [p for p in self._load_placements(conn) if p.hold_id != hold_id]
            overrides = self._grant_overrides(conn, hold["grant_id"], hold["tenant_id"],
                                              hold["space_id"], at)
            reasons = engine.evaluate(candidate, self._load_spaces(conn), others,
                                      self._load_taboos(conn), grant_overrides=overrides)
            if reasons:
                raise CapacityError(reasons)
            conn.execute("update cargo set in_transit=0 where cargo_id=?", (hold["cargo_id"],))
            conn.execute("update holds set status='placed', placed_at=? where hold_id=?",
                         (at.isoformat(), hold_id))
            self._event(conn, "space.held", hold_id, actor, {"status": "placed"}, at=at)

    def release_hold(self, actor: Actor, hold_id: str, at: datetime | None = None) -> None:
        """货物离场，释放空间，幂等。placed（含超期 expired 但未离场）均可释放。"""
        at = at or self.now()
        with self._tx() as conn:
            hold = self._get_hold(conn, hold_id)
            if actor.tenant_id is not None and actor.tenant_id != hold["tenant_id"] and not actor.is_staff:
                raise AuthError("不能释放其他商户的占位")
            if hold["status"] == "released":
                return
            if hold["status"] not in ("placed", "expired") or not hold["placed_at"]:
                raise StateError(f"占位状态为 {hold['status']}，无在场货物可释放")
            conn.execute("update holds set status='released', released_at=? where hold_id=?",
                         (at.isoformat(), hold_id))
            self._event(conn, "space.released", hold_id, actor, {"reason": "departed"}, at=at)

    def sweep_expiry(self, now: datetime | None = None) -> dict[str, list[str]]:
        """到期处理：未到场的预订过期释放；在场超期记 OVERSTAY（货仍物理占位，继续占容量）。"""
        now = now or self.now()
        expired_reserved: list[str] = []
        overstays: list[str] = []
        with self._tx() as conn:
            rows = conn.execute(
                "select * from holds where status in ('reserved','placed') and leave_at<=?",
                (now.isoformat(),)).fetchall()
            for hold in rows:
                if hold["status"] == "reserved":
                    conn.execute("update holds set status='expired' where hold_id=?", (hold["hold_id"],))
                    self._event(conn, "hold.expired", hold["hold_id"], None,
                                {"was": "reserved"}, at=now)
                    expired_reserved.append(hold["hold_id"])
                else:
                    conn.execute("update holds set status='expired' where hold_id=?", (hold["hold_id"],))
                    self._event(conn, "hold.expired", hold["hold_id"], None,
                                {"was": "placed", "overstay": True}, at=now)
                    self._record_violation(conn, "OVERSTAY", hold["space_id"],
                                           hold["cargo_id"], hold["tenant_id"],
                                           f"超过预计离场时间 {hold['leave_at']} 仍未搬离",
                                           actor=None, at=now)
                    overstays.append(hold["hold_id"])
        return {"expired_reserved": expired_reserved, "overstay": overstays}

    # ---------------------------------------------------------------- 接货建议

    def suggest_receiving(self, actor: Actor, cargo_id: str, start_at: datetime,
                          leave_at: datetime, *, grant_id: str | None = None) -> list[dict[str, Any]]:
        """给出可行货位候选（空间、局部坐标、层），固定档口优先。"""
        with self._tx() as conn:
            cargo = self._get_cargo(conn, cargo_id)
            self._assert_cargo_access(actor, cargo["tenant_id"])
            spaces = self._load_spaces(conn)
            others = self._load_placements(conn)
            taboos = self._load_taboos(conn)
            options: list[dict[str, Any]] = []
            for sid, sp in spaces.items():
                if sp["kind"] == "fire_lane":
                    continue
                overrides = self._grant_overrides(conn, grant_id, cargo["tenant_id"], sid, start_at)
                for level in range(0, 3):
                    for lx, ly in self._candidate_positions(sp, cargo):
                        candidate = self._candidate(cargo, sp, lx, ly, level, start_at, leave_at,
                                                    hold_id="(probe)", tenant_id=cargo["tenant_id"])
                        if not engine.evaluate(candidate, spaces, others, taboos,
                                               grant_overrides=overrides):
                            options.append({"space_id": sid, "local_x": lx, "local_y": ly,
                                            "level": level, "kind": sp["kind"]})
                    if level > 0 and not cargo["stackable"]:
                        break
            options.sort(key=lambda o: ({"fixed_stall": 0, "temp_area": 1, "shelf_level": 2}[o["kind"]],
                                        o["space_id"], o["level"], o["local_y"], o["local_x"]))
            return options

    @staticmethod
    def _candidate_positions(sp: dict[str, Any], cargo) -> list[tuple[float, float]]:
        if sp["w"] < cargo["length"] or sp["d"] < cargo["width"]:
            return []
        nx = min(4, int((sp["w"] - cargo["length"]) // cargo["length"]) + 1)
        ny = min(4, int((sp["d"] - cargo["width"]) // cargo["width"]) + 1)
        positions = [(0.0, 0.0)]
        for iy in range(ny):
            for ix in range(nx):
                positions.append((round(ix * cargo["length"], 6), round(iy * cargo["width"], 6)))
        seen: set[tuple[float, float]] = set()
        uniq: list[tuple[float, float]] = []
        for p in positions:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq

    # ---------------------------------------------------------------- 跨区域搬运（分步留痕 + 恢复）

    def create_move(self, actor: Actor, steps: Sequence[StepSpec], *,
                    start_at: datetime, leave_at: datetime,
                    idem_key: str | None = None) -> str:
        """编排跨区域搬运。审批通过时才为每步锁定目标 reserved 货位。"""
        if not steps:
            raise DomainError("搬运至少需要一个步骤")
        with self._tx() as conn:
            idem = idem_key or _uid("idem-move")
            existing = conn.execute("select move_id from move_tasks where idem_key=?", (idem,)).fetchone()
            if existing:
                return existing["move_id"]
            tenant_id = self._get_cargo(conn, steps[0].cargo_id)["tenant_id"]
            for idx, s in enumerate(steps, start=1):
                c = self._get_cargo(conn, s.cargo_id)
                self._assert_cargo_access(actor, c["tenant_id"])
                if c["tenant_id"] != tenant_id:
                    raise AuthError("一个搬运任务不能跨租户拼单")
                if s.from_hold:
                    src = self._get_hold(conn, s.from_hold)
                    if src["cargo_id"] != s.cargo_id:
                        raise DomainError(f"步骤 {idx} 源占位与货物不符")
            move_id = _uid("move")
            conn.execute("insert into move_tasks values (?,?,?,?,?)",
                         (move_id, tenant_id, "planned", idem, self.now().isoformat()))
            for idx, s in enumerate(steps, start=1):
                conn.execute(
                    "insert into move_steps(move_id,step_no,cargo_id,from_space,from_hold,to_space,"
                    "to_x,to_y,to_level,start_at,leave_at,status) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (move_id, idx, s.cargo_id,
                     self._get_hold(conn, s.from_hold)["space_id"] if s.from_hold else None,
                     s.from_hold, s.to_space, s.to_x, s.to_y, s.to_level,
                     (s.start_at or start_at).isoformat(), (s.leave_at or leave_at).isoformat(),
                     "pending"))
            self._event(conn, "move.planned", move_id, actor, {"steps": len(steps)})
            return move_id

    def approve_move(self, actor: Actor, move_id: str, charge_per_step: float = 0.0) -> list[str]:
        """管理员批准搬运；为每个目标步创建 reserved 占位（锁定容量），整单原子。"""
        self._require_staff(actor)
        with self._tx() as conn:
            task = self._get_move(conn, move_id)
            if task["status"] != "planned":
                raise StateError(f"搬运状态为 {task['status']}，不能审批")
            # 解析每步源位：显式 from_hold 优先；否则按货物当前在场位置，
            # 链式步骤中后一步自动接前一步的目标
            current_hold: dict[str, str] = {}
            for row in conn.execute(
                "select cargo_id, hold_id from holds where status in ('placed','expired') "
                "and placed_at is not null"
            ).fetchall():
                current_hold[row["cargo_id"]] = row["hold_id"]
            dest_holds: list[str] = []
            steps = self._steps(conn, move_id)
            for step in steps:
                if step["from_hold"] is None:
                    src_id = current_hold.get(step["cargo_id"])
                    if src_id:
                        src = self._get_hold(conn, src_id)
                        conn.execute(
                            "update move_steps set from_hold=?, from_space=? where move_id=? and step_no=?",
                            (src_id, src["space_id"], move_id, step["step_no"]))
                cargo = self._get_cargo(conn, step["cargo_id"])
                sp = self._get_space(conn, step["to_space"])
                hold_id = self._create_hold(
                    conn, actor, cargo, sp, step["to_x"], step["to_y"], step["to_level"],
                    parse_ts(step["start_at"]), parse_ts(step["leave_at"]),
                    f"move:{move_id}:{step['step_no']}:dst", set(), None)
                conn.execute("update move_steps set new_hold_id=? where move_id=? and step_no=?",
                             (hold_id, move_id, step["step_no"]))
                dest_holds.append(hold_id)
                current_hold[step["cargo_id"]] = hold_id
            conn.execute("update move_tasks set status='approved' where move_id=?", (move_id,))
            self._charge(conn, actor, f"move-approval:{move_id}", task["tenant_id"],
                         "approval_charge", "move", move_id,
                         charge_per_step * len(dest_holds))
            self._event(conn, "move.approved", move_id, actor, {"holds": dest_holds})
            return dest_holds

    def begin_step(self, actor: Actor, move_id: str, step_no: int, at: datetime | None = None) -> None:
        at = at or self.now()
        with self._tx() as conn:
            self._assert_not_manual(conn, move_id)
            task = self._get_move(conn, move_id)
            step = self._get_step(conn, move_id, step_no)
            if step["status"] == "in_progress":
                return  # 崩溃后重发，幂等
            if task["status"] not in ("approved", "moving"):
                raise StateError(f"搬运状态为 {task['status']}，不能开始步骤")
            if step["status"] != "pending":
                raise StateError(f"步骤状态为 {step['status']}")
            if step_no > 1:
                prev = self._get_step(conn, move_id, step_no - 1)
                if prev["status"] != "done":
                    raise StateError("必须按顺序执行，上一步尚未完成")
            conn.execute("update move_tasks set status='moving' where move_id=?", (move_id,))
            conn.execute("update move_steps set status='in_progress' where move_id=? and step_no=?",
                         (move_id, step_no))
            self._event(conn, "move.started" if step_no == 1 else "move.step_recorded",
                        move_id, actor, {"step_no": step_no, "phase": "begin"}, at=at)

    def complete_step(self, actor: Actor, move_id: str, step_no: int,
                      at: datetime | None = None, charge: float = 0.0) -> dict[str, Any]:
        """完成一步：目标位 reserved->placed、源位 placed->released、计费一次。全幂等。"""
        at = at or self.now()
        with self._tx() as conn:
            self._assert_not_manual(conn, move_id)
            task = self._get_move(conn, move_id)
            step = self._get_step(conn, move_id, step_no)

            # 幂等重放：崩溃后重发完成指令，已落库则原样返回，绝不二次释放/计费
            if step["status"] == "done":
                return {"move_id": move_id, "step_no": step_no, "idempotent_replay": True,
                        "new_hold_id": step["new_hold_id"]}
            if step["status"] != "in_progress":
                raise StateError(f"步骤状态为 {step['status']}，不能完成")

            dest_hold = self._get_hold(conn, step["new_hold_id"])
            if dest_hold["status"] == "reserved":
                # 落地瞬间再校验一次现实容量
                cargo = self._get_cargo(conn, step["cargo_id"])
                candidate = self._placement_from_hold(dest_hold, cargo, dest_hold["hold_id"])
                others = [p for p in self._load_placements(conn)
                          if p.hold_id != dest_hold["hold_id"]]
                reasons = engine.evaluate(candidate, self._load_spaces(conn), others,
                                          self._load_taboos(conn))
                if reasons:
                    raise CapacityError(reasons)
                conn.execute("update holds set status='placed', placed_at=? where hold_id=?",
                             (at.isoformat(), dest_hold["hold_id"]))
                conn.execute("update cargo set in_transit=0 where cargo_id=?", (step["cargo_id"],))
                self._event(conn, "space.held", dest_hold["hold_id"], actor,
                            {"status": "placed", "move_id": move_id, "step_no": step_no}, at=at)

            if step["from_hold"]:
                src = self._get_hold(conn, step["from_hold"])
                if src["status"] in ("placed", "expired") and src["placed_at"]:
                    conn.execute("update holds set status='released', released_at=? where hold_id=?",
                                 (at.isoformat(), src["hold_id"]))
                    self._event(conn, "space.released", src["hold_id"], actor,
                                {"reason": "moved", "move_id": move_id, "step_no": step_no}, at=at)

            conn.execute("update move_steps set status='done' where move_id=? and step_no=?",
                         (move_id, step_no))
            self._event(conn, "move.step_recorded", move_id, actor,
                        {"step_no": step_no, "phase": "done"}, at=at)
            self._charge(conn, actor, f"move:{move_id}:{step_no}", task["tenant_id"],
                         "move_charge", "move_step", f"{move_id}:{step_no}", charge, at=at)

            if not conn.execute(
                "select 1 from move_steps where move_id=? and status!='done' limit 1",
                (move_id,)).fetchone():
                conn.execute("update move_tasks set status='completed' where move_id=?", (move_id,))
                self._event(conn, "move.completed", move_id, actor, {}, at=at)
            return {"move_id": move_id, "step_no": step_no, "idempotent_replay": False,
                    "new_hold_id": dest_hold["hold_id"]}

    def cancel_move(self, actor: Actor, move_id: str) -> None:
        """搬运未起步前可整单撤销，释放已锁定的目标货位。"""
        with self._tx() as conn:
            task = self._get_move(conn, move_id)
            if not actor.is_staff and actor.tenant_id != task["tenant_id"]:
                raise AuthError("不能撤销他人搬运单")
            if task["status"] == "completed":
                raise StateError("搬运已完成")
            if task["status"] == "moving":
                raise StateError("搬运进行中，需先走恢复判定（resume/revert/manual）")
            for step in self._steps(conn, move_id):
                if step["new_hold_id"]:
                    h = self._get_hold(conn, step["new_hold_id"])
                    if h["status"] == "reserved":
                        self._cancel_hold(conn, actor, h["hold_id"])
                conn.execute("update move_steps set status='cancelled' where move_id=? and step_no=?",
                             (move_id, step["step_no"]))
            conn.execute("update move_tasks set status='cancelled' where move_id=?", (move_id,))
            self._event(conn, "move.cancelled", move_id, actor, {})

    def decide_recovery(self, actor: Actor, move_id: str, decision: str, reason: str) -> dict[str, Any]:
        """异常重启后对进行中的搬运判定 继续/撤销/待人工。

        * resume ：步骤幂等，重发 begin_step/complete_step 即可安全继续；
        * revert ：仅当没有任何一步真正完成、在途步也未落地时可自动撤销；
        * manual ：无法判定物理状态，冻结该单，等待管理员人工了结。
        判定本身落库留痕。
        """
        self._require_staff(actor)
        if decision not in ("resume", "revert", "manual"):
            raise DomainError("decision 必须是 resume/revert/manual")
        with self._tx() as conn:
            task = self._get_move(conn, move_id)
            if task["status"] not in ("approved", "moving"):
                raise StateError(f"仅 approved/moving 状态需要恢复判定，当前 {task['status']}")
            steps = self._steps(conn, move_id)
            done = [s for s in steps if s["status"] == "done"]
            in_flight = next((s for s in steps if s["status"] == "in_progress"), None)

            if decision == "revert":
                if done:
                    raise StateError("已有步骤完成，物理位置已改变，不能自动撤销，请判定 manual")
                if in_flight is not None:
                    raise StateError("步骤已开始（begin 已落库），无法确认货物是否已离地，"
                                     "请判定 resume 或 manual")
                for s in steps:
                    if s["new_hold_id"]:
                        h = self._get_hold(conn, s["new_hold_id"])
                        if h["status"] == "reserved":
                            self._cancel_hold(conn, actor, h["hold_id"])
                    conn.execute("update move_steps set status='cancelled' where move_id=? and step_no=?",
                                 (move_id, s["step_no"]))
                conn.execute("update move_tasks set status='cancelled' where move_id=?", (move_id,))
            # resume：不改状态，complete_step 的幂等保证安全
            # manual：仅落决策，_assert_not_manual 会冻结后续自动执行

            conn.execute(
                "insert into recovery_decisions values (?,?,?,?,?) "
                "on conflict(move_id) do update set decision=excluded.decision, reason=excluded.reason, "
                "decided_by=excluded.decided_by, decided_at=excluded.decided_at",
                (move_id, decision, reason, actor.actor_id, self.now().isoformat()))
            self._event(conn, "recovery.decided", move_id, actor,
                        {"decision": decision, "reason": reason})
            return {"move_id": move_id, "decision": decision}

    def resolve_manual(self, actor: Actor, move_id: str, action: str) -> None:
        """manual 冻结单的人工了结：complete_current（继续幂等完成）或 cancel。"""
        self._require_staff(actor)
        if action not in ("complete_current", "cancel"):
            raise DomainError("action 必须是 complete_current/cancel")
        with self._tx() as conn:
            decision = conn.execute("select * from recovery_decisions where move_id=?",
                                    (move_id,)).fetchone()
            if decision is None or decision["decision"] != "manual":
                raise StateError("该搬运单不处于 manual 冻结状态")
            step = conn.execute(
                "select * from move_steps where move_id=? and status='in_progress'",
                (move_id,)).fetchone()
            if action == "cancel":
                if step and step["new_hold_id"]:
                    h = self._get_hold(conn, step["new_hold_id"])
                    if h["status"] == "reserved":
                        self._cancel_hold(conn, actor, h["hold_id"])
                conn.execute("update move_steps set status='cancelled' where move_id=? and status='in_progress'",
                             (move_id,))
                conn.execute("update move_tasks set status='cancelled' where move_id=?", (move_id,))
                conn.execute("delete from recovery_decisions where move_id=?", (move_id,))
                self._event(conn, "move.cancelled", move_id, actor, {"manual": True})
                return
            if step is None:
                raise StateError("没有 in_progress 步骤需要了结")
            # 解除冻结后交回常规幂等路径（独立事务，避免事务重入）
            conn.execute("delete from recovery_decisions where move_id=?", (move_id,))
        if action == "complete_current":
            self.complete_step(actor, move_id, step["step_no"])

    # ---------------------------------------------------------------- 巡检/越权/强制清退

    def observe_occupancy(self, actor: Actor, space_id: str, observed_at: datetime,
                          items: Sequence[dict[str, Any]], *, complete_snapshot: bool = True) -> list[str]:
        """巡场上报实际堆放，与台账比对。

        item: {cargo_id?, tenant_id?, x,y,length,width}（空间内局部坐标；
        cargo_id 缺省 = 账外货）。返回新建违规单号列表，重复上报不会刷重复单。
        """
        self._require_staff(actor)
        with self._tx() as conn:
            sp = self._get_space(conn, space_id)
            violations: list[str] = []
            ledger = conn.execute(
                "select * from holds where status='placed' and space_id=? and start_at<=? and leave_at>?",
                (space_id, observed_at.isoformat(), observed_at.isoformat())).fetchall()
            # 超期未离场（expired 且已搬入）物理上仍在场，同样参与账实核对
            ledger += conn.execute(
                "select * from holds where status='expired' and placed_at is not null and space_id=?",
                (space_id,)).fetchall()
            lanes = [d for d in self._load_spaces(conn).values() if d["kind"] == "fire_lane"]

            matched_holds: set[str] = set()
            for item in items:
                gx, gy = sp["x"] + item["x"], sp["y"] + item["y"]
                foot = Rect(gx, gy, item["length"], item["width"])
                match = None
                if item.get("cargo_id"):
                    match = next((h for h in ledger if h["cargo_id"] == item["cargo_id"]), None)
                for lane in lanes:
                    if rect_intersects(foot, Rect(lane["x"], lane["y"], lane["w"], lane["d"])):
                        vid = self._record_violation(
                            conn, "FIRE_LANE_BLOCKED", lane["space_id"], item.get("cargo_id"),
                            item.get("tenant_id") or (match["tenant_id"] if match else None),
                            f"实际堆放遮压消防通道 {lane['space_id']}", actor, observed_at)
                        if vid:
                            violations.append(vid)
                if match:
                    matched_holds.add(match["hold_id"])
                    if item.get("tenant_id") and item["tenant_id"] != match["tenant_id"]:
                        vid = self._record_violation(
                            conn, "UNAUTHORIZED_OCCUPANCY", space_id, item["cargo_id"],
                            item["tenant_id"], "实际占用人与台账租户不符", actor, observed_at)
                        if vid:
                            violations.append(vid)
                else:
                    vid = self._record_violation(
                        conn, "UNAUTHORIZED_OCCUPANCY", space_id, item.get("cargo_id"),
                        item.get("tenant_id"), "账外/越权占位：现场有货但无有效在场凭证",
                        actor, observed_at,
                        detail_extra={"footprint": [gx, gy, item["length"], item["width"]]})
                    if vid:
                        violations.append(vid)
            if complete_snapshot:
                for h in ledger:
                    if h["hold_id"] not in matched_holds:
                        vid = self._record_violation(
                            conn, "LEDGER_MISMATCH", space_id, h["cargo_id"], h["tenant_id"],
                            "台账显示在场但巡场快照未见该货", actor, observed_at)
                        if vid:
                            violations.append(vid)
            return violations

    def order_clearance(self, actor: Actor, violation_id: str) -> str:
        """管理员对违规（遮通道/越权占位等）下强制清退工单。"""
        self._require_staff(actor)
        with self._tx() as conn:
            v = conn.execute("select * from violations where violation_id=?",
                             (violation_id,)).fetchone()
            if v is None:
                raise NotFoundError(f"违规单不存在：{violation_id}")
            if v["status"] != "open":
                raise StateError("违规单已处理")
            order_id = _uid("order")
            conn.execute(
                "insert into clearance_orders(order_id,violation_id,tenant_id,space_id,cargo_id,"
                "status,created_by,created_at) values (?,?,?,?,?,'open',?,?)",
                (order_id, violation_id, v["tenant_id"], v["space_id"], v["cargo_id"],
                 actor.actor_id, self.now().isoformat()))
            conn.execute("update violations set status='resolved', resolved_at=?, clearance_order_id=? "
                         "where violation_id=?",
                         (self.now().isoformat(), order_id, violation_id))
            self._event(conn, "clearance.ordered", order_id, actor,
                        {"violation_id": violation_id, "cargo_id": v["cargo_id"],
                         "space_id": v["space_id"]})
            return order_id

    def plan_clearance_relocation(self, actor: Actor, order_id: str,
                                  candidate_spaces: Sequence[str] | None = None,
                                  *, leave_at: datetime | None = None) -> str:
        """为清退工单生成实际可执行的腾挪顺序（上方遮挡货先移，违规货最后移）。"""
        self._require_staff(actor)
        with self._tx() as conn:
            order = conn.execute("select * from clearance_orders where order_id=?",
                                 (order_id,)).fetchone()
            if order is None:
                raise NotFoundError(f"清退工单不存在：{order_id}")
            cargo_ids = self._blocking_chain(conn, order["space_id"], order["cargo_id"])
            leave = leave_at or self.now() + timedelta(hours=24)
            plan_id = self._build_relocation_plan(
                conn, actor, "clearance", cargo_ids, candidate_spaces, leave)
            conn.execute("update clearance_orders set plan_id=? where order_id=?",
                         (plan_id, order_id))
            return plan_id

    def plan_closure_relocation(self, actor: Actor, space_id: str, leave_at: datetime,
                                candidate_spaces: Sequence[str] | None = None) -> str:
        """区域封闭前，把空间内全部在场货排出腾挪顺序（高层先于低层）。"""
        self._require_staff(actor)
        with self._tx() as conn:
            rows = conn.execute(
                "select cargo_id, stack_level from holds "
                "where space_id=? and status in ('placed','expired') and placed_at is not null",
                (space_id,)).fetchall()
            ordered = [r["cargo_id"] for r in sorted(rows, key=lambda r: -r["stack_level"])]
            return self._build_relocation_plan(
                conn, actor, "closure", ordered, candidate_spaces, leave_at)

    def _blocking_chain(self, conn, space_id: str | None, cargo_id: str | None) -> list[str]:
        """违规货 + 几何上叠压在它上方、必须先移开的货，按层从高到低。"""
        rows = conn.execute(
            "select h.*, c.length c_length, c.width c_width "
            "from holds h join cargo c on c.cargo_id=h.cargo_id "
            "where h.status in ('placed','expired') and h.placed_at is not null",
            ()).fetchall()
        present = [r for r in rows if space_id is None or r["space_id"] == space_id
                   or self._footprint_in_space(conn, r, space_id)]
        if cargo_id is None:
            return [r["cargo_id"] for r in sorted(present, key=lambda r: -r["stack_level"])]
        target = next((r for r in present if r["cargo_id"] == cargo_id), None)
        if target is None:
            target = next((r for r in rows if r["cargo_id"] == cargo_id), None)
            if target is None:
                return [cargo_id]
        above = [r for r in rows
                 if r["space_id"] == target["space_id"]
                 and r["cargo_id"] != cargo_id and r["stack_level"] > target["stack_level"]
                 and rect_intersects(Rect(r["x"], r["y"], r["c_length"], r["c_width"]),
                                     Rect(target["x"], target["y"], target["c_length"], target["c_width"]))]
        above.sort(key=lambda r: -r["stack_level"])
        chain = [r["cargo_id"] for r in above] + [cargo_id]
        seen: set[str] = set()
        result: list[str] = []
        for cid in chain:
            if cid not in seen:
                seen.add(cid)
                result.append(cid)
        return result

    @staticmethod
    def _footprint_in_space(conn, hold_row, space_id: str) -> bool:
        sp = conn.execute("select * from spaces where space_id=?", (space_id,)).fetchone()
        if sp is None or sp["kind"] != "fire_lane":
            return False
        return rect_intersects(Rect(hold_row["x"], hold_row["y"],
                                    hold_row["c_length"], hold_row["c_width"]),
                               Rect(sp["x"], sp["y"], sp["w"], sp["d"]))

    def _build_relocation_plan(self, conn, actor, reason: str, cargo_ids: Sequence[str],
                               candidate_spaces: Sequence[str] | None,
                               leave_at: datetime) -> str:
        plan_id = _uid("plan")
        conn.execute("insert into relocation_plans(plan_id,reason,status,created_by,created_at) "
                     "values (?,?, 'open', ?,?)",
                     (plan_id, reason, actor.actor_id, self.now().isoformat()))
        spaces = self._load_spaces(conn)
        targets = [sid for sid in (candidate_spaces or []) if sid in spaces]
        if not targets:
            targets = [sid for sid, sp in spaces.items()
                       if sp["kind"] in ("temp_area", "fixed_stall", "shelf_level")
                       and sp["status"] == "open"]
        taboos = self._load_taboos(conn)
        planned: list[engine.Placement] = []
        moved: set[str] = set()
        step_no = 0
        for cargo_id in cargo_ids:
            cargo = self._get_cargo(conn, cargo_id)
            src = conn.execute(
                "select * from holds where cargo_id=? and status in ('placed','expired') "
                "and placed_at is not null order by stack_level desc limit 1",
                (cargo_id,)).fetchone()
            placement = self._find_target(
                conn, spaces, taboos, cargo,
                [sid for sid in targets if src is None or sid != src["space_id"]],
                self.now(), leave_at, planned, cargo_id, moved)
            if placement is None:
                raise CapacityError(f"找不到可安置 {cargo_id} 的目标空间，需人工指定")
            step_no += 1
            conn.execute(
                "insert into relocation_steps(plan_id,step_no,cargo_id,from_space,to_space,to_x,to_y,"
                "to_level,start_at,leave_at,status) values (?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, step_no, cargo_id, src["space_id"] if src else None,
                 placement.space_id,
                 placement.x - spaces[placement.space_id]["x"],
                 placement.y - spaces[placement.space_id]["y"],
                 placement.level, self.now().isoformat(), leave_at.isoformat(), "pending"))
            planned.append(placement)
            moved.add(cargo_id)
        self._event(conn, "relocation.planned", plan_id, actor,
                    {"reason": reason, "steps": step_no})
        return plan_id

    def _find_target(self, conn, spaces, taboos, cargo, target_spaces, start_at, leave_at,
                     already_planned: list[engine.Placement], cargo_id: str,
                     moved_cargo: set[str]):
        # 已排进计划的货物，其旧位在执行到本步时已释放，不计入冲突背景
        others = [p for p in self._load_placements(conn) if p.cargo_id not in moved_cargo]
        others += already_planned
        for sid in target_spaces:
            sp = spaces[sid]
            for level in range(0, 3):
                for lx, ly in self._candidate_positions(sp, cargo):
                    cand = self._candidate(cargo, sp, lx, ly, level, start_at, leave_at,
                                           hold_id=f"(plan-{cargo_id})", tenant_id=cargo["tenant_id"])
                    if not engine.evaluate(cand, spaces, others, taboos):
                        return cand
        return None

    def execute_relocation_step(self, actor: Actor, plan_id: str, step_no: int,
                                charge: float = 0.0) -> str:
        """按腾挪计划执行一步：内部生成单步 move，走同一套留痕/计费/恢复路径。

        必须严格按 step_no 顺序执行；返回新 hold_id。
        """
        self._require_staff(actor)
        with self._tx() as conn:
            plan = conn.execute("select * from relocation_plans where plan_id=?",
                                (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError(f"腾挪计划不存在：{plan_id}")
            if plan["status"] == "done":
                # 允许对已完成计划的最后一步做幂等重放
                step = conn.execute("select * from relocation_steps where plan_id=? and step_no=?",
                                    (plan_id, step_no)).fetchone()
                if step is not None and step["status"] == "done":
                    row = conn.execute(
                        "select h.hold_id from relocation_steps r join move_steps m "
                        "on m.move_id=r.move_id join holds h on h.hold_id=m.new_hold_id "
                        "where r.plan_id=? and r.step_no=?",
                        (plan_id, step_no)).fetchone()
                    return row["hold_id"]
                raise StateError("腾挪计划已执行完毕")
            step = conn.execute("select * from relocation_steps where plan_id=? and step_no=?",
                                (plan_id, step_no)).fetchone()
            if step is None:
                raise NotFoundError(f"步骤不存在：{plan_id}:{step_no}")
            if step["status"] == "done":
                # 崩溃后重发：步骤已原子落库，幂等返回既有目标位
                row = conn.execute(
                    "select h.hold_id from relocation_steps r join move_steps m "
                    "on m.move_id=r.move_id join holds h on h.hold_id=m.new_hold_id "
                    "where r.plan_id=? and r.step_no=?",
                    (plan_id, step_no)).fetchone()
                return row["hold_id"]
            if step["status"] != "pending":
                raise StateError(f"步骤状态为 {step['status']}")
            if step_no > 1:
                prev = conn.execute("select status from relocation_steps where plan_id=? and step_no=?",
                                    (plan_id, step_no - 1)).fetchone()
                if prev["status"] != "done":
                    raise StateError("必须按腾挪顺序执行，上一步未完成")
            src = conn.execute(
                "select * from holds where cargo_id=? and status in ('placed','expired') "
                "and placed_at is not null order by stack_level desc limit 1",
                (step["cargo_id"],)).fetchone()
            cargo = self._get_cargo(conn, step["cargo_id"])
            sp = self._get_space(conn, step["to_space"])
            start_at, leave_at = parse_ts(step["start_at"]), parse_ts(step["leave_at"])

            move_id = _uid("move")
            conn.execute("update relocation_plans set status='executing' where plan_id=?", (plan_id,))
            conn.execute("insert into move_tasks values (?,?,?,?,?)",
                         (move_id, cargo["tenant_id"], "moving", f"reloc:{plan_id}:{step_no}",
                          self.now().isoformat()))
            conn.execute(
                "insert into move_steps(move_id,step_no,cargo_id,from_space,from_hold,to_space,"
                "to_x,to_y,to_level,start_at,leave_at,status) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                (move_id, 1, step["cargo_id"], src["space_id"] if src else None,
                 src["hold_id"] if src else None, step["to_space"], step["to_x"], step["to_y"],
                 step["to_level"], start_at.isoformat(), leave_at.isoformat(), "in_progress"))
            dest_hold = self._create_hold(
                conn, actor, cargo, sp, step["to_x"], step["to_y"], step["to_level"],
                start_at, leave_at, f"move:{move_id}:1:dst", set(), None)
            # 落地（容量已在 _create_hold 用当前现实状态校验）
            conn.execute("update holds set status='placed', placed_at=? where hold_id=?",
                         (self.now().isoformat(), dest_hold))
            conn.execute("update cargo set in_transit=0 where cargo_id=?", (step["cargo_id"],))
            self._event(conn, "space.held", dest_hold, actor,
                        {"status": "placed", "plan_id": plan_id})
            if src is not None:
                conn.execute("update holds set status='released', released_at=? where hold_id=?",
                             (self.now().isoformat(), src["hold_id"]))
                self._event(conn, "space.released", src["hold_id"], actor,
                            {"reason": "relocated", "plan_id": plan_id, "move_id": move_id})
            conn.execute("update move_steps set status='done',new_hold_id=? where move_id=? and step_no=1",
                         (dest_hold, move_id))
            conn.execute("update move_tasks set status='completed' where move_id=?", (move_id,))
            self._event(conn, "move.started", move_id, actor, {"plan_id": plan_id, "step_no": step_no})
            self._event(conn, "move.completed", move_id, actor, {"plan_id": plan_id, "step_no": step_no})
            self._charge(conn, actor, f"move:{move_id}:1", cargo["tenant_id"],
                         "move_charge", "move_step", f"{move_id}:1", charge)
            conn.execute("update relocation_steps set status='done', move_id=? where plan_id=? and step_no=?",
                         (move_id, plan_id, step_no))
            self._event(conn, "relocation.step_executed", plan_id, actor,
                        {"step_no": step_no, "move_id": move_id})
            if not conn.execute(
                "select 1 from relocation_steps where plan_id=? and status!='done' limit 1",
                (plan_id,)).fetchone():
                conn.execute("update relocation_plans set status='done' where plan_id=?", (plan_id,))
                conn.execute("update clearance_orders set status='done', done_at=? where plan_id=?",
                             (self.now().isoformat(), plan_id))
            return dest_hold

    def get_relocation_plan(self, actor: Actor, plan_id: str) -> dict[str, Any]:
        self._require_staff(actor)
        with self.db:
            plan = self.db.execute("select * from relocation_plans where plan_id=?",
                                   (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError(plan_id)
            steps = self.db.execute(
                "select * from relocation_steps where plan_id=? order by step_no",
                (plan_id,)).fetchall()
            return {"plan_id": plan_id, "reason": plan["reason"], "status": plan["status"],
                    "steps": [dict(s) for s in steps]}

    # ---------------------------------------------------------------- 紧急放行（双人批准 + 到期回收）

    def request_emergency_grant(self, actor: Actor, space_id: str, override: str,
                                reason: str, expires_at: datetime) -> str:
        if override not in {"fire_lane", "ownership", "closure"}:
            raise DomainError("override 必须是 fire_lane/ownership/closure")
        with self._tx() as conn:
            self._get_space(conn, space_id)
            grant_id = _uid("grant")
            conn.execute(
                "insert into emergency_grants(grant_id,tenant_id,space_id,override,reason,"
                "requested_by,requested_at,expires_at,status,idem_key) values (?,?,?,?,?,?,?,?,?,?)",
                (grant_id, actor.tenant_id or "", space_id, override, reason,
                 actor.actor_id, self.now().isoformat(), expires_at.isoformat(),
                 "pending_second_approval", f"grant:{grant_id}"))
            self._event(conn, "emergency.requested", grant_id, actor,
                        {"space_id": space_id, "override": override, "reason": reason})
            return grant_id

    def _second_approval(self, actor: Actor, grant_id: str, status: str, event_type: str) -> None:
        self._require_staff(actor)
        with self._tx() as conn:
            g = conn.execute("select * from emergency_grants where grant_id=?", (grant_id,)).fetchone()
            if g is None:
                raise NotFoundError(grant_id)
            if g["status"] != "pending_second_approval":
                raise StateError(f"放行单状态为 {g['status']}")
            if g["requested_by"] == actor.actor_id:
                raise AuthError("双人控制：第二人不得与申请人为同一人")
            conn.execute(
                f"update emergency_grants set status=?, approved_by=?, approved_at=? where grant_id=?",
                (status, actor.actor_id, self.now().isoformat(), grant_id))
            self._event(conn, event_type, grant_id, actor, {"requester": g["requested_by"]})

    def approve_emergency_grant(self, actor: Actor, grant_id: str) -> None:
        """第二人批准：必须是管理员/安保，且不能是申请人本人。"""
        self._second_approval(actor, grant_id, "active", "emergency.approved")

    def reject_emergency_grant(self, actor: Actor, grant_id: str) -> None:
        self._second_approval(actor, grant_id, "rejected", "emergency.rejected")

    def reclaim_expired_grants(self, now: datetime | None = None) -> list[str]:
        """放行到期回收：未搬入的预订撤销；已搬入的开违规单转强制清退。"""
        now = now or self.now()
        reclaimed: list[str] = []
        with self._tx() as conn:
            grants = conn.execute(
                "select * from emergency_grants where status='active' and expires_at<=?",
                (now.isoformat(),)).fetchall()
            for g in grants:
                conn.execute("update emergency_grants set status='reclaimed' where grant_id=?",
                             (g["grant_id"],))
                for h in conn.execute("select * from holds where grant_id=?", (g["grant_id"],)).fetchall():
                    if h["status"] == "reserved":
                        conn.execute("update holds set status='cancelled' where hold_id=?",
                                     (h["hold_id"],))
                        self._event(conn, "space.released", h["hold_id"], None,
                                    {"reason": "grant_reclaimed"})
                    elif h["status"] in ("placed", "expired") and h["placed_at"]:
                        kind = "FIRE_LANE_BLOCKED" if g["override"] == "fire_lane" else "UNAUTHORIZED_OCCUPANCY"
                        self._record_violation(
                            conn, kind, h["space_id"], h["cargo_id"], h["tenant_id"],
                            f"紧急放行到期（{g['expires_at']}）未离场，需强制清退",
                            actor=None, at=now)
                self._event(conn, "emergency.reclaimed", g["grant_id"], None,
                            {"override": g["override"]}, at=now)
                reclaimed.append(g["grant_id"])
        return reclaimed

    # ---------------------------------------------------------------- 查询/租户可见性

    def space_ledger(self, actor: Actor, space_id: str, at: datetime | None = None) -> dict[str, Any]:
        """空间账视图。租户只能看到自己的货位细节，他人仅返回占用掩码。"""
        at = at or self.now()
        with self.db:
            sp = self._get_space(self.db, space_id)
            holds = self.db.execute(
                "select * from holds where "
                "((status in ('reserved','placed') and start_at<=? and leave_at>?) "
                "or (status='expired' and placed_at is not null)) "
                "and space_id=? order by stack_level, x, y",
                (at.isoformat(), at.isoformat(), space_id)).fetchall()
            items = []
            for h in holds:
                own = actor.tenant_id == h["tenant_id"]
                visible = actor.is_staff or own
                items.append({
                    "hold_id": h["hold_id"] if visible else f"masked-{h['hold_id'][:8]}",
                    "cargo_id": h["cargo_id"] if visible else "***",
                    "tenant_id": h["tenant_id"] if (actor.is_staff or own) else "***",
                    "x": h["x"] if visible else None,
                    "y": h["y"] if visible else None,
                    "level": h["stack_level"] if visible else None,
                    "status": h["status"],
                    "start_at": h["start_at"] if visible else None,
                    "leave_at": h["leave_at"] if visible else None,
                    "occupied": True,
                })
            return {"space_id": space_id, "kind": sp["kind"], "status": sp["status"],
                    "owner_id": sp["owner_id"] if actor.is_staff or sp["owner_id"] == actor.tenant_id else "***",
                    "occupancy": len(items), "items": items}

    def events(self, aggregate_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.db:
            if aggregate_id:
                rows = self.db.execute(
                    "select * from event_log where aggregate_id=? order by occurred_at, rowid limit ?",
                    (aggregate_id, limit)).fetchall()
            else:
                rows = self.db.execute(
                    "select * from event_log order by occurred_at, rowid limit ?",
                    (limit,)).fetchall()
            return [dict(r) for r in rows]

    def violations(self, actor: Actor, open_only: bool = True) -> list[dict[str, Any]]:
        self._require_staff(actor)
        sql = "select * from violations"
        if open_only:
            sql += " where status='open'"
        sql += " order by observed_at"
        with self.db:
            return [dict(r) for r in self.db.execute(sql).fetchall()]

    def ledger_entries(self, actor: Actor, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """租户只查自己的账单；管理员可按租户或全量查。"""
        with self.db:
            if actor.is_staff:
                rows = self.db.execute(
                    "select * from ledger where ifnull(?,'')= '' or tenant_id=? order by rowid",
                    (tenant_id, tenant_id)).fetchall()
            else:
                rows = self.db.execute(
                    "select * from ledger where tenant_id=? order by rowid",
                    (actor.tenant_id,)).fetchall()
            return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 内部工具

    def _require_staff(self, actor: Actor) -> None:
        if not actor.is_staff:
            raise AuthError("该操作仅管理员/安保可执行")

    def _assert_cargo_access(self, actor: Actor, tenant_id: str) -> None:
        if not actor.is_staff and actor.tenant_id != tenant_id:
            raise AuthError("租户不能操作其他商户的货物/货位")

    @staticmethod
    def _assert_not_manual(conn, move_id: str) -> None:
        row = conn.execute("select decision from recovery_decisions where move_id=?",
                           (move_id,)).fetchone()
        if row and row["decision"] == "manual":
            raise StateError("搬运单处于 manual 冻结状态，等待管理员人工了结")

    def _grant_overrides(self, conn, grant_id, tenant_id, space_id, at) -> set[str]:
        if not grant_id:
            return set()
        g = conn.execute("select * from emergency_grants where grant_id=?", (grant_id,)).fetchone()
        if g is None:
            raise NotFoundError(f"紧急放行单不存在：{grant_id}")
        if g["status"] != "active":
            raise StateError(f"紧急放行单状态为 {g['status']}")
        if at >= parse_ts(g["expires_at"]):
            raise StateError("紧急放行已到期")
        if g["space_id"] != "*" and g["space_id"] != space_id:
            raise AuthError("紧急放行不适用于该空间")
        if g["tenant_id"] and g["tenant_id"] != tenant_id:
            raise AuthError("紧急放行不属于该租户")
        return {g["override"]}

    def _charge(self, conn, actor: Actor | None, idem_key: str, tenant_id: str,
                kind: str, ref_type: str, ref_id: str, amount: float,
                at: datetime | None = None) -> None:
        conn.execute(
            "insert into ledger(entry_id,idem_key,tenant_id,kind,ref_type,ref_id,amount,created_at) "
            "values (?,?,?,?,?,?,?,?) on conflict(idem_key) do nothing",
            (_uid("bill"), idem_key, tenant_id, kind, ref_type, ref_id, amount,
             (at or self.now()).isoformat()))
        self._event(conn, "billing.charged", ref_id, actor,
                    {"idem_key": idem_key, "amount": amount, "kind": kind}, at=at)

    def _record_violation(self, conn, kind: str, space_id: str | None, cargo_id: str | None,
                          tenant_id: str | None, detail: str, actor: Actor | None,
                          at: datetime, detail_extra: dict[str, Any] | None = None) -> str | None:
        # 同一未决违规去重，避免重复巡场刷罚单
        dup = conn.execute(
            "select violation_id from violations where kind=? and status='open' and "
            "ifnull(space_id,'')=ifnull(?,'') and ifnull(cargo_id,'')=ifnull(?,'')",
            (kind, space_id, cargo_id)).fetchone()
        if dup:
            return None
        vid = _uid("viol")
        payload: dict[str, Any] = {"detail": detail}
        if detail_extra:
            payload.update(detail_extra)
        conn.execute(
            "insert into violations(violation_id,kind,space_id,cargo_id,tenant_id,detail,status,"
            "observed_by,observed_at) values (?,?,?,?,?,?,'open',?,?)",
            (vid, kind, space_id, cargo_id, tenant_id, json.dumps(payload, ensure_ascii=False),
             actor.actor_id if actor else "system", at.isoformat()))
        self._event(conn, "violation.recorded", vid, actor,
                    {"kind": kind, "space_id": space_id, "cargo_id": cargo_id}, at=at)
        return vid

    def _get_space(self, conn, space_id: str):
        row = conn.execute("select * from spaces where space_id=?", (space_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"空间不存在：{space_id}")
        return row

    def _get_cargo(self, conn, cargo_id: str):
        row = conn.execute("select * from cargo where cargo_id=?", (cargo_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"货物未申报：{cargo_id}")
        return row

    def _get_hold(self, conn, hold_id: str):
        row = conn.execute("select * from holds where hold_id=?", (hold_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"占位不存在：{hold_id}")
        return row

    def _get_move(self, conn, move_id: str):
        row = conn.execute("select * from move_tasks where move_id=?", (move_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"搬运任务不存在：{move_id}")
        return row

    def _get_step(self, conn, move_id: str, step_no: int):
        row = conn.execute("select * from move_steps where move_id=? and step_no=?",
                           (move_id, step_no)).fetchone()
        if row is None:
            raise NotFoundError(f"步骤不存在：{move_id}:{step_no}")
        return row

    def _steps(self, conn, move_id: str):
        return conn.execute("select * from move_steps where move_id=? order by step_no",
                            (move_id,)).fetchall()

    def _load_spaces(self, conn) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in conn.execute("select * from spaces").fetchall():
            d = dict(r)
            try:
                attrs = json.loads(r["attrs"])
                for key in ("closed_from", "closed_until"):
                    if key in attrs:
                        attrs[key] = parse_ts(attrs[key])
                d.update(attrs)
            except (TypeError, ValueError):
                pass
            out[d["space_id"]] = d
        return out

    def _load_taboos(self, conn) -> dict[tuple[str, str], float]:
        return {(r["category_a"], r["category_b"]): r["min_gap"]
                for r in conn.execute("select * from adjacency_taboos").fetchall()}

    def _load_placements(self, conn) -> list[engine.Placement]:
        """容量视角的有效占用。

        reserved/placed 按预订区间计入（未来容量已卖）；已搬入但超期
        （expired + placed_at）的货物物理上仍在场，离场时间延展到远期，
        在真正 released 前继续阻塞容量与消防判定。
        """
        rows = conn.execute("""
            select h.*, c.length, c.width, c.height, c.weight, c.category,
                   c.stackable, c.max_layers, c.bear_load
            from holds h join cargo c on c.cargo_id=h.cargo_id
            where h.status in ('reserved','placed')
               or (h.status='expired' and h.placed_at is not null)
        """).fetchall()
        result = []
        for r in rows:
            leave = parse_ts(r["leave_at"])
            if r["status"] == "expired" and r["placed_at"]:
                leave = FAR_FUTURE
            result.append(engine.Placement(
                hold_id=r["hold_id"], space_id=r["space_id"], tenant_id=r["tenant_id"],
                cargo_id=r["cargo_id"], x=r["x"], y=r["y"], level=r["stack_level"],
                start=parse_ts(r["start_at"]), leave=leave,
                length=r["length"], width=r["width"], height=r["height"], weight=r["weight"],
                category=r["category"], stackable=bool(r["stackable"]),
                max_layers=r["max_layers"], bear_load=r["bear_load"]))
        return result

    @staticmethod
    def _candidate(cargo, sp, local_x, local_y, level, start_at, leave_at, *,
                   hold_id: str, tenant_id: str) -> engine.Placement:
        return engine.Placement(
            hold_id=hold_id, space_id=sp["space_id"], tenant_id=tenant_id,
            cargo_id=cargo["cargo_id"], x=sp["x"] + local_x, y=sp["y"] + local_y,
            level=level, start=start_at, leave=leave_at,
            length=cargo["length"], width=cargo["width"], height=cargo["height"],
            weight=cargo["weight"], category=cargo["category"],
            stackable=bool(cargo["stackable"]), max_layers=cargo["max_layers"],
            bear_load=cargo["bear_load"])

    def _placement_from_hold(self, hold, cargo, hold_id: str) -> engine.Placement:
        return engine.Placement(
            hold_id=hold_id, space_id=hold["space_id"], tenant_id=hold["tenant_id"],
            cargo_id=hold["cargo_id"], x=hold["x"], y=hold["y"], level=hold["stack_level"],
            start=parse_ts(hold["start_at"]), leave=parse_ts(hold["leave_at"]),
            length=cargo["length"], width=cargo["width"], height=cargo["height"],
            weight=cargo["weight"], category=cargo["category"],
            stackable=bool(cargo["stackable"]), max_layers=cargo["max_layers"],
            bear_load=cargo["bear_load"])
