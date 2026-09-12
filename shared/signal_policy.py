from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class _FrozenDict(dict[str, Any]):
    """保持 JSON object 语义的只读字典。"""

    @staticmethod
    def _immutable(*args: Any, **kwargs: Any) -> None:
        raise TypeError("frozen mapping does not support mutation")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


@dataclass(frozen=True)
class SignalOutcome:
    """平台信号政策的不可变结果。"""

    predicted_direction: int
    extra: Mapping[str, Any]


def no_signal_as_flat(
    source_component: str,
    *,
    extra: Mapping[str, Any] | None = None,
) -> SignalOutcome:
    """将 core 当前特征缺信号统一映射为可审计的平记录。"""
    merged_extra = dict(extra or {})
    merged_extra.update(
        {
            "signal_state": "no_signal",
            "signal_policy": "no_signal_to_flat_v1",
            "signal_policy_applied": True,
            "no_signal_reason": "core_output_missing_current_feature",
            "source_component": source_component,
        }
    )
    return SignalOutcome(
        predicted_direction=0,
        extra=_freeze_json_container(merged_extra),
    )


def _freeze_json_container(value: Any) -> Any:
    """复制并递归冻结 JSON 的 object/array 容器。"""
    if isinstance(value, Mapping):
        return _FrozenDict(
            {key: _freeze_json_container(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_container(item) for item in value)
    return value
