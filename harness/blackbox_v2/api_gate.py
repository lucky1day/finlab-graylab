from __future__ import annotations

from typing import Any

from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.gates.prediction_semantics import LIVE_PHASES
from harness.probes.api_probe import (
    DEFAULT_MAX_RESPONSE_BYTES,
    factor_lab_url,
    fetch_json,
    find_factor_lab_cell,
    health_url,
    metrics_url,
    schemes_url,
)
from harness.result import Evidence, GateResult, GateStatus
from scheduler.repository import registry_scheme_id
from shared.blackbox_v2.contracts import load_metadata
from shared.service_instance import build_service_instance_identity


BLACKBOX_BACKTEST_DATA_SOURCE = "blackbox_v2_current_snapshot_as_of"


class BlackboxApiGate(Gate):
    """验证 active Blackbox 的隔离实例、Registry、live metrics 与回测 API。"""

    name = "api"

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = ctx.config
        if cfg is None or getattr(cfg, "runtime_type", None) != "blackbox_v2":
            raise ValueError("Blackbox API gate requires runtime_type=blackbox_v2")
        if cfg.delivery_metadata is None:
            raise ValueError("Blackbox API gate requires delivery metadata")
        if not str(ctx.api_instance_nonce or "").strip():
            raise ValueError("Blackbox API gate requires explicit api_instance_nonce")
        metadata = load_metadata(cfg.delivery_metadata)
        registry_id = registry_scheme_id(cfg.scheme_id, metadata.horizon, metadata.target_tenor)
        base_url = str(ctx.api_base_url).rstrip("/")
        if not base_url:
            raise ValueError("Blackbox API gate requires non-empty api_base_url")
        errors: list[str] = []

        engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
        try:
            try:
                registry_row = _read_active_registry(engine, registry_id)
            except Exception as exc:  # noqa: BLE001
                registry_row = None
                errors.append(f"active Registry probe failed: {_error_summary(exc)}")
            try:
                expected_instance = build_service_instance_identity(
                    engine,
                    project_root=ctx.project_root,
                    runtime_profile=str(cfg.runtime_profile or ""),
                    instance_nonce=str(ctx.api_instance_nonce),
                )
            except Exception as exc:  # noqa: BLE001
                expected_instance = None
                errors.append(f"expected service instance fingerprint failed: {_error_summary(exc)}")
        finally:
            if hasattr(engine, "dispose"):
                engine.dispose()

        registry_errors = _identity_errors(
            registry_row,
            registry_id=registry_id,
            base_scheme_id=cfg.scheme_id,
            target_tenor=metadata.target_tenor,
            task_type=metadata.task_type,
            horizon=metadata.horizon,
        )
        errors.extend(f"active Registry: {error}" for error in registry_errors)

        health_payload, health_status, health_error = _fetch(
            health_url(base_url), timeout_sec=min(ctx.timeout_sec, 30)
        )
        actual_instance = (
            health_payload.get("service_instance")
            if isinstance(health_payload, dict)
            else None
        )
        expected_fingerprint = (
            str(expected_instance.get("fingerprint") or "")
            if isinstance(expected_instance, dict)
            else ""
        )
        actual_fingerprint = (
            str(actual_instance.get("fingerprint") or "")
            if isinstance(actual_instance, dict)
            else ""
        )
        if health_error:
            errors.append(f"/api/health probe failed: {health_error}")
        if health_status != 200:
            errors.append(f"/api/health returned HTTP {health_status}")
        if not isinstance(health_payload, dict) or health_payload.get("status") != "ok":
            errors.append("/api/health status must be ok")
        instance_identity_errors = _service_instance_identity_errors(
            expected_instance,
            actual_instance,
        )
        errors.extend(instance_identity_errors)
        if not expected_fingerprint or actual_fingerprint != expected_fingerprint:
            errors.append(
                "service instance fingerprint mismatch: "
                f"expected={expected_fingerprint or None}, actual={actual_fingerprint or None}"
            )

        schemes_payload, schemes_status, schemes_error = _fetch(
            schemes_url(base_url), timeout_sec=min(ctx.timeout_sec, 30)
        )
        schemes_row = _find_scheme(schemes_payload, registry_id)
        if schemes_error:
            errors.append(f"/api/schemes probe failed: {schemes_error}")
        if schemes_status != 200:
            errors.append(f"/api/schemes returned HTTP {schemes_status}")
        schemes_identity_errors = _identity_errors(
            schemes_row,
            registry_id=registry_id,
            base_scheme_id=cfg.scheme_id,
            target_tenor=metadata.target_tenor,
            task_type=metadata.task_type,
            horizon=metadata.horizon,
            require_runtime_type=False,
        )
        errors.extend(f"/api/schemes: {error}" for error in schemes_identity_errors)

        metrics_payload, metrics_status, metrics_error = _fetch(
            metrics_url(base_url, registry_id), timeout_sec=min(ctx.timeout_sec, 30)
        )
        if metrics_error:
            errors.append(f"metrics probe failed: {metrics_error}")
        if metrics_status != 200:
            errors.append(f"metrics returned HTTP {metrics_status}")
        metrics_errors, metrics_counts = _metrics_contract_errors(
            metrics_payload,
            registry_id=registry_id,
            base_scheme_id=cfg.scheme_id,
            target_tenor=metadata.target_tenor,
            task_type=metadata.task_type,
            horizon=metadata.horizon,
            scheme_version=cfg.scheme_version,
            expected_phase=ctx.prediction_phase,
        )
        errors.extend(f"metrics: {error}" for error in metrics_errors)

        backtest_payload, backtest_status, backtest_error = _fetch(
            factor_lab_url(base_url, data_source=BLACKBOX_BACKTEST_DATA_SOURCE),
            timeout_sec=min(ctx.timeout_sec, 30),
        )
        if backtest_error:
            errors.append(f"backtest probe failed: {backtest_error}")
        if backtest_status != 200:
            errors.append(f"backtest returned HTTP {backtest_status}")
        backtest_row = find_factor_lab_cell(
            backtest_payload if isinstance(backtest_payload, dict) else {},
            [registry_id],
        )
        backtest_errors = _backtest_contract_errors(
            backtest_row,
            base_scheme_id=cfg.scheme_id,
            target_tenor=metadata.target_tenor,
            task_type=metadata.task_type,
            horizon=metadata.horizon,
        )
        errors.extend(f"backtest: {error}" for error in backtest_errors)

        status = GateStatus.PASSED if not errors else GateStatus.FAILED
        return GateResult(
            gate_name=self.name,
            status=status,
            passed=status == GateStatus.PASSED,
            evidence=[
                Evidence("registry_id", registry_id),
                Evidence("base_scheme_id", cfg.scheme_id),
                Evidence("target_tenor", metadata.target_tenor),
                Evidence("task_type", metadata.task_type),
                Evidence("horizon", metadata.horizon),
                Evidence("data_source", BLACKBOX_BACKTEST_DATA_SOURCE),
                Evidence("effective_api_base_url", base_url),
                Evidence("expected_service_fingerprint", expected_fingerprint or None),
                Evidence("actual_service_fingerprint", actual_fingerprint or None),
                Evidence("registry_active", registry_row is not None and not registry_errors),
                Evidence("schemes_visible", schemes_row is not None and not schemes_identity_errors),
                Evidence("metrics_visible", not metrics_errors),
                Evidence("backtest_visible", backtest_row is not None and not backtest_errors),
                Evidence("live_rows", metrics_counts["live_rows"]),
                Evidence("matched_actual_rows", metrics_counts["matched_actual_rows"]),
                Evidence("pending_actual_rows", metrics_counts["pending_actual_rows"]),
                Evidence("monthly_metric_rows", metrics_counts["monthly_metric_rows"]),
                Evidence("health_http_status", health_status),
                Evidence("schemes_http_status", schemes_status),
                Evidence("metrics_http_status", metrics_status),
                Evidence("backtest_http_status", backtest_status),
                Evidence("health_error_summary", health_error),
                Evidence("schemes_error_summary", schemes_error),
                Evidence("metrics_error_summary", metrics_error),
                Evidence("backtest_error_summary", backtest_error),
                Evidence("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES),
            ],
            errors=errors,
            started_at=started_at,
            finished_at=utc_now(),
        )


def _read_active_registry(engine, registry_id: str) -> dict[str, Any] | None:
    from sqlalchemy import text

    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT scheme_id, base_scheme_id, runtime_type, status, "
                "target_tenor, task_type, horizon FROM t_scheme_registry "
                "WHERE scheme_id = :scheme_id AND status = 'active' LIMIT 1"
            ),
            {"scheme_id": registry_id},
        ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _identity_errors(
    row: dict[str, Any] | None,
    *,
    registry_id: str,
    base_scheme_id: str,
    target_tenor: str,
    task_type: str,
    horizon: int,
    require_runtime_type: bool = True,
) -> list[str]:
    if row is None:
        return [f"row missing for {registry_id}"]
    expected = {
        "scheme_id": registry_id,
        "base_scheme_id": base_scheme_id,
        "target_tenor": target_tenor,
        "task_type": task_type,
        "horizon": int(horizon),
        "status": "active",
    }
    if require_runtime_type:
        expected["runtime_type"] = "blackbox_v2"
    errors: list[str] = []
    for key, expected_value in expected.items():
        actual = row.get(key)
        if key == "horizon":
            try:
                actual = int(actual)
            except (TypeError, ValueError):
                pass
        if actual != expected_value:
            errors.append(f"{key}: expected={expected_value!r}, got={actual!r}")
    return errors


