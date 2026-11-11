from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode

from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.gates.dashboard_gate import ApiProbeError, fetch_json
from harness.http_target import validate_api_target
from harness.result import Evidence, GateResult, GateStatus, redact_gate_result
from scheduler.discovery import load_scheme_config
from scheduler.repository import registry_scheme_id
from shared.historical_reference_compatibility import (
    load_historical_backtest_references,
)
from shared.legacy_prediction_migration import (
    load_legacy_corrected_exact_evidence,
    load_legacy_prediction_migrations,
)
from shared.legacy_backtest_lineage import load_legacy_backtest_lineages
from shared.task_specs import load_legacy_native_scheme_ids
from harness.gates.consistency.common import (
    ConsistencyFact,
    DETAIL_ROW_FIELDS,
    DataConsistencyAuthorityUnavailable,
    DataConsistencyError,
    DataConsistencyTimeout,
    DatabaseSnapshot,
    HISTORY_START_DATE,
    LIVE_FEATURE_START_DATE,
    LIVE_RUN_EVIDENCE_START_DATE,
    MAX_RESPONSE_BYTES,
    MONTHLY_ROW_FIELDS,
    PROJECT_ROOT,
    SHANGHAI_TIMEZONE,
    _LegacyBacktestLineageMatch,
    _LegacyMigrationMatch,
    _actual_selector,
    _backtest_source_key,
    _canonical_json_sha256,
    _clock_display_date,
    _direction,
    _display_month,
    _is_sha256,
    _is_snapshot_id,
    _iso_date,
    _json_mapping,
    _optional_positive_int,
    _positive_int,
    _product_business_key,
    _product_reference_key,
    _remaining_budget,
    _required_text,
    _source,
    _source_registry_scope_status,
)
from harness.gates.consistency.frozen_calendar import (
    _FrozenCalendar,
    _next_month_anchor,
    _validate_task_date_contract,
)
from harness.gates.consistency.legacy import (
    _legacy_backtest_lineage_evidence,
    _legacy_comparison_identity,
    _legacy_fact,
    _legacy_fact_sort_key,
    _legacy_migration_evidence,
    _resolve_legacy_backtest_lineage_matches,
    _resolve_legacy_migration_matches,
)
from harness.gates.consistency.completeness import (
    _validated_live_only_baselines,
    validate_snapshot_completeness,
)
from harness.gates.consistency.lineage import validate_snapshot_lineage
from harness.gates.consistency.facts import (
    _compare_detail,
    _compare_summary,
    _detail_rows_by_partition,
    aggregate_display_facts,
    build_representation_facts,
    validate_and_join_facts,
)
from harness.gates.consistency.snapshot import (
    _capture_snapshot,
    _chunks,
    _execute_budgeted,
    _read_actual_watermarks,
    _read_actuals,
    _read_authoritative_live_run_ids,
    _read_backtest_predictions,
    _read_connection,
    _read_input_artifacts,
    _read_predictions,
    _read_references,
    _read_versions,
    _snapshot_digest,
)


__all__ = [
    "ConsistencyFact",
    "DETAIL_ROW_FIELDS",
    "DataConsistencyAuthorityUnavailable",
    "DataConsistencyError",
    "DataConsistencyTimeout",
    "DatabaseSnapshot",
    "HISTORY_START_DATE",
    "LIVE_FEATURE_START_DATE",
    "LIVE_RUN_EVIDENCE_START_DATE",
    "MAX_RESPONSE_BYTES",
    "MONTHLY_ROW_FIELDS",
    "PROJECT_ROOT",
    "SHANGHAI_TIMEZONE",
    "_FrozenCalendar",
    "_LegacyBacktestLineageMatch",
    "_LegacyMigrationMatch",
    "_actual_selector",
    "_backtest_source_key",
    "_canonical_json_sha256",
    "_capture_snapshot",
    "_chunks",
    "_clock_display_date",
    "_compare_detail",
    "_compare_summary",
    "_detail_rows_by_partition",
    "_direction",
    "_display_month",
    "_execute_budgeted",
    "_is_sha256",
    "_is_snapshot_id",
    "_iso_date",
    "_json_mapping",
    "_legacy_backtest_lineage_evidence",
    "_legacy_comparison_identity",
    "_legacy_fact",
    "_legacy_fact_sort_key",
    "_legacy_migration_evidence",
    "_next_month_anchor",
    "_optional_positive_int",
    "_positive_int",
    "_product_business_key",
    "_product_reference_key",
    "_read_actual_watermarks",
    "_read_actuals",
    "_read_authoritative_live_run_ids",
    "_read_backtest_predictions",
    "_read_connection",
    "_read_input_artifacts",
    "_read_predictions",
    "_read_references",
    "_read_versions",
    "_remaining_budget",
    "_required_text",
    "_resolve_legacy_backtest_lineage_matches",
    "_resolve_legacy_migration_matches",
    "_snapshot_digest",
    "_source",
    "_source_registry_scope_status",
    "_validate_task_date_contract",
    "_validated_live_only_baselines",
    "aggregate_display_facts",
    "build_representation_facts",
    "validate_and_join_facts",
    "validate_snapshot_completeness",
    "validate_snapshot_lineage",
    "_subresult",
    "_merge_subresult_error",
    "DataConsistencyGate",
    "_selected_registry_ids",
    "_fetch_json_object",
]


