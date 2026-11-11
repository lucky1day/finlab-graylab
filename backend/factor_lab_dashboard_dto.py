"""Registry/target 源行到 Dashboard DTO 的严格字段转换。"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Mapping

from shared.scheme_config_schema import normalize_scheme_owner

from backend.factor_lab_dashboard_semantics import (
    DashboardDataError,
    live_actual_selector,
)


def _direction_value(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise DashboardDataError(
            f"dashboard {field} is invalid: {value!r}"
        ) from exc
    if result not in {-1, 0, 1} or result != value:
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    return result



def _registry_dto(
    row: Mapping[str, Any],
) -> dict[str, Any]:
    scheme_id = _required_text(row.get("scheme_id"), field="registry scheme_id")
    base_scheme_id = _required_text(
        row.get("base_scheme_id"), field="registry base_scheme_id"
    )
    target_tenor = _required_text(
        row.get("target_tenor"), field="registry target_tenor"
    )
    task_type = row.get("task_type")
    live_actual_selector(task_type)
    deployed_at = _iso_date(row.get("deployed_at"), field="registry deployed_at")
    if row.get("status") != "active":
        raise DashboardDataError(
            f"dashboard registry row is not active: scheme_id={scheme_id}"
        )
    return {
        "scheme_id": scheme_id,
        "base_scheme_id": base_scheme_id,
        "name": _required_text(row.get("name"), field="registry name"),
        "owner": _required_owner(row.get("owner")),
        "description": str(row.get("description") or ""),
        "horizon": _required_int(row.get("horizon"), field="registry horizon"),
        "task_type": task_type,
        "frequency": _required_text(
            row.get("frequency"), field="registry frequency"
        ),
        "target_tenor": target_tenor,
        "status": "active",
        "deployed_at": deployed_at,
    }



def _target_dto(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "target_code": _required_text(
            row.get("target_code"), field="target target_code"
        ),
        "display_name": _required_text(
            row.get("display_name"), field="target display_name"
        ),
        "asset_class": str(row.get("asset_class") or ""),
        "target_type": str(row.get("target_type") or ""),
        "sort_order": _required_int(row.get("sort_order"), field="target sort_order"),
        "status": _required_text(row.get("status"), field="target status"),
        "extra": _json_object(row.get("extra")),
    }



def _json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DashboardDataError(f"dashboard JSON object is invalid: {value!r}") from exc
    if not isinstance(parsed, dict):
        raise DashboardDataError(f"dashboard JSON object is invalid: {value!r}")
    return parsed



def _required_text(value: Any, *, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    return result



def _required_owner(value: Any) -> str:
    try:
        return normalize_scheme_owner(value)
    except ValueError as exc:
        raise DashboardDataError(
            f"dashboard registry owner is invalid: {value!r}"
        ) from exc



def _required_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DashboardDataError(
            f"dashboard {field} is invalid: {value!r}"
        ) from exc



def _iso_date(value: Any, *, field: str) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise DashboardDataError(
                f"dashboard {field} is invalid: {value!r}"
            ) from exc
        if parsed.isoformat() == value:
            return value
    raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")

