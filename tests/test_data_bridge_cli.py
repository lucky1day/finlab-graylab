from __future__ import annotations

import inspect
import os
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, call, patch


class DataBridgeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        from scripts import refresh_data_bridge_current as command

        self._publish_marker = patch.dict(
            os.environ,
            {"BFL_DATABRIDGE_PRODUCER": "launchd-one-shot"},
        )
        self._publish_marker.start()
        self._publisher_lock = patch.object(
            command,
            "_publisher_lock",
            side_effect=lambda _config: nullcontext(True),
            create=True,
        )
        self._publisher_lock.start()

    def tearDown(self) -> None:
        self._publisher_lock.stop()
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

    def test_refresh_uses_mysql_round_source_and_continuity_authority(self) -> None:
        from scripts import refresh_data_bridge_current as command

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
            ) as resolve,
            patch.object(
                command,
                "MySqlDataBridgeRoundBuilder",
                return_value=builder,
            ) as builder_factory,
            patch.object(command, "run_full_refresh", return_value=result) as refresh,
        ):
            actual = command.refresh_current(
                refresh_date="2026-07-29",
                expected_feature_date="2026-07-28",
                publish=False,
                config=config,
            )

        self.assertIs(actual, result)
        resolve.assert_called_once_with(
            config,
            feature_date="2026-07-28",
            engine=engine,
        )
        builder_factory.assert_called_once_with(engine=engine, config=config)
        self.assertEqual(
            refresh.call_args.kwargs,
            {
                "config": config,
                "expected_daily_date": "2026-07-28",
                "refresh_date": "2026-07-29",
                "publish": False,
                "continuity_authority": authority,
                "round_builder": builder,
                "enforce_legacy_publication_fence": False,
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
            ),
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
            patch.object(command, "refresh_current", return_value=result),
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
        self.assertEqual(blocked.call_count, 2)

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