def _metrics_contract_errors(
    payload: Any,
    *,
    registry_id: str,
    base_scheme_id: str,
    target_tenor: str,
    task_type: str,
    horizon: int,
    scheme_version: str,
    expected_phase: str | None,
) -> tuple[list[str], dict[str, int]]:
    errors: list[str] = []
    counts = {
        "live_rows": 0,
        "matched_actual_rows": 0,
        "pending_actual_rows": 0,
        "monthly_metric_rows": 0,
    }
    if not isinstance(payload, dict):
        return ["payload must be an object"], counts
    for key, value in {
        "scheme_id": registry_id,
        "base_scheme_id": base_scheme_id,
        "target_tenor": target_tenor,
        "task_type": task_type,
    }.items():
        if str(payload.get(key) or "") != value:
            errors.append(f"{key}: expected={value!r}, got={payload.get(key)!r}")
    daily_rows = payload.get("daily_rows")
    monthly_metrics = payload.get("monthly_metrics")
    if not isinstance(daily_rows, list) or not daily_rows:
        errors.append("daily_rows must be non-empty")
        daily_rows = []
    if not isinstance(monthly_metrics, list) or not monthly_metrics:
        errors.append("monthly_metrics must be non-empty")
        monthly_metrics = []
    counts["live_rows"] = len(daily_rows)
    counts["monthly_metric_rows"] = len(monthly_metrics)
    required_phase = expected_phase if expected_phase in LIVE_PHASES else None
    for index, row in enumerate(daily_rows):
        if not isinstance(row, dict):
            errors.append(f"daily_rows[{index}] must be an object")
            continue
        expected_fields = {
            "scheme_id": registry_id,
            "base_scheme_id": base_scheme_id,
            "target_tenor": target_tenor,
            "horizon": int(horizon),
            "scheme_version": scheme_version,
        }
        for key, expected_value in expected_fields.items():
            actual = row.get(key)
            if key == "horizon":
                try:
                    actual = int(actual)
                except (TypeError, ValueError):
                    pass
            if actual != expected_value:
                errors.append(
                    f"daily_rows[{index}].{key}: expected={expected_value!r}, got={actual!r}"
                )
        phase = str(row.get("prediction_phase") or "")
        if phase not in LIVE_PHASES or (required_phase and phase != required_phase):
            errors.append(f"daily_rows[{index}].prediction_phase is not the expected live phase")
        for key in ("request_id", "data_snapshot_id"):
            if not str(row.get(key) or "").strip():
                errors.append(f"daily_rows[{index}].{key} must be non-empty")
        if type(row.get("predicted_direction")) is not int or row.get("predicted_direction") not in {-1, 0, 1}:
            errors.append(f"daily_rows[{index}].predicted_direction is invalid")
        actual = row.get("actual_direction")
        if actual is None:
            counts["pending_actual_rows"] += 1
        elif type(actual) is int and actual in {-1, 0, 1}:
            counts["matched_actual_rows"] += 1
            if type(row.get("is_correct")) is not bool:
                errors.append(f"daily_rows[{index}].is_correct must be boolean when actual exists")
        else:
            errors.append(f"daily_rows[{index}].actual_direction is invalid")
    if counts["matched_actual_rows"] == 0:
        errors.append("actual contract requires at least one matched actual row")
    summary = payload.get("summary")
    if not isinstance(summary, dict) or int(summary.get("metric_samples") or 0) <= 0:
        errors.append("summary.metric_samples must be positive")
    for index, metric in enumerate(monthly_metrics):
        if not isinstance(metric, dict) or int(metric.get("metric_samples") or 0) <= 0:
            errors.append(f"monthly_metrics[{index}].metric_samples must be positive")
    return errors, counts


