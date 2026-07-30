from __future__ import annotations

import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class ExecutorAggregateFenceTests(unittest.TestCase):
    def test_invalid_prediction_phase_has_zero_side_effects(
        self,
    ) -> None:
        from scheduler import executor

        with (
            patch.object(
                executor,
                "discover_schemes",
            ) as discover,
            patch.object(
                executor,
                "create_engine_from_env",
            ) as create_engine,
            patch.object(
                executor,
                "sync_scheme_registry",
            ) as sync_registry,
            patch.object(
                executor,
                "execute_scheme",
            ) as execute_one,
            self.assertRaisesRegex(
                ValueError,
                "prediction_phase",
            ),
        ):
            executor.execute_all(
                "2026-07-28",
                prediction_phase="invalid_phase",
            )

        discover.assert_not_called()
        create_engine.assert_not_called()
        sync_registry.assert_not_called()
        execute_one.assert_not_called()

    def test_scheduled_aggregate_rejects_exact_gray_without_registry_sync(
        self,
    ) -> None:
        from scheduler import executor
        from scheduler import blackbox_scheduler_admission
        from scheduler.blackbox_scheduler_admission import (
            EXPECTED_EXACT_ADMISSIONS,
        )

        schemes = executor.discover_schemes()
        gray_ids = {
            scheme_id
            for (
                scheme_id,
                _version,
            ), entry in EXPECTED_EXACT_ADMISSIONS.items()
            if entry.mode == "gray"
        }
        formal_ids = {
            scheme_id
            for (
                scheme_id,
                _version,
            ), entry in EXPECTED_EXACT_ADMISSIONS.items()
            if entry.mode == "formal"
        }

        def execute(config, *_args, **_kwargs):
            return executor.SchemeRunResult(
                config.scheme_id,
                "success",
                1,
                0.1,
            )

        with (
            patch.object(
                executor,
                "discover_schemes",
                return_value=schemes,
            ),
            patch.object(
                executor,
                "create_engine_from_env",
            ) as create_engine,
            patch.object(
                executor,
                "sync_scheme_registry",
            ) as sync_registry,
            patch.object(
                executor,
                "bootstrap_deployment_daily_coordinator_mode",
                return_value="legacy",
            ) as coordinator_mode,
            patch.object(
                blackbox_scheduler_admission,
                "load_blackbox_scheduler_admission",
                wraps=(
                    blackbox_scheduler_admission
                    .load_blackbox_scheduler_admission
                ),
            ) as load_admission,
            patch.object(
                executor,
                "execute_scheme",
                side_effect=execute,
            ) as execute_one,
        ):
            results = executor.execute_all(
                "2026-07-28",
                prediction_phase="scheduled_live",
            )

        executed_ids = [
            call.args[0].scheme_id
            for call in execute_one.call_args_list
        ]
        active_ids = {
            config.scheme_id
            for config in schemes
            if config.status == "active"
        }
        self.assertTrue(
            (formal_ids & active_ids) <= set(executed_ids)
        )
        self.assertTrue(gray_ids.isdisjoint(executed_ids))
        self.assertEqual(
            executed_ids,
            [
                config.scheme_id
                for config in schemes
                if (
                    config.status == "active"
                    and config.scheme_id not in gray_ids
                )
            ],
        )
        self.assertEqual(
            [result.scheme_id for result in results],
            [
                config.scheme_id
                for config in schemes
                if config.status == "active"
            ],
        )
        self.assertEqual(
            {
                result.scheme_id
                for result in results
                if result.status == "failed"
            },
            gray_ids,
        )
        for result in results:
            if result.scheme_id in gray_ids:
                self.assertTrue(
                    (result.error_msg or "").startswith(
                        "platform configuration error:"
                    )
                )
        create_engine.assert_not_called()
        sync_registry.assert_not_called()
        load_admission.assert_called_once_with()
        coordinator_mode.assert_called_once_with()

    def test_scheduled_aggregate_invalid_policy_fails_blackbox_in_order(
        self,
    ) -> None:
        from scheduler import executor
        from scheduler.blackbox_scheduler_admission import (
            BlackboxSchedulerAdmissionError,
        )

        native_a = SimpleNamespace(
            scheme_id="native_a",
            status="active",
            runtime_type="native_adapter",
        )
        blackbox_a = SimpleNamespace(
            scheme_id="blackbox_a",
            status="active",
            runtime_type="blackbox_v2",
        )
        native_b = SimpleNamespace(
            scheme_id="native_b",
            status="active",
            runtime_type="native_adapter",
        )
        blackbox_b = SimpleNamespace(
            scheme_id="blackbox_b",
            status="active",
            runtime_type="blackbox_v2",
        )
        schemes = [
            native_a,
            blackbox_a,
            native_b,
            blackbox_b,
        ]

        def execute(config, *_args, **_kwargs):
            return executor.SchemeRunResult(
                config.scheme_id,
                "success",
                1,
                0.1,
            )

        with (
            patch.object(
                executor,
                "discover_schemes",
                return_value=schemes,
            ),
            patch(
                "scheduler.blackbox_scheduler_admission."
                "load_blackbox_scheduler_admission",
                side_effect=BlackboxSchedulerAdmissionError(
                    "missing /private/secret/admission.json"
                ),
            ) as load_admission,
            patch.object(
                executor,
                "create_engine_from_env",
            ) as create_engine,
            patch.object(
                executor,
                "sync_scheme_registry",
            ) as sync_registry,
            patch.object(
                executor,
                "bootstrap_deployment_daily_coordinator_mode",
                return_value="legacy",
            ) as coordinator_mode,
            patch.object(
                executor,
                "execute_scheme",
                side_effect=execute,
            ) as execute_one,
        ):
            results = executor.execute_all(
                "2026-07-28",
                prediction_phase="scheduled_live",
            )

        self.assertEqual(
            [result.scheme_id for result in results],
            [
                "native_a",
                "blackbox_a",
                "native_b",
                "blackbox_b",
            ],
        )
        self.assertEqual(
            [
                result.scheme_id
                for result in results
                if result.status == "failed"
            ],
            ["blackbox_a", "blackbox_b"],
        )
        for result in (results[1], results[3]):
            self.assertTrue(
                (result.error_msg or "").startswith(
                    "platform configuration error:"
                )
            )
            self.assertNotIn(
                "/private/secret",
                result.error_msg or "",
            )
        self.assertEqual(
            [
                call.args[0].scheme_id
                for call in execute_one.call_args_list
            ],
            ["native_a", "native_b"],
        )
        create_engine.assert_not_called()
        sync_registry.assert_not_called()
        load_admission.assert_called_once_with()
        coordinator_mode.assert_called_once_with()

    def test_scheduled_aggregate_keeps_admitted_daily_and_recurring(
        self,
    ) -> None:
        from scheduler import executor
        from scheduler.discovery import discover_schemes

        selected_ids = {
            "one_y_t5_liq_excess_a_v1",
            "weekly_10y_lgbm_point_v1",
            "monthly_1y_rf_top30_0629",
        }
        schemes = [
            config
            for config in discover_schemes()
            if config.scheme_id in selected_ids
        ]

        def execute(config, *_args, **_kwargs):
            return executor.SchemeRunResult(
                config.scheme_id,
                "success",
                1,
                0.1,
            )

        with (
            patch.object(
                executor,
                "discover_schemes",
                return_value=schemes,
            ),
            patch.object(
                executor,
                "create_engine_from_env",
            ) as create_engine,
            patch.object(
                executor,
                "sync_scheme_registry",
            ) as sync_registry,
            patch.object(
                executor,
                "bootstrap_deployment_daily_coordinator_mode",
                return_value="ledger",
            ) as coordinator_mode,
            patch.object(
                executor,
                "execute_scheme",
                side_effect=execute,
            ) as execute_one,
        ):
            results = executor.execute_all(
                "2026-07-28",
                prediction_phase="scheduled_live",
            )

        self.assertEqual(
            [call.args[0].scheme_id for call in execute_one.call_args_list],
            [
                config.scheme_id
                for config in schemes
                if config.frequency in {"weekly", "monthly"}
            ],
        )
        self.assertEqual(
            [result.status for result in results],
            [
                (
                    "failed"
                    if config.frequency == "daily"
                    else "success"
                )
                for config in schemes
            ],
        )
        create_engine.assert_not_called()
        sync_registry.assert_not_called()
        coordinator_mode.assert_called_once_with()

    def test_scheduled_aggregate_finishes_all_preflight_before_execution(
        self,
    ) -> None:
        from scheduler import executor

        events: list[str] = []
        native = SimpleNamespace(
            scheme_id="native_daily",
            status="active",
            runtime_type="native_adapter",
            frequency="daily",
        )
        formal = SimpleNamespace(
            scheme_id="formal_weekly",
            scheme_version="version-1",
            status="active",
            version_status="active",
            runtime_type="blackbox_v2",
            frequency="weekly",
        )

        class Policy:
            def mode(self, config):
                events.append(f"policy-mode:{config.scheme_id}")
                return "formal"

            def allows(self, config, *, plane):
                events.append(f"policy-allows:{config.scheme_id}")
                return True

        def execute(config, *_args, **_kwargs):
            events.append(f"execute:{config.scheme_id}")
            return executor.SchemeRunResult(
                config.scheme_id,
                "success",
                1,
                0.1,
            )

        with (
            patch.object(
                executor,
                "discover_schemes",
                return_value=[native, formal],
            ),
            patch(
                "scheduler.blackbox_scheduler_admission."
                "load_blackbox_scheduler_admission",
                side_effect=lambda: (
                    events.append("policy-load") or Policy()
                ),
            ),
            patch.object(
                executor,
                "bootstrap_deployment_daily_coordinator_mode",
                side_effect=lambda: (
                    events.append("authoritative-mode") or "legacy"
                ),
            ),
            patch.object(
                executor,
                "execute_scheme",
                side_effect=execute,
            ),
        ):
            results = executor.execute_all(
                "2026-07-28",
                prediction_phase="scheduled_live",
            )

        self.assertEqual(
            events,
            [
                "policy-load",
                "authoritative-mode",
                "policy-mode:formal_weekly",
                "policy-allows:formal_weekly",
                "execute:native_daily",
                "execute:formal_weekly",
            ],
        )
        self.assertEqual(
            [result.status for result in results],
            ["success", "success"],
        )

    def test_scheduled_aggregate_rejects_stale_daily_as_weekly_before_execution(
        self,
    ) -> None:
        from scheduler import executor

        events: list[str] = []
        valid_weekly = SimpleNamespace(
            scheme_id="valid_weekly",
            scheme_version="native-v1",
            status="active",
            version_status="active",
            runtime_type="native_adapter",
            frequency="weekly",
            task_type="weekly_point",
            horizon=1,
            tenors=["10Y"],
        )
        canonical_daily = SimpleNamespace(
            scheme_id="stale_daily",
            scheme_version="native-v1",
            status="active",
            version_status="active",
            runtime_type="native_adapter",
            frequency="daily",
            task_type="T+1",
            horizon=1,
            tenors=["10Y"],
        )
        stale_daily_as_weekly = SimpleNamespace(
            **{
                **vars(canonical_daily),
                "frequency": "weekly",
            }
        )
        received = [valid_weekly, stale_daily_as_weekly]
        canonical = [valid_weekly, canonical_daily]
        snapshots = iter((received, canonical))

        def discover():
            label = (
                "discover-initial"
                if not events
                else "discover-canonical"
            )
            events.append(label)
            return next(snapshots)

        def execute(config, *_args, **_kwargs):
            events.append(f"execute:{config.scheme_id}")
            return executor.SchemeRunResult(
                config.scheme_id,
                "success",
                1,
                0.1,
            )

        with (
            patch.object(
                executor,
                "discover_schemes",
                side_effect=discover,
            ),
            patch.object(
                executor,
                "execute_scheme",
                side_effect=execute,
            ),
        ):
            results = executor.execute_all(
                "2026-07-28",
                prediction_phase="scheduled_live",
            )

        self.assertEqual(
            events,
            [
                "discover-initial",
                "discover-canonical",
                "execute:valid_weekly",
            ],
        )
        self.assertEqual(
            [result.scheme_id for result in results],
            ["valid_weekly", "stale_daily"],
        )
        self.assertEqual(
            [result.status for result in results],
            ["success", "failed"],
        )
        self.assertTrue(
            (results[1].error_msg or "").startswith(
                "platform configuration error:"
            )
        )

    def test_scheduled_aggregate_canonical_snapshot_failures_do_not_execute(
        self,
    ) -> None:
        from scheduler import executor

        config = SimpleNamespace(
            scheme_id="native_weekly",
            scheme_version="native-v1",
            status="active",
            version_status="active",
            runtime_type="native_adapter",
            frequency="weekly",
            task_type="weekly_point",
            horizon=1,
            tenors=["10Y"],
        )
        canonical_cases = (
            [],
            [config, config],
            ValueError("discovery failed"),
        )
        for canonical in canonical_cases:
            with (
                self.subTest(canonical=repr(canonical)),
                patch.object(
                    executor,
                    "discover_schemes",
                    side_effect=(
                        [config],
                        canonical,
                    ),
                ),
                patch.object(
                    executor,
                    "execute_scheme",
                ) as execute_one,
            ):
                results = executor.execute_all(
                    "2026-07-28",
                    prediction_phase="scheduled_live",
                )

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].status, "failed")
            self.assertTrue(
                (results[0].error_msg or "").startswith(
                    "platform configuration error:"
                )
            )
            execute_one.assert_not_called()

    def test_gray_aggregate_keeps_registry_sync_behavior(self) -> None:
        from scheduler import executor

        engine = _FakeEngine()
        config = SimpleNamespace(
            scheme_id="gray_demo",
            status="active",
        )
        expected = executor.SchemeRunResult(
            config.scheme_id,
            "success",
            1,
            0.1,
        )
        with (
            patch.object(
                executor,
                "discover_schemes",
                return_value=[config],
            ),
            patch.object(
                executor,
                "create_engine_from_env",
                return_value=engine,
            ) as create_engine,
            patch.object(
                executor,
                "sync_scheme_registry",
            ) as sync_registry,
            patch.object(
                executor,
                "bootstrap_deployment_daily_coordinator_mode",
            ) as coordinator_mode,
            patch.object(
                executor,
                "execute_scheme",
                return_value=expected,
            ) as execute_one,
        ):
            results = executor.execute_all(
                "2026-07-28",
                prediction_phase="gray_live",
            )

        self.assertEqual(results, [expected])
        create_engine.assert_called_once_with()
        sync_registry.assert_called_once_with(engine, [config])
        execute_one.assert_called_once_with(
            config,
            "2026-07-28",
            algo_env=executor.DEFAULT_ALGO_ENV,
            prediction_phase="gray_live",
        )
        self.assertTrue(engine.disposed)
        coordinator_mode.assert_not_called()


