"""Actual 刷新阶段可对外报告的稳定失败分类。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


class ActualStageError(RuntimeError):
    """携带稳定错误码的 Actual 阶段失败。"""

    code = "stage_failed"


class ActualSourceIncompleteError(ActualStageError):
    """Actual 源事实不足以完成当前阶段。"""

    code = "source_incomplete"


def require_actual_source_tenors(
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_tenors: Iterable[str],
    stage: str,
) -> list[dict[str, Any]]:
    """物化源行并拒绝完全缺失的 active tenor。"""
    materialized = [dict(row) for row in rows]
    expected = {str(tenor) for tenor in expected_tenors}
    observed = {
        str(row["tenor"])
        for row in materialized
        if row.get("tenor") is not None
    }
    missing = sorted(expected - observed)
    if missing:
        raise ActualSourceIncompleteError(
            f"{stage} Actual source is missing active tenors: {missing}"
        )
    return materialized
