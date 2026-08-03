"""Blackbox V2 legacy preflight retirement tests."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest.mock import Mock, patch

from scheduler.v2_daily_preflight import ASIA_SHANGHAI, main, run_phase


class V2DailyPreflightRetirementTests(unittest.TestCase):
    @staticmethod
    def _at(hour: int, minute: int) -> datetime:
        return datetime(2026, 7, 22, hour, minute, tzinfo=ASIA_SHANGHAI)

    def test_run_phase_is_retired_without_dependencies_gate_or_restart(self) -> None:
        dependencies = Mock()
        dependencies.config_factory.return_value = Mock()
        dependencies.expected_daily_date.return_value = "2026-07-21"
        dependencies.refresh.return_value = {
            "generation_id": "legacy-generation",
        }

        with (
            patch(
                "scheduler.v2_daily_preflight.default_dependencies",
                return_value=dependencies,
            ) as default_dependencies,
            patch(
                "scheduler.v2_daily_preflight.write_gate_record",
            ) as write_gate,
            patch(
                "scheduler.v2_daily_preflight.restart_scheduler",
            ) as restart,
        ):
            payload = run_phase("refresh-primary", now=self._at(6, 0))

        self.assertEqual(
            payload,
            {
                "event": "v2_daily_preflight",
                "phase": "refresh-primary",
                "reason": "launchd-one-shot-data-bridge-publisher",
                "run_date": "2026-07-22",
                "status": "retired",
            },
        )
        default_dependencies.assert_not_called()
        dependencies.refresh.assert_not_called()
        write_gate.assert_not_called()
        restart.assert_not_called()

    def test_main_returns_retired_json_before_mode_or_dependencies(self) -> None:
        stdout = io.StringIO()
        with (
            patch(
                "scheduler.v2_daily_preflight._daily_coordinator_mode",
                side_effect=AssertionError("retired preflight must not resolve mode"),
            ) as coordinator_mode,
            patch(
                "scheduler.v2_daily_preflight.default_dependencies",
            ) as default_dependencies,
            patch(
                "scheduler.v2_daily_preflight.resolve_phase",
            ) as resolve_phase,
            patch(
                "scheduler.v2_daily_preflight.write_gate_record",
            ) as write_gate,
            patch(
                "scheduler.v2_daily_preflight.restart_scheduler",
            ) as restart,
            redirect_stdout(stdout),
        ):
            exit_code = main(["--phase", "auto"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["event"], "v2_daily_preflight")
        self.assertEqual(payload["phase"], "auto")
        self.assertEqual(
            payload["reason"],
            "launchd-one-shot-data-bridge-publisher",
        )
        self.assertEqual(payload["status"], "retired")
        coordinator_mode.assert_not_called()
        default_dependencies.assert_not_called()
        resolve_phase.assert_not_called()
        write_gate.assert_not_called()
        restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
