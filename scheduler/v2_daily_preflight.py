"""Blackbox V2 DataBridge 每日检查与 scheduler 受控重启。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo

from scheduler.repository import create_engine_from_env
from scheduler.v2_daily_gate import write_gate_record
from shared.calendar_service import get_calendar
from shared.data_bridge.refresh import (
    DataBridgeRefreshConfig,
    check_current_dataset,
)
from shared.daily_coordinator_mode import (
    DAILY_COORDINATOR_MODE_ENV,
    bootstrap_deployment_daily_coordinator_mode,
)


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEDULER_LABEL = "com.bond-factor-lab.scheduler"
PHASE_CLOCKS = {
    (6, 0): "refresh-primary",
    (6, 30): "check-primary",
    (6, 35): "refresh-retry",
    (7, 0): "finalize",
}
PHASES = frozenset(PHASE_CLOCKS.values())


class PreflightError(RuntimeError):
    """V2 每日 preflight 配置或执行失败。"""


@dataclass(frozen=True)
class PreflightDependencies:
    """隔离外部刷新、DB 日历和 launchd 副作用。"""

    config_factory: Callable[[], DataBridgeRefreshConfig]
    refresh: Callable[[str], Mapping[str, object]]
    check: Callable[[str, str], Mapping[str, object]]
    expected_daily_date: Callable[[str], str]
    restart_scheduler: Callable[[datetime], Mapping[str, object]]


def resolve_phase(now: datetime) -> str:
    """把 launchd 触发时间映射到唯一管理阶段。"""
    localized = _localized(now)
    phase = PHASE_CLOCKS.get((localized.hour, localized.minute))
    if phase is None:
        raise PreflightError(
            f"unmanaged V2 preflight time: {localized.isoformat(timespec='seconds')}"
        )
    return phase


def run_phase(
    phase: str,
    *,
    now: datetime | None = None,
    dependencies: PreflightDependencies | None = None,
) -> dict[str, object]:
    """执行一个 preflight 阶段并持久化结构化决策。"""
    if phase not in PHASES:
        raise PreflightError(f"unsupported V2 preflight phase: {phase}")
    run_now = _localized(now or datetime.now(ASIA_SHANGHAI))
    if phase == "finalize" and (run_now.hour, run_now.minute) != (7, 0):
        raise PreflightError(
            f"finalize is outside the 07:00 safe window: {run_now.isoformat(timespec='seconds')}"
        )
    deps = dependencies or default_dependencies()
    config = deps.config_factory()
    run_date = run_now.date().isoformat()
    expected_daily_date = deps.expected_daily_date(run_date)

    if phase == "refresh-primary":
        return _run_refresh_phase(
            phase=phase,
            run_now=run_now,
            run_date=run_date,
            expected_daily_date=expected_daily_date,
            config=config,
            refresh=deps.refresh,
            prior_checks=[],
        )
    if phase == "check-primary":
        return _run_check_phase(
            run_now=run_now,
            run_date=run_date,
            expected_daily_date=expected_daily_date,
            config=config,
            check=deps.check,
        )
    if phase == "refresh-retry":
        try:
            state = dict(deps.check(run_date, expected_daily_date))
        except Exception as exc:
            return _run_refresh_phase(
                phase=phase,
                run_now=run_now,
                run_date=run_date,
                expected_daily_date=expected_daily_date,
                config=config,
                refresh=deps.refresh,
                prior_checks=[_failed_check("pre_retry_current_dataset", exc)],
            )
        record = _write_decision(
            config,
            run_now=run_now,
            run_date=run_date,
            expected_daily_date=expected_daily_date,
            status="checking",
            state=state,
            checks=[_passed_check("pre_retry_current_dataset")],
        )
        return _summary(phase, "skipped-current", record)
    return _run_finalize_phase(
        run_now=run_now,
        run_date=run_date,
        expected_daily_date=expected_daily_date,
        config=config,
        check=deps.check,
        restart=deps.restart_scheduler,
    )


def _run_refresh_phase(
    *,
    phase: str,
    run_now: datetime,
    run_date: str,
    expected_daily_date: str,
    config: DataBridgeRefreshConfig,
    refresh: Callable[[str], Mapping[str, object]],
    prior_checks: list[dict[str, object]],
) -> dict[str, object]:
    try:
        state = dict(refresh(run_date))
    except Exception as exc:
        checks = [*prior_checks, _failed_check(phase, exc)]
        record = _write_decision(
            config,
            run_now=run_now,
            run_date=run_date,
            expected_daily_date=expected_daily_date,
            status="checking",
            state=_best_effort_state(config),
            checks=checks,
        )
        return _summary(phase, "refresh-failed", record, error=str(exc))
    record = _write_decision(
        config,
        run_now=run_now,
        run_date=run_date,
        expected_daily_date=expected_daily_date,
        status="checking",
        state=state,
        checks=[*prior_checks, _passed_check(phase)],
    )
    return _summary(phase, "refreshed", record)


def _run_check_phase(
    *,
    run_now: datetime,
    run_date: str,
    expected_daily_date: str,
    config: DataBridgeRefreshConfig,
    check: Callable[[str, str], Mapping[str, object]],
) -> dict[str, object]:
    try:
        state = dict(check(run_date, expected_daily_date))
    except Exception as exc:
        record = _write_decision(
            config,
            run_now=run_now,
            run_date=run_date,
            expected_daily_date=expected_daily_date,
            status="checking",
            state=_best_effort_state(config),
            checks=[_failed_check("primary_current_dataset", exc)],
        )
        return _summary("check-primary", "stale", record, error=str(exc))
    record = _write_decision(
        config,
        run_now=run_now,
        run_date=run_date,
        expected_daily_date=expected_daily_date,
        status="checking",
        state=state,
        checks=[_passed_check("primary_current_dataset")],
    )
    return _summary("check-primary", "current", record)


def _run_finalize_phase(
    *,
    run_now: datetime,
    run_date: str,
    expected_daily_date: str,
    config: DataBridgeRefreshConfig,
    check: Callable[[str, str], Mapping[str, object]],
    restart: Callable[[datetime], Mapping[str, object]],
) -> dict[str, object]:
    try:
        state = dict(check(run_date, expected_daily_date))
    except Exception as exc:
        record = _write_decision(
            config,
            run_now=run_now,
            run_date=run_date,
            expected_daily_date=expected_daily_date,
            status="blocked",
            state=_best_effort_state(config),
            checks=[_failed_check("final_current_dataset", exc)],
            restart={"requested": False, "verified": False},
        )
        return _summary("finalize", "blocked", record, error=str(exc))

    _write_decision(
        config,
        run_now=run_now,
        run_date=run_date,
        expected_daily_date=expected_daily_date,
        status="ready",
        state=state,
        checks=[_passed_check("final_current_dataset")],
        restart={"requested": True, "verified": False},
    )
    try:
        restart_result = dict(restart(run_now))
        if restart_result.get("verified") is not True:
            raise PreflightError("scheduler restart was not verified")
    except Exception as exc:
        record = _write_decision(
            config,
            run_now=run_now,
            run_date=run_date,
            expected_daily_date=expected_daily_date,
            status="blocked",
            state=state,
            checks=[_passed_check("final_current_dataset")],
            restart={"requested": True, "verified": False, "error": str(exc)},
        )
        return _summary("finalize", "blocked", record, error=str(exc))
    record = _write_decision(
        config,
        run_now=run_now,
        run_date=run_date,
        expected_daily_date=expected_daily_date,
        status="ready",
        state=state,
        checks=[_passed_check("final_current_dataset")],
        restart=restart_result,
    )
    return _summary("finalize", "ready", record)


def restart_scheduler(
    now: datetime,
    *,
    uid: int | None = None,
    timeout_sec: float = 45.0,
) -> dict[str, object]:
    """只重启固定 launchd scheduler label，并验证 PID 已切换。"""
    if _daily_coordinator_mode() == "ledger":
        raise PreflightError("scheduler restart is disabled in ledger mode")
    run_now = _localized(now)
    if (run_now.hour, run_now.minute) != (7, 0):
        raise PreflightError("scheduler restart is outside the 07:00 safe window")
    resolved_uid = os.getuid() if uid is None else int(uid)
    service = f"gui/{resolved_uid}/{SCHEDULER_LABEL}"
    old_pid = _launchd_pid(_launchctl_print(service))
    subprocess.run(
        ["launchctl", "kickstart", "-k", service],
        check=True,
        capture_output=True,
        text=True,
    )
    deadline = time.monotonic() + timeout_sec
    new_pid: int | None = None
    while time.monotonic() <= deadline:
        new_pid = _launchd_pid(_launchctl_print(service))
        if new_pid is not None and new_pid != old_pid:
            return {
                "requested": True,
                "verified": True,
                "label": SCHEDULER_LABEL,
                "old_pid": old_pid,
                "new_pid": new_pid,
                "verified_at": datetime.now(ASIA_SHANGHAI).isoformat(timespec="seconds"),
            }
        time.sleep(0.25)
    raise PreflightError(
        f"scheduler PID did not change within {timeout_sec:.1f}s: old={old_pid} new={new_pid}"
    )


def _launchctl_print(service: str) -> str:
    result = subprocess.run(
        ["launchctl", "print", service],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _launchd_pid(output: str) -> int | None:
    match = re.search(r"(?m)^\s*pid\s*=\s*(\d+)\s*$", output)
    return int(match.group(1)) if match else None


def default_dependencies() -> PreflightDependencies:
    return PreflightDependencies(
        config_factory=DataBridgeRefreshConfig.from_env,
        refresh=_default_refresh,
        check=_default_check,
        expected_daily_date=_default_expected_daily_date,
        restart_scheduler=restart_scheduler,
    )


def _default_refresh(run_date: str) -> Mapping[str, object]:
    from scheduler.main import run_data_bridge_refresh_job

    result = run_data_bridge_refresh_job(run_date, enforce_deadline=False)
    return dict(result.state)


def _default_check(run_date: str, expected_daily_date: str) -> Mapping[str, object]:
    current = check_current_dataset(
        DataBridgeRefreshConfig.from_env(),
        required_refresh_date=run_date,
        expected_daily_date=expected_daily_date,
    )
    return dict(current.state)


def _default_expected_daily_date(run_date: str) -> str:
    engine = create_engine_from_env()
    try:
        return get_calendar(engine).previous_trading_day(run_date)
    finally:
        engine.dispose()


def _write_decision(
    config: DataBridgeRefreshConfig,
    *,
    run_now: datetime,
    run_date: str,
    expected_daily_date: str,
    status: str,
    state: Mapping[str, object],
    checks: Sequence[Mapping[str, object]],
    restart: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return write_gate_record(
        config,
        run_date=run_date,
        status=status,
        checked_at=run_now.isoformat(timespec="seconds"),
        generation_id=_optional_text(state.get("generation_id")),
        refresh_date=_optional_text(state.get("refresh_date")),
        expected_daily_date=expected_daily_date,
        business_digest=_optional_text(state.get("business_digest")),
        checks=checks,
        restart=restart,
    )


def _best_effort_state(config: DataBridgeRefreshConfig) -> dict[str, object]:
    state_path = config.runtime_root / "state.json"
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _passed_check(name: str) -> dict[str, object]:
    return {"name": name, "status": "passed"}


def _failed_check(name: str, exc: Exception) -> dict[str, object]:
    return {"name": name, "status": "failed", "error": str(exc)}


def _summary(
    phase: str,
    status: str,
    record: Mapping[str, object],
    *,
    error: str | None = None,
) -> dict[str, object]:
    return {
        "event": "v2_daily_preflight",
        "phase": phase,
        "status": status,
        "run_date": record.get("run_date"),
        "checked_at": record.get("checked_at"),
        "generation_id": record.get("generation_id"),
        "error": error,
    }


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _localized(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=ASIA_SHANGHAI)
    return value.astimezone(ASIA_SHANGHAI)


def _daily_coordinator_mode() -> str:
    try:
        return bootstrap_deployment_daily_coordinator_mode()
    except ValueError as exc:
        raise PreflightError(
            str(exc)
        ) from exc


@contextmanager
def _single_instance(config: DataBridgeRefreshConfig) -> Iterator[bool]:
    lock_path = config.runtime_root / "v2_scheduler_gate" / "preflight.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Blackbox V2 daily preflight phase.")
    parser.add_argument("--phase", choices=["auto", *sorted(PHASES)], default="auto")
    args = parser.parse_args(argv)
    now = datetime.now(ASIA_SHANGHAI)
    try:
        coordinator_mode = _daily_coordinator_mode()
        if coordinator_mode == "ledger":
            payload = {
                "coordinator_mode": coordinator_mode,
                "event": "v2_daily_preflight",
                "phase": "disabled",
                "reason": "daily-coordinator-ledger-mode",
                "run_date": now.date().isoformat(),
                "status": "disabled",
            }
            print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
            return 0
        phase = resolve_phase(now) if args.phase == "auto" else args.phase
        dependencies = default_dependencies()
        with _single_instance(dependencies.config_factory()) as acquired:
            if not acquired:
                payload = {
                    "event": "v2_daily_preflight",
                    "phase": phase,
                    "status": "skipped-busy",
                    "run_date": now.date().isoformat(),
                }
                print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
                return 0
            payload = run_phase(phase, now=now, dependencies=dependencies)
    except Exception as exc:
        payload = {
            "event": "v2_daily_preflight",
            "status": "error",
            "run_date": now.date().isoformat(),
            "error": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    if payload.get("status") in {"blocked", "refresh-failed"}:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
