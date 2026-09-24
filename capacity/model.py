"""领域值对象。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Actor:
    """操作人：staff 为管理员/安保，tenant_id 非空时为商户身份。"""

    actor_id: str
    role: str  # admin / security / tenant
    tenant_id: str | None = None

    @property
    def is_staff(self) -> bool:
        return self.role in ("admin", "security")


@dataclass(frozen=True)
class CargoSpec:
    """货物尺寸、重量与叠放属性。

    length/width/height 米，weight 千克。
    max_layers：该货参与叠放时整垛允许的最大层数（含自身）。
    bear_load：作为下层时顶面可承受的最大重量（千克）。
    """

    length: float
    width: float
    height: float
    weight: float
    category: str
    stackable: bool = False
    max_layers: int = 1
    bear_load: float = 0.0


# 违规原因码 / 容量校验原因码
FIRE_LANE = "FIRE_LANE_BLOCKED"  # 遮压消防通道
AREA_CONFLICT = "AREA_CONFLICT"  # 平面重叠且非合法叠放
STACK_RULE = "STACK_RULE"  # 叠放关系不合法（层数/连续性）
HEIGHT_LIMIT = "HEIGHT_LIMIT"  # 叠放总高超过净高
FLOOR_LOAD = "FLOOR_LOAD"  # 地面承重超限
LEVEL_LOAD = "LEVEL_LOAD"  # 货架层承重超限
ADJACENCY = "ADJACENCY_TABOO"  # 相邻禁忌
OWNERSHIP = "UNAUTHORIZED_SPACE"  # 占用他人固定档口
CLOSURE = "AREA_CLOSED"  # 区域封闭期内占用
GEOMETRY = "GEOMETRY"  # 货位超出空间边界
INTERVAL = "BAD_INTERVAL"  # 时间区间非法
UNAUTHORIZED_OCCUPANCY = "UNAUTHORIZED_OCCUPANCY"  # 账外/越权占位
LEDGER_MISMATCH = "LEDGER_MISMATCH"  # 账实不符
OVERSTAY = "OVERSTAY"  # 到期未离场

# 紧急放行可覆盖的边界类型
OVERRIDE_FIRE_LANE = "fire_lane"
OVERRIDE_OWNERSHIP = "ownership"
OVERRIDE_CLOSURE = "closure"