def _subresult(
    status: str,
    errors: Sequence[str],
    **details: Any,
) -> dict[str, Any]:
    if status not in {"PASSED", "FAILED", "BLOCKED"}:
        raise ValueError("invalid data consistency subresult status")
    return {"status": status, "errors": list(errors), **details}


def _merge_subresult_error(
    result: Mapping[str, Any],
    error: str,
    status: str,
) -> dict[str, Any]:
    merged = dict(result)
    merged["status"] = status
    merged["errors"] = [*result.get("errors", []), error]
    return merged


class DataConsistencyGate(Gate):
    """独立对账预测、Actual 与 Dashboard Summary/Detail。"""

    name = "data-consistency"

    def __init__(
        self,
        *,
        fetcher=fetch_json,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._clock = clock or (lambda: datetime.now(SHANGHAI_TIMEZONE))
        self._monotonic = monotonic or time.monotonic

    def run(self, ctx: GateContext) -> GateResult:
        result = guarded_result(
            self.name,
            lambda started_at: self._run(ctx, started_at),
        )
        return redact_gate_result(result, ctx.dashboard_session_token)

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        deadline = self._monotonic() + ctx.timeout_sec
        _remaining_budget(deadline, self._monotonic)
        selected_base_ids, registry_ids = _selected_registry_ids(ctx)
        if ctx.engine_factory is None:
            raise ValueError(
                "data consistency gate requires same-host engine_factory"
            )
        target = validate_api_target(ctx.api_base_url, ctx.api_prefix)
        summary_url = target.url("/api/factor-lab/dashboard")
        display_until = _clock_display_date(self._clock)
        engine = ctx.engine_factory()
        final_snapshot: DatabaseSnapshot | None = None
        final_snapshot_failed = False
        operation_error: Exception | None = None
        summary: Mapping[str, Any] | None = None
        summary_metadata: Mapping[str, Any] = {}
        validated_facts: list[ConsistencyFact] = []
        representation_facts: list[ConsistencyFact] = []
        detail_metadata: list[dict[str, Any]] = []
        completeness = _subresult("BLOCKED", ["completeness not evaluated"])
        lineage = _subresult("BLOCKED", ["lineage not evaluated"])
        representation = _subresult(
            "BLOCKED", ["representation not evaluated"]
        )
        try:
            _remaining_budget(deadline, self._monotonic)
            snapshot = _capture_snapshot(
                engine,
                registry_ids,
                deadline=deadline,
                monotonic=self._monotonic,
            )
            _remaining_budget(deadline, self._monotonic)
            legacy_migrations = load_legacy_prediction_migrations(
                PROJECT_ROOT
            )
            corrected_exact_evidence = (
                load_legacy_corrected_exact_evidence(PROJECT_ROOT)
            )
            legacy_backtest_lineages = load_legacy_backtest_lineages(
                PROJECT_ROOT
            )
            historical_references = load_historical_backtest_references(
                PROJECT_ROOT
            )
            try:
                completeness_errors, completeness_details = (
                    validate_snapshot_completeness(
                        snapshot,
                        display_until=display_until,
                        historical_references=historical_references,
                        legacy_migrations=legacy_migrations,
                        corrected_exact_evidence=corrected_exact_evidence,
                    )
                )
                completeness_authority_gaps = completeness_details.get(
                    "authority_gaps", []
                )
                completeness = _subresult(
                    (
                        "FAILED"
                        if completeness_errors
                        else "BLOCKED"
                        if completeness_authority_gaps
                        else "PASSED"
                    ),
                    [
                        *completeness_errors,
                        *completeness_authority_gaps,
                    ],
                    **completeness_details,
                )
            except DataConsistencyAuthorityUnavailable as exc:
                completeness = _subresult("BLOCKED", [str(exc)])
            except DataConsistencyError as exc:
                completeness = _subresult("FAILED", [str(exc)])

            try:
                lineage_errors, lineage_details = validate_snapshot_lineage(
                    snapshot,
                    historical_references=historical_references,
                    legacy_migrations=legacy_migrations,
                    corrected_exact_evidence=corrected_exact_evidence,
                    legacy_backtest_lineages=legacy_backtest_lineages,
                )
                lineage_authority_gaps = lineage_details.get(
                    "authority_gaps", []
                )
                lineage = _subresult(
                    (
                        "FAILED"
                        if lineage_errors
                        else "BLOCKED"
                        if lineage_authority_gaps
                        else "PASSED"
                    ),
                    [*lineage_errors, *lineage_authority_gaps],
                    **lineage_details,
                )
            except DataConsistencyAuthorityUnavailable as exc:
                lineage = _subresult("BLOCKED", [str(exc)])
            except DataConsistencyError as exc:
                lineage = _subresult("FAILED", [str(exc)])

            try:
                validated_facts = validate_and_join_facts(
                    snapshot,
                    display_until=display_until,
                    legacy_native_scheme_ids=load_legacy_native_scheme_ids(
                        PROJECT_ROOT
                    ),
                    historical_references=historical_references,
                    legacy_migrations=legacy_migrations,
                    corrected_exact_evidence=corrected_exact_evidence,
                )
            except DataConsistencyError as exc:
                lineage = _merge_subresult_error(lineage, str(exc), "FAILED")

            try:
                representation_facts = build_representation_facts(
                    snapshot,
                    display_until=display_until,
                )
                summary, summary_metadata = _fetch_json_object(
                    self._fetcher,
                    summary_url,
                    ctx,
                    deadline=deadline,
                    monotonic=self._monotonic,
                )
                summary_display_until = _iso_date(
                    summary.get("display_until"),
                    field="summary display_until",
                )
                expected_monthly = aggregate_display_facts(
                    representation_facts
                )
                _remaining_budget(deadline, self._monotonic)
                representation_errors = _compare_summary(
                    summary,
                    registry_ids=registry_ids,
                    expected=expected_monthly,
                )
                if summary_display_until != display_until:
                    representation_errors.append(
                        "Dashboard Summary display_until does not match the "
                        "independent Shanghai business date"
                    )

                expected_details = _detail_rows_by_partition(
                    representation_facts
                )
                for (scheme_id, month, source), rows in sorted(
                    expected_details.items()
                ):
                    detail_url = summary_url + "?" + urlencode(
                        {
                            "scheme-id": scheme_id,
                            "month": month,
                            "source": source,
                        }
                    )
                    detail, metadata = _fetch_json_object(
                        self._fetcher,
                        detail_url,
                        ctx,
                        deadline=deadline,
                        monotonic=self._monotonic,
                    )
                    representation_errors.extend(
                        _compare_detail(
                            detail,
                            scheme_id=scheme_id,
                            month=month,
                            source=source,
                            expected_rows=rows,
                            display_until=display_until,
                        )
                    )
                    detail_metadata.append(
                        {
                            "scheme_id": scheme_id,
                            "month": month,
                            "source": source,
                            "snapshot_id": detail.get("snapshot_id"),
                            "request_id": metadata.get("request_id"),
                            "fetched_at": metadata.get("fetched_at"),
                        }
                    )
                representation = _subresult(
                    "PASSED" if not representation_errors else "FAILED",
                    representation_errors,
                    summary_snapshot_id=summary.get("snapshot_id"),
                    detail_request_count=len(detail_metadata),
                )
            except DataConsistencyTimeout:
                raise
            except Exception as exc:
                representation = _subresult("FAILED", [str(exc)])
            finally:
                try:
                    _remaining_budget(deadline, self._monotonic)
                    final_snapshot = _capture_snapshot(
                        engine,
                        registry_ids,
                        deadline=deadline,
                        monotonic=self._monotonic,
                    )
                    _remaining_budget(deadline, self._monotonic)
                except DataConsistencyTimeout as exc:
                    operation_error = exc
                    final_snapshot_failed = True
                except Exception:
                    final_snapshot_failed = True
        finally:
            engine.dispose()

        evidence = [
            Evidence("base_scheme_ids", list(selected_base_ids)),
            Evidence("registry_ids", list(registry_ids)),
            Evidence("database_snapshot_digest", snapshot.digest),
            Evidence("prediction_fact_count", len(snapshot.prediction_rows)),
            Evidence("joined_fact_count", len(representation_facts)),
            Evidence(
                "business_contract_validated_fact_count",
                len(validated_facts),
            ),
            Evidence(
                "summary_snapshot_id",
                summary.get("snapshot_id") if summary is not None else None,
            ),
            Evidence("summary_request_id", summary_metadata.get("request_id")),
            Evidence("summary_fetched_at", summary_metadata.get("fetched_at")),
            Evidence("detail_requests", detail_metadata),
            Evidence("completeness", completeness),
            Evidence("lineage", lineage),
            Evidence("representation", representation),
        ]
        if isinstance(operation_error, DataConsistencyTimeout):
            raise operation_error
        if (
            final_snapshot_failed
            or final_snapshot is None
            or final_snapshot.digest != snapshot.digest
            or _clock_display_date(self._clock) != display_until
        ):
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                evidence=evidence,
                errors=[
                    "selected database facts or the Shanghai display date changed "
                    "during HTTP reconciliation; retry against a stable input window"
                ],
                started_at=started_at,
                finished_at=utc_now(),
            )
        if operation_error is not None:
            raise operation_error
        subresults = (completeness, lineage, representation)
        errors = [
            str(error)
            for subresult in subresults
            for error in subresult["errors"]
        ]
        if any(item["status"] == "FAILED" for item in subresults):
            status = GateStatus.FAILED
        elif any(item["status"] == "BLOCKED" for item in subresults):
            status = GateStatus.BLOCKED
        else:
            status = GateStatus.PASSED
        return GateResult(
            gate_name=self.name,
            status=status,
            evidence=evidence,
            errors=errors,
            started_at=started_at,
            finished_at=utc_now(),
        )


