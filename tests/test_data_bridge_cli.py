from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


class DataBridgeCliTests(unittest.TestCase):
    def test_refresh_passes_database_authoritative_continuity_cutoffs(
        self,
    ) -> None:
        from scripts import refresh_data_bridge_current as command

        engine = SimpleNamespace(dispose=Mock())
        authority = object()
        with (
            patch.object(
                command,
                "_previous_trading_day",
                return_value="2026-07-28",
            ),
            patch.object(
                command,
                "create_engine_from_env",
                return_value=engine,
                create=True,
            ),
            patch.object(
                command,
                "resolve_databridge_continuity_authority_from_engine",
                return_value=authority,
                create=True,
            ) as resolve,
            patch.object(
                command.DataBridgeClientConfig,
                "from_env",
                return_value=SimpleNamespace(),
            ),
            patch.object(
                command.DataBridgeRefreshConfig,
                "from_env",
                return_value=SimpleNamespace(),
            ) as config_factory,
            patch.object(
                command,
                "DataBridgeClient",
                return_value=SimpleNamespace(),
            ),
            patch.object(
                command,
                "run_full_refresh",
                return_value=SimpleNamespace(),
            ) as refresh,
        ):
            command.refresh_current(
                refresh_date="2026-07-29",
                publish=False,
            )

        config = config_factory.return_value
        resolve.assert_called_once_with(
            config,
            feature_date="2026-07-28",
            engine=engine,
        )
        self.assertIs(
            refresh.call_args.kwargs["continuity_authority"],
            authority,
        )
        engine.dispose.assert_called_once_with()

    def test_publish_mode_returns_machine_readable_success(self) -> None:
        from scripts import refresh_data_bridge_current as command

        result = SimpleNamespace(
            state={"generation_id": "full-test"},
            published=True,
            rounds_completed=2,
            duration_sec=1.25,
        )
        with (
            patch.dict(
                os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "legacy"},
                clear=True,
            ),
            patch.object(
                command,
                "refresh_current",
                return_value=result,
            ) as refresh,
        ):
            exit_code, payload = command.run_command("publish", refresh_date="2026-07-19")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "ok")
        refresh.assert_called_once_with(refresh_date="2026-07-19", publish=True)

    def test_ledger_mode_rejects_standalone_publish(self) -> None:
        from scripts import refresh_data_bridge_current as command

        with (
            patch.dict(
                os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
            ),
            patch.object(command, "refresh_current") as refresh,
        ):
            exit_code, payload = command.run_command(
                "publish",
                refresh_date="2026-07-24",
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "configuration_error")
        self.assertIn("coordinator", str(payload["error"]).lower())
        refresh.assert_not_called()

    def test_ledger_mode_rejects_standalone_dry_run(self) -> None:
        from scripts import refresh_data_bridge_current as command

        with (
            patch.dict(
                os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
                clear=True,
            ),
            patch.object(command, "refresh_current") as refresh,
        ):
            exit_code, payload = command.run_command(
                "dry-run",
                refresh_date="2026-07-24",
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "configuration_error")
        self.assertIn("check-only", str(payload["error"]))
        refresh.assert_not_called()

    def test_publish_without_explicit_mode_fails_closed(self) -> None:
        from scripts import refresh_data_bridge_current as command

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(command, "refresh_current") as refresh,
        ):
            exit_code, payload = command.run_command(
                "publish",
                refresh_date="2026-07-24",
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "configuration_error")
        self.assertIn("explicitly set", str(payload["error"]))
        refresh.assert_not_called()

    def test_configuration_error_uses_exit_code_two_and_redacts_password(self) -> None:
        from scripts import refresh_data_bridge_current as command
        from shared.data_bridge.client import DataBridgeConfigurationError

        with (
            patch.dict(
                os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "legacy"},
                clear=True,
            ),
            patch.object(
                command,
                "refresh_current",
                side_effect=DataBridgeConfigurationError(
                    "bad credential 123"
                ),
            ),
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