def _service_instance_identity_errors(expected: Any, actual: Any) -> list[str]:
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return ["service instance identity must be present in expected and actual probes"]
    fields = (
        "fingerprint_version",
        "fingerprint",
        "database_identity_sha256",
        "code_commit",
        "runtime_profile",
        "instance_nonce_sha256",
    )
    return [
        "service instance identity mismatch: "
        f"{field} expected={expected.get(field)!r}, actual={actual.get(field)!r}"
        for field in fields
        if actual.get(field) != expected.get(field)
    ]


def _backtest_contract_errors(
    row: dict[str, Any] | None,
    *,
    base_scheme_id: str,
    target_tenor: str,
    task_type: str,
    horizon: int,
) -> list[str]:
    if row is None:
        return ["payload does not contain the exact active Blackbox result"]
    errors: list[str] = []
    expected = {
        "base_scheme_id": base_scheme_id,
        "target_tenor": target_tenor,
        "task_type": task_type,
        "horizon": int(horizon),
        "data_source": BLACKBOX_BACKTEST_DATA_SOURCE,
    }
    for key, value in expected.items():
        actual = row.get(key)
        if key == "horizon":
            try:
                actual = int(actual)
            except (TypeError, ValueError):
                pass
        if actual != value:
            errors.append(f"{key}: expected={value!r}, got={actual!r}")
    for key in ("daily_rows", "monthly_metrics"):
        if not isinstance(row.get(key), list) or not row[key]:
            errors.append(f"{key} must be non-empty")
    return errors


def _find_scheme(payload: Any, registry_id: str) -> dict[str, Any] | None:
    rows = payload.get("schemes") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return None
    return next(
        (
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("scheme_id") or "") == registry_id
        ),
        None,
    )


def _fetch(url: str, *, timeout_sec: int) -> tuple[Any | None, int | None, str | None]:
    try:
        payload, status = fetch_json(
            url,
            timeout_sec=timeout_sec,
            max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
        )
        return payload, int(status), None
    except Exception as exc:  # noqa: BLE001
        return (
            None,
            getattr(exc, "status_code", getattr(exc, "code", None)),
            _error_summary(exc),
        )


def _error_summary(exc: BaseException) -> str:
    value = str(getattr(exc, "error_summary", None) or exc).replace("\n", " ")
    return value[:512]


def _create_engine():
    from scheduler.repository import create_engine_from_env

    return create_engine_from_env()
