from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlalchemy import bindparam, text

from backend.factor_lab_dashboard import MAX_RAW_JSON_BYTES, dashboard_read_connection
from backend.factor_lab_dashboard_semantics import validate_dashboard_payload
from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.http_target import url_origin, validate_api_target
from harness.result import Evidence, GateResult, GateStatus
from scheduler.discovery import SchemeConfig, load_scheme_config
from scheduler.repository import registry_scheme_id
from shared.legacy_prediction_migration import load_legacy_prediction_migrations


DashboardFetcher = Callable[..., tuple[Any, ...]]
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
_COOKIE_NAME = "__Host-bfl-session"


class ApiProbeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_summary: str | None = None,
        request_id: str | None = None,
        fetched_at: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_summary = error_summary or message
        self.request_id = request_id
        self.fetched_at = fetched_at or utc_now()


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    """认证探针只允许在初始 HTTP Origin 内跟随跳转。"""

    def __init__(self, initial_url: str) -> None:
        super().__init__()
        self._origin = url_origin(initial_url)

    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        if url_origin(newurl) != self._origin:
            raise ApiProbeError(
                "authenticated dashboard probe rejected cross-origin redirect",
                status_code=int(code),
                error_summary="cross-origin redirect rejected",
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_session_token(value: str | None) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("dashboard probe requires a session token")
    if len(value) > 512 or any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in value
    ):
        raise ValueError("dashboard session token has an invalid format")
    return value


