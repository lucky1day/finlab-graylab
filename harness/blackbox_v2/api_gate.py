from __future__ import annotations

import os
from typing import Any

from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.probes.api_probe import (
    factor_lab_url,
    fetch_json,
    find_factor_lab_cell,
    metrics_cell_present,
    metrics_url,
    schemes_url,
)
from harness.result import Evidence, GateResult, GateStatus
from scheduler.repository import registry_scheme_id
from shared.blackbox_v2.contracts import load_metadata


BLACKBOX_BACKTEST_DATA_SOURCE = "blackbox_v2_current_snapshot_as_of"


class BlackboxApiGate(Gate):
    """验证 active Blackbox 在 Registry、API 与回测读取链路中的可见性。"""

    name = "api"

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = ctx.config
        if cfg is None or getattr(cfg, "runtime_type", None) != "blackbox_v2":
            raise ValueError("Blackbox API gate requires runtime_type=blackbox_v2")
        if cfg.delivery_metadata is None:
            raise ValueError("Blackbox API gate requires delivery metadata")
        metadata = load_metadata(cfg.delivery_metadata)
        registry_id = registry_scheme_id(cfg.scheme_id, metadata.horizon, metadata.target_tenor)
        base_url = os.getenv("BOND_FACTOR_LAB_API_BASE_URL", ctx.api_base_url).rstrip("/")
        errors: list[str] = []

        engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
        try:
            registry_row = _read_active_registry(engine, registry_id)
        except Exception as exc:  # noqa: BLE001
            registry_row = None
            errors.append(f"active Registry probe failed: {exc}")
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

        schemes_payload, schemes_status, schemes_error = _fetch(
            schemes_url(base_url),
            timeout_sec=min(ctx.timeout_sec, 30),
        )
        schemes_row = _find_scheme(schemes_payload, registry_id)
        if schemes_error:
            errors.append(f"/api/schemes probe failed: {schemes_error}")
        if schemes_status != 200:
            errors.append(f"/api/schemes returned HTTP {schemes_status}")
        errors.extend(
            f"/api/schemes: {error}"
            for error in _identity_errors(
                schemes_row,
                registry_id=registry_id,
                base_scheme_id=cfg.scheme_id,
                target_tenor=metadata.target_tenor,
                task_type=metadata.task_type,
                horizon=metadata.horizon,
                require_runtime_type=False,
            )
        )

        metrics_payload, metrics_status, metrics_error = _fetch(
            metrics_url(base_url, registry_id),
            timeout_sec=min(ctx.timeout_sec, 30),
        )
        if metrics_error:
            errors.append(f"metrics probe failed: {metrics_error}")
        if metrics_status != 200:
            errors.append(f"metrics returned HTTP {metrics_status}")
        metrics_visible = bool(
            metrics_payload
            and str(metrics_payload.get("scheme_id") or "") == registry_id
            and str(metrics_payload.get("base_scheme_id") or "") == cfg.scheme_id
            and str(metrics_payload.get("target_tenor") or "") == metadata.target_tenor
            and metrics_cell_present(metrics_payload)
        )
        if not metrics_visible:
            errors.append("metrics payload does not contain the exact active Blackbox identity")

        backtest_payload, backtest_status, backtest_error = _fetch(
            factor_lab_url(base_url, data_source=BLACKBOX_BACKTEST_DATA_SOURCE),
            timeout_sec=min(ctx.timeout_sec, 30),
        )
        if backtest_error:
            errors.append(f"backtest probe failed: {backtest_error}")
        if backtest_status != 200:
            errors.append(f"backtest returned HTTP {backtest_status}")
        backtest_row = find_factor_lab_cell(backtest_payload or {}, [registry_id])
        backtest_visible = bool(
            backtest_row
            and str(backtest_row.get("base_scheme_id") or "") == cfg.scheme_id
            and str(backtest_row.get("target_tenor") or "") == metadata.target_tenor
            and str(backtest_row.get("task_type") or "") == metadata.task_type
            and int(backtest_row.get("horizon", -1)) == metadata.horizon
            and str(backtest_row.get("data_source") or "") == BLACKBOX_BACKTEST_DATA_SOURCE
        )
        if not backtest_visible:
            errors.append("backtest payload does not contain the exact active Blackbox result")

        registry_active = not registry_errors and registry_row is not None
        schemes_visible = schemes_row is not None and not _identity_errors(
            schemes_row,
            registry_id=registry_id,
            base_scheme_id=cfg.scheme_id,
            target_tenor=metadata.target_tenor,
            task_type=metadata.task_type,
            horizon=metadata.horizon,
            require_runtime_type=False,
        )
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
                Evidence("registry_active", registry_active),
                Evidence("schemes_visible", schemes_visible),
                Evidence("metrics_visible", metrics_visible),
                Evidence("backtest_visible", backtest_visible),
                Evidence("schemes_http_status", schemes_status),
                Evidence("metrics_http_status", metrics_status),
                Evidence("backtest_http_status", backtest_status),
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
                """
                SELECT scheme_id, base_scheme_id, runtime_type, status,
                       target_tenor, task_type, horizon
                FROM t_scheme_registry
                WHERE scheme_id = :scheme_id AND status = 'active'
                LIMIT 1
                """
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


def _find_scheme(payload: dict[str, Any] | None, registry_id: str) -> dict[str, Any] | None:
    if not payload or not isinstance(payload.get("schemes"), list):
        return None
    return next(
        (
            row
            for row in payload["schemes"]
            if isinstance(row, dict) and str(row.get("scheme_id") or "") == registry_id
        ),
        None,
    )


def _fetch(url: str, *, timeout_sec: int) -> tuple[dict[str, Any] | None, int | None, str | None]:
    try:
        value = fetch_json(url, timeout_sec=timeout_sec)
        if isinstance(value, tuple) and len(value) == 2:
            payload, status = value
        else:
            payload, status = value, 200
        if not isinstance(payload, dict):
            raise ValueError("API payload must be an object")
        return payload, int(status), None
    except Exception as exc:  # noqa: BLE001
        return None, None, str(exc)


def _create_engine():
    from scheduler.repository import create_engine_from_env

    return create_engine_from_env()
