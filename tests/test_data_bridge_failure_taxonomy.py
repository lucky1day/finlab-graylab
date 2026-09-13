"""DataBridge refresh 失败保留稳定类别且不泄露错误细节。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import OperationalError

from shared.data_bridge.refresh import (
    DataBridgeContinuityIdentityError,
    DataBridgeCurrentInvalidError,
    DataBridgeCurrentMissingError,
    DataBridgeCurrentReadError,
    DataBridgeNonRetryableRefreshError,
    DataBridgeRecoveryIdentityError,
    DataBridgeRefreshError,
    DataBridgeStore,
    DataBridgeStoreLockError,
)
from shared.data_bridge.validation import DataBridgeValidationError


CASES = [
    (DataBridgeCurrentMissingError("secret-dsn://user:pw@host"), "current_dataset_missing", 1),
    (DataBridgeCurrentInvalidError("secret-dsn://user:pw@host"), "current_dataset_invalid", 1),
    (DataBridgeCurrentReadError("secret-dsn://user:pw@host"), "current_dataset_read_failed", 1),
    (DataBridgeContinuityIdentityError("secret-dsn://user:pw@host"), "continuity_identity_invalid", 1),
    (DataBridgeRefreshError("secret-dsn://user:pw@host"), "refresh_failed", 1),
    (DataBridgeValidationError("secret-dsn://user:pw@host"), "validation_failed", 1),
    (OSError("secret-dsn://user:pw@host"), "configuration_error", 2),
    (OperationalError("SELECT 1", {}, Exception("secret-dsn://user:pw@host")), "configuration_error", 2),
    (RuntimeError("secret-dsn://user:pw@host"), "unexpected_error", 1),
]


def _run(exc: BaseException):
    from scripts import refresh_data_bridge_current as script

    gate_writes: list[str] = []

    def capture_gate(_config, **kwargs):
        gate_writes.append(str(kwargs.get("check_name")))
        return True

    with (
        patch.object(script, "_try_write_blocked_gate", side_effect=capture_gate),
        patch.object(script, "refresh_current", side_effect=exc),
    ):
        exit_code, payload = script._run_refresh_with_config(
            "publish",
            refresh_date="2026-08-11",
            config=SimpleNamespace(),
            expected_feature_date="2026-08-10",
        )
    return exit_code, payload, gate_writes


def test_each_failure_keeps_category_gate_evidence_and_redaction() -> None:
    for exc, expected_category, expected_exit in CASES:
        exit_code, payload, gate_writes = _run(exc)
        assert payload["failure_category"] == expected_category
        assert exit_code == expected_exit
        assert gate_writes[-1] == expected_category
        blob = repr(payload) + repr(gate_writes)
        assert "secret-dsn" not in blob
        assert "user:pw" not in blob


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


def test_publish_retry_stops_at_deadline() -> None:
    from scripts import refresh_data_bridge_current as script

    clock = _Clock(
        datetime(2026, 8, 11, 6, 59, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    )
    config = SimpleNamespace(
        deadline_at=lambda _refresh_date: datetime(
            2026,
            8,
            11,
            7,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
    )
    failure = (
        1,
        {
            "status": "failed",
            "mode": "publish",
            "error": "local MySQL DataBridge refresh or validation failed",
            "retryable": True,
        },
    )
    with (
        patch.object(script, "_now", side_effect=clock.now),
        patch.object(script.time, "sleep", side_effect=clock.sleep),
        patch.object(
            script,
            "_run_refresh_with_config",
            return_value=failure,
        ) as refresh,
    ):
        code, payload = script._run_publish_with_retries(
            refresh_date="2026-08-11",
            config=config,
            expected_feature_date="2026-08-10",
        )

    assert code == 1
    assert payload["attempt_count"] == 1
    assert payload["deadline_reached"] is True
    assert clock.sleeps == [30]
    assert refresh.call_count == 1


@pytest.mark.parametrize(
    "exc, expected_category, expected_exit",
    [
        (
            DataBridgeCurrentInvalidError("invalid current"),
            "current_dataset_invalid",
            1,
        ),
        (
            DataBridgeContinuityIdentityError("identity drift"),
            "continuity_identity_invalid",
            1,
        ),
        (OSError("configuration failure"), "configuration_error", 2),
    ],
)
def test_publish_does_not_retry_identity_or_configuration_failures(
    exc: BaseException,
    expected_category: str,
    expected_exit: int,
) -> None:
    from scripts import refresh_data_bridge_current as script

    config = SimpleNamespace(
        deadline_at=lambda _refresh_date: datetime(
            2026,
            8,
            11,
            7,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
    )
    with (
        patch.object(
            script,
            "_now",
            return_value=datetime(
                2026,
                8,
                11,
                6,
                30,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
        ),
        patch.object(script, "_try_write_blocked_gate", return_value=True),
        patch.object(script, "refresh_current", side_effect=exc) as refresh,
    ):
        code, payload = script._run_publish_with_retries(
            refresh_date="2026-08-11",
            config=config,
            expected_feature_date="2026-08-10",
        )

    assert code == expected_exit
    assert payload["failure_category"] == expected_category
    assert payload["attempt_count"] == 1
    assert payload["retryable"] is False
    refresh.assert_called_once()


def test_real_store_recovery_identity_failure_is_not_retryable(
    tmp_path: Path,
) -> None:
    from scripts import refresh_data_bridge_current as script

    data_root = tmp_path / "data"
    runtime_root = tmp_path / "runtime"
    data_root.mkdir(mode=0o700)
    runtime_root.mkdir(mode=0o700)
    (data_root / "current").mkdir(mode=0o700)
    store = DataBridgeStore(
        data_root=data_root,
        runtime_root=runtime_root,
    )
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "shared"
        / "blackbox_v2"
        / "data_bridge_v1_schema.json"
    )

    with pytest.raises(DataBridgeRecoveryIdentityError) as caught:
        store.recover(schema_path=schema_path)

    assert isinstance(caught.value, DataBridgeNonRetryableRefreshError)
    assert script._is_retryable_refresh_error(caught.value) is False
    assert script._failure_category(caught.value) == (
        "current_dataset_invalid"
    )


def test_real_store_lock_upgrade_failure_is_not_retryable(
    tmp_path: Path,
) -> None:
    from scripts import refresh_data_bridge_current as script

    data_root = tmp_path / "data"
    runtime_root = tmp_path / "runtime"
    store = DataBridgeStore(
        data_root=data_root,
        runtime_root=runtime_root,
    )

    with store.lock(exclusive=False):
        with pytest.raises(DataBridgeStoreLockError) as caught:
            with store.lock(exclusive=True):
                raise AssertionError("exclusive lock must not be acquired")

    assert isinstance(caught.value, DataBridgeNonRetryableRefreshError)
    assert script._is_retryable_refresh_error(caught.value) is False
    assert script._failure_category(caught.value) == "store_lock_conflict"
