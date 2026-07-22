from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.db import get_engine
from backend.dashboard_snapshot import (
    DashboardSnapshotStore,
    SnapshotResult,
    SnapshotUnavailable,
)
from backend.factor_lab_dashboard import (
    build_factor_lab_dashboard,
    dashboard_build_diagnostics,
    encode_canonical_snapshot,
)
from backend.factor_lab_dashboard_semantics import DashboardDataError
from backend.http_compression import QAwareGZipMiddleware
from backend.services import (
    backtest_diffs,
    backtest_factor_lab_results,
    get_backtest_run,
    list_actuals,
    list_backtest_runs,
    list_data_checks,
    list_predictions,
    list_schemes,
    list_targets,
    scheme_metrics,
    sync_registry_from_configs,
)
from scheduler.executor import DEFAULT_ALGO_ENV
from scheduler.main import run_prediction_job
from shared.service_instance import (
    FINGERPRINT_VERSION,
    build_service_instance_identity,
    service_fingerprint_secret,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
DEFAULT_CORS_ORIGINS = ["http://localhost", "http://127.0.0.1"]
ADMIN_TOKEN_HEADER = "X-Admin-Token"
INDEX_CACHE_CONTROL = "no-cache, must-revalidate"
VERSIONED_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
UNVERSIONED_ASSET_CACHE_CONTROL = "no-cache, must-revalidate"
# Backward-compatible name for older tests/importers. Static serving now uses
# the three explicit policies above.
FRONTEND_CACHE_CONTROL = UNVERSIONED_ASSET_CACHE_CONTROL
logger = logging.getLogger(__name__)
_DEFAULT_INSTANCE_NONCE = secrets.token_hex(32)
_REQUEST_ID_PATTERN = re.compile(r"[!-~]{1,128}\Z", flags=re.ASCII)
_DASHBOARD_ERROR_UNAVAILABLE = "dashboard_snapshot_unavailable"
_DASHBOARD_ERROR_STALE = "dashboard_snapshot_stale"
_DASHBOARD_ERROR_NOT_PREWARMED = "dashboard_snapshot_not_prewarmed"
_DASHBOARD_QUERY_ERROR = "dashboard_query_not_allowed"
_dashboard_health_lock = Lock()
_dashboard_health: dict[str, str | None] = {
    "status": "degraded",
    "error_code": _DASHBOARD_ERROR_NOT_PREWARMED,
}


class _FrontendAssetReferenceParser(HTMLParser):
    """提取 index 中带非空版本 token 的本地 CSS/JS 精确引用。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: dict[str, str] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attribute_name = "href" if tag.casefold() == "link" else "src"
        if tag.casefold() not in {"link", "script"}:
            return
        raw_url = dict(attrs).get(attribute_name)
        if not raw_url:
            return
        parsed = urlsplit(raw_url)
        if parsed.scheme or parsed.netloc:
            return
        normalized_path = parsed.path.removeprefix("./").lstrip("/")
        if Path(normalized_path).suffix.casefold() not in {".css", ".js"}:
            return
        if not _has_nonempty_version_token(parsed.query):
            return
        self.references[normalized_path] = parsed.query


def _has_nonempty_version_token(query: str) -> bool:
    return any(
        key.casefold() in {"v", "version"} and bool(value)
        for key, value in parse_qsl(query, keep_blank_values=True)
    )


class NoCacheFrontendStaticFiles(StaticFiles):
    """按 index 当前精确引用为前端资源设置安全缓存策略。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._versioned_asset_queries = self._load_versioned_asset_queries()

    def _load_versioned_asset_queries(self) -> dict[str, str]:
        directory = getattr(self, "directory", None)
        if directory is None:
            return {}
        index_path = Path(directory) / "index.html"
        try:
            html = index_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return {}
        parser = _FrontendAssetReferenceParser()
        parser.feed(html)
        return parser.references

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        normalized_path = path.removeprefix("./").lstrip("/")
        query_string = scope.get("query_string", b"").decode(
            "latin-1",
            errors="strict",
        )
        is_versioned_asset = (
            Path(normalized_path).suffix.casefold() in {".css", ".js"}
            and bool(query_string)
            and self._versioned_asset_queries.get(normalized_path)
            == query_string
        )
        if is_versioned_asset:
            response.headers["Cache-Control"] = VERSIONED_ASSET_CACHE_CONTROL
            for header_name in ("Pragma", "Expires"):
                if header_name in response.headers:
                    del response.headers[header_name]
        else:
            response.headers["Cache-Control"] = (
                INDEX_CACHE_CONTROL
                if normalized_path in {"", ".", "index.html"}
                else UNVERSIONED_ASSET_CACHE_CONTROL
            )
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


def _build_dashboard_snapshot() -> dict[str, Any]:
    """延迟取得 DB engine；模块 import 不连接数据库。"""
    return build_factor_lab_dashboard(get_engine())


dashboard_snapshot_store = DashboardSnapshotStore(_build_dashboard_snapshot)


def _set_dashboard_health(
    status: Literal["ready", "degraded"],
    error_code: str | None,
) -> None:
    """原子更新 health 中不含内部异常文本的 dashboard 子状态。"""
    if status == "ready":
        error_code = None
    with _dashboard_health_lock:
        _dashboard_health["status"] = status
        _dashboard_health["error_code"] = error_code


def _dashboard_health_snapshot() -> dict[str, str | None]:
    """返回 dashboard health 状态副本。"""
    with _dashboard_health_lock:
        return dict(_dashboard_health)


def _record_dashboard_prewarm_failure() -> None:
    """记录不含内部异常文本的稳定预热失败状态。"""
    _set_dashboard_health("degraded", _DASHBOARD_ERROR_UNAVAILABLE)
    event = {
        "status": "degraded",
        "error_code": _DASHBOARD_ERROR_UNAVAILABLE,
    }
    logger.error(
        "factor_lab_dashboard_prewarm %s",
        json.dumps(event, separators=(",", ":"), sort_keys=True),
        extra={"dashboard_event": dict(event)},
    )


def _cors_origins() -> list[str]:
    """从环境变量读取 CORS 允许来源；未配置时退回本地白名单。"""
    raw = os.getenv("BOND_CORS_ORIGINS")
    if not raw:
        return list(DEFAULT_CORS_ORIGINS)
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    return origins or list(DEFAULT_CORS_ORIGINS)


def require_admin_token(x_admin_token: str | None = Header(default=None, alias=ADMIN_TOKEN_HEADER)) -> None:
    """管理员令牌校验。

    令牌从环境变量 BOND_ADMIN_TOKEN 读取，与请求头 X-Admin-Token 比对。
    - 未配置 BOND_ADMIN_TOKEN：禁用写接口，避免本地直连或隧道误配暴露写库入口。
    - 已配置：必须携带匹配的 X-Admin-Token，否则拒绝。
    """
    expected = os.getenv("BOND_ADMIN_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="admin token is not configured")
    if not x_admin_token:
        raise HTTPException(status_code=401, detail="missing admin token")
    if not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=403, detail="invalid admin token")


app = FastAPI(title="Bond Factor Lab API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", ADMIN_TOKEN_HEADER],
)
app.add_middleware(QAwareGZipMiddleware)


