from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from shared.historical_reference_compatibility import HistoricalBacktestReference
from shared.legacy_prediction_migration import (
    LegacyCorrectedExactEvidence,
    LegacyPredictionMigration,
)
from shared.task_specs import runtime_task_contract
from harness.gates.consistency.common import (
    ConsistencyFact,
    DETAIL_ROW_FIELDS,
    DataConsistencyError,
    DatabaseSnapshot,
    HISTORY_START_DATE,
    MONTHLY_ROW_FIELDS,
    _actual_selector,
    _direction,
    _display_month,
    _iso_date,
    _json_mapping,
    _optional_positive_int,
    _positive_int,
    _required_text,
    _source,
    _source_registry_scope_status,
)
from harness.gates.consistency.frozen_calendar import (
    _FrozenCalendar,
    _validate_task_date_contract,
)
from harness.gates.consistency.legacy import _resolve_legacy_migration_matches


def validate_and_join_facts(
    snapshot: DatabaseSnapshot,
    *,
    display_until: str,
    legacy_native_scheme_ids: frozenset[str] = frozenset(),
    historical_references: frozenset[
        HistoricalBacktestReference
    ] = frozenset(),
    legacy_migrations: frozenset[
        LegacyPredictionMigration
    ] = frozenset(),
    corrected_exact_evidence: frozenset[
        LegacyCorrectedExactEvidence
    ] = frozenset(),
) -> list[ConsistencyFact]:
    """独立校验产品事实并按任务规则关联已发布或 live Actual。"""
    calendar = _FrozenCalendar.from_rows(snapshot.calendar_rows)
    legacy_matches = _resolve_legacy_migration_matches(
        snapshot,
        legacy_migrations,
        corrected_exact_evidence,
    )
    registry_by_scope: dict[tuple[str, str, int], Mapping[str, Any]] = {}
    for row in snapshot.registry_rows:
        scope = (
            _required_text(row.get("base_scheme_id"), field="base_scheme_id"),
            _required_text(row.get("target_tenor"), field="target_tenor"),
            _positive_int(row.get("horizon"), field="horizon"),
        )
        if scope in registry_by_scope:
            raise DataConsistencyError(f"duplicate active Registry scope: {scope!r}")
        task_type = _required_text(row.get("task_type"), field="task_type")
        runtime_type = _required_text(
            row.get("runtime_type"), field="runtime_type"
        )
        try:
            expected = runtime_task_contract(
                runtime_type=runtime_type,
                task_type=task_type,
                base_scheme_id=scope[0],
                legacy_native_scheme_ids=legacy_native_scheme_ids,
            )
        except ValueError as exc:
            raise DataConsistencyError(
                f"Registry task contract mismatch: scheme_id={row.get('scheme_id')!r}"
            ) from exc
        if expected.horizon != scope[2] or (
            row.get("frequency") is not None
            and str(row["frequency"]) != expected.frequency
        ):
            raise DataConsistencyError(
                f"Registry task contract mismatch: scheme_id={row.get('scheme_id')!r}"
            )
        registry_by_scope[scope] = row

    actuals: dict[tuple[str, str, str, str], int | None] = {}
    for row in snapshot.actual_rows:
        key = (
            _required_text(row.get("actual_kind"), field="actual kind"),
            _required_text(row.get("target_tenor"), field="actual target_tenor"),
            _iso_date(row.get("target_date"), field="actual target_date"),
            _required_text(row.get("target_rule"), field="actual target_rule"),
        )
        direction = _direction(
            row.get("actual_direction"),
            field="actual_direction",
            allow_none=True,
        )
        if key in actuals and actuals[key] != direction:
            raise DataConsistencyError(f"conflicting Actual facts: {key!r}")
        actuals[key] = direction

    live_runs = {
        int(row["reference_id"]): row for row in snapshot.live_runs
    }
    backtest_runs = {
        int(row["reference_id"]): row for row in snapshot.backtest_runs
    }
    versions = {
        (str(row["scheme_id"]), str(row["scheme_version"])): row
        for row in snapshot.version_rows
    }
    compatible_pairs = {
        (item.prediction_scheme_id, item.backtest_source_scheme_id)
        for item in historical_references
    }
    business_keys: set[tuple[str, str, int, str]] = set()
    facts: list[ConsistencyFact] = []
    for row in snapshot.prediction_rows:
        scheme_id = _required_text(row.get("scheme_id"), field="scheme_id")
        target_tenor = _required_text(
            row.get("target_tenor"), field="target_tenor"
        )
        horizon = _positive_int(row.get("horizon"), field="horizon")
        target_date = _iso_date(row.get("target_date"), field="target_date")
        business_key = (scheme_id, target_tenor, horizon, target_date)
        if business_key in business_keys:
            raise DataConsistencyError(
                "duplicate prediction business key (exact version is not part "
                f"of the key): {business_key!r}"
            )
        business_keys.add(business_key)

        registry = registry_by_scope.get((scheme_id, target_tenor, horizon))
        if registry is None:
            raise DataConsistencyError(
                f"prediction has no selected active Registry scope: {business_key!r}"
            )
        predict_date = _iso_date(row.get("predict_date"), field="predict_date")
        feature_date = _iso_date(row.get("feature_date"), field="feature_date")
        if not feature_date <= predict_date <= target_date or not feature_date < target_date:
            raise DataConsistencyError(
                f"prediction dates violate feature <= predict <= target: {business_key!r}"
            )
        predicted_direction = _direction(
            row.get("predicted_direction"),
            field="predicted_direction",
        )
        live_run_id = _optional_positive_int(row.get("run_id"), field="run_id")
        backtest_run_id = _optional_positive_int(
            row.get("backtest_run_id"), field="backtest_run_id"
        )
        published_actual = _direction(
            row.get("backtest_actual_direction"),
            field="backtest_actual_direction",
            allow_none=True,
        )
        if (live_run_id is None) == (backtest_run_id is None):
            raise DataConsistencyError(
                f"prediction run references must be mutually exclusive: {business_key!r}"
            )
        if live_run_id is not None:
            if live_run_id not in live_runs:
                raise DataConsistencyError(
                    f"prediction references missing live run: {business_key!r}"
                )
            live_run = live_runs[live_run_id]
            if live_run.get("scheme_id") != scheme_id:
                raise DataConsistencyError(
                    f"prediction references another scheme's live run: {business_key!r}"
                )
            if live_run.get("status") not in {None, "success"}:
                raise DataConsistencyError(
                    f"prediction references non-success live run: {business_key!r}"
                )
            if published_actual is not None:
                raise DataConsistencyError(
                    f"live prediction carries backtest Actual: {business_key!r}"
                )
        else:
            if backtest_run_id not in backtest_runs:
                raise DataConsistencyError(
                    f"prediction references missing backtest run: {business_key!r}"
                )
            backtest_run = backtest_runs[backtest_run_id]
            source_scheme_id = str(backtest_run.get("scheme_id") or "")
            if source_scheme_id != scheme_id:
                if (scheme_id, source_scheme_id) not in compatible_pairs:
                    raise DataConsistencyError(
                        "prediction references another scheme's backtest run: "
                        f"{business_key!r}"
                    )
                source_scope_status = _source_registry_scope_status(
                    backtest_run,
                    target_tenor=target_tenor,
                    horizon=horizon,
                )
                source_summary = _json_mapping(
                    backtest_run.get("summary"), field="backtest summary"
                )
                source_exact = str(
                    source_summary.get("scheme_version") or ""
                )
                source_version = versions.get(
                    (source_scheme_id, source_exact)
                )
                if (
                    source_scope_status != "archived"
                    or source_version is None
                    or source_version.get("status") != "retired"
                    or backtest_run.get("status") != "success"
                    or not backtest_run.get("code_hash")
                    or not backtest_run.get("config_hash")
                    or not backtest_run.get("input_artifact_hash")
                ):
                    raise DataConsistencyError(
                        "historical cross-scheme reference lacks retired source evidence: "
                        f"{business_key!r}"
                    )
            elif backtest_run.get("status") not in {None, "success"}:
                raise DataConsistencyError(
                    f"prediction references non-success backtest run: {business_key!r}"
                )
            if published_actual is None:
                raise DataConsistencyError(
                    f"backtest prediction is missing immutable Actual: {business_key!r}"
                )
            if (
                str(registry["task_type"]) != "monthly"
                and predict_date != feature_date
            ):
                raise DataConsistencyError(
                    f"backtest prediction must have predict_date=feature_date: {business_key!r}"
                )

        legacy_match = (
            legacy_matches.get(backtest_run_id)
            if backtest_run_id is not None
            else None
        )
        _validate_task_date_contract(
            task_type=str(registry["task_type"]),
            horizon=horizon,
            predict_date=predict_date,
            feature_date=feature_date,
            target_date=target_date,
            is_live=live_run_id is not None,
            calendar=calendar,
            business_key=business_key,
            allow_legacy_target_date=(
                legacy_match is not None
                and business_key in legacy_match.mismatch_business_keys
            ),
        )

        if feature_date < HISTORY_START_DATE or predict_date > display_until:
            continue
        task_type = str(registry["task_type"])
        actual_kind, target_rule = _actual_selector(task_type)
        actual_direction = (
            published_actual
            if published_actual is not None
            else actuals.get(
                (actual_kind, target_tenor, target_date, target_rule)
            )
        )
        facts.append(
            ConsistencyFact(
                scheme_id=str(registry["scheme_id"]),
                target_tenor=target_tenor,
                horizon=horizon,
                task_type=task_type,
                predict_date=predict_date,
                feature_date=feature_date,
                target_date=target_date,
                predicted_direction=predicted_direction,
                actual_direction=actual_direction,
            )
        )
    return sorted(
        facts,
        key=lambda item: (
            item.scheme_id,
            item.target_date,
            item.predict_date,
        ),
    )


