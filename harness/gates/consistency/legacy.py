from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

from scheduler.repository import registry_scheme_id
from shared.legacy_prediction_migration import (
    LegacyCorrectedExactEvidence,
    LegacyPredictionMigration,
)
from shared.legacy_backtest_lineage import LegacyBacktestLineage
from harness.gates.consistency.common import (
    DataConsistencyError,
    DatabaseSnapshot,
    _LegacyBacktestLineageMatch,
    _LegacyMigrationMatch,
    _canonical_json_sha256,
    _direction,
    _iso_date,
    _json_mapping,
    _positive_int,
    _required_text,
)
from harness.gates.consistency.frozen_calendar import (
    _FrozenCalendar,
    _validate_task_date_contract,
)


def _legacy_fact(
    row: Mapping[str, Any],
    *,
    product: bool,
) -> dict[str, Any]:
    return {
        "scheme_id": _required_text(row.get("scheme_id"), field="scheme_id"),
        "target_tenor": _required_text(
            row.get("target_tenor"), field="target_tenor"
        ),
        "horizon": _positive_int(row.get("horizon"), field="horizon"),
        "predict_date": _iso_date(row.get("predict_date"), field="predict_date"),
        "feature_date": _iso_date(row.get("feature_date"), field="feature_date"),
        "target_date": _iso_date(row.get("target_date"), field="target_date"),
        "predicted_direction": _direction(
            row.get("predicted_direction"), field="predicted_direction"
        ),
        "actual_direction": _direction(
            row.get("backtest_actual_direction" if product else "label"),
            field="backtest actual direction",
        ),
    }