@app.on_event("startup")
def _sync_registry_on_startup() -> None:
    """启动时先尝试同步 registry，再预热 dashboard 快照。"""
    try:
        sync_registry_from_configs(get_engine())
    except Exception:
        logger.error("Registry sync on startup failed")

    try:
        result = dashboard_snapshot_store.prewarm()
    except (SnapshotUnavailable, DashboardDataError):
        _record_dashboard_prewarm_failure()
    except Exception:
        _record_dashboard_prewarm_failure()
    else:
        if result.cache_status == "STALE":
            _set_dashboard_health("degraded", _DASHBOARD_ERROR_STALE)
        else:
            _set_dashboard_health("ready", None)


class TriggerRequest(BaseModel):
    """手动触发预测请求。"""

    predict_date: str | None = Field(default=None, description="YYYY-MM-DD; omitted means today")
    force: bool = Field(default=False, description="Run even when the date is not a trading day")
    algo_env: str | None = Field(default=None, description="Override algorithm conda env")


@app.get("/api/health")
def health() -> dict:
    engine = get_engine()
    with engine.connect() as connection:
        value = connection.execute(text("SELECT 1")).scalar_one()
    if int(value) != 1:
        raise RuntimeError("database health check returned an unexpected value")
    fingerprint_secret = service_fingerprint_secret()
    if fingerprint_secret is None:
        identity = {"fingerprint_version": FINGERPRINT_VERSION, "fingerprint": None}
    else:
        identity = build_service_instance_identity(
            engine,
            project_root=PROJECT_ROOT,
            runtime_profile=os.getenv(
                "BOND_FACTOR_LAB_RUNTIME_PROFILE",
                "blackbox-v2-v1",
            ),
            instance_nonce=os.getenv(
                "BOND_FACTOR_LAB_INSTANCE_NONCE",
                _DEFAULT_INSTANCE_NONCE,
            ),
            fingerprint_secret=fingerprint_secret,
        )
    return {
        "status": "ok",
        "service_instance": identity,
        "dashboard_snapshot": _dashboard_health_snapshot(),
    }