def build_representation_facts(
    snapshot: DatabaseSnapshot,
    *,
    display_until: str,
) -> list[ConsistencyFact]:
    """只按持久化产品事实与 Actual 独立重建公开表示。"""
    registry_by_scope: dict[
        tuple[str, str, int], Mapping[str, Any]
    ] = {}
    for row in snapshot.registry_rows:
        scope = (
            _required_text(row.get("base_scheme_id"), field="base_scheme_id"),
            _required_text(row.get("target_tenor"), field="target_tenor"),
            _positive_int(row.get("horizon"), field="horizon"),
        )
        if scope in registry_by_scope:
            raise DataConsistencyError(
                f"duplicate active Registry scope: {scope!r}"
            )
        _required_text(row.get("scheme_id"), field="registry scheme_id")
        _required_text(row.get("task_type"), field="task_type")
        registry_by_scope[scope] = row

    actuals: dict[tuple[str, str, str, str], int | None] = {}
    for row in snapshot.actual_rows:
        key = (
            _required_text(row.get("actual_kind"), field="actual kind"),
            _required_text(
                row.get("target_tenor"), field="actual target_tenor"
            ),
            _iso_date(row.get("target_date"), field="actual target_date"),
            _required_text(
                row.get("target_rule"), field="actual target_rule"
            ),
        )
        direction = _direction(
            row.get("actual_direction"),
            field="actual_direction",
            allow_none=True,
        )
        if key in actuals and actuals[key] != direction:
            raise DataConsistencyError(f"conflicting Actual facts: {key!r}")
        actuals[key] = direction

    business_keys: set[tuple[str, str, int, str]] = set()
    facts: list[ConsistencyFact] = []
    for row in snapshot.prediction_rows:
        scheme_id = _required_text(row.get("scheme_id"), field="scheme_id")
        target_tenor = _required_text(
            row.get("target_tenor"), field="target_tenor"
        )
        horizon = _positive_int(row.get("horizon"), field="horizon")
        target_date = _iso_date(row.get("target_date"), field="target_date")
        business_key = (scheme_id, target_tenor, horizon, target_date)
        if business_key in business_keys:
            raise DataConsistencyError(
                "duplicate prediction business key (exact version is not part "
                f"of the key): {business_key!r}"
            )
        business_keys.add(business_key)

        registry = registry_by_scope.get((scheme_id, target_tenor, horizon))
        if registry is None:
            raise DataConsistencyError(
                "prediction has no selected active Registry scope: "
                f"{business_key!r}"
            )
        predict_date = _iso_date(row.get("predict_date"), field="predict_date")
        feature_date = _iso_date(row.get("feature_date"), field="feature_date")
        predicted_direction = _direction(
            row.get("predicted_direction"),
            field="predicted_direction",
        )
        published_actual = _direction(
            row.get("backtest_actual_direction"),
            field="backtest_actual_direction",
            allow_none=True,
        )
        if feature_date < HISTORY_START_DATE or predict_date > display_until:
            continue
        task_type = str(registry["task_type"])
        actual_kind, target_rule = _actual_selector(task_type)
        actual_direction = (
            published_actual
            if published_actual is not None
            else actuals.get(
                (actual_kind, target_tenor, target_date, target_rule)
            )
        )
        facts.append(
            ConsistencyFact(
                scheme_id=str(registry["scheme_id"]),
                target_tenor=target_tenor,
                horizon=horizon,
                task_type=task_type,
                predict_date=predict_date,
                feature_date=feature_date,
                target_date=target_date,
                predicted_direction=predicted_direction,
                actual_direction=actual_direction,
            )
        )
    return sorted(
        facts,
        key=lambda item: (
            item.scheme_id,
            item.target_date,
            item.predict_date,
        ),
    )


