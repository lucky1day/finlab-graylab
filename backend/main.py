from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import time
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from fastapi import (
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
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.db import get_dashboard_engine, get_engine
from backend.factor_lab_dashboard import (
    build_factor_lab_dashboard,
    dashboard_build_diagnostics,
    encode_canonical_snapshot,
)
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
from shared.one_shot_control_plane import (
    LAUNCHD_ONE_SHOT_CONTROL_PLANE,
    require_scheduled_one_shot_control_plane,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
DEFAULT_CORS_ORIGINS = ["http://localhost", "http://127.0.0.1"]
ADMIN_TOKEN_HEADER = "X-Admin-Token"
INDEX_CACHE_CONTROL = "no-cache, must-revalidate"
VERSIONED_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
UNVERSIONED_ASSET_CACHE_CONTROL = "no-cache, must-revalidate"
logger = logging.getLogger(__name__)
_REQUEST_ID_PATTERN = re.compile(r"[!-~]{1,128}\Z", flags=re.ASCII)
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTH_PATTERN = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
_DASHBOARD_ERROR_UNAVAILABLE = "dashboard_data_unavailable"
_DASHBOARD_QUERY_ERROR = "dashboard_query_not_allowed"


class _FrontendAssetReferenceParser(HTMLParser):
    """提取 index 中带非空版本 token 的本地 CSS/JS 精确引用。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: dict[str, set[tuple[str, str]]] = {}

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
        version_token = _single_unambiguous_version_token(parsed.query)
        if version_token is None:
            return
        self.references.setdefault(normalized_path, set()).add(version_token)


def _single_unambiguous_version_token(
    query: str,
) -> tuple[str, str] | None:
    """仅接受一个未编码、非空的 v/version 参数。"""
    if not query or "%" in query:
        return None
    version_tokens = [
        (key.casefold(), value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.casefold() in {"v", "version"}
    ]
    if len(version_tokens) != 1 or not version_tokens[0][1]:
        return None
    return version_tokens[0]


def _apply_revalidation_headers(headers: Any, cache_control: str) -> None:
    """统一给所有非 immutable 静态结果设置显式重验证头。"""
    headers["Cache-Control"] = cache_control
    headers["Pragma"] = "no-cache"
    headers["Expires"] = "0"


class NoCacheFrontendStaticFiles(StaticFiles):
    """按 index 当前精确引用为前端资源设置安全缓存策略。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._versioned_asset_queries = self._load_versioned_asset_queries()

    def _load_versioned_asset_queries(
        self,
    ) -> dict[str, set[tuple[str, str]]]:
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
        if (
            str(scope.get("path") or "").startswith("/api/")
            and scope.get("method") not in {"GET", "HEAD"}
        ):
            return Response(status_code=404)
        normalized_path = path.removeprefix("./").lstrip("/")
        query_string = scope.get("query_string", b"").decode(
            "latin-1",
            errors="strict",
        )
        version_token = _single_unambiguous_version_token(query_string)
        has_current_version_token = (
            Path(normalized_path).suffix.casefold() in {".css", ".js"}
            and version_token is not None
            and version_token
            in self._versioned_asset_queries.get(normalized_path, set())
        )
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as error:
            error.headers = dict(error.headers or {})
            _apply_revalidation_headers(
                error.headers,
                UNVERSIONED_ASSET_CACHE_CONTROL,
            )
            raise

        content_type = response.headers.get("Content-Type", "").casefold()
        is_immutable_asset = (
            has_current_version_token
            and response.status_code in {200, 206, 304}
            and not content_type.startswith("text/html")
        )
        if is_immutable_asset:
            response.headers["Cache-Control"] = VERSIONED_ASSET_CACHE_CONTROL
            for header_name in ("Pragma", "Expires"):
                if header_name in response.headers:
                    del response.headers[header_name]
        else:
            cache_control = (
                INDEX_CACHE_CONTROL
                if normalized_path in {"", ".", "index.html"}
                else UNVERSIONED_ASSET_CACHE_CONTROL
            )
            _apply_revalidation_headers(response.headers, cache_control)
        return response


def _require_iso_date(value: str | None, *, field: str) -> str | None:
    """校验日期参数的形状与日历语义。

    只做形状约束时 ``\\d{2}`` 同样匹配 ``99``，也匹配 2 月的 ``31``；这类值会
    直接进入 SQL 日期比较，由 MySQL 被动承担最后的日期验证，而数据库异常又
    被呈现为 HTTP 500。形状与语义在此一并校验，使同一类输入错误只有一个
    响应通道。
    """
    if value is None:
        return None
    if not _DATE_PATTERN.fullmatch(value):
        raise HTTPException(
            status_code=400, detail=f"{field} must be formatted as YYYY-MM-DD"
        )
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"{field} is not a real calendar date"
        ) from None
    return value


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
    - 未配置、仅空白或仍为仓库占位符：禁用写接口，避免本地直连或隧道误配暴露写库入口。
    - 已配置：必须携带匹配的 X-Admin-Token，否则拒绝。
    """
    expected = os.getenv("BOND_ADMIN_TOKEN")
    if (
        not expected
        or not expected.strip()
        or expected.strip() == "__SET_REAL_TOKEN__"
    ):
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


@app.get("/api/health")
def health() -> dict:
    engine = get_engine()
    with engine.connect() as connection:
        value = connection.execute(text("SELECT 1")).scalar_one()
    if int(value) != 1:
        raise RuntimeError("database health check returned an unexpected value")
    control_plane = require_scheduled_one_shot_control_plane(
        os.getenv(
            "BOND_FACTOR_LAB_CONTROL_PLANE",
            LAUNCHD_ONE_SHOT_CONTROL_PLANE,
        )
    )
    return {
        "status": "ok",
        "daily_schedule": {
            "mode": control_plane,
            "overall": "not_enabled",
            "reasons": ["LEDGER_RETIRED"],
        },
    }


def _request_id(request: Request) -> str:
    """复用合法 edge request ID；缺失或不安全时生成本地 UUID。"""
    candidate = request.headers.get("x-request-id")
    if candidate is not None and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return uuid4().hex


def _safe_build_diagnostics(snapshot_id: str) -> dict[str, Any]:
    try:
        return dict(dashboard_build_diagnostics(snapshot_id) or {})
    except Exception:
        return {}


def _duration_ms(value: Any) -> float | None:
    seconds = _numeric_seconds(value)
    return None if seconds is None else round(seconds * 1_000, 3)


def _numeric_seconds(value: Any) -> float | None:
    """将诊断值收窄为非负有限秒数。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0.0 else None


def _integer_metric(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _actual_frequency_metrics(value: Any) -> dict[str, int | None]:
    """仅允许固定 actual 事实类型进入结构化请求日志。"""
    source = value if isinstance(value, dict) else {}
    return {
        frequency: _integer_metric(source.get(frequency))
        for frequency in ("daily", "weekly", "monthly", "period_average")
    }


def _server_timing(
    *,
    build_diagnostics: dict[str, Any],
    response_encoding_name: str,
    response_encoding_seconds: float,
    route_seconds: float,
) -> str:
    timings: list[tuple[str, float | None]] = [
        (
            "dashboard_db",
            _duration_ms(build_diagnostics.get("db_read_seconds")),
        ),
        (
            "dashboard_canonical",
            _duration_ms(
                build_diagnostics.get("canonical_build_seconds")
            ),
        ),
        (
            "dashboard_serialization",
            _duration_ms(
                build_diagnostics.get("canonical_serialization_seconds")
            ),
        ),
        (
            response_encoding_name,
            round(response_encoding_seconds * 1_000, 3),
        ),
        ("request_route", round(route_seconds * 1_000, 3)),
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
    server_timing: str,
) -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "X-Request-ID": request_id,
        "X-Dashboard-Snapshot-ID": snapshot_id,
        "Server-Timing": server_timing,
    }


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
    encoding_started_at = time.perf_counter()
    raw_body = json.dumps(
        {"error_code": error_code},
        separators=(",", ":"),
    ).encode("utf-8")
    response_encoding_seconds = time.perf_counter() - encoding_started_at
    route_seconds = time.perf_counter() - route_started_at
    headers = _dashboard_response_headers(
        request_id=request_id,
        snapshot_id="unavailable",
        server_timing=_server_timing(
            build_diagnostics={},
            response_encoding_name="request_json_encoding",
            response_encoding_seconds=response_encoding_seconds,
            route_seconds=route_seconds,
        ),
    )
    _log_dashboard_request(
        {
            "request_id": request_id,
            "snapshot_id": "unavailable",
            "dashboard_db_ms": None,
            "dashboard_canonical_ms": None,
            "dashboard_serialization_ms": None,
            "request_json_encoding_ms": round(
                response_encoding_seconds * 1_000,
                3,
            ),
            "request_route_ms": round(route_seconds * 1_000, 3),
            "scheme_count": None,
            "live_row_count": None,
            "backtest_row_count": None,
            "response_raw_bytes": len(raw_body),
            "response_budget_gzip_bytes": None,
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
        response_payload = build_factor_lab_dashboard(get_dashboard_engine())
        serialization_started_at = time.perf_counter()
        encoding = encode_canonical_snapshot(response_payload)
        response_serialization_seconds = (
            time.perf_counter() - serialization_started_at
        )
    except Exception:  # noqa: BLE001 - 公开接口只能返回稳定可恢复状态
        return _dashboard_error_response(
            request_id=request_id,
            error_code=_DASHBOARD_ERROR_UNAVAILABLE,
            status_code=503,
            route_started_at=route_started_at,
        )

    snapshot_id = str(response_payload["snapshot_id"])
    build_diagnostics = _safe_build_diagnostics(snapshot_id)
    route_seconds = time.perf_counter() - route_started_at
    server_timing = _server_timing(
        build_diagnostics=build_diagnostics,
        response_encoding_name="request_compact_json_budget_gzip",
        response_encoding_seconds=response_serialization_seconds,
        route_seconds=route_seconds,
    )
    headers = _dashboard_response_headers(
        request_id=request_id,
        snapshot_id=snapshot_id,
        server_timing=server_timing,
    )
    _log_dashboard_request(
        {
            "request_id": request_id,
            "snapshot_id": snapshot_id,
            "dashboard_db_ms": _duration_ms(
                build_diagnostics.get("db_read_seconds")
            ),
            "dashboard_canonical_ms": _duration_ms(
                build_diagnostics.get("canonical_build_seconds")
            ),
            "dashboard_serialization_ms": _duration_ms(
                build_diagnostics.get("canonical_serialization_seconds")
            ),
            "request_compact_json_budget_gzip_ms": round(
                response_serialization_seconds * 1_000,
                3,
            ),
            "request_route_ms": round(route_seconds * 1_000, 3),
            "scheme_count": _integer_metric(
                build_diagnostics.get("scheme_count")
            ),
            "live_row_count": _integer_metric(
                build_diagnostics.get("live_row_count")
            ),
            "backtest_row_count": _integer_metric(
                build_diagnostics.get("backtest_row_count")
            ),
            "actual_same_direction_duplicates_folded": (
                _actual_frequency_metrics(
                    build_diagnostics.get(
                        "actual_same_direction_duplicates_folded"
                    )
                )
            ),
            "actual_direction_conflicts": _actual_frequency_metrics(
                build_diagnostics.get("actual_direction_conflicts")
            ),
            "dashboard_raw_bytes": _integer_metric(
                build_diagnostics.get("raw_bytes")
            ),
            "dashboard_budget_gzip_bytes": _integer_metric(
                build_diagnostics.get("gzip_bytes")
            ),
            "response_raw_bytes": encoding.raw_size,
            "response_budget_gzip_bytes": encoding.gzip_size,
            "status": 200,
            "error_code": None,
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
    for field, value in (("start_month", start_month), ("end_month", end_month)):
        if value and not _MONTH_PATTERN.fullmatch(value):
            raise HTTPException(
                status_code=400,
                detail=f"{field} must be formatted as YYYY-MM",
            )
    if start_month and end_month and end_month < start_month:
        raise HTTPException(status_code=400, detail="end_month must be greater than or equal to start_month")
    try:
        return scheme_metrics(get_engine(), scheme_id, start_month=start_month, end_month=end_month)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/predictions")
def api_predictions(
    scheme_id: str,
    response: Response,
    tenor: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Prediction-Visibility"] = "uncached-db"
    if tenor is not None:
        raise HTTPException(status_code=400, detail="tenor query is not supported; use registry scheme_id")
    start_date = _require_iso_date(start_date, field="start_date")
    end_date = _require_iso_date(end_date, field="end_date")
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
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    start_date = _require_iso_date(start_date, field="start_date")
    end_date = _require_iso_date(end_date, field="end_date")
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


@app.post("/api/admin/registry/sync", dependencies=[Depends(require_admin_token)])
def api_admin_registry_sync() -> dict:
    """受保护的管理端点：显式把 schemes/ 配置同步到 registry（写库）。"""
    synced = sync_registry_from_configs(get_engine())
    return {"synced": synced}


if FRONTEND_ROOT.exists():
    app.mount("/", NoCacheFrontendStaticFiles(directory=FRONTEND_ROOT, html=True), name="frontend")
