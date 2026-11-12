"""Dashboard 展示对账：HTTP 响应必须等于后端同一套构建函数在同一数据库上的输出。

聚合、分区与日期口径只在 backend 定义一处；这里只验证 HTTP 路径没有改变、丢失或使用过期缓存。
完整性与血缘由独立的 completeness / lineage 校验负责。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

_VOLATILE_KEYS = frozenset({"snapshot_id", "generated_at"})


def default_summary(engine: Any) -> Mapping[str, Any]:
    """用后端构建函数生成预期 Summary，并转为与 HTTP JSON 同型的规范化载荷。"""
    from backend.factor_lab_dashboard import build_factor_lab_dashboard, encode_canonical_snapshot

    return json.loads(encode_canonical_snapshot(build_factor_lab_dashboard(engine)).raw_body)


def default_detail(
    engine: Any, *, scheme_id: str, month: str, source: str
) -> Mapping[str, Any] | None:
    """用后端构建函数生成预期 Detail 分区。"""
    from backend.factor_lab_dashboard import build_factor_lab_dashboard_detail, encode_canonical_snapshot

    payload = build_factor_lab_dashboard_detail(engine, scheme_id=scheme_id, month=month, source=source)
    return None if payload is None else json.loads(encode_canonical_snapshot(payload).raw_body)


def _stable(payload: Mapping[str, Any], *, drop: Sequence[str] = ()) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in _VOLATILE_KEYS and k not in drop}


def _differing_fields(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> list[str]:
    return sorted(k for k in actual.keys() | expected.keys() if actual.get(k) != expected.get(k))


def compare_summary(
    actual: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    registry_ids: Sequence[str],
) -> list[str]:
    """顶层字段与所选方案条目逐项相等；未选中的方案不参与比较。"""
    errors: list[str] = []
    fields = _differing_fields(_stable(actual, drop=("schemes",)), _stable(expected, drop=("schemes",)))
    if fields:
        errors.append(f"Dashboard Summary top-level mismatch: fields={fields}")
    for label, payload in (("actual", actual), ("expected", expected)):
        if not isinstance(payload.get("schemes"), list):
            return [*errors, f"Dashboard Summary {label} schemes must be a list"]
    for scheme_id in registry_ids:
        found = [
            [row for row in payload["schemes"] if isinstance(row, Mapping) and row.get("scheme_id") == scheme_id]
            for payload in (actual, expected)
        ]
        if len(found[0]) != 1 or len(found[1]) != 1:
            errors.append(
                f"Dashboard Summary scheme count mismatch: scheme_id={scheme_id} "
                f"api={len(found[0])} database={len(found[1])}"
            )
            continue
        fields = _differing_fields(found[0][0], found[1][0])
        if fields:
            errors.append(f"Dashboard Summary scheme mismatch: scheme_id={scheme_id} fields={fields}")
    return errors


def detail_partitions(
    expected_summary: Mapping[str, Any], registry_ids: Sequence[str]
) -> list[tuple[str, str, str]]:
    """所选方案在预期 Summary 月度行中出现的 (方案, 月份, 来源) 分区。"""
    fields = list(expected_summary.get("monthly_row_fields") or [])
    month_index, source_index = fields.index("month"), fields.index("source")
    wanted = set(registry_ids)
    partitions = {
        (row["scheme_id"], monthly[month_index], monthly[source_index])
        for row in expected_summary.get("schemes", [])
        if row.get("scheme_id") in wanted
        for monthly in row.get("monthly_rows", [])
    }
    return sorted(partitions)


def compare_detail(
    actual: Mapping[str, Any],
    expected: Mapping[str, Any] | None,
    *,
    scheme_id: str,
    month: str,
    source: str,
) -> list[str]:
    if expected is None:
        return [f"Dashboard Detail partition is absent in the database: scheme_id={scheme_id} month={month} source={source}"]
    fields = _differing_fields(_stable(actual), _stable(expected))
    if not fields:
        return []
    return [f"Dashboard Detail mismatch: scheme_id={scheme_id} month={month} source={source} fields={fields}"]