def _request_id(request: Request) -> str:
    """复用合法 edge request ID；缺失或不安全时生成本地 UUID。"""
    candidate = request.headers.get("x-request-id")
    if candidate is not None and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return uuid4().hex


def _dashboard_age_ms(result: SnapshotResult) -> int:
    return max(0, int(result.age_seconds * 1_000))


def _safe_store_diagnostics() -> dict[str, Any]:
    try:
        return dict(dashboard_snapshot_store.diagnostics())
    except Exception:
        return {}


def _safe_build_diagnostics(snapshot_id: str) -> dict[str, Any]:
    try:
        return dict(dashboard_build_diagnostics(snapshot_id) or {})
    except Exception:
        return {}


def _duration_ms(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        duration = float(value) * 1_000
    except (TypeError, ValueError):
        return None
    if duration < 0:
        return None
    return round(duration, 3)


def _integer_metric(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _server_timing(
    *,
    build_diagnostics: dict[str, Any],
    build_seconds: float | None,
    response_serialization_seconds: float,
    route_seconds: float,
) -> str:
    timings: list[tuple[str, float | None]] = [
        (
            "db",
            _duration_ms(build_diagnostics.get("db_read_seconds")),
        ),
        (
            "canonical",
            _duration_ms(
                build_diagnostics.get("canonical_build_seconds")
            ),
        ),
        (
            "canonical_serialization",
            _duration_ms(
                build_diagnostics.get("canonical_serialization_seconds")
            ),
        ),
        ("build", _duration_ms(build_seconds)),
        (
            "serialization",
            round(response_serialization_seconds * 1_000, 3),
        ),
        ("route", round(route_seconds * 1_000, 3)),
    ]
    return ", ".join(
        f"{name};dur={duration:.3f}"
        for name, duration in timings
        if duration is not None
    )


def _dashboard_response_headers(
    *,
    request_id: str,
    snapshot_id: str,
    cache_status: str,
    age_ms: int,
    server_timing: str,
    stale: bool,
) -> dict[str, str]:
    headers = {
        "Cache-Control": "no-store",
        "X-Request-ID": request_id,
        "X-Dashboard-Snapshot-ID": snapshot_id,
        "X-Dashboard-Cache": cache_status,
        "X-Dashboard-Snapshot-Age": str(age_ms),
        "Server-Timing": server_timing,
    }
    if stale:
        headers["X-Dashboard-Warning"] = "stale-last-known-good"
    return headers


def _log_dashboard_request(event: dict[str, Any]) -> None:
    structured_event = dict(event)
    logger.info(
        "factor_lab_dashboard_request %s",
        json.dumps(
            structured_event,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        extra={"dashboard_event": structured_event},
    )


def _dashboard_error_response(
    *,
    request_id: str,
    error_code: str,
    status_code: int,
    route_started_at: float,
) -> Response:
    route_seconds = time.perf_counter() - route_started_at
    raw_body = json.dumps(
        {"error_code": error_code},
        separators=(",", ":"),
    ).encode("utf-8")
    headers = _dashboard_response_headers(
        request_id=request_id,
        snapshot_id="unavailable",
        cache_status="UNAVAILABLE",
        age_ms=0,
        server_timing=_server_timing(
            build_diagnostics={},
            build_seconds=None,
            response_serialization_seconds=0.0,
            route_seconds=route_seconds,
        ),
        stale=False,
    )
    _log_dashboard_request(
        {
            "request_id": request_id,
            "snapshot_id": "unavailable",
            "cache": "UNAVAILABLE",
            "snapshot_age_ms": 0,
            "waiter_count": 0,
            "db_read_ms": None,
            "canonical_build_ms": None,
            "canonical_serialization_ms": None,
            "response_serialization_ms": 0.0,
            "build_ms": None,
            "route_ms": round(route_seconds * 1_000, 3),
            "scheme_count": None,
            "live_row_count": None,
            "backtest_row_count": None,
            "raw_bytes": len(raw_body),
            "gzip_bytes": None,
            "status": status_code,
            "error_code": error_code,
        }
    )
    return Response(
        content=raw_body,
        status_code=status_code,
        media_type="application/json",
        headers=headers,
    )


def _factor_lab_dashboard_response(request: Request) -> Response:
    """构造 GET/HEAD 共用的精确 dashboard 表示。"""
    route_started_at = time.perf_counter()
    request_id = _request_id(request)
    if request.scope.get("query_string", b""):
        return _dashboard_error_response(
            request_id=request_id,
            error_code=_DASHBOARD_QUERY_ERROR,
            status_code=400,
            route_started_at=route_started_at,
        )

    try:
        result = dashboard_snapshot_store.get()
        age_ms = _dashboard_age_ms(result)
        response_payload = dict(result.payload)
        is_stale = result.cache_status == "STALE"
        response_payload["stale"] = is_stale
        response_payload["snapshot_age_ms"] = age_ms
        serialization_started_at = time.perf_counter()
        encoding = encode_canonical_snapshot(response_payload)
        response_serialization_seconds = (
            time.perf_counter() - serialization_started_at
        )
    except (SnapshotUnavailable, DashboardDataError):
        _set_dashboard_health("degraded", _DASHBOARD_ERROR_UNAVAILABLE)
        return _dashboard_error_response(
            request_id=request_id,
            error_code=_DASHBOARD_ERROR_UNAVAILABLE,
            status_code=503,
            route_started_at=route_started_at,
        )

    snapshot_id = str(response_payload["snapshot_id"])
    build_diagnostics = _safe_build_diagnostics(snapshot_id)
    store_diagnostics = _safe_store_diagnostics()
    build_seconds = result.build_seconds
    if build_seconds is None:
        last_build_seconds = store_diagnostics.get("last_build_seconds")
        build_seconds = (
            float(last_build_seconds)
            if isinstance(last_build_seconds, (int, float))
            and not isinstance(last_build_seconds, bool)
            else None
        )
    active_waiters = _integer_metric(
        store_diagnostics.get("active_waiter_count")
    )
    last_waiters = _integer_metric(store_diagnostics.get("last_waiter_count"))
    waiter_count = active_waiters if active_waiters else (last_waiters or 0)
    route_seconds = time.perf_counter() - route_started_at
    server_timing = _server_timing(
        build_diagnostics=build_diagnostics,
        build_seconds=build_seconds,
        response_serialization_seconds=response_serialization_seconds,
        route_seconds=route_seconds,
    )
    headers = _dashboard_response_headers(
        request_id=request_id,
        snapshot_id=snapshot_id,
        cache_status=result.cache_status,
        age_ms=age_ms,
        server_timing=server_timing,
        stale=is_stale,
    )
    if is_stale:
        _set_dashboard_health("degraded", _DASHBOARD_ERROR_STALE)
    else:
        _set_dashboard_health("ready", None)
    _log_dashboard_request(
        {
            "request_id": request_id,
            "snapshot_id": snapshot_id,
            "cache": result.cache_status,
            "snapshot_age_ms": age_ms,
            "waiter_count": waiter_count,
            "db_read_ms": _duration_ms(
                build_diagnostics.get("db_read_seconds")
            ),
            "canonical_build_ms": _duration_ms(
                build_diagnostics.get("canonical_build_seconds")
            ),
            "canonical_serialization_ms": _duration_ms(
                build_diagnostics.get("canonical_serialization_seconds")
            ),
            "response_serialization_ms": round(
                response_serialization_seconds * 1_000,
                3,
            ),
            "build_ms": _duration_ms(build_seconds),
            "route_ms": round(route_seconds * 1_000, 3),
            "scheme_count": _integer_metric(
                build_diagnostics.get("scheme_count")
            ),
            "live_row_count": _integer_metric(
                build_diagnostics.get("live_row_count")
            ),
            "backtest_row_count": _integer_metric(
                build_diagnostics.get("backtest_row_count")
            ),
            "raw_bytes": encoding.raw_size,
            "gzip_bytes": encoding.gzip_size,
            "status": 200,
            "error_code": (
                _DASHBOARD_ERROR_STALE if is_stale else None
            ),
        }
    )
    return Response(
        content=encoding.raw_body,
        media_type="application/json",
        headers=headers,
    )


@app.get("/api/factor-lab/dashboard")
def api_factor_lab_dashboard(request: Request) -> Response:
    return _factor_lab_dashboard_response(request)


@app.head("/api/factor-lab/dashboard")
def api_factor_lab_dashboard_head(request: Request) -> Response:
    return _factor_lab_dashboard_response(request)


@app.get("/api/schemes")
def api_schemes() -> list[dict]:
    return list_schemes(get_engine())


@app.get("/api/targets")
def api_targets() -> dict:
    targets = list_targets(get_engine())
    return {
        "targets": targets,
        "target_labels": {
            item["target_code"]: item["display_name"]
            for item in targets
            if item["status"] == "active"
        },
    }


@app.get("/api/metrics/{scheme_id}")
def api_metrics(
    scheme_id: str,
    tenor: str | None = None,
    start_month: str | None = None,
    end_month: str | None = None,
) -> dict:
    if tenor is not None:
        raise HTTPException(status_code=400, detail="tenor query is not supported; use registry scheme_id")
    if start_month and end_month and end_month < start_month:
        raise HTTPException(status_code=400, detail="end_month must be greater than or equal to start_month")
    try:
        return scheme_metrics(get_engine(), scheme_id, start_month=start_month, end_month=end_month)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/predictions")
def api_predictions(
    scheme_id: str,
    tenor: str | None = None,
    start_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    if tenor is not None:
        raise HTTPException(status_code=400, detail="tenor query is not supported; use registry scheme_id")
    try:
        return list_predictions(
            get_engine(),
            scheme_id=scheme_id,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/actuals")
def api_actuals(
    tenor: str | None = None,
    start_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return list_actuals(
        get_engine(),
        tenor=tenor,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )


@app.get("/api/backtests/runs")
def api_backtest_runs(
    scheme_id: str | None = None,
    benchmark_id: str | None = None,
) -> dict:
    return {"runs": list_backtest_runs(get_engine(), scheme_id=scheme_id, benchmark_id=benchmark_id)}


@app.get("/api/backtests/runs/{run_id}")
def api_backtest_run(
    run_id: int,
    include_predictions: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    result = get_backtest_run(
        get_engine(),
        run_id,
        include_predictions=include_predictions,
        limit=limit,
        offset=offset,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"backtest run not found: {run_id}")
    return result


@app.get("/api/backtests/runs/{run_id}/diffs")
def api_backtest_diffs(run_id: int) -> dict:
    result = backtest_diffs(get_engine(), run_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"backtest run not found: {run_id}")
    return result


@app.get("/api/backtests/data-checks")
def api_backtest_data_checks(
    benchmark_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    return {"checks": list_data_checks(get_engine(), benchmark_id=benchmark_id, limit=limit)}


@app.get("/api/backtests/factor-lab")
def api_backtest_factor_lab(
    benchmark_id: str | None = None,
    data_source: str | None = None,
) -> dict:
    return backtest_factor_lab_results(get_engine(), benchmark_id=benchmark_id, data_source=data_source)


def _run_trigger(scheme_id: str, request: TriggerRequest) -> None:
    logger.info("Manual trigger started for %s (triggered_by=admin_api)", scheme_id)
    try:
        run_prediction_job(
            scheme_id,
            run_date=request.predict_date,
            algo_env=request.algo_env or os.getenv("BOND_ALGO_CONDA_ENV", DEFAULT_ALGO_ENV),
            force=request.force,
        )
    except Exception:
        logger.exception("Manual trigger failed for %s", scheme_id)


@app.post("/api/schemes/{scheme_id}/trigger", status_code=202, dependencies=[Depends(require_admin_token)])
def api_trigger_scheme(scheme_id: str, request: TriggerRequest, background_tasks: BackgroundTasks) -> dict:
    known = {
        item["scheme_id"]: item["base_scheme_id"]
        for item in list_schemes(get_engine())
        if item.get("status") == "active"
    }
    base_scheme_id = known.get(scheme_id)
    if base_scheme_id is None:
        raise HTTPException(status_code=404, detail=f"scheme not found: {scheme_id}")
    background_tasks.add_task(_run_trigger, base_scheme_id, request)
    return {
        "accepted": True,
        "scheme_id": scheme_id,
        "base_scheme_id": base_scheme_id,
        "predict_date": request.predict_date,
        "force": request.force,
    }


@app.post("/api/admin/registry/sync", dependencies=[Depends(require_admin_token)])
def api_admin_registry_sync() -> dict:
    """受保护的管理端点：显式把 schemes/ 配置同步到 registry（写库）。"""
    synced = sync_registry_from_configs(get_engine(), force=True)
    return {"synced": synced}


if FRONTEND_ROOT.exists():
    app.mount("/", NoCacheFrontendStaticFiles(directory=FRONTEND_ROOT, html=True), name="frontend")