def aggregate_display_facts(
    facts: Sequence[ConsistencyFact],
) -> dict[str, list[list[Any]]]:
    """不调用 Dashboard builder，独立形成公开月份计数。"""
    groups: dict[tuple[str, str, str], list[ConsistencyFact]] = defaultdict(list)
    for fact in facts:
        groups[(fact.scheme_id, _display_month(fact), _source(fact.feature_date))].append(
            fact
        )

    result: dict[str, list[list[Any]]] = defaultdict(list)
    for (scheme_id, month, source), group in sorted(groups.items()):
        verified = [item for item in group if item.actual_direction is not None]
        directional = [
            item for item in verified if item.predicted_direction in {-1, 1}
        ]
        predicted = [item.predicted_direction for item in verified]
        actual = [int(item.actual_direction) for item in verified]
        result[scheme_id].append(
            [
                month,
                source,
                len(verified),
                len(directional),
                sum(
                    item.predicted_direction == item.actual_direction
                    for item in directional
                ),
                predicted.count(1),
                predicted.count(-1),
                predicted.count(0),
                actual.count(1),
                actual.count(-1),
                actual.count(0),
                sum(
                    item.predicted_direction == item.actual_direction == 1
                    for item in directional
                ),
                sum(
                    item.predicted_direction == item.actual_direction == -1
                    for item in directional
                ),
            ]
        )
    return dict(result)


