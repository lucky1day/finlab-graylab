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
from shared.unversioned_backtest_facts import load_unversioned_backtest_facts
from harness.gates.consistency.common import (
    DataConsistencyAuthorityUnavailable,
    DataConsistencyError,
    DataConsistencyTimeout,
    DatabaseSnapshot,
    MAX_RESPONSE_BYTES,
    SHANGHAI_TIMEZONE,
    _clock_display_date,
    _iso_date,
    _remaining_budget,
)
from harness.gates.consistency.completeness import validate_snapshot_completeness
from harness.gates.consistency.lineage import validate_snapshot_lineage
from harness.gates.consistency.representation import (
    compare_detail,
    compare_summary,
    default_detail,
    default_summary,
    detail_partitions,
)
from harness.gates.consistency.snapshot import _capture_snapshot


def _subresult(
    status: str,
    errors: Sequence[str],
    **details: Any,
) -> dict[str, Any]:
    if status not in {"PASSED", "FAILED", "BLOCKED"}:
        raise ValueError("invalid data consistency subresult status")
    return {"status": status, "errors": list(errors), **details}


_EVIDENCE_LOADERS = (("unversioned_facts", load_unversioned_backtest_facts),)
_LINEAGE_EVIDENCE = ("unversioned_facts",)


def _missing_evidence(
    load_errors: Mapping[str, str], required: Sequence[str]
) -> list[str]:
    return [load_errors[name] for name in required if name in load_errors]


class DataConsistencyGate(Gate):
    """校验产品事实的完整性与血缘，并核对 Dashboard HTTP 响应等于后端在同一数据库上的构建结果。"""

    name = "data-consistency"

    def __init__(
        self,
        *,
        fetcher=fetch_json,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        summary_builder: Callable[[Any], Mapping[str, Any]] = default_summary,
        detail_builder: Callable[..., Mapping[str, Any] | None] = default_detail,
    ) -> None:
        self._summary_builder = summary_builder
        self._detail_builder = detail_builder
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
            root = ctx.project_root
            loaded: dict[str, Any] = {}
            load_errors: dict[str, str] = {}
            for label, loader in _EVIDENCE_LOADERS:
                try:
                    loaded[label] = loader(root)
                except Exception as exc:
                    load_errors[label] = (
                        f"{label} evidence unavailable: {type(exc).__name__}"
                    )
            unversioned_backtest_facts = loaded.get("unversioned_facts")
            lineage_missing = _missing_evidence(
                load_errors, _LINEAGE_EVIDENCE
            )
            try:
                completeness_errors, completeness_details = (
                    validate_snapshot_completeness(
                        snapshot,
                        display_until=display_until,
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
                if lineage_missing:
                    raise DataConsistencyAuthorityUnavailable(
                        "; ".join(lineage_missing)
                    )
                lineage_errors, lineage_details = validate_snapshot_lineage(
                    snapshot,
                    unversioned_backtest_facts=unversioned_backtest_facts,
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
                expected_summary = self._summary_builder(engine)
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
                _remaining_budget(deadline, self._monotonic)
                representation_errors = compare_summary(
                    summary, expected_summary, registry_ids=registry_ids
                )
                if summary_display_until != display_until:
                    representation_errors.append(
                        "Dashboard Summary display_until does not match the "
                        "independent Shanghai business date"
                    )

                for scheme_id, month, source in detail_partitions(
                    expected_summary, registry_ids
                ):
                    _remaining_budget(deadline, self._monotonic)
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
                        compare_detail(
                            detail,
                            self._detail_builder(
                                engine,
                                scheme_id=scheme_id,
                                month=month,
                                source=source,
                            ),
                            scheme_id=scheme_id,
                            month=month,
                            source=source,
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
