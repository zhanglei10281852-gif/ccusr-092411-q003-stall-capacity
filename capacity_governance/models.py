"""领域模型：空间、货架层位、货物批次、搬运任务、紧急放行。

状态命名与 ``domain/contract.json`` 对齐：
planned → approved → moving → placed → released；
放行到期未续则 expired。
所有时间字段统一使用带时区的 datetime。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class SpaceKind(str, Enum):
    FIXED = "fixed"        # 固定档口（有归属租户）
    TEMP = "temp"          # 临时区域
    RACK = "rack"          # 货架层位（隶属于某个物理空间）


class SpaceState(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"      # 区域封闭：只能调整尚未搬入的计划


class TaskState(str, Enum):
    PLANNED = "planned"    # 已申报占位，待审批
    APPROVED = "approved"  # 已批准，等待搬入（仍冻结容量）
    MOVING = "moving"      # 搬运中：已离开来源、未到达目标
    PLACED = "placed"      # 已送达，实际在场
    RELEASED = "released"  # 货物离场，容量已释放
    EXPIRED = "expired"    # 计划超期未执行 / 紧急放行到期被回收
    CANCELLED = "cancelled"
    PENDING_REVIEW = "pending_review"  # 异常重启后待人工裁定

    @property
    def occupies(self) -> bool:
        return self in (
            TaskState.PLANNED, TaskState.APPROVED,
            TaskState.MOVING, TaskState.PLACED,
            TaskState.PENDING_REVIEW,
        )

    @property
    def not_yet_moved_in(self) -> bool:
        """尚未搬入：销售加快、车辆延误、区域封闭时只允许调整这些计划。"""
        return self in (TaskState.PLANNED, TaskState.APPROVED)


class ReviewDecision(str, Enum):
    CONTINUE = "continue"   # 继续完成搬运
    REVOKE = "revoke"       # 撤销，释放容量（来源空间容量归还）
    MANUAL = "manual"       # 保持待人工确认，容量继续冻结


class PermitState(str, Enum):
    PENDING = "pending"
    GRANTED = "granted"
    EXPIRED = "expired"
    REVOKED = "revoked"


def require_aware(dt: datetime, name: str = "时间") -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{name}必须带时区")
    return dt


@dataclass(frozen=True)
class Dimensions:
    """货物尺寸（米）与重量（千克）。占地按外接矩形 footprint_m2 计算。"""

    length_m: float
    width_m: float
    height_m: float
    weight_kg: float

    @property
    def footprint_m2(self) -> float:
        return self.length_m * self.width_m


@dataclass(frozen=True)
class Space:
    space_id: str
    kind: SpaceKind
    name: str
    area_m2: float
    max_weight_kg: float
    max_height_m: float
    is_fire_lane: bool
    state: SpaceState
    owner_tenant_id: str | None
    parent_space_id: str | None
    allowed_categories: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CargoLot:
    lot_id: str
    tenant_id: str
    category: str
    dimensions: Dimensions
    stackable: bool
    stacks_on: frozenset[str] = frozenset()
    taboo_adjacent: frozenset[str] = frozenset()
    declared_at: datetime | None = None
    expected_departure: datetime | None = None


@dataclass(frozen=True)
class MoveTask:
    task_id: str
    lot_id: str
    tenant_id: str
    from_space_id: str | None
    to_space_id: str
    planned_in_at: datetime
    planned_out_at: datetime
    state: TaskState
    started_at: datetime | None = None
    completed_at: datetime | None = None
    actual_out_at: datetime | None = None
    review_decision: ReviewDecision | None = None
    stacked_on_task: str | None = None
    permit_id: str | None = None


@dataclass(frozen=True)
class EmergencyPermit:
    permit_id: str
    space_id: str | None
    reason: str
    requested_by: str
    approvers: tuple[str | None, str | None]
    valid_from: datetime
    valid_until: datetime
    state: PermitState


@dataclass(frozen=True)
class UsageSlice:
    """容量时间轴上的一个占用区间。叠放（stacked）不重复占地面，仍计承重。"""

    task_id: str
    tenant_id: str
    lot_id: str
    category: str
    footprint: float
    weight: float
    starts: datetime
    ends: datetime
    stacked: bool = False
