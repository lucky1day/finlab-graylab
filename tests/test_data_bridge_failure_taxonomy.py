"""DataBridge refresh 失败必须保留安全的原因类别。

脱敏与故障分类是两件事。为避免 DSN、凭据和驱动细节进入 launchd 日志，当前把
`DataBridgeRefreshError`、`DataBridgeValidationError` 与未分类异常一并折叠成同
一个泛化文本，gate 侧也只留下 `refresh_failed`。安全边界保护了秘密，却把因果
可观测性一并删掉：恢复动作完全不同的故障共享同一外部表象。

异常层级本身已经编码了分类（`DataBridgeCurrentMissingError` 等），且类型名不含
任何敏感信息——它可以安全地映射成稳定类别码。
"""

from __future__ import annotations

import unittest
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

# (抛出的异常, 期望的 failure_category, 期望的退出码)
CASES = [
    (DataBridgeCurrentMissingError("secret-dsn://user:pw@host"), "current_dataset_missing", 1),
    (DataBridgeCurrentInvalidError("secret-dsn://user:pw@host"), "current_dataset_invalid", 1),
    (DataBridgeCurrentReadError("secret-dsn://user:pw@host"), "current_dataset_read_failed", 1),
    (DataBridgeRefreshError("secret-dsn://user:pw@host"), "refresh_failed", 1),
    (DataBridgeValidationError("secret-dsn://user:pw@host"), "validation_failed", 1),
    (OSError("secret-dsn://user:pw@host"), "configuration_error", 2),
    (RuntimeError("secret-dsn://user:pw@host"), "unexpected_error", 1),
]


class FailureTaxonomyTests(unittest.TestCase):
    def _run(self, exc: BaseException):
        from scripts import refresh_data_bridge_current as script

        gate_writes: list[str] = []

        def _capture_gate(config, **kwargs):
            gate_writes.append(str(kwargs.get("check_name")))
            return True

        with (
            patch.object(script, "_try_write_blocked_gate", side_effect=_capture_gate),
            patch.object(script, "refresh_current", side_effect=exc),
        ):
            exit_code, payload = script._run_refresh_with_config(
                "publish",
                refresh_date="2026-08-11",
                config=SimpleNamespace(),
                expected_feature_date="2026-08-10",
            )
        return exit_code, payload, gate_writes

    def test_each_failure_keeps_category_gate_evidence_and_redaction(self) -> None:
        seen: set[str] = set()
        for exc, expected_category, expected_exit in CASES:
            with self.subTest(exception=type(exc).__name__):
                exit_code, payload, gate_writes = self._run(exc)
                self.assertEqual(payload["failure_category"], expected_category)
                self.assertEqual(exit_code, expected_exit)
                self.assertEqual(gate_writes[-1], expected_category)
                blob = repr(payload) + repr(gate_writes)
                self.assertNotIn("secret-dsn", blob)
                self.assertNotIn("user:pw", blob)
                seen.add(str(payload["failure_category"]))
        self.assertEqual(len(seen), len(CASES), "类别不得互相折叠")

    def test_sqlalchemy_error_is_configuration_not_unexpected(self) -> None:
        exc = OperationalError("SELECT 1", {}, Exception("secret-dsn://user:pw@host"))
        exit_code, payload, _ = self._run(exc)
        self.assertEqual(payload["failure_category"], "configuration_error")
        self.assertEqual(exit_code, 2)
        self.assertNotIn("secret-dsn", repr(payload))

    def test_safe_error_text_is_preserved(self) -> None:
        """既有 error 文本保持不变——分类是新增维度，不是替换。"""
        _, payload, _ = self._run(DataBridgeRefreshError("boom"))
        self.assertIn("error", payload)
        self.assertEqual(payload["status"], "failed")
