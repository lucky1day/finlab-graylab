from __future__ import annotations

import os
from typing import Any

from harness.config_loader import load_config_raw
from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.probes.api_probe import (
    factor_lab_url,
    fetch_json,
    find_factor_lab_cell,
    metrics_cell_present,
    metrics_url,
)
from harness.result import Evidence, GateResult, GateStatus


class ApiGate(Gate):
    name = "api"

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        config = load_config_raw(ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml")
        tenors = [str(item) for item in config.get("tenors", [])]
        base_url = os.getenv("BOND_FACTOR_LAB_API_BASE_URL", ctx.api_base_url).rstrip("/")
        probe_warnings: list[str] = []
        endpoint = factor_lab_url(base_url)
        status_code: int | None = None
        cell: dict[str, Any] | None = None
        payload_keys: list[str] = []

        try:
            payload, status_code = _normalize_fetch_response(fetch_json(endpoint, timeout_sec=min(ctx.timeout_sec, 30)))
            payload_keys = sorted(str(key) for key in payload.keys())
            cell = find_factor_lab_cell(payload, ctx.scheme_id, tenors)
        except Exception as exc:
            probe_warnings.append(f"factor-lab probe failed: {exc}")

        if cell is None and tenors:
            endpoint = metrics_url(base_url, ctx.scheme_id, tenors[0])
            try:
                payload, status_code = _normalize_fetch_response(fetch_json(endpoint, timeout_sec=min(ctx.timeout_sec, 30)))
                payload_keys = sorted(str(key) for key in payload.keys())
                if metrics_cell_present(payload):
                    cell = {"scheme_id": ctx.scheme_id, "tenor": tenors[0], **payload}
            except Exception as exc:
                probe_warnings.append(f"metrics probe failed: {exc}")

        errors: list[str] = []
        if cell is None:
            errors.extend(probe_warnings)
            errors.append(f"API matrix cell not found for scheme_id={ctx.scheme_id}")

        finished_at = utc_now()
        status = GateStatus.PASSED if cell is not None and not errors else GateStatus.FAILED
        return GateResult(
            gate_name=self.name,
            status=status,
            passed=status == GateStatus.PASSED,
            evidence=[
                Evidence("endpoint", endpoint),
                Evidence("http_status", status_code),
                Evidence("payload_keys", payload_keys),
                Evidence("matrix_cell_present", cell is not None),
                Evidence("matched_scheme_id", cell.get("scheme_id") if cell else None),
                Evidence("matched_tenor", cell.get("tenor") if cell else None),
                Evidence("monthly_rows", len(cell.get("monthly_metrics", [])) if cell else 0),
                Evidence("probe_warnings", probe_warnings),
            ],
            errors=[] if status == GateStatus.PASSED else errors,
            started_at=started_at,
            finished_at=finished_at,
        )


def _normalize_fetch_response(value: Any) -> tuple[dict[str, Any], int]:
    if isinstance(value, tuple) and len(value) == 2:
        payload, status_code = value
        if not isinstance(payload, dict):
            raise ValueError("API probe returned non-object payload")
        return payload, int(status_code)
    if isinstance(value, dict):
        return value, 200
    raise ValueError("API probe returned unsupported response shape")