def _selected_registry_ids(ctx: GateContext) -> tuple[tuple[str, ...], tuple[str, ...]]:
    selected = ctx.dashboard_scheme_ids or (ctx.scheme_id,)
    if (
        not selected
        or len(set(selected)) != len(selected)
        or any(
            not isinstance(item, str) or not item or item != item.strip()
            for item in selected
        )
    ):
        raise ValueError(
            "data consistency gate requires unique non-empty base scheme ids"
        )
    registry_ids: list[str] = []
    for base_scheme_id in selected:
        config = load_scheme_config(
            ctx.project_root / "schemes" / base_scheme_id / "config.yaml"
        )
        if config.scheme_id != base_scheme_id:
            raise ValueError("scheme directory and canonical id mismatch")
        registry_ids.extend(
            registry_scheme_id(base_scheme_id, config.horizon, tenor)
            for tenor in config.tenors
        )
    return tuple(selected), tuple(registry_ids)


def _fetch_json_object(
    fetcher,
    url: str,
    ctx: GateContext,
    *,
    deadline: float,
    monotonic: Callable[[], float],
):
    remaining = _remaining_budget(deadline, monotonic)
    try:
        response = fetcher(
            url,
            timeout_sec=min(remaining, 30),
            max_response_bytes=MAX_RESPONSE_BYTES,
            session_token=ctx.dashboard_session_token,
        )
    except ApiProbeError as exc:
        raise DataConsistencyError(
            f"Dashboard probe failed: {exc.error_summary}"
        ) from exc
    _remaining_budget(deadline, monotonic)
    if not isinstance(response, tuple) or len(response) not in {2, 3}:
        raise DataConsistencyError("Dashboard fetcher returned an invalid result")
    payload, status = response[:2]
    metadata = response[2] if len(response) == 3 else {}
    if int(status) != 200:
        raise DataConsistencyError(f"Dashboard probe returned HTTP {int(status)}")
    if not isinstance(payload, Mapping):
        raise DataConsistencyError("Dashboard probe payload must be an object")
    if not isinstance(metadata, Mapping):
        raise DataConsistencyError("Dashboard probe metadata must be an object")
    return payload, dict(metadata)
