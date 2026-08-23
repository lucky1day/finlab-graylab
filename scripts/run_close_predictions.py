#!/usr/bin/env python3
"""收盘后按需刷新一次 DataBridge，并顺序执行月度/周期均值预测。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
import json
import os
from pathlib import Path
import sys
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.deployment_scope import (
    DeploymentScopeError,
    require_deployment_target_for_control_plane,
)
from scheduler.discovery import discover_schemes
from scheduler.launchd_prediction_runner import (
    _candidate_matches_cadence,
    _period_due_task_types,
)
from scheduler.repository import create_engine_from_env
from scheduler.v2_daily_gate import V2DailyGateBlocked, require_v2_daily_ready
from shared.calendar_service import get_calendar
from shared.data_bridge.refresh import DataBridgeRefreshConfig
from shared.one_shot_control_plane import (
    LAUNCHD_ONE_SHOT_CONTROL_PLANE,
    SYSTEMD_ONE_SHOT_CONTROL_PLANE,
)
from shared.prediction_context import build_monthly_live_context
from scripts.refresh_data_bridge_current import run_command as refresh_data_bridge


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
CONTROL_PLANES = {
    "launchd": LAUNCHD_ONE_SHOT_CONTROL_PLANE,
    "systemd": SYSTEMD_ONE_SHOT_CONTROL_PLANE,
}


@dataclass(frozen=True)
class CloseJobResult:
    outcome: str
    exit_code: int
    predict_date: str
    refresh_required: bool
    refresh_status: str | None
    cadences: tuple[dict[str, object], ...]

    def payload(self) -> dict[str, object]:
        return {
            "event": "close_prediction_run",
            "predict_date": self.predict_date,
            "outcome": self.outcome,
            "refresh_required": self.refresh_required,
            "refresh_status": self.refresh_status,
            "cadences": list(self.cadences),
            "exit_code": self.exit_code,
        }


def _today() -> str:
    return datetime.now(ASIA_SHANGHAI).date().isoformat()


def _normalize_date(value: str) -> str:
    return date.fromisoformat(str(value)).isoformat()


def _refresh_clock(value: str) -> str:
    """校验显式 job 刷新窗口使用严格 HH:MM。"""
    raw = str(value)
    try:
        parsed = datetime.strptime(raw, "%H:%M")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "refresh window must use HH:MM"
        ) from exc
    if parsed.strftime("%H:%M") != raw:
        raise argparse.ArgumentTypeError("refresh window must use HH:MM")
    return raw


def _runner_for_control_plane(control_plane: str) -> Callable[..., object]:
    if control_plane == "launchd":
        from scheduler.launchd_prediction_runner import run

        return run
    if control_plane == "systemd":
        from scheduler.systemd_prediction_runner import run

        return run
    raise ValueError("invalid close prediction control plane")


def _is_current_ready(
    *,
    run_date: str,
    expected_feature_date: str,
    refresh_start: str | None = None,
    refresh_deadline: str | None = None,
) -> bool:
    """只读确认当日 ready 证据已覆盖期望 cutoff。"""
    config = DataBridgeRefreshConfig.from_env(
        refresh_start=refresh_start,
        refresh_deadline=refresh_deadline,
    )
    try:
        require_v2_daily_ready(
            config,
            run_date,
            expected_feature_date,
        )
    except V2DailyGateBlocked:
        return False
    return True


def run_close_job(
    *,
    control_plane: str,
    predict_date: str,
    algo_env: str,
    refresh_start: str | None = None,
    refresh_deadline: str | None = None,
) -> CloseJobResult:
    """执行一次到期判断、单次刷新和顺序预测。"""
    if (refresh_start is None) != (refresh_deadline is None):
        raise ValueError(
            "close prediction refresh window requires both start and deadline"
        )
    normalized_date = _normalize_date(predict_date)
    try:
        control_plane_identity = CONTROL_PLANES[control_plane]
    except KeyError as exc:
        raise ValueError("invalid close prediction control plane") from exc
    try:
        require_deployment_target_for_control_plane(control_plane_identity)
    except DeploymentScopeError as exc:
        raise ValueError("deployment target does not match control plane") from exc

    discovered = discover_schemes(strict=True)
    monthly_candidates = [
        cfg
        for cfg in discovered
        if getattr(cfg, "status", None) == "active"
        and _candidate_matches_cadence(cfg, "monthly")
    ]
    period_candidates = [
        cfg
        for cfg in discovered
        if getattr(cfg, "status", None) == "active"
        and _candidate_matches_cadence(cfg, "period_average")
    ]
    monthly_due = date.fromisoformat(normalized_date).day == 15 and bool(
        monthly_candidates
    )

    engine = create_engine_from_env()
    try:
        calendar = get_calendar(engine)
        if not calendar.covers(normalized_date):
            raise ValueError("trade calendar does not cover predict_date")
        due_period_types, invalid_period_types = _period_due_task_types(
            period_candidates,
            calendar,
            normalized_date,
        )
        period_due = bool(due_period_types or invalid_period_types)
        if not monthly_due and not period_due:
            return CloseJobResult(
                outcome="not_applicable",
                exit_code=0,
                predict_date=normalized_date,
                refresh_required=False,
                refresh_status=None,
                cadences=(),
            )
        expected_dates: set[str] = set()
        refresh_required = False
        if monthly_due and any(
            getattr(cfg, "runtime_type", None) == "blackbox_v2"
            and getattr(cfg, "input_source", None) == "data_bridge_current"
            for cfg in monthly_candidates
        ):
            expected_dates.add(
                build_monthly_live_context(calendar, normalized_date).feature_date
            )
            refresh_required = True
        due_period_candidates = [
            cfg
            for cfg in period_candidates
            if str(getattr(cfg, "task_type", "") or "") in due_period_types
        ]
        if any(
            getattr(cfg, "runtime_type", None) == "blackbox_v2"
            and getattr(cfg, "input_source", None) == "data_bridge_current"
            for cfg in due_period_candidates
        ):
            expected_dates.add(normalized_date)
            refresh_required = True
        if len(expected_dates) > 1:
            raise ValueError("close cadences require conflicting DataBridge cutoffs")
        expected_feature_date = next(iter(expected_dates), None)
    finally:
        engine.dispose()

    refresh_status: str | None = None
    if refresh_required:
        assert expected_feature_date is not None
        refresh_window = (
            {}
            if refresh_start is None
            else {
                "refresh_start": refresh_start,
                "refresh_deadline": refresh_deadline,
            }
        )
        if _is_current_ready(
            run_date=normalized_date,
            expected_feature_date=expected_feature_date,
            **refresh_window,
        ):
            refresh_status = "already_ready"
        else:
            refresh_code, refresh_payload = refresh_data_bridge(
                "publish",
                refresh_date=normalized_date,
                expected_feature_date=expected_feature_date,
                **refresh_window,
            )
            refresh_status = str(refresh_payload.get("status") or "failed")
            if refresh_code != 0:
                return CloseJobResult(
                    outcome="refresh_failed",
                    exit_code=1 if refresh_code == 1 else 2,
                    predict_date=normalized_date,
                    refresh_required=True,
                    refresh_status=refresh_status,
                    cadences=(),
                )

    run_prediction = _runner_for_control_plane(control_plane)
    summaries: list[dict[str, object]] = []
    exit_code = 0
    for cadence, due in (
        ("monthly", monthly_due),
        ("period_average", period_due),
    ):
        if not due:
            continue
        summary = run_prediction(
            cadence,
            predict_date=normalized_date,
            algo_env=algo_env,
        )
        payload = summary.to_payload()
        summaries.append(payload)
        exit_code = max(exit_code, int(payload["exit_code"]))
    return CloseJobResult(
        outcome="success" if exit_code == 0 else "partial",
        exit_code=exit_code,
        predict_date=normalized_date,
        refresh_required=refresh_required,
        refresh_status=refresh_status,
        cadences=tuple(summaries),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-plane", choices=sorted(CONTROL_PLANES), required=True)
    parser.add_argument("--predict-date", default=_today())
    parser.add_argument("--algo-env", default=os.getenv("BOND_ALGO_CONDA_ENV", "forecast_env"))
    parser.add_argument("--refresh-start", type=_refresh_clock)
    parser.add_argument("--refresh-deadline", type=_refresh_clock)
    args = parser.parse_args(argv)
    try:
        result = run_close_job(
            control_plane=args.control_plane,
            predict_date=args.predict_date,
            algo_env=args.algo_env,
            refresh_start=args.refresh_start,
            refresh_deadline=args.refresh_deadline,
        )
    except Exception:
        result = CloseJobResult(
            outcome="configuration_error",
            exit_code=2,
            predict_date=str(args.predict_date),
            refresh_required=False,
            refresh_status=None,
            cadences=(),
        )
    print(
        json.dumps(
            result.payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
