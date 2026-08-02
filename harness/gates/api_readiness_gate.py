from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import text

from backend.factor_lab_dashboard_semantics import (
    BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE,
)
from harness.config_loader import load_config_raw
from harness.context import GateContext
from harness.gates.api_gate import _normalize_fetch_response
from harness.gates.base import Gate, guarded_result, utc_now
from harness.probes.api_probe import (
    factor_lab_url,
    fetch_json,
    find_factor_lab_cell,
    metrics_cell_present,
    metrics_url,
)
from harness.result import Evidence, GateResult, GateStatus
from scheduler.repository import registry_scheme_id


class ApiReadinessGate(Gate):
    """激活前 API 就绪 gate：校验 paused registry/backtest 就绪且不会泄漏到 public API。"""

    name = "api-readiness"

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        config = load_config_raw(ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml")
        tenors = [str(item) for item in config.get("tenors", [])]
        horizon = int(config.get("horizon"))
        registry_ids = [registry_scheme_id(ctx.scheme_id, horizon, tenor) for tenor in tenors]

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

        errors = _validate_registry_rows(config, ctx.scheme_id, registry_rows, registry_ids, tenors)
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
        )
        config_status = str(config.get("status") or "")
        if config_status != "active":
            if api_probe["factor_lab_visible"]:
                errors.append(f"paused scheme is visible in /api/backtests/factor-lab: {registry_ids}")
            if api_probe["metrics_visible"]:
                errors.append(f"paused scheme is visible in /api/metrics: {registry_ids[0] if registry_ids else ctx.scheme_id}")
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
                Evidence("config_status", config_status),
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
                Evidence("public_factor_lab_visible", api_probe["factor_lab_visible"]),
                Evidence("public_metrics_visible", api_probe["metrics_visible"]),
                Evidence("probe_warnings", api_probe["probe_warnings"]),
            ],
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
        )


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
) -> list[str]:
    errors: list[str] = []
    expected_common = {
        "base_scheme_id": base_scheme_id,
        "name": str(config.get("name")),
        "horizon": int(config.get("horizon")),
        "task_type": str(config.get("task_type")),
        "frequency": str(config.get("frequency")),
        "status": str(config.get("status")),
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
) -> dict[str, Any]:
    base_url = os.getenv("BOND_FACTOR_LAB_API_BASE_URL", ctx.api_base_url).rstrip("/")
    factor_endpoint = factor_lab_url(base_url, benchmark_id=benchmark_id)
    metrics_endpoint = metrics_url(base_url, registry_ids[0]) if registry_ids else None
    warnings: list[str] = []
    errors: list[str] = []
    factor_lab_visible = False
    metrics_visible = False

    try:
        payload, _status_code = _normalize_fetch_response(fetch_json(factor_endpoint, timeout_sec=min(ctx.timeout_sec, 30)))
        factor_lab_visible = find_factor_lab_cell(payload, registry_ids) is not None
    except Exception as exc:
        errors.append(f"factor-lab readiness probe failed: {exc}")

    if metrics_endpoint:
        try:
            payload, _status_code = _normalize_fetch_response(fetch_json(metrics_endpoint, timeout_sec=min(ctx.timeout_sec, 30)))
            if metrics_cell_present(payload):
                metrics_visible = True
        except Exception as exc:
            if _is_not_found(exc):
                warnings.append(f"metrics probe returned 404 for hidden scheme: {registry_ids[0]}")
            else:
                errors.append(f"metrics readiness probe failed: {exc}")
    else:
        warnings.append("metrics probe skipped: no registry ids")

    return {
        "factor_lab_endpoint": factor_endpoint,
        "metrics_endpoint": metrics_endpoint,
        "factor_lab_visible": factor_lab_visible,
        "metrics_visible": metrics_visible,
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
