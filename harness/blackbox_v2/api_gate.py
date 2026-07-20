from __future__ import annotations

import json
from typing import Any

from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.gates.prediction_semantics import LIVE_PHASES
from harness.probes.api_probe import (
    DEFAULT_MAX_RESPONSE_BYTES,
    factor_lab_url,
    fetch_json,
    health_url,
    metrics_url,
    schemes_url,
)
from harness.result import Evidence, GateResult, GateStatus
from scheduler.repository import registry_scheme_id
from shared.blackbox_v2.contracts import load_metadata
from shared.service_instance import (
    build_service_instance_identity,
    service_fingerprint_secret,
)


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
        if ctx.prediction_phase not in LIVE_PHASES:
            errors.append("Blackbox API gate requires explicit gray_live or scheduled_live phase")
        fingerprint_secret = service_fingerprint_secret()
        if fingerprint_secret is None:
            errors.append(
                "formal Blackbox API gate requires HARNESS_AUTH_SECRET or "
                "BOND_FACTOR_LAB_SERVICE_FINGERPRINT_SECRET"
            )

        engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
        try:
            try:
                registry_row = _read_active_registry(engine, registry_id)
            except Exception as exc:  # noqa: BLE001
                registry_row = None
                errors.append(f"active Registry probe failed: {_error_summary(exc)}")
            try:
                expected_backtest = _read_expected_backtest_evidence(engine, cfg)
            except Exception as exc:  # noqa: BLE001
                expected_backtest = None
                errors.append(f"persisted backtest evidence probe failed: {_error_summary(exc)}")
            try:
                expected_live = _read_expected_live_evidence(
                    engine,
                    cfg,
                    target_tenor=metadata.target_tenor,
                    horizon=metadata.horizon,
                    prediction_phase=ctx.prediction_phase,
                )
            except Exception as exc:  # noqa: BLE001
                expected_live = None
                errors.append(f"live prediction evidence probe failed: {_error_summary(exc)}")
            try:
                if fingerprint_secret is None:
                    raise ValueError("service fingerprint authentication secret is unavailable")
                expected_instance = build_service_instance_identity(
                    engine,
                    project_root=ctx.project_root,
                    runtime_profile=str(cfg.runtime_profile or ""),
                    instance_nonce=str(ctx.api_instance_nonce),
                    fingerprint_secret=fingerprint_secret,
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
            expected_live=expected_live,
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
        backtest_row, backtest_card_errors = _find_unique_factor_lab_cell(
            backtest_payload if isinstance(backtest_payload, dict) else {},
            registry_id,
        )
        errors.extend(f"backtest: {error}" for error in backtest_card_errors)
        backtest_errors = _backtest_contract_errors(
            backtest_row,
            base_scheme_id=cfg.scheme_id,
            target_tenor=metadata.target_tenor,
            task_type=metadata.task_type,
            horizon=metadata.horizon,
            expected_run_id=(expected_backtest or {}).get("run_id"),
            expected_benchmark_id=(expected_backtest or {}).get("benchmark_id"),
            expected_scheme_version=cfg.scheme_version,
            expected_snapshot_id=(expected_backtest or {}).get("data_snapshot_id"),
            expected_harness_run_id=(expected_backtest or {}).get("harness_run_id"),
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
                Evidence("exact_current_live_rows", metrics_counts["exact_current_live_rows"]),
                Evidence("monthly_metric_rows", metrics_counts["monthly_metric_rows"]),
                Evidence("expected_backtest_run_id", (expected_backtest or {}).get("run_id")),
                Evidence("expected_benchmark_id", (expected_backtest or {}).get("benchmark_id")),
                Evidence("expected_harness_run_id", (expected_backtest or {}).get("harness_run_id")),
                Evidence("expected_data_snapshot_id", (expected_backtest or {}).get("data_snapshot_id")),
                Evidence("expected_live_run_id", (expected_live or {}).get("run_id")),
                Evidence("expected_live_request_id", (expected_live or {}).get("request_id")),
                Evidence("expected_live_snapshot_id", (expected_live or {}).get("data_snapshot_id")),
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


def _read_expected_backtest_evidence(engine, cfg) -> dict[str, Any]:
    """读取本次 formal 认证唯一允许的 persisted backtest 身份。"""
    from sqlalchemy import text

    with engine.begin() as connection:
        harness_row = connection.execute(
            text(
                """
                SELECT harness_run_id
                FROM t_harness_runs
                WHERE scheme_id = :scheme_id
                  AND scheme_version = :scheme_version
                  AND stage = 'all'
                  AND status = 'passed'
                ORDER BY finished_at DESC, harness_run_id DESC
                LIMIT 1
                """
            ),
            {"scheme_id": cfg.scheme_id, "scheme_version": cfg.scheme_version},
        ).mappings().one_or_none()
        if harness_row is None:
            raise ValueError("latest passed all-stage harness run is missing")
        harness_run_id = str(harness_row["harness_run_id"])
        benchmark_id = f"bbv2-{cfg.scheme_id}-{harness_run_id}"
        rows = connection.execute(
            text(
                """
                SELECT id, benchmark_id, summary
                FROM t_backtest_runs
                WHERE benchmark_id = :benchmark_id
                  AND scheme_id = :scheme_id
                  AND data_source = :data_source
                  AND status = 'success'
                ORDER BY updated_at DESC, id DESC
                LIMIT 2
                """
            ),
            {
                "benchmark_id": benchmark_id,
                "scheme_id": cfg.scheme_id,
                "data_source": BLACKBOX_BACKTEST_DATA_SOURCE,
            },
        ).mappings().all()
    if len(rows) != 1:
        raise ValueError(
            "exact persisted backtest run must exist exactly once: "
            f"benchmark_id={benchmark_id}, count={len(rows)}"
        )
    row = rows[0]
    summary = _json_object(row["summary"])
    expected_summary = {
        "scheme_version": cfg.scheme_version,
        "harness_run_id": harness_run_id,
    }
    for key, expected in expected_summary.items():
        if str(summary.get(key) or "") != str(expected):
            raise ValueError(
                f"persisted backtest {key} mismatch: "
                f"expected={expected!r}, got={summary.get(key)!r}"
            )
    snapshot_id = str(summary.get("data_snapshot_id") or "").strip()
    if not snapshot_id:
        raise ValueError("persisted backtest data_snapshot_id provenance is missing")
    return {
        "run_id": int(row["id"]),
        "benchmark_id": benchmark_id,
        "scheme_version": cfg.scheme_version,
        "harness_run_id": harness_run_id,
        "data_snapshot_id": snapshot_id,
    }


def _read_expected_live_evidence(
    engine,
    cfg,
    *,
    target_tenor: str,
    horizon: int,
    prediction_phase: str | None,
) -> dict[str, Any]:
    """读取当前成功 live run 的精确 Request 与 snapshot provenance。"""
    from sqlalchemy import text

    if prediction_phase not in LIVE_PHASES:
        raise ValueError("live evidence requires explicit prediction_phase")
    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT r.run_id, r.data_snapshot_id AS run_data_snapshot_id,
                       p.predict_date, p.feature_date, p.target_date, p.extra
                FROM t_scheme_predictions p
                INNER JOIN t_scheme_runs r ON r.run_id = p.run_id
                WHERE p.scheme_id = :scheme_id
                  AND p.target_tenor = :target_tenor
                  AND p.horizon = :horizon
                  AND p.scheme_version = :scheme_version
                  AND p.prediction_phase = :prediction_phase
                  AND r.scheme_id = :scheme_id
                  AND r.scheme_version = :scheme_version
                  AND r.runtime_type = 'blackbox_v2'
                  AND r.prediction_phase = :prediction_phase
                  AND r.status = 'success'
                ORDER BY r.finished_at DESC, r.run_id DESC, p.id DESC
                LIMIT 1
                """
            ),
            {
                "scheme_id": cfg.scheme_id,
                "target_tenor": target_tenor,
                "horizon": int(horizon),
                "scheme_version": cfg.scheme_version,
                "prediction_phase": prediction_phase,
            },
        ).mappings().one_or_none()
    if row is None:
        raise ValueError("exact successful live prediction is missing")
    extra = _json_object(row["extra"])
    run_snapshot_id = str(row["run_data_snapshot_id"] or "").strip()
    prediction_snapshot_id = str(extra.get("data_snapshot_id") or "").strip()
    if not run_snapshot_id or prediction_snapshot_id != run_snapshot_id:
        raise ValueError(
            "successful live run/prediction data_snapshot_id provenance mismatch: "
            f"run={run_snapshot_id or None}, prediction={prediction_snapshot_id or None}"
        )
    evidence = {
        "run_id": int(row["run_id"]),
        "predict_date": str(row["predict_date"]),
        "feature_date": str(row["feature_date"]),
        "target_date": str(row["target_date"]),
        "request_id": str(extra.get("request_id") or "").strip(),
        "data_snapshot_id": prediction_snapshot_id,
    }
    canonical_request_id = (
        f"{cfg.scheme_id}:{evidence['predict_date']}:{evidence['feature_date']}:"
        f"{evidence['target_date']}"
    )
    if evidence["request_id"] != canonical_request_id:
        raise ValueError("successful live prediction request_id provenance is not canonical")
    if not evidence["data_snapshot_id"]:
        raise ValueError("successful live prediction data_snapshot_id provenance is missing")
    return evidence


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("persisted backtest summary must be a JSON object")


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
    expected_live: dict[str, Any] | None,
) -> tuple[list[str], dict[str, int]]:
    errors: list[str] = []
    counts = {
        "live_rows": 0,
        "matched_actual_rows": 0,
        "pending_actual_rows": 0,
        "exact_current_live_rows": 0,
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
    if required_phase is None:
        errors.append("expected live phase must be explicit")
    if not isinstance(expected_live, dict):
        errors.append("expected live Request/snapshot provenance must be present")
        expected_live = {}
    for index, row in enumerate(daily_rows):
        if not isinstance(row, dict):
            errors.append(f"daily_rows[{index}] must be an object")
            continue
        expected_fields = {
            "scheme_id": registry_id,
            "base_scheme_id": base_scheme_id,
            "target_tenor": target_tenor,
            "horizon": int(horizon),
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
        if phase not in LIVE_PHASES:
            errors.append(f"daily_rows[{index}].prediction_phase is not a live phase")
        if not str(row.get("scheme_version") or "").strip():
            errors.append(f"daily_rows[{index}].scheme_version must be non-empty")
        for key in ("predict_date", "feature_date", "target_date"):
            if not str(row.get(key) or "").strip():
                errors.append(f"daily_rows[{index}].{key} must be non-empty")
        for key in ("request_id", "data_snapshot_id"):
            if not str(row.get(key) or "").strip():
                errors.append(f"daily_rows[{index}].{key} must be non-empty")
        canonical_request_id = (
            f"{base_scheme_id}:{row.get('predict_date')}:{row.get('feature_date')}:"
            f"{row.get('target_date')}"
        )
        if str(row.get("request_id") or "") != canonical_request_id:
            errors.append(f"daily_rows[{index}].request_id is not canonical")
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
        if (
            row.get("scheme_version") == scheme_version
            and required_phase is not None
            and phase == required_phase
            and all(
                str(row.get(key) or "") == str(expected_live.get(key) or "")
                for key in (
                    "run_id",
                    "predict_date",
                    "feature_date",
                    "target_date",
                    "request_id",
                    "data_snapshot_id",
                )
            )
            and str(row.get("request_id") or "") == canonical_request_id
        ):
            counts["exact_current_live_rows"] += 1
    if counts["exact_current_live_rows"] == 0:
        errors.append(
            "daily_rows requires at least one exact current live row matching canonical "
            "scheme_version, prediction_phase, target, Request and snapshot provenance"
        )
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
    fields = ("fingerprint_version", "fingerprint")
    errors = [
        "service instance identity mismatch: "
        f"{field} expected={expected.get(field)!r}, actual={actual.get(field)!r}"
        for field in fields
        if actual.get(field) != expected.get(field)
    ]
    allowed = set(fields)
    if set(actual) != allowed:
        errors.append(
            "service instance health payload must expose only fingerprint_version and fingerprint"
        )
    return errors


def _backtest_contract_errors(
    row: dict[str, Any] | None,
    *,
    base_scheme_id: str,
    target_tenor: str,
    task_type: str,
    horizon: int,
    expected_run_id: int | None,
    expected_benchmark_id: str | None,
    expected_scheme_version: str,
    expected_snapshot_id: str | None,
    expected_harness_run_id: str | None,
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
        "runtime_type": "blackbox_v2",
        "run_id": expected_run_id,
        "benchmark_id": expected_benchmark_id,
        "scheme_version": expected_scheme_version,
        "data_snapshot_id": expected_snapshot_id,
        "harness_run_id": expected_harness_run_id,
    }
    for key, value in expected.items():
        actual = row.get(key)
        if key in {"horizon", "run_id"}:
            try:
                actual = int(actual)
                value = int(value) if value is not None else None
            except (TypeError, ValueError):
                pass
        if actual != value:
            errors.append(f"{key}: expected={value!r}, got={actual!r}")
    for key in ("daily_rows", "monthly_metrics"):
        if not isinstance(row.get(key), list) or not row[key]:
            errors.append(f"{key} must be non-empty")
    return errors


def _find_unique_factor_lab_cell(
    payload: dict[str, Any],
    registry_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    rows = payload.get("schemes")
    if not isinstance(rows, list):
        return None, ["factor-lab schemes must be a list"]
    matches = [
        row
        for row in rows
        if isinstance(row, dict) and str(row.get("scheme_id") or "") == registry_id
    ]
    if len(matches) != 1:
        return None, [
            "factor-lab must expose exactly one latest successful card for "
            f"{registry_id}: got {len(matches)}"
        ]
    return matches[0], []


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
