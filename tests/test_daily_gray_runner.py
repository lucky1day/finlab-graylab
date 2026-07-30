"""daily_gray_runner 两阶段调度与汇总逻辑的单元测试（不触库）。"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from unittest import mock

from scheduler import daily_gray_runner as runner


@dataclass
class _FakeCfg:
    scheme_id: str
    status: str = "active"
    frequency: str = "daily"
    runtime_type: str = "native_adapter"


@dataclass
class _FakeResult:
    scheme_id: str
    status: str
    records_written: int = 1
    error_msg: str | None = None


def _cfgs(*ids: str) -> list[_FakeCfg]:
    return [_FakeCfg(scheme_id=i) for i in ids]


class DailyGrayRunnerClassifyTests(unittest.TestCase):
    def test_heavy_vs_light_classification(self) -> None:
        # publisher 与训练型 native 属重方案
        self.assertTrue(runner._is_heavy("liwei_0616_10y01_full_oos_k3_div_k10"))
        self.assertTrue(runner._is_heavy("daily_5y_2_v28"))
        # consumer 命中缓存属轻方案
        self.assertFalse(runner._is_heavy("liwei_0616_10y01_cons_say_k3_div_k10"))
        # V2 读 DataBridge 属轻方案
        self.assertFalse(runner._is_heavy("ten_y_t5_maj3_k3_ic_static_v1"))

    def test_consumer_dependencies_map(self) -> None:
        deps = runner.LIWEI_CONSUMER_DEPENDENCIES
        self.assertEqual(
            deps["liwei_0616_10y01_cons_say_k3_div_k10"],
            "liwei_0616_10y01_full_oos_k3_div_k10",
        )
        self.assertEqual(
            deps["liwei_0616_cons_sda_k3_div_k10"],
            "liwei_0616_5y01_full_oos_k3_div_k10",
        )
        # 依赖的 publisher 必须是重方案（会真正扩缓存）
        for pub in set(deps.values()):
            self.assertTrue(runner._is_heavy(pub))


class DailyGrayRunnerOrchestrationTests(unittest.TestCase):
    def _patch_common(self, schemes):
        # 交易日 + 引擎 + 方案发现全部打桩，避免触库。
        self._is_trading = mock.patch.object(
            runner, "is_trading_day", return_value=True
        )
        self._engine = mock.patch.object(
            runner, "create_engine_from_env", return_value=mock.MagicMock()
        )
        self._discover = mock.patch.object(
            runner, "_daily_active_schemes", return_value=schemes
        )
        self._is_trading.start()
        self._engine.start()
        self._discover.start()
        self.addCleanup(self._is_trading.stop)
        self.addCleanup(self._engine.stop)
        self.addCleanup(self._discover.stop)

    def test_consumer_runs_when_publisher_succeeds(self) -> None:
        schemes = _cfgs(
            "liwei_0616_10y01_full_oos_k3_div_k10",
            "liwei_0616_10y01_cons_say_k3_div_k10",
        )
        self._patch_common(schemes)
        order: list[tuple[str, str]] = []  # (scheme_id, phase) 用于验证依赖顺序
        lock = __import__("threading").Lock()

        def fake_exec(cfg, predict_date, *, algo_env, prediction_phase):
            with lock:
                order.append(cfg.scheme_id)
            return _FakeResult(cfg.scheme_id, "success")

        with mock.patch.object(runner, "execute_scheme", side_effect=fake_exec):
            summary = runner.run("2026-07-30", max_heavy=2, light_concurrency=2)

        # 两者都执行成功；publisher 必须在 consumer 之前完成执行
        self.assertEqual(summary.success, 2)
        self.assertEqual(summary.skipped, 0)
        self.assertIn("liwei_0616_10y01_full_oos_k3_div_k10", order)
        self.assertIn("liwei_0616_10y01_cons_say_k3_div_k10", order)
        self.assertLess(
            order.index("liwei_0616_10y01_full_oos_k3_div_k10"),
            order.index("liwei_0616_10y01_cons_say_k3_div_k10"),
        )

    def test_consumer_skipped_when_publisher_fails(self) -> None:
        schemes = _cfgs(
            "liwei_0616_10y01_full_oos_k3_div_k10",
            "liwei_0616_10y01_cons_say_k3_div_k10",
        )
        self._patch_common(schemes)
        calls: list[str] = []

        lock = __import__("threading").Lock()

        def fake_exec(cfg, predict_date, *, algo_env, prediction_phase):
            with lock:
                calls.append(cfg.scheme_id)
            status = (
                "failed"
                if cfg.scheme_id.endswith("full_oos_k3_div_k10")
                else "success"
            )
            return _FakeResult(cfg.scheme_id, status)

        with mock.patch.object(runner, "execute_scheme", side_effect=fake_exec):
            summary = runner.run("2026-07-30", max_heavy=2, light_concurrency=2)

        # publisher 失败 -> consumer 不应被执行（execute_scheme 只被 publisher 调用）
        self.assertEqual(calls, ["liwei_0616_10y01_full_oos_k3_div_k10"])
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.skipped, 1)

    def test_non_trading_day_runs_nothing(self) -> None:
        schemes = _cfgs("daily_1y_xgb_1y13_0629")
        self._patch_common(schemes)
        with mock.patch.object(runner, "is_trading_day", return_value=False):
            with mock.patch.object(runner, "execute_scheme") as exec_mock:
                summary = runner.run("2026-07-25")
        exec_mock.assert_not_called()
        self.assertFalse(summary.is_trading_day)
        self.assertEqual(summary.total, 0)

    def test_exception_isolated_and_counted(self) -> None:
        schemes = _cfgs("daily_1y_xgb_1y13_0629", "daily_5y_2_v28")
        self._patch_common(schemes)

        def fake_exec(cfg, predict_date, *, algo_env, prediction_phase):
            if cfg.scheme_id == "daily_1y_xgb_1y13_0629":
                raise RuntimeError("boom")
            return _FakeResult(cfg.scheme_id, "success")

        with mock.patch.object(runner, "execute_scheme", side_effect=fake_exec):
            summary = runner.run("2026-07-30")

        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.success, 1)
        self.assertEqual(summary.total, 2)

    def test_daily_runner_writes_gray_live_predictions(self) -> None:
        schemes = _cfgs("daily_1y_xgb_1y13_0629")
        self._patch_common(schemes)
        phases: list[str] = []

        def fake_exec(cfg, predict_date, *, algo_env, prediction_phase):
            phases.append(prediction_phase)
            return _FakeResult(cfg.scheme_id, "success")

        with mock.patch.object(runner, "execute_scheme", side_effect=fake_exec):
            summary = runner.run("2026-07-30")

        self.assertEqual(summary.success, 1)
        self.assertEqual(phases, ["gray_live"])

    def test_heavy_concurrency_capped(self) -> None:
        # 5 个重方案，max_heavy=2 时并发峰值不得超过 2（防 CPU 抢占）。
        import threading as _t
        import time as _time
        schemes = _cfgs(
            "liwei_0616_5y_auc_static_all_k3_div_k10",
            "liwei_0616_5y_auc_yearly_all_k3_div_k10",
            "liwei_0616_5y_ic_yearly_all_k3_div_k10",
            "liwei_0616_7y01_cons_say_k3_div_k10",
            "liwei_0616_7y03_cons_all_k3_div_k8",
        )
        self._patch_common(schemes)
        lock = _t.Lock()
        running = 0
        peak = 0

        def fake_exec(cfg, predict_date, *, algo_env, prediction_phase):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            _time.sleep(0.02)
            with lock:
                running -= 1
            return _FakeResult(cfg.scheme_id, "success")

        with mock.patch.object(runner, "execute_scheme", side_effect=fake_exec):
            summary = runner.run("2026-07-30", max_heavy=2, light_concurrency=4)

        self.assertLessEqual(peak, 2)
        self.assertEqual(summary.success, 5)


if __name__ == "__main__":
    unittest.main()
