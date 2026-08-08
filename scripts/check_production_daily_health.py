#!/usr/bin/env python
"""只读检查生产日频预测与 actual 水位健康状态。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Mapping, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.repository import create_engine_from_env  # noqa: E402
from scheduler.v2_daily_gate import (  # noqa: E402
    V2DailyGateBlocked,
    load_gate_record,
)
from shared.calendar_service import get_calendar  # noqa: E402
from shared.data_bridge.refresh import (  # noqa: E402
    DataBridgeRefreshConfig,
    DataBridgeStore,
    check_current_dataset,
)
from shared.tenor_mapping import TENOR_TO_INDICATOR, indicator_for_tenor, normalize_tenor  # noqa: E402


FindingLevel = Literal["warning", "error"]
HealthStatus = Literal["ok", "warning", "error"]


@dataclass(frozen=True)
class ActualWatermark:
    tenor: str
    actual_max: str | None
    source_max: str | None


@dataclass(frozen=True)
class RunPredictionCount:
    run_id: int
    scheme_id: str
    records_written: int | None
    prediction_rows: int


@dataclass(frozen=True)
class PredictionDateCheck:
    prediction_id: int
    scheme_id: str
    target_tenor: str
    horizon: int
    predict_date: str
    feature_date: str | None
    target_date: str
    expected_feature_date: str | None
    expected_target_date: str | None


@dataclass(frozen=True)
class DailyHealthSnapshot:
    predict_date: str
    expected_feature_date: str | None
    is_trading_day: bool
    active_daily_base_schemes: tuple[str, ...]
    successful_daily_run_schemes: tuple[str, ...]
    predictions_count: int
    run_prediction_counts: tuple[RunPredictionCount, ...]
    prediction_date_checks: tuple[PredictionDateCheck, ...]
    actual_watermarks: tuple[ActualWatermark, ...]


@dataclass(frozen=True)
class DataBridgeHealthSnapshot:
    required_refresh_date: str
    current_refresh_date: str | None
    generation_id: str | None
    refreshed_at: str | None
    business_digest: str | None
    files: Mapping[str, object]
    last_attempt: Mapping[str, object] | None
    validation_error: str | None


@dataclass(frozen=True)
class V2SchedulerGateSnapshot:
    run_date: str
    status: str | None
    generation_id: str | None
    current_generation_id: str | None
    restart_verified: bool
    error: str | None


@dataclass(frozen=True)
class HealthFinding:
    level: FindingLevel
    code: str
    message: str
    detail: dict[str, object]


SCHEME_REQUIRED_SOURCE_TENORS: dict[str, tuple[str, ...]] = {
    "daily_10y_lgbm_10y04_0629": ("1Y", "5Y", "10Y"),
    "daily_1y_xgb_1y13_0629": ("1Y", "5Y", "10Y"),
    "daily_5y_lgbm_5y10_0629": ("1Y", "5Y", "10Y"),
    "daily_5y_2_v28": ("5Y",),
    "daily_7y_1_v28": ("7Y",),
    "liwei_0616_10y01_cons_say_k3_div_k10": ("1Y", "3Y", "5Y", "7Y", "10Y"),
    "liwei_0616_10y02_cons_say_k3_div_k5": ("1Y", "3Y", "5Y", "7Y", "10Y"),
    "liwei_0616_7y01_cons_say_k3_div_k10": ("1Y", "3Y", "5Y", "7Y", "10Y"),
    "liwei_0616_7y03_cons_all_k3_div_k8": ("1Y", "3Y", "5Y", "7Y", "10Y"),
    "liwei_0616_cons_sda_k3_div_k10": ("1Y", "3Y", "5Y", "7Y", "10Y"),
    "t1_daily": ("5Y", "10Y"),
    "t5_daily": ("3Y", "5Y", "7Y", "10Y"),
}


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())[:10]
    text_value = str(value).strip()
    return text_value or None


def status_from_findings(findings: Sequence[HealthFinding]) -> HealthStatus:
    if any(item.level == "error" for item in findings):
        return "error"
    if any(item.level == "warning" for item in findings):
        return "warning"
    return "ok"


def evaluate_data_bridge_health(
    snapshot: DataBridgeHealthSnapshot,
    *,
    now: datetime | None = None,
    deadline: str = "07:00",
) -> list[HealthFinding]:
    run_now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if run_now.tzinfo is None:
        run_now = run_now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    deadline_clock = datetime.strptime(deadline, "%H:%M").time()
    deadline_at = datetime.combine(
        date.fromisoformat(snapshot.required_refresh_date),
        deadline_clock,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    if (
        snapshot.validation_error is None
        and snapshot.current_refresh_date == snapshot.required_refresh_date
    ):
        return []
    level: FindingLevel = "error" if run_now >= deadline_at else "warning"
    return [
        HealthFinding(
            level=level,
            code="data_bridge_refresh_stale",
            message="DataBridge current files are stale or invalid",
            detail=asdict(snapshot),
        )
    ]


def evaluate_v2_scheduler_gate(
    snapshot: V2SchedulerGateSnapshot,
) -> list[HealthFinding]:
    """独立评估 Blackbox V2 日级凭证，不改变 Native V1 健康口径。"""
    if snapshot.status is None:
        code = "v2_daily_gate_missing"
    elif snapshot.status != "ready":
        code = "v2_daily_gate_blocked"
    elif snapshot.generation_id != snapshot.current_generation_id:
        code = "v2_daily_gate_generation_mismatch"
    else:
        return []
    return [
        HealthFinding(
            level="error",
            code=code,
            message=f"Blackbox V2 scheduler gate failed for {snapshot.run_date}",
            detail=asdict(snapshot),
        )
    ]


def load_data_bridge_health(refresh_date: str) -> DataBridgeHealthSnapshot:
    config = DataBridgeRefreshConfig.from_env()
    store = DataBridgeStore(data_root=config.data_root, runtime_root=config.runtime_root)
    state: dict[str, object] = {}
    validation_error: str | None = None
    try:
        checked = check_current_dataset(config)
        state = dict(checked.state)
    except Exception as exc:
        validation_error = str(exc)
        try:
            state = store.load_state()
        except Exception:
            state = {}
    files = state.get("files")
    last_attempt = state.get("last_attempt")
    return DataBridgeHealthSnapshot(
        required_refresh_date=refresh_date,
        current_refresh_date=str(state["refresh_date"]) if state.get("refresh_date") else None,
        generation_id=str(state["generation_id"]) if state.get("generation_id") else None,
        refreshed_at=str(state["refreshed_at"]) if state.get("refreshed_at") else None,
        business_digest=str(state["business_digest"]) if state.get("business_digest") else None,
        files=files if isinstance(files, dict) else {},
        last_attempt=last_attempt if isinstance(last_attempt, dict) else None,
        validation_error=validation_error,
    )


def load_v2_scheduler_gate(
    run_date: str,
    *,
    data_bridge: DataBridgeHealthSnapshot,
) -> V2SchedulerGateSnapshot:
    """读取 V2 日级凭证，并与当前 DataBridge generation 对照。"""
    config = DataBridgeRefreshConfig.from_env()
    try:
        record = load_gate_record(config, run_date)
    except V2DailyGateBlocked as exc:
        return V2SchedulerGateSnapshot(
            run_date=run_date,
            status=None,
            generation_id=None,
            current_generation_id=data_bridge.generation_id,
            restart_verified=False,
            error=str(exc),
        )
    restart = record.get("restart")
    restart_mapping = restart if isinstance(restart, dict) else {}
    return V2SchedulerGateSnapshot(
        run_date=run_date,
        status=str(record["status"]) if record.get("status") else None,
        generation_id=(
            str(record["generation_id"]) if record.get("generation_id") else None
        ),
        current_generation_id=data_bridge.generation_id,
        restart_verified=restart_mapping.get("verified") is True,
        error=_gate_record_error(record),
    )


def _gate_record_error(record: Mapping[str, object]) -> str | None:
    restart = record.get("restart")
    if isinstance(restart, dict) and restart.get("error"):
        return str(restart["error"])
    checks = record.get("checks")
    if isinstance(checks, list):
        for check in reversed(checks):
            if isinstance(check, dict) and check.get("error"):
                return str(check["error"])
    return None


def evaluate_daily_health(
    snapshot: DailyHealthSnapshot,
    *,
    strict_runs: bool = False,
) -> list[HealthFinding]:
    """根据只读快照生成健康检查发现。"""
    findings: list[HealthFinding] = []
    active = set(snapshot.active_daily_base_schemes)
    successful = set(snapshot.successful_daily_run_schemes)
    missing_runs = sorted(active - successful)
    blocked_schemes = _source_blocked_schemes(snapshot, missing_runs)
    blocked_scheme_ids = {str(item["scheme_id"]) for item in blocked_schemes}
    all_missing_due_source_watermark = (
        bool(active)
        and snapshot.predictions_count == 0
        and set(missing_runs) == active
        and active.issubset(blocked_scheme_ids)
    )

    for watermark in snapshot.actual_watermarks:
        if watermark.actual_max and not watermark.source_max:
            findings.append(
                HealthFinding(
                    level="warning",
                    code="source_watermark_missing",
                    message=f"{watermark.tenor} actual exists but source watermark is missing",
                    detail=asdict(watermark),
                )
            )
            continue
        if watermark.actual_max and watermark.source_max and watermark.actual_max > watermark.source_max:
            findings.append(
                HealthFinding(
                    level="error",
                    code="actual_tail_after_source",
                    message=f"{watermark.tenor} actual tail is later than source watermark",
                    detail=asdict(watermark),
                )
            )

    if not snapshot.is_trading_day:
        return findings

    if active and snapshot.predictions_count == 0:
        findings.append(
            HealthFinding(
                level="warning" if all_missing_due_source_watermark else "error",
                code="daily_predictions_missing",
                message=f"{snapshot.predict_date} is a trading day but has no prediction rows",
                detail={
                    "predict_date": snapshot.predict_date,
                    "active_daily_base_count": len(active),
                    "all_missing_due_source_watermark": all_missing_due_source_watermark,
                },
            )
        )

    invalid_predictions = [
        asdict(item)
        for item in snapshot.prediction_date_checks
        if item.predict_date != snapshot.predict_date
        or item.feature_date != item.expected_feature_date
        or item.target_date != item.expected_target_date
    ]
    if invalid_predictions:
        findings.append(
            HealthFinding(
                level="error",
                code="daily_prediction_date_semantics_mismatch",
                message="daily prediction rows do not match expected live feature/target dates",
                detail={
                    "predict_date": snapshot.predict_date,
                    "invalid_predictions": invalid_predictions,
                },
            )
        )

    mismatched_runs = [
        asdict(item)
        for item in snapshot.run_prediction_counts
        if item.records_written is not None and item.records_written != item.prediction_rows
    ]
    if mismatched_runs:
        findings.append(
            HealthFinding(
                level="error",
                code="successful_run_prediction_rows_mismatch",
                message="successful run log records_written does not match prediction rows",
                detail={
                    "predict_date": snapshot.predict_date,
                    "mismatched_runs": mismatched_runs,
                },
            )
        )

    if missing_runs:
        findings.append(
            HealthFinding(
                level="error" if strict_runs else "warning",
                code="active_daily_runs_missing",
                message=f"{len(missing_runs)} active daily base schemes have no successful run",
                detail={
                    "predict_date": snapshot.predict_date,
                    "missing_base_schemes": missing_runs,
                    "successful_base_schemes": sorted(successful),
                },
            )
        )
        findings.extend(_input_watermark_findings(snapshot, blocked_schemes))

    return findings


def _input_watermark_findings(
    snapshot: DailyHealthSnapshot,
    blocked_schemes: Sequence[dict[str, object]],
) -> list[HealthFinding]:
    if not blocked_schemes:
        return []
    return [
        HealthFinding(
            level="warning",
            code="daily_run_input_watermark_blocked",
            message="missing daily runs have source watermarks earlier than expected feature date",
            detail={
                "predict_date": snapshot.predict_date,
                "expected_feature_date": snapshot.expected_feature_date,
                "blocked_schemes": blocked_schemes,
            },
        )
    ]


def _source_blocked_schemes(
    snapshot: DailyHealthSnapshot,
    missing_runs: Sequence[str],
) -> list[dict[str, object]]:
    expected_feature_date = snapshot.expected_feature_date
    if not expected_feature_date:
        return []

    source_by_tenor = {item.tenor: item.source_max for item in snapshot.actual_watermarks}
    blocked_schemes: list[dict[str, object]] = []
    for scheme_id in missing_runs:
        required_tenors = SCHEME_REQUIRED_SOURCE_TENORS.get(scheme_id)
        if not required_tenors:
            continue
        blocked_tenors = [
            {"tenor": tenor, "source_max": source_by_tenor.get(tenor)}
            for tenor in required_tenors
            if not source_by_tenor.get(tenor) or str(source_by_tenor[tenor]) < expected_feature_date
        ]
        if blocked_tenors:
            blocked_schemes.append(
                {
                    "scheme_id": scheme_id,
                    "required_tenors": list(required_tenors),
                    "blocked_tenors": blocked_tenors,
                }
            )
    return blocked_schemes


def load_snapshot(
    engine: Engine,
    *,
    predict_date: str,
    tenors: Sequence[str] | None = None,
) -> DailyHealthSnapshot:
    """从生产库读取只读健康快照。"""
    normalized_tenors = tuple(normalize_tenor(item) for item in (tenors or TENOR_TO_INDICATOR.keys()))
    with engine.connect() as conn:
        active_rows = conn.execute(
            text(
                """
                SELECT DISTINCT base_scheme_id
                FROM t_scheme_registry
                WHERE status = 'active'
                  AND frequency = 'daily'
                  AND task_type IN ('T+1', 'T+5')
                ORDER BY base_scheme_id
                """
            )
        ).scalars().all()
        active_schemes = tuple(str(item) for item in active_rows)

        if active_schemes:
            successful_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT scheme_id
                    FROM t_scheme_runs
                    WHERE predict_date = :predict_date
                      AND status = 'success'
                      AND scheme_id IN :scheme_ids
                    ORDER BY scheme_id
                    """
                ).bindparams(bindparam("scheme_ids", expanding=True)),
                {"predict_date": predict_date, "scheme_ids": list(active_schemes)},
            ).scalars().all()
        else:
            successful_rows = []

        if active_schemes:
            run_prediction_rows = conn.execute(
                text(
                    """
                    SELECT
                        r.run_id,
                        r.scheme_id,
                        r.records_written,
                        COUNT(p.id) AS prediction_rows
                    FROM t_scheme_runs r
                    LEFT JOIN t_scheme_predictions p
                      ON p.run_id = r.run_id
                    WHERE r.predict_date = :predict_date
                      AND r.status = 'success'
                      AND r.scheme_id IN :scheme_ids
                    GROUP BY r.run_id, r.scheme_id, r.records_written
                    ORDER BY r.run_id
                    """
                ).bindparams(bindparam("scheme_ids", expanding=True)),
                {"predict_date": predict_date, "scheme_ids": list(active_schemes)},
            )
            run_prediction_counts = tuple(
                RunPredictionCount(
                    run_id=int(row.run_id),
                    scheme_id=str(row.scheme_id),
                    records_written=(
                        int(row.records_written)
                        if row.records_written is not None
                        else None
                    ),
                    prediction_rows=int(row.prediction_rows),
                )
                for row in run_prediction_rows
            )
        else:
            run_prediction_counts = ()

        prediction_rows = conn.execute(
            text(
                """
                SELECT
                    p.id,
                    p.scheme_id,
                    p.target_tenor,
                    p.horizon,
                    p.predict_date,
                    p.feature_date,
                    p.target_date
                FROM t_scheme_predictions p
                JOIN t_scheme_registry r
                  ON r.base_scheme_id = p.scheme_id
                 AND r.target_tenor = p.target_tenor
                 AND r.horizon = p.horizon
                 AND r.status = 'active'
                 AND r.frequency = 'daily'
                 AND r.task_type IN ('T+1', 'T+5')
                WHERE p.predict_date = :predict_date
                ORDER BY p.id
                """
            ),
            {"predict_date": predict_date},
        ).mappings().all()
        predictions_count = len(prediction_rows)

        actual_watermarks: list[ActualWatermark] = []
        for tenor in normalized_tenors:
            indicator = indicator_for_tenor(tenor)
            if not indicator:
                actual_watermarks.append(ActualWatermark(tenor, None, None))
                continue
            actual_max = conn.execute(
                text("SELECT MAX(trade_date) FROM t_scheme_actuals WHERE tenor = :tenor"),
                {"tenor": tenor},
            ).scalar_one()
            source_max = conn.execute(
                text(
                    """
                    SELECT MAX(rdate)
                    FROM api_wind_daily
                    WHERE indicators_code = :indicator
                      AND indicators_value IS NOT NULL
                    """
                ),
                {"indicator": indicator},
            ).scalar_one()
            actual_watermarks.append(ActualWatermark(tenor, _iso(actual_max), _iso(source_max)))

    expected_feature_date = _expected_daily_feature_date(engine, predict_date)
    prediction_date_checks = _prediction_date_checks(
        engine,
        predict_date=predict_date,
        expected_feature_date=expected_feature_date,
        prediction_rows=prediction_rows,
    )
    return DailyHealthSnapshot(
        predict_date=predict_date,
        expected_feature_date=expected_feature_date,
        is_trading_day=get_calendar(engine=engine).is_trading_day(predict_date),
        active_daily_base_schemes=active_schemes,
        successful_daily_run_schemes=tuple(str(item) for item in successful_rows),
        predictions_count=predictions_count,
        run_prediction_counts=run_prediction_counts,
        prediction_date_checks=prediction_date_checks,
        actual_watermarks=tuple(actual_watermarks),
    )