def fetch_json(
    url: str,
    timeout_sec: int = 10,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    *,
    session_token: str | None = None,
) -> tuple[Any, int, dict[str, str | None]]:
    """使用授权会话执行只读、同源 JSON API 探针。"""
    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")
    token = _validate_session_token(session_token)
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Cookie": f"{_COOKIE_NAME}={token}",
        },
        method="GET",
    )
    opener = build_opener(_SameOriginRedirectHandler(url))
    try:
        with opener.open(request, timeout=timeout_sec) as response:
            status = int(getattr(response, "status", 200))
            raw = response.read(max_response_bytes + 1)
            request_id = response.headers.get("X-Request-ID")
    except HTTPError as exc:
        try:
            raw = exc.read(max_response_bytes + 1)
            request_id = exc.headers.get("X-Request-ID") if exc.headers else None
        finally:
            exc.close()
        if len(raw) > max_response_bytes:
            raise ApiProbeError(
                f"API error response exceeds {max_response_bytes} bytes",
                status_code=int(exc.code),
                error_summary=f"response exceeds {max_response_bytes} bytes",
                request_id=request_id,
            ) from exc
        raise ApiProbeError(
            f"HTTP {exc.code}",
            status_code=int(exc.code),
            error_summary=f"http_status_{int(exc.code)}",
            request_id=request_id,
        ) from exc
    if len(raw) > max_response_bytes:
        raise ApiProbeError(
            f"API response exceeds {max_response_bytes} bytes",
            status_code=status,
            error_summary=f"response exceeds {max_response_bytes} bytes",
            request_id=request_id,
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiProbeError(
            f"API response is not valid UTF-8 JSON: {exc}",
            status_code=status,
            error_summary=str(exc)[:512],
            request_id=request_id,
        ) from exc
    if not isinstance(payload, (dict, list)):
        raise ApiProbeError(
            "API payload must be a JSON object or array",
            status_code=status,
            request_id=request_id,
        )
    return payload, status, {
        "fetched_at": utc_now(),
        "request_id": request_id,
    }


class DashboardGate(Gate):
    """批量校验 active 方案在唯一 Dashboard Summary 中的公开表示。"""

    name = "dashboard"

    def __init__(self, *, fetcher: DashboardFetcher = fetch_json) -> None:
        self._fetcher = fetcher

    def run(self, ctx: GateContext) -> GateResult:
        result = guarded_result(
            self.name,
            lambda started_at: self._run(ctx, started_at),
        )
        return _redact_gate_result(result, ctx.dashboard_session_token)

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        selected_ids = ctx.dashboard_scheme_ids or (ctx.scheme_id,)
        if (
            not selected_ids
            or any(
                not isinstance(item, str)
                or not item
                or item != item.strip()
                for item in selected_ids
            )
            or len(set(selected_ids)) != len(selected_ids)
        ):
            raise ValueError("dashboard gate requires unique non-empty scheme ids")
        configs = [
            load_scheme_config(
                ctx.project_root / "schemes" / scheme_id / "config.yaml"
            )
            for scheme_id in selected_ids
        ]
        if any(
            config.scheme_id != selected_id
            for selected_id, config in zip(selected_ids, configs, strict=True)
        ):
            raise ValueError("dashboard scheme directory and canonical id mismatch")

        target = validate_api_target(ctx.api_base_url, ctx.api_prefix)
        endpoint = target.url("/api/factor-lab/dashboard")
        probe_error: str | None = None
        try:
            payload, http_status, response_metadata = _fetch_response(
                self._fetcher(
                    endpoint,
                    timeout_sec=min(ctx.timeout_sec, 30),
                    max_response_bytes=MAX_RAW_JSON_BYTES,
                    session_token=ctx.dashboard_session_token,
                )
            )
        except ApiProbeError as exc:
            payload = None
            http_status = exc.status_code or 0
            response_metadata = {
                "fetched_at": exc.fetched_at,
                "request_id": exc.request_id,
            }
            probe_error = f"dashboard probe failed: {exc.error_summary}"
        fetched_at = response_metadata.get("fetched_at") or utc_now()
        request_id = response_metadata.get("request_id")
        common_errors: list[str] = []
        if probe_error is not None:
            common_errors.append(probe_error)
        elif http_status != 200:
            common_errors.append(
                f"dashboard probe returned HTTP {http_status}, expected 200"
            )
        if probe_error is None and not isinstance(payload, Mapping):
            common_errors.append("dashboard probe did not return a JSON object")
        elif probe_error is None and http_status == 200:
            try:
                validate_dashboard_payload(payload)
            except Exception as exc:
                common_errors.append(
                    f"dashboard payload validation failed: {exc}"
                )

        expected_by_config = {
            config.scheme_id: [
                registry_scheme_id(config.scheme_id, config.horizon, tenor)
                for tenor in config.tenors
            ]
            for config in configs
        }
        registry_by_id: dict[str, Mapping[str, Any]] = {}
        if not common_errors:
            if ctx.engine_factory is None:
                raise ValueError(
                    "dashboard gate requires same-host Registry engine_factory"
                )
            expected_registry_ids = [
                registry_id
                for config in configs
                for registry_id in expected_by_config[config.scheme_id]
            ]
            engine = ctx.engine_factory()
            try:
                with dashboard_read_connection(engine) as conn:
                    registry_rows = conn.execute(
                        text(
                            "SELECT scheme_id, name, description, owner, status "
                            "FROM t_scheme_registry WHERE scheme_id IN :registry_ids"
                        ).bindparams(bindparam("registry_ids", expanding=True)),
                        {"registry_ids": expected_registry_ids},
                    ).mappings().all()
            finally:
                engine.dispose()
            registry_by_id = {row["scheme_id"]: row for row in registry_rows}

        all_errors: list[str] = []
        scheme_results: list[dict[str, Any]] = []
        compatibility_path = (
            ctx.project_root
            / "deploy"
            / "legacy_prediction_migration_compatibility_v1.json"
        )
        live_only_scopes = (
            {
                (item.prediction_scheme_id, item.target_tenor, item.horizon)
                for item in load_legacy_prediction_migrations(ctx.project_root)
            }
            if compatibility_path.is_file()
            else set()
        )
        for config in configs:
            result = _validate_config_summary(
                config,
                payload if isinstance(payload, Mapping) else None,
                expected_by_config[config.scheme_id],
                registry_by_id,
                common_errors,
                live_only_scopes,
            )
            result.update(
                {
                    "snapshot_id": (
                        payload.get("snapshot_id")
                        if isinstance(payload, Mapping)
                        else None
                    ),
                    "fetched_at": fetched_at,
                    "request_id": request_id,
                }
            )
            scheme_results.append(result)
            all_errors.extend(
                f"{config.scheme_id}: {error}" for error in result["errors"]
            )

        return _result(
            started_at,
            endpoint=endpoint,
            http_status=http_status,
            payload=payload if isinstance(payload, Mapping) else None,
            scheme_results=scheme_results,
            errors=all_errors,
            fetched_at=fetched_at,
            request_id=request_id,
        )


def _fetch_response(
    response: Any,
) -> tuple[Any, int, dict[str, str | None]]:
    if not isinstance(response, tuple) or len(response) not in {2, 3}:
        raise ValueError(
            "dashboard fetcher must return (payload, status_code[, metadata])"
        )
    payload, status_code = response[:2]
    if len(response) == 2:
        metadata: dict[str, str | None] = {}
    else:
        raw_metadata = response[2]
        if not isinstance(raw_metadata, Mapping):
            raise ValueError("dashboard fetcher metadata must be an object")
        metadata = {
            "fetched_at": _optional_string(
                raw_metadata.get("fetched_at"),
                field="fetched_at",
            ),
            "request_id": _optional_string(
                raw_metadata.get("request_id"),
                field="request_id",
            ),
        }
    return payload, int(status_code), metadata


def _optional_string(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or not value.isprintable()
        or (
            field == "request_id"
            and any(
                not (
                    character.isascii()
                    and (character.isalnum() or character in "._:-")
                )
                for character in value
            )
        )
    ):
        raise ValueError(
            f"dashboard fetcher {field} must be a bounded printable string"
        )
    return value


def _validate_config_summary(
    config: SchemeConfig,
    payload: Mapping[str, Any] | None,
    expected_registry_ids: list[str],
    registry_by_id: Mapping[str, Mapping[str, Any]],
    common_errors: list[str],
    live_only_scopes: set[tuple[str, str, int]],
) -> dict[str, Any]:
    errors = list(common_errors)
    matched_rows: list[Mapping[str, Any]] = []
    if not errors and payload is not None:
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
            registry = registry_by_id.get(registry_id)
            if registry is None or registry["status"] != "active":
                errors.append(f"active Registry missing: scheme_id={registry_id}")
                continue
            expected_fields = {
                "base_scheme_id": config.scheme_id,
                "horizon": config.horizon,
                "task_type": config.task_type,
                "frequency": config.frequency,
                "target_tenor": target_tenor,
                "status": "active",
                "name": str(registry["name"] or "").strip(),
                "description": str(registry["description"] or ""),
                "owner": registry["owner"],
            }
            for field, expected in expected_fields.items():
                if row[field] != expected:
                    errors.append(
                        f"dashboard {registry_id} {field} mismatch: "
                        f"expected {expected!r}, got {row[field]!r}"
                    )
            if row["backtest"] is None and not (
                (config.scheme_id, target_tenor, config.horizon)
                in live_only_scopes
                and any(
                    monthly[1] == "live" and monthly[2] > 0
                    for monthly in row["monthly_rows"]
                )
            ):
                errors.append(
                    f"dashboard backtest partition missing: scheme_id={registry_id}"
                )
    return {
        "base_scheme_id": config.scheme_id,
        "status": "passed" if not errors else "failed",
        "errors": errors,
        "config_status": config.status,
        "config_version_status": config.version_status,
        "expected_registry_ids": expected_registry_ids,
        "matched_registry_ids": [str(row["scheme_id"]) for row in matched_rows],
        "backtest_registry_ids": [
            str(row["scheme_id"])
            for row in matched_rows
            if row["backtest"] is not None
        ],
    }


def _result(
    started_at: str,
    *,
    endpoint: str,
    http_status: int,
    payload: Mapping[str, Any] | None,
    scheme_results: list[dict[str, Any]],
    errors: list[str],
    fetched_at: str,
    request_id: str | None,
) -> GateResult:
    status = GateStatus.PASSED if not errors else GateStatus.FAILED
    expected_registry_ids = [
        registry_id
        for result in scheme_results
        for registry_id in result["expected_registry_ids"]
    ]
    matched_registry_ids = [
        registry_id
        for result in scheme_results
        for registry_id in result["matched_registry_ids"]
    ]
    backtest_registry_ids = [
        registry_id
        for result in scheme_results
        for registry_id in result["backtest_registry_ids"]
    ]
    evidence = [
        Evidence("endpoint", endpoint),
        Evidence("http_status", http_status),
        Evidence("snapshot_id", payload.get("snapshot_id") if payload else None),
        Evidence("fetched_at", fetched_at),
        Evidence("request_id", request_id),
        Evidence("expected_registry_ids", expected_registry_ids),
        Evidence("matched_registry_ids", matched_registry_ids),
        Evidence("backtest_registry_ids", backtest_registry_ids),
        Evidence("scheme_results", scheme_results),
    ]
    if len(scheme_results) == 1:
        evidence[2:2] = [
            Evidence("config_status", scheme_results[0]["config_status"]),
            Evidence(
                "config_version_status",
                scheme_results[0]["config_version_status"],
            ),
        ]
    return GateResult(
        gate_name="dashboard",
        status=status,
        evidence=evidence,
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
    )


def _redact_gate_result(result: GateResult, secret: str | None) -> GateResult:
    """在 Gate 的唯一返回边界清除所有路径可能携带的会话值。"""
    return GateResult(
        gate_name=result.gate_name,
        status=result.status,
        evidence=[
            Evidence(item.key, _redact_secret(item.value, secret))
            for item in result.evidence
        ],
        errors=[str(_redact_secret(error, secret)) for error in result.errors],
        started_at=result.started_at,
        finished_at=result.finished_at,
    )


def _redact_secret(value: Any, secret: str | None) -> Any:
    """递归移除服务端响应或注入 fetcher 可能回显的会话值。"""
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[redacted-session]")
    if isinstance(value, Mapping):
        return {
            _redact_secret(key, secret): _redact_secret(item, secret)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secret(item, secret) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_secret(item, secret) for item in value)
    return value
