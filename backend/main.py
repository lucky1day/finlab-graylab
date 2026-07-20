from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.db import get_engine
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
FRONTEND_CACHE_CONTROL = "no-store, no-cache, must-revalidate, max-age=0"
logger = logging.getLogger(__name__)
_DEFAULT_INSTANCE_NONCE = secrets.token_hex(32)


class NoCacheFrontendStaticFiles(StaticFiles):
    """前端静态资源统一禁用浏览器缓存，避免 iframe 内继续展示旧资源。"""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = FRONTEND_CACHE_CONTROL
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


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


@app.on_event("startup")
def _sync_registry_on_startup() -> None:
    """启动时同步一次 registry（写库），使读路径可纯读。"""
    try:
        sync_registry_from_configs(get_engine())
    except Exception:
        logger.exception("Registry sync on startup failed")


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
    return {"status": "ok", "service_instance": identity}


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