class ExecutorCliExitCodeTests(unittest.TestCase):
    def _run_cli(
        self,
        argv: list[str],
        *,
        schemes=None,
        single_result=None,
        aggregate_results=None,
    ):
        from scheduler import executor

        with (
            patch.object(
                executor,
                "discover_schemes",
                return_value=list(schemes or []),
            ),
            patch.object(
                executor,
                "execute_scheme",
                return_value=single_result,
            ) as execute_one,
            patch.object(
                executor,
                "execute_all",
                return_value=list(aggregate_results or []),
            ) as execute_all,
        ):
            code = executor.main(argv)
        return code, execute_one, execute_all

    def test_single_denied_gray_exits_two(self) -> None:
        from scheduler.executor import SchemeRunResult

        config = SimpleNamespace(scheme_id="gray_exact")
        result = SchemeRunResult(
            config.scheme_id,
            "failed",
            0,
            0.0,
            "platform configuration error: direct control plane denied",
        )
        code, execute_one, execute_all = self._run_cli(
            [
                "2026-07-28",
                "--scheme-id",
                config.scheme_id,
            ],
            schemes=[config],
            single_result=result,
        )
        self.assertEqual(code, 2)
        execute_one.assert_called_once()
        execute_all.assert_not_called()

    def test_unknown_single_scheme_exits_two_without_execution(
        self,
    ) -> None:
        code, execute_one, execute_all = self._run_cli(
            [
                "2026-07-28",
                "--scheme-id",
                "missing_scheme",
            ],
        )
        self.assertEqual(code, 2)
        execute_one.assert_not_called()
        execute_all.assert_not_called()

    def test_aggregate_configuration_failure_exits_two(self) -> None:
        from scheduler.executor import SchemeRunResult

        code, _execute_one, execute_all = self._run_cli(
            ["2026-07-28"],
            aggregate_results=[
                SchemeRunResult("native", "success", 1, 0.1),
                SchemeRunResult(
                    "blackbox",
                    "failed",
                    0,
                    0.0,
                    "platform configuration error: admission invalid",
                ),
            ],
        )
        self.assertEqual(code, 2)
        execute_all.assert_called_once()

    def test_business_failure_exits_one(self) -> None:
        from scheduler.executor import SchemeRunResult

        code, _execute_one, _execute_all = self._run_cli(
            ["2026-07-28"],
            aggregate_results=[
                SchemeRunResult(
                    "native",
                    "failed",
                    0,
                    0.1,
                    "algorithm failed",
                )
            ],
        )
        self.assertEqual(code, 1)

    def test_success_and_normal_filter_exit_zero(self) -> None:
        from scheduler.executor import SchemeRunResult

        code, _execute_one, _execute_all = self._run_cli(
            ["2026-07-28"],
            aggregate_results=[
                SchemeRunResult("native", "success", 1, 0.1)
            ],
        )
        self.assertEqual(code, 0)

    def test_module_entry_exits_with_main_return_code(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "scheduler.executor",
                "2026-07-28",
                "--scheme-id",
                "definitely_missing_executor_scheme",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn(
            "platform configuration error:",
            completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
