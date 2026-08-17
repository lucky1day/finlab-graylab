from __future__ import annotations

import unittest
from contextlib import nullcontext
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


def _blackbox_config(scheme_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id=scheme_id,
        scheme_version=f"{scheme_id}-version",
        frequency="weekly",
        status="active",
        runtime_type="blackbox_v2",
        input_source="data_bridge_current",
        version_status="active",
        task_type="weekly_point",
        horizon=1,
        tenors=["10Y"],
    )


class _CoveredCalendar:
    def covers(self, value: str) -> bool:
        return value == "2026-08-15"

    def previous_trading_day(self, value: str) -> str:
        return "2026-08-14"

    def is_trading_day(self, value: str) -> bool:
        return False


class SystemdControlPlaneTests(unittest.TestCase):
    def test_executor_accepts_only_matching_systemd_context(self) -> None:
        from scheduler import executor

        cfg = _blackbox_config("systemd_candidate")
        context_factory = getattr(
            executor,
            "_systemd_scheduled_execution_context",
            None,
        )
        self.assertIsNotNone(context_factory)
        context = context_factory()
        with patch.object(executor, "discover_schemes", return_value=[cfg]):
            self.assertIsNone(
                executor.scheduled_live_execution_configuration_error(
                    cfg,
                    scheduled_control_plane="systemd_one_shot",
                    scheduled_execution_context=context,
                )
            )
            mismatch = executor.scheduled_live_execution_configuration_error(
                cfg,
                scheduled_control_plane="systemd_one_shot",
                scheduled_execution_context=(
                    executor._launchd_scheduled_execution_context()
                ),
            )

        self.assertIn("requires one-shot execution context", mismatch)

    def test_systemd_runner_passes_truthful_control_plane(self) -> None:
        from scheduler import launchd_prediction_runner as common_runner
        from scheduler import systemd_prediction_runner as runner

        cfg = _blackbox_config("systemd_candidate")
        engine = Mock()
        with (
            patch.object(
                common_runner.DataBridgeRefreshConfig,
                "from_env",
                return_value=object(),
            ),
            patch.object(
                common_runner,
                "_runner_lock",
                return_value=nullcontext(),
            ),
            patch.object(
                common_runner,
                "discover_schemes",
                return_value=[cfg],
            ),
            patch.object(
                common_runner,
                "create_engine_from_env",
                return_value=engine,
            ),
            patch.object(
                common_runner,
                "get_calendar",
                return_value=_CoveredCalendar(),
            ),
            patch.object(
                common_runner,
                "is_weekly_signal_date",
                return_value=True,
            ),
            patch.object(
                common_runner,
                "execute_scheme",
                return_value=SimpleNamespace(
                    scheme_id=cfg.scheme_id,
                    status="success",
                    records_written=1,
                    run_id=100,
                ),
            ) as execute_one,
        ):
            summary = runner.run(
                "weekly",
                predict_date="2026-08-15",
                algo_env="forecast_env",
            )

        self.assertEqual(
            execute_one.call_args.kwargs["scheduled_control_plane"],
            "systemd_one_shot",
        )
        self.assertEqual(
            summary.to_payload()["event"],
            "systemd_prediction_run",
        )

    def test_databridge_publish_accepts_systemd_one_shot_producer(
        self,
    ) -> None:
        from scripts import refresh_data_bridge_current as entry

        with (
            patch.dict(
                os.environ,
                {"BFL_DATABRIDGE_PRODUCER": "systemd-one-shot"},
                clear=False,
            ),
            patch.object(
                entry.DataBridgeRefreshConfig,
                "from_env",
                return_value=object(),
            ),
            patch.object(
                entry,
                "expected_daily_date",
                return_value="2026-08-14",
            ),
            patch.object(
                entry,
                "_publisher_lock",
                return_value=nullcontext(True),
            ),
            patch.object(
                entry,
                "_run_publish_with_retries",
                return_value=(0, {"status": "ok", "mode": "publish"}),
            ),
        ):
            code, payload = entry.run_command(
                "publish",
                refresh_date="2026-08-17",
            )

        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")

    def test_databridge_publish_rejects_noncanonical_producers(self) -> None:
        from scripts import refresh_data_bridge_current as entry

        for producer in (
            "",
            "cron",
            "systemd_one_shot",
            "systemd-one-shot-extra",
        ):
            with self.subTest(producer=producer):
                with patch.dict(
                    os.environ,
                    {"BFL_DATABRIDGE_PRODUCER": producer},
                    clear=False,
                ):
                    code, payload = entry.run_command(
                        "publish",
                        refresh_date="2026-08-17",
                    )
                self.assertEqual(code, 2)
                self.assertEqual(
                    payload["status"],
                    "configuration_error",
                )

    def test_systemd_templates_are_disabled_first_one_shots(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        deploy_root = project_root / "deploy" / "systemd"
        expected_files = {
            "bond-factor-lab-backend.service",
            "bond-factor-lab-data-bridge.service",
            "bond-factor-lab-data-bridge.timer",
            "bond-factor-lab-prediction-daily.service",
            "bond-factor-lab-prediction-daily.timer",
            "bond-factor-lab-prediction-weekly.service",
            "bond-factor-lab-prediction-weekly.timer",
            "bond-factor-lab-prediction-monthly.service",
            "bond-factor-lab-prediction-monthly.timer",
            "bond-factor-lab-actuals.service",
            "bond-factor-lab-actuals.timer",
        }

        self.assertTrue(deploy_root.is_dir())
        self.assertEqual(
            {path.name for path in deploy_root.iterdir() if path.is_file()},
            expected_files,
        )
        contents = {
            name: (deploy_root / name).read_text(encoding="utf-8")
            for name in expected_files
        }

        for name, content in contents.items():
            self.assertNotIn("scheduler.main", content, name)
            self.assertNotIn("APScheduler", content, name)
            self.assertNotIn("ledger", content.lower(), name)
            self.assertNotIn("backtest", content.lower(), name)
            self.assertNotIn("systemctl", content, name)
            if name.endswith(".service"):
                self.assertIn(
                    "After=network-online.target mysql.service",
                    content,
                    name,
                )
                self.assertNotIn("mysqld.service", content, name)
                self.assertIn(
                    "WorkingDirectory=/opt/bond-factor-lab/current",
                    content,
                    name,
                )
                self.assertIn(
                    "EnvironmentFile=/etc/bond-factor-lab/bond-factor-lab.env",
                    content,
                    name,
                )

        backend = contents["bond-factor-lab-backend.service"]
        self.assertIn("--host 127.0.0.1 --port 8100", backend)
        self.assertIn(
            "Environment=BOND_FACTOR_LAB_CONTROL_PLANE=systemd_one_shot",
            backend,
        )

        data_bridge = contents["bond-factor-lab-data-bridge.service"]
        self.assertIn(
            "Environment=BFL_DATABRIDGE_PRODUCER=systemd-one-shot",
            data_bridge,
        )
        self.assertIn(
            "scripts/refresh_data_bridge_current.py --publish",
            data_bridge,
        )

        for cadence in ("daily", "weekly", "monthly"):
            service = contents[
                f"bond-factor-lab-prediction-{cadence}.service"
            ]
            self.assertIn(
                "Environment=BFL_DATABASE_ENV_FILE="
                "/etc/bond-factor-lab/bond-factor-lab.env",
                service,
            )
            self.assertIn(
                "python -m scheduler.systemd_prediction_runner "
                f"--cadence {cadence}",
                service,
            )

        self.assertIn(
            "python -m scheduler.actuals_runner",
            contents["bond-factor-lab-actuals.service"],
        )

        expected_schedules = {
            "bond-factor-lab-data-bridge.timer": (
                "OnCalendar=*-*-* 06:30:00 Asia/Shanghai",
            ),
            "bond-factor-lab-prediction-daily.timer": (
                "OnCalendar=Mon..Fri *-*-* 07:03:00 Asia/Shanghai",
            ),
            "bond-factor-lab-prediction-weekly.timer": (
                "OnCalendar=Sat *-*-* 11:30:00 Asia/Shanghai",
            ),
            "bond-factor-lab-prediction-monthly.timer": (
                "OnCalendar=*-*-15 18:00:00 Asia/Shanghai",
            ),
            "bond-factor-lab-actuals.timer": (
                "OnCalendar=*-*-* 08:30:00 Asia/Shanghai",
                "OnCalendar=*-*-* 19:00:00 Asia/Shanghai",
                "OnCalendar=*-*-* 23:45:00 Asia/Shanghai",
            ),
        }
        for name, schedules in expected_schedules.items():
            content = contents[name]
            self.assertIn("Persistent=false", content, name)
            self.assertIn("RandomizedDelaySec=0", content, name)
            self.assertIn(
                f"Unit={name.removesuffix('.timer')}.service",
                content,
                name,
            )
            for schedule in schedules:
                self.assertIn(schedule, content, name)


if __name__ == "__main__":
    unittest.main()