def _detail_rows_by_partition(
    facts: Sequence[ConsistencyFact],
) -> dict[tuple[str, str, str], list[list[Any]]]:
    result: dict[tuple[str, str, str], list[list[Any]]] = defaultdict(list)
    for fact in facts:
        source = _source(fact.feature_date)
        result[(fact.scheme_id, _display_month(fact), source)].append(
            [
                source,
                fact.predict_date,
                fact.feature_date,
                fact.target_date,
                fact.predicted_direction,
                fact.actual_direction,
            ]
        )
    for rows in result.values():
        rows.sort(key=lambda row: (str(row[2]), str(row[3]), str(row[1])))
    return dict(result)


def _compare_summary(
    payload: Mapping[str, Any],
    *,
    registry_ids: Sequence[str],
    expected: Mapping[str, list[list[Any]]],
) -> list[str]:
    errors: list[str] = []
    if payload.get("representation") != "summary":
        return ["Dashboard Summary representation is invalid"]
    if payload.get("selected_feature_range") is not None:
        errors.append("Dashboard unfiltered Summary unexpectedly has a selected range")
    if payload.get("range_row_fields") != ["source", *MONTHLY_ROW_FIELDS[2:]]:
        errors.append("Dashboard Summary range_row_fields mismatch")
    if payload.get("monthly_row_fields") != list(MONTHLY_ROW_FIELDS):
        errors.append("Dashboard Summary monthly_row_fields mismatch")
    schemes = payload.get("schemes")
    if not isinstance(schemes, list):
        return errors + ["Dashboard Summary schemes must be a list"]
    for scheme_id in registry_ids:
        matches = [
            row
            for row in schemes
            if isinstance(row, Mapping) and row.get("scheme_id") == scheme_id
        ]
        if len(matches) != 1:
            errors.append(
                f"Dashboard Summary scheme count mismatch: scheme_id={scheme_id} "
                f"count={len(matches)}"
            )
            continue
        expected_sources: dict[str, list[int]] = {}
        for monthly in expected.get(scheme_id, []):
            counts = expected_sources.setdefault(monthly[1], [0] * (len(MONTHLY_ROW_FIELDS) - 2))
            for index, count in enumerate(monthly[2:]):
                counts[index] += count
        expected_range = [[source, *counts] for source, counts in sorted(expected_sources.items())]
        if matches[0].get("range_rows") != expected_range:
            errors.append(
                f"Dashboard Summary range rows mismatch: scheme_id={scheme_id}"
            )
        actual_rows = matches[0].get("monthly_rows")
        if actual_rows != expected.get(scheme_id, []):
            errors.append(
                f"Dashboard Summary monthly rows mismatch: scheme_id={scheme_id}"
            )
    return errors


def _compare_detail(
    payload: Mapping[str, Any],
    *,
    scheme_id: str,
    month: str,
    source: str,
    expected_rows: list[list[Any]],
    display_until: str,
) -> list[str]:
    errors: list[str] = []
    expected_identity = {
        "representation": "detail",
        "scheme_id": scheme_id,
        "month": month,
        "source": source,
        "row_fields": list(DETAIL_ROW_FIELDS),
        "display_until": display_until,
    }
    for field, expected in expected_identity.items():
        if payload.get(field) != expected:
            errors.append(
                f"Dashboard Detail {field} mismatch: scheme_id={scheme_id} "
                f"month={month} source={source}"
            )
    rows = payload.get("rows")
    if rows != expected_rows:
        errors.append(
            f"Dashboard Detail rows mismatch: scheme_id={scheme_id} "
            f"month={month} source={source}"
        )
    return errors