def _expected_daily_feature_date(engine: Engine, predict_date: str) -> str | None:
    try:
        return get_calendar(engine=engine).previous_trading_day(predict_date)
    except Exception:
        return None


def _prediction_date_checks(
    engine: Engine,
    *,
    predict_date: str,
    expected_feature_date: str | None,
    prediction_rows: Sequence[object],
) -> tuple[PredictionDateCheck, ...]:
    expected_target_by_horizon: dict[int, str | None] = {}
    calendar = get_calendar(engine=engine)
    checks: list[PredictionDateCheck] = []
    for row in prediction_rows:
        horizon = int(row["horizon"])
        if horizon not in expected_target_by_horizon:
            expected_target = None
            if expected_feature_date:
                try:
                    expected_target = str(calendar.nth_trading_day_after(expected_feature_date, horizon))[:10]
                except Exception:
                    expected_target = None
            expected_target_by_horizon[horizon] = expected_target
        checks.append(
            PredictionDateCheck(
                prediction_id=int(row["id"]),
                scheme_id=str(row["scheme_id"]),
                target_tenor=str(row["target_tenor"]),
                horizon=horizon,
                predict_date=_iso(row["predict_date"]) or str(predict_date)[:10],
                feature_date=_iso(row["feature_date"]),
                target_date=_iso(row["target_date"]) or "",
                expected_feature_date=expected_feature_date,
                expected_target_date=expected_target_by_horizon[horizon],
            )
        )
    return tuple(checks)


