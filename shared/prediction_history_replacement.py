from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from shared.legacy_prediction_migration import (
    LegacyCorrectedExactEvidence,
    LegacyPredictionMigration,
)


class PredictionHistoryReplacementError(ValueError):
    """历史替代事实不完整或与批准摘要不一致。"""


@dataclass(frozen=True, slots=True)
class PredictionHistoryProjection:
    """一次精确历史替代解析后的查询投影。"""

    source_rows: tuple[dict[str, Any], ...]
    excluded_product_ids: frozenset[int]
    deletable_product_ids: frozenset[int]
    deletable_live_run_ids: frozenset[int]
    deletable_backtest_run_ids: frozenset[int]


def resolve_prediction_history_projection(
    product_rows: Iterable[Mapping[str, Any]],
    source_rows: Iterable[Mapping[str, Any]],
    *,
    entry: LegacyPredictionMigration,
    corrected: LegacyCorrectedExactEvidence,
) -> PredictionHistoryProjection | None:
    """验证完整替代集，并返回展示投影与获批删除集合。"""
    products = [dict(row) for row in product_rows]
    sources = [dict(row) for row in source_rows]
    if not sources:
        return None
    _validate_source_identity(entry, corrected)

    source_backtest = [
        row
        for row in sources
        if row.get("backtest_run_id") is not None
        and str(row.get("target_date")) < entry.live_target_date_from
    ]
    source_live = [
        row
        for row in sources
        if row.get("run_id") is not None
        and entry.live_target_date_from
        <= str(row.get("target_date"))
        <= entry.live_target_date_through
    ]
    if len(source_backtest) != entry.expected_fact_count:
        raise PredictionHistoryReplacementError(
            "designated backtest replacement count changed"
        )
    if _corrected_rows(source_backtest) != _corrected_evidence_rows(corrected):
        raise PredictionHistoryReplacementError(
            "designated backtest replacement facts changed"
        )
    if (
        len(source_live) != entry.expected_live_fact_count
        or live_fact_digest(source_live) != entry.designated_live_facts_sha256
    ):
        raise PredictionHistoryReplacementError(
            "designated live replacement facts changed"
        )
    if len(source_backtest) + len(source_live) != len(sources):
        raise PredictionHistoryReplacementError(
            "designated replacement exact contains unapproved facts"
        )

    legacy_backtest = [
        row
        for row in products
        if row.get("scheme_version") in {None, ""}
        and row.get("backtest_run_id") is not None
    ]
    if legacy_backtest and (
        len(legacy_backtest) != entry.expected_fact_count
        or _legacy_product_digest(legacy_backtest) != entry.product_facts_sha256
    ):
        raise PredictionHistoryReplacementError(
            "legacy backtest product facts are partial or changed"
        )

    legacy_live = [
        row
        for row in products
        if row.get("run_id") is not None
        and entry.live_target_date_from
        <= str(row.get("target_date"))
        <= entry.live_target_date_through
    ]
    if legacy_live and (
        len(legacy_live) != entry.expected_live_fact_count
        or live_fact_digest(legacy_live) != entry.product_live_facts_sha256
    ):
        raise PredictionHistoryReplacementError(
            "legacy live product facts are partial or changed"
        )

    source_by_fact = {_comparison_fact(row) for row in sources}
    display_duplicates = {
        _required_id(row)
        for row in products
        if _comparison_fact(row) in source_by_fact
    }
    deletable_ids = {
        *(_required_id(row) for row in legacy_backtest),
        *(_required_id(row) for row in legacy_live),
    }
    projected = tuple(
        {
            **row,
            "scheme_id": entry.prediction_scheme_id,
            "lineage_scheme_id": entry.designated_source_scheme_id,
            "history_replacement": True,
        }
        for row in sources
    )
    return PredictionHistoryProjection(
        source_rows=projected,
        excluded_product_ids=frozenset(display_duplicates | deletable_ids),
        deletable_product_ids=frozenset(deletable_ids),
        deletable_live_run_ids=frozenset(
            int(row["run_id"]) for row in legacy_live
        ),
        deletable_backtest_run_ids=frozenset(
            int(row["backtest_run_id"]) for row in legacy_backtest
        ),
    )


