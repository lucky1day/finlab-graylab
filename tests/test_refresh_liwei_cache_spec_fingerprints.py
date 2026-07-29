from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "refresh_liwei_cache_spec_fingerprints.py"
# 只覆盖 v2：ledger 固定加载 v2；v1 是按字节冻结的历史基线，由
# tests.test_daily_policy_v2.test_v1_policy_bytes_remain_immutable 守护。
POLICY_PATHS = (PROJECT_ROOT / "deploy" / "daily_scheduler_policy_v2.json",)


def _algo_python() -> Path | None:
    """定位算法环境解释器；缺失时让调用方 skip。"""
    algo_env = os.getenv("BOND_ALGO_CONDA_ENV", "forecast_env")
    candidate = Path(sys.prefix).parent / algo_env / "bin" / "python"
    return candidate if candidate.is_file() else None


class LiweiCacheSpecFingerprintPinTests(unittest.TestCase):
    """policy 钉值必须与算法环境实算值一致。

    ledger 在资格校验层把钉值与运行期实算值逐一比对，不一致即 fail-closed。
    钉值又随算法配置与算法环境依赖版本变化（`_baseline_fingerprint` 的 payload
    含 python/numpy/pandas/lightgbm），因此必须由测试守护，不能靠手工维护。
    """

    def test_pins_match_algorithm_environment(self) -> None:
        algo_python = _algo_python()
        if algo_python is None:
            self.skipTest("算法环境解释器不可用")

        # 必须在算法环境子进程里算：本测试自身跑在服务环境，Python 版本不同，
        # 直接在进程内计算会得出另一个值。
        result = subprocess.run(
            [str(algo_python), str(SCRIPT), "--check"],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONNOUSERSITE": "1"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            "policy 钉值与算法环境实算值不一致；请在算法环境运行 "
            f"`{SCRIPT.relative_to(PROJECT_ROOT)} --write` 重算。\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_script_refuses_non_algorithm_environment(self) -> None:
        """在服务环境运行必须 fail-closed，避免写入错值。"""
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--check"],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONNOUSERSITE": "1"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("必须在算法环境中计算", result.stderr)

    def test_every_registered_cache_group_is_pinned(self) -> None:
        """publisher 注册表里的每个 cache_group 都必须在两份 policy 中有钉值。"""
        from shared.liwei_0616_cache_contract import (
            APPROVED_PHASE_A_CACHE_PUBLISHERS,
        )

        expected_groups = {
            f"{family}:{tenor}"
            for family, (tenor, _publisher) in (
                APPROVED_PHASE_A_CACHE_PUBLISHERS.items()
            )
        }
        for path in POLICY_PATHS:
            with self.subTest(policy=path.name):
                payload = json.loads(path.read_text(encoding="utf-8"))
                pinned = {
                    str(row["cache_group"])
                    for row in payload.get("schemes") or ()
                    if row.get("cache_spec_fingerprint")
                }
                self.assertEqual(
                    pinned,
                    expected_groups,
                    "policy 钉值覆盖的 cache_group 与 publisher 注册表不一致",
                )

    def test_pins_are_consistent_within_each_cache_group(self) -> None:
        """同一 cache_group 的多个方案必须钉同一个值。"""
        for path in POLICY_PATHS:
            with self.subTest(policy=path.name):
                payload = json.loads(path.read_text(encoding="utf-8"))
                by_group: dict[str, set[str]] = {}
                for row in payload.get("schemes") or ():
                    fingerprint = row.get("cache_spec_fingerprint")
                    if not fingerprint:
                        continue
                    by_group.setdefault(
                        str(row["cache_group"]), set()
                    ).add(str(fingerprint))
                divergent = {
                    group: sorted(values)
                    for group, values in by_group.items()
                    if len(values) != 1
                }
                self.assertEqual(divergent, {})


if __name__ == "__main__":
    unittest.main()
