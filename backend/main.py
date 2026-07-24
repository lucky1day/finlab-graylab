from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import time
from datetime import date, datetime, time as wall_time, timezone
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Mapping
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

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
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.db import get_dashboard_engine, get_engine
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
from scheduler.daily_health import (
    project_daily_health,
    project_scheduler_heartbeat,
)
from scheduler.main import (
    _daily_coordinator_mode,
    run_daily_operator_recovery_job,
    run_prediction_job,
)
from scheduler.repository import (
    find_schedule_occurrence_id,
    read_dashboard_source_generation,
    read_schedule_api_visibility_probe,
    read_schedule_execution_envelope,
    read_schedule_health_envelope,
    read_schedule_occurrence_snapshot,
    read_scheduler_heartbeat,
)
from shared.service_instance import (
    FINGERPRINT_VERSION,
    build_service_instance_identity,
    service_fingerprint_secret,
)
from shared.calendar_service import CalendarService
from shared.daily_coordinator_mode import (
    assert_daily_coordinator_epoch_matches_policy,
    require_current_daily_coordinator_identity,
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
_DAILY_HEARTBEAT_SERVICE = "daily-coordinator"
_DAILY_SCHEDULE_KEY = "critical-daily-signals-v1"
_DAILY_TIMEZONE = ZoneInfo("Asia/Shanghai")
_DAILY_NOT_BEFORE = wall_time(6, 30)
_dashboard_health_lock = Lock()
_dashboard_health: dict[str, str | None] = {
    "status": "degraded",
    "error_code": _DASHBOARD_ERROR_NOT_PREWARMED,
}
_dashboard_health_revision = 0


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


def _build_dashboard_snapshot() -> dict[str, Any]:
    """延迟取得 DB engine；模块 import 不连接数据库。"""
    return build_factor_lab_dashboard(get_dashboard_engine())


def _dashboard_source_generation() -> str:
    """ledger 模式以数据库 receipt 代次跨进程失效 dashboard。"""
    if _daily_coordinator_mode() != "ledger":
        return "legacy-ttl-only"
    return read_dashboard_source_generation(
        get_dashboard_engine(),
        schedule_key=_DAILY_SCHEDULE_KEY,
    )


dashboard_snapshot_store = DashboardSnapshotStore(
    _build_dashboard_snapshot,
    source_generation=_dashboard_source_generation,
)


def _set_dashboard_health(
    status: Literal["ready", "degraded"],
    error_code: str | None,
    *,
    observation_revision: int | None = None,
) -> bool:
    """原子更新 health 中不含内部异常文本的 dashboard 子状态。"""
    global _dashboard_health_revision
    if status == "ready":
        error_code = None
    with _dashboard_health_lock:
        if observation_revision is None:
            return False
        if observation_revision < _dashboard_health_revision:
            return False
        _dashboard_health_revision = observation_revision
        _dashboard_health["status"] = status
        _dashboard_health["error_code"] = error_code
        return True


def _reset_dashboard_health_for_tests(
    status: Literal["ready", "degraded"],
    error_code: str | None,
    *,
    observation_revision: int = 0,
) -> None:
    """测试专用：显式重置 health 状态及其观察序号。"""
    global _dashboard_health_revision
    if status == "ready":
        error_code = None
    with _dashboard_health_lock:
        _dashboard_health_revision = observation_revision
        _dashboard_health["status"] = status
        _dashboard_health["error_code"] = error_code


def _dashboard_health_snapshot() -> dict[str, str | None]:
    """返回 dashboard health 状态副本。"""
    with _dashboard_health_lock:
        return dict(_dashboard_health)


def _record_dashboard_prewarm_failure(
    observation_revision: int | None,
) -> None:
    """记录不含内部异常文本的稳定预热失败状态。"""
    _set_dashboard_health(
        "degraded",
        _DASHBOARD_ERROR_UNAVAILABLE,
        observation_revision=observation_revision,
    )
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
    """legacy 启动时同步 registry；ledger 模式仅预热只读快照。"""
    if _daily_coordinator_mode() != "ledger":
        try:
            sync_registry_from_configs(get_engine())
        except Exception:
            logger.error("Registry sync on startup failed")

    try:
        result = dashboard_snapshot_store.prewarm()
    except (SnapshotUnavailable, DashboardDataError):
        request_diagnostics = _safe_request_diagnostics()
        _record_dashboard_prewarm_failure(
            _observation_revision(request_diagnostics)
        )
    except Exception:
        request_diagnostics = _safe_request_diagnostics()
        _record_dashboard_prewarm_failure(
            _observation_revision(request_diagnostics)
        )
    else:
        request_diagnostics = _safe_request_diagnostics()
        observation_revision = _observation_revision(request_diagnostics)
        if result.cache_status == "STALE":
            _set_dashboard_health(
                "degraded",
                _DASHBOARD_ERROR_STALE,
                observation_revision=observation_revision,
            )
        else:
            _set_dashboard_health(
                "ready",
                None,
                observation_revision=observation_revision,
            )


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
    daily_schedule = _daily_schedule_health(engine)
    daily_overall = daily_schedule.get("overall")
    return {
        "status": (
            "ok"
            if daily_overall in {"ok", "not_enabled"}
            else "error"
        ),
        "service_instance": identity,
        "dashboard_snapshot": _dashboard_health_snapshot(),
        "daily_schedule": daily_schedule,
    }


@app.get("/api/daily-schedule/visibility")
def api_daily_schedule_visibility(response: Response) -> dict[str, object]:
    """返回 fresh/no-store 的精确 target API 可见性探针。

    DB ``visible_at`` 只作为 commit receipt 返回；调用方实际收到本
    HTTP 响应的时刻，才构成外部 API visibility 观察证据。
    """
    try:
        current_identity = (
            require_current_daily_coordinator_identity()
        )
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="daily coordinator epoch is unavailable",
        )
    if current_identity.mode != "ledger":
        raise HTTPException(
            status_code=409,
            detail="daily schedule visibility requires ledger mode",
        )
    engine = get_engine()
    heartbeat = read_scheduler_heartbeat(
        engine,
        service_name=_DAILY_HEARTBEAT_SERVICE,
    )
    if heartbeat is None or heartbeat.occurrence_id is None:
        raise HTTPException(
            status_code=503,
            detail="current daily occurrence is unavailable",
        )
    heartbeat_details = getattr(heartbeat, "details", None)
    if (
        not isinstance(heartbeat_details, Mapping)
        or heartbeat_details.get("coordinator_mode") != "ledger"
        or not _coordinator_epoch_matches(
            current_identity,
            heartbeat_details.get("daily_coordinator_epoch"),
        )
    ):
        raise HTTPException(
            status_code=503,
            detail="daily coordinator heartbeat mode is unavailable",
        )
    snapshot = read_schedule_occurrence_snapshot(
        engine,
        occurrence_id=heartbeat.occurrence_id,
    )
    occurrence_policy = getattr(
        snapshot.occurrence,
        "policy_json",
        None,
    )
    if (
        not isinstance(occurrence_policy, Mapping)
        or not _coordinator_epoch_matches(
            current_identity,
            occurrence_policy.get("daily_coordinator_epoch"),
        )
        or occurrence_policy.get("daily_coordinator_epoch")
        != heartbeat_details.get("daily_coordinator_epoch")
    ):
        raise HTTPException(
            status_code=503,
            detail="daily schedule coordinator epoch mismatch",
        )
    probe = read_schedule_api_visibility_probe(
        engine,
        occurrence_id=heartbeat.occurrence_id,
    )
    linked = set(probe.linked_registry_ids)
    db_visible = set(probe.db_visible_registry_ids)
    api_ready_ids = sorted(linked & db_visible)
    observed_at = probe.observed_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    else:
        observed_at = observed_at.astimezone(timezone.utc)
    occurrence = snapshot.occurrence
    predict_date = str(occurrence.predict_date)
    current_business_date = observed_at.astimezone(
        _DAILY_TIMEZONE
    ).date().isoformat()
    heartbeat_projection = project_scheduler_heartbeat(
        heartbeat,
        expected_occurrence_id=occurrence.occurrence_id,
        now=observed_at,
    )
    if heartbeat_projection["status"] in {"missing", "stale", "error"}:
        raise HTTPException(
            status_code=503,
            detail="daily coordinator heartbeat is not current",
        )
    if (
        int(probe.occurrence_id) != int(occurrence.occurrence_id)
        or predict_date != current_business_date
        or heartbeat_details.get("business_date") != predict_date
    ):
        raise HTTPException(
            status_code=503,
            detail="daily schedule occurrence is not current",
        )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Prediction-Visibility"] = (
        "uncached-db-probe"
    )
    return {
        "probe_completed": True,
        "occurrence_id": probe.occurrence_id,
        "predict_date": predict_date,
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "expected": probe.expected_target_count,
        "committed": len(probe.committed_registry_ids),
        "linked": len(probe.linked_registry_ids),
        "db_visible": len(probe.db_visible_registry_ids),
        "api_ready": len(api_ready_ids),
        "missing": max(
            probe.expected_target_count - len(api_ready_ids),
            0,
        ),
        "api_ready_registry_ids": api_ready_ids,
        "missing_registry_ids": list(probe.missing_registry_ids),
        "receipt_missing_registry_ids": list(
            probe.receipt_missing_registry_ids
        ),
        "source_generation": probe.source_generation,
        "db_receipt_semantics": "committed_prediction_db_visible",
        "api_receipt_semantics": (
            "caller_must_receive_this_uncached_response"
        ),
    }


