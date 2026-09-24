"""容量与安全边界校验引擎（纯函数，不碰数据库）。

坐标约定：hold 的 x/y 为全局坐标（= 所属 space 原点 + 档口内偏移），
因此同空间叠放与跨空间相邻禁忌可以在同一坐标系下计算。

把候选占位放进时间轴：用候选区间与既有占位端点切成时间片，
逐片校验——
  * 同空间：平面冲突、叠放关系、层数、净高、地面/层板承重；
  * 跨空间：相邻品类按矩形净距判定；
  * 消防通道：足迹与任何 fire_lane 相交即阻断（紧急放行可覆盖）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from .model import (
    ADJACENCY,
    AREA_CONFLICT,
    CLOSURE,
    FLOOR_LOAD,
    GEOMETRY,
    HEIGHT_LIMIT,
    LEVEL_LOAD,
    OWNERSHIP,
    STACK_RULE,
)

EPS = 1e-6

_REASON_ORDER = [
    "FIRE_LANE_BLOCKED",
    AREA_CONFLICT,
    STACK_RULE,
    HEIGHT_LIMIT,
    FLOOR_LOAD,
    LEVEL_LOAD,
    ADJACENCY,
    OWNERSHIP,
    CLOSURE,
    GEOMETRY,
    "BAD_INTERVAL",
]


@dataclass
class Placement:
    """解析后的占位（货物属性冗余在内）。"""

    hold_id: str
    space_id: str
    tenant_id: str
    cargo_id: str
    x: float
    y: float
    level: int
    start: datetime
    leave: datetime
    length: float
    width: float
    height: float
    weight: float
    category: str
    stackable: bool
    max_layers: int
    bear_load: float


def _bounds(p: Placement) -> tuple[float, float, float, float]:
    return p.x, p.y, p.x + p.length, p.y + p.width


def _overlap(a: Placement, b: Placement) -> bool:
    ax1, ay1, ax2, ay2 = _bounds(a)
    bx1, by1, bx2, by2 = _bounds(b)
    return not (
        ax2 <= bx1 + EPS or bx2 <= ax1 + EPS or ay2 <= by1 + EPS or by2 <= ay1 + EPS
    )


def _contained(upper: Placement, lower: Placement) -> bool:
    ux1, uy1, ux2, uy2 = _bounds(upper)
    lx1, ly1, lx2, ly2 = _bounds(lower)
    return (
        ux1 + EPS >= lx1 and uy1 + EPS >= ly1 and ux2 <= lx2 + EPS and uy2 <= ly2 + EPS
    )


def rect_hits(p: Placement, rect: dict[str, Any]) -> bool:
    px1, py1, px2, py2 = _bounds(p)
    rx2 = rect["x"] + rect["w"]
    ry2 = rect["y"] + rect["d"]
    return not (
        px2 <= rect["x"] + EPS
        or rx2 <= px1 + EPS
        or py2 <= rect["y"] + EPS
        or ry2 <= py1 + EPS
    )


def rect_within(p: Placement, rect: dict[str, Any]) -> bool:
    """候选足迹完全落在 rect 内。"""
    px1, py1, px2, py2 = _bounds(p)
    return (
        px1 + EPS >= rect["x"]
        and py1 + EPS >= rect["y"]
        and px2 <= rect["x"] + rect["w"] + EPS
        and py2 <= rect["y"] + rect["d"] + EPS
    )


def static_checks(candidate: Placement, space: dict[str, Any], overrides: set[str]) -> list[str]:
    """与其他占位无关的边界检查。"""
    reasons: list[str] = []
    if candidate.start >= candidate.leave:
        return ["BAD_INTERVAL"]

    if space.get("status") == "closed":
        cf, cu = space.get("closed_from"), space.get("closed_until")
        if cf is not None and candidate.start < cu and cf < candidate.leave and "closure" not in overrides:
            reasons.append(CLOSURE)

    if (
        space.get("kind") == "fixed_stall"
        and space.get("owner_id")
        and space["owner_id"] != candidate.tenant_id
        and "ownership" not in overrides
    ):
        reasons.append(OWNERSHIP)

    if not rect_within(candidate, space):
        reasons.append(GEOMETRY)

    return reasons


def _stack_edges(group: list[Placement]) -> tuple[dict[int, set[int]], set[str]]:
    """构建 lower -> {upper} 叠放边。"""
    edges: dict[int, set[int]] = {i: set() for i in range(len(group))}
    reasons: set[str] = set()
    for i in range(len(group)):
        for j in range(i + 1, len(group)):
            a, b = group[i], group[j]
            if not _overlap(a, b):
                continue
            if a.level == b.level:
                reasons.add(AREA_CONFLICT)
                continue
            upper, lower = (a, b) if a.level > b.level else (b, a)
            upper_i, lower_i = (i, j) if a.level > b.level else (j, i)
            if upper.level - lower.level != 1:
                reasons.add(STACK_RULE)  # 跨层悬空
                continue
            if not upper.stackable or not lower.stackable or not _contained(upper, lower):
                reasons.add(STACK_RULE)
                continue
            edges[lower_i].add(upper_i)
    return edges, reasons


def _descendants(edges: dict[int, set[int]], node: int) -> set[int]:
    out: set[int] = set()
    stack = list(edges.get(node, ()))
    while stack:
        cur = stack.pop()
        if cur not in out:
            out.add(cur)
            stack.extend(edges.get(cur, ()))
    return out


def _check_stacks(group: list[Placement], space: dict[str, Any]) -> set[str]:
    edges, reasons = _stack_edges(group)

    # level>0 的货必须有直接下层支承（出现在某条叠放边的上端），否则悬空
    supported = {upper for uppers in edges.values() for upper in uppers}
    for i, p in enumerate(group):
        if p.level > 0 and i not in supported:
            reasons.add(STACK_RULE)

    # 沿每条竖向链校验层数 / 净高 / 上下层承重
    def walk(node: int, path: list[int]) -> None:
        path = path + [node]
        chain_len = group[node].level + 1
        if any(chain_len > group[k].max_layers for k in path):
            reasons.add(STACK_RULE)
        if sum(group[k].height for k in path) > space["net_height"] + EPS:
            reasons.add(HEIGHT_LIMIT)
        for idx, k in enumerate(path[:-1]):
            above = sum(group[m].weight for m in path[idx + 1 :])
            if above > group[k].bear_load + EPS:
                reasons.add(STACK_RULE)
        for child in edges.get(node, set()):
            if child not in path:
                walk(child, path)

    for i, p in enumerate(group):
        if p.level == 0:
            walk(i, [])

    # 落地压强（地面承重）/ 层板总承重
    if space.get("kind") == "shelf_level":
        total = 0.0
        for i, p in enumerate(group):
            if p.level == 0:
                total += p.weight + sum(group[d].weight for d in _descendants(edges, i))
        if total > space["level_load"] + EPS:
            reasons.add(LEVEL_LOAD)
    else:
        for i, p in enumerate(group):
            if p.level != 0:
                continue
            stack_weight = p.weight + sum(group[d].weight for d in _descendants(edges, i))
            if stack_weight / (p.length * p.width) > space["floor_load"] + EPS:
                reasons.add(FLOOR_LOAD)
    return reasons


def _check_adjacency(
    active: list[Placement], taboos: dict[tuple[str, str], float]
) -> set[str]:
    reasons: set[str] = set()
    for i in range(len(active)):
        for j in range(i + 1, len(active)):
            a, b = active[i], active[j]
            if a.space_id == b.space_id and _overlap(a, b):
                continue  # 同空间重叠归叠放规则管
            need = taboos.get(tuple(sorted((a.category, b.category))))
            if need is None:
                continue
            ax1, ay1, ax2, ay2 = _bounds(a)
            bx1, by1, bx2, by2 = _bounds(b)
            gx = max(0.0, max(ax1, bx1) - min(ax2, bx2))
            gy = max(0.0, max(ay1, by1) - min(ay2, by2))
            if (gx * gx + gy * gy) ** 0.5 < need - EPS:
                reasons.add(ADJACENCY)
    return reasons


def evaluate(
    candidate: Placement,
    spaces: dict[str, dict[str, Any]],
    others: list[Placement],
    taboos: dict[tuple[str, str], float],
    *,
    grant_overrides: Iterable[str] = (),
) -> list[str]:
    """返回违规原因码（按严重度排序）。空列表 = 可以接货。"""
    overrides = set(grant_overrides)
    space = spaces[candidate.space_id]
    reasons: set[str] = set(static_checks(candidate, space, overrides))

    for lane in spaces.values():
        if lane.get("kind") == "fire_lane" and rect_hits(candidate, lane):
            if "fire_lane" not in overrides:
                reasons.add("FIRE_LANE_BLOCKED")

    # 同批货物的既有占位（搬运交接）不与候选互斥
    relevant = [
        p
        for p in others
        if p.cargo_id != candidate.cargo_id
        and p.start < candidate.leave
        and candidate.start < p.leave
    ]

    cuts = sorted(
        {candidate.start, candidate.leave}
        | {ts for p in relevant for ts in (p.start, p.leave)}
    )
    for t0, t1 in zip(cuts, cuts[1:]):
        if t1 <= candidate.start or t0 >= candidate.leave:
            continue
        active = [p for p in relevant if p.start < t1 and t0 < p.leave]
        active.append(candidate)
        by_space: dict[str, list[Placement]] = {}
        for p in active:
            by_space.setdefault(p.space_id, []).append(p)
        for sid, group in by_space.items():
            sp = spaces[sid]
            if sp.get("kind") == "fire_lane":
                continue
            reasons |= _check_stacks(group, sp)
        reasons |= _check_adjacency(active, taboos)

    return sorted(reasons, key=_REASON_ORDER.index)
