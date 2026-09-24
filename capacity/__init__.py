"""档口峰值容量治理服务。

同一本空间账：固定档口 / 临时区域 / 货架层位 / 消防通道统一建模为 space，
带时间区间的 hold 记录"某批货物在某段时间占用某块空间"。
所有写入在事务内重算容量，并发下容量不可穿透。
"""
from __future__ import annotations

from .errors import (
    AuthError,
    CapacityError,
    DomainError,
    NotFoundError,
    StateError,
)
from .geometry import Rect
from .model import Actor, CargoSpec
from .service import CapacityService

__all__ = [
    "CapacityService",
    "Actor",
    "CargoSpec",
    "Rect",
    "DomainError",
    "CapacityError",
    "StateError",
    "AuthError",
    "NotFoundError",
]