def _daily_schedule_health(engine) -> dict[str, object]:
    """从 ledger 只读投影日批健康；legacy rollout 不触碰新表。"""
    try:
        current_identity = (
            require_current_daily_coordinator_identity()
        )
        mode = current_identity.mode
    except Exception:
        return {
            "mode": "unknown",
            "overall": "error",
            "reasons": ["COORDINATOR_EPOCH_UNAVAILABLE"],
        }
    if mode != "ledger":
        return {
            "mode": mode,
            "overall": "not_enabled",
            "reasons": ["LEDGER_ROLLOUT_DISABLED"],
        }
    try:
        checked_at = datetime.now(timezone.utc)
        local_now = checked_at.astimezone(_DAILY_TIMEZONE)
        heartbeat = read_scheduler_heartbeat(
            engine,
            service_name=_DAILY_HEARTBEAT_SERVICE,
        )
        if heartbeat is None:
            return {
                "mode": "ledger",
                "overall": "error",
                "reasons": ["HEARTBEAT_MISSING"],
            }
        heartbeat_details = getattr(heartbeat, "details", {})
        heartbeat_mode = (
            heartbeat_details.get("coordinator_mode")
            if isinstance(heartbeat_details, Mapping)
            else None
        )
        if heartbeat_mode != "ledger":
            return {
                "mode": "ledger",
                "overall": "error",
                "reasons": ["MODE_MISMATCH"],
                "scheduler_heartbeat": project_scheduler_heartbeat(
                    heartbeat,
                    expected_occurrence_id=heartbeat.occurrence_id,
                    now=checked_at,
                ),
            }
        if not _coordinator_epoch_matches(
            current_identity,
            heartbeat_details.get("daily_coordinator_epoch"),
        ):
            return {
                "mode": "ledger",
                "overall": "error",
                "reasons": ["COORDINATOR_EPOCH_MISMATCH"],
            }
        if heartbeat.occurrence_id is None:
            heartbeat_projection = project_scheduler_heartbeat(
                heartbeat,
                expected_occurrence_id=None,
                now=checked_at,
            )
            reasons: list[str] = []
            heartbeat_status = heartbeat_projection["status"]
            if heartbeat_status == "stale":
                reasons.append("HEARTBEAT_STALE")
            elif heartbeat_status == "error":
                reasons.append("HEARTBEAT_ERROR")
            idle = str(heartbeat.state).upper() == "IDLE"
            if not idle:
                reasons.append("OCCURRENCE_MISSING")
            if (
                local_now.time().replace(tzinfo=None)
                >= _DAILY_NOT_BEFORE
                and CalendarService(engine).is_trading_day(
                    local_now.date()
                )
                and "OCCURRENCE_MISSING" not in reasons
            ):
                reasons.append("OCCURRENCE_MISSING")
            return {
                "mode": "ledger",
                "overall": "error" if reasons else "ok",
                "reasons": reasons,
                "scheduler_heartbeat": heartbeat_projection,
            }
        snapshot = read_schedule_occurrence_snapshot(
            engine,
            occurrence_id=heartbeat.occurrence_id,
        )
        occurrence_policy = getattr(
            snapshot.occurrence,
            "policy_json",
            None,
        )
        if (
            not isinstance(occurrence_policy, Mapping)
            or not _coordinator_epoch_matches(
                current_identity,
                occurrence_policy.get("daily_coordinator_epoch"),
            )
            or occurrence_policy.get("daily_coordinator_epoch")
            != heartbeat_details.get("daily_coordinator_epoch")
        ):
            return {
                "mode": "ledger",
                "overall": "error",
                "reasons": ["COORDINATOR_EPOCH_MISMATCH"],
            }
        envelopes = tuple(
            read_schedule_health_envelope(
                engine,
                item_id=summary.item.item_id,
            )
            for summary in snapshot.items
        )
        projection = project_daily_health(
            snapshot,
            heartbeat,
            execution_envelopes=envelopes,
            now=checked_at,
        )
        snapshot_occurrence = getattr(snapshot, "occurrence", None)
        if (
            snapshot_occurrence is not None
            and snapshot_occurrence.predict_date
            != local_now.date().isoformat()
        ):
            reasons = list(projection.get("reasons") or [])
            if "OCCURRENCE_DATE_MISMATCH" not in reasons:
                reasons.append("OCCURRENCE_DATE_MISMATCH")
            projection = {
                **projection,
                "overall": "error",
                "reasons": reasons,
            }
        return {"mode": "ledger", **projection}
    except Exception as exc:
        logger.error(
            "Daily ledger health projection failed: error_type=%s",
            type(exc).__name__,
        )
        return {
            "mode": "ledger",
            "overall": "error",
            "reasons": ["LEDGER_HEALTH_UNAVAILABLE"],
        }


