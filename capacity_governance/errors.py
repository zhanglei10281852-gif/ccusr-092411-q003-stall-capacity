"""领域错误类型。

所有业务规则校验失败都抛出 DomainError 的子类，
调用方可以按类型区分容量、安全、权限、状态等不同失败原因。
"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则错误基类。"""


class ValidationError(DomainError):
    """输入参数不满足领域约束（如时间缺时区、窗口倒置）。"""


class NotFound(DomainError):
    """目标实体不存在，或对当前操作者不可见。"""


class PermissionDenied(DomainError):
    """当前操作者无权执行该操作或查看该数据。"""


class CapacityExceeded(DomainError):
    """空间在请求的时间窗口内容量（面积或承重）不足。"""


class NoSpaceAvailable(DomainError):
    """没有任何空间能容纳该货物（尺寸、限高等硬约束不满足）。"""


class SafetyViolation(DomainError):
    """触碰安全边界（如消防通道）且没有有效紧急放行许可。"""


class TabooViolation(DomainError):
    """货物类别与相邻空间在场货物存在相邻禁忌。"""


class StateError(DomainError):
    """实体当前状态不允许该操作（含重复释放、重复完成等）。"""


class ApprovalError(DomainError):
    """紧急放行审批规则被违反（自批、重复批准等）。"""
