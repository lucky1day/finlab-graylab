"""DataBridge refresh error taxonomy; re-exported by refresh.py."""

from __future__ import annotations


class DataBridgeRefreshError(RuntimeError):
    """A full refresh could not produce a stable valid dataset."""


class DataBridgeNonRetryableRefreshError(DataBridgeRefreshError):
    """重试不能改变结果的 refresh 控制面失败。"""


class DataBridgeContinuityIdentityError(
    DataBridgeNonRetryableRefreshError
):
    """current 与冻结 continuity authority 身份不一致。"""


class DataBridgeCurrentReadError(DataBridgeNonRetryableRefreshError):
    """严格只读 current 检查失败。"""


class DataBridgeCurrentMissingError(DataBridgeCurrentReadError):
    """严格只读 current 所需的已发布状态缺失。"""


class DataBridgeCurrentInvalidError(DataBridgeCurrentReadError):
    """严格只读 current 的目录、状态或内容无效。"""


class DataBridgeStoreLockError(DataBridgeNonRetryableRefreshError):
    """DataBridge store owner lock 无法取得或升级。"""


class DataBridgeRecoveryIdentityError(
    DataBridgeNonRetryableRefreshError
):
    """current/previous 与已发布 state 身份无法恢复一致。"""
