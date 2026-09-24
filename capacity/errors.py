"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有业务规则错误的基类。"""


class NotFoundError(DomainError):
    """业务标识不存在。"""


class StateError(DomainError):
    """实体当前状态不允许该操作（如已搬入货物仍尝试改期）。"""


class AuthError(DomainError):
    """越权访问或缺少角色权限。"""


class CapacityError(DomainError):
    """容量/安全边界校验失败，reasons 为机器可读的原因码列表。"""

    def __init__(self, reasons: list[str] | str):
        if isinstance(reasons, str):
            reasons = [reasons]
        self.reasons = reasons
        super().__init__("；".join(reasons))
