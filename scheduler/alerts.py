"""日批 SLA 的结构化、本机且可扩展告警出口。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


logger = logging.getLogger(__name__)
_SEVERITIES = {"warning", "error", "critical"}
_MAX_MESSAGE_LENGTH = 2000
_MAX_PAYLOAD_BYTES = 64 * 1024
_SAFE_SUBPROCESS_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_SAFE_INHERITED_ENV_NAMES = ("LANG", "LC_ALL", "LC_CTYPE", "TZ")
_DEFAULT_QUEUE_CAPACITY = 128
_DEFAULT_IDLE_TIMEOUT_SEC = 5.0
_DEFAULT_DETAIL_FIELDS: frozenset[str] = frozenset()
_DETAIL_FIELDS_BY_CODE = {
    "DAILY_ETA_OVERLINE": frozenset(
        {
            "accepted_target_count",
            "eta_basis",
            "eta_overline",
            "expected_target_count",
            "late_source_writes",
            "late_source_writes_skipped_reason",
            "optimistic_remaining_sec",
            "projected_finish_at",
            "target_ready_at",
            "terminal_missing_scheme_ids",
        }
    ),
    "DAILY_NO_PROGRESS": frozenset(
        {
            "accepted_target_count",
            "eta_basis",
            "eta_overline",
            "expected_target_count",
            "late_source_writes",
            "late_source_writes_skipped_reason",
            "optimistic_remaining_sec",
            "projected_finish_at",
            "target_ready_at",
            "terminal_missing_scheme_ids",
        }
    ),
    "DAILY_CONTROL_PLANE_DRIFT": frozenset(
        {"error_type", "stage"}
    ),
    "DAILY_OCCURRENCE_MISSING": frozenset({"stage"}),
    "DAILY_TARGET_SLA_BREACHED": frozenset(
        {
            "accepted_target_count",
            "expected_target_count",
            "late_source_writes",
            "late_source_writes_skipped_reason",
            "sla_outcome",
            "sla_reason",
        }
    ),
    "DATABRIDGE_READINESS_LATE": frozenset(
        {
            "databridge_generation_id",
            "databridge_readiness_guardrail_at",
            "databridge_readiness_reason",
            "databridge_readiness_sealed_at",
            "databridge_readiness_status",
            "stage",
        }
    ),
    "DATA_CONTRACT_BREACH": frozenset({"findings", "stage"}),
    "GENERATION_BUILD_FAILED": frozenset(
        {
            "failed_scheme_ids",
            "failure_codes",
            "generations",
            "reason",
            "stage",
        }
    ),
    "GENERATION_HASH_MISMATCH": frozenset(
        {
            "failed_scheme_ids",
            "failure_codes",
            "generation_id",
            "generation_type",
            "reason",
            "stage",
        }
    ),
    "GENERATION_INVALIDATED": frozenset(
        {
            "failed_scheme_ids",
            "failure_codes",
            "generation_id",
            "generation_type",
            "reason",
            "stage",
        }
    ),
    "GENERATION_STORAGE_UNAVAILABLE": frozenset(
        {
            "databridge_generation_count",
            "databridge_total_bytes",
            "error_type",
            "native_generation_count",
            "native_total_bytes",
            "reason",
            "stage",
        }
    ),
    "ITEM_EXECUTION_AUDIT_FAILURE": frozenset({"error_type", "stage"}),
    "ITEM_TERMINAL_FAILURE": frozenset(
        {"error_type", "failure_code", "reason", "stage"}
    ),
    "NATIVE_GENERATION_UNSUPPORTED": frozenset({"reason", "stage"}),
    "ORPHAN_CLEANUP_UNCONFIRMED": frozenset(
        {"reason", "scheme_ids", "stage"}
    ),
    "RECOVERY_CUTOFF_INCOMPLETE": frozenset(
        {
            "accepted_target_count",
            "expected_target_count",
            "expired_item_count",
            "late_source_writes",
            "late_source_writes_skipped_reason",
        }
    ),
    "SLA_TARGET_MISSING": frozenset({"missing_registry_ids", "stage"}),
    "V2_START_LATE": frozenset({"reason", "stage"}),
}
_IDEMPOTENCY_DETAIL_FIELDS = frozenset(
    {
        "error_type",
        "failure_code",
        "failure_codes",
        "generation_id",
        "generation_type",
        "reason",
        "sla_reason",
        "stage",
    }
)
_FINDING_FIELDS = frozenset(
    {"late_row_count", "latest_write_at", "table_name"}
)
_CREDENTIAL_URL_PATTERN = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)"
    r"([^/\s:@]+):([^@\s/]+)@"
)
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*)"
    r"(?:bearer\s+)?[^\s,;]+"
)
_SECRET_KEY_PATTERN = (
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|token|"
    r"password|passwd|secret|authorization"
)
_SECRET_QUOTED_ASSIGNMENT_PATTERN = re.compile(
    rf"""
    (?P<prefix>
        (?P<key_quote>["']?)
        (?P<key>{_SECRET_KEY_PATTERN})
        (?P=key_quote)
        \s*[:=]\s*
    )
    (?P<value_quote>["'])
    (?P<value>(?:\\.|(?!(?P=value_quote)).)*)
    (?P=value_quote)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_SECRET_BARE_ASSIGNMENT_PATTERN = re.compile(
    rf"""
    (?P<prefix>
        (?P<key_quote>["']?)
        (?P<key>{_SECRET_KEY_PATTERN})
        (?P=key_quote)
        \s*[:=]\s*
    )
    (?!["'\[])
    [^\s,;}}\]]+
    """,
    re.IGNORECASE | re.VERBOSE,
)
_SENSITIVE_PATH_PATTERN = re.compile(
    r"(?<![\w:])/(?:Users|Volumes|home|private|var|tmp|opt|etc)"
    r"(?:/[^\s,;\"'()\[\]{}]+)+"
)


@dataclass(frozen=True)
class AlertEvent:
    """一个可审计的日批告警事件。"""

    code: str
    severity: str
    occurred_at: datetime
    predict_date: str
    occurrence_id: int | None
    message: str
    scheme_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str)
            or not self.code.strip()
            or len(self.code) > 64
        ):
            raise ValueError("alert code must be non-empty and <= 64 chars")
        if self.severity not in _SEVERITIES:
            raise ValueError(
                f"alert severity must be one of {sorted(_SEVERITIES)}"
            )
        if (
            self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise ValueError("alert occurred_at must be timezone-aware")
        if (
            date.fromisoformat(self.predict_date).isoformat()
            != self.predict_date
        ):
            raise ValueError("alert predict_date must be canonical YYYY-MM-DD")
        if self.occurrence_id is not None and self.occurrence_id <= 0:
            raise ValueError("alert occurrence_id must be positive")
        if (
            not isinstance(self.message, str)
            or not self.message.strip()
            or len(self.message) > _MAX_MESSAGE_LENGTH
        ):
            raise ValueError(
                "alert message must be non-empty and <= "
                f"{_MAX_MESSAGE_LENGTH} chars"
            )
        if self.scheme_id is not None and (
            not self.scheme_id.strip() or len(self.scheme_id) > 128
        ):
            raise ValueError("alert scheme_id must be non-empty and bounded")
        try:
            json.dumps(
                dict(self.details),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("alert details must be JSON serializable") from exc

    @classmethod
    def now(
        cls,
        *,
        code: str,
        severity: str,
        predict_date: str,
        occurrence_id: int | None,
        message: str,
        scheme_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> "AlertEvent":
        """以受信 UTC clock 创建事件。"""
        return cls(
            code=code,
            severity=severity,
            occurred_at=datetime.now(timezone.utc),
            predict_date=predict_date,
            occurrence_id=occurrence_id,
            scheme_id=scheme_id,
            message=message,
            details=dict(details or {}),
        )

    def payload(self) -> dict[str, Any]:
        """返回稳定、无 traceback 的 JSON payload。"""
        occurred_at = (
            self.occurred_at.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        safe_details = _safe_alert_details(self.code, self.details)
        safe_message = _redact_text(self.message)
        identity = {
            "code": self.code,
            "predict_date": self.predict_date,
            "occurrence_id": self.occurrence_id,
            "scheme_id": self.scheme_id,
            "details": {
                key: safe_details[key]
                for key in sorted(_IDEMPOTENCY_DETAIL_FIELDS)
                if key in safe_details
            },
        }
        idempotency_key = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "event": "daily_sla_alert",
            "code": self.code,
            "severity": self.severity,
            "occurred_at": occurred_at,
            "predict_date": self.predict_date,
            "occurrence_id": self.occurrence_id,
            "scheme_id": self.scheme_id,
            "message": safe_message,
            "details": safe_details,
            "idempotency_key": idempotency_key,
        }


@dataclass(frozen=True)
class AlertSettings:
    """告警出口配置；命令必须是无 shell 的绝对 argv。"""

    command: tuple[Path | str, ...] | None = None
    notification_center: bool = True
    timeout_sec: int = 10
    config_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.timeout_sec <= 0 or self.timeout_sec > 60:
            raise ValueError("alert timeout_sec must be in 1..60")
        if (
            not isinstance(self.config_errors, tuple)
            or any(
                not isinstance(error, str) or not error
                for error in self.config_errors
            )
        ):
            raise ValueError("alert config_errors must be a tuple of codes")
        if self.command is None:
            return
        if not isinstance(self.command, tuple) or not self.command:
            raise ValueError("alert command must be a non-empty tuple")
        executable = Path(str(self.command[0]))
        if not executable.is_absolute():
            raise ValueError("alert command executable must be absolute")
        if any(
            not isinstance(value, (str, Path))
            or "\x00" in str(value)
            for value in self.command
        ):
            raise ValueError("alert command argv contains an invalid value")

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "AlertSettings":
        """从 JSON-compatible mapping 加载，不解析 shell 字符串。"""
        raw_command = value.get("command")
        if raw_command is None:
            command = None
        elif (
            isinstance(raw_command, list)
            and raw_command
            and all(isinstance(item, str) for item in raw_command)
        ):
            command = tuple(
                Path(item) if index == 0 else str(item)
                for index, item in enumerate(raw_command)
            )
        else:
            raise ValueError("alert command must be a JSON array of argv")
        notification = value.get("notification_center", True)
        if not isinstance(notification, bool):
            raise ValueError("notification_center must be boolean")
        raw_timeout = value.get("timeout_sec", 10)
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int):
            raise ValueError("alert timeout_sec must be an integer")
        return cls(
            command=command,
            notification_center=notification,
            timeout_sec=raw_timeout,
        )

    @classmethod
    def from_env(cls) -> "AlertSettings":
        """加载可选出口；坏配置只禁用对应出口并留下错误码。"""
        config_errors: list[str] = []
        raw = os.getenv("BOND_ALERT_SINK_COMMAND_JSON")
        command: tuple[Path | str, ...] | None = None
        if raw:
            try:
                raw_command = json.loads(raw)
            except json.JSONDecodeError:
                config_errors.append("SINK_COMMAND_INVALID_JSON")
            else:
                try:
                    command = cls.from_mapping(
                        {
                            "command": raw_command,
                            "notification_center": False,
                        }
                    ).command
                except (TypeError, ValueError):
                    config_errors.append("SINK_COMMAND_INVALID_ARGV")
        notification_raw = os.getenv(
            "BOND_ALERT_NOTIFICATION_CENTER",
            "true",
        ).strip().lower()
        if notification_raw in {"true", "false", "1", "0"}:
            notification_center = notification_raw in {"true", "1"}
        else:
            notification_center = False
            config_errors.append(
                "NOTIFICATION_CENTER_INVALID_BOOLEAN"
            )
        timeout_raw = os.getenv("BOND_ALERT_TIMEOUT_SEC", "10")
        try:
            timeout = int(timeout_raw)
        except ValueError:
            timeout = 10
            config_errors.append("TIMEOUT_INVALID_INTEGER")
        if timeout <= 0 or timeout > 60:
            timeout = 10
            config_errors.append("TIMEOUT_OUT_OF_RANGE")
        return cls(
            command=command,
            notification_center=notification_center,
            timeout_sec=timeout,
            config_errors=tuple(config_errors),
        )


@dataclass(frozen=True)
class AlertDispatchResult:
    """各本机告警出口的 best-effort 结果。"""

    command_status: str
    notification_status: str


@dataclass(frozen=True)
class _AlertDelivery:
    payload_text: str
    notification_message: str


_STOP_DELIVERY = object()


class AlertDispatcher:
    """同步审计、非阻塞入队，由专用有界后台通道发送。"""

    def __init__(
        self,
        settings: AlertSettings,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = (
            subprocess.run
        ),
        logger: logging.Logger = logger,
        queue_capacity: int = _DEFAULT_QUEUE_CAPACITY,
        idle_timeout_sec: float = _DEFAULT_IDLE_TIMEOUT_SEC,
    ) -> None:
        if (
            isinstance(queue_capacity, bool)
            or not isinstance(queue_capacity, int)
            or queue_capacity <= 0
        ):
            raise ValueError("alert queue_capacity must be a positive integer")
        if (
            isinstance(idle_timeout_sec, bool)
            or not isinstance(idle_timeout_sec, (int, float))
            or idle_timeout_sec <= 0
        ):
            raise ValueError(
                "alert idle_timeout_sec must be a positive number"
            )
        self._settings = settings
        self._runner = runner
        self._logger = logger
        self._queue_capacity = queue_capacity
        self._idle_timeout_sec = float(idle_timeout_sec)
        self._queue: queue.Queue[_AlertDelivery | object] = queue.Queue(
            maxsize=queue_capacity
        )
        self._condition = threading.Condition()
        self._accepted_count = 0
        self._completed_count = 0
        self._accepting = True
        self._stop_enqueued = False
        self._worker: threading.Thread | None = None
        for error_code in settings.config_errors:
            self._log_json(
                {
                    "event": "alert_configuration_error",
                    "error_code": error_code,
                }
            )

    def dispatch(self, event: AlertEvent) -> AlertDispatchResult:
        """审计后立即入队；外部 sink 永不阻塞调用线程。"""
        payload = event.payload()
        payload_text = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(payload_text.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise ValueError("alert payload exceeds safe size limit")
        log_method = (
            self._logger.warning
            if event.severity == "warning"
            else self._logger.error
        )
        log_method(payload_text)

        command_enabled = self._settings.command is not None
        notification_enabled = self._settings.notification_center
        if not command_enabled and not notification_enabled:
            return AlertDispatchResult(
                command_status="disabled",
                notification_status="disabled",
            )
        delivery = _AlertDelivery(
            payload_text=payload_text,
            notification_message=str(payload["message"]),
        )
        with self._condition:
            if not self._accepting:
                queued = False
                drop_reason = "dispatcher_closed"
            else:
                self._ensure_worker_started_locked()
                try:
                    self._queue.put_nowait(delivery)
                except queue.Full:
                    queued = False
                    drop_reason = "queue_full"
                else:
                    queued = True
                    drop_reason = None
                    self._accepted_count += 1
        if not queued:
            self._log_json(
                {
                    "event": "alert_delivery_dropped",
                    "reason": drop_reason,
                    "queue_capacity": self._queue_capacity,
                    "code": payload["code"],
                    "predict_date": payload["predict_date"],
                    "occurrence_id": payload["occurrence_id"],
                    "idempotency_key": payload["idempotency_key"],
                }
            )
        status = "queued" if queued else "dropped"
        return AlertDispatchResult(
            command_status=status if command_enabled else "disabled",
            notification_status=(
                status if notification_enabled else "disabled"
            ),
        )

    @property
    def worker_alive(self) -> bool:
        """供停机与测试确认后台线程已退出。"""
        with self._condition:
            return (
                self._worker is not None
                and self._worker.is_alive()
            )

    def drain(self, *, timeout_sec: float | None = None) -> bool:
        """等待调用前已接受的事件完成；超时返回 False。"""
        deadline = _deadline_after(timeout_sec)
        with self._condition:
            target = self._accepted_count
            while self._completed_count < target:
                remaining = _remaining_seconds(deadline)
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def close(
        self,
        *,
        drain: bool = True,
        timeout_sec: float | None = None,
    ) -> bool:
        """停止接受新事件，可选排空后关闭后台线程。"""
        deadline = _deadline_after(timeout_sec)
        with self._condition:
            self._accepting = False
        if drain and not self.drain(
            timeout_sec=_remaining_seconds(deadline)
        ):
            return False
        with self._condition:
            worker = self._worker
            if worker is None:
                return True
            if not self._stop_enqueued:
                try:
                    self._queue.put_nowait(_STOP_DELIVERY)
                except queue.Full:
                    return False
                self._stop_enqueued = True
        worker.join(timeout=_remaining_seconds(deadline))
        return not worker.is_alive()

    def _ensure_worker_started_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="bond-factor-lab-alert-dispatcher",
            daemon=True,
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            try:
                delivery = self._queue.get(
                    timeout=self._idle_timeout_sec
                )
            except queue.Empty:
                with self._condition:
                    if not self._queue.empty():
                        continue
                    if self._worker is threading.current_thread():
                        self._worker = None
                        self._condition.notify_all()
                    return
            try:
                if delivery is _STOP_DELIVERY:
                    with self._condition:
                        if self._worker is threading.current_thread():
                            self._worker = None
                            self._condition.notify_all()
                    return
                assert isinstance(delivery, _AlertDelivery)
                if self._settings.command is not None:
                    self._deliver(
                        "command",
                        [
                            str(value)
                            for value in self._settings.command
                        ],
                        input_text=delivery.payload_text + "\n",
                    )
                if self._settings.notification_center:
                    self._deliver(
                        "notification_center",
                        _notification_command(
                            delivery.notification_message
                        ),
                        input_text=None,
                    )
            finally:
                self._queue.task_done()
                if delivery is not _STOP_DELIVERY:
                    with self._condition:
                        self._completed_count += 1
                        self._condition.notify_all()

    def _deliver(
        self,
        sink: str,
        command: Sequence[str],
        *,
        input_text: str | None,
    ) -> str:
        try:
            self._runner(
                list(command),
                input=input_text,
                text=True,
                capture_output=True,
                timeout=self._settings.timeout_sec,
                check=True,
                shell=False,
                env=_minimal_subprocess_env(),
            )
            return "sent"
        except (
            OSError,
            subprocess.SubprocessError,
        ) as exc:
            failure = {
                "event": "alert_delivery_failure",
                "sink": sink,
                "error_type": type(exc).__name__,
            }
            self._log_json(failure)
            return "failed"

    def _log_json(self, payload: Mapping[str, Any]) -> None:
        self._logger.error(
            json.dumps(
                dict(payload),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )


def _minimal_subprocess_env() -> dict[str, str]:
    """只传递本机命令需要的固定 PATH 与非敏感 locale/timezone。"""
    environment = {"PATH": _SAFE_SUBPROCESS_PATH}
    for name in _SAFE_INHERITED_ENV_NAMES:
        value = os.environ.get(name)
        if value and "\x00" not in value:
            environment[name] = value
    return environment


def _deadline_after(timeout_sec: float | None) -> float | None:
    if timeout_sec is None:
        return None
    if (
        isinstance(timeout_sec, bool)
        or not isinstance(timeout_sec, (int, float))
        or timeout_sec < 0
    ):
        raise ValueError("alert timeout must be a non-negative number")
    return time.monotonic() + timeout_sec


def _remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _safe_alert_details(
    code: str,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    """按事件类型只保留稳定诊断字段，并递归脱敏其值。"""
    allowed = _DETAIL_FIELDS_BY_CODE.get(code, _DEFAULT_DETAIL_FIELDS)
    return {
        key: _safe_detail_value(key, details[key])
        for key in sorted(allowed)
        if key in details
    }


def _safe_detail_value(key: str, value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        nested_allowed = (
            _FINDING_FIELDS
            if key in {"findings", "late_source_writes"}
            else frozenset()
        )
        return {
            nested_key: _safe_detail_value(nested_key, value[nested_key])
            for nested_key in sorted(nested_allowed)
            if nested_key in value
        }
    if isinstance(value, (list, tuple)):
        return [_safe_detail_value(key, item) for item in value]
    return _redact_text(str(value))


def _redact_text(value: str) -> str:
    """移除凭据、授权头和本机敏感绝对路径。"""
    redacted = _CREDENTIAL_URL_PATTERN.sub(
        lambda match: f"{match.group(1)}[REDACTED]@",
        value,
    )
    redacted = _AUTHORIZATION_PATTERN.sub(
        lambda match: f"{match.group(1)}[REDACTED]",
        redacted,
    )
    redacted = _SECRET_QUOTED_ASSIGNMENT_PATTERN.sub(
        lambda match: (
            f"{match.group('prefix')}"
            f"{match.group('value_quote')}[REDACTED]"
            f"{match.group('value_quote')}"
        ),
        redacted,
    )
    redacted = _SECRET_BARE_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        redacted,
    )
    return _SENSITIVE_PATH_PATTERN.sub("[REDACTED_PATH]", redacted)


def _notification_command(message: str) -> list[str]:
    script = (
        "on run argv\n"
        "display notification (item 1 of argv) "
        "with title (item 2 of argv)\n"
        "end run"
    )
    return [
        "/usr/bin/osascript",
        "-e",
        script,
        "--",
        message,
        "Bond Factor Lab",
    ]
