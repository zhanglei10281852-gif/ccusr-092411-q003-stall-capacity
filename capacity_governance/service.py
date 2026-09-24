"""容量治理核心服务。

把安全边界与经营安排放进同一本“空间账”：

* 接货判断：固定档口 / 临时区域 / 货架层位的面积、承重、限高、品类准入、
  货物尺寸重量、可叠放性、相邻禁忌、预计离场时间一起参与校验；
  planned/approved 即冻结容量，时间轴峰值核算保证并发占位不穿透容量。
* 计划调整：销售加快、车辆延误、区域封闭只能改“尚未搬入”的计划。
* 安全：消防通道占用必须持有两人批准、期限覆盖在场窗口的紧急放行，
  到期由 sweep 回收；管理员可报告越权占位、强制清退并生成可执行腾挪顺序。
* 留痕：货物跨区域每一步都追加事件；重启恢复对 moving 任务裁定
  继续 / 撤销 / 待人工确认，不重复释放空间或计费。
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .errors import (
    ApprovalError,
    CapacityExceeded,
    DomainError,
    NoSpaceAvailable,
    NotFound,
    PermissionDenied,
    SafetyViolation,
    StateError,
    TabooViolation,
    ValidationError,
)
from .models import (
    CargoLot,
    Dimensions,
    EmergencyPermit,
    MoveTask,
    ReviewDecision,
    Space,
    SpaceKind,
    SpaceState,
    TaskState,
    require_aware,
)
from .storage import Store

EPS = 1e-6
FAR_FUTURE = datetime(2126, 1, 1, tzinfo=timezone.utc)

# 在合同首版基础上追加的事件类型（version_policy: 新版本追加）
EXTRA_EVENT_TYPES = (
    "permit.requested",
    "permit.approved",
    "permit.granted",
    "permit.expired",
    "permit.revoked",
    "move.approved",
    "move.interrupted",
    "move.reviewed",
    "plan.adjusted",
    "plan.cancelled",
    "plan.expired",
    "space.force_released",
    "encroachment.reported",
    "encroachment.resolved",
)


# --------------------------------------------------------------------- #
# 行 → 领域对象
# --------------------------------------------------------------------- #

def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _frozen(value: str) -> frozenset[str]:
    return frozenset(v for v in value.split(",") if v)


def row_to_space(row) -> Space:
    return Space(
        space_id=row["space_id"],
        kind=SpaceKind(row["kind"]),
        name=row["name"],
        area_m2=row["area_m2"],
        max_weight_kg=row["max_weight_kg"],
        max_height_m=row["max_height_m"],
        is_fire_lane=bool(row["is_fire_lane"]),
        state=SpaceState(row["state"]),
        owner_tenant_id=row["owner_tenant_id"],
        parent_space_id=row["parent_space_id"],
        allowed_categories=_frozen(row["allowed_categories"]),
    )


def row_to_lot(row) -> CargoLot:
    return CargoLot(
        lot_id=row["lot_id"],
        tenant_id=row["tenant_id"],
        category=row["category"],
        dimensions=Dimensions(
            row["length_m"], row["width_m"], row["height_m"], row["weight_kg"]
        ),
        stackable=bool(row["stackable"]),
        stacks_on=_frozen(row["stacks_on"]),
        taboo_adjacent=_frozen(row["taboo_adjacent"]),
        declared_at=_dt(row["declared_at"]),
        expected_departure=_dt(row["expected_departure"]),
    )


def row_to_task(row) -> MoveTask:
    return MoveTask(
        task_id=row["task_id"],
        lot_id=row["lot_id"],
        tenant_id=row["tenant_id"],
        from_space_id=row["from_space_id"],
        to_space_id=row["to_space_id"],
        planned_in_at=_dt(row["planned_in_at"]),
        planned_out_at=_dt(row["planned_out_at"]),
        state=TaskState(row["state"]),
        started_at=_dt(row["started_at"]),
        completed_at=_dt(row["completed_at"]),
        actual_out_at=_dt(row["actual_out_at"]),
        review_decision=ReviewDecision(row["review_decision"]) if row["review_decision"] else None,
        stacked_on_task=row["stacked_on_task"],
        permit_id=row["permit_id"],
    )


def row_to_permit(row) -> EmergencyPermit:
    return EmergencyPermit(
        permit_id=row["permit_id"],
        space_id=row["space_id"],
        reason=row["reason"],
        requested_by=row["requested_by"],
        approvers=(row["approver_a"], row["approver_b"]),
        valid_from=_dt(row["valid_from"]),
        valid_until=_dt(row["valid_until"]),
        state=row["state"],
    )


# --------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------- #

class CapacityService:
    def __init__(
        self,
        store: Store,
        *,
        usage_rate_per_m2_hour: float = 1.0,
        clearance_fee: float = 200.0,
        overstay_penalty_ratio: float = 0.5,
        clock=None,
    ) -> None:
        self.store = store
        self.rate = usage_rate_per_m2_hour
        self.clearance_fee = clearance_fee
        self.overstay_ratio = overstay_penalty_ratio
        self._clock = clock
        self._tx_depth = threading.local()

    # ---- 通用 ---------------------------------------------------------

    def now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(timezone.utc)

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """可重入事务：同线程内层调用并入外层事务，仅最外层提交/回滚。"""
        with self.store.lock:
            depth = getattr(self._tx_depth, "depth", 0)
            if depth == 0:
                self.store.begin_immediate()
            self._tx_depth.depth = depth + 1
            try:
                yield
                if depth == 0:
                    self.store.commit()
            except Exception:
                if depth == 0:
                    self.store.rollback()
                raise
            finally:
                self._tx_depth.depth = depth

    def _replayed(self, request_id: str | None) -> dict | None:
        if not request_id:
            return None
        row = self.store.find_event_by_request(request_id)
        return json.loads(row["payload"]) if row else None

    def _event(self, event_type: str, aggregate_id: str, actor: str,
               payload: dict, at: datetime, request_id: str | None = None) -> None:
        self.store.append_event(
            event_type, aggregate_id, actor, payload, at, request_id=request_id
        )

    def _require_actor(self, actor: str) -> None:
        if not self.store.tenant_exists(actor):
            raise PermissionDenied(f"未知操作者：{actor}")

    def _is_admin(self, actor: str) -> bool:
        return self.store.is_admin(actor)

    def _require_admin(self, actor: str) -> None:
        self._require_actor(actor)
        if not self._is_admin(actor):
            raise PermissionDenied("该操作仅管理员可执行")

    def _space(self, space_id: str) -> Space:
        row = self.store.get_space_row(space_id)
        if row is None:
            raise NotFound(f"空间不存在：{space_id}")
        return row_to_space(row)

    def _lot(self, lot_id: str) -> CargoLot:
        row = self.store.get_lot_row(lot_id)
        if row is None:
            raise NotFound(f"货物批次不存在：{lot_id}")
        return row_to_lot(row)

    def _task(self, task_id: str) -> MoveTask:
        row = self.store.get_task_row(task_id)
        if row is None:
            raise NotFound(f"搬运任务不存在：{task_id}")
        return row_to_task(row)

    def _task_row(self, task_id: str):
        row = self.store.get_task_row(task_id)
        if row is None:
            raise NotFound(f"搬运任务不存在：{task_id}")
        return row

    # ---- 基础资料登记 --------------------------------------------------

    def register_tenant(self, tenant_id: str, name: str, is_admin: bool = False) -> None:
        with self._tx():
            self.store.add_tenant(tenant_id, name, is_admin)

    def define_space(self, *, space_id: str, kind: str, name: str, area_m2: float,
                     max_weight_kg: float, max_height_m: float,
                     is_fire_lane: bool = False, owner_tenant_id: str | None = None,
                     parent_space_id: str | None = None,
                     allowed_categories: tuple[str, ...] = ()) -> None:
        if area_m2 <= 0 or max_weight_kg <= 0 or max_height_m <= 0:
            raise ValidationError("空间面积、承重、限高必须为正")
        with self._tx():
            self.store.add_space({
                "space_id": space_id, "kind": kind, "name": name,
                "area_m2": area_m2, "max_weight_kg": max_weight_kg,
                "max_height_m": max_height_m, "is_fire_lane": is_fire_lane,
                "owner_tenant_id": owner_tenant_id,
                "parent_space_id": parent_space_id,
                "allowed_categories": allowed_categories,
            })

    def add_adjacency(self, space_id: str, neighbor_id: str) -> None:
        with self._tx():
            if self.store.get_space_row(space_id) is None or \
                    self.store.get_space_row(neighbor_id) is None:
                raise NotFound("相邻空间不存在")
            self.store.add_adjacency(space_id, neighbor_id)

    def close_space(self, space_id: str, actor: str) -> None:
        """区域封闭：空间拒绝新的接货与到达，只能调整尚未搬入的计划。"""
        self._require_admin(actor)
        with self._tx():
            self._space(space_id)
            self.store.set_space_state(space_id, SpaceState.CLOSED.value)
            self.store.audit(actor, "space.closed", "space", space_id,
                             {"space_id": space_id}, self.now())

    def declare_lot(self, *, lot_id: str, tenant_id: str, category: str,
                    length_m: float, width_m: float, height_m: float,
                    weight_kg: float, stackable: bool = False,
                    stacks_on: tuple[str, ...] = (),
                    taboo_adjacent: tuple[str, ...] = (),
                    expected_departure: datetime | None = None,
                    request_id: str | None = None) -> str:
        if min(length_m, width_m, height_m, weight_kg) <= 0:
            raise ValidationError("尺寸与重量必须为正")
        with self._tx():
            prior = self._replayed(request_id)
            if prior is not None:
                return prior["lot_id"]
            if not self.store.tenant_exists(tenant_id):
                raise NotFound(f"租户不存在：{tenant_id}")
            if self.store.get_lot_row(lot_id) is not None:
                raise ValidationError(f"货物批次已存在：{lot_id}")
            if expected_departure is not None:
                require_aware(expected_departure, "预计离场时间")
            now = self.now()
            self.store.insert_lot({
                "lot_id": lot_id, "tenant_id": tenant_id, "category": category,
                "length_m": length_m, "width_m": width_m, "height_m": height_m,
                "weight_kg": weight_kg, "stackable": stackable,
                "stacks_on": stacks_on, "taboo_adjacent": taboo_adjacent,
                "declared_at": now, "expected_departure": expected_departure,
            })
            self._event("cargo.declared", lot_id, tenant_id, {
                "lot_id": lot_id, "tenant_id": tenant_id, "category": category,
                "footprint_m2": round(length_m * width_m, 6),
                "weight_kg": weight_kg, "stackable": stackable,
                "expected_departure": expected_departure.isoformat()
                if expected_departure else None,
            }, now, request_id)
            return lot_id

    # ---- 容量时间轴 ----------------------------------------------------

    def _occupancy_slices(self, space_id: str, *, now: datetime | None = None,
                          exclude_task: str | None = None) -> list:
        from .models import UsageSlice

        now = now or self.now()
        slices: list[UsageSlice] = []
        for row in self.store.tasks_touching_space(space_id):
            if exclude_task and row["task_id"] == exclude_task:
                continue
            t = row_to_task(row)
            if t.state == TaskState.PLACED:
                start = t.completed_at or t.planned_in_at
                if t.actual_out_at is not None:
                    end = t.actual_out_at
                elif t.planned_out_at > now:
                    end = t.planned_out_at
                else:
                    end = FAR_FUTURE  # 超期未离场：按仍在场保守核算
            else:
                start, end = t.planned_in_at, t.planned_out_at
            if end <= start:
                continue
            lot = row_to_lot(self.store.get_lot_row(t.lot_id))
            stacked = False
            if t.stacked_on_task:
                base = self.store.get_task_row(t.stacked_on_task)
                if base and TaskState(base["state"]).occupies:
                    stacked = True  # 底座在场时不重复占地面，承重仍计
            slices.append(UsageSlice(
                task_id=t.task_id, tenant_id=t.tenant_id, lot_id=t.lot_id,
                category=lot.category,
                footprint=0.0 if stacked else lot.dimensions.footprint_m2,
                weight=lot.dimensions.weight_kg,
                starts=start, ends=end, stacked=stacked,
            ))
        return slices

    @staticmethod
    def _peak(slices, start: datetime, end: datetime) -> tuple[float, float]:
        """窗口 [start, end) 内同时占用的面积/承重峰值。"""
        points = {start, end}
        for s in slices:
            if s.ends > start and s.starts < end:
                points.add(max(s.starts, start))
                points.add(min(s.ends, end))
        ordered = sorted(points)
        best_area = best_weight = 0.0
        for a, b in zip(ordered, ordered[1:]):
            if b <= a:
                continue
            mid = a + (b - a) / 2
            area = sum(
                s.footprint for s in slices
                if s.footprint and s.starts <= mid < s.ends
            )
            weight = sum(
                s.weight for s in slices if s.starts <= mid < s.ends
            )
            best_area = max(best_area, area)
            best_weight = max(best_weight, weight)
        return best_area, best_weight

    # ---- 接货可行性 ----------------------------------------------------

    def _check_permit(self, space: Space, permit_id: str | None,
                      starts: datetime, ends: datetime) -> None:
        if not space.is_fire_lane:
            if permit_id:
                raise ValidationError("非消防通道无需紧急放行许可")
            return
        if not permit_id:
            raise SafetyViolation(
                f"{space.name}（{space.space_id}）属于消防通道，须持有效紧急放行许可"
            )
        row = self.store.get_permit_row(permit_id)
        if row is None or row["state"] != "granted":
            raise SafetyViolation("紧急放行许可不存在或未生效")
        permit = row_to_permit(row)
        if permit.space_id not in (None, space.space_id):
            raise SafetyViolation("许可不适用于该空间")
        if permit.valid_from > starts or permit.valid_until < ends:
            raise SafetyViolation("许可期限必须覆盖整个在场窗口，到期后空间将被回收")

    def _check_taboo(self, space: Space, lot: CargoLot,
                     starts: datetime, ends: datetime,
                     exclude_task: str | None) -> None:
        for neighbor_id in self.store.neighbors_of(space.space_id):
            for s in self._occupancy_slices(neighbor_id, exclude_task=exclude_task):
                if s.ends <= starts or s.starts >= ends:
                    continue
                if s.category in lot.taboo_adjacent:
                    raise TabooViolation(
                        f"{lot.category} 与相邻区域 {neighbor_id} 在场的 "
                        f"{s.category} 存在相邻禁忌"
                    )
                neighbor_lot_row = self.store.get_lot_row(s.lot_id)
                if neighbor_lot_row and lot.category in _frozen(
                        neighbor_lot_row["taboo_adjacent"]):
                    raise TabooViolation(
                        f"相邻区域 {neighbor_id} 在场的 {s.category} 禁忌 {lot.category}"
                    )

    def validate_intake(self, *, space_id: str, lot_id: str,
                        starts: datetime, ends: datetime,
                        permit_id: str | None = None,
                        stack_on_task_id: str | None = None,
                        actor: str | None = None,
                        exclude_task: str | None = None) -> None:
        """接货判断的完整规则集合；不通过即抛 DomainError 子类。"""
        require_aware(starts, "计划入场时间")
        require_aware(ends, "预计离场时间")
        if starts >= ends:
            raise ValidationError("离场时间必须晚于入场时间")
        space = self._space(space_id)
        lot = self._lot(lot_id)
        d = lot.dimensions

        if space.state is SpaceState.CLOSED:
            raise SafetyViolation(f"区域 {space.space_id} 已封闭，不再接收货物")
        if actor and not self._is_admin(actor):
            if space.owner_tenant_id and space.owner_tenant_id != lot.tenant_id:
                raise PermissionDenied("该固定档口归属其他商户")
        if space.allowed_categories and lot.category not in space.allowed_categories:
            raise SafetyViolation(f"{space.name} 不允许品类 {lot.category}")
        if lot.expected_departure and ends > lot.expected_departure:
            raise ValidationError("在场窗口超出货物的预计离场时间")

        # 叠放校验：沿底座链逐层核对“上层货物可压在该层类别上”，
        # 并累计整摞高度与在场窗口
        stacked_height = d.height_m
        if stack_on_task_id:
            if stack_on_task_id == exclude_task:
                raise ValidationError("不能叠放在自身任务上")
            upper_lot = lot
            cur_id: str | None = stack_on_task_id
            chain_height = 0.0
            chain_end, chain_start = ends, starts
            seen: set[str] = set()
            while cur_id:
                if cur_id in seen:
                    raise ValidationError("叠放关系存在循环")
                seen.add(cur_id)
                base_row = self._task_row(cur_id)
                if base_row["to_space_id"] != space.space_id:
                    raise ValidationError("叠放底座不在同一空间")
                if not TaskState(base_row["state"]).occupies:
                    raise ValidationError("底座任务已不占容量，无法叠放")
                base_lot = self._lot(base_row["lot_id"])
                base_task = row_to_task(base_row)
                if not base_lot.stackable:
                    raise SafetyViolation(
                        f"底座货物 {base_lot.category} 不可承压叠放"
                    )
                if base_lot.category not in upper_lot.stacks_on:
                    raise SafetyViolation(
                        f"{upper_lot.category} 不可叠放于 "
                        f"{base_lot.category} 之上"
                    )
                chain_height += base_lot.dimensions.height_m
                chain_end = min(chain_end, base_task.planned_out_at)
                chain_start = max(chain_start, base_task.planned_in_at)
                upper_lot = base_lot
                cur_id = base_task.stacked_on_task
            if chain_end < ends or chain_start > starts:
                raise ValidationError("叠放底座无法覆盖本批货物的完整在场窗口")
            stacked_height = d.height_m + chain_height
        elif d.footprint_m2 > space.area_m2 + EPS:
            raise CapacityExceeded("货物占地超过空间总面积")

        if stacked_height > space.max_height_m + EPS:
            raise CapacityExceeded(
                f"高度 {stacked_height}m 超过限高 {space.max_height_m}m"
            )
        if d.weight_kg > space.max_weight_kg + EPS:
            raise CapacityExceeded("货物重量超过空间最大承重")

        # 时间轴容量峰值
        slices = self._occupancy_slices(space.space_id, exclude_task=exclude_task)
        from .models import UsageSlice
        extra = UsageSlice(
            task_id="(new)", tenant_id=lot.tenant_id, lot_id=lot.lot_id,
            category=lot.category,
            footprint=0.0 if stack_on_task_id else d.footprint_m2,
            weight=d.weight_kg, starts=starts, ends=ends,
            stacked=bool(stack_on_task_id),
        )
        peak_area, peak_weight = self._peak(slices + [extra], starts, ends)
        if peak_area > space.area_m2 + EPS:
            raise CapacityExceeded(
                f"面积时间轴峰值 {peak_area:.2f}m² 超过 {space.area_m2}m²"
            )
        if peak_weight > space.max_weight_kg + EPS:
            raise CapacityExceeded(
                f"承重时间轴峰值 {peak_weight:.0f}kg 超过 {space.max_weight_kg}kg"
            )

        self._check_taboo(space, lot, starts, ends, exclude_task)
        self._check_permit(space, permit_id, starts, ends)

    def find_options(self, *, lot_id: str, starts: datetime, ends: datetime,
                     actor: str) -> list[dict[str, Any]]:
        """枚举通过接货判断的空间，按剩余容量降序，供经营安排选择。"""
        self._require_actor(actor)
        lot = self._lot(lot_id)
        options: list[dict[str, Any]] = []
        with self._tx():
            for row in self.store.list_space_rows():
                space = row_to_space(row)
                try:
                    self.validate_intake(
                        space_id=space.space_id, lot_id=lot_id,
                        starts=starts, ends=ends, actor=actor,
                    )
                except DomainError:
                    continue
                slices = self._occupancy_slices(space.space_id)
                peak_area, peak_weight = self._peak(slices, starts, ends)
                options.append({
                    "space_id": space.space_id, "name": space.name,
                    "kind": space.kind.value,
                    "free_area_m2": round(space.area_m2 - peak_area, 3),
                    "free_weight_kg": round(space.max_weight_kg - peak_weight, 1),
                })
        options.sort(key=lambda o: (-o["free_area_m2"], -o["free_weight_kg"],
                                    o["space_id"]))
        return options

    # ---- 接货计划 ------------------------------------------------------

    def plan_move(self, *, task_id: str, lot_id: str, to_space_id: str,
                  planned_in_at: datetime, planned_out_at: datetime,
                  actor: str, from_space_id: str | None = None,
                  permit_id: str | None = None,
                  stack_on_task_id: str | None = None,
                  request_id: str | None = None) -> str:
        self._require_actor(actor)
        with self._tx():
            prior = self._replayed(request_id)
            if prior is not None:
                return prior["task_id"]
            lot = self._lot(lot_id)
            if lot.tenant_id != actor and not self._is_admin(actor):
                raise PermissionDenied("只能为自己的货物安排接货")
            if self.store.get_task_row(task_id) is not None:
                raise ValidationError(f"任务已存在：{task_id}")
            self.validate_intake(
                space_id=to_space_id, lot_id=lot_id,
                starts=planned_in_at, ends=planned_out_at,
                permit_id=permit_id, stack_on_task_id=stack_on_task_id, actor=actor,
            )
            target = self._space(to_space_id)
            auto_approved = self._is_admin(actor) or (
                target.owner_tenant_id == actor
            )
            state = TaskState.APPROVED if auto_approved else TaskState.PLANNED
            self.store.insert_task({
                "task_id": task_id, "lot_id": lot_id, "tenant_id": lot.tenant_id,
                "from_space_id": from_space_id, "to_space_id": to_space_id,
                "planned_in_at": planned_in_at, "planned_out_at": planned_out_at,
                "state": state.value,
                "stacked_on_task": stack_on_task_id,
                "permit_id": permit_id, "request_id": request_id,
            })
            now = self.now()
            payload = {
                "task_id": task_id, "lot_id": lot_id, "space_id": to_space_id,
                "tenant_id": lot.tenant_id,
                "planned_in_at": planned_in_at.isoformat(),
                "planned_out_at": planned_out_at.isoformat(),
                "permit_id": permit_id, "stacked_on_task": stack_on_task_id,
            }
            self._event("space.held", to_space_id, actor, payload, now, request_id)
            if auto_approved:
                self._event("move.approved", task_id, actor,
                            {"task_id": task_id, "auto": True}, now)
            return task_id

    def approve_plan(self, task_id: str, actor: str) -> None:
        """管理员（临时区域）或档口业主（自有固定档口）批准接货。"""
        self._require_actor(actor)
        with self._tx():
            t = self._task(task_id)
            if t.state is not TaskState.PLANNED:
                raise StateError(f"任务状态 {t.state.value} 不可审批")
            target = self._space(t.to_space_id)
            if not self._is_admin(actor) and target.owner_tenant_id != actor:
                raise PermissionDenied("无权批准该空间的接货")
            # 批准时按最新计划重新校验容量与安全
            self.validate_intake(
                space_id=t.to_space_id, lot_id=t.lot_id,
                starts=t.planned_in_at, ends=t.planned_out_at,
                permit_id=t.permit_id, stack_on_task_id=t.stacked_on_task,
                actor=actor, exclude_task=task_id,
            )
            self.store.update_task_state(task_id, TaskState.APPROVED.value)
            self._event("move.approved", task_id, actor,
                        {"task_id": task_id}, self.now())

    def _require_not_moved_in(self, t: MoveTask) -> None:
        if not t.state.not_yet_moved_in:
            raise StateError(
                f"任务已处于 {t.state.value}，只能调整尚未搬入的计划"
            )

    def reschedule_plan(self, task_id: str, actor: str, *,
                        new_in_at: datetime, new_out_at: datetime,
                        request_id: str | None = None) -> None:
        """销售加快（提前离场）或车辆延误（推迟入场）：只改未搬入的计划。"""
        self._require_actor(actor)
        with self._tx():
            if self._replayed(request_id) is not None:
                return
            t = self._task(task_id)
            self._require_not_moved_in(t)
            if t.tenant_id != actor and not self._is_admin(actor):
                raise PermissionDenied("无权调整他人计划")
            self.validate_intake(
                space_id=t.to_space_id, lot_id=t.lot_id,
                starts=new_in_at, ends=new_out_at, permit_id=t.permit_id,
                stack_on_task_id=t.stacked_on_task, actor=actor,
                exclude_task=task_id,
            )
            self.store.update_task_state(
                task_id, t.state.value,
                planned_in_at=new_in_at, planned_out_at=new_out_at,
            )
            self._event("plan.adjusted", task_id, actor, {
                "task_id": task_id, "lot_id": t.lot_id, "kind": "reschedule",
                "planned_in_at": new_in_at.isoformat(),
                "planned_out_at": new_out_at.isoformat(),
            }, self.now(), request_id)

    def retarget_plan(self, task_id: str, actor: str, *,
                      new_space_id: str, permit_id: str | None = None,
                      stack_on_task_id: str | None = None,
                      request_id: str | None = None) -> None:
        """区域封闭后把尚未搬入的计划改投其他空间（冻结容量随之迁移）。"""
        self._require_actor(actor)
        with self._tx():
            if self._replayed(request_id) is not None:
                return
            t = self._task(task_id)
            self._require_not_moved_in(t)
            if t.tenant_id != actor and not self._is_admin(actor):
                raise PermissionDenied("无权调整他人计划")
            self.validate_intake(
                space_id=new_space_id, lot_id=t.lot_id,
                starts=t.planned_in_at, ends=t.planned_out_at,
                permit_id=permit_id, stack_on_task_id=stack_on_task_id,
                actor=actor, exclude_task=task_id,
            )
            self.store.update_task_state(
                task_id, t.state.value,
                to_space_id=new_space_id, permit_id=permit_id,
                stacked_on_task=stack_on_task_id,
            )
            self._event("plan.adjusted", task_id, actor, {
                "task_id": task_id, "kind": "retarget",
                "from_space_id": t.to_space_id, "space_id": new_space_id,
                "permit_id": permit_id,
            }, self.now(), request_id)

    def cancel_plan(self, task_id: str, actor: str, *,
                    request_id: str | None = None) -> None:
        self._require_actor(actor)
        with self._tx():
            if self._replayed(request_id) is not None:
                return
            t = self._task(task_id)
            self._require_not_moved_in(t)
            if t.tenant_id != actor and not self._is_admin(actor):
                raise PermissionDenied("无权取消他人计划")
            self.store.update_task_state(task_id, TaskState.CANCELLED.value)
            self._event("plan.cancelled", task_id, actor, {
                "task_id": task_id, "space_id": t.to_space_id,
                "frozen_capacity_released": True,
            }, self.now(), request_id)
            self._event("space.released", t.to_space_id, actor, {
                "task_id": task_id, "lot_id": t.lot_id,
                "reason": "plan_cancelled",
            }, self.now(), request_id)

    # ---- 搬运执行（跨区域每一步留痕）------------------------------------

    def _release_source_for_move(self, move_task: MoveTask, at: datetime) -> None:
        """货物已离开来源空间：据实结算来源在场段并结束分录。"""
        prior_placed = None
        for row in self.store.tasks_for_lot(move_task.lot_id):
            if row["task_id"] == move_task.task_id:
                continue
            if row["state"] == TaskState.PLACED.value and \
                    row["to_space_id"] == move_task.from_space_id:
                prior_placed = row
                break
        if prior_placed is None:
            return
        prior_task = row_to_task(prior_placed)
        prior_lot = self._lot(move_task.lot_id)
        self.store.update_task_state(
            prior_placed["task_id"], TaskState.RELEASED.value, actual_out_at=at
        )
        self._usage_charge(prior_task, prior_lot, at, forced=False)
        self._event("space.released", prior_placed["to_space_id"], move_task.tenant_id, {
            "task_id": prior_placed["task_id"], "lot_id": move_task.lot_id,
            "reason": "move_source", "move_task_id": move_task.task_id,
            "actual_out_at": at.isoformat(),
        }, at)

    def start_move(self, task_id: str, *, actor: str | None = None,
                   at: datetime | None = None) -> None:
        at = at or self.now()
        with self._tx():
            t = self._task(task_id)
            if actor:
                self._require_actor(actor)
                if t.tenant_id != actor and not self._is_admin(actor):
                    raise PermissionDenied("无权启动他人任务")
            if t.state not in (TaskState.APPROVED, TaskState.PENDING_REVIEW):
                raise StateError(f"任务状态 {t.state.value} 不可开始搬运")
            self.store.update_task_state(
                task_id, TaskState.MOVING.value, started_at=at,
                review_decision=None,
            )
            self._event("move.started", task_id, actor or t.tenant_id, {
                "task_id": task_id, "lot_id": t.lot_id,
                "from_space_id": t.from_space_id, "space_id": t.to_space_id,
            }, at)
            if t.from_space_id:
                self._release_source_for_move(t, at)

    def complete_move(self, task_id: str, *, at: datetime | None = None) -> None:
        at = at or self.now()
        with self._tx():
            t = self._task(task_id)
            if t.state is not TaskState.MOVING:
                raise StateError(f"任务状态 {t.state.value} 不可完成搬运")
            target = self._space(t.to_space_id)
            if target.state is SpaceState.CLOSED:
                raise SafetyViolation(
                    "目标区域已封闭，货物不得入场；请联系管理员改投或人工裁定"
                )
            # 到达时按最新现场重校：许可可能已到期，容量可能被超期占用挤压
            self.validate_intake(
                space_id=t.to_space_id, lot_id=t.lot_id,
                starts=at, ends=max(t.planned_out_at, at),
                permit_id=t.permit_id, stack_on_task_id=t.stacked_on_task,
                exclude_task=task_id,
            )
            self.store.update_task_state(
                task_id, TaskState.PLACED.value, completed_at=at
            )
            self._event("move.completed", task_id, t.tenant_id, {
                "task_id": task_id, "lot_id": t.lot_id,
                "space_id": t.to_space_id, "arrived_at": at.isoformat(),
            }, at)

    def _usage_charge(self, t: MoveTask, lot: CargoLot,
                      out_at: datetime, *, forced: bool) -> None:
        in_at = t.completed_at or t.planned_in_at
        hours = max((out_at - in_at).total_seconds() / 3600.0, 1.0)
        amount = round(lot.dimensions.footprint_m2 * self.rate * hours, 2)
        self.store.add_charge(t.task_id, t.tenant_id, amount, "usage", out_at)
        if forced:
            self.store.add_charge(
                t.task_id, t.tenant_id, self.clearance_fee, "clearance", out_at
            )

    def release_lot(self, task_id: str, *, at: datetime | None = None) -> None:
        """正常离场：placed → released，按实际在场时长计费一次。"""
        at = at or self.now()
        with self._tx():
            t = self._task(task_id)
            if t.state is not TaskState.PLACED:
                raise StateError(f"任务状态 {t.state.value}，无在场货物可放行")
            lot = self._lot(t.lot_id)
            self.store.update_task_state(
                task_id, TaskState.RELEASED.value, actual_out_at=at
            )
            self._usage_charge(t, lot, at, forced=False)
            self._event("space.released", t.to_space_id, t.tenant_id, {
                "task_id": task_id, "lot_id": t.lot_id,
                "actual_out_at": at.isoformat(),
            }, at)

    # ---- 异常重启恢复 --------------------------------------------------

    def recover(self, *, at: datetime | None = None) -> list[str]:
        """重启后对 moving 任务逐一判定：

        事件日志显示已完成而投影异常 → 直接补登 placed（不重复计费）；
        否则置 pending_review，容量继续冻结，等待人工裁定。
        """
        at = at or self.now()
        pending: list[str] = []
        with self._tx():
            for row in self.store.inflight_tasks():
                task_id = row["task_id"]
                if row["state"] != TaskState.MOVING.value:
                    continue
                completed = any(
                    e["event_type"] == "move.completed"
                    for e in self.store.events_for(task_id)
                )
                if completed:
                    self.store.update_task_state(
                        task_id, TaskState.PLACED.value,
                        completed_at=at,
                    )
                    continue
                self.store.update_task_state(
                    task_id, TaskState.PENDING_REVIEW.value
                )
                self._event("move.interrupted", task_id, "system", {
                    "task_id": task_id, "lot_id": row["lot_id"],
                    "from_space_id": row["from_space_id"],
                    "space_id": row["to_space_id"],
                }, at)
                pending.append(task_id)
        return pending

    def review_inflight(self, task_id: str, actor: str,
                        decision: ReviewDecision, *,
                        at: datetime | None = None) -> None:
        """对异常中断的搬运做人工裁定：继续 / 撤销 / 保持待确认。"""
        self._require_admin(actor)
        at = at or self.now()
        with self._tx():
            t = self._task(task_id)
            if t.state is not TaskState.PENDING_REVIEW:
                raise StateError("只有待人工确认的任务可裁定")
            if decision is ReviewDecision.CONTINUE:
                target = self._space(t.to_space_id)
                if target.state is SpaceState.CLOSED:
                    raise SafetyViolation("目标区域已封闭，不能继续入场")
                # 恢复期间现场可能变化：目标容量、相邻禁忌、放行许可全部重校
                self.validate_intake(
                    space_id=t.to_space_id, lot_id=t.lot_id,
                    starts=at, ends=t.planned_out_at, permit_id=t.permit_id,
                    stack_on_task_id=t.stacked_on_task,
                    actor=actor, exclude_task=t.task_id,
                )
                self.store.update_task_state(
                    task_id, TaskState.PLACED.value,
                    completed_at=at, review_decision=decision.value,
                )
                self._event("move.completed", task_id, actor, {
                    "task_id": task_id, "review": "continue",
                    "arrived_at": at.isoformat(),
                }, at)
            elif decision is ReviewDecision.REVOKE:
                # 归还来源空间：找到因本任务而结束的来源分录并恢复；
                # 恢复前重校来源容量，恢复期间可能已有新占位
                restored: list[str] = []
                skipped: list[str] = []
                for e in self.store.all_events():
                    if e["event_type"] != "space.released":
                        continue
                    p = json.loads(e["payload"])
                    if p.get("move_task_id") == task_id and \
                            p.get("reason") == "move_source":
                        source_task = self._task(p["task_id"])
                        if source_task.planned_out_at <= at:
                            # 来源计划窗口已过：不再恢复占位，留待人工盘点
                            skipped.append(p["task_id"])
                            continue
                        source_lot = self._lot(source_task.lot_id)
                        self.validate_intake(
                            space_id=source_task.to_space_id,
                            lot_id=source_lot.lot_id,
                            starts=at, ends=source_task.planned_out_at,
                            actor=actor, exclude_task=source_task.task_id,
                        )
                        self.store.update_task_state(
                            p["task_id"], TaskState.PLACED.value,
                            actual_out_at=None,
                        )
                        restored.append(p["task_id"])
                self.store.update_task_state(
                    task_id, TaskState.CANCELLED.value,
                    review_decision=decision.value,
                )
                self._event("space.released", t.to_space_id, actor, {
                    "task_id": task_id, "lot_id": t.lot_id,
                    "reason": "review_revoke",
                    "restored_source_tasks": restored,
                    "skipped_source_tasks": skipped,
                }, at)
            else:
                self.store.update_task_state(
                    task_id, TaskState.PENDING_REVIEW.value,
                    review_decision=ReviewDecision.MANUAL.value,
                )
            self._event("move.reviewed", task_id, actor, {
                "task_id": task_id, "decision": decision.value,
            }, at)

    # ---- 紧急放行：两人批准、到期回收 -----------------------------------

    def request_permit(self, *, permit_id: str, space_id: str | None,
                       reason: str, requested_by: str,
                       valid_from: datetime, valid_until: datetime,
                       request_id: str | None = None) -> str:
        self._require_actor(requested_by)
        require_aware(valid_from); require_aware(valid_until)
        if valid_from >= valid_until:
            raise ValidationError("放行期限倒置")
        with self._tx():
            if self._replayed(request_id) is not None:
                return permit_id
            if space_id is not None:
                self._space(space_id)
            self.store.insert_permit({
                "permit_id": permit_id, "space_id": space_id, "reason": reason,
                "requested_by": requested_by,
                "valid_from": valid_from, "valid_until": valid_until,
            })
            self._event("permit.requested", permit_id, requested_by, {
                "permit_id": permit_id, "space_id": space_id, "reason": reason,
                "valid_from": valid_from.isoformat(),
                "valid_until": valid_until.isoformat(),
            }, self.now(), request_id)
            return permit_id

    def approve_permit(self, permit_id: str, approver: str) -> None:
        """紧急放行须两人批准：批准人不得是申请人，也不得重复批准。"""
        self._require_admin(approver)
        with self._tx():
            row = self.store.get_permit_row(permit_id)
            if row is None:
                raise NotFound("许可不存在")
            if row["state"] != "pending":
                raise ApprovalError(f"许可状态 {row['state']}，不可批准")
            if approver == row["requested_by"]:
                raise ApprovalError("申请人不能作为批准人")
            a, b = row["approver_a"], row["approver_b"]
            if approver in (a, b):
                raise ApprovalError("同一批准人不能重复批准")
            self.store.approve_permit(permit_id, approver)
            self._event("permit.approved", permit_id, approver, {
                "permit_id": permit_id, "approver": approver,
            }, self.now())
            a2, b2 = self.store.permit_approvals(permit_id)
            if a2 and b2:
                self._event("permit.granted", permit_id, approver, {
                    "permit_id": permit_id, "approvers": [a2, b2],
                }, self.now())

    def revoke_permit(self, permit_id: str, actor: str) -> None:
        self._require_admin(actor)
        with self._tx():
            row = self.store.get_permit_row(permit_id)
            if row is None or row["state"] not in ("pending", "granted"):
                raise StateError("许可已不可撤销")
            self.store.conn.execute(
                "update emergency_permits set state = 'revoked' where permit_id = ?",
                (permit_id,),
            )
            self._event("permit.revoked", permit_id, actor,
                        {"permit_id": permit_id}, self.now())

    # ---- 周期巡检：到期回收、失约计划、超期占用 ---------------------------

    def sweep(self, *, at: datetime | None = None) -> dict[str, Any]:
        at = at or self.now()
        result = {"permits_expired": [], "plans_expired": [], "overstay": []}
        with self._tx():
            # 1) 紧急放行到期：尚未搬入的计划失效；仍在场的立即强制清退回收
            for permit_id in self.store.expire_due_permits(at):
                self._event("permit.expired", permit_id, "system",
                            {"permit_id": permit_id}, at)
                result["permits_expired"].append(permit_id)
                for row in self.store.tasks_using_permit(permit_id):
                    state = row["state"]
                    if state in (TaskState.PLANNED.value, TaskState.APPROVED.value):
                        self.store.update_task_state(
                            row["task_id"], TaskState.EXPIRED.value
                        )
                        self._event("plan.expired", row["task_id"], "system", {
                            "task_id": row["task_id"], "permit_id": permit_id,
                            "space_id": row["to_space_id"],
                        }, at)
                        self._event("space.released", row["to_space_id"], "system", {
                            "task_id": row["task_id"], "lot_id": row["lot_id"],
                            "reason": "permit_expired",
                        }, at)
                        result["plans_expired"].append(row["task_id"])
                    elif state == TaskState.PLACED.value:
                        # 期限到达即回收：按强制清退据实结算并留痕
                        t = row_to_task(row)
                        lot = self._lot(t.lot_id)
                        self.store.update_task_state(
                            t.task_id, TaskState.RELEASED.value, actual_out_at=at
                        )
                        self._usage_charge(t, lot, at, forced=True)
                        self.store.audit("system", "permit.expired.force_cleared",
                                         "move_task", t.task_id,
                                         {"permit_id": permit_id,
                                          "space_id": t.to_space_id}, at)
                        self._event("space.force_released", t.to_space_id, "system", {
                            "task_id": t.task_id, "lot_id": t.lot_id,
                            "tenant_id": t.tenant_id,
                            "reason": "permit_expired",
                            "actual_out_at": at.isoformat(),
                        }, at)
                        self._event("space.released", t.to_space_id, "system", {
                            "task_id": t.task_id, "lot_id": t.lot_id,
                            "reason": "permit_expired_forced",
                        }, at)
                        result["overstay"].append(t.task_id)

            # 2) 失约计划：计划窗口已过仍未搬入，冻结容量收回
            for row in self.store.list_task_rows():
                t = row_to_task(row)
                if t.state in (TaskState.PLANNED, TaskState.APPROVED) \
                        and t.planned_out_at <= at:
                    self.store.update_task_state(
                        t.task_id, TaskState.EXPIRED.value
                    )
                    self._event("plan.expired", t.task_id, "system", {
                        "task_id": t.task_id, "reason": "no_show",
                    }, at)
                    self._event("space.released", t.to_space_id, "system", {
                        "task_id": t.task_id, "lot_id": t.lot_id,
                        "reason": "plan_expired",
                    }, at)
                    result["plans_expired"].append(t.task_id)

            # 3) 超期在场：登记一次性超期费，等待自行离场或强制清退
            for row in self.store.list_task_rows():
                t = row_to_task(row)
                if t.state is TaskState.PLACED and t.planned_out_at < at:
                    lot = self._lot(t.lot_id)
                    in_at = t.completed_at or t.planned_in_at
                    hours = max((at - in_at).total_seconds() / 3600.0, 1.0)
                    amount = round(
                        lot.dimensions.footprint_m2 * self.rate * hours
                        * self.overstay_ratio, 2
                    )
                    if self.store.add_charge(t.task_id, t.tenant_id, amount,
                                             "overstay", at):
                        result["overstay"].append(t.task_id)
        return result

    # ---- 越权追查与强制清退 ---------------------------------------------

    def report_encroachment(self, *, actor: str, space_id: str,
                            description: str, ref: str | None = None) -> str:
        """巡场发现未登记/越权占用（如货柜遮挡消防通道），登记追查记录。"""
        self._require_admin(actor)
        with self._tx():
            self._space(space_id)
            now = self.now()
            self.store.audit(actor, "encroachment.reported", "space", space_id, {
                "description": description, "ref": ref, "status": "open",
            }, now)
            record_id = f"enc-{space_id}-{now.timestamp():.6f}"
            self._event("encroachment.reported", space_id, actor, {
                "record_id": record_id, "space_id": space_id,
                "description": description, "ref": ref,
            }, now)
            return record_id

    def resolve_encroachment(self, *, actor: str, space_id: str,
                             record_id: str, note: str = "") -> None:
        self._require_admin(actor)
        with self._tx():
            now = self.now()
            self.store.audit(actor, "encroachment.resolved", "space", space_id, {
                "record_id": record_id, "note": note,
            }, now)
            self._event("encroachment.resolved", space_id, actor, {
                "record_id": record_id, "space_id": space_id, "note": note,
            }, now)

    def force_clear(self, task_id: str, actor: str, *, reason: str,
                    at: datetime | None = None) -> None:
        """强制清退：释放容量、计清退费、全程留痕。搬运中的任务须先经恢复裁定。"""
        self._require_admin(actor)
        at = at or self.now()
        with self._tx():
            t = self._task(task_id)
            if t.state in (TaskState.RELEASED, TaskState.CANCELLED,
                           TaskState.EXPIRED):
                raise StateError(f"任务已终结（{t.state.value}），无需清退")
            if t.state in (TaskState.MOVING, TaskState.PENDING_REVIEW):
                raise StateError("搬运未完成，请先经异常恢复流程裁定")
            lot = self._lot(t.lot_id)
            self.store.update_task_state(
                task_id, TaskState.RELEASED.value, actual_out_at=at
            )
            if t.state is TaskState.PLACED:
                self._usage_charge(t, lot, at, forced=True)
            else:
                self.store.add_charge(task_id, t.tenant_id,
                                      self.clearance_fee, "clearance", at)
            self.store.audit(actor, "force_clear", "move_task", task_id, {
                "space_id": t.to_space_id, "tenant_id": t.tenant_id,
                "lot_id": t.lot_id, "reason": reason,
            }, at)
            self._event("space.force_released", t.to_space_id, actor, {
                "task_id": task_id, "lot_id": t.lot_id,
                "tenant_id": t.tenant_id, "reason": reason,
                "actual_out_at": at.isoformat(),
            }, at)
            self._event("space.released", t.to_space_id, actor, {
                "task_id": task_id, "lot_id": t.lot_id, "reason": "forced",
            }, at)

    # ---- 腾挪顺序 ------------------------------------------------------

    def build_evacuation_plan(self, space_id: str, actor: str, *,
                              depart_within: timedelta = timedelta(hours=2),
                              at: datetime | None = None) -> list[dict[str, Any]]:
        """为消防通道受阻 / 区域封闭生成实际可执行的腾挪顺序。

        排序原则：叠放在上层的先挪（底座最后动）；临期货物直接加速离场；
        其余按预计离场时间从早到晚改投同租户可用空间；尚未搬入的计划改投或
        取消。每一步都通过完整接货判断，可直接交 execute_evacuation_step 执行。
        """
        self._require_admin(actor)
        at = at or self.now()
        steps: list[dict[str, Any]] = []
        with self._tx():
            space = self._space(space_id)
            rows = list(self.store.tasks_touching_space(space_id))
            placed = [r for r in rows if r["state"] == TaskState.PLACED.value]
            planned = [
                r for r in rows
                if TaskState(r["state"]).not_yet_moved_in
            ]

            # 上层先于底座
            def stack_depth(row) -> int:
                depth = 0
                cur = row["stacked_on_task"]
                seen = set()
                while cur and cur not in seen:
                    depth += 1
                    seen.add(cur)
                    base = self.store.get_task_row(cur)
                    cur = base["stacked_on_task"] if base else None
                return depth

            placed.sort(key=lambda r: (-stack_depth(r),
                                       _dt(r["planned_out_at"]) or FAR_FUTURE))

            for row in placed:
                t = row_to_task(row)
                lot = self._lot(t.lot_id)
                if t.planned_out_at <= at + depart_within:
                    steps.append({
                        "type": "expedite_departure", "task_id": t.task_id,
                        "lot_id": t.lot_id, "tenant_id": t.tenant_id,
                        "from_space_id": space_id, "to_space_id": None,
                        "reason": "临期货物加速离场",
                    })
                    continue
                dest = self._find_relocation_dest(t, lot, at)
                if dest is None:
                    raise NoSpaceAvailable(
                        f"货物 {t.lot_id} 无可用安置空间，需人工决策"
                    )
                steps.append({
                    "type": "relocate", "task_id": t.task_id,
                    "lot_id": t.lot_id, "tenant_id": t.tenant_id,
                    "from_space_id": space_id, "to_space_id": dest["space_id"],
                    "permit_id": dest.get("permit_id"),
                    "stacked_on_task": dest.get("stacked_on_task"),
                    "reason": f"清空{space.name}",
                })

            for row in planned:
                t = row_to_task(row)
                lot = self._lot(t.lot_id)
                try:
                    dest = self._find_relocation_dest(t, lot, at)
                except NoSpaceAvailable:
                    dest = None
                steps.append({
                    "type": "retarget" if dest else "cancel_plan",
                    "task_id": t.task_id, "lot_id": t.lot_id,
                    "tenant_id": t.tenant_id,
                    "from_space_id": space_id,
                    "to_space_id": dest["space_id"] if dest else None,
                    "permit_id": dest.get("permit_id") if dest else None,
                    "stacked_on_task": dest.get("stacked_on_task") if dest else None,
                    "reason": "目标区域封闭/消防净空，计划尚未搬入",
                })
        return steps

    def _find_relocation_dest(self, t: MoveTask, lot: CargoLot,
                              at: datetime) -> dict[str, Any] | None:
        """在不违反品类准入、业主边界与容量的前提下找安置空间。"""
        candidates = []
        for row in self.store.list_space_rows():
            s = row_to_space(row)
            if s.state is SpaceState.CLOSED or s.is_fire_lane:
                continue
            if s.owner_tenant_id and s.owner_tenant_id != t.tenant_id:
                continue  # 腾挪不得侵入其他商户的固定档口
            try:
                self.validate_intake(
                    space_id=s.space_id, lot_id=lot.lot_id,
                    starts=at, ends=t.planned_out_at, actor=t.tenant_id,
                )
            except DomainError:
                continue
            slices = self._occupancy_slices(s.space_id)
            peak_area, peak_weight = self._peak(slices, at, t.planned_out_at)
            candidates.append((s.space_id, s.area_m2 - peak_area,
                               s.max_weight_kg - peak_weight, None, None))
        if not candidates:
            return None
        candidates.sort(key=lambda c: (-c[1], -c[2], c[0]))
        sid, free_area, free_weight, permit, stack = candidates[0]
        return {
            "space_id": sid, "free_area_m2": round(free_area, 3),
            "free_weight_kg": round(free_weight, 1),
            "permit_id": permit, "stacked_on_task": stack,
        }

    def execute_evacuation_step(self, step: dict[str, Any], actor: str, *,
                                at: datetime | None = None,
                                request_id: str | None = None) -> str:
        """执行单步腾挪：落库前按当前现场重新校验，跨区域移动全程留痕。"""
        self._require_admin(actor)
        at = at or self.now()
        with self._tx():
            if self._replayed(request_id) is not None:
                return step["task_id"]
            kind = step["type"]
            t = self._task(step["task_id"])
            lot = self._lot(t.lot_id)
            if kind == "expedite_departure":
                self.release_lot(step["task_id"], at=at)
            elif kind == "cancel_plan":
                self._require_not_moved_in(t)
                self.store.update_task_state(
                    t.task_id, TaskState.CANCELLED.value
                )
                self._event("plan.cancelled", t.task_id, actor, {
                    "task_id": t.task_id, "space_id": t.to_space_id,
                    "reason": "evacuation_no_destination",
                }, at)
                self._event("space.released", t.to_space_id, actor, {
                    "task_id": t.task_id, "lot_id": t.lot_id,
                    "reason": "evacuation_cancel",
                }, at)
            elif kind == "retarget":
                self._require_not_moved_in(t)
                dest = step["to_space_id"]
                # 执行时按最新现场重新做完整接货判断
                self.validate_intake(
                    space_id=dest, lot_id=t.lot_id,
                    starts=t.planned_in_at, ends=t.planned_out_at,
                    permit_id=step.get("permit_id"),
                    stack_on_task_id=step.get("stacked_on_task"),
                    actor=actor, exclude_task=t.task_id,
                )
                self.retarget_plan(
                    t.task_id, actor, new_space_id=dest,
                    permit_id=step.get("permit_id"),
                    stack_on_task_id=step.get("stacked_on_task"),
                )
            elif kind == "relocate":
                if t.state is not TaskState.PLACED:
                    raise StateError("relocate 步骤要求货物在场")
                dest = step["to_space_id"]
                self.validate_intake(
                    space_id=dest, lot_id=t.lot_id, starts=at,
                    ends=t.planned_out_at, permit_id=step.get("permit_id"),
                    stack_on_task_id=step.get("stacked_on_task"),
                    actor=actor,
                )
                new_task_id = f"move-{t.lot_id}-{at.timestamp():.6f}"
                self.store.insert_task({
                    "task_id": new_task_id, "lot_id": t.lot_id,
                    "tenant_id": t.tenant_id,
                    "from_space_id": step["from_space_id"],
                    "to_space_id": dest,
                    "planned_in_at": at, "planned_out_at": t.planned_out_at,
                    "state": TaskState.APPROVED.value,
                    "stacked_on_task": step.get("stacked_on_task"),
                    "permit_id": step.get("permit_id"),
                })
                # 原在场段据实结算后释放，再走完整搬运链路，保证每一步留痕
                self.store.update_task_state(
                    t.task_id, TaskState.RELEASED.value, actual_out_at=at
                )
                self._usage_charge(t, lot, at, forced=False)
                self._event("space.released", t.to_space_id, actor, {
                    "task_id": t.task_id, "lot_id": t.lot_id,
                    "reason": "evacuation_relocate",
                }, at)
                self._event("space.held", dest, actor, {
                    "task_id": new_task_id, "lot_id": t.lot_id,
                    "space_id": dest,
                    "planned_in_at": at.isoformat(),
                    "planned_out_at": t.planned_out_at.isoformat(),
                    "evacuation_of": t.task_id,
                }, at, request_id)
                self.start_move(new_task_id, actor=actor, at=at)
                self.complete_move(new_task_id, at=at)
                self.store.audit(actor, "evacuation.relocate",
                                 "move_task", new_task_id, {
                                     **step, "old_task_id": t.task_id,
                                 }, at)
            else:
                raise ValidationError(f"未知腾挪步骤类型：{kind}")
            return step["task_id"]

    # ---- 视图：租户隔离 -------------------------------------------------

    def space_view(self, space_id: str, actor: str, *,
                   at: datetime | None = None) -> dict[str, Any]:
        self._require_actor(actor)
        at = at or self.now()
        with self._tx():
            space = self._space(space_id)
            slices = self._occupancy_slices(space_id, now=at)
            current = [s for s in slices if s.starts <= at < s.ends]
            used_area = sum(s.footprint for s in current)
            used_weight = sum(s.weight for s in current)
            is_admin = self._is_admin(actor)
            lots = []
            for s in current:
                if is_admin or s.tenant_id == actor:
                    lots.append({
                        "task_id": s.task_id, "lot_id": s.lot_id,
                        "tenant_id": s.tenant_id, "category": s.category,
                        "footprint_m2": round(s.footprint, 3),
                        "weight_kg": s.weight, "stacked": s.stacked,
                        "until": s.ends.isoformat(),
                    })
                else:
                    # 其他商户：只给聚合占用，不透露货位细节
                    lots.append({"redacted": True,
                                 "footprint_m2": round(s.footprint, 3),
                                 "weight_kg": s.weight})
            return {
                "space_id": space.space_id, "name": space.name,
                "kind": space.kind.value, "state": space.state.value,
                "is_fire_lane": space.is_fire_lane,
                "area_m2": space.area_m2, "max_weight_kg": space.max_weight_kg,
                "used_area_m2": round(used_area, 3),
                "used_weight_kg": round(used_weight, 1),
                "free_area_m2": round(space.area_m2 - used_area, 3),
                "occupants": lots,
            }

    def ledger(self, actor: str, *, at: datetime | None = None) -> list[dict]:
        """全市场空间账（租户视角自动脱敏）。"""
        self._require_actor(actor)
        with self._tx():
            return [
                self.space_view(row["space_id"], actor, at=at)
                for row in self.store.list_space_rows()
            ]

    def list_tasks(self, actor: str) -> list[dict[str, Any]]:
        self._require_actor(actor)
        with self._tx():
            rows = self.store.list_task_rows(
                tenant_id=None if self._is_admin(actor) else actor
            )
            return [asdict(row_to_task(r)) for r in rows]

    def trajectory(self, lot_id: str, actor: str) -> list[dict[str, Any]]:
        """货物跨区域移动的完整轨迹（货主或管理员可见）。"""
        self._require_actor(actor)
        with self._tx():
            lot = self._lot(lot_id)
            if lot.tenant_id != actor and not self._is_admin(actor):
                raise PermissionDenied("无权查看他户货物轨迹")
            result = []
            for e in self.store.all_events():
                payload = json.loads(e["payload"])
                if payload.get("lot_id") == lot_id or \
                        e["aggregate_id"] == lot_id:
                    result.append({
                        "event_id": e["event_id"], "event_type": e["event_type"],
                        "actor": e["actor"], "occurred_at": e["occurred_at"],
                        "payload": payload,
                    })
            return result

    def list_charges(self, actor: str) -> list[dict[str, Any]]:
        self._require_actor(actor)
        with self._tx():
            rows = self.store.list_charges(
                tenant_id=None if self._is_admin(actor) else actor
            )
            return [dict(r) for r in rows]

    def audit_log(self, actor: str, target_id: str | None = None) -> list[dict]:
        self._require_admin(actor)
        with self._tx():
            return [dict(r) for r in self.store.list_audit(target_id)]