def _resolve_legacy_migration_matches(
    snapshot: DatabaseSnapshot,
    entries: frozenset[LegacyPredictionMigration],
    corrected_exact_evidence: frozenset[LegacyCorrectedExactEvidence],
) -> dict[int, _LegacyMigrationMatch]:
    """只在整批不可变证据逐字段命中时接受已批准迁移例外。"""
    if not entries:
        return {}
    calendar = _FrozenCalendar.from_rows(snapshot.calendar_rows)
    runs = {int(row["reference_id"]): row for row in snapshot.backtest_runs}
    sources_by_run: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in snapshot.backtest_prediction_rows:
        sources_by_run[int(row["run_id"])].append(row)
    evidence_by_identity = {
        (item.source_scheme_id, item.designated_exact): item
        for item in corrected_exact_evidence
    }

    matches: dict[int, _LegacyMigrationMatch] = {}
    for entry in entries:
        products = [
            row
            for row in snapshot.prediction_rows
            if str(row.get("scheme_id") or "") == entry.prediction_scheme_id
            and str(row.get("target_tenor") or "") == entry.target_tenor
            and int(row.get("horizon") or 0) == entry.horizon
            and row.get("backtest_run_id") is not None
            and not row.get("scheme_version")
        ]
        if not products:
            continue
        run_ids = {int(row["backtest_run_id"]) for row in products}
        if len(run_ids) != 1:
            raise DataConsistencyError(
                "legacy migration evidence drift: expected one immutable "
                f"backtest run for {entry.prediction_scheme_id}"
            )
        run_id = next(iter(run_ids))
        if run_id in matches:
            raise DataConsistencyError(
                f"legacy migration evidence drift: duplicate run {run_id}"
            )
        run = runs.get(run_id)
        sources = sources_by_run.get(run_id, [])
        try:
            corrected = evidence_by_identity.get(
                (
                    entry.designated_source_scheme_id,
                    entry.designated_exact,
                )
            )
            if corrected is None:
                raise ValueError("designated corrected exact evidence is missing")
            if (
                corrected.source_registry_scheme_id
                != registry_scheme_id(
                    entry.designated_source_scheme_id,
                    entry.horizon,
                    entry.target_tenor,
                )
                or corrected.registry_status != "archived"
                or corrected.runtime_type != "blackbox_v2"
                or corrected.version_status != "retired"
                or corrected.code_hash != entry.designated_code_hash
                or corrected.config_hash != entry.designated_config_hash
                or corrected.manifest_hash != entry.designated_manifest_hash
                or corrected.input_artifact_id
                != entry.designated_input_artifact_id
                or corrected.backtest_status != "success"
                or corrected.persisted_prediction_count
                != entry.expected_fact_count
                or corrected.corrected_facts_sha256
                != entry.designated_corrected_facts_sha256
            ):
                raise ValueError("designated corrected exact identity changed")
            if run is None:
                raise ValueError("backtest run is missing")
            if (
                str(run.get("scheme_id") or "")
                != entry.historical_source_scheme_id
                or run.get("status") != "success"
                or run.get("code_hash") is not None
                or run.get("config_hash") is not None
                or run.get("input_artifact_hash") != entry.input_artifact_hash
            ):
                raise ValueError("historical run identity changed")
            summary = _json_mapping(run.get("summary"), field="backtest summary")
            if (
                summary.get("scheme_version") is not None
                or summary.get("manifest_hash") is not None
                or _canonical_json_sha256(summary)
                != entry.backtest_summary_sha256
            ):
                raise ValueError("historical summary changed")
            summary_counts = (
                summary.get("raw_row_count"),
                summary.get("row_count"),
                (
                    summary.get("evaluation_filter", {}).get(
                        "included_row_count"
                    )
                    if isinstance(summary.get("evaluation_filter"), Mapping)
                    else None
                ),
            )
            if summary_counts != (entry.expected_fact_count,) * 3:
                raise ValueError("historical summary count authority changed")
            if (
                len(products) != entry.expected_fact_count
                or len(sources) != entry.expected_fact_count
            ):
                raise ValueError("historical fact count changed")
            product_facts = sorted(
                (_legacy_fact(row, product=True) for row in products),
                key=_legacy_fact_sort_key,
            )
            source_facts = sorted(
                (_legacy_fact(row, product=False) for row in sources),
                key=_legacy_fact_sort_key,
            )
            if (
                _canonical_json_sha256(product_facts)
                != entry.product_facts_sha256
                or _canonical_json_sha256(source_facts)
                != entry.source_facts_sha256
                or product_facts != source_facts
            ):
                raise ValueError("historical facts changed")
            mismatches: list[dict[str, str]] = []
            mismatch_keys: set[tuple[str, str, int, str]] = set()
            for fact in product_facts:
                expected_target = calendar.nth_trading_day_after(
                    fact["feature_date"],
                    int(fact["horizon"]),
                )
                if fact["target_date"] == expected_target:
                    continue
                mismatches.append(
                    {
                        "predict_date": fact["predict_date"],
                        "feature_date": fact["feature_date"],
                        "target_date": fact["target_date"],
                        "expected_target_date": expected_target,
                    }
                )
                mismatch_keys.add(
                    (
                        fact["scheme_id"],
                        fact["target_tenor"],
                        int(fact["horizon"]),
                        fact["target_date"],
                    )
                )
            if (
                len(mismatches) != entry.target_contract_mismatch_count
                or _canonical_json_sha256(mismatches)
                != entry.target_contract_mismatch_sha256
            ):
                raise ValueError("historical target-date exception changed")
            corrected_facts = sorted(
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
                key=_legacy_fact_sort_key,
            )
            if len(corrected_facts) != entry.expected_fact_count:
                raise ValueError("designated corrected fact count changed")
            for fact in corrected_facts:
                if (
                    fact["scheme_id"] != entry.designated_source_scheme_id
                    or fact["target_tenor"] != entry.target_tenor
                    or int(fact["horizon"]) != entry.horizon
                ):
                    raise ValueError("designated corrected fact scope changed")
                _validate_task_date_contract(
                    task_type="T+5",
                    horizon=entry.horizon,
                    predict_date=fact["predict_date"],
                    feature_date=fact["feature_date"],
                    target_date=fact["target_date"],
                    is_live=False,
                    calendar=calendar,
                    business_key=(
                        fact["scheme_id"],
                        fact["target_tenor"],
                        int(fact["horizon"]),
                        fact["target_date"],
                    ),
                )
            old_identity = sorted(
                _legacy_comparison_identity(fact)
                for fact in product_facts
            )
            corrected_identity = sorted(
                _legacy_comparison_identity(fact)
                for fact in corrected_facts
            )
            if old_identity != corrected_identity:
                raise ValueError(
                    "designated corrected facts do not match legacy signals"
                )
        except (DataConsistencyError, ValueError, TypeError) as exc:
            raise DataConsistencyError(
                "legacy migration evidence drift: "
                f"scheme_id={entry.prediction_scheme_id}: {exc}"
            ) from exc
        matches[run_id] = _LegacyMigrationMatch(
            entry=entry,
            backtest_run_id=run_id,
            mismatch_business_keys=frozenset(mismatch_keys),
        )
    return matches