def _coordinator_epoch_matches(
    current_identity: object,
    frozen: object,
) -> bool:
    """不抛异常地比较 exact epoch capability。"""
    if not isinstance(frozen, Mapping):
        return False
    try:
        expected = current_identity.policy_payload()
    except Exception:
        return False
    return (
        set(frozen) == {"epoch", "mode", "record_sha256"}
        and dict(frozen) == expected
    )


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


def _safe_request_diagnostics() -> dict[str, Any]:
    try:
        return dict(dashboard_snapshot_store.request_diagnostics())
    except Exception:
        return {}


def _observation_revision(diagnostics: dict[str, Any]) -> int | None:
    return _integer_metric(diagnostics.get("observation_revision"))


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


def _attempt_status(value: Any) -> str | None:
    """仅记录稳定的刷新尝试状态枚举。"""
    return value if value in {"success", "failed", "timeout"} else None


def _integer_metric(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _actual_frequency_metrics(value: Any) -> dict[str, int | None]:
    """仅允许三类固定 actual 频率进入结构化请求日志。"""
    source = value if isinstance(value, dict) else {}
    return {
        frequency: _integer_metric(source.get(frequency))
        for frequency in ("daily", "weekly", "monthly")
    }


def _server_timing(
    *,
    build_diagnostics: dict[str, Any],
    snapshot_origin_build_seconds: float | None,
    associated_attempt_seconds: float | None,
    response_encoding_name: str,
    response_encoding_seconds: float,
    route_seconds: float,
) -> str:
    timings: list[tuple[str, float | None]] = [
        (
            "snapshot_origin_db",
            _duration_ms(build_diagnostics.get("db_read_seconds")),
        ),
        (
            "snapshot_origin_canonical",
            _duration_ms(
                build_diagnostics.get("canonical_build_seconds")
            ),
        ),
        (
            "snapshot_origin_serialization",
            _duration_ms(
                build_diagnostics.get("canonical_serialization_seconds")
            ),
        ),
        (
            "snapshot_origin_build",
            _duration_ms(snapshot_origin_build_seconds),
        ),
        (
            "associated_refresh_attempt",
            _duration_ms(associated_attempt_seconds),
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
    cache_status: str,
    age_ms: int,
    server_timing: str,
    stale: bool,
    source_generation: str | None = None,
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
    if source_generation is not None:
        headers["X-Dashboard-Source-Generation"] = source_generation
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
    request_diagnostics: dict[str, Any] | None = None,
    store_diagnostics: dict[str, Any] | None = None,
) -> Response:
    encoding_started_at = time.perf_counter()
    raw_body = json.dumps(
        {"error_code": error_code},
        separators=(",", ":"),
    ).encode("utf-8")
    response_encoding_seconds = time.perf_counter() - encoding_started_at
    route_seconds = time.perf_counter() - route_started_at
    request_diagnostics = request_diagnostics or {}
    store_diagnostics = store_diagnostics or {}
    active_waiter_count = _integer_metric(
        store_diagnostics.get("active_waiter_count")
    )
    build_waiter_count = _integer_metric(
        request_diagnostics.get("build_waiter_count")
    )
    associated_attempt_id = _integer_metric(
        request_diagnostics.get("attempt_id")
    )
    associated_attempt_seconds = _numeric_seconds(
        request_diagnostics.get("attempt_seconds")
    )
    headers = _dashboard_response_headers(
        request_id=request_id,
        snapshot_id="unavailable",
        cache_status="UNAVAILABLE",
        age_ms=0,
        server_timing=_server_timing(
            build_diagnostics={},
            snapshot_origin_build_seconds=None,
            associated_attempt_seconds=associated_attempt_seconds,
            response_encoding_name="request_json_encoding",
            response_encoding_seconds=response_encoding_seconds,
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
            "active_waiter_count": (
                0 if active_waiter_count is None else active_waiter_count
            ),
            "request_waited": (
                request_diagnostics.get("request_waited") is True
            ),
            "build_waiter_count": (
                0 if build_waiter_count is None else build_waiter_count
            ),
            "associated_refresh_attempt_id": associated_attempt_id,
            "associated_refresh_attempt_status": _attempt_status(
                request_diagnostics.get("attempt_status")
            ),
            "associated_refresh_attempt_ms": _duration_ms(
                associated_attempt_seconds
            ),
            "snapshot_origin_db_ms": None,
            "snapshot_origin_canonical_ms": None,
            "snapshot_origin_serialization_ms": None,
            "snapshot_origin_build_ms": None,
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
        result = dashboard_snapshot_store.get()
        request_diagnostics = _safe_request_diagnostics()
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
        request_diagnostics = _safe_request_diagnostics()
        _set_dashboard_health(
            "degraded",
            _DASHBOARD_ERROR_UNAVAILABLE,
            observation_revision=_observation_revision(
                request_diagnostics
            ),
        )
        return _dashboard_error_response(
            request_id=request_id,
            error_code=_DASHBOARD_ERROR_UNAVAILABLE,
            status_code=503,
            route_started_at=route_started_at,
            request_diagnostics=request_diagnostics,
            store_diagnostics=_safe_store_diagnostics(),
        )

    snapshot_id = str(response_payload["snapshot_id"])
    build_diagnostics = _safe_build_diagnostics(snapshot_id)
    store_diagnostics = _safe_store_diagnostics()
    snapshot_origin_build_seconds = result.build_seconds
    if snapshot_origin_build_seconds is None:
        snapshot_origin_build_seconds = _numeric_seconds(
            store_diagnostics.get("snapshot_origin_build_seconds")
        )
    if snapshot_origin_build_seconds is None:
        snapshot_origin_build_seconds = _numeric_seconds(
            store_diagnostics.get("last_build_seconds")
        )
    active_waiter_count = _integer_metric(
        store_diagnostics.get("active_waiter_count")
    )
    build_waiter_count = _integer_metric(
        request_diagnostics.get("build_waiter_count")
    )
    associated_attempt_id = _integer_metric(
        request_diagnostics.get("attempt_id")
    )
    associated_attempt_seconds = _numeric_seconds(
        request_diagnostics.get("attempt_seconds")
    )
    route_seconds = time.perf_counter() - route_started_at
    server_timing = _server_timing(
        build_diagnostics=build_diagnostics,
        snapshot_origin_build_seconds=snapshot_origin_build_seconds,
        associated_attempt_seconds=associated_attempt_seconds,
        response_encoding_name="request_compact_json_budget_gzip",
        response_encoding_seconds=response_serialization_seconds,
        route_seconds=route_seconds,
    )
    headers = _dashboard_response_headers(
        request_id=request_id,
        snapshot_id=snapshot_id,
        cache_status=result.cache_status,
        age_ms=age_ms,
        server_timing=server_timing,
        stale=is_stale,
        source_generation=result.source_generation,
    )
    if is_stale:
        _set_dashboard_health(
            "degraded",
            _DASHBOARD_ERROR_STALE,
            observation_revision=_observation_revision(
                request_diagnostics
            ),
        )
    else:
        _set_dashboard_health(
            "ready",
            None,
            observation_revision=_observation_revision(
                request_diagnostics
            ),
        )
    _log_dashboard_request(
        {
            "request_id": request_id,
            "snapshot_id": snapshot_id,
            "cache": result.cache_status,
            "snapshot_age_ms": age_ms,
            "active_waiter_count": (
                0 if active_waiter_count is None else active_waiter_count
            ),
            "request_waited": (
                request_diagnostics.get("request_waited") is True
            ),
            "build_waiter_count": (
                0 if build_waiter_count is None else build_waiter_count
            ),
            "associated_refresh_attempt_id": associated_attempt_id,
            "associated_refresh_attempt_status": _attempt_status(
                request_diagnostics.get("attempt_status")
            ),
            "associated_refresh_attempt_ms": _duration_ms(
                associated_attempt_seconds
            ),
            "snapshot_origin_db_ms": _duration_ms(
                build_diagnostics.get("db_read_seconds")
            ),
            "snapshot_origin_canonical_ms": _duration_ms(
                build_diagnostics.get("canonical_build_seconds")
            ),
            "snapshot_origin_serialization_ms": _duration_ms(
                build_diagnostics.get("canonical_serialization_seconds")
            ),
            "request_compact_json_budget_gzip_ms": round(
                response_serialization_seconds * 1_000,
                3,
            ),
            "snapshot_origin_build_ms": _duration_ms(
                snapshot_origin_build_seconds
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
            "snapshot_origin_raw_bytes": _integer_metric(
                build_diagnostics.get("raw_bytes")
            ),
            "snapshot_origin_budget_gzip_bytes": _integer_metric(
                build_diagnostics.get("gzip_bytes")
            ),
            "response_raw_bytes": encoding.raw_size,
            "response_budget_gzip_bytes": encoding.gzip_size,
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
    response: Response,
    tenor: str | None = None,
    start_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Prediction-Visibility"] = "uncached-db"
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


def _run_trigger(
    scheme_id: str,
    request: TriggerRequest,
    frequency: str | None = None,
) -> None:
    logger.info("Manual trigger started for %s (triggered_by=admin_api)", scheme_id)
    try:
        algo_env = request.algo_env or os.getenv(
            "BOND_ALGO_CONDA_ENV",
            DEFAULT_ALGO_ENV,
        )
        if _daily_coordinator_mode() == "ledger" and frequency in {
            None,
            "daily",
        }:
            run_daily_operator_recovery_job(
                scheme_id,
                run_date=request.predict_date,
                algo_env=algo_env,
            )
            return
        run_prediction_job(
            scheme_id,
            run_date=request.predict_date,
            algo_env=algo_env,
            force=request.force,
        )
    except Exception:
        logger.exception("Manual trigger failed for %s", scheme_id)


@app.post("/api/schemes/{scheme_id}/trigger", status_code=202, dependencies=[Depends(require_admin_token)])
def api_trigger_scheme(scheme_id: str, request: TriggerRequest, background_tasks: BackgroundTasks) -> dict:
    engine = get_engine()
    known = {
        item["scheme_id"]: (
            item["base_scheme_id"],
            item.get("frequency"),
        )
        for item in list_schemes(engine)
        if item.get("status") == "active"
    }
    identity = known.get(scheme_id)
    if identity is None:
        raise HTTPException(status_code=404, detail=f"scheme not found: {scheme_id}")
    base_scheme_id, frequency = identity
    if frequency in {None, "daily"}:
        try:
            coordinator_mode = _daily_coordinator_mode()
            current_identity = (
                require_current_daily_coordinator_identity()
            )
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="daily coordinator epoch is unavailable",
            )
        if (
            coordinator_mode == "ledger"
            and current_identity.mode != "ledger"
        ):
            raise HTTPException(
                status_code=503,
                detail="daily coordinator epoch mode mismatch",
            )
        if coordinator_mode == "ledger":
            requested_date = date.fromisoformat(
                request.predict_date
                or datetime.now(_DAILY_TIMEZONE).date().isoformat()
            )
            occurrence_id = find_schedule_occurrence_id(
                engine,
                schedule_key=_DAILY_SCHEDULE_KEY,
                predict_date=requested_date.isoformat(),
            )
            if occurrence_id is None:
                raise HTTPException(
                    status_code=503,
                    detail="daily schedule occurrence is unavailable",
                )
            else:
                try:
                    snapshot = read_schedule_occurrence_snapshot(
                        engine,
                        occurrence_id=occurrence_id,
                    )
                    frozen_identity = (
                        assert_daily_coordinator_epoch_matches_policy(
                            snapshot.occurrence.policy_json
                        )
                    )
                except Exception:
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "daily schedule coordinator epoch mismatch"
                        ),
                    )
                if (
                    frozen_identity.policy_payload()
                    != current_identity.policy_payload()
                ):
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "daily schedule coordinator epoch mismatch"
                        ),
                    )
    background_tasks.add_task(
        _run_trigger,
        base_scheme_id,
        request,
        frequency,
    )
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
    if _daily_coordinator_mode() == "ledger":
        raise HTTPException(
            status_code=409,
            detail=(
                "registry sync is disabled while the ledger coordinator "
                "is active; change Registry only through the pre-cutover "
                "authorized workflow"
            ),
        )
    synced = sync_registry_from_configs(get_engine(), force=True)
    return {"synced": synced}


if FRONTEND_ROOT.exists():
    app.mount("/", NoCacheFrontendStaticFiles(directory=FRONTEND_ROOT, html=True), name="frontend")
