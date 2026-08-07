from __future__ import annotations

import inspect
import os
import unittest
from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


class DataBridgeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        from scripts import refresh_data_bridge_current as command

        self._legacy_publish_deadline = datetime(
            2099,
            1,
            1,
            tzinfo=SHANGHAI,
        )
        self._publish_marker = patch.dict(
            os.environ,
            {"BFL_DATABRIDGE_PRODUCER": "launchd-one-shot"},
        )
        self._publish_marker.start()
        self._refresh_deadline = patch.object(
            command,
            "_refresh_deadline_at",
            side_effect=lambda config, *, refresh_date: getattr(
                config,
                "deadline_at",
                lambda _refresh_date: self._legacy_publish_deadline,
            )(refresh_date),
        )
        self._refresh_deadline.start()
        self._sleep = patch.object(command.time, "sleep")
        self._sleep.start()
        self._publisher_lock = patch.object(
            command,
            "_publisher_lock",
            side_effect=lambda _config: nullcontext(True),
            create=True,
        )
        self._publisher_lock.start()

    def tearDown(self) -> None:
        self._publisher_lock.stop()
        self._sleep.stop()
        self._refresh_deadline.stop()
        self._publish_marker.stop()

    def test_expected_feature_date_uses_shared_calendar(self) -> None:
        from scripts import refresh_data_bridge_current as command

        engine = SimpleNamespace(dispose=Mock())
        calendar = SimpleNamespace(
            previous_trading_day=Mock(return_value="2026-07-28")
        )
        with (
            patch.object(command, "create_sqlalchemy_engine", return_value=engine),
            patch.object(command, "get_calendar", return_value=calendar),
        ):
            actual = command.expected_daily_date(refresh_date="2026-07-29")

        self.assertEqual(actual, "2026-07-28")
        calendar.previous_trading_day.assert_called_once_with("2026-07-29")
        engine.dispose.assert_called_once_with()

    def test_refresh_limits_legacy_fallback_to_admitted_publish(self) -> None:
        from scripts import refresh_data_bridge_current as command

        scenarios = (
            ("dry-run", False, "launchd-one-shot", False),
            ("publish admitted", True, "launchd-one-shot", True),
            ("publish direct", True, None, False),
        )
        for label, publish, marker, expected_fallback in scenarios:
            with self.subTest(label=label):
                engine = SimpleNamespace(dispose=Mock())
                config = SimpleNamespace()
                authority = object()
                builder = object()
                result = SimpleNamespace()
                environment = (
                    {command.LAUNCHD_PUBLISHER_ENV: marker}
                    if marker is not None
                    else {}
                )
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(
                        command,
                        "create_sqlalchemy_engine",
                        return_value=engine,
                    ),
                    patch.object(
                        command,
                        "resolve_databridge_continuity_authority_from_engine",
                        return_value=authority,
                    ) as resolve,
                    patch.object(
                        command,
                        "MySqlDataBridgeRoundBuilder",
                        return_value=builder,
                    ) as builder_factory,
                    patch.object(
                        command,
                        "run_full_refresh",
                        return_value=result,
                    ) as refresh,
                ):
                    actual = command.refresh_current(
                        refresh_date="2026-07-29",
                        expected_feature_date="2026-07-28",
                        publish=publish,
                        config=config,
                    )

                self.assertIs(actual, result)
                resolve.assert_called_once_with(
                    config,
                    feature_date="2026-07-28",
                    engine=engine,
                    allow_legacy_v1_period_fallback=expected_fallback,
                )
                builder_factory.assert_called_once_with(
                    engine=engine,
                    config=config,
                )
                self.assertEqual(
                    refresh.call_args.kwargs,
                    {
                        "config": config,
                        "expected_daily_date": "2026-07-28",
                        "refresh_date": "2026-07-29",
                        "publish": publish,
                        "continuity_authority": authority,
                        "round_builder": builder,
                        "require_launchd_round_builder": True,
                    },
                )
                engine.dispose.assert_called_once_with()

    def test_publish_writes_ready_only_after_strict_current_read(self) -> None:
        from scripts import refresh_data_bridge_current as command

        config = SimpleNamespace()
        result = SimpleNamespace(
            state={
                "generation_id": "full-test",
                "business_digest": "a" * 64,
            },
            published=True,
            rounds_completed=2,
            duration_sec=1.25,
        )
        current = SimpleNamespace(
            state={
                "generation_id": "full-test",
                "refresh_date": "2026-07-29",
                "business_digest": "a" * 64,
            }
        )
        with (
            patch.object(command.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(command, "expected_daily_date", return_value="2026-07-28"),
            patch.object(command, "refresh_current", return_value=result) as refresh,
            patch.object(command, "check_current_dataset", return_value=current) as check,
            patch.object(command, "_write_blocked_gate") as blocked,
            patch.object(command, "_write_ready_gate") as ready,
        ):
            exit_code, payload = command.run_command(
                "publish",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "ok")
        blocked.assert_called_once_with(
            config,
            refresh_date="2026-07-29",
            expected_feature_date="2026-07-28",
            check_name="refresh_pending",
        )
        refresh.assert_called_once_with(
            refresh_date="2026-07-29",
            expected_feature_date="2026-07-28",
            publish=True,
            config=config,
            deadline_at=self._legacy_publish_deadline,
        )
        check.assert_called_once_with(
            config,
            required_refresh_date="2026-07-29",
            expected_daily_date="2026-07-28",
            expected_generation_id="full-test",
            expected_business_digest="a" * 64,
            strict_read_only=True,
            require_source_provenance=True,
        )
        ready.assert_called_once_with(
            config,
            refresh_date="2026-07-29",
            expected_feature_date="2026-07-28",
            current=current,
        )

    def test_publish_retries_failed_refresh_before_deadline(self) -> None:
        from scripts import refresh_data_bridge_current as command

        deadline = datetime(2026, 7, 29, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = SimpleNamespace(deadline_at=lambda _refresh_date: deadline)
        first_failure = (
            1,
            {
                "status": "failed",
                "mode": "publish",
                "error": "local MySQL DataBridge refresh or validation failed",
            },
        )
        success = (
            0,
            {
                "status": "ok",
                "mode": "publish",
            },
        )
        sleep = Mock()
        with (
            patch.object(
                command,
                "_run_refresh_with_config",
                side_effect=[first_failure, success],
            ) as refresh,
            patch.object(
                command,
                "_now",
                side_effect=[
                    datetime(2026, 7, 29, 6, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
                    datetime(2026, 7, 29, 6, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
                    datetime(2026, 7, 29, 6, 31, tzinfo=ZoneInfo("Asia/Shanghai")),
                ],
                create=True,
            ),
            patch.object(command, "time", SimpleNamespace(sleep=sleep), create=True),
        ):
            actual = command._run_publish_with_retries(
                refresh_date="2026-07-29",
                config=config,
                expected_feature_date="2026-07-28",
            )

        self.assertEqual(actual, success)
        self.assertEqual(
            refresh.call_args_list,
            [
                call(
                    "publish",
                    refresh_date="2026-07-29",
                    config=config,
                    expected_feature_date="2026-07-28",
                    deadline_at=deadline,
                ),
                call(
                    "publish",
                    refresh_date="2026-07-29",
                    config=config,
                    expected_feature_date="2026-07-28",
                    deadline_at=deadline,
                ),
            ],
        )
        sleep.assert_called_once_with(30)

    def test_publish_retries_through_real_refresh_current_boundary(self) -> None:
        from scripts import refresh_data_bridge_current as command
        from shared.data_bridge.refresh import DataBridgeRefreshError

        config = SimpleNamespace()
        engine = SimpleNamespace(dispose=Mock())
        authority = object()
        builder = object()
        result = SimpleNamespace(
            state={"generation_id": "full-test", "business_digest": "a" * 64},
            published=True,
            rounds_completed=2,
            duration_sec=1.25,
        )
        current = SimpleNamespace(
            state={
                "generation_id": "full-test",
                "refresh_date": "2026-07-29",
                "business_digest": "a" * 64,
            }
        )
        with (
            patch.object(command.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(command, "expected_daily_date", return_value="2026-07-28"),
            patch.object(command, "create_sqlalchemy_engine", return_value=engine),
            patch.object(
                command,
                "resolve_databridge_continuity_authority_from_engine",
                return_value=authority,
            ),
            patch.object(command, "MySqlDataBridgeRoundBuilder", return_value=builder),
            patch.object(
                command,
                "run_full_refresh",
                side_effect=[DataBridgeRefreshError("source unavailable"), result],
            ) as full_refresh,
            patch.object(command, "check_current_dataset", return_value=current),
            patch.object(command, "_write_blocked_gate") as blocked,
            patch.object(command, "_write_ready_gate") as ready,
        ):
            exit_code, payload = command.run_command(
                "publish",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(full_refresh.call_count, 2)
        self.assertTrue(
            all(
                refresh_call.kwargs["deadline_at"]
                == self._legacy_publish_deadline
                for refresh_call in full_refresh.call_args_list
            )
        )
        command.time.sleep.assert_called_once_with(
            command.PUBLISH_REFRESH_RETRY_DELAY_SEC
        )
        self.assertEqual(engine.dispose.call_count, 2)
        self.assertEqual(
            blocked.call_args_list,
            [
                call(
                    config,
                    refresh_date="2026-07-29",
                    expected_feature_date="2026-07-28",
                    check_name="refresh_pending",
                ),
                call(
                    config,
                    refresh_date="2026-07-29",
                    expected_feature_date="2026-07-28",
                    check_name="refresh_failed",
                ),
                call(
                    config,
                    refresh_date="2026-07-29",
                    expected_feature_date="2026-07-28",
                    check_name="refresh_pending",
                ),
            ],
        )
        ready.assert_called_once_with(
            config,
            refresh_date="2026-07-29",
            expected_feature_date="2026-07-28",
            current=current,
        )

    def test_publish_does_not_retry_or_sleep_at_deadline(self) -> None:
        from scripts import refresh_data_bridge_current as command

        deadline = datetime(2026, 7, 29, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = SimpleNamespace(deadline_at=lambda _refresh_date: deadline)
        failure = (
            1,
            {
                "status": "failed",
                "mode": "publish",
                "error": "local MySQL DataBridge refresh or validation failed",
            },
        )
        sleep = Mock()
        with (
            patch.object(
                command,
                "_run_refresh_with_config",
                return_value=failure,
            ) as refresh,
            patch.object(
                command,
                "_now",
                side_effect=[
                    datetime(2026, 7, 29, 6, 59, 59, tzinfo=ZoneInfo("Asia/Shanghai")),
                    deadline,
                ],
                create=True,
            ),
            patch.object(command, "time", SimpleNamespace(sleep=sleep), create=True),
        ):
            actual = command._run_publish_with_retries(
                refresh_date="2026-07-29",
                config=config,
                expected_feature_date="2026-07-28",
            )

        self.assertEqual(actual, failure)
        refresh.assert_called_once_with(
            "publish",
            refresh_date="2026-07-29",
            config=config,
            expected_feature_date="2026-07-28",
            deadline_at=deadline,
        )
        sleep.assert_not_called()

    def test_publish_clips_retry_sleep_to_remaining_deadline(self) -> None:
        from scripts import refresh_data_bridge_current as command

        deadline = datetime(2026, 7, 29, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = SimpleNamespace(deadline_at=lambda _refresh_date: deadline)
        failure = (
            1,
            {
                "status": "failed",
                "mode": "publish",
                "error": "local MySQL DataBridge refresh or validation failed",
            },
        )
        sleep = Mock()
        with (
            patch.object(
                command,
                "_run_refresh_with_config",
                return_value=failure,
            ) as refresh,
            patch.object(
                command,
                "_now",
                side_effect=[
                    datetime(2026, 7, 29, 6, 59, 50, tzinfo=ZoneInfo("Asia/Shanghai")),
                    datetime(2026, 7, 29, 6, 59, 50, tzinfo=ZoneInfo("Asia/Shanghai")),
                    deadline,
                ],
                create=True,
            ),
            patch.object(command, "time", SimpleNamespace(sleep=sleep), create=True),
        ):
            actual = command._run_publish_with_retries(
                refresh_date="2026-07-29",
                config=config,
                expected_feature_date="2026-07-28",
            )

        self.assertEqual(actual, failure)
        refresh.assert_called_once()
        sleep.assert_called_once_with(10.0)

    def test_publish_does_not_retry_configuration_result(self) -> None:
        from scripts import refresh_data_bridge_current as command

        deadline = datetime(2026, 7, 29, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = SimpleNamespace(deadline_at=lambda _refresh_date: deadline)
        configuration_error = (
            2,
            {
                "status": "configuration_error",
                "mode": "publish",
                "error": "local MySQL DataBridge configuration is invalid or incomplete",
            },
        )
        sleep = Mock()
        with (
            patch.object(
                command,
                "_run_refresh_with_config",
                return_value=configuration_error,
            ) as refresh,
            patch.object(
                command,
                "_now",
                return_value=datetime(
                    2026,
                    7,
                    29,
                    6,
                    30,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
                create=True,
            ),
            patch.object(command, "time", SimpleNamespace(sleep=sleep), create=True),
        ):
            actual = command._run_publish_with_retries(
                refresh_date="2026-07-29",
                config=config,
                expected_feature_date="2026-07-28",
            )

        self.assertEqual(actual, configuration_error)
        refresh.assert_called_once()
        sleep.assert_not_called()

    def test_publish_returns_sanitized_failure_without_late_first_attempt(self) -> None:
        from scripts import refresh_data_bridge_current as command

        deadline = datetime(2026, 7, 29, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = SimpleNamespace(deadline_at=lambda _refresh_date: deadline)
        sleep = Mock()
        with (
            patch.object(command, "_run_refresh_with_config") as refresh,
            patch.object(command, "_now", return_value=deadline, create=True),
            patch.object(command, "time", SimpleNamespace(sleep=sleep), create=True),
        ):
            exit_code, payload = command._run_publish_with_retries(
                refresh_date="2026-07-29",
                config=config,
                expected_feature_date="2026-07-28",
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["mode"], "publish")
        self.assertNotIn("deadline", payload["error"])
        refresh.assert_not_called()
        sleep.assert_not_called()

    def test_refresh_current_forwards_deadline_to_run_full_refresh(self) -> None:
        from scripts import refresh_data_bridge_current as command

        deadline = datetime(2026, 7, 29, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        engine = SimpleNamespace(dispose=Mock())
        config = SimpleNamespace()
        authority = object()
        builder = object()
        result = SimpleNamespace()
        with (
            patch.object(command, "create_sqlalchemy_engine", return_value=engine),
            patch.object(
                command,
                "resolve_databridge_continuity_authority_from_engine",
                return_value=authority,
            ),
            patch.object(
                command,
                "MySqlDataBridgeRoundBuilder",
                return_value=builder,
            ),
            patch.object(
                command,
                "run_full_refresh",
                return_value=result,
            ) as refresh,
        ):
            actual = command.refresh_current(
                refresh_date="2026-07-29",
                expected_feature_date="2026-07-28",
                publish=True,
                config=config,
                deadline_at=deadline,
            )

        self.assertIs(actual, result)
        self.assertEqual(refresh.call_args.kwargs["deadline_at"], deadline)
        engine.dispose.assert_called_once_with()

    def test_non_publish_refresh_does_not_retry(self) -> None:
        from scripts import refresh_data_bridge_current as command

        config = SimpleNamespace()
        failure = (
            1,
            {
                "status": "failed",
                "mode": "dry-run",
                "error": "local MySQL DataBridge refresh or validation failed",
            },
        )
        with (
            patch.object(command.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(command, "expected_daily_date", return_value="2026-07-28"),
            patch.object(
                command,
                "_run_refresh_with_config",
                return_value=failure,
            ) as refresh,
        ):
            actual = command.run_command("dry-run", refresh_date="2026-07-29")

        self.assertEqual(actual, failure)
        refresh.assert_called_once_with(
            "dry-run",
            refresh_date="2026-07-29",
            config=config,
            expected_feature_date="2026-07-28",
        )

    def test_publish_rejects_missing_launchd_one_shot_marker(self) -> None:
        from scripts import refresh_data_bridge_current as command

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(command.DataBridgeRefreshConfig, "from_env") as config,
            patch.object(command, "expected_daily_date") as expected,
            patch.object(command, "refresh_current") as refresh,
            patch.object(command, "_write_blocked_gate") as blocked,
            patch.object(command, "_write_ready_gate") as ready,
        ):
            exit_code, payload = command.run_command(
                "publish",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "configuration_error")
        self.assertEqual(
            payload["error"],
            "local MySQL DataBridge publish requires launchd one-shot admission",
        )
        config.assert_not_called()
        expected.assert_not_called()
        refresh.assert_not_called()
        blocked.assert_not_called()
        ready.assert_not_called()

    def test_publish_lock_conflict_never_refreshes_or_writes_gate(self) -> None:
        from scripts import refresh_data_bridge_current as command

        config = SimpleNamespace()
        with (
            patch.object(command.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(command, "expected_daily_date", return_value="2026-07-28"),
            patch.object(
                command,
                "_publisher_lock",
                return_value=nullcontext(False),
                create=True,
            ),
            patch.object(command, "refresh_current") as refresh,
            patch.object(command, "_write_blocked_gate") as blocked,
            patch.object(command, "_write_ready_gate") as ready,
        ):
            exit_code, payload = command.run_command(
                "publish",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(
            payload["error"],
            "local MySQL DataBridge publisher is already running",
        )
        refresh.assert_not_called()
        blocked.assert_not_called()
        ready.assert_not_called()

    def test_publish_failure_overwrites_any_prior_ready_with_blocked(self) -> None:
        from scripts import refresh_data_bridge_current as command
        from shared.data_bridge.refresh import DataBridgeRefreshError

        config = SimpleNamespace()
        with (
            patch.object(command.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(command, "expected_daily_date", return_value="2026-07-28"),
            patch.object(
                command,
                "refresh_current",
                side_effect=DataBridgeRefreshError("source unavailable"),
            ) as refresh,
            patch.object(command, "_write_blocked_gate") as blocked,
            patch.object(command, "_write_ready_gate") as ready,
        ):
            exit_code, payload = command.run_command(
                "publish",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(
            blocked.call_args_list,
            [
                gate_call
                for _ in range(command.PUBLISH_REFRESH_MAX_ATTEMPTS)
                for gate_call in (
                    call(
                        config,
                        refresh_date="2026-07-29",
                        expected_feature_date="2026-07-28",
                        check_name="refresh_pending",
                    ),
                    call(
                        config,
                        refresh_date="2026-07-29",
                        expected_feature_date="2026-07-28",
                        check_name="refresh_failed",
                    ),
                )
            ],
        )
        self.assertEqual(refresh.call_count, command.PUBLISH_REFRESH_MAX_ATTEMPTS)
        self.assertTrue(
            all(
                refresh_call.kwargs["deadline_at"]
                == self._legacy_publish_deadline
                for refresh_call in refresh.call_args_list
            )
        )
        ready.assert_not_called()

    def test_publish_initial_blocked_gate_io_failure_stops_before_refresh(self) -> None:
        from scripts import refresh_data_bridge_current as command

        config = SimpleNamespace()
        with (
            patch.object(command.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(command, "expected_daily_date", return_value="2026-07-28"),
            patch.object(
                command,
                "_write_blocked_gate",
                side_effect=OSError("dsn=password=do-not-leak"),
            ) as blocked,
            patch.object(command, "refresh_current") as refresh,
            patch.object(command, "_write_ready_gate") as ready,
        ):
            exit_code, payload = command.run_command(
                "publish",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertNotIn("do-not-leak", str(payload))
        blocked.assert_called_once()
        refresh.assert_not_called()
        ready.assert_not_called()

    def test_publish_refresh_failure_keeps_primary_result_when_blocked_write_fails(
        self,
    ) -> None:
        from scripts import refresh_data_bridge_current as command
        from shared.data_bridge.refresh import DataBridgeRefreshError

        config = SimpleNamespace()
        with (
            patch.object(command.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(command, "expected_daily_date", return_value="2026-07-28"),
            patch.object(
                command,
                "refresh_current",
                side_effect=DataBridgeRefreshError("source unavailable"),
            ),
            patch.object(command, "_write_blocked_gate", side_effect=[None, OSError("password=do-not-leak")]),
            patch.object(command, "_write_ready_gate") as ready,
        ):
            exit_code, payload = command.run_command(
                "publish",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertNotIn("do-not-leak", str(payload))
        ready.assert_not_called()

    def test_publish_ready_gate_io_failure_returns_failed_and_attempts_blocked(
        self,
    ) -> None:
        from scripts import refresh_data_bridge_current as command

        config = SimpleNamespace()
        result = SimpleNamespace(
            state={"generation_id": "full-test", "business_digest": "a" * 64},
            published=True,
            rounds_completed=2,
            duration_sec=1.25,
        )
        current = SimpleNamespace(
            state={
                "generation_id": "full-test",
                "refresh_date": "2026-07-29",
                "business_digest": "a" * 64,
            }
        )
        with (
            patch.object(command.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(command, "expected_daily_date", return_value="2026-07-28"),
            patch.object(command, "refresh_current", return_value=result) as refresh,
            patch.object(command, "check_current_dataset", return_value=current),
            patch.object(command, "_write_blocked_gate") as blocked,
            patch.object(
                command,
                "_write_ready_gate",
                side_effect=OSError("password=do-not-leak"),
            ),
        ):
            exit_code, payload = command.run_command(
                "publish",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertNotIn("do-not-leak", str(payload))
        refresh.assert_called_once()
        self.assertEqual(
            blocked.call_args_list,
            [
                call(
                    config,
                    refresh_date="2026-07-29",
                    expected_feature_date="2026-07-28",
                    check_name="refresh_pending",
                ),
                call(
                    config,
                    refresh_date="2026-07-29",
                    expected_feature_date="2026-07-28",
                    check_name="refresh_failed",
                ),
            ],
        )

    def test_unexpected_publish_exception_is_sanitized_and_blocks_gate(
        self,
    ) -> None:
        """未知运行时异常也只能走固定的 CLI 失败出口。"""
        from scripts import refresh_data_bridge_current as command

        config = SimpleNamespace()
        with (
            patch.object(command.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(command, "expected_daily_date", return_value="2026-07-28"),
            patch.object(
                command,
                "refresh_current",
                side_effect=RuntimeError("mysql://user:password=do-not-leak"),
            ) as refresh,
            patch.object(command, "_write_blocked_gate") as blocked,
            patch.object(command, "_write_ready_gate") as ready,
        ):
            exit_code, payload = command.run_command(
                "publish",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error"], "local MySQL DataBridge refresh failed")
        self.assertNotIn("do-not-leak", str(payload))
        refresh.assert_called_once()
        self.assertEqual(blocked.call_count, 2)
        ready.assert_not_called()

    def test_dry_run_does_not_write_a_gate(self) -> None:
        from scripts import refresh_data_bridge_current as command

        config = SimpleNamespace()
        result = SimpleNamespace(
            state={"generation_id": "dry"},
            published=False,
            rounds_completed=2,
            duration_sec=0.1,
        )
        with (
            patch.object(command.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(command, "expected_daily_date", return_value="2026-07-28"),
            patch.object(command, "refresh_current", return_value=result) as refresh,
            patch.object(command, "_write_blocked_gate") as blocked,
            patch.object(command, "_write_ready_gate") as ready,
        ):
            exit_code, payload = command.run_command(
                "dry-run",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["mode"], "dry-run")
        refresh.assert_called_once()
        blocked.assert_not_called()
        ready.assert_not_called()

    def test_check_only_is_strict_and_does_not_write_a_gate(self) -> None:
        from scripts import refresh_data_bridge_current as command

        current = SimpleNamespace(state={"generation_id": "current"})
        with (
            patch.object(command, "check_current", return_value=current) as check,
            patch.object(command, "_write_blocked_gate") as blocked,
            patch.object(command, "_write_ready_gate") as ready,
        ):
            exit_code, payload = command.run_command(
                "check-only",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["state"], current.state)
        check.assert_called_once_with(refresh_date="2026-07-29")
        blocked.assert_not_called()
        ready.assert_not_called()

    def test_check_only_preserves_success_attempt_without_error(self) -> None:
        from scripts import refresh_data_bridge_current as command

        current = SimpleNamespace(
            state={
                "generation_id": "current",
                "last_attempt": {
                    "status": "success",
                    "refresh_date": "2026-07-29",
                    "error": None,
                },
            }
        )
        with patch.object(command, "check_current", return_value=current):
            exit_code, payload = command.run_command(
                "check-only",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 0)
        self.assertIsNone(payload["state"]["last_attempt"]["error"])

    def test_check_only_redacts_legacy_raw_failed_attempt_error(self) -> None:
        from scripts import refresh_data_bridge_current as command

        current = SimpleNamespace(
            state={
                "generation_id": "current",
                "last_attempt": {
                    "status": "failed",
                    "error": "mysql://user:password=do-not-leak",
                },
            }
        )
        with patch.object(command, "check_current", return_value=current):
            exit_code, payload = command.run_command(
                "check-only",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 0)
        self.assertNotIn("do-not-leak", str(payload))
        self.assertEqual(
            payload["state"]["last_attempt"]["error"],
            "refresh_failed",
        )

    def test_check_only_projects_malformed_failed_attempt_error(self) -> None:
        from scripts import refresh_data_bridge_current as command

        current = SimpleNamespace(
            state={
                "generation_id": "current",
                "last_attempt": {"status": "failed", "error": ["secret"]},
            }
        )
        with patch.object(command, "check_current", return_value=current):
            exit_code, payload = command.run_command(
                "check-only",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            payload["state"]["last_attempt"]["error"],
            "refresh_failed",
        )

    def test_check_only_omits_unknown_legacy_state_fields(self) -> None:
        from scripts import refresh_data_bridge_current as command

        current = SimpleNamespace(
            state={
                "generation_id": "current",
                "refresh_date": "2026-07-29",
                "legacy_debug": {
                    "dsn": "mysql://user:password=do-not-leak",
                },
            }
        )
        with patch.object(command, "check_current", return_value=current):
            exit_code, payload = command.run_command(
                "check-only",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["state"]["generation_id"], "current")
        self.assertNotIn("legacy_debug", payload["state"])
        self.assertNotIn("do-not-leak", str(payload))

    def test_configuration_error_redacts_connection_details(self) -> None:
        from scripts import refresh_data_bridge_current as command

        with patch.object(
            command,
            "expected_daily_date",
            side_effect=ValueError("password=do-not-leak"),
        ):
            exit_code, payload = command.run_command(
                "dry-run",
                refresh_date="2026-07-29",
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "configuration_error")
        self.assertNotIn("do-not-leak", str(payload))

    def test_direct_cli_does_not_depend_on_scheduler_main_or_http_client(self) -> None:
        from scripts import refresh_data_bridge_current as command

        source = inspect.getsource(command)
        self.assertNotIn("scheduler.main", source)
        self.assertNotIn("DataBridgeClient", source)


if __name__ == "__main__":
    unittest.main()
