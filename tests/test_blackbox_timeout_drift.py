"""Blackbox 配置 timeout 被平台上限截断时必须显式可见。

`_effective_timeout_sec` 对 Blackbox 取 `min(configured, caller_default)`，这个
上限是有意策略（调用方 policy 不可被方案配置放宽）。但平台同时**接受并保留**
一个它永远不会兑现的值：39 个 active Blackbox 全部声明 3600，实际有效 600。

配置因此不再是可直接相信的执行契约——方案作者、运维、容量评估和实际子进程
使用的并非同一时间预算，而一旦运行超过 10 分钟，表象是意外执行失败而不是
显式配置冲突。
"""

from __future__ import annotations

import json
import logging
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


class TimeoutDriftTests(unittest.TestCase):
    def _effective(self, cfg, default=600):
        from scheduler.executor import _effective_timeout_sec

        return _effective_timeout_sec(cfg, default)

    def test_blackbox_ceiling_still_applies(self) -> None:
        """上限是有意策略，本次不改变任何有效预算。"""
        self.assertEqual(self._effective(_cfg("blackbox_v2", 3600)), 600)

    def test_native_config_is_still_honoured(self) -> None:
        self.assertEqual(self._effective(_cfg("native_adapter", 3600)), 3600)

    def test_truncation_emits_structured_warning(self) -> None:
        with self.assertLogs("scheduler.executor", level="WARNING") as captured:
            self._effective(_cfg("blackbox_v2", 3600))
        blob = "\n".join(captured.output)
        self.assertIn("blackbox_timeout_truncated", blob)
        payload = json.loads(blob.split("blackbox_timeout_truncated ", 1)[1].splitlines()[0])
        self.assertEqual(payload["scheme_id"], "demo")
        self.assertEqual(payload["configured_timeout_sec"], 3600)
        self.assertEqual(payload["effective_timeout_sec"], 600)

    def test_no_warning_when_config_is_within_ceiling(self) -> None:
        logger = logging.getLogger("scheduler.executor")
        with self.assertNoLogs(logger, level="WARNING"):
            self.assertEqual(self._effective(_cfg("blackbox_v2", 300)), 300)

    def test_no_warning_for_native_above_default(self) -> None:
        """Native 不受该上限约束，声明值即有效值，不该报漂移。"""
        logger = logging.getLogger("scheduler.executor")
        with self.assertNoLogs(logger, level="WARNING"):
            self._effective(_cfg("native_adapter", 3600))


if __name__ == "__main__":
    unittest.main()
