#!/usr/bin/env python
"""刷新或检查平台统一 DataBridge 五文件 current。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.v2_daily_gate import write_gate_record  # noqa: E402
from shared.calendar_service import get_calendar  # noqa: E402
from shared.data_bridge.refresh import (  # noqa: E402
    DataBridgeRefreshConfig,
    DataBridgeContinuityIdentityError,
    DataBridgeCurrentInvalidError,
    DataBridgeCurrentMissingError,
    DataBridgeCurrentReadError,
    DataBridgeNonRetryableRefreshError,
    DataBridgeRecoveryIdentityError,
    DataBridgeRefreshError,
    DataBridgeStoreLockError,
    FAILED_ATTEMPT_ERROR_CATEGORIES,
    check_current_dataset,
    run_full_refresh,
)
from shared.data_bridge.authority import (  # noqa: E402
    resolve_databridge_continuity_authority_from_engine,
)
from shared.data_bridge.mysql_exporter import (  # noqa: E402
    MySqlDataBridgeRoundBuilder,
)
from shared.data_bridge.validation import DataBridgeValidationError  # noqa: E402
from shared.data_service import create_sqlalchemy_engine  # noqa: E402
from shared.input_artifacts import (  # noqa: E402
    invalidate_ready_blackbox_snapshot,
    prepare_blackbox_generation_snapshot,
)
from shared.one_shot_control_plane import (  # noqa: E402
    DATABRIDGE_LAUNCHD_PRODUCER,
    DATABRIDGE_ONE_SHOT_PRODUCERS,
)

Mode = Literal["dry-run", "publish", "check-only"]
DATABRIDGE_PRODUCER_ENV = "BFL_DATABRIDGE_PRODUCER"
LAUNCHD_PUBLISHER_ENV = DATABRIDGE_PRODUCER_ENV
LAUNCHD_PUBLISHER_VALUE = DATABRIDGE_LAUNCHD_PRODUCER
PUBLISH_REFRESH_INITIAL_RETRY_DELAY_SEC = 30
PUBLISH_REFRESH_MAX_RETRY_DELAY_SEC = 120
_CHECK_ONLY_STATE_FIELDS = (
    "schema_version",
    "generation_id",
    "refresh_date",
    "refresh_started_at",
    "refreshed_at",
    "published_at",
    "source_mode",
    "stability_rounds",
    "business_digest",
)
_CHECK_ONLY_PROVENANCE_FIELDS = (
    "provenance_version",
    "feature_date",
    "source_rdate_cutoff",
    "snapshot_started_at",
    "source_commit_token",
    "source_evidence_sha256",
)
_CHECK_ONLY_ATTEMPT_FIELDS = (
    "status",
    "refresh_date",
    "started_at",
    "finished_at",
    "duration_sec",
)


class _ReadyGateWriteError(RuntimeError):
    """ready gate 写入失败不应触发 refresh retry。"""


def expected_daily_date(*, refresh_date: str) -> str:
    """通过平台唯一共享交易日历计算本次 feature date。"""
    engine = create_sqlalchemy_engine()
    try:
        return get_calendar(engine).previous_trading_day(refresh_date)
    finally:
        engine.dispose()


def refresh_current(
    *,
    refresh_date: str,
    expected_feature_date: str,
    publish: bool,
    config: DataBridgeRefreshConfig,
    deadline_at: datetime | None = None,
):
    """仅通过本机 MySQL source 完成完整 DataBridge refresh。"""
    one_shot_publisher = (
        publish
        and os.getenv(DATABRIDGE_PRODUCER_ENV)
        in DATABRIDGE_ONE_SHOT_PRODUCERS
    )
    engine = create_sqlalchemy_engine()
    try:
        if publish:
            invalidate_ready_blackbox_snapshot()
        continuity_authority = (
            resolve_databridge_continuity_authority_from_engine(
                config,
                feature_date=expected_feature_date,
                engine=engine,
                allow_legacy_v1_period_fallback=(
                    one_shot_publisher
                ),
                allow_producer_period_bootstrap=one_shot_publisher,
            )
        )
        refresh_kwargs = {
            "config": config,
            "expected_daily_date": expected_feature_date,
            "refresh_date": refresh_date,
            "publish": publish,
            "continuity_authority": continuity_authority,
            "round_builder": MySqlDataBridgeRoundBuilder(
                engine=engine,
                config=config,
            ),
            "require_launchd_round_builder": True,
        }
        if deadline_at is not None:
            refresh_kwargs["deadline_at"] = deadline_at
        result = run_full_refresh(**refresh_kwargs)
        if result.published:
            prepare_blackbox_generation_snapshot(
                state=result.state,
                dataset=result.dataset,
                schema_path=config.schema_path,
            )
        return result
    finally:
        engine.dispose()


def check_current(*, refresh_date: str):
    config = DataBridgeRefreshConfig.from_env()
    return check_current_dataset(
        config,
        required_refresh_date=refresh_date,
        expected_daily_date=expected_daily_date(refresh_date=refresh_date),
        strict_read_only=True,
        require_source_provenance=True,
    )


def _checked_at() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
        timespec="seconds"
    )


def _now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _refresh_deadline_at(
    config: DataBridgeRefreshConfig,
    *,
    refresh_date: str,
) -> datetime:
    return config.deadline_at(refresh_date)


def _write_blocked_gate(
    config: DataBridgeRefreshConfig,
    *,
    refresh_date: str,
    expected_feature_date: str,
    check_name: str,
) -> None:
    write_gate_record(
        config,
        run_date=refresh_date,
        status="blocked",
        checked_at=_checked_at(),
        generation_id=None,
        refresh_date=None,
        expected_daily_date=expected_feature_date,
        business_digest=None,
        checks=[{"name": check_name, "status": "blocked"}],
    )


def _write_ready_gate(
    config: DataBridgeRefreshConfig,
    *,
    refresh_date: str,
    expected_feature_date: str,
    state: Mapping[str, object],
) -> None:
    write_gate_record(
        config,
        run_date=refresh_date,
        status="ready",
        checked_at=_checked_at(),
        generation_id=str(state["generation_id"]),
        refresh_date=str(state["refresh_date"]),
        expected_daily_date=expected_feature_date,
        business_digest=str(state["business_digest"]),
        checks=[
            {"name": "local_mysql_refresh", "status": "passed"},
            {"name": "generation_snapshot_ready", "status": "passed"},
        ],
    )


def _check_only_state(state: Mapping[str, object]) -> dict[str, object]:
    """check-only 只公开经过白名单筛选的运行证据。"""
    result: dict[str, object] = {}
    for field in _CHECK_ONLY_STATE_FIELDS:
        value = state.get(field)
        if field in state and _is_check_only_scalar(value):
            result[field] = value

    source_provenance = state.get("source_provenance")
    if isinstance(source_provenance, Mapping):
        safe_provenance = {
            field: source_provenance[field]
            for field in _CHECK_ONLY_PROVENANCE_FIELDS
            if field in source_provenance
            and _is_check_only_scalar(source_provenance[field])
        }
        if safe_provenance:
            result["source_provenance"] = safe_provenance

    last_attempt = state.get("last_attempt")
    if not isinstance(last_attempt, Mapping):
        return result
    safe_attempt = {
        field: last_attempt[field]
        for field in _CHECK_ONLY_ATTEMPT_FIELDS
        if field in last_attempt and _is_check_only_scalar(last_attempt[field])
    }
    error = last_attempt.get("error")
    if "error" in last_attempt and error is None:
        safe_attempt["error"] = None
    elif not (
        isinstance(error, str)
        and error in FAILED_ATTEMPT_ERROR_CATEGORIES
    ):
        safe_attempt["error"] = "refresh_failed"
    result["last_attempt"] = safe_attempt
    return result


def _is_check_only_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _try_write_blocked_gate(
    config: DataBridgeRefreshConfig,
    *,
    refresh_date: str,
    expected_feature_date: str,
    check_name: str,
) -> bool:
    """尽力将 gate 置 blocked，绝不让二次 I/O 覆盖主失败。"""
    try:
        _write_blocked_gate(
            config,
            refresh_date=refresh_date,
            expected_feature_date=expected_feature_date,
            check_name=check_name,
        )
    except Exception:
        return False
    return True


@contextmanager
def _publisher_lock(config: DataBridgeRefreshConfig):
    """避免多个 one-shot publisher 交错改写同日 gate/current。"""
    lock_directory = Path(config.runtime_root) / "v2_scheduler_gate"
    lock_directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock_path = lock_directory / "publisher.lock"
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


# 异常类型是安全的分类事实：它由代码结构决定，不含 DSN、凭据或驱动上下文。
# 顺序从具体到一般——第一个匹配即为该次失败的类别。
_FAILURE_CATEGORIES: tuple[tuple[type[BaseException], str], ...] = (
    (DataBridgeCurrentMissingError, "current_dataset_missing"),
    (DataBridgeCurrentInvalidError, "current_dataset_invalid"),
    (DataBridgeCurrentReadError, "current_dataset_read_failed"),
    (
        DataBridgeContinuityIdentityError,
        "continuity_identity_invalid",
    ),
    (DataBridgeRecoveryIdentityError, "current_dataset_invalid"),
    (DataBridgeStoreLockError, "store_lock_conflict"),
    (DataBridgeRefreshError, "refresh_failed"),
    (DataBridgeValidationError, "validation_failed"),
)


def _failure_category(exc: BaseException) -> str:
    """把异常类型映射成稳定类别码，绝不携带异常自身的文本。"""
    for exc_type, category in _FAILURE_CATEGORIES:
        if isinstance(exc, exc_type):
            return category
    if isinstance(exc, (OSError, ValueError, SQLAlchemyError)):
        return "configuration_error"
    return "unexpected_error"


def _is_retryable_refresh_error(exc: BaseException) -> bool:
    """只允许 refresh/validation 瞬时失败进入同进程重试。"""
    return isinstance(
        exc,
        (DataBridgeRefreshError, DataBridgeValidationError),
    ) and not isinstance(exc, DataBridgeNonRetryableRefreshError)


def _configuration_error(mode: Mode) -> tuple[int, dict[str, object]]:
    return 2, {
        "status": "configuration_error",
        "mode": mode,
        "failure_category": "configuration_error",
        "error": "local MySQL DataBridge configuration is invalid or incomplete",
    }


def _refresh_failure(
    mode: Mode, *, failure_category: str = "refresh_failed"
) -> tuple[int, dict[str, object]]:
    return 1, {
        "status": "failed",
        "mode": mode,
        "failure_category": failure_category,
        "error": "local MySQL DataBridge refresh failed",
    }


def _run_refresh_with_config(
    mode: Mode,
    *,
    refresh_date: str,
    config: DataBridgeRefreshConfig,
    expected_feature_date: str,
    deadline_at: datetime | None = None,
) -> tuple[int, dict[str, object]]:
    """在 publish caller 已持有 publisher lock 时执行刷新与 gate 事务。"""
    try:
        if mode == "publish" and not _try_write_blocked_gate(
            config,
            refresh_date=refresh_date,
            expected_feature_date=expected_feature_date,
            check_name="refresh_pending",
        ):
            return 1, {
                "status": "failed",
                "mode": mode,
                "error": "local MySQL DataBridge gate write failed",
            }
        refresh_kwargs = {
            "refresh_date": refresh_date,
            "expected_feature_date": expected_feature_date,
            "publish": mode == "publish",
            "config": config,
        }
        if deadline_at is not None:
            refresh_kwargs["deadline_at"] = deadline_at
        result = refresh_current(
            **refresh_kwargs,
        )
        if mode == "publish":
            try:
                _write_ready_gate(
                    config,
                    refresh_date=refresh_date,
                    expected_feature_date=expected_feature_date,
                    state=result.state,
                )
            except Exception as exc:
                raise _ReadyGateWriteError(
                    "local MySQL DataBridge ready gate write failed"
                ) from exc
        return 0, {
            "status": "ok",
            "mode": mode,
            "published": result.published,
            "rounds_completed": result.rounds_completed,
            "duration_sec": round(result.duration_sec, 3),
            "state": result.state,
        }
    except (DataBridgeRefreshError, DataBridgeValidationError) as exc:
        category = _failure_category(exc)
        if mode == "publish":
            _try_write_blocked_gate(
                config,
                refresh_date=refresh_date,
                expected_feature_date=expected_feature_date,
                check_name=category,
            )
        return 1, {
            "status": "failed",
            "mode": mode,
            "failure_category": category,
            "error": "local MySQL DataBridge refresh or validation failed",
            "retryable": _is_retryable_refresh_error(exc),
        }
    except (OSError, ValueError, SQLAlchemyError) as exc:
        category = _failure_category(exc)
        if mode == "publish":
            _try_write_blocked_gate(
                config,
                refresh_date=refresh_date,
                expected_feature_date=expected_feature_date,
                check_name=category,
            )
        return _configuration_error(mode)
    except _ReadyGateWriteError:
        raise
    except Exception:
        # CLI 是操作面边界：未知运行时异常不得把 DSN、凭据或驱动上下文
        # 透传给 launchd 日志。KeyboardInterrupt/SystemExit 不属于 Exception，
        # 仍保留其进程控制语义。
        if mode == "publish":
            _try_write_blocked_gate(
                config,
                refresh_date=refresh_date,
                expected_feature_date=expected_feature_date,
                check_name="unexpected_error",
            )
        return _refresh_failure(mode, failure_category="unexpected_error")


def _run_publish_with_retries(
    *,
    refresh_date: str,
    config: DataBridgeRefreshConfig,
    expected_feature_date: str,
) -> tuple[int, dict[str, object]]:
    """在同一发布进程内，于已有 refresh deadline 前有限重试。"""
    deadline_at = _refresh_deadline_at(config, refresh_date=refresh_date)
    result: tuple[int, dict[str, object]] | None = None
    attempt_count = 0
    retry_delay_sec = PUBLISH_REFRESH_INITIAL_RETRY_DELAY_SEC
    while True:
        if _now() >= deadline_at:
            terminal = (
                result
                if result is not None
                else _refresh_failure("publish")
            )
            return _with_retry_metadata(
                terminal,
                attempt_count=attempt_count,
                retryable=result is not None,
                deadline_reached=True,
            )
        result = _run_refresh_with_config(
            "publish",
            refresh_date=refresh_date,
            config=config,
            expected_feature_date=expected_feature_date,
            deadline_at=deadline_at,
        )
        attempt_count += 1
        exit_code, payload = result
        retryable = (
            exit_code == 1
            and payload.get("retryable") is True
        )
        if not retryable:
            return _with_retry_metadata(
                result,
                attempt_count=attempt_count,
                retryable=False,
                deadline_reached=False,
            )

        remaining_sec = (deadline_at - _now()).total_seconds()
        if remaining_sec <= 0:
            return _with_retry_metadata(
                result,
                attempt_count=attempt_count,
                retryable=True,
                deadline_reached=True,
            )
        time.sleep(min(retry_delay_sec, remaining_sec))
        retry_delay_sec = min(
            retry_delay_sec * 2,
            PUBLISH_REFRESH_MAX_RETRY_DELAY_SEC,
        )


def _with_retry_metadata(
    result: tuple[int, dict[str, object]],
    *,
    attempt_count: int,
    retryable: bool,
    deadline_reached: bool,
) -> tuple[int, dict[str, object]]:
    """向安全 CLI payload 添加有界重试摘要。"""
    exit_code, payload = result
    return exit_code, {
        **payload,
        "attempt_count": attempt_count,
        "retryable": retryable,
        "deadline_reached": deadline_reached,
    }


def run_command(
    mode: Mode,
    *,
    refresh_date: str,
    expected_feature_date: str | None = None,
    refresh_start: str | None = None,
    refresh_deadline: str | None = None,
) -> tuple[int, dict[str, object]]:
    if (
        mode == "publish"
        and os.getenv(DATABRIDGE_PRODUCER_ENV)
        not in DATABRIDGE_ONE_SHOT_PRODUCERS
    ):
        return 2, {
            "status": "configuration_error",
            "mode": mode,
            "error": "local MySQL DataBridge publish requires installed one-shot admission",
        }
    if mode == "check-only":
        try:
            current = check_current(refresh_date=refresh_date)
        except (DataBridgeRefreshError, DataBridgeValidationError) as exc:
            return _refresh_failure(mode, failure_category=_failure_category(exc))
        except (OSError, ValueError, SQLAlchemyError):
            return _configuration_error(mode)
        except Exception:
            return _refresh_failure(mode, failure_category="unexpected_error")
        return 0, {
            "status": "ok",
            "mode": mode,
            "state": _check_only_state(current.state),
        }
    try:
        config = DataBridgeRefreshConfig.from_env(
            refresh_start=refresh_start,
            refresh_deadline=refresh_deadline,
        )
        resolved_feature_date = (
            str(expected_feature_date)
            if expected_feature_date is not None
            else expected_daily_date(refresh_date=refresh_date)
        )
        parsed_refresh_date = datetime.strptime(refresh_date, "%Y-%m-%d").date()
        parsed_feature_date = datetime.strptime(
            resolved_feature_date,
            "%Y-%m-%d",
        ).date()
        if parsed_feature_date > parsed_refresh_date:
            raise ValueError("expected feature date cannot exceed refresh date")
    except (DataBridgeRefreshError, DataBridgeValidationError) as exc:
        return _refresh_failure(mode, failure_category=_failure_category(exc))
    except (OSError, ValueError, SQLAlchemyError):
        return _configuration_error(mode)
    except Exception:
        return _refresh_failure(mode, failure_category="unexpected_error")
    if mode != "publish":
        return _run_refresh_with_config(
            mode,
            refresh_date=refresh_date,
            config=config,
            expected_feature_date=resolved_feature_date,
        )
    try:
        with _publisher_lock(config) as acquired:
            if not acquired:
                return 1, {
                    "status": "failed",
                    "mode": mode,
                    "error": "local MySQL DataBridge publisher is already running",
                }
            return _run_publish_with_retries(
                refresh_date=refresh_date,
                config=config,
                expected_feature_date=resolved_feature_date,
            )
    except _ReadyGateWriteError:
        _try_write_blocked_gate(
            config,
            refresh_date=refresh_date,
            expected_feature_date=resolved_feature_date,
            check_name="refresh_failed",
        )
        return 1, {
            "status": "failed",
            "mode": mode,
            "error": "local MySQL DataBridge refresh or validation failed",
        }
    except (OSError, ValueError, SQLAlchemyError):
        return _configuration_error(mode)
    except Exception:
        return _refresh_failure(mode)


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--publish", action="store_true")
    modes.add_argument("--check-only", action="store_true")
    parser.add_argument("--date", default=_today(), help="刷新日期，格式 YYYY-MM-DD")
    args = parser.parse_args()
    mode: Mode = "publish" if args.publish else "check-only" if args.check_only else "dry-run"
    exit_code, payload = run_command(mode, refresh_date=args.date)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
