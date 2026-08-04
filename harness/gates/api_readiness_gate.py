from __future__ import annotations

import json
import os
from dataclasses import replace
from enum import StrEnum
from typing import Any

from sqlalchemy import text

from backend.factor_lab_dashboard_semantics import (
    BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE,
)
from harness.config_loader import load_config_raw
from harness.context import GateContext
from harness.gates.api_gate import _normalize_fetch_response
from harness.gates.base import Gate, guarded_result, utc_now
from harness.gates.native_maintenance_admission_gate import (
    NativeMaintenanceAdmission,
    verify_native_maintenance_admission,
)
from harness.probes.api_probe import (
    factor_lab_url,
    fetch_json,
    find_factor_lab_cell,
    metrics_cell_present,
    metrics_url,
)
from harness.result import Evidence, GateResult, GateStatus
from scheduler.repository import registry_scheme_id


class ApiReadinessProfile(StrEnum):
    """API readiness 的显式生命周期验证配置。"""

    ORDINARY = "ordinary"
    NATIVE_MAINTENANCE_PRE_ACTIVATION = "native_maintenance_pre_activation"


class ApiReadinessGate(Gate):
    """激活前 API 就绪 gate：校验 paused registry/backtest 就绪且不会泄漏到 public API。"""

    name = "api-readiness"

    def __init__(
        self,
        *,
        profile: ApiReadinessProfile = ApiReadinessProfile.ORDINARY,
    ) -> None:
        if not isinstance(profile, ApiReadinessProfile):
            raise ValueError(f"unsupported api-readiness profile: {profile!r}")
        self.profile = profile

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        config = load_config_raw(ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml")
        tenors = [str(item) for item in config.get("tenors", [])]
        horizon = int(config.get("horizon"))
        registry_ids = [registry_scheme_id(ctx.scheme_id, horizon, tenor) for tenor in tenors]
        expected_registry_status, profile_errors, admission = self._profile_validation(
            ctx,
            config,
        )

        engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
        owns_engine = ctx.engine_factory is None
        try:
            registry_rows = _fetch_registry_rows(engine, registry_ids)
            latest_backtest = _fetch_latest_successful_backtest(
                engine,
                ctx.scheme_id,
                runtime_type=str(config.get("runtime_type") or ""),
            )
        finally:
            if owns_engine and engine is not None and hasattr(engine, "dispose"):
                engine.dispose()

        errors = profile_errors
        errors.extend(
            _validate_registry_rows(
                config,
                ctx.scheme_id,
                registry_rows,
                registry_ids,
                tenors,
                expected_status=expected_registry_status,
            )
        )
        if latest_backtest["run_id"] is None:
            errors.append(f"latest successful backtest run missing for scheme_id={ctx.scheme_id}")
        elif int(latest_backtest["prediction_count"]) <= 0:
            errors.append(
                f"latest successful backtest has no prediction rows: "
                f"run_id={latest_backtest['run_id']} scheme_id={ctx.scheme_id}"
            )

        latest_backtest_benchmark_id = latest_backtest["benchmark_id"]
        api_probe = _probe_public_api(
            ctx,
            registry_ids,
            tenors,
            benchmark_id=(
                str(latest_backtest_benchmark_id)
                if latest_backtest_benchmark_id is not None
                else None
            ),
            probe_all_metrics=(
                self.profile
                == ApiReadinessProfile.NATIVE_MAINTENANCE_PRE_ACTIVATION
            ),
        )
        config_status = str(config.get("status") or "")
        if (
            self.profile == ApiReadinessProfile.NATIVE_MAINTENANCE_PRE_ACTIVATION
            or config_status != "active"
        ):
            if api_probe["factor_lab_visible"]:
                errors.append(f"paused scheme is visible in /api/backtests/factor-lab: {registry_ids}")
            if api_probe["metrics_visible"]:
                metrics_target: object
                if self.profile == ApiReadinessProfile.NATIVE_MAINTENANCE_PRE_ACTIVATION:
                    metrics_target = api_probe["visible_metrics_registry_ids"]
                else:
                    metrics_target = registry_ids[0] if registry_ids else ctx.scheme_id
                errors.append(
                    f"paused scheme is visible in /api/metrics: {metrics_target}"
                )
        else:
            api_probe["probe_warnings"].append("config is already active; run api gate for active visibility acceptance")

        errors.extend(api_probe["probe_errors"])

        finished_at = utc_now()
        status = GateStatus.PASSED if not errors else GateStatus.FAILED
        return GateResult(
            gate_name=self.name,
            status=status,
            passed=status == GateStatus.PASSED,
            evidence=[
                Evidence("scheme_id", ctx.scheme_id),
                Evidence("readiness_profile", self.profile.value),
                Evidence("config_status", config_status),
                Evidence("expected_registry_status", expected_registry_status),
                Evidence(
                    "native_maintenance_admission_verified",
                    admission is not None,
                ),
                Evidence(
                    "native_maintenance_candidate_status",
                    admission.current_candidate_status if admission is not None else None,
                ),
                Evidence(
                    "native_maintenance_registry_lifecycle",
                    admission.registry_lifecycle if admission is not None else None,
                ),
                Evidence("registry_ids", registry_ids),
                Evidence("registry_rows_present", sorted(registry_rows.keys())),
                Evidence("latest_backtest_run_id", latest_backtest["run_id"]),
                Evidence(
                    "latest_backtest_benchmark_id",
                    latest_backtest["benchmark_id"],
                ),
                Evidence("latest_backtest_prediction_count", latest_backtest["prediction_count"]),
                Evidence("factor_lab_endpoint", api_probe["factor_lab_endpoint"]),
                Evidence("metrics_endpoint", api_probe["metrics_endpoint"]),
                Evidence("metrics_endpoints", api_probe["metrics_endpoints"]),
                Evidence("public_factor_lab_visible", api_probe["factor_lab_visible"]),
                Evidence("public_metrics_visible", api_probe["metrics_visible"]),
                Evidence(
                    "public_metrics_visibility",
                    api_probe["metrics_visibility"],
                ),
                Evidence("probe_warnings", api_probe["probe_warnings"]),
            ],
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _profile_validation(
        self,
        ctx: GateContext,
        config: dict[str, Any],
    ) -> tuple[str, list[str], NativeMaintenanceAdmission | None]:
        """返回 profile 对应的 Registry 期望状态及 fail-closed 准入错误。"""
        if self.profile == ApiReadinessProfile.ORDINARY:
            return str(config.get("status") or ""), [], None

        errors: list[str] = []
        if str(config.get("runtime_type") or "") != "native_adapter":
            errors.append(
                "native-maintenance api-readiness requires raw config "
                "runtime_type=native_adapter"
            )
        if str(config.get("status") or "") != "active":
            errors.append(
                "native-maintenance api-readiness requires raw config status=active"
            )
        if errors:
            return "paused", errors, None

        try:
            from scheduler.discovery import load_scheme_config

            current_config = load_scheme_config(
                ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
            )
        except Exception as exc:  # noqa: BLE001 - unreadable canonical config blocks admission.
            return (
                "paused",
                [
                    "native-maintenance api-readiness cannot reload canonical "
                    f"scheme config: {exc}"
                ],
                None,
            )

        errors.extend(_context_identity_errors(ctx, current_config))
        admission_ctx = replace(ctx, config=current_config)
        admission, admission_errors = verify_native_maintenance_admission(admission_ctx)
        if admission is None:
            errors.extend(
                "native-maintenance api-readiness admission failed: " + error
                for error in admission_errors
            )
            if not admission_errors:
                errors.append(
                    "native-maintenance api-readiness admission returned no evidence"
                )
            return "paused", errors, None

        if admission.current_candidate_runtime_type != "native_adapter":
            errors.append(
                "native-maintenance api-readiness requires exact current candidate "
                "runtime_type=native_adapter"
            )
        if admission.current_candidate_status != "draft":
            errors.append(
                "native-maintenance api-readiness requires exact current candidate "
                "status=draft"
            )
        if admission.registry_lifecycle != "paused":
            errors.append(
                "native-maintenance api-readiness requires uniformly paused Registry"
            )
        return "paused", errors, admission


def _context_identity_errors(ctx: GateContext, current_config: Any) -> list[str]:
    """拒绝 caller config 与磁盘 canonical identity 的任何关键漂移。"""
    if ctx.config is None:
        return [
            "native-maintenance api-readiness requires a context config to bind "
            "the canonical candidate identity"
        ]

    errors: list[str] = []
    if ctx.scheme_id != current_config.scheme_id:
        errors.append(
            "native-maintenance api-readiness context scheme_id mismatch: "
            f"context={ctx.scheme_id!r}, canonical={current_config.scheme_id!r}"
        )
    for field in (
        "scheme_id",
        "scheme_version",
        "code_hash",
        "config_hash",
        "manifest_hash",
        "runtime_type",
        "status",
        "version_status",
    ):
        context_value = getattr(ctx.config, field, None)
        canonical_value = getattr(current_config, field, None)
        if context_value != canonical_value:
            errors.append(
                "native-maintenance api-readiness context config identity mismatch: "
                f"{field} context={context_value!r}, canonical={canonical_value!r}"
            )
    return errors


def _fetch_registry_rows(engine, registry_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not registry_ids:
        return {}
    placeholders = ", ".join(f":scheme_id_{index}" for index, _ in enumerate(registry_ids))
    params = {f"scheme_id_{index}": scheme_id for index, scheme_id in enumerate(registry_ids)}
    sql = text(
        f"""
        SELECT scheme_id, base_scheme_id, name, horizon, task_type, tenors, frequency, target_tenor, status
        FROM t_scheme_registry
        WHERE scheme_id IN ({placeholders})
        """
    )
    with engine.begin() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {str(row._mapping["scheme_id"]): dict(row._mapping) for row in rows}


def _fetch_latest_successful_backtest(
    engine,
    scheme_id: str,
    *,
    runtime_type: str,
) -> dict[str, int | str | None]:
    try:
        data_source = BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE[runtime_type]
    except KeyError as exc:
        raise ValueError(
            "api-readiness runtime_type has no default backtest source: "
            f"{runtime_type!r}"
        ) from exc
    sql = text(
        """
        SELECT r.id AS run_id, r.benchmark_id, r.updated_at,
               COUNT(p.id) AS prediction_count
        FROM t_backtest_runs r
        LEFT JOIN t_backtest_predictions p
          ON p.run_id = r.id
        WHERE r.scheme_id = :scheme_id
          AND r.status = 'success'
          AND r.data_source = :data_source
        GROUP BY r.id, r.benchmark_id, r.updated_at
        ORDER BY r.updated_at DESC, r.id DESC
        LIMIT 1
        """
    )
    with engine.begin() as conn:
        row = conn.execute(
            sql,
            {"scheme_id": scheme_id, "data_source": data_source},
        ).one_or_none()
    if row is None:
        return {"run_id": None, "benchmark_id": None, "prediction_count": 0}
    mapping = row._mapping
    benchmark_id = str(mapping["benchmark_id"] or "")
    if not benchmark_id.strip():
        raise ValueError(
            "latest successful backtest benchmark_id must be non-empty: "
            f"run_id={mapping['run_id']} scheme_id={scheme_id}"
        )
    return {
        "run_id": int(mapping["run_id"]) if mapping["run_id"] is not None else None,
        "benchmark_id": benchmark_id,
        "prediction_count": int(mapping["prediction_count"] or 0),
    }


def _validate_registry_rows(
    config: dict[str, Any],
    base_scheme_id: str,
    registry_rows: dict[str, dict[str, Any]],
    registry_ids: list[str],
    tenors: list[str],
    *,
    expected_status: str | None = None,
) -> list[str]:
    errors: list[str] = []
    expected_common = {
        "base_scheme_id": base_scheme_id,
        "name": str(config.get("name")),
        "horizon": int(config.get("horizon")),
        "task_type": str(config.get("task_type")),
        "frequency": str(config.get("frequency")),
        "status": (
            str(config.get("status"))
            if expected_status is None
            else expected_status
        ),
    }
    for registry_id, target_tenor in zip(registry_ids, tenors):
        row = registry_rows.get(registry_id)
        if row is None:
            errors.append(f"registry row missing: {registry_id}")
            continue
        for field, expected in expected_common.items():
            actual = row.get(field)
            if field == "horizon" and actual is not None:
                actual = int(actual)
            else:
                actual = str(actual) if actual is not None else None
            if actual != expected:
                errors.append(f"registry {registry_id} {field} mismatch: expected {expected!r}, got {actual!r}")
        if str(row.get("target_tenor") or "") != target_tenor:
            errors.append(
                f"registry {registry_id} target_tenor mismatch: expected {target_tenor!r}, "
                f"got {row.get('target_tenor')!r}"
            )
        row_tenors = _normalize_tenors(row.get("tenors"))
        if row_tenors and row_tenors != [target_tenor]:
            errors.append(f"registry {registry_id} tenors mismatch: expected {[target_tenor]!r}, got {row_tenors!r}")
    return errors


def _probe_public_api(
    ctx: GateContext,
    registry_ids: list[str],
    tenors: list[str],
    *,
    benchmark_id: str | None = None,
    probe_all_metrics: bool = False,
) -> dict[str, Any]:
    base_url = os.getenv("BOND_FACTOR_LAB_API_BASE_URL", ctx.api_base_url).rstrip("/")
    factor_endpoint = factor_lab_url(base_url, benchmark_id=benchmark_id)
    metrics_registry_ids = registry_ids if probe_all_metrics else registry_ids[:1]
    metrics_endpoints = [
        metrics_url(base_url, registry_id)
        for registry_id in metrics_registry_ids
    ]
    metrics_endpoint = metrics_endpoints[0] if metrics_endpoints else None
    warnings: list[str] = []
    errors: list[str] = []
    factor_lab_visible = False
    metrics_visible = False
    metrics_visibility = {
        registry_id: False for registry_id in metrics_registry_ids
    }

    try:
        payload, _status_code = _normalize_fetch_response(fetch_json(factor_endpoint, timeout_sec=min(ctx.timeout_sec, 30)))
        factor_lab_visible = find_factor_lab_cell(payload, registry_ids) is not None
    except Exception as exc:
        errors.append(f"factor-lab readiness probe failed: {exc}")

    if metrics_endpoints:
        for registry_id, endpoint in zip(metrics_registry_ids, metrics_endpoints):
            try:
                payload, _status_code = _normalize_fetch_response(
                    fetch_json(endpoint, timeout_sec=min(ctx.timeout_sec, 30))
                )
                if metrics_cell_present(payload):
                    metrics_visible = True
                    metrics_visibility[registry_id] = True
            except Exception as exc:
                if _is_not_found(exc):
                    warnings.append(
                        "metrics probe returned 404 for hidden scheme: "
                        f"{registry_id}"
                    )
                else:
                    if probe_all_metrics:
                        errors.append(
                            "metrics readiness probe failed for "
                            f"{registry_id}: {exc}"
                        )
                    else:
                        errors.append(f"metrics readiness probe failed: {exc}")
    else:
        warnings.append("metrics probe skipped: no registry ids")

    return {
        "factor_lab_endpoint": factor_endpoint,
        "metrics_endpoint": metrics_endpoint,
        "metrics_endpoints": metrics_endpoints,
        "factor_lab_visible": factor_lab_visible,
        "metrics_visible": metrics_visible,
        "metrics_visibility": metrics_visibility,
        "visible_metrics_registry_ids": [
            registry_id
            for registry_id, visible in metrics_visibility.items()
            if visible
        ],
        "probe_warnings": warnings,
        "probe_errors": errors,
        "target_tenors": tenors,
    }


def _normalize_tenors(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return [str(parsed)]
    return [str(value)]


def _is_not_found(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    return code == 404 or "404" in str(exc)


def _create_engine():
    from scheduler.repository import create_engine_from_env

    return create_engine_from_env()