def _resolve_legacy_backtest_lineage_matches(
    snapshot: DatabaseSnapshot,
    entries: frozenset[LegacyBacktestLineage],
) -> dict[int, _LegacyBacktestLineageMatch]:
    """仅在版本、run、summary 与两侧逐事实摘要全部命中时恢复旧血缘。"""
    if not entries:
        return {}
    runs = {int(row["reference_id"]): row for row in snapshot.backtest_runs}
    versions = {
        (str(row["scheme_id"]), str(row["scheme_version"])): row
        for row in snapshot.version_rows
    }
    sources_by_run: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in snapshot.backtest_prediction_rows:
        sources_by_run[int(row["run_id"])].append(row)

    matches: dict[int, _LegacyBacktestLineageMatch] = {}
    for entry in entries:
        products = [
            row
            for row in snapshot.prediction_rows
            if str(row.get("scheme_id") or "") == entry.scheme_id
            and str(row.get("scheme_version") or "")
            == entry.scheme_version
            and str(row.get("target_tenor") or "") == entry.target_tenor
            and int(row.get("horizon") or 0) == entry.horizon
            and row.get("backtest_run_id") is not None
        ]
        if not products:
            continue
        try:
            run_ids = {int(row["backtest_run_id"]) for row in products}
            if len(run_ids) != 1:
                raise ValueError("expected one immutable backtest run")
            run_id = next(iter(run_ids))
            if run_id in matches:
                raise ValueError("duplicate immutable backtest run")
            run = runs.get(run_id)
            version = versions.get((entry.scheme_id, entry.scheme_version))
            sources = sources_by_run.get(run_id, [])
            if run is None or version is None:
                raise ValueError("run or version evidence is missing")
            if (
                str(run.get("scheme_id") or "") != entry.scheme_id
                or run.get("status") != "success"
                or run.get("benchmark_id") != entry.benchmark_id
                or run.get("code_hash") is not None
                or run.get("config_hash") is not None
                or run.get("input_artifact_hash") is not None
            ):
                raise ValueError("legacy run identity changed")
            if (
                version.get("runtime_type") != "blackbox_v2"
                or version.get("data_snapshot_id") != entry.input_snapshot_id
                or version.get("code_hash") != entry.code_hash
                or version.get("config_hash") != entry.config_hash
                or version.get("manifest_hash") != entry.manifest_hash
            ):
                raise ValueError("legacy exact version evidence changed")
            summary = _json_mapping(run.get("summary"), field="backtest summary")
            if (
                summary.get("scheme_version") != entry.scheme_version
                or summary.get("data_snapshot_id")
                != entry.input_snapshot_id
                or summary.get("manifest_hash") is not None
                or _canonical_json_sha256(summary)
                != entry.backtest_summary_sha256
            ):
                raise ValueError("legacy backtest summary changed")
            summary_counts = (
                summary.get("raw_row_count"),
                summary.get("row_count"),
                summary.get("persisted_prediction_count"),
                (
                    summary.get("evaluation_filter", {}).get(
                        "included_row_count"
                    )
                    if isinstance(summary.get("evaluation_filter"), Mapping)
                    else None
                ),
            )
            if summary_counts != (entry.expected_fact_count,) * 4:
                raise ValueError("legacy backtest summary count changed")
            if (
                len(products) != entry.expected_fact_count
                or len(sources) != entry.expected_fact_count
            ):
                raise ValueError("legacy backtest fact count changed")
            source_facts = sorted(
                (_legacy_fact(row, product=False) for row in sources),
                key=_legacy_fact_sort_key,
            )
            product_facts = sorted(
                (
                    {
                        **_legacy_fact(row, product=True),
                        "scheme_version": _required_text(
                            row.get("scheme_version"),
                            field="prediction exact",
                        ),
                    }
                    for row in products
                ),
                key=_legacy_fact_sort_key,
            )
            comparable_products = [
                {
                    key: value
                    for key, value in fact.items()
                    if key != "scheme_version"
                }
                for fact in product_facts
            ]
            if (
                _canonical_json_sha256(source_facts)
                != entry.backtest_facts_sha256
                or _canonical_json_sha256(product_facts)
                != entry.product_facts_sha256
                or source_facts != comparable_products
            ):
                raise ValueError("legacy backtest facts changed")
        except (DataConsistencyError, TypeError, ValueError) as exc:
            raise DataConsistencyError(
                "legacy backtest lineage evidence drift: "
                f"scheme_id={entry.scheme_id} exact={entry.scheme_version}: "
                f"{exc}"
            ) from exc
        matches[run_id] = _LegacyBacktestLineageMatch(
            entry=entry,
            backtest_run_id=run_id,
        )
    return matches


