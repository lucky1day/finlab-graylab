from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch


class DataBridgeCliTests(unittest.TestCase):
    def test_publish_mode_returns_machine_readable_success(self) -> None:
        from scripts import refresh_data_bridge_current as command

        result = SimpleNamespace(
            state={"generation_id": "full-test"},
            published=True,
            rounds_completed=2,
            duration_sec=1.25,
        )
        with patch.object(command, "refresh_current", return_value=result) as refresh:
            exit_code, payload = command.run_command("publish", refresh_date="2026-07-19")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "ok")
        refresh.assert_called_once_with(refresh_date="2026-07-19", publish=True)

    def test_configuration_error_uses_exit_code_two_and_redacts_password(self) -> None:
        from scripts import refresh_data_bridge_current as command
        from shared.data_bridge.client import DataBridgeConfigurationError

        with patch.object(
            command,
            "refresh_current",
            side_effect=DataBridgeConfigurationError("bad credential 123"),
        ):
            exit_code, payload = command.run_command("dry-run", refresh_date="2026-07-19")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "configuration_error")
        self.assertNotIn("123", str(payload))

    def test_check_only_rejects_stale_current_with_exit_code_one(self) -> None:
        from scripts import refresh_data_bridge_current as command
        from shared.data_bridge.refresh import DataBridgeRefreshError

        with patch.object(
            command,
            "check_current",
            side_effect=DataBridgeRefreshError("refresh_date is stale"),
        ):
            exit_code, payload = command.run_command("check-only", refresh_date="2026-07-19")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")


if __name__ == "__main__":
    unittest.main()
