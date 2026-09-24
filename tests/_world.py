"""服务级测试夹具。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from capacity import Actor, CapacityService, CargoSpec
from capacity.service import SpaceSpec

TZ = timezone(timedelta(hours=8))


def t(hour: int, day: int = 24) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=TZ)


class WorldTest(unittest.TestCase):
    """标准场地：

    stall-A 固定档口 [0,0]-[10,10] 归 ten-A
    temp-1  临时区   [20,0]-[34,10]（西侧 2 米与 lane-1 地理重叠）
    temp-2  临时区   [40,0]-[54,10]
    lane-1  消防通道 [10,0]-[22,3]（与 temp-1 西侧重叠 2 米）
    shelf-1 货架层位 [60,0]-[64,2]，层承重 300kg
    """

    def setUp(self) -> None:
        self.admin = Actor("admin-1", "admin")
        self.sec = Actor("sec-1", "security")
        self.A = Actor("u-A", "tenant", "ten-A")
        self.B = Actor("u-B", "tenant", "ten-B")
        self.svc = CapacityService(clock=lambda: t(7))
        self.svc.register_tenant(self.admin, "ten-A", "甲精品果档")
        self.svc.register_tenant(self.admin, "ten-B", "乙档口")
        self.svc.register_space(self.admin, SpaceSpec(
            "stall-A", "fixed_stall", 0, 0, 10, 10, 4, 500, owner_id="ten-A"))
        self.svc.register_space(self.admin, SpaceSpec(
            "temp-1", "temp_area", 20, 0, 14, 10, 4, 300))
        self.svc.register_space(self.admin, SpaceSpec(
            "temp-2", "temp_area", 40, 0, 14, 10, 4, 300))
        self.svc.register_space(self.admin, SpaceSpec(
            "lane-1", "fire_lane", 10, 0, 12, 3, 3, 0))
        self.svc.register_space(self.admin, SpaceSpec(
            "shelf-1", "shelf_level", 60, 0, 4, 2, 2, 10_000, level_load=300))
        self.svc.add_adjacency_taboo(self.admin, "fruit", "chemical", 2.0)

    def tearDown(self) -> None:
        self.svc.close()

    def declare(self, actor, cargo_id, spec, **kw):
        self.svc.declare_cargo(actor, cargo_id, spec, **kw)

    @staticmethod
    def box(weight=50, category="fruit", *, stackable=False, max_layers=1, bear_load=0,
            length=1, width=1, height=1):
        return CargoSpec(length, width, height, weight, category, stackable,
                         max_layers, bear_load)