def live_fact_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    """计算不含宿主主键和方案别名的 live 事实摘要。"""
    payload = sorted(
        (_live_fact(row) for row in rows),
        key=lambda row: (
            row["target_tenor"],
            row["horizon"],
            row["target_date"],
            row["predict_date"],
        ),
    )
    return _digest(payload)


def _validate_source_identity(
    entry: LegacyPredictionMigration,
    corrected: LegacyCorrectedExactEvidence,
) -> None:
    if (
        corrected.source_scheme_id != entry.designated_source_scheme_id
        or corrected.designated_exact != entry.designated_exact
        or corrected.registry_status != "archived"
        or corrected.version_status != "retired"
        or corrected.code_hash != entry.designated_code_hash
        or corrected.config_hash != entry.designated_config_hash
        or corrected.manifest_hash != entry.designated_manifest_hash
        or corrected.corrected_facts_sha256
        != entry.designated_corrected_facts_sha256
    ):
        raise PredictionHistoryReplacementError(
            "designated replacement identity changed"
        )


def _corrected_rows(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "scheme_id": str(row["scheme_id"]),
                "target_tenor": str(row["target_tenor"]),
                "horizon": int(row["horizon"]),
                "predict_date": str(row["predict_date"]),
                "feature_date": str(row["feature_date"]),
                "target_date": str(row["target_date"]),
                "predicted_direction": int(row["predicted_direction"]),
                "actual_direction": int(row["backtest_actual_direction"]),
            }
            for row in rows
        ),
        key=lambda row: (
            row["target_tenor"],
            row["horizon"],
            row["target_date"],
            row["predict_date"],
        ),
    )


def _corrected_evidence_rows(
    corrected: LegacyCorrectedExactEvidence,
) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "scheme_id": fact.scheme_id,
                "target_tenor": fact.target_tenor,
                "horizon": fact.horizon,
                "predict_date": fact.predict_date,
                "feature_date": fact.feature_date,
                "target_date": fact.target_date,
                "predicted_direction": fact.predicted_direction,
                "actual_direction": fact.actual_direction,
            }
            for fact in corrected.corrected_facts
        ),
        key=lambda row: (
            row["target_tenor"],
            row["horizon"],
            row["target_date"],
            row["predict_date"],
        ),
    )


def _legacy_product_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    payload = []
    for row in rows:
        fact = _live_fact(row, require_actual=True)
        payload.append(
            {
                "scheme_id": str(row["scheme_id"]),
                "target_tenor": fact["target_tenor"],
                "horizon": fact["horizon"],
                "predict_date": fact["predict_date"],
                "feature_date": fact["feature_date"],
                "target_date": fact["target_date"],
                "predicted_direction": fact["predicted_direction"],
                "actual_direction": fact["backtest_actual_direction"],
            }
        )
    payload.sort(
        key=lambda row: (
            row["target_tenor"],
            row["horizon"],
            row["target_date"],
            row["predict_date"],
        )
    )
    return _digest(payload)


def _live_fact(
    row: Mapping[str, Any],
    *,
    require_actual: bool = False,
) -> dict[str, Any]:
    actual = row.get("backtest_actual_direction")
    if require_actual and actual is None:
        raise PredictionHistoryReplacementError(
            "legacy backtest product actual is missing"
        )
    return {
        "target_tenor": str(row["target_tenor"]),
        "horizon": int(row["horizon"]),
        "predict_date": str(row["predict_date"]),
        "feature_date": str(row["feature_date"]),
        "target_date": str(row["target_date"]),
        "predicted_direction": int(row["predicted_direction"]),
        "backtest_actual_direction": None if actual is None else int(actual),
    }


def _comparison_fact(row: Mapping[str, Any]) -> tuple[Any, ...]:
    fact = _live_fact(row)
    return tuple(fact.values())


def _required_id(row: Mapping[str, Any]) -> int:
    try:
        return int(row["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PredictionHistoryReplacementError(
            "replacement row id is missing"
        ) from exc


def _digest(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
