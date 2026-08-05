"""后端直调预测辅助的最小回归覆盖。"""

from __future__ import annotations

import ast
import importlib.util
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scheduler.blackbox_scheduler_admission import EXPECTED_EXACT_ADMISSIONS
from scheduler.executor import SchemeRunResult
from scheduler.v2_daily_gate import V2DailyGateBlocked


def _cfg(
    scheme_id: str,
    *,
    frequency: str | None = None,
    status: str = "active",
    version_status: str = "active",
    runtime_type: str = "native_adapter",
    input_source: str | None = None,
    scheme_version: str = "version-1",
) -> SimpleNamespace:
    admission = EXPECTED_EXACT_ADMISSIONS.get((scheme_id, scheme_version))
    return SimpleNamespace(
        scheme_id=scheme_id,
        scheme_version=scheme_version,
        status=status,
        version_status=version_status,
        frequency=frequency or (admission.frequency if admission else "daily"),
        task_type=admission.task_type if admission else "T+1",
        horizon=admission.horizon if admission else 1,
        runtime_type=runtime_type,
        input_source=input_source,
        tenors=[admission.target_tenor if admission else "5Y"],
    )


class DirectPredictionTests(unittest.TestCase):
    @staticmethod
    def _module():
        spec = importlib.util.find_spec("scheduler.direct_prediction")
        if spec is None:
            raise AssertionError(
                "direct/manual prediction helpers must have an isolated module"
            )
        from scheduler import direct_prediction

        return direct_prediction

    def test_direct_module_is_available_without_resident_scheduler_import(
        self,
    ) -> None:
        direct_prediction = self._module()
        source = ast.parse(
            Path(str(direct_prediction.__file__)).read_text(
                encoding="utf-8"
            )
        )
        imports = [
            node.module
            for node in ast.walk(source)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("apscheduler")
        ]
        imports.extend(
            alias.name
            for node in ast.walk(source)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name.startswith("apscheduler")
        )
        self.assertEqual([], imports)

    def test_daily_direct_prediction_skips_non_trading_day(self) -> None:
        direct_prediction = self._module()
        config = _cfg("daily_demo", frequency="daily")
        with (
            patch.object(
                direct_prediction,
                "discover_schemes",
                return_value=[config],
            ),
            patch.object(
                direct_prediction,
                "_daily_coordinator_mode",
                return_value="legacy",
            ),
            patch.object(
                direct_prediction,
                "_is_trading_day",
                return_value=False,
            ) as trading_day,
            patch.object(direct_prediction, "execute_scheme") as execute_scheme,
            self.assertLogs(direct_prediction.logger, level=logging.INFO) as logs,
        ):
            result = direct_prediction.run_prediction_job(
                "daily_demo",
                run_date="2026-06-15",
            )

        self.assertEqual(
            SchemeRunResult(
                "daily_demo", "skipped", 0, 0.0, "non-trading day"
            ),
            result,
        )
        trading_day.assert_called_once_with("2026-06-15")
        execute_scheme.assert_not_called()
        self.assertTrue(
            any("Skip daily_demo on non-trading day" in line for line in logs.output)
        )

    def test_direct_prediction_executes_canonical_config(self) -> None:
        direct_prediction = self._module()
        config = _cfg("daily_demo", frequency="daily")
        expected = SchemeRunResult("daily_demo", "success", 1, 0.1, run_id=7)
        with (
            patch.object(
                direct_prediction,
                "discover_schemes",
                return_value=[config],
            ),
            patch.object(
                direct_prediction,
                "_daily_coordinator_mode",
                return_value="legacy",
            ),
            patch.object(direct_prediction, "_is_trading_day", return_value=True),
            patch.object(
                direct_prediction,
                "execute_scheme",
                return_value=expected,
            ) as execute_scheme,
        ):
            actual = direct_prediction.run_prediction_job(
                "daily_demo", run_date="2026-06-16"
            )

        self.assertIs(actual, expected)
        execute_scheme.assert_called_once_with(
            config,
            "2026-06-16",
            algo_env=direct_prediction.DEFAULT_ALGO_ENV,
        )

    def test_ledger_mode_rejects_direct_daily_before_execution(self) -> None:
        direct_prediction = self._module()
        config = _cfg("daily_demo", frequency="daily")
        with (
            patch.object(
                direct_prediction,
                "discover_schemes",
                return_value=[config],
            ),
            patch.object(
                direct_prediction,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                direct_prediction,
                "_run_prediction_config",
            ) as run_config,
            self.assertRaisesRegex(
                RuntimeError,
                "direct daily prediction.*ledger coordinator",
            ),
        ):
            direct_prediction.run_prediction_job(
                "daily_demo", run_date="2026-07-24"
            )

        run_config.assert_not_called()

    def test_blackbox_v2_requires_readiness_but_native_does_not(self) -> None:
        direct_prediction = self._module()
        blackbox = _cfg(
            "one_y_t5_liq_excess_a_v1",
            runtime_type="blackbox_v2",
            scheme_version="8d583560c9f1",
            input_source="data_bridge_current",
        )
        native = _cfg("native_demo", runtime_type="native_adapter")
        native_result = SchemeRunResult("native_demo", "success", 1, 0.1)
        with (
            patch.object(
                direct_prediction,
                "discover_schemes",
                side_effect=[[blackbox], [native]],
            ),
            patch.object(
                direct_prediction,
                "_daily_coordinator_mode",
                return_value="legacy",
            ),
            patch.object(direct_prediction, "_is_trading_day", return_value=True),
            patch.object(
                direct_prediction,
                "_previous_trading_day",
                return_value="2026-07-21",
            ),
            patch.object(
                direct_prediction,
                "require_v2_daily_ready",
                side_effect=[V2DailyGateBlocked("blocked")],
            ) as gate,
            patch.object(
                direct_prediction,
                "execute_scheme",
                return_value=native_result,
            ) as execute_scheme,
            self.assertLogs(direct_prediction.logger, level=logging.ERROR),
        ):
            blocked = direct_prediction.run_prediction_job(
                blackbox.scheme_id,
                run_date="2026-07-22",
            )
            native_actual = direct_prediction.run_prediction_job(
                native.scheme_id,
                run_date="2026-07-22",
            )

        self.assertEqual("skipped", blocked.status)
        self.assertEqual("blocked", blocked.error_msg)
        self.assertIs(native_result, native_actual)
        gate.assert_called_once()
        execute_scheme.assert_called_once_with(
            native,
            "2026-07-22",
            algo_env=direct_prediction.DEFAULT_ALGO_ENV,
        )

    def test_direct_path_has_no_registry_sync_side_effect(self) -> None:
        direct_prediction = self._module()
        config = _cfg("native_demo", frequency="weekly")
        expected = SchemeRunResult("native_demo", "success", 1, 0.1)
        self.assertFalse(hasattr(direct_prediction, "_sync_registry"))
        self.assertFalse(hasattr(direct_prediction, "sync_scheme_registry"))
        with (
            patch.object(
                direct_prediction,
                "discover_schemes",
                return_value=[config],
            ),
            patch.object(
                direct_prediction,
                "execute_scheme",
                return_value=expected,
            ),
        ):
            actual = direct_prediction.run_prediction_job(
                config.scheme_id,
                run_date="2026-07-22",
            )

        self.assertIs(actual, expected)

    def test_unknown_config_returns_platform_failure(self) -> None:
        direct_prediction = self._module()
        with patch.object(
            direct_prediction,
            "discover_schemes",
            return_value=[],
        ):
            result = direct_prediction.run_prediction_job(
                "missing",
                run_date="2026-06-16",
            )

        self.assertEqual("failed", result.status)
        self.assertIn("platform configuration error", result.error_msg or "")
        self.assertIn("scheme not found", result.error_msg or "")

    def test_gray_blackbox_is_denied_before_readiness_or_execution(self) -> None:
        direct_prediction = self._module()
        config = _cfg(
            "cgb_a4_fundseason_1y",
            runtime_type="blackbox_v2",
            scheme_version="04e7af163fb0",
        )
        with (
            patch.object(
                direct_prediction,
                "discover_schemes",
                return_value=[config],
            ),
            patch.object(direct_prediction, "_is_trading_day") as trading_day,
            patch.object(
                direct_prediction,
                "require_v2_daily_ready",
            ) as readiness,
            patch.object(
                direct_prediction,
                "_run_prediction_config",
            ) as run_config,
        ):
            result = direct_prediction.run_prediction_job(
                config.scheme_id,
                run_date="2026-07-27",
            )

        self.assertEqual("failed", result.status)
        self.assertIn("direct_scheduled", result.error_msg or "")
        trading_day.assert_not_called()
        readiness.assert_not_called()
        run_config.assert_not_called()

    def test_inactive_scheme_or_blackbox_version_is_denied_before_policy(
        self,
    ) -> None:
        direct_prediction = self._module()
        configs = (
            _cfg("paused_native", status="paused"),
            _cfg(
                "one_y_t5_liq_excess_a_v1",
                runtime_type="blackbox_v2",
                scheme_version="8d583560c9f1",
                version_status="shadow",
            ),
        )
        for config in configs:
            with self.subTest(scheme_id=config.scheme_id):
                with (
                    patch.object(
                        direct_prediction,
                        "discover_schemes",
                        return_value=[config],
                    ),
                    patch(
                        "scheduler.blackbox_scheduler_admission."
                        "load_blackbox_scheduler_admission",
                        side_effect=AssertionError(
                            "inactive direct path must not load policy"
                        ),
                    ) as load_policy,
                    patch.object(
                        direct_prediction,
                        "_run_prediction_config",
                    ) as run_config,
                ):
                    result = direct_prediction.run_prediction_job(
                        config.scheme_id,
                        run_date="2026-07-27",
                    )

                self.assertEqual("failed", result.status)
                self.assertIn(
                    direct_prediction.PLATFORM_CONFIGURATION_ERROR_PREFIX,
                    result.error_msg or "",
                )
                load_policy.assert_not_called()
                run_config.assert_not_called()

    def test_exact_formal_blackbox_daily_and_weekly_are_admitted(self) -> None:
        direct_prediction = self._module()
        identities = (
            ("one_y_t5_liq_excess_a_v1", "8d583560c9f1"),
            ("weekly_10y_lgbm_point_v1", "0666a6989d6b"),
        )
        for scheme_id, scheme_version in identities:
            config = _cfg(
                scheme_id,
                runtime_type="blackbox_v2",
                scheme_version=scheme_version,
            )
            expected = SchemeRunResult(config.scheme_id, "success", 1, 0.1)
            with self.subTest(scheme_id=scheme_id):
                with (
                    patch.object(
                        direct_prediction,
                        "discover_schemes",
                        return_value=[config],
                    ),
                    patch.object(
                        direct_prediction,
                        "_daily_coordinator_mode",
                        return_value="legacy",
                    ),
                    patch.object(
                        direct_prediction,
                        "_run_prediction_config",
                        return_value=expected,
                    ) as run_config,
                ):
                    actual = direct_prediction.run_prediction_job(
                        config.scheme_id,
                        run_date="2026-07-27",
                    )

                self.assertIs(actual, expected)
                run_config.assert_called_once_with(
                    config,
                    "2026-07-27",
                    algo_env=direct_prediction.DEFAULT_ALGO_ENV,
                    force=False,
                )

    def test_native_direct_path_does_not_load_blackbox_policy(self) -> None:
        direct_prediction = self._module()
        config = _cfg("native_demo", frequency="weekly")
        expected = SchemeRunResult(config.scheme_id, "success", 1, 0.1)
        with (
            patch.object(
                direct_prediction,
                "discover_schemes",
                return_value=[config],
            ),
            patch(
                "scheduler.blackbox_scheduler_admission."
                "load_blackbox_scheduler_admission",
                side_effect=AssertionError("native direct path must not load policy"),
            ) as load_policy,
            patch.object(
                direct_prediction,
                "_run_prediction_config",
                return_value=expected,
            ),
        ):
            actual = direct_prediction.run_prediction_job(
                config.scheme_id,
                run_date="2026-07-27",
            )

        self.assertIs(actual, expected)
        load_policy.assert_not_called()

    def test_weekly_and_monthly_run_on_non_trading_day(self) -> None:
        direct_prediction = self._module()
        for frequency in ("weekly", "monthly"):
            config = _cfg(f"{frequency}_demo", frequency=frequency)
            with self.subTest(frequency=frequency):
                with (
                    patch.object(
                        direct_prediction,
                        "discover_schemes",
                        return_value=[config],
                    ),
                    patch.object(
                        direct_prediction,
                        "_is_trading_day",
                        return_value=False,
                    ) as trading_day,
                    patch.object(
                        direct_prediction,
                        "execute_scheme",
                        return_value="ok",
                    ) as execute_scheme,
                ):
                    actual = direct_prediction.run_prediction_job(
                        config.scheme_id,
                        run_date="2026-06-15",
                    )

                self.assertEqual("ok", actual)
                trading_day.assert_not_called()
                execute_scheme.assert_called_once_with(
                    config,
                    "2026-06-15",
                    algo_env=direct_prediction.DEFAULT_ALGO_ENV,
                )

    def test_operator_recovery_requires_ledger_and_routes_to_runtime(self) -> None:
        direct_prediction = self._module()
        with (
            patch.object(
                direct_prediction,
                "_daily_coordinator_mode",
                return_value="legacy",
            ),
            patch("scheduler.daily_runtime.run_operator_recovery") as recovery,
            self.assertRaisesRegex(RuntimeError, "ledger coordinator mode"),
        ):
            direct_prediction.run_daily_operator_recovery_job(
                "daily_demo",
                run_date="2026-07-24",
            )
        recovery.assert_not_called()

        with (
            patch.object(
                direct_prediction,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch(
                "scheduler.daily_runtime.run_operator_recovery",
                return_value="recovered",
            ) as recovery,
        ):
            result = direct_prediction.run_daily_operator_recovery_job(
                "daily_demo",
                run_date="2026-07-24",
            )

        self.assertEqual("recovered", result)
        recovery.assert_called_once_with(
            scheme_id="daily_demo",
            run_date="2026-07-24",
            algo_env=direct_prediction.DEFAULT_ALGO_ENV,
        )


if __name__ == "__main__":
    unittest.main()
