from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from backend.factor_lab_dashboard import MAX_RAW_JSON_BYTES
from backend.factor_lab_dashboard_semantics import validate_dashboard_payload
from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.probes.api_probe import fetch_json
from harness.result import Evidence, GateResult, GateStatus
from scheduler.discovery import SchemeConfig, load_scheme_config
from scheduler.repository import registry_scheme_id


DashboardFetcher = Callable[..., tuple[Any, int]]


class DashboardGate(Gate):
    """校验 active 方案在唯一 Dashboard 公开读模型中的完整分区。"""

    name = "dashboard"

    def __init__(self, *, fetcher: DashboardFetcher = fetch_json) -> None:
        self._fetcher = fetcher

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(
            self.name,
            lambda started_at: self._run(ctx, started_at),
        )

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        config = load_scheme_config(
            ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
        )
        if config.status != "active" or config.version_status != "active":
            raise ValueError(
                "dashboard gate requires canonical config active+active: "
                f"status={config.status!r}, "
                f"version_status={config.version_status!r}"
            )

        base_url = str(ctx.api_base_url).strip().rstrip("/")
        if not base_url:
            raise ValueError("dashboard gate requires non-empty api_base_url")
        endpoint = f"{base_url}/api/factor-lab/dashboard"
        payload, http_status = _fetch_response(
            # 响应大小预算以生产端 MAX_RAW_JSON_BYTES 为唯一权威：探针默认
            # 1MiB 小于生产端 1.5MB，不显式传预算时 1MiB..1.5MB 之间的合法
            # Dashboard 会被 Gate 误报为超限（issue #40）。
            self._fetcher(
                endpoint,
                timeout_sec=min(ctx.timeout_sec, 30),
                max_response_bytes=MAX_RAW_JSON_BYTES,
            )
        )
        errors: list[str] = []
        if http_status != 200:
            errors.append(f"dashboard probe returned HTTP {http_status}, expected 200")
        if not isinstance(payload, Mapping):
            errors.append("dashboard probe did not return a JSON object")
        elif http_status == 200:
            try:
                validate_dashboard_payload(payload)
            except Exception as exc:
                errors.append(f"dashboard payload validation failed: {exc}")

        expected_registry_ids = [
            registry_scheme_id(config.scheme_id, config.horizon, tenor)
            for tenor in config.tenors
        ]
        matched_rows: list[Mapping[str, Any]] = []
        if errors:
            return _result(
                started_at,
                endpoint=endpoint,
                http_status=http_status,
                config=config,
                payload=payload if isinstance(payload, Mapping) else None,
                expected_registry_ids=expected_registry_ids,
                matched_rows=matched_rows,
                errors=errors,
            )

        schemes = payload["schemes"]
        for registry_id, target_tenor in zip(
            expected_registry_ids,
            config.tenors,
            strict=True,
        ):
            matches = [row for row in schemes if row["scheme_id"] == registry_id]
            if len(matches) != 1:
                errors.append(
                    "dashboard composite scheme must appear exactly once: "
                    f"scheme_id={registry_id} count={len(matches)}"
                )
                continue
            row = matches[0]
            matched_rows.append(row)
            expected_fields = {
                "base_scheme_id": config.scheme_id,
                "horizon": config.horizon,
                "task_type": config.task_type,
                "frequency": config.frequency,
                "target_tenor": target_tenor,
                "status": "active",
            }
            for field, expected in expected_fields.items():
                if row[field] != expected:
                    errors.append(
                        f"dashboard {registry_id} {field} mismatch: "
                        f"expected {expected!r}, got {row[field]!r}"
                    )
            if row["signal_status"] == "missing":
                errors.append(
                    f"dashboard signal missing: scheme_id={registry_id} "
                    "failure_category="
                    f"{row['signal_failure_category']}"
                )
            if row["backtest"] is None:
                errors.append(
                    f"dashboard backtest partition missing: scheme_id={registry_id}"
                )

        return _result(
            started_at,
            endpoint=endpoint,
            http_status=http_status,
            config=config,
            payload=payload,
            expected_registry_ids=expected_registry_ids,
            matched_rows=matched_rows,
            errors=errors,
        )


def _fetch_response(response: Any) -> tuple[Any, int]:
    if not isinstance(response, tuple) or len(response) != 2:
        raise ValueError("dashboard fetcher must return (payload, status_code)")
    payload, status_code = response
    return payload, int(status_code)


def _result(
    started_at: str,
    *,
    endpoint: str,
    http_status: int,
    config: SchemeConfig,
    payload: Mapping[str, Any] | None,
    expected_registry_ids: list[str],
    matched_rows: list[Mapping[str, Any]],
    errors: list[str],
) -> GateResult:
    status = GateStatus.PASSED if not errors else GateStatus.FAILED
    return GateResult(
        gate_name="dashboard",
        status=status,
        passed=status == GateStatus.PASSED,
        evidence=[
            Evidence("endpoint", endpoint),
            Evidence("http_status", http_status),
            Evidence("config_status", config.status),
            Evidence("config_version_status", config.version_status),
            Evidence("snapshot_id", payload.get("snapshot_id") if payload else None),
            Evidence("expected_registry_ids", expected_registry_ids),
            Evidence(
                "matched_registry_ids",
                [str(row["scheme_id"]) for row in matched_rows],
            ),
            Evidence(
                "signal_statuses",
                {
                    str(row["scheme_id"]): row["signal_status"]
                    for row in matched_rows
                },
            ),
            Evidence(
                "signal_failure_categories",
                {
                    str(row["scheme_id"]): row["signal_failure_category"]
                    for row in matched_rows
                    if row["signal_status"] == "missing"
                },
            ),
            Evidence(
                "backtest_registry_ids",
                [
                    str(row["scheme_id"])
                    for row in matched_rows
                    if row["backtest"] is not None
                ],
            ),
        ],
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
    )