def _unversioned_facts_sha256(rows: Iterable[Mapping[str, Any]]) -> str:
    """exact 为空的回测产品事实的规范摘要，与冻结清单的生成口径一致。"""
    facts = sorted(
        (_legacy_fact(row, product=True) for row in rows),
        key=_legacy_fact_sort_key,
    )
    return _canonical_json_sha256(facts)


def _legacy_fact_sort_key(fact: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        fact["target_tenor"],
        fact["horizon"],
        fact["target_date"],
        fact["predict_date"],
    )


def _legacy_comparison_identity(fact: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        fact["target_tenor"],
        fact["horizon"],
        fact["predict_date"],
        fact["feature_date"],
        fact["predicted_direction"],
        fact["actual_direction"],
    )


def _legacy_migration_evidence(
    matches: Mapping[int, _LegacyMigrationMatch],
) -> list[dict[str, Any]]:
    return [
        {
            "prediction_scheme_id": match.entry.prediction_scheme_id,
            "designated_source_scheme_id": (
                match.entry.designated_source_scheme_id
            ),
            "designated_exact": match.entry.designated_exact,
            "fact_count": match.entry.expected_fact_count,
            "target_contract_mismatch_count": (
                match.entry.target_contract_mismatch_count
            ),
        }
        for _, match in sorted(matches.items())
    ]


def _legacy_backtest_lineage_evidence(
    matches: Mapping[int, _LegacyBacktestLineageMatch],
) -> list[dict[str, Any]]:
    return [
        {
            "scheme_id": match.entry.scheme_id,
            "scheme_version": match.entry.scheme_version,
            "backtest_run_id": match.backtest_run_id,
            "benchmark_id": match.entry.benchmark_id,
            "input_snapshot_id": match.entry.input_snapshot_id,
            "fact_count": match.entry.expected_fact_count,
        }
        for match in sorted(
            matches.values(), key=lambda item: item.backtest_run_id
        )
    ]
