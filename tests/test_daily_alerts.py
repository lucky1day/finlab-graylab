from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch


class DailyAlertTests(unittest.TestCase):
    def test_dispatch_does_not_wait_for_a_slow_sink(self) -> None:
        from scheduler.alerts import (
            AlertDispatcher,
            AlertEvent,
            AlertSettings,
        )

        entered = threading.Event()
        release = threading.Event()

        def slow_runner(*args: object, **kwargs: object) -> object:
            entered.set()
            release.wait(timeout=2)
            return subprocess.CompletedProcess(args[0], 0, "", "")

        dispatcher = AlertDispatcher(
            AlertSettings(
                command=(Path("/opt/local/bin/slow-alert-sink"),),
                notification_center=False,
            ),
            runner=slow_runner,
            queue_capacity=2,
        )
        event = AlertEvent.now(
            code="SLA_TARGET_MISSING",
            severity="critical",
            predict_date="2026-07-24",
            occurrence_id=70,
            message="08:00 target availability is 24/25",
        )

        started_at = time.monotonic()
        result = dispatcher.dispatch(event)
        elapsed = time.monotonic() - started_at

        self.assertLess(elapsed, 0.1)
        self.assertEqual(result.command_status, "queued")
        self.assertTrue(entered.wait(timeout=1))
        release.set()
        self.assertTrue(dispatcher.drain(timeout_sec=1))
        self.assertTrue(dispatcher.close(timeout_sec=1))
        self.assertFalse(dispatcher.worker_alive)

    def test_queue_full_is_logged_and_reported_as_dropped(self) -> None:
        from scheduler.alerts import (
            AlertDispatcher,
            AlertEvent,
            AlertSettings,
        )

        entered = threading.Event()
        release = threading.Event()

        def blocked_runner(*args: object, **kwargs: object) -> object:
            entered.set()
            release.wait(timeout=2)
            return subprocess.CompletedProcess(args[0], 0, "", "")

        alert_logger = logging.getLogger("tests.daily-alert.queue-full")
        dispatcher = AlertDispatcher(
            AlertSettings(
                command=(Path("/opt/local/bin/blocked-alert-sink"),),
                notification_center=False,
            ),
            runner=blocked_runner,
            logger=alert_logger,
            queue_capacity=1,
        )

        def event(occurrence_id: int) -> AlertEvent:
            return AlertEvent.now(
                code="SLA_TARGET_MISSING",
                severity="critical",
                predict_date="2026-07-24",
                occurrence_id=occurrence_id,
                message="08:00 target missing",
            )

        with self.assertLogs(alert_logger, level=logging.ERROR) as captured:
            first = dispatcher.dispatch(event(901))
            self.assertTrue(entered.wait(timeout=1))
            second = dispatcher.dispatch(event(902))
            dropped = dispatcher.dispatch(event(903))

        self.assertEqual(first.command_status, "queued")
        self.assertEqual(second.command_status, "queued")
        self.assertEqual(dropped.command_status, "dropped")
        drop_records = [
            json.loads(record.getMessage())
            for record in captured.records
            if '"event":"alert_delivery_dropped"' in record.getMessage()
        ]
        self.assertEqual(len(drop_records), 1)
        self.assertEqual(drop_records[0]["reason"], "queue_full")
        self.assertEqual(drop_records[0]["queue_capacity"], 1)
        self.assertEqual(drop_records[0]["code"], "SLA_TARGET_MISSING")
        release.set()
        self.assertTrue(dispatcher.close(timeout_sec=1))
        self.assertFalse(dispatcher.worker_alive)

    def test_short_lived_dispatchers_auto_exit_without_explicit_close(
        self,
    ) -> None:
        from scheduler.alerts import (
            AlertDispatcher,
            AlertEvent,
            AlertSettings,
        )

        thread_name = "bond-factor-lab-alert-dispatcher"
        baseline = sum(
            thread.name == thread_name
            for thread in threading.enumerate()
        )
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ["/opt/local/bin/alert-sink"],
                0,
                "",
                "",
            )
        )
        dispatchers = []
        for occurrence_id in range(910, 918):
            dispatcher = AlertDispatcher(
                AlertSettings(
                    command=(Path("/opt/local/bin/alert-sink"),),
                    notification_center=False,
                ),
                runner=runner,
                idle_timeout_sec=0.02,
            )
            dispatcher.dispatch(
                AlertEvent.now(
                    code="SLA_TARGET_MISSING",
                    severity="critical",
                    predict_date="2026-07-24",
                    occurrence_id=occurrence_id,
                    message="target missing",
                )
            )
            self.assertTrue(dispatcher.drain(timeout_sec=1))
            dispatchers.append(dispatcher)

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            current = sum(
                thread.name == thread_name
                for thread in threading.enumerate()
            )
            if current == baseline:
                break
            time.sleep(0.01)
        self.assertEqual(
            sum(
                thread.name == thread_name
                for thread in threading.enumerate()
            ),
            baseline,
        )
        self.assertTrue(
            all(not dispatcher.worker_alive for dispatcher in dispatchers)
        )

    def test_from_env_disables_invalid_optional_sinks_and_records_errors(
        self,
    ) -> None:
        from scheduler.alerts import AlertDispatcher, AlertSettings

        alert_logger = logging.getLogger("tests.daily-alert.bad-config")
        cases = (
            (
                {
                    "BOND_ALERT_SINK_COMMAND_JSON": "not-json",
                    "BOND_ALERT_NOTIFICATION_CENTER": "false",
                },
                "SINK_COMMAND_INVALID_JSON",
            ),
            (
                {
                    "BOND_ALERT_SINK_COMMAND_JSON": '["relative-sink"]',
                    "BOND_ALERT_NOTIFICATION_CENTER": "false",
                },
                "SINK_COMMAND_INVALID_ARGV",
            ),
            (
                {
                    "BOND_ALERT_NOTIFICATION_CENTER": "maybe",
                },
                "NOTIFICATION_CENTER_INVALID_BOOLEAN",
            ),
        )
        for environment, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                with patch.dict(os.environ, environment, clear=True):
                    settings = AlertSettings.from_env()
                self.assertIsNone(settings.command)
                self.assertFalse(settings.notification_center)
                self.assertIn(expected_error, settings.config_errors)
                with self.assertLogs(
                    alert_logger,
                    level=logging.ERROR,
                ) as captured:
                    dispatcher = AlertDispatcher(
                        settings,
                        logger=alert_logger,
                    )
                config_records = [
                    json.loads(record.getMessage())
                    for record in captured.records
                ]
                self.assertTrue(
                    any(
                        record.get("event")
                        == "alert_configuration_error"
                        and record.get("error_code") == expected_error
                        for record in config_records
                    )
                )
                self.assertTrue(dispatcher.close(timeout_sec=1))

    def test_redaction_covers_json_and_quoted_assignments_in_all_outputs(
        self,
    ) -> None:
        from scheduler.alerts import (
            AlertDispatcher,
            AlertEvent,
            AlertSettings,
        )

        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ["/opt/local/bin/alert-sink"],
                0,
                "",
                "",
            )
        )
        alert_logger = logging.getLogger("tests.daily-alert.json-redaction")
        dispatcher = AlertDispatcher(
            AlertSettings(
                command=(Path("/opt/local/bin/alert-sink"),),
                notification_center=False,
            ),
            runner=runner,
            logger=alert_logger,
        )
        event = AlertEvent.now(
            code="ITEM_TERMINAL_FAILURE",
            severity="critical",
            predict_date="2026-07-24",
            occurrence_id=904,
            message=(
                'payload={"password":"json-password",'
                '"access_token": "json access token"} '
                "token='quoted token' secret=plain-secret"
            ),
            details={
                "reason": (
                    '{"refresh_token":"detail-token"} '
                    'password = "detail password" api_key=detail-key'
                ),
            },
        )

        with self.assertLogs(alert_logger, level=logging.ERROR) as captured:
            result = dispatcher.dispatch(event)
            self.assertTrue(dispatcher.drain(timeout_sec=1))

        self.assertEqual(result.command_status, "queued")
        hook_payload = runner.call_args.kwargs["input"]
        log_payload = captured.records[0].getMessage()
        for secret in (
            "json-password",
            "json access token",
            "quoted token",
            "plain-secret",
            "detail-token",
            "detail password",
            "detail-key",
        ):
            self.assertNotIn(secret, hook_payload)
            self.assertNotIn(secret, log_payload)
        self.assertGreaterEqual(hook_payload.count("[REDACTED]"), 7)
        self.assertTrue(dispatcher.close(timeout_sec=1))

    def test_dispatch_logs_structured_event_and_calls_safe_command_hook(
        self,
    ) -> None:
        from scheduler.alerts import (
            AlertDispatcher,
            AlertEvent,
            AlertSettings,
        )

        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ["/opt/local/bin/alert-sink"],
                0,
                "",
                "",
            )
        )
        logger = logging.getLogger("tests.daily-alert.command")
        event = AlertEvent(
            code="SLA_TARGET_MISSING",
            severity="critical",
            occurred_at=datetime(
                2026,
                7,
                24,
                0,
                0,
                tzinfo=timezone.utc,
            ),
            predict_date="2026-07-24",
            occurrence_id=71,
            scheme_id=None,
            message="08:00 target availability is 24/25",
            details={"missing_registry_ids": ["alpha__h1__10Y"]},
        )
        dispatcher = AlertDispatcher(
            AlertSettings(
                command=(
                    Path("/opt/local/bin/alert-sink"),
                    "--source",
                    "bond-factor-lab",
                ),
                notification_center=False,
                timeout_sec=9,
            ),
            runner=runner,
            logger=logger,
        )

        with (
            patch.dict(
                os.environ,
                {"BOND_DB_PASSWORD": "must-not-reach-alert-hook"},
            ),
            self.assertLogs(logger, level=logging.ERROR) as captured,
        ):
            result = dispatcher.dispatch(event)
            self.assertTrue(dispatcher.drain(timeout_sec=1))

        self.assertEqual(result.command_status, "queued")
        call = runner.call_args
        self.assertEqual(
            call.args[0],
            [
                "/opt/local/bin/alert-sink",
                "--source",
                "bond-factor-lab",
            ],
        )
        self.assertFalse(call.kwargs.get("shell", False))
        self.assertEqual(call.kwargs["timeout"], 9)
        self.assertNotIn("BOND_DB_PASSWORD", call.kwargs["env"])
        self.assertEqual(
            call.kwargs["env"]["PATH"],
            "/usr/bin:/bin:/usr/sbin:/sbin",
        )
        payload = json.loads(call.kwargs["input"])
        self.assertEqual(payload["event"], "daily_sla_alert")
        self.assertEqual(payload["code"], "SLA_TARGET_MISSING")
        self.assertEqual(
            payload["details"]["missing_registry_ids"],
            ["alpha__h1__10Y"],
        )
        log_payload = json.loads(captured.records[0].getMessage())
        self.assertEqual(log_payload, payload)
        self.assertTrue(dispatcher.close(timeout_sec=1))

    def test_notification_uses_argv_not_message_interpolation(self) -> None:
        from scheduler.alerts import (
            AlertDispatcher,
            AlertEvent,
            AlertSettings,
        )

        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ["/usr/bin/osascript"],
                0,
                "",
                "",
            )
        )
        message = 'bad " quote\nand shell $(touch /tmp/nope)'
        dispatcher = AlertDispatcher(
            AlertSettings(
                command=None,
                notification_center=True,
                timeout_sec=5,
            ),
            runner=runner,
        )
        event = AlertEvent.now(
            code="DATABRIDGE_FAILED",
            severity="error",
            predict_date="2026-07-24",
            occurrence_id=72,
            message=message,
        )

        result = dispatcher.dispatch(event)
        self.assertTrue(dispatcher.drain(timeout_sec=1))

        self.assertEqual(result.notification_status, "queued")
        command = runner.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/osascript")
        self.assertEqual(
            command[-2],
            'bad " quote\nand shell $(touch [REDACTED_PATH])',
        )
        self.assertNotIn("/tmp/nope", command[-2])
        self.assertEqual(command[-1], "Bond Factor Lab")
        self.assertFalse(runner.call_args.kwargs.get("shell", False))
        self.assertTrue(dispatcher.close(timeout_sec=1))

    def test_hook_failure_is_reported_but_does_not_mask_primary_alert(
        self,
    ) -> None:
        from scheduler.alerts import (
            AlertDispatcher,
            AlertEvent,
            AlertSettings,
        )

        runner = Mock(side_effect=subprocess.TimeoutExpired(["sink"], 3))
        logger = logging.getLogger("tests.daily-alert.failure")
        dispatcher = AlertDispatcher(
            AlertSettings(
                command=(Path("/opt/local/bin/sink"),),
                notification_center=False,
                timeout_sec=3,
            ),
            runner=runner,
            logger=logger,
        )

        with self.assertLogs(logger, level=logging.ERROR) as captured:
            result = dispatcher.dispatch(
                AlertEvent.now(
                    code="GENERATION_HASH_MISMATCH",
                    severity="critical",
                    predict_date="2026-07-24",
                    occurrence_id=73,
                    message="generation rejected",
                )
            )
            self.assertTrue(dispatcher.drain(timeout_sec=1))

        self.assertEqual(result.command_status, "queued")
        self.assertGreaterEqual(len(captured.records), 2)
        failure = json.loads(captured.records[-1].getMessage())
        self.assertEqual(failure["event"], "alert_delivery_failure")
        self.assertEqual(failure["sink"], "command")
        self.assertNotIn("traceback", failure)
        self.assertTrue(dispatcher.close(timeout_sec=1))

    def test_payload_and_hook_drop_unapproved_details_and_redact_secrets(
        self,
    ) -> None:
        from scheduler.alerts import (
            AlertDispatcher,
            AlertEvent,
            AlertSettings,
        )

        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ["/opt/local/bin/alert-sink"],
                0,
                "",
                "",
            )
        )
        logger = logging.getLogger("tests.daily-alert.redaction")
        dispatcher = AlertDispatcher(
            AlertSettings(
                command=(Path("/opt/local/bin/alert-sink"),),
                notification_center=False,
            ),
            runner=runner,
            logger=logger,
        )
        event = AlertEvent(
            code="ITEM_TERMINAL_FAILURE",
            severity="critical",
            occurred_at=datetime(
                2026,
                7,
                24,
                0,
                1,
                tzinfo=timezone.utc,
            ),
            predict_date="2026-07-24",
            occurrence_id=81,
            scheme_id="safe-scheme",
            message=(
                "Terminal failure Authorization: Bearer hook-secret "
                "at /Users/macstudio0/private/model.pkl"
            ),
            details={
                "failure_code": "ALGORITHM_ERROR",
                "reason": (
                    "mysql+pymysql://db-user:db-pass@127.0.0.1/bond_db"
                ),
                "error_type": "RuntimeError",
                "error_message": (
                    "RuntimeError: token=raw-token "
                    "/Users/macstudio0/private/input.csv"
                ),
                "Authorization": "Bearer details-secret",
                "access_token": "details-token",
                "manifest_uri": (
                    "/Users/macstudio0/private/manifest.json"
                ),
            },
        )

        with self.assertLogs(logger, level=logging.ERROR) as captured:
            result = dispatcher.dispatch(event)
            self.assertTrue(dispatcher.drain(timeout_sec=1))

        self.assertEqual(result.command_status, "queued")
        payload_text = runner.call_args.kwargs["input"]
        log_text = captured.records[0].getMessage()
        for secret in (
            "hook-secret",
            "db-user",
            "db-pass",
            "raw-token",
            "details-secret",
            "details-token",
            "/Users/macstudio0",
            "RuntimeError:",
        ):
            self.assertNotIn(secret, payload_text)
            self.assertNotIn(secret, log_text)
        payload = json.loads(payload_text)
        self.assertEqual(
            payload["details"],
            {
                "error_type": "RuntimeError",
                "failure_code": "ALGORITHM_ERROR",
                "reason": "mysql+pymysql://[REDACTED]@127.0.0.1/bond_db",
            },
        )
        self.assertIn("[REDACTED]", payload["message"])
        self.assertIn("[REDACTED_PATH]", payload["message"])
        self.assertTrue(dispatcher.close(timeout_sec=1))

    def test_notification_receives_the_same_redacted_message_as_payload(
        self,
    ) -> None:
        from scheduler.alerts import (
            AlertDispatcher,
            AlertEvent,
            AlertSettings,
        )

        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ["/usr/bin/osascript"],
                0,
                "",
                "",
            )
        )
        dispatcher = AlertDispatcher(
            AlertSettings(
                command=None,
                notification_center=True,
            ),
            runner=runner,
        )

        dispatcher.dispatch(
            AlertEvent.now(
                code="GENERATION_BUILD_FAILED",
                severity="critical",
                predict_date="2026-07-24",
                occurrence_id=82,
                message=(
                    "Authorization=Bearer notification-secret "
                    "/Users/macstudio0/private/generation"
                ),
            )
        )
        self.assertTrue(dispatcher.drain(timeout_sec=1))

        notification_message = runner.call_args.args[0][-2]
        self.assertNotIn("notification-secret", notification_message)
        self.assertNotIn("/Users/macstudio0", notification_message)
        self.assertIn("[REDACTED]", notification_message)
        self.assertIn("[REDACTED_PATH]", notification_message)
        self.assertTrue(dispatcher.close(timeout_sec=1))

    def test_idempotency_key_uses_stable_event_identity_not_timestamp(
        self,
    ) -> None:
        from scheduler.alerts import AlertEvent

        first = AlertEvent(
            code="V2_START_LATE",
            severity="critical",
            occurred_at=datetime(
                2026,
                7,
                24,
                0,
                0,
                tzinfo=timezone.utc,
            ),
            predict_date="2026-07-24",
            occurrence_id=83,
            scheme_id="v2-scheme",
            message="first delivery",
            details={"reason": "NOT_STARTED_BY_GUARDRAIL"},
        )
        replay = AlertEvent(
            code="V2_START_LATE",
            severity="error",
            occurred_at=datetime(
                2026,
                7,
                24,
                0,
                5,
                tzinfo=timezone.utc,
            ),
            predict_date="2026-07-24",
            occurrence_id=83,
            scheme_id="v2-scheme",
            message="replayed delivery",
            details={
                "reason": "NOT_STARTED_BY_GUARDRAIL",
                "access_token": "must-not-affect-identity",
            },
        )
        different_reason = AlertEvent(
            code="V2_START_LATE",
            severity="critical",
            occurred_at=datetime(
                2026,
                7,
                24,
                0,
                5,
                tzinfo=timezone.utc,
            ),
            predict_date="2026-07-24",
            occurrence_id=83,
            scheme_id="v2-scheme",
            message="different logical event",
            details={"reason": "STARTED_AFTER_GUARDRAIL"},
        )

        first_payload = first.payload()
        replay_payload = replay.payload()
        different_payload = different_reason.payload()
        self.assertNotEqual(
            first_payload["occurred_at"],
            replay_payload["occurred_at"],
        )
        self.assertEqual(
            first_payload["idempotency_key"],
            replay_payload["idempotency_key"],
        )
        self.assertNotEqual(
            first_payload["idempotency_key"],
            different_payload["idempotency_key"],
        )

    def test_unknown_code_fails_closed_for_all_detail_fields(self) -> None:
        from scheduler.alerts import AlertEvent

        payload = AlertEvent.now(
            code="UNREGISTERED_EVENT",
            severity="error",
            predict_date="2026-07-24",
            occurrence_id=84,
            message="unregistered event",
            details={
                "error_type": "RuntimeError",
                "failure_code": "UNKNOWN",
                "reason": "LOOKS_STABLE_BUT_IS_NOT_APPROVED",
                "stage": "progress",
            },
        ).payload()

        self.assertEqual(payload["details"], {})

    def test_generation_invalidated_preserves_only_stable_diagnostics(
        self,
    ) -> None:
        from scheduler.alerts import AlertEvent

        payload = AlertEvent.now(
            code="GENERATION_INVALIDATED",
            severity="critical",
            predict_date="2026-07-24",
            occurrence_id=85,
            message="generation invalidated",
            details={
                "failed_scheme_ids": ["native-a"],
                "failure_codes": ["GENERATION_INVALIDATED"],
                "reason": "MANIFEST_REVOKED",
                "native_error": "RuntimeError: private raw exception",
            },
        ).payload()

        self.assertEqual(
            payload["details"],
            {
                "failed_scheme_ids": ["native-a"],
                "failure_codes": ["GENERATION_INVALIDATED"],
                "reason": "MANIFEST_REVOKED",
            },
        )

    def test_settings_reject_relative_or_shell_command_configuration(
        self,
    ) -> None:
        from scheduler.alerts import AlertSettings

        with self.assertRaisesRegex(ValueError, "absolute"):
            AlertSettings.from_mapping(
                {
                    "command": ["relative-sink", "--send"],
                    "notification_center": False,
                }
            )
        with self.assertRaisesRegex(ValueError, "JSON array"):
            AlertSettings.from_mapping(
                {
                    "command": "/bin/sh -c dangerous",
                    "notification_center": False,
                }
            )


if __name__ == "__main__":
    unittest.main()