def _default_predict_date() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _payload(
    snapshot: DailyHealthSnapshot,
    daily_findings: Sequence[HealthFinding],
    *,
    data_bridge: DataBridgeHealthSnapshot | None = None,
    data_bridge_findings: Sequence[HealthFinding] = (),
    v2_scheduler_gate: V2SchedulerGateSnapshot | None = None,
    v2_gate_findings: Sequence[HealthFinding] = (),
) -> dict[str, object]:
    findings = [*daily_findings, *data_bridge_findings, *v2_gate_findings]
    return {
        "status": status_from_findings(findings),
        "snapshot": asdict(snapshot),
        "data_bridge": asdict(data_bridge) if data_bridge is not None else None,
        "findings": [asdict(item) for item in findings],
        "daily_health": {
            "status": status_from_findings(daily_findings),
            "snapshot": asdict(snapshot),
            "findings": [asdict(item) for item in daily_findings],
        },
        "v2_scheduler_gate": {
            "status": status_from_findings([*data_bridge_findings, *v2_gate_findings]),
            "snapshot": (
                asdict(v2_scheduler_gate) if v2_scheduler_gate is not None else None
            ),
            "data_bridge": asdict(data_bridge) if data_bridge is not None else None,
            "findings": [
                asdict(item) for item in [*data_bridge_findings, *v2_gate_findings]
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only production daily prediction health check.")
    parser.add_argument("--predict-date", default=_default_predict_date(), help="YYYY-MM-DD, defaults to today")
    parser.add_argument("--tenor", action="append", help="Limit actual watermark checks to one tenor")
    parser.add_argument(
        "--strict-runs",
        action="store_true",
        help="Treat missing successful runs for active daily schemes as errors instead of warnings.",
    )
    args = parser.parse_args()
    engine = create_engine_from_env()

    try:
        snapshot = load_snapshot(engine, predict_date=args.predict_date, tenors=args.tenor)
    finally:
        engine.dispose()

    data_bridge = load_data_bridge_health(args.predict_date)
    v2_scheduler_gate = load_v2_scheduler_gate(
        args.predict_date,
        data_bridge=data_bridge,
    )
    daily_findings = evaluate_daily_health(snapshot, strict_runs=args.strict_runs)
    data_bridge_findings = evaluate_data_bridge_health(
        data_bridge,
        deadline=DataBridgeRefreshConfig.from_env().refresh_deadline,
    )
    v2_gate_findings = evaluate_v2_scheduler_gate(v2_scheduler_gate)
    payload = _payload(
        snapshot,
        daily_findings,
        data_bridge=data_bridge,
        data_bridge_findings=data_bridge_findings,
        v2_scheduler_gate=v2_scheduler_gate,
        v2_gate_findings=v2_gate_findings,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    status = payload["status"]
    if status == "error":
        return 2
    if status == "warning":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
