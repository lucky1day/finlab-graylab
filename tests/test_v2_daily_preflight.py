"""Blackbox V2 每日 DataBridge preflight 编排测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from scheduler.v2_daily_gate import load_gate_record
from scheduler.v2_daily_preflight import (
    ASIA_SHANGHAI,
    PreflightDependencies,
    PreflightError,
    resolve_phase,
    restart_scheduler,
    run_phase,
)
from shared.data_bridge.refresh import DataBridgeRefreshConfig


class V2DailyPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.config = DataBridgeRefreshConfig(
            data_root=root / "data",
            runtime_root=root / "runtime",
            schema_path=root / "schema.json",
            refresh_start="06:00",
            refresh_deadline="07:00",
        )
        self.current_state = {
            "generation_id": "full-20260722-current",
            "refresh_date": "2026-07-22",
            "business_digest": "digest-current",
        }
        self.refresh = Mock(return_value=dict(self.current_state))
        self.check = Mock(return_value=dict(self.current_state))
        self.restart = Mock(
            return_value={
                "requested": True,
                "verified": True,
                "label": "com.bond-factor-lab.scheduler",
                "old_pid": 100,
                "new_pid": 200,
            }
        )
        self.expected_daily_date = Mock(return_value="2026-07-21")
        self.dependencies = PreflightDependencies(
            config_factory=lambda: self.config,
            refresh=self.refresh,
            check=self.check,
            expected_daily_date=self.expected_daily_date,
            restart_scheduler=self.restart,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _at(hour: int, minute: int, second: int = 0) -> datetime:
        return datetime(2026, 7, 22, hour, minute, second, tzinfo=ASIA_SHANGHAI)

    def test_resolve_phase_uses_four_exact_windows(self) -> None:
        self.assertEqual(resolve_phase(self._at(6, 0)), "refresh-primary")
        self.assertEqual(resolve_phase(self._at(6, 30)), "check-primary")
        self.assertEqual(resolve_phase(self._at(6, 35)), "refresh-retry")
        self.assertEqual(resolve_phase(self._at(7, 0, 59)), "finalize")

    def test_resolve_phase_rejects_unmanaged_time(self) -> None:
        with self.assertRaisesRegex(PreflightError, "unmanaged"):
            resolve_phase(self._at(6, 31))

    def test_primary_phase_refreshes_and_records_checking_state(self) -> None:
        result = run_phase(
            "refresh-primary",
            now=self._at(6, 0),
            dependencies=self.dependencies,
        )

        self.assertEqual(result["status"], "refreshed")
        self.refresh.assert_called_once_with("2026-07-22")
        self.restart.assert_not_called()
        record = load_gate_record(self.config, "2026-07-22")
        self.assertEqual(record["status"], "checking")
        self.assertEqual(record["generation_id"], "full-20260722-current")

    def test_primary_check_records_stale_without_retrying_early(self) -> None:
        self.check.side_effect = ValueError("stale generation")

        result = run_phase(
            "check-primary",
            now=self._at(6, 30),
            dependencies=self.dependencies,
        )

        self.assertEqual(result["status"], "stale")
        self.refresh.assert_not_called()
        self.restart.assert_not_called()
        record = load_gate_record(self.config, "2026-07-22")
        self.assertEqual(record["status"], "checking")
        self.assertEqual(record["checks"][0]["error"], "stale generation")

    def test_retry_skips_when_current_is_valid(self) -> None:
        result = run_phase(
            "refresh-retry",
            now=self._at(6, 35),
            dependencies=self.dependencies,
        )

        self.assertEqual(result["status"], "skipped-current")
        self.refresh.assert_not_called()

    def test_retry_refreshes_when_current_is_stale(self) -> None:
        self.check.side_effect = ValueError("stale generation")

        result = run_phase(
            "refresh-retry",
            now=self._at(6, 35),
            dependencies=self.dependencies,
        )

        self.assertEqual(result["status"], "refreshed")
        self.refresh.assert_called_once_with("2026-07-22")

    def test_finalize_failure_writes_blocked_without_restart(self) -> None:
        self.check.side_effect = ValueError("stale generation")

        result = run_phase(
            "finalize",
            now=self._at(7, 0),
            dependencies=self.dependencies,
        )

        self.assertEqual(result["status"], "blocked")
        self.restart.assert_not_called()
        record = load_gate_record(self.config, "2026-07-22")
        self.assertEqual(record["status"], "blocked")
        self.assertIn("stale generation", record["checks"][0]["error"])

    def test_finalize_success_binds_generation_and_verified_restart(self) -> None:
        result = run_phase(
            "finalize",
            now=self._at(7, 0),
            dependencies=self.dependencies,
        )

        self.assertEqual(result["status"], "ready")
        self.restart.assert_called_once_with(self._at(7, 0))
        record = load_gate_record(self.config, "2026-07-22")
        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["generation_id"], "full-20260722-current")
        self.assertTrue(record["restart"]["verified"])

    def test_restart_failure_rewrites_ready_decision_as_blocked(self) -> None:
        self.restart.side_effect = PreflightError("PID did not change")

        result = run_phase(
            "finalize",
            now=self._at(7, 0),
            dependencies=self.dependencies,
        )

        self.assertEqual(result["status"], "blocked")
        record = load_gate_record(self.config, "2026-07-22")
        self.assertEqual(record["status"], "blocked")
        self.assertIn("PID did not change", record["restart"]["error"])

    def test_late_finalize_cannot_authorize_restart(self) -> None:
        with self.assertRaisesRegex(PreflightError, "safe window"):
            run_phase(
                "finalize",
                now=self._at(7, 1),
                dependencies=self.dependencies,
            )

        self.restart.assert_not_called()


class SchedulerRestartTests(unittest.TestCase):
    def test_restart_uses_fixed_launchd_label_and_requires_changed_pid(self) -> None:
        print_outputs = [
            "state = running\n\tpid = 100\n",
            "state = running\n\tpid = 200\n",
        ]

        with (
            patch(
                "scheduler.v2_daily_preflight.subprocess.run"
            ) as run,
            patch(
                "scheduler.v2_daily_preflight._launchctl_print",
                side_effect=print_outputs,
            ),
        ):
            result = restart_scheduler(
                datetime(2026, 7, 22, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                uid=501,
                timeout_sec=1.0,
            )

        run.assert_called_once_with(
            [
                "launchctl",
                "kickstart",
                "-k",
                "gui/501/com.bond-factor-lab.scheduler",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result["old_pid"], 100)
        self.assertEqual(result["new_pid"], 200)
        self.assertTrue(result["verified"])


if __name__ == "__main__":
    unittest.main()
