from __future__ import annotations

from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from scheduler import daily_gray_runner as runner
from scheduler.daily_gray_launchd_policy import DailyGrayLaunchdPolicyError


class _ImmediateFuture:
    def __init__(self, function, *args) -> None:
        try:
            self._value = function(*args)
            self._error: BaseException | None = None
        except BaseException as exc:  # pragma: no cover - result() re-raises it
            self._value = None
            self._error = exc

    def result(self):
        if self._error is not None:
            raise self._error
        return self._value


class _RecordingExecutor:
    instances: list[_RecordingExecutor] = []

    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers
        self.submitted: list[str] = []
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def submit(self, function, config):
        self.submitted.append(config.scheme_id)
        return _ImmediateFuture(function, config)


class _BoundedCondition:
    """用真实 Condition 等待，但把潜在死锁转为有界测试失败。"""

    def __init__(self, lock) -> None:
        self._condition = threading.Condition(lock)

    def __enter__(self):
        return self._condition.__enter__()

    def __exit__(self, exc_type, exc, traceback) -> None:
        return self._condition.__exit__(exc_type, exc, traceback)

    def notify_all(self) -> None:
        self._condition.notify_all()

    def wait(self, timeout: float | None = None) -> bool:
        notified = self._condition.wait(timeout=0.2)
        if not notified:
            raise RuntimeError("consumer would wait indefinitely")
        return notified


def _config(scheme_id: str):
    return SimpleNamespace(
        scheme_id=scheme_id,
        status="active",
        frequency="daily",
    )


def _policy(
    rows: list[tuple[str, str, str | None]],
    *,
    isolated_active_daily_scheme_ids: tuple[str, ...] = (),
):
    return SimpleNamespace(
        schemes={
            scheme_id: SimpleNamespace(
                scheme_id=scheme_id,
                execution_class=execution_class,
                publisher_scheme_id=publisher_scheme_id,
            )
            for scheme_id, execution_class, publisher_scheme_id in rows
        },
        isolated_active_daily_scheme_ids=isolated_active_daily_scheme_ids,
    )


def _success(records: int = 1):
    return SimpleNamespace(
        status="success",
        records_written=records,
        error_msg=None,
    )


class DailyGrayRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        _RecordingExecutor.instances = []

    def _run_with_fakes(
        self,
        discovered,
        policy,
        *,
        only: set[str] | None = None,
        trading: bool = True,
        execute_side_effect=None,
    ):
        engine = Mock()
        loader_patch = patch.object(
            runner,
            "load_daily_gray_launchd_policy",
            return_value=policy,
        )
        execute_mock = (
            Mock(return_value=_success())
            if execute_side_effect is None
            else Mock(side_effect=execute_side_effect)
        )
        with (
            patch.object(
                runner,
                "discover_schemes",
                return_value=discovered,
            ) as discovery,
            loader_patch as loader,
            patch.object(
                runner,
                "create_engine_from_env",
                return_value=engine,
            ) as create_engine,
            patch.object(
                runner,
                "is_trading_day",
                return_value=trading,
            ) as trading_day,
            patch.object(
                runner,
                "ThreadPoolExecutor",
                _RecordingExecutor,
            ),
            patch.object(
                runner,
                "execute_scheme",
                execute_mock,
            ) as execute,
        ):
            summary = runner.run("2026-08-03", only=only)
        return SimpleNamespace(
            summary=summary,
            engine=engine,
            discovery=discovery,
            loader=loader,
            create_engine=create_engine,
            trading_day=trading_day,
            execute=execute,
        )

    def test_run_uses_strict_discovery_and_policy_external_isolation_opt_in(
        self,
    ) -> None:
        discovered = [_config("alpha")]
        result = self._run_with_fakes(
            discovered,
            _policy([("alpha", "light", None)]),
            trading=False,
        )

        result.discovery.assert_called_once_with(strict=True)
        result.loader.assert_called_once_with(
            discovered=discovered,
            allow_policy_external_active_daily=True,
        )

    def test_policy_external_identities_are_logged_before_engine_and_never_executed(
        self,
    ) -> None:
        discovered = [
            _config("alpha"),
            _config("outside-policy-a"),
            _config("outside-policy-z"),
        ]
        policy = _policy(
            [("alpha", "light", None)],
            isolated_active_daily_scheme_ids=(
                "outside-policy-a",
                "outside-policy-z",
            ),
        )
        engine = Mock()
        events: list[str] = []

        def create_engine():
            events.append("engine")
            return engine

        def execute(config, *args, **kwargs):
            events.append(f"execute:{config.scheme_id}")
            return _success()

        with (
            patch.object(
                runner,
                "discover_schemes",
                return_value=discovered,
            ),
            patch.object(
                runner,
                "load_daily_gray_launchd_policy",
                return_value=policy,
            ),
            patch.object(
                runner.logger,
                "error",
                side_effect=lambda *args: events.append("isolation-log"),
            ) as isolation_log,
            patch.object(
                runner,
                "create_engine_from_env",
                side_effect=create_engine,
            ),
            patch.object(runner, "is_trading_day", return_value=True),
            patch.object(
                runner,
                "ThreadPoolExecutor",
                _RecordingExecutor,
            ),
            patch.object(
                runner,
                "execute_scheme",
                side_effect=execute,
            ) as execute_mock,
        ):
            summary = runner.run("2026-08-03")

        self.assertEqual(events[0], "isolation-log")
        self.assertLess(events.index("isolation-log"), events.index("engine"))
        self.assertEqual(
            summary.isolated_active_daily_scheme_ids,
            ("outside-policy-a", "outside-policy-z"),
        )
        self.assertEqual(
            [call.args[0].scheme_id for call in execute_mock.call_args_list],
            ["alpha"],
        )
        self.assertIn("outside-policy-a", str(isolation_log.call_args))
        self.assertIn("outside-policy-z", str(isolation_log.call_args))

    def test_policy_failure_precedes_engine_pool_and_execution(self) -> None:
        discovered = [_config("alpha")]
        failure = DailyGrayLaunchdPolicyError("policy drift")
        engine = Mock()
        with (
            patch.object(
                runner,
                "discover_schemes",
                return_value=discovered,
            ) as discovery,
            patch.object(
                runner,
                "load_daily_gray_launchd_policy",
                side_effect=failure,
            ) as loader,
            patch.object(
                runner,
                "create_engine_from_env",
                return_value=engine,
            ) as create_engine,
            patch.object(
                runner,
                "is_trading_day",
                return_value=False,
            ) as trading_day,
            patch.object(
                runner,
                "ThreadPoolExecutor",
            ) as executor_pool,
            patch.object(runner, "execute_scheme") as execute,
            self.assertRaises(DailyGrayLaunchdPolicyError) as captured,
        ):
            runner.run("2026-08-03")

        self.assertIs(captured.exception, failure)
        discovery.assert_called_once_with(strict=True)
        loader.assert_called_once()
        self.assertIs(loader.call_args.kwargs["discovered"], discovered)
        create_engine.assert_not_called()
        trading_day.assert_not_called()
        executor_pool.assert_not_called()
        execute.assert_not_called()

    def test_strict_discovery_failure_is_domain_error_before_side_effects(
        self,
    ) -> None:
        failure = RuntimeError("strict discovery exploded")
        with (
            patch.object(
                runner,
                "discover_schemes",
                side_effect=failure,
            ) as discovery,
            patch.object(
                runner,
                "load_daily_gray_launchd_policy",
            ) as loader,
            patch.object(
                runner,
                "create_engine_from_env",
            ) as create_engine,
            patch.object(runner, "is_trading_day") as trading_day,
            patch.object(
                runner,
                "ThreadPoolExecutor",
            ) as executor_pool,
            patch.object(runner, "execute_scheme") as execute,
            self.assertRaisesRegex(
                DailyGrayLaunchdPolicyError,
                "strict discovery failed",
            ) as captured,
        ):
            runner.run("2026-08-03")

        self.assertIs(captured.exception.__cause__, failure)
        discovery.assert_called_once_with(strict=True)
        loader.assert_not_called()
        create_engine.assert_not_called()
        trading_day.assert_not_called()
        executor_pool.assert_not_called()
        execute.assert_not_called()

    def test_isolated_only_identity_fails_before_engine(self) -> None:
        discovered = [_config("alpha"), _config("outside-policy")]
        engine = Mock()
        with (
            patch.object(
                runner,
                "discover_schemes",
                return_value=discovered,
            ),
            patch.object(
                runner,
                "load_daily_gray_launchd_policy",
                return_value=_policy(
                    [("alpha", "light", None)],
                    isolated_active_daily_scheme_ids=("outside-policy",),
                ),
            ),
            patch.object(
                runner,
                "create_engine_from_env",
                return_value=engine,
            ) as create_engine,
            patch.object(
                runner,
                "is_trading_day",
                return_value=False,
            ) as trading_day,
            patch.object(
                runner,
                "ThreadPoolExecutor",
            ) as executor_pool,
            patch.object(runner, "execute_scheme") as execute,
            self.assertRaisesRegex(ValueError, "unknown.*outside-policy"),
        ):
            runner.run("2026-08-03", only={"alpha", "outside-policy"})

        create_engine.assert_not_called()
        trading_day.assert_not_called()
        executor_pool.assert_not_called()
        execute.assert_not_called()

    def test_explicit_empty_only_fails_before_engine(self) -> None:
        discovered = [_config("alpha")]
        with (
            patch.object(
                runner,
                "discover_schemes",
                return_value=discovered,
            ),
            patch.object(
                runner,
                "load_daily_gray_launchd_policy",
                return_value=_policy([("alpha", "light", None)]),
            ),
            patch.object(
                runner,
                "create_engine_from_env",
            ) as create_engine,
            patch.object(runner, "is_trading_day") as trading_day,
            patch.object(
                runner,
                "ThreadPoolExecutor",
            ) as executor_pool,
            patch.object(runner, "execute_scheme") as execute,
            self.assertRaisesRegex(
                runner.DailyGrayRunnerInputError,
                "empty",
            ),
        ):
            runner.run("2026-08-03", only=set())

        create_engine.assert_not_called()
        trading_day.assert_not_called()
        executor_pool.assert_not_called()
        execute.assert_not_called()

    def test_policy_order_drives_exact_28_scheme_execution_set(self) -> None:
        policy_order = [f"scheme_{index:02d}" for index in range(27, -1, -1)]
        discovered = [_config(scheme_id) for scheme_id in sorted(policy_order)]
        result = self._run_with_fakes(
            discovered,
            _policy(
                [
                    (scheme_id, "light", None)
                    for scheme_id in policy_order
                ]
            ),
        )

        self.assertEqual(result.summary.total, 28)
        self.assertEqual(result.summary.success, 28)
        self.assertEqual(
            [args.args[0].scheme_id for args in result.execute.call_args_list],
            policy_order,
        )

    def test_heavy_and_light_partition_comes_from_policy(self) -> None:
        old_hardcoded_heavy = "daily_5y_2_v28"
        policy_declared_heavy = "policy_declared_heavy"
        discovered = [
            _config(old_hardcoded_heavy),
            _config(policy_declared_heavy),
        ]
        self._run_with_fakes(
            discovered,
            _policy(
                [
                    (old_hardcoded_heavy, "light", None),
                    (policy_declared_heavy, "heavy", None),
                ]
            ),
        )

        self.assertEqual(len(_RecordingExecutor.instances), 2)
        heavy_pool, light_pool = _RecordingExecutor.instances
        self.assertEqual(heavy_pool.submitted, [policy_declared_heavy])
        self.assertEqual(light_pool.submitted, [old_hardcoded_heavy])

    def test_publisher_dependency_comes_from_policy(self) -> None:
        publisher = "policy_publisher"
        consumer = "policy_consumer"

        def execute(config, *args, **kwargs):
            if config.scheme_id == publisher:
                return SimpleNamespace(
                    status="failed",
                    records_written=0,
                    error_msg="publisher failed",
                )
            return _success()

        result = self._run_with_fakes(
            [_config(publisher), _config(consumer)],
            _policy(
                [
                    (publisher, "heavy", None),
                    (consumer, "light", publisher),
                ]
            ),
            execute_side_effect=execute,
        )

        self.assertEqual(
            [args.args[0].scheme_id for args in result.execute.call_args_list],
            [publisher],
        )
        self.assertEqual(result.summary.failed, 1)
        self.assertEqual(result.summary.skipped, 1)

    def test_publisher_unexpected_error_cannot_deadlock_real_pool(
        self,
    ) -> None:
        publisher = "policy_publisher"
        consumer = "policy_consumer"
        discovered = [_config(publisher), _config(consumer)]
        engine = Mock()

        class ExplodingResult:
            @property
            def status(self):
                raise RuntimeError("malformed execution result")

        def execute(config, *args, **kwargs):
            if config.scheme_id == publisher:
                return ExplodingResult()
            return _success()

        bounded_threading = SimpleNamespace(
            Lock=threading.Lock,
            Condition=_BoundedCondition,
        )
        with (
            patch.object(
                runner,
                "discover_schemes",
                return_value=discovered,
            ),
            patch.object(
                runner,
                "load_daily_gray_launchd_policy",
                return_value=_policy(
                    [
                        (publisher, "heavy", None),
                        (consumer, "light", publisher),
                    ]
                ),
            ),
            patch.object(
                runner,
                "create_engine_from_env",
                return_value=engine,
            ),
            patch.object(runner, "is_trading_day", return_value=True),
            patch.object(runner, "threading", bounded_threading),
            patch.object(
                runner,
                "execute_scheme",
                side_effect=execute,
            ) as execute_mock,
        ):
            try:
                summary = runner.run(
                    "2026-08-03",
                    max_heavy=1,
                    light_concurrency=1,
                )
            except RuntimeError as exc:
                self.fail(f"publisher error escaped or deadlocked: {exc}")

        self.assertEqual(
            [args.args[0].scheme_id for args in execute_mock.call_args_list],
            [publisher],
        )
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.skipped, 1)

    def test_consumer_selected_without_publisher_is_skipped(self) -> None:
        publisher = "policy_publisher"
        consumer = "policy_consumer"
        result = self._run_with_fakes(
            [_config(publisher), _config(consumer)],
            _policy(
                [
                    (publisher, "heavy", None),
                    (consumer, "light", publisher),
                ]
            ),
            only={consumer},
        )

        result.execute.assert_not_called()
        self.assertEqual(result.summary.total, 1)
        self.assertEqual(result.summary.skipped, 1)
        self.assertIn("status=absent", result.summary.details[0][3])

    def test_cli_returns_two_and_prints_no_summary_on_policy_failure(
        self,
    ) -> None:
        failure = DailyGrayLaunchdPolicyError("policy drift")
        with (
            patch.object(runner, "run", side_effect=failure),
            patch.object(runner, "_print_summary") as print_summary,
            patch(
                "sys.argv",
                ["daily_gray_runner", "--predict-date", "2026-08-03"],
            ),
        ):
            try:
                exit_code = runner.main()
            except DailyGrayLaunchdPolicyError:
                self.fail("CLI must convert policy errors to a non-zero exit code")

        self.assertEqual(exit_code, 2)
        print_summary.assert_not_called()

    def test_cli_returns_one_when_frozen_batch_succeeds_with_isolated_identities(
        self,
    ) -> None:
        summary = runner.RunnerSummary(
            predict_date="2026-08-03",
            is_trading_day=True,
            isolated_active_daily_scheme_ids=("outside-policy",),
        )
        with (
            patch.object(runner, "run", return_value=summary),
            patch.object(runner, "_print_summary") as print_summary,
            patch(
                "sys.argv",
                ["daily_gray_runner", "--predict-date", "2026-08-03"],
            ),
        ):
            exit_code = runner.main()

        self.assertEqual(exit_code, 1)
        print_summary.assert_called_once_with(summary)

    def test_cli_returns_zero_on_non_trading_day_with_isolated_identities(
        self,
    ) -> None:
        summary = runner.RunnerSummary(
            predict_date="2026-08-03",
            is_trading_day=False,
            isolated_active_daily_scheme_ids=("outside-policy",),
        )
        with (
            patch.object(runner, "run", return_value=summary),
            patch.object(runner, "_print_summary") as print_summary,
            patch(
                "sys.argv",
                ["daily_gray_runner", "--predict-date", "2026-08-03"],
            ),
        ):
            exit_code = runner.main()

        self.assertEqual(exit_code, 0)
        print_summary.assert_called_once_with(summary)

    def test_cli_rejects_unknown_and_empty_only_without_summary(
        self,
    ) -> None:
        discovered = [_config("alpha")]
        cases = (
            ("alpha,outside-policy", "outside-policy"),
            (",,", "empty"),
            ("", "empty"),
        )
        for raw_only, expected in cases:
            with (
                self.subTest(raw_only=raw_only),
                patch.object(
                    runner,
                    "discover_schemes",
                    return_value=discovered,
                ),
                patch.object(
                    runner,
                    "load_daily_gray_launchd_policy",
                    return_value=_policy([("alpha", "light", None)]),
                ),
                patch.object(
                    runner,
                    "create_engine_from_env",
                ) as create_engine,
                patch.object(runner, "is_trading_day") as trading_day,
                patch.object(
                    runner,
                    "ThreadPoolExecutor",
                ) as executor_pool,
                patch.object(runner, "execute_scheme") as execute,
                patch.object(runner, "_print_summary") as print_summary,
                patch(
                    "sys.argv",
                    ["daily_gray_runner", "--only", raw_only],
                ),
                self.assertLogs(runner.logger, level="ERROR") as logs,
            ):
                exit_code = runner.main()

            self.assertEqual(exit_code, 2)
            self.assertIn(expected, " ".join(logs.output))
            create_engine.assert_not_called()
            trading_day.assert_not_called()
            executor_pool.assert_not_called()
            execute.assert_not_called()
            print_summary.assert_not_called()

    def test_success_and_failure_summary_remains_isolated(self) -> None:
        configs = [_config("alpha"), _config("beta")]

        def execute(config, *args, **kwargs):
            if config.scheme_id == "beta":
                raise RuntimeError("isolated failure")
            return _success(records=2)

        result = self._run_with_fakes(
            configs,
            _policy(
                [
                    ("alpha", "light", None),
                    ("beta", "light", None),
                ]
            ),
            execute_side_effect=execute,
        )

        self.assertEqual(result.summary.total, 2)
        self.assertEqual(result.summary.success, 1)
        self.assertEqual(result.summary.failed, 1)
        self.assertEqual(result.summary.records_written, 2)
        self.assertEqual(
            {row[0]: row[1] for row in result.summary.details or []},
            {"alpha": "success", "beta": "failed"},
        )

    def test_malformed_result_does_not_partially_double_tally(self) -> None:
        malformed = SimpleNamespace(
            status="success",
            records_written="not-an-integer",
            error_msg=None,
        )
        result = self._run_with_fakes(
            [_config("alpha")],
            _policy([("alpha", "light", None)]),
            execute_side_effect=lambda *args, **kwargs: malformed,
        )

        self.assertEqual(result.summary.total, 1)
        self.assertEqual(result.summary.success, 0)
        self.assertEqual(result.summary.failed, 1)
        self.assertEqual(len(result.summary.details or []), 1)

    def test_success_log_failure_does_not_double_tally(self) -> None:
        with patch.object(
            runner.logger,
            "info",
            side_effect=RuntimeError("logging handler failed"),
        ):
            result = self._run_with_fakes(
                [_config("alpha")],
                _policy([("alpha", "light", None)]),
            )

        self.assertEqual(result.summary.total, 1)
        self.assertEqual(result.summary.success, 1)
        self.assertEqual(result.summary.failed, 0)
        self.assertEqual(len(result.summary.details or []), 1)

    def test_non_trading_day_still_preflights_policy_without_execution(
        self,
    ) -> None:
        discovered = [_config("alpha")]
        result = self._run_with_fakes(
            discovered,
            _policy([("alpha", "light", None)]),
            trading=False,
        )

        result.discovery.assert_called_once_with(strict=True)
        result.loader.assert_called_once()
        result.create_engine.assert_called_once_with()
        result.trading_day.assert_called_once_with(
            result.engine,
            "2026-08-03",
        )
        result.engine.dispose.assert_called_once_with()
        result.execute.assert_not_called()
        self.assertFalse(result.summary.is_trading_day)
        self.assertEqual(result.summary.total, 0)


if __name__ == "__main__":
    unittest.main()
