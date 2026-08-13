"""Blackbox predict timeout 由方案请求、平台上限和显式 deadline 分层计算。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _cfg(runtime_type: str, configured: int | None):
    return SimpleNamespace(
        scheme_id="demo",
        runtime_type=runtime_type,
        schedule=SimpleNamespace(timeout_sec=configured),
    )


class TimeoutAuthorityTests(unittest.TestCase):
    def _effective(self, cfg, deadline=None):
        from scheduler.executor import _effective_timeout_sec

        return _effective_timeout_sec(cfg, deadline)

    def test_runtime_profile_keeps_3600_second_predict_ceiling(self) -> None:
        from scheduler.blackbox_v2_runner import DEFAULT_RUNTIME_PROFILE

        self.assertEqual(DEFAULT_RUNTIME_PROFILE.predict_timeout_sec, 3600)
        self.assertEqual(DEFAULT_RUNTIME_PROFILE.backtest_timeout_sec, 14400)

    def test_blackbox_without_operation_deadline_uses_scheme_request(self) -> None:
        self.assertEqual(self._effective(_cfg("blackbox_v2", 3600)), 3600)

    def test_blackbox_explicit_deadline_can_only_narrow_scheme_request(self) -> None:
        cfg = _cfg("blackbox_v2", 3600)

        self.assertEqual(self._effective(cfg, 600), 600)
        self.assertEqual(self._effective(cfg, 7200), 3600)

    def test_blackbox_missing_scheme_request_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "timeout_sec must be configured"):
            self._effective(_cfg("blackbox_v2", None))

    def test_native_config_is_still_honoured(self) -> None:
        self.assertEqual(self._effective(_cfg("native_adapter", 3600)), 3600)

    def test_native_without_config_keeps_600_second_default(self) -> None:
        self.assertEqual(self._effective(_cfg("native_adapter", None)), 600)

    def test_runtime_profile_file_matches_loaded_contract(self) -> None:
        raw = json.loads(
            (PROJECT_ROOT / "deploy/blackbox_v2/runtime_profile_v1.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(raw["predict_timeout_sec"], 3600)
        self.assertEqual(raw["backtest_timeout_sec"], 14400)


if __name__ == "__main__":
    unittest.main()
