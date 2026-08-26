from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date


DEFAULT_BACKTEST_START_DATE = "2025-01-01"
SIDE_EFFECT_ACTIONS = frozenset(
    {
        "activate",
        "backtest_persist",
        "blackbox_activate",
        "blackbox_reconcile",
    }
)
EXACT_PREDICT_DATE_ACTIONS = frozenset(
    {
        "backtest_persist",
    }
)


@dataclass(frozen=True)
class DirectOperation:
    """由当前 CLI 命令直接表达的非秘密副作用作用域。"""

    scheme_id: str
    action: str
    predict_date: str | None
    issued_by: str
    scheme_version: str
    backtest_start_date: str | None = None


def build_direct_operation(
    scheme_id: str,
    action: str,
    predict_date: str | None = None,
    *,
    scheme_version: str,
    issued_by: str,
    backtest_start_date: str | None = None,
) -> DirectOperation:
    """从精确命令参数构造副作用作用域，不生成 token 或 replay 状态。"""
    if action not in SIDE_EFFECT_ACTIONS:
        raise ValueError(f"unknown operation action: {action}")
    normalized_predict_date = (
        _normalize_action_predict_date(action, predict_date)
        if action in EXACT_PREDICT_DATE_ACTIONS
        else None
    )
    if action not in EXACT_PREDICT_DATE_ACTIONS and predict_date is not None:
        raise ValueError(f"{action} operation does not accept predict_date")
    return DirectOperation(
        scheme_id=_require_text(scheme_id, "scheme_id"),
        action=action,
        predict_date=normalized_predict_date,
        issued_by=_require_text(issued_by, "issued_by"),
        scheme_version=_require_text(scheme_version, "scheme_version"),
        backtest_start_date=(
            normalize_backtest_start_date(backtest_start_date)
            if action == "backtest_persist"
            else None
        ),
    )


def verify_direct_operation(
    operation: DirectOperation | None,
    *,
    scheme_id: str,
    action: str,
    scheme_version: str,
    predict_date: str | None = None,
    backtest_start_date: str | None = None,
) -> tuple[DirectOperation | None, list[str]]:
    """校验命令作用域。"""
    if operation is None:
        return None, ["direct operator command is required"]
    if not isinstance(operation, DirectOperation):
        return None, ["direct operator command scope is invalid"]

    errors: list[str] = []
    if operation.scheme_id != scheme_id:
        errors.append(
            f"scheme_id mismatch: operation={operation.scheme_id}, ctx={scheme_id}"
        )
    if operation.action != action:
        errors.append(
            f"action mismatch: operation={operation.action}, expected={action}"
        )
    if operation.scheme_version != scheme_version:
        errors.append(
            "scheme_version mismatch: "
            f"operation={operation.scheme_version}, ctx={scheme_version}"
        )

    if action in EXACT_PREDICT_DATE_ACTIONS:
        try:
            expected_date = _normalize_action_predict_date(action, predict_date)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if operation.predict_date != expected_date:
                errors.append(
                    "predict_date mismatch: "
                    f"operation={operation.predict_date}, ctx={expected_date}"
                )
    elif predict_date is not None:
        errors.append(f"{action} operation does not accept predict_date")

    if action == "backtest_persist":
        try:
            expected_start = normalize_backtest_start_date(backtest_start_date)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if operation.backtest_start_date != expected_start:
                errors.append(
                    "backtest_start_date mismatch: "
                    f"operation={operation.backtest_start_date}, ctx={expected_start}"
                )
    if errors:
        return None, errors
    return operation, []


def operation_scope_sha256(operation: DirectOperation) -> str:
    """返回可持久化的命令作用域摘要。"""
    return hashlib.sha256(
        json.dumps(
            asdict(operation),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def normalize_backtest_start_date(value: str | None) -> str:
    resolved = DEFAULT_BACKTEST_START_DATE if value is None else value
    return _normalize_iso_date("backtest_start_date", resolved)


def _normalize_action_predict_date(action: str, value: str | None) -> str:
    return _normalize_iso_date(f"{action} predict_date", value)


def _normalize_iso_date(field: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a canonical YYYY-MM-DD date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a canonical YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be a canonical YYYY-MM-DD date")
    return value


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value
