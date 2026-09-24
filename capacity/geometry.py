"""矩形几何与时间区间工具（单位：米；时间为带时区 datetime）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

EPS = 1e-6


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    d: float

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.d

    @property
    def area(self) -> float:
        return self.w * self.d

    def moved_to(self, x: float, y: float) -> "Rect":
        return Rect(x, y, self.w, self.d)


def intersects(a: Rect, b: Rect, eps: float = EPS) -> bool:
    """两个矩形是否面积相交（仅边相切不算）。"""
    return not (
        a.x2 <= b.x + eps or b.x2 <= a.x + eps or a.y2 <= b.y + eps or b.y2 <= a.y + eps
    )


def contains(outer: Rect, inner: Rect, eps: float = EPS) -> bool:
    return (
        inner.x + eps >= outer.x
        and inner.y + eps >= outer.y
        and inner.x2 <= outer.x2 + eps
        and inner.y2 <= outer.y2 + eps
    )


def gaps(a: Rect, b: Rect) -> tuple[float, float]:
    """x / y 方向上的净距（相交方向为 0）。"""
    gx = max(0.0, max(a.x, b.x) - min(a.x2, b.x2))
    gy = max(0.0, max(a.y, b.y) - min(a.y2, b.y2))
    return gx, gy


def interval_overlap(s1: datetime, e1: datetime, s2: datetime, e2: datetime) -> bool:
    """半开区间 [s,e) 是否相交。"""
    return s1 < e2 and s2 < e1
