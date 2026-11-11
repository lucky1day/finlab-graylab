from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping

from harness.signal_gap_plan import (
    RegistryTarget,
    SignalGapPlanError,
    expected_signal_case_from_calendar_rows,
)
from harness.gates.consistency.common import (
    DatabaseSnapshot,
    _actual_selector,
    _backtest_source_key,
    _direction,
    _iso_date,
    _json_mapping,
    _product_business_key,
    _product_reference_key,
    _required_text,
)


def validate_snapshot_completeness(
    snapshot: DatabaseSnapshot,
    *,
    display_until: str,
) -> tuple[list[str], dict[str, Any]]:
    """用成功 run 与不可变回测事实建立预期键并核对产品事实。"""
    errors: list[str] = []
    authority_gaps: set[str] = set()
    product_rows = list(snapshot.prediction_rows)
    product_by_backtest_run: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in product_rows:
        if row.get("backtest_run_id") is not None:
            product_by_backtest_run[int(row["backtest_run_id"])].append(row)

    registry_scopes = {
        (
            str(row["base_scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
        )
        for row in snapshot.registry_rows
    }
    referenced_backtest_scopes = {
        (
            str(row["scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
        )
        for row in product_rows
        if row.get("backtest_run_id") is not None
    }
    missing_backtest_authority = sorted(
        registry_scopes - referenced_backtest_scopes
    )
    if missing_backtest_authority:
        authority_gaps.add(
            "selected Registry scopes have no durable backtest product "
            f"reference: {missing_backtest_authority!r}"
        )

    source_by_backtest_run: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in snapshot.backtest_prediction_rows:
        source_by_backtest_run[int(row["run_id"])].append(row)
    # 单一结果段：业务键只需一条产品事实；live 已覆盖的回测源键不再要求发布。
    live_product_keys = {
        _product_business_key(row)
        for row in product_rows
        if row.get("run_id") is not None
    }
    backtest_expected = 0
    backtest_live_covered = 0
    for run in snapshot.backtest_runs:
        run_id = int(run["reference_id"])
        source_rows = source_by_backtest_run.get(run_id, [])
        if not source_rows:
            authority_gaps.add(
                f"backtest run {run_id} has no immutable source predictions"
            )
            continue
        summary = _json_mapping(run.get("summary"), field="backtest summary")
        declared_count = summary.get("persisted_prediction_count")
        if not isinstance(declared_count, int) or declared_count < 0:
            authority_gaps.add(
                f"backtest run {run_id} lacks persisted_prediction_count authority"
            )
            declared_count = None
        if declared_count is not None and declared_count != len(source_rows):
            errors.append(
                f"backtest source count mismatch: run_id={run_id} "
                f"summary={declared_count} source={len(source_rows)}"
            )
        source_index = {
            _backtest_source_key(row): row for row in source_rows
        }
        if len(source_index) != len(source_rows):
            errors.append(f"duplicate immutable backtest key: run_id={run_id}")
        product_for_run = product_by_backtest_run.get(run_id, [])
        product_index = {
            _product_reference_key(row): row for row in product_for_run
        }
        if len(product_index) != len(product_for_run):
            errors.append(f"duplicate product backtest key: run_id={run_id}")
        unpublished = set(source_index) - set(product_index)
        live_covered = {
            key
            for key in unpublished
            if (str(run["scheme_id"]), *key) in live_product_keys
        }
        backtest_live_covered += len(live_covered)
        missing = sorted(unpublished - live_covered)
        extra = sorted(set(product_index) - set(source_index))
        if missing:
            errors.append(
                f"missing product predictions from backtest run {run_id}: {missing!r}"
            )
        if extra:
            errors.append(
                f"extra product predictions for backtest run {run_id}: {extra!r}"
            )
        for key in sorted(set(source_index) & set(product_index)):
            source = source_index[key]
            product = product_index[key]
            expected_values = (
                _iso_date(source.get("predict_date"), field="source predict_date"),
                _iso_date(source.get("feature_date"), field="source feature_date"),
                _direction(source.get("predicted_direction"), field="source direction"),
                _direction(source.get("label"), field="source label"),
            )
            product_values = (
                _iso_date(product.get("predict_date"), field="predict_date"),
                _iso_date(product.get("feature_date"), field="feature_date"),
                _direction(product.get("predicted_direction"), field="direction"),
                _direction(
                    product.get("backtest_actual_direction"),
                    field="backtest_actual_direction",
                ),
            )
            if product_values != expected_values:
                errors.append(
                    f"backtest product fact drift: run_id={run_id} key={key!r}"
                )
        backtest_expected += len(source_rows)

    registry_by_base: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in snapshot.registry_rows:
        registry_by_base[str(row["base_scheme_id"])].append(row)
    expected_live_by_key: dict[
        tuple[str, str, int, str], set[int]
    ] = defaultdict(set)
    unresolved_live_run_ids: set[int] = set()
    for run in snapshot.live_runs:
        if run.get("status") != "success":
            continue
        predict_date = _iso_date(run.get("predict_date"), field="run predict_date")
        if predict_date > display_until:
            continue
        base_scheme_id = _required_text(
            run.get("scheme_id"), field="run scheme_id"
        )
        targets = registry_by_base.get(base_scheme_id, [])
        run_id = int(run["reference_id"])
        if not targets:
            authority_gaps.add(
                f"live run {run_id} has no selected Registry targets"
            )
            unresolved_live_run_ids.add(run_id)
            continue
        run_expected_keys: list[tuple[str, str, int, str]] = []
        for registry in targets:
            target = RegistryTarget(
                registry_scheme_id=str(registry["scheme_id"]),
                base_scheme_id=base_scheme_id,
                runtime_type=str(registry["runtime_type"]),
                frequency=str(registry["frequency"]),
                task_type=str(registry["task_type"]),
                target_tenor=str(registry["target_tenor"]),
                horizon=int(registry["horizon"]),
                scheme_version=str(run.get("scheme_version") or ""),
            )
            try:
                expected = expected_signal_case_from_calendar_rows(
                    target,
                    predict_date=predict_date,
                    calendar_rows=snapshot.calendar_rows,
                )
            except (SignalGapPlanError, ValueError) as exc:
                authority_gaps.add(
                    f"live run {run_id} date authority unavailable: {exc}"
                )
                unresolved_live_run_ids.add(run_id)
                continue
            run_expected_keys.append(expected.business_key)
        if run_id not in unresolved_live_run_ids:
            for business_key in run_expected_keys:
                expected_live_by_key[business_key].add(run_id)
    observed_live_by_key = {
        _product_business_key(row): row
        for row in product_rows
        if row.get("run_id") is not None
        and int(row["run_id"]) not in unresolved_live_run_ids
    }
    missing_live = sorted(
        set(expected_live_by_key) - set(observed_live_by_key)
    )
    extra_live = sorted(
        set(observed_live_by_key) - set(expected_live_by_key)
    )
    if missing_live:
        errors.append(f"missing live product predictions: keys={missing_live!r}")
    if extra_live:
        errors.append(f"extra live product predictions: keys={extra_live!r}")
    for business_key, product in observed_live_by_key.items():
        expected_run_ids = expected_live_by_key.get(business_key)
        if expected_run_ids is None:
            continue
        run_id = int(product["run_id"])
        if run_id not in expected_run_ids:
            errors.append(
                "live product run binding drift: "
                f"key={business_key!r} run_id={run_id} "
                f"expected_run_ids={sorted(expected_run_ids)!r}"
            )
    live_expected = len(expected_live_by_key)

    actual_keys = {
        (
            str(row["actual_kind"]),
            str(row["target_tenor"]),
            _iso_date(row.get("target_date"), field="actual target_date"),
            str(row["target_rule"]),
        )
        for row in snapshot.actual_rows
        if row.get("actual_direction") is not None
    }
    actual_watermarks: dict[tuple[str, str, str], str] = {}
    for row in snapshot.actual_watermarks:
        if row.get("max_target_date") is None:
            continue
        scope = (
            str(row["actual_kind"]),
            str(row["target_tenor"]),
            str(row["target_rule"]),
        )
        actual_watermarks[scope] = _iso_date(
            row["max_target_date"],
            field="Actual watermark",
        )
    if not snapshot.actual_watermarks:
        for kind, tenor, target_date, rule in actual_keys:
            scope = (kind, tenor, rule)
            actual_watermarks[scope] = max(
                target_date,
                actual_watermarks.get(scope, target_date),
            )
    mature_actual_missing = 0
    pending_actual = 0
    registry_by_scope = {
        (
            str(row["base_scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
        ): row
        for row in snapshot.registry_rows
    }
    for row in product_rows:
        if row.get("backtest_actual_direction") is not None:
            continue
        registry = registry_by_scope.get(
            (str(row["scheme_id"]), str(row["target_tenor"]), int(row["horizon"]))
        )
        if registry is None:
            continue
        actual_kind, target_rule = _actual_selector(str(registry["task_type"]))
        target_date = _iso_date(row.get("target_date"), field="target_date")
        actual_key = (
            actual_kind,
            str(row["target_tenor"]),
            target_date,
            target_rule,
        )
        watermark = actual_watermarks.get(actual_key[:2] + actual_key[3:])
        if actual_key in actual_keys:
            continue
        if watermark is not None and target_date <= watermark:
            mature_actual_missing += 1
            errors.append(f"mature Actual is missing: {actual_key!r}")
        else:
            pending_actual += 1

    return errors, {
        "expected_live_fact_count": live_expected,
        "expected_backtest_fact_count": backtest_expected,
        "backtest_source_live_covered_count": backtest_live_covered,
        "observed_product_fact_count": len(product_rows),
        "authority_gaps": sorted(authority_gaps),
        "mature_actual_missing_count": mature_actual_missing,
        "pending_actual_count": pending_actual,
    }
