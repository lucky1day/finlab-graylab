"""DataBridge refresh 失败保留稳定类别且不泄露错误细节。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from shared.data_bridge.refresh import (
    DataBridgeCurrentInvalidError,
    DataBridgeCurrentMissingError,
    DataBridgeCurrentReadError,
    DataBridgeRefreshError,
)
from shared.data_bridge.validation import DataBridgeValidationError


CASES = [
    (DataBridgeCurrentMissingError("secret-dsn://user:pw@host"), "current_dataset_missing", 1),
    (DataBridgeCurrentInvalidError("secret-dsn://user:pw@host"), "current_dataset_invalid", 1),
    (DataBridgeCurrentReadError("secret-dsn://user:pw@host"), "current_dataset_read_failed", 1),
    (DataBridgeRefreshError("secret-dsn://user:pw@host"), "refresh_failed", 1),
    (DataBridgeValidationError("secret-dsn://user:pw@host"), "validation_failed", 1),
    (OSError("secret-dsn://user:pw@host"), "configuration_error", 2),
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
    seen: set[str] = set()
    for exc, expected_category, expected_exit in CASES:
        exit_code, payload, gate_writes = _run(exc)
        assert payload["failure_category"] == expected_category
        assert exit_code == expected_exit
        assert gate_writes[-1] == expected_category
        blob = repr(payload) + repr(gate_writes)
        assert "secret-dsn" not in blob
        assert "user:pw" not in blob
        seen.add(str(payload["failure_category"]))
    assert len(seen) == len(CASES), "类别不得互相折叠"


def test_sqlalchemy_error_is_configuration_not_unexpected() -> None:
    exc = OperationalError("SELECT 1", {}, Exception("secret-dsn://user:pw@host"))

    exit_code, payload, _ = _run(exc)

    assert payload["failure_category"] == "configuration_error"
    assert exit_code == 2
    assert "secret-dsn" not in repr(payload)


def test_safe_error_text_is_preserved() -> None:
    """分类不替换既有安全 error 文本。"""
    _, payload, _ = _run(DataBridgeRefreshError("boom"))

    assert "error" in payload
    assert payload["status"] == "failed"


def test_publish_ready_gate_does_not_revalidate_published_csv() -> None:
    from scripts import refresh_data_bridge_current as script

    result = SimpleNamespace(
        published=True,
        rounds_completed=2,
        duration_sec=1.0,
        state={
            "generation_id": "generation-ready",
            "refresh_date": "2026-08-11",
            "business_digest": "b" * 64,
        },
    )
    config = SimpleNamespace()
    with (
        patch.object(script, "_try_write_blocked_gate", return_value=True),
        patch.object(script, "refresh_current", return_value=result),
        patch.object(
            script,
            "check_current_dataset",
            side_effect=AssertionError("published CSV must not be revalidated"),
        ) as check_current,
        patch.object(script, "_write_ready_gate") as write_ready,
    ):
        exit_code, payload = script._run_refresh_with_config(
            "publish",
            refresh_date="2026-08-11",
            config=config,
            expected_feature_date="2026-08-10",
        )

    assert exit_code == 0
    assert payload["status"] == "ok"
    check_current.assert_not_called()
    write_ready.assert_called_once_with(
        config,
        refresh_date="2026-08-11",
        expected_feature_date="2026-08-10",
        state=result.state,
    )
