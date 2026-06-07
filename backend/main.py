from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.db import get_engine
from backend.services import (
    backtest_diffs,
    backtest_factor_lab_results,
    backtest_metrics,
    get_backtest_run,
    list_actuals,
    list_backtest_runs,
    list_data_checks,
    list_predictions,
    list_schemes,
    list_targets,
    scheme_metrics,
)
from scheduler.executor import DEFAULT_ALGO_ENV
from scheduler.main import run_prediction_job


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
logger = logging.getLogger(__name__)

app = FastAPI(title="Bond Factor Lab API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TriggerRequest(BaseModel):
    """手动触发预测请求。"""

    predict_date: str | None = Field(default=None, description="YYYY-MM-DD; omitted means today")
    force: bool = Field(default=False, description="Run even when the date is not a trading day")
    algo_env: str | None = Field(default=None, description="Override algorithm conda env")


@app.get("/api/health")
def health() -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1")).scalar_one()
    return {"status": "ok"}


@app.get("/api/schemes")
def api_schemes() -> dict:
    engine = get_engine()
    targets = list_targets(engine)
    return {
        "targets": targets,
        "target_labels": {
            item["target_code"]: item["display_name"]
            for item in targets
            if item["status"] == "active"
        },
        "schemes": list_schemes(engine),
    }


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
    tenor: str = Query(..., description="Target tenor, e.g. 10Y"),
    start_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    end_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
) -> dict:
    if start_month and end_month and end_month < start_month:
        raise HTTPException(status_code=400, detail="end_month must be greater than or equal to start_month")
    return scheme_metrics(get_engine(), scheme_id, tenor, start_month=start_month, end_month=end_month)


@app.get("/api/predictions")
def api_predictions(
    scheme_id: str | None = None,
    tenor: str | None = None,
    start_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return list_predictions(
        get_engine(),
        scheme_id=scheme_id,
        tenor=tenor,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )


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


@app.get("/api/backtests/runs/{run_id}/metrics")
def api_backtest_metrics(run_id: int) -> dict:
    result = backtest_metrics(get_engine(), run_id)
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
    benchmark_id: str = "model_muti_0529",
    data_source: str = "framework_db_aligned",
) -> dict:
    return backtest_factor_lab_results(get_engine(), benchmark_id=benchmark_id, data_source=data_source)


def _run_trigger(scheme_id: str, request: TriggerRequest) -> None:
    try:
        run_prediction_job(
            scheme_id,
            run_date=request.predict_date,
            algo_env=request.algo_env or os.getenv("BOND_ALGO_CONDA_ENV", DEFAULT_ALGO_ENV),
            force=request.force,
        )
    except Exception:
        logger.exception("Manual trigger failed for %s", scheme_id)


@app.post("/api/schemes/{scheme_id}/trigger", status_code=202)
def api_trigger_scheme(scheme_id: str, request: TriggerRequest, background_tasks: BackgroundTasks) -> dict:
    known = {item["scheme_id"] for item in list_schemes(get_engine())}
    if scheme_id not in known:
        raise HTTPException(status_code=404, detail=f"scheme not found: {scheme_id}")
    background_tasks.add_task(_run_trigger, scheme_id, request)
    return {"accepted": True, "scheme_id": scheme_id, "predict_date": request.predict_date, "force": request.force}


if FRONTEND_ROOT.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_ROOT, html=True), name="frontend")
