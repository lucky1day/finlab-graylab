from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from shared.blackbox_v2.contracts import BlackboxMetadata, BlackboxRequest


_UNDECLARED_SYSTEM_READ_PROBES = {
    "passwd_read_denied": Path("/private/etc/passwd"),
    "language_assets_read_denied": Path(
        "/usr/share/com.apple.languageassetd/_CodeSignature/CodeResources"
    ),
    "calculator_info_read_denied": Path(
        "/System/Applications/Calculator.app/Contents/Info.plist"
    ),
}


class BlackboxV2RunnerTests(unittest.TestCase):
    def test_runner_validates_exact_files_from_platform_input_ids(self) -> None:
        from scheduler.blackbox_v2_runner import _validate_data_dir

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = _write_data_dir(root)
            calendar_path = data_dir / "api_wind_date.csv"
            calendar_path.write_text(
                "rdate,week_id\n2026-07-24,202629\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "exactly"):
                _validate_data_dir(data_dir)
            _validate_data_dir(
                data_dir,
                platform_input_ids=("api-wind-date-v1",),
            )

            (data_dir / "unexpected.csv").write_text(
                "x\n1\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "exactly"):
                _validate_data_dir(
                    data_dir,
                    platform_input_ids=("api-wind-date-v1",),
                )

    def test_runner_rejects_symlink_for_declared_platform_input(self) -> None:
        from scheduler.blackbox_v2_runner import _validate_data_dir

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = _write_data_dir(root)
            calendar_source = root / "calendar-source.csv"
            calendar_source.write_text(
                "rdate,week_id\n2026-07-24,202629\n",
                encoding="utf-8",
            )
            (data_dir / "api_wind_date.csv").symlink_to(calendar_source)

            with self.assertRaisesRegex(ValueError, "symlink"):
                _validate_data_dir(
                    data_dir,
                    platform_input_ids=("api-wind-date-v1",),
                )

    def test_runner_rejects_missing_declared_platform_input(self) -> None:
        from scheduler.blackbox_v2_runner import _validate_data_dir

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = _write_data_dir(Path(tmpdir))

            with self.assertRaisesRegex(ValueError, "exactly"):
                _validate_data_dir(
                    data_dir,
                    platform_input_ids=("api-wind-date-v1",),
                )

    def test_runner_rejects_directory_named_as_platform_input(self) -> None:
        from scheduler.blackbox_v2_runner import _validate_data_dir

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = _write_data_dir(Path(tmpdir))
            (data_dir / "api_wind_date.csv").mkdir()

            with self.assertRaisesRegex(ValueError, "regular"):
                _validate_data_dir(
                    data_dir,
                    platform_input_ids=("api-wind-date-v1",),
                )

    def test_runner_rejects_hardlinked_base_data_file(self) -> None:
        from scheduler.blackbox_v2_runner import _validate_data_dir

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = _write_data_dir(root)
            daily = data_dir / "daily_output.csv"
            source = root / "daily-source.csv"
            daily.replace(source)
            os.link(source, daily)

            with self.assertRaisesRegex(ValueError, "hardlink"):
                _validate_data_dir(data_dir)

    def test_runner_rejects_hardlinked_platform_input_file(self) -> None:
        from scheduler.blackbox_v2_runner import _validate_data_dir

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = _write_data_dir(root)
            source = root / "calendar-source.csv"
            source.write_text(
                "rdate,week_id\n2026-07-24,202629\n",
                encoding="utf-8",
            )
            os.link(source, data_dir / "api_wind_date.csv")

            with self.assertRaisesRegex(ValueError, "hardlink"):
                _validate_data_dir(
                    data_dir,
                    platform_input_ids=("api-wind-date-v1",),
                )

    def test_sandbox_grants_only_declared_platform_input_literal_read(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            _sandbox_command,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = _write_data_dir(root)
            run_dir = root / "run"
            run_dir.mkdir()
            calendar_path = data_dir / "api_wind_date.csv"
            calendar_path.write_text(
                "rdate,week_id\n2026-07-24,202629\n",
                encoding="utf-8",
            )
            delivery_dir = root / "delivery"
            delivery_dir.mkdir()
            delivery_calendar = delivery_dir / "api_wind_date.csv"
            delivery_calendar.write_text(
                "rdate,week_id\n1900-01-01,190001\n",
                encoding="utf-8",
            )
            command = _sandbox_command(
                [sys.executable, "-c", "pass"],
                run_dir,
                profile=RuntimeProfile.for_tests(sandbox_enabled=True),
                data_dir=data_dir,
                platform_input_ids=("api-wind-date-v1",),
            )

        policy = command[4]
        self.assertIn(
            f'(literal "{calendar_path.resolve()}")',
            policy,
        )
        self.assertNotIn(
            f'(literal "{delivery_calendar.resolve()}")',
            policy,
        )
        self.assertIn("(deny network*)", policy)

    def test_runtime_environment_does_not_inherit_parent_secrets(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, _runtime_environment

        inherited = {
            "LANG": "zh_CN.UTF-8",
            "BOND_DB_PASSWORD": "db-secret",
            "BOND_SCHEDULE_EXECUTION_TOKEN": "stale-attempt-token",
            "DATABRIDGE_API_PASSWORD": "bridge-secret",
            "HARNESS_AUTH_SECRET": "auth-secret",
            "AWS_SECRET_ACCESS_KEY": "cloud-secret",
            "HTTPS_PROXY": "http://proxy.invalid",
            "BLACKBOX_TEST_SECRET": "user-secret",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            profile = RuntimeProfile.for_tests()
            self.assertNotIn(
                "BOND_SCHEDULE_EXECUTION_TOKEN",
                profile.environment_allowlist,
            )
            with patch.dict(os.environ, inherited, clear=True):
                env = _runtime_environment(profile, run_dir)

        for key in inherited.keys() - {"LANG"}:
            self.assertNotIn(key, env)
        self.assertEqual(env["LANG"], "zh_CN.UTF-8")
        self.assertEqual(env["TZ"], "Asia/Shanghai")
        self.assertEqual(env["HOME"], str(run_dir))
        self.assertEqual(env["TMPDIR"], str(run_dir))

    def test_sandbox_policy_has_no_unrestricted_file_read_clause(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, _sandbox_command

        with tempfile.TemporaryDirectory() as tmpdir:
            command = _sandbox_command(
                [sys.executable, "-c", "pass"],
                Path(tmpdir),
                profile=RuntimeProfile.for_tests(sandbox_enabled=True),
            )

        policy = command[4]
        self.assertNotIn("(allow file-read*)", policy)
        self.assertNotIn('(import "system.sb")', policy)
        self.assertNotIn("(allow process*)", policy)
        self.assertNotIn("(allow mach-lookup)", policy)
        self.assertNotIn("(allow signal)", policy)
        self.assertNotIn("(allow sysctl-read)", policy)
        self.assertNotIn('(allow file-read-data file-test-existence (literal "/"))', policy)
        forbidden_subpaths = {
            "/",
            "/Users",
            str(Path.home()),
            str(Path(__file__).resolve().parents[1]),
            "/etc",
            "/private/etc",
            "/usr/share",
            "/System",
            "/Library",
        }
        for path in forbidden_subpaths:
            self.assertNotIn(f'(subpath "{path}")', policy)

    def test_help_probe_requires_both_cli_modes(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, probe_blackbox_help

        with tempfile.TemporaryDirectory() as tmpdir:
            script = _write_script(Path(tmpdir) / "trial.py", _SUCCESS_SCRIPT)
            output = probe_blackbox_help(script, profile=RuntimeProfile.for_tests())

        self.assertIn("predict", output)
        self.assertIn("backtest", output)

    def test_predict_rejects_calendar_week_mapping_mismatch_before_process_start(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            execute_blackbox_cli,
        )
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = _write_data_dir(root)
            (data_dir / "api_wind_date.csv").write_text(
                "rdate,week_id\n2026-07-15,202628\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "scheduler.blackbox_v2_runner.subprocess.Popen",
                    side_effect=AssertionError("child process started"),
                ) as popen,
                self.assertRaisesRegex(ValueError, "weekly_cutoff_key mismatch"),
            ):
                execute_blackbox_cli(
                    script_path=_write_script(root / "trial.py", _SUCCESS_SCRIPT),
                    mode="predict",
                    input_path=write_request(_request("mismatch"), root / "request.json"),
                    data_dir=data_dir,
                    output_path=root / "run" / "prediction.json",
                    platform_input_ids=("api-wind-date-v1",),
                    profile=RuntimeProfile.for_tests(),
                )

        popen.assert_not_called()

    def test_predict_rejects_missing_calendar_daily_cutoff_before_process_start(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            execute_blackbox_cli,
        )
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = _write_data_dir(root)
            (data_dir / "api_wind_date.csv").write_text(
                "rdate,week_id\n2026-07-14,202627\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "scheduler.blackbox_v2_runner.subprocess.Popen",
                    side_effect=AssertionError("child process started"),
                ) as popen,
                self.assertRaisesRegex(
                    ValueError,
                    "exactly one row.*daily_cutoff_key=2026-07-15",
                ),
            ):
                execute_blackbox_cli(
                    script_path=_write_script(root / "trial.py", _SUCCESS_SCRIPT),
                    mode="predict",
                    input_path=write_request(_request("missing"), root / "request.json"),
                    data_dir=data_dir,
                    output_path=root / "run" / "prediction.json",
                    platform_input_ids=("api-wind-date-v1",),
                    profile=RuntimeProfile.for_tests(),
                )

        popen.assert_not_called()

    def test_predict_rejects_duplicate_calendar_daily_cutoff_before_process_start(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            execute_blackbox_cli,
        )
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = _write_data_dir(root)
            (data_dir / "api_wind_date.csv").write_text(
                "rdate,week_id\n2026-07-15,202627\n2026-07-15,202627\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "scheduler.blackbox_v2_runner.subprocess.Popen",
                    side_effect=AssertionError("child process started"),
                ) as popen,
                self.assertRaisesRegex(
                    ValueError,
                    "exactly one row.*daily_cutoff_key=2026-07-15",
                ),
            ):
                execute_blackbox_cli(
                    script_path=_write_script(root / "trial.py", _SUCCESS_SCRIPT),
                    mode="predict",
                    input_path=write_request(_request("duplicate"), root / "request.json"),
                    data_dir=data_dir,
                    output_path=root / "run" / "prediction.json",
                    platform_input_ids=("api-wind-date-v1",),
                    profile=RuntimeProfile.for_tests(),
                )

        popen.assert_not_called()

    def test_invalid_predict_input_with_calendar_preserves_upstream_rejection(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            BlackboxExecutionError,
            RuntimeProfile,
            execute_blackbox_cli,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = _write_data_dir(root)
            (data_dir / "api_wind_date.csv").write_text(
                "rdate,week_id\n2026-07-15,202627\n",
                encoding="utf-8",
            )
            invalid_request = root / "invalid-request.json"
            invalid_request.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(BlackboxExecutionError, "exited"):
                execute_blackbox_cli(
                    script_path=_write_script(root / "trial.py", _SUCCESS_SCRIPT),
                    mode="predict",
                    input_path=invalid_request,
                    data_dir=data_dir,
                    output_path=root / "run" / "prediction.json",
                    platform_input_ids=("api-wind-date-v1",),
                    profile=RuntimeProfile.for_tests(),
                )

    def test_backtest_validates_every_calendar_request_before_process_start(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            execute_blackbox_cli,
        )
        from shared.blackbox_v2.requests import write_requests

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = _write_data_dir(root)
            (data_dir / "api_wind_date.csv").write_text(
                "rdate,week_id\n2026-07-15,202627\n2026-07-16,202628\n",
                encoding="utf-8",
            )
            mismatched_second_request = BlackboxRequest(
                request_id="second",
                predict_date="2026-07-16",
                feature_date="2026-07-16",
                target_date="2026-07-17",
                daily_cutoff_key="2026-07-16",
                weekly_cutoff_key="202627",
                monthly_cutoff_key="202606",
            )
            with (
                patch(
                    "scheduler.blackbox_v2_runner.subprocess.Popen",
                    side_effect=AssertionError("child process started"),
                ) as popen,
                self.assertRaisesRegex(
                    ValueError,
                    "request_id=second.*weekly_cutoff_key mismatch",
                ),
            ):
                execute_blackbox_cli(
                    script_path=_write_script(root / "trial.py", _SUCCESS_SCRIPT),
                    mode="backtest",
                    input_path=write_requests(
                        [_request("first"), mismatched_second_request],
                        root / "requests.csv",
                    ),
                    data_dir=data_dir,
                    output_path=root / "run" / "backtest.csv",
                    platform_input_ids=("api-wind-date-v1",),
                    profile=RuntimeProfile.for_tests(),
                )

        popen.assert_not_called()

    def test_scheduler_dispatches_by_explicit_runtime_type(self) -> None:
        from scheduler.executor import run_configured_scheme

        cfg = SimpleNamespace(
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
            scheme_id="blackbox_trial",
        )
        with patch("scheduler.executor.run_blackbox_scheme_subprocess", return_value=["blackbox"]) as blackbox:
            with patch("scheduler.executor.run_scheme_subprocess") as native:
                result = run_configured_scheme(
                    cfg,
                    "2026-07-16",
                    engine="engine",
                    algo_env="forecast_env",
                    timeout_sec=3600,
                )

        self.assertEqual(result, ["blackbox"])
        blackbox.assert_called_once()
        native.assert_not_called()

    def test_native_dispatch_rejects_historical_blackbox_snapshot_mode(self) -> None:
        from scheduler.executor import run_configured_scheme

        cfg = SimpleNamespace(runtime_type="native_adapter", scheme_id="native_trial")
        with self.assertRaisesRegex(ValueError, "snapshot mode"):
            run_configured_scheme(
                cfg,
                "2026-05-26",
                engine="engine",
                algo_env="forecast_env",
                timeout_sec=600,
                blackbox_snapshot_mode="historical_as_of_replay",
            )

    def test_scheduled_blackbox_uses_fresh_temporary_current_snapshot(self) -> None:
        from scheduler.executor import run_blackbox_scheme_subprocess
        from scheduler.process_control import ProcessStartGuard
        from shared.blackbox_v2.snapshot import BlackboxSnapshot, CutoffKeys

        events: list[str] = []

        @contextmanager
        def open_snapshot(**kwargs):
            self.assertEqual(kwargs["snapshot_date"], "2026-07-16")
            self.assertTrue(kwargs["require_fresh"])
            self.assertEqual(
                kwargs["data_root"],
                Path("/srv/bond/data_bridge"),
            )
            self.assertEqual(
                kwargs["refresh_runtime_root"],
                Path("/srv/bond/data_bridge_runtime"),
            )
            self.assertEqual(
                kwargs["schema_path"],
                Path("/srv/bond/data_bridge_schema.json"),
            )
            events.append("opened")
            yield BlackboxSnapshot(
                snapshot_id="snapshot-current",
                root_dir=Path("/tmp/snapshot-current"),
                data_dir=Path("/tmp/snapshot-current/data"),
                manifest_path=Path("/tmp/snapshot-current/manifest.json"),
                schema_version="data-bridge-v1",
            )
            events.append("closed")

        cfg = SimpleNamespace(
            scheme_id="blackbox_trial",
            input_source="data_bridge_current",
            delivery_script=Path("trial.py"),
            delivery_metadata=Path("trial.json"),
        )

        def process_started(_pid: int, _pgid: int) -> None:
            return None

        process_start_guard = ProcessStartGuard()
        data_bridge_config = SimpleNamespace(
            data_root=Path("/srv/bond/data_bridge"),
            runtime_root=Path("/srv/bond/data_bridge_runtime"),
            schema_path=Path("/srv/bond/data_bridge_schema.json"),
        )
        trusted_bundle = SimpleNamespace(
            combined_snapshot_id="snapshot-trusted",
            platform_input_ids=("api-wind-date-v1",),
            parent_snapshot_id="snapshot-parent-trusted",
            identity_manifest={"identity": "trusted"},
            audit_manifest={"audit": "trusted"},
        )

        @contextmanager
        def open_runtime_view(bundle):
            self.assertEqual(
                bundle.combined_snapshot_id,
                "snapshot-current",
            )
            yield SimpleNamespace(
                data_dir=Path("/tmp/private-runtime-view"),
                bundle=trusted_bundle,
            )

        with (
            patch("scheduler.executor.load_metadata", return_value=_metadata()),
            patch(
                "shared.data_bridge.refresh.DataBridgeRefreshConfig.from_env",
                return_value=data_bridge_config,
            ) as data_bridge_config_from_env,
            patch(
                "scheduler.executor.open_blackbox_input_snapshot",
                side_effect=open_snapshot,
            ),
            patch("scheduler.executor.get_calendar", return_value="calendar"),
            patch(
                "scheduler.executor.build_daily_live_context",
                return_value=SimpleNamespace(feature_date="2026-07-15"),
            ),
            patch(
                "scheduler.executor.resolve_blackbox_input_cutoffs",
                return_value=CutoffKeys("2026-07-15", "202627", "202606"),
            ),
            patch(
                "scheduler.executor.build_live_request",
                return_value=_request("001"),
            ),
            patch(
                "scheduler.blackbox_v2_runner.run_blackbox_predict",
                return_value="record",
            ) as predict,
            patch(
                "scheduler.executor.open_blackbox_runtime_view",
                side_effect=open_runtime_view,
            ),
        ):
            result = run_blackbox_scheme_subprocess(
                cfg,
                "2026-07-16",
                engine="engine",
                algo_env="forecast_env_blackbox_v1",
                timeout_sec=300,
                execution_token="scheduled-token_123",
                process_started=process_started,
                process_start_guard=process_start_guard,
            )

        self.assertEqual(result, ["record"])
        data_bridge_config_from_env.assert_called_once_with()
        self.assertEqual(events, ["opened", "closed"])
        self.assertIs(
            predict.call_args.kwargs["process_started"],
            process_started,
        )
        self.assertEqual(
            predict.call_args.kwargs["execution_token"],
            "scheduled-token_123",
        )
        self.assertEqual(
            predict.call_args.kwargs["data_dir"],
            Path("/tmp/private-runtime-view"),
        )
        self.assertEqual(
            predict.call_args.kwargs["data_snapshot_id"],
            "snapshot-trusted",
        )
        self.assertEqual(
            predict.call_args.kwargs["platform_input_ids"],
            ("api-wind-date-v1",),
        )
        self.assertEqual(
            predict.call_args.kwargs["parent_data_snapshot_id"],
            "snapshot-parent-trusted",
        )
        self.assertEqual(
            predict.call_args.kwargs["input_identity_manifest"],
            {"identity": "trusted"},
        )
        self.assertEqual(
            predict.call_args.kwargs["input_audit_manifest"],
            {"audit": "trusted"},
        )
        self.assertEqual(
            predict.call_args.kwargs["profile"].predict_timeout_sec,
            600,
        )
        self.assertEqual(
            predict.call_args.kwargs["timeout_sec"],
            300,
        )
        self.assertIs(
            predict.call_args.kwargs["process_start_guard"],
            process_start_guard,
        )

    def test_predict_forwards_optional_operation_deadline_independently(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            run_blackbox_predict,
        )

        for deadline in (None, 1800):
            with (
                self.subTest(deadline=deadline),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                root = Path(tmpdir)
                kwargs = {
                    "metadata": _metadata(),
                    "script_path": root / "trial.py",
                    "request": _request("001"),
                    "data_dir": root / "data",
                    "data_snapshot_id": "snapshot-test",
                    "profile": RuntimeProfile.for_tests(
                        predict_timeout_sec=600
                    ),
                    "timeout_sec": deadline,
                }
                with (
                    patch(
                        "scheduler.blackbox_v2_runner."
                        "execute_blackbox_cli"
                    ) as execute,
                    patch(
                        "scheduler.blackbox_v2_runner."
                        "load_prediction_result",
                        return_value="result",
                    ),
                    patch(
                        "scheduler.blackbox_v2_runner."
                        "_to_prediction_record",
                        return_value="record",
                    ),
                ):
                    self.assertEqual(
                        run_blackbox_predict(**kwargs),
                        "record",
                    )

                if deadline is None:
                    self.assertNotIn(
                        "timeout_sec",
                        execute.call_args.kwargs,
                    )
                else:
                    self.assertEqual(
                        execute.call_args.kwargs["timeout_sec"],
                        deadline,
                    )

    def test_predict_operation_deadline_cannot_enlarge_profile(self) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            execute_blackbox_cli,
        )
        from shared.blackbox_v2.requests import write_request

        for deadline, expected in ((1800, 600.0), (300, 300.0)):
            with (
                self.subTest(deadline=deadline),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                root = Path(tmpdir)
                script = _write_script(root / "trial.py", _SUCCESS_SCRIPT)
                data_dir = _write_data_dir(root)
                request = write_request(
                    _request("001"),
                    root / "request.json",
                )
                output = root / "run" / "prediction.json"

                def completed(*_args, **_kwargs):
                    output.write_text("{}\n", encoding="utf-8")
                    return subprocess.CompletedProcess([], 0, "", "")

                with patch(
                    "scheduler.blackbox_v2_runner._run_process",
                    side_effect=completed,
                ) as run:
                    execute_blackbox_cli(
                        script_path=script,
                        mode="predict",
                        input_path=request,
                        data_dir=data_dir,
                        output_path=output,
                        profile=RuntimeProfile.for_tests(
                            predict_timeout_sec=600
                        ),
                        timeout_sec=deadline,
                    )

                self.assertEqual(
                    run.call_args.kwargs["timeout"],
                    expected,
                )

    def test_historical_replay_uses_as_of_snapshot_with_provenance(self) -> None:
        from scheduler.executor import run_blackbox_scheme_subprocess
        from shared.blackbox_v2.snapshot import (
            BlackboxSnapshot,
            CutoffKeys,
        )
        from shared.models import PredictionRecord

        @contextmanager
        def open_snapshot(**kwargs):
            self.assertEqual(kwargs["snapshot_date"], "2026-05-26")
            self.assertFalse(kwargs["require_fresh"])
            yield BlackboxSnapshot(
                snapshot_id="snapshot-current",
                root_dir=Path("/tmp/snapshot-current"),
                data_dir=Path("/tmp/snapshot-current/data"),
                manifest_path=Path(
                    "/tmp/snapshot-current/manifest.json"
                ),
                schema_version="data-bridge-v1",
                generation_id="full-20260720-test",
                refresh_date="2026-07-20",
            )

        @contextmanager
        def open_runtime_view(bundle):
            yield SimpleNamespace(
                data_dir=Path("/tmp/private-runtime-view"),
                bundle=bundle,
            )

        cfg = SimpleNamespace(
            scheme_id="blackbox_trial",
            input_source="data_bridge_current",
            delivery_script=Path("trial.py"),
            delivery_metadata=Path("trial.json"),
        )
        raw_record = PredictionRecord(
            scheme_id="blackbox_trial",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-05-26",
            feature_date="2026-05-25",
            target_date="2026-05-26",
            predicted_direction=1,
            extra={"data_snapshot_id": "snapshot-current"},
        )
        with (
            patch("scheduler.executor.load_metadata", return_value=_metadata()),
            patch("scheduler.executor.open_blackbox_input_snapshot", side_effect=open_snapshot),
            patch("scheduler.executor.get_calendar", return_value="calendar"),
            patch(
                "scheduler.executor.build_daily_live_context",
                return_value=SimpleNamespace(feature_date="2026-05-25"),
            ),
            patch(
                "scheduler.executor.resolve_blackbox_input_cutoffs",
                return_value=CutoffKeys("2026-05-25", "202621", "202605"),
            ),
            patch(
                "scheduler.executor.open_blackbox_runtime_view",
                side_effect=open_runtime_view,
            ),
            patch("scheduler.executor.build_live_request", return_value=_request("001")),
            patch(
                "scheduler.blackbox_v2_runner.run_blackbox_predict",
                return_value=raw_record,
            ),
        ):
            result = run_blackbox_scheme_subprocess(
                cfg,
                "2026-05-26",
                engine="engine",
                algo_env="forecast_env_blackbox_v1",
                timeout_sec=600,
                snapshot_mode="historical_as_of_replay",
            )

        extra = result[0].extra
        self.assertEqual(
            extra["replay_semantics"],
            "current_snapshot_as_of_not_historical_vintage",
        )
        self.assertEqual(
            extra["backfill_mode"],
            "post_deployment_live_safe_replay",
        )
        self.assertEqual(extra["data_generation_id"], "full-20260720-test")
        self.assertEqual(extra["source_refresh_date"], "2026-07-20")
        self.assertEqual(extra["daily_cutoff_key"], "2026-05-25")
        self.assertEqual(extra["weekly_cutoff_key"], "202621")
        self.assertEqual(extra["monthly_cutoff_key"], "202605")
        self.assertTrue(extra["backfilled_at"].endswith("+00:00"))

    def test_historical_replay_allows_snapshot_refreshed_on_predict_date(
        self,
    ) -> None:
        """同日 DataBridge 刷新可用于前一 feature 日的历史重放。"""
        from scheduler.executor import _validate_historical_snapshot

        snapshot = SimpleNamespace(
            generation_id="full-20260807-test",
            refresh_date="2026-08-07",
        )

        _validate_historical_snapshot(
            snapshot,
            predict_date="2026-08-07",
            expected_generation_id="full-20260807-test",
            expected_refresh_date="2026-08-07",
        )

    def test_unconfirmed_process_cleanup_preserves_runtime_view(self) -> None:
        from scheduler.executor import run_blackbox_scheme_subprocess
        from scheduler.process_control import (
            ProcessGroupTerminationError,
            ProcessGroupTerminationResult,
        )
        from shared.blackbox_v2.snapshot import (
            BlackboxSnapshot,
            CutoffKeys,
        )

        snapshot = BlackboxSnapshot(
            snapshot_id="snapshot-current",
            root_dir=Path("/tmp/snapshot-current"),
            data_dir=Path("/tmp/snapshot-current/data"),
            manifest_path=Path("/tmp/snapshot-current/manifest.json"),
            schema_version="data-bridge-v1",
        )
        @contextmanager
        def open_snapshot(**_kwargs):
            yield snapshot

        @contextmanager
        def open_runtime_view(bundle):
            view.bundle = bundle
            yield view

        view = SimpleNamespace(
            data_dir=Path("/tmp/private-runtime-view"),
            bundle=None,
            mark_termination_uncertain=Mock(),
        )

        cleanup_error = ProcessGroupTerminationError(
            termination=ProcessGroupTerminationResult(
                process_id=12345,
                process_group_id=12345,
                term_sent=True,
                kill_sent=True,
                confirmed_gone=False,
                failure_reason="still running",
            ),
            context="test runtime",
        )
        cfg = SimpleNamespace(
            scheme_id="blackbox_trial",
            input_source="data_bridge_current",
            delivery_script=Path("trial.py"),
            delivery_metadata=Path("trial.json"),
        )
        with (
            patch(
                "scheduler.executor.open_blackbox_input_snapshot",
                side_effect=open_snapshot,
            ),
            patch(
                "scheduler.executor.open_blackbox_runtime_view",
                side_effect=open_runtime_view,
            ),
            patch(
                "scheduler.executor.load_metadata",
                return_value=_metadata(),
            ),
            patch("scheduler.executor.get_calendar", return_value="calendar"),
            patch(
                "scheduler.executor.build_daily_live_context",
                return_value=SimpleNamespace(
                    feature_date="2026-07-15"
                ),
            ),
            patch(
                "scheduler.executor.resolve_blackbox_input_cutoffs",
                return_value=CutoffKeys(
                    "2026-07-15",
                    "202627",
                    "202607",
                ),
            ),
            patch(
                "scheduler.executor.build_live_request",
                return_value=_request("001"),
            ),
            patch(
                "scheduler.blackbox_v2_runner.run_blackbox_predict",
                side_effect=cleanup_error,
            ),
            self.assertRaises(ProcessGroupTerminationError),
        ):
            run_blackbox_scheme_subprocess(
                cfg,
                "2026-07-16",
                engine="engine",
                algo_env="forecast_env_blackbox_v1",
                timeout_sec=3600,
            )

        view.mark_termination_uncertain.assert_called_once_with()

    def test_nonbound_platform_capture_uses_read_only_transaction(
        self,
    ) -> None:
        from scheduler.executor import (
            _capture_blackbox_platform_inputs_from_engine,
        )

        events: list[str] = []

        class Connection:
            def __enter__(self):
                events.append("enter")
                return self

            def __exit__(self, *_exc_info):
                events.append("exit")

            def exec_driver_sql(self, statement):
                events.append(statement)

            def rollback(self):
                events.append("rollback")

        class Engine:
            def connect(self):
                return Connection()

        with patch(
            "scheduler.executor."
            "capture_blackbox_platform_inputs_from_connection",
            return_value=("artifact",),
        ) as capture:
            result = _capture_blackbox_platform_inputs_from_engine(
                Engine(),
                platform_input_ids=("api-wind-date-v1",),
                weekly_cutoff_key="202627",
            )

        self.assertEqual(result, ("artifact",))
        self.assertEqual(
            events,
            [
                "enter",
                "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
                "rollback",
                "exit",
            ],
        )
        capture.assert_called_once_with(
            ("api-wind-date-v1",),
            connection=capture.call_args.kwargs["connection"],
            weekly_cutoff_key="202627",
        )

    def test_historical_replay_rejects_changed_current_generation(self) -> None:
        from scheduler.executor import run_blackbox_scheme_subprocess

        @contextmanager
        def open_snapshot(**_kwargs):
            yield SimpleNamespace(
                snapshot_id="snapshot-current",
                data_dir=Path("/tmp/snapshot-current/data"),
                generation_id="full-generation-b",
                refresh_date="2026-07-20",
            )

        cfg = SimpleNamespace(
            scheme_id="blackbox_trial",
            input_source="data_bridge_current",
            delivery_script=Path("trial.py"),
            delivery_metadata=Path("trial.json"),
        )
        with (
            patch("scheduler.executor.load_metadata", return_value=_metadata()),
            patch(
                "scheduler.executor.open_blackbox_input_snapshot",
                side_effect=open_snapshot,
            ),
        ):
            with self.assertRaisesRegex(ValueError, "generation changed"):
                run_blackbox_scheme_subprocess(
                    cfg,
                    "2026-05-26",
                    engine="engine",
                    algo_env="forecast_env_blackbox_v1",
                    timeout_sec=600,
                    snapshot_mode="historical_as_of_replay",
                    expected_generation_id="full-generation-a",
                    expected_refresh_date="2026-07-20",
                )

    def test_predict_converts_valid_result_to_prediction_record(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, run_blackbox_predict

        evidence = _platform_bundle_evidence()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "trial.py", _SUCCESS_SCRIPT)
            data_dir = _write_data_dir(root)
            (data_dir / "api_wind_date.csv").write_text(
                "rdate,week_id\n2026-07-15,202627\n",
                encoding="utf-8",
            )
            record = run_blackbox_predict(
                metadata=_metadata(),
                script_path=script,
                request=_request("001"),
                data_dir=data_dir,
                data_snapshot_id=evidence.combined_snapshot_id,
                platform_input_ids=("api-wind-date-v1",),
                parent_data_snapshot_id=evidence.parent_snapshot_id,
                input_identity_manifest=evidence.identity_manifest,
                input_audit_manifest=evidence.audit_manifest,
                profile=RuntimeProfile.for_tests(),
            )

        self.assertEqual(record.scheme_id, "blackbox_trial")
        self.assertEqual(record.target_tenor, "10Y")
        self.assertEqual(record.predicted_direction, 1)
        self.assertEqual(record.extra["runtime_type"], "blackbox_v2")
        self.assertEqual(record.extra["request_id"], "001")
        self.assertEqual(
            record.extra["data_snapshot_id"],
            evidence.combined_snapshot_id,
        )
        self.assertEqual(
            record.extra["parent_data_snapshot_id"],
            evidence.parent_snapshot_id,
        )
        self.assertEqual(
            record.extra["platform_input_ids"],
            ["api-wind-date-v1"],
        )
        self.assertEqual(
            record.extra["platform_input_identity_manifest"][
                "platform_inputs"
            ][0]["artifact_id"],
            "api-wind-date-v1",
        )
        self.assertEqual(
            record.extra["platform_input_audit_manifest"][
                "platform_inputs"
            ][0]["provenance"]["source_kind"],
            "harness_database",
        )

    def test_predict_rejects_missing_platform_evidence_before_execution(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            run_blackbox_predict,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = _write_data_dir(root)
            (data_dir / "api_wind_date.csv").write_text(
                "rdate,week_id\n2026-07-24,202629\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "scheduler.blackbox_v2_runner."
                    "execute_blackbox_cli",
                ) as execute,
                self.assertRaisesRegex(
                    ValueError,
                    "require parent snapshot",
                ),
            ):
                run_blackbox_predict(
                    metadata=_metadata(),
                    script_path=root / "trial.py",
                    request=_request("001"),
                    data_dir=data_dir,
                    data_snapshot_id="snapshot-combined",
                    platform_input_ids=("api-wind-date-v1",),
                    profile=RuntimeProfile.for_tests(),
                )

        execute.assert_not_called()

    def test_predict_rejects_mismatched_combined_snapshot_before_execution(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            run_blackbox_predict,
        )

        evidence = _platform_bundle_evidence()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with (
                patch(
                    "scheduler.blackbox_v2_runner."
                    "execute_blackbox_cli",
                ) as execute,
                self.assertRaisesRegex(
                    ValueError,
                    "combined snapshot",
                ),
            ):
                run_blackbox_predict(
                    metadata=_metadata(),
                    script_path=root / "trial.py",
                    request=_request("001"),
                    data_dir=root / "data",
                    data_snapshot_id="snapshot-tampered",
                    platform_input_ids=("api-wind-date-v1",),
                    parent_data_snapshot_id=evidence.parent_snapshot_id,
                    input_identity_manifest=evidence.identity_manifest,
                    input_audit_manifest=evidence.audit_manifest,
                    profile=RuntimeProfile.for_tests(),
                )

        execute.assert_not_called()

    def test_backtest_rejects_audit_identity_mismatch_before_execution(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            run_blackbox_backtest,
        )

        evidence = _platform_bundle_evidence()
        audit_manifest = evidence.audit_manifest
        audit_manifest["identity"]["parent_snapshot_id"] = (
            "snapshot-tampered"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with (
                patch(
                    "scheduler.blackbox_v2_runner."
                    "execute_blackbox_cli",
                ) as execute,
                self.assertRaisesRegex(
                    ValueError,
                    "audit manifest identity",
                ),
            ):
                run_blackbox_backtest(
                    metadata=_metadata(),
                    script_path=root / "trial.py",
                    requests=[_request("001"), _request("002")],
                    data_dir=root / "data",
                    data_snapshot_id=evidence.combined_snapshot_id,
                    platform_input_ids=("api-wind-date-v1",),
                    parent_data_snapshot_id=evidence.parent_snapshot_id,
                    input_identity_manifest=evidence.identity_manifest,
                    input_audit_manifest=audit_manifest,
                    profile=RuntimeProfile.for_tests(
                        max_batch_requests=1,
                    ),
                )

        execute.assert_not_called()

    def test_predict_subprocess_receives_exact_explicit_execution_token(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            run_blackbox_predict,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            record = run_blackbox_predict(
                metadata=_metadata(),
                script_path=_write_script(
                    root / "execution-token.py",
                    _EXECUTION_TOKEN_SUCCESS_SCRIPT,
                ),
                request=_request("001"),
                data_dir=_write_data_dir(root),
                data_snapshot_id="snapshot-test",
                profile=RuntimeProfile.for_tests(),
                execution_token="scheduled-token_123",
            )

        self.assertEqual(record.predicted_direction, 1)

    def test_predict_subprocess_does_not_inherit_execution_token(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            run_blackbox_predict,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(
                os.environ,
                {"BOND_SCHEDULE_EXECUTION_TOKEN": "stale-parent-token"},
            ):
                record = run_blackbox_predict(
                    metadata=_metadata(),
                    script_path=_write_script(
                        root / "execution-token-absent.py",
                        _EXECUTION_TOKEN_ABSENT_SCRIPT,
                    ),
                    request=_request("001"),
                    data_dir=_write_data_dir(root),
                    data_snapshot_id="snapshot-test",
                    profile=RuntimeProfile.for_tests(),
                )

        self.assertEqual(record.predicted_direction, 1)

    def test_predict_rejects_unsafe_execution_token_before_process_start(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            run_blackbox_predict,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch(
                "scheduler.blackbox_v2_runner._run_process",
            ) as process:
                with self.assertRaisesRegex(
                    ValueError,
                    "execution_token is unsafe",
                ):
                    run_blackbox_predict(
                        metadata=_metadata(),
                        script_path=_write_script(
                            root / "trial.py",
                            _SUCCESS_SCRIPT,
                        ),
                        request=_request("001"),
                        data_dir=_write_data_dir(root),
                        data_snapshot_id="snapshot-test",
                        profile=RuntimeProfile.for_tests(),
                        execution_token="../unsafe",
                    )

        process.assert_not_called()

    def test_predict_reports_real_pid_and_pgid_exactly_once(self) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            run_blackbox_predict,
        )

        started: list[tuple[int, int]] = []
        fence_calls: list[str] = []
        from scheduler.process_control import ProcessStartGuard

        process_start_guard = ProcessStartGuard()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            record = run_blackbox_predict(
                metadata=_metadata(),
                script_path=_write_script(root / "trial.py", _SUCCESS_SCRIPT),
                request=_request("001"),
                data_dir=_write_data_dir(root),
                data_snapshot_id="snapshot-test",
                profile=RuntimeProfile.for_tests(),
                process_started=lambda pid, pgid: started.append((pid, pgid)),
                process_fence=lambda: fence_calls.append("epoch-fence"),
                process_start_guard=process_start_guard,
            )

        self.assertEqual(record.predicted_direction, 1)
        self.assertEqual(len(started), 1)
        self.assertEqual(fence_calls, ["epoch-fence", "epoch-fence"])
        self.assertGreater(started[0][0], 1)
        self.assertGreater(started[0][1], 1)

    def test_blackbox_backtest_rejects_process_start_guard(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            execute_blackbox_cli,
        )
        from scheduler.process_control import ProcessStartGuard

        with self.assertRaisesRegex(
            ValueError,
            "only valid for Blackbox predict",
        ):
            execute_blackbox_cli(
                script_path="/missing/trial.py",
                mode="backtest",
                input_path="/missing/requests.jsonl",
                data_dir="/missing/data",
                output_path="/missing/output.jsonl",
                profile=RuntimeProfile.for_tests(),
                process_start_guard=ProcessStartGuard(),
            )

    def test_blackbox_process_start_guard_has_exact_short_sequence(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import _run_process

        events: list[object] = []
        guard_lock = threading.Lock()

        class RecordingGuard:
            def __enter__(self):
                guard_lock.acquire()
                events.append("guard-enter")
                return self

            def __exit__(self, *_exc_info):
                events.append("guard-exit")
                guard_lock.release()

        class FakeProcess:
            pid = 22334
            returncode = 0

            def poll(self):
                if guard_lock.locked():
                    raise AssertionError(
                        "poll must run after process-start guard release"
                    )
                events.append("poll")
                return self.returncode

        def popen(*_args, **_kwargs):
            events.append("popen")
            return FakeProcess()

        def getpgid(pid: int) -> int:
            events.append(("capture-pgid", pid))
            return 33445

        def process_started(pid: int, pgid: int) -> None:
            events.append(("started", pid, pgid))

        def process_fence() -> None:
            events.append("epoch-fence")

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch(
                    "scheduler.blackbox_v2_runner.subprocess.Popen",
                    side_effect=popen,
                ),
                patch(
                    "scheduler.blackbox_v2_runner.os.getpgid",
                    side_effect=getpgid,
                ),
            ):
                completed = _run_process(
                    ["blackbox"],
                    cwd=Path(tmpdir),
                    env={},
                    timeout=10,
                    memory_limit_bytes=0,
                    max_capture_bytes=1024,
                    max_run_dir_bytes=4096,
                    max_run_dir_entries=10,
                    process_started=process_started,
                    process_fence=process_fence,
                    process_start_guard=RecordingGuard(),
                )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            events,
            [
                "guard-enter",
                "epoch-fence",
                "popen",
                ("capture-pgid", 22334),
                ("started", 22334, 33445),
                "epoch-fence",
                "guard-exit",
                "poll",
            ],
        )

    def test_blackbox_guard_exit_failure_kills_registered_process_group(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import _run_process

        events: list[object] = []

        class GuardReleaseError(RuntimeError):
            pass

        class FailingExitGuard:
            def __enter__(self):
                events.append("guard-enter")
                return self

            def __exit__(self, *_exc_info):
                events.append("guard-exit")
                raise GuardReleaseError("guard release failed")

        class FakeProcess:
            pid = 22334
            returncode = None

            def poll(self):
                raise AssertionError(
                    "guard-release failure must stop algorithm runtime"
                )

        def process_started(pid: int, pgid: int) -> None:
            events.append(("started", pid, pgid))

        def killpg(pgid: int, signum: int) -> None:
            events.append(("killpg", pgid, signum))

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch(
                    "scheduler.blackbox_v2_runner.subprocess.Popen",
                    return_value=FakeProcess(),
                ),
                patch(
                    "scheduler.blackbox_v2_runner.os.getpgid",
                    return_value=33445,
                ),
                patch(
                    "scheduler.blackbox_v2_runner.os.killpg",
                    side_effect=killpg,
                ),
                patch(
                    "scheduler.process_control."
                    "_wait_for_process_group_exit",
                    return_value=True,
                ),
            ):
                with self.assertRaisesRegex(
                    GuardReleaseError,
                    "guard release failed",
                ):
                    _run_process(
                        ["blackbox"],
                        cwd=Path(tmpdir),
                        env={},
                        timeout=10,
                        memory_limit_bytes=0,
                        max_capture_bytes=1024,
                        max_run_dir_bytes=4096,
                        max_run_dir_entries=10,
                        process_started=process_started,
                        process_start_guard=FailingExitGuard(),
                    )

        self.assertEqual(
            events,
            [
                "guard-enter",
                ("started", 22334, 33445),
                "guard-exit",
                ("killpg", 33445, signal.SIGTERM),
            ],
        )

    def test_blackbox_unconfirmed_post_fence_cleanup_wins_over_guard_exit(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import _run_process
        from scheduler.process_control import (
            ProcessRegistrationCleanupError,
        )

        events: list[object] = []

        class EpochDriftError(RuntimeError):
            pass

        class GuardReleaseError(RuntimeError):
            pass

        class FailingExitGuard:
            def __enter__(self):
                events.append("guard-enter")
                return self

            def __exit__(self, *_exc_info):
                events.append("guard-exit")
                raise GuardReleaseError("guard release failed")

        class FakeProcess:
            pid = 22334
            returncode = None

            def poll(self):
                raise AssertionError(
                    "unconfirmed cleanup must stop algorithm runtime"
                )

        fence_calls = 0
        epoch_drift_error = EpochDriftError(
            "post-registration epoch drift"
        )

        def process_fence() -> None:
            nonlocal fence_calls
            fence_calls += 1
            events.append(("fence", fence_calls))
            if fence_calls == 2:
                raise epoch_drift_error

        def killpg(pgid: int, signum: int) -> None:
            events.append(("killpg", pgid, signum))

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch(
                    "scheduler.blackbox_v2_runner.subprocess.Popen",
                    return_value=FakeProcess(),
                ),
                patch(
                    "scheduler.blackbox_v2_runner.os.getpgid",
                    return_value=33445,
                ),
                patch(
                    "scheduler.blackbox_v2_runner.os.killpg",
                    side_effect=killpg,
                ),
                patch(
                    "scheduler.process_control."
                    "_wait_for_process_group_exit",
                    side_effect=(False, False),
                ),
            ):
                with self.assertRaises(
                    ProcessRegistrationCleanupError
                ) as raised:
                    _run_process(
                        ["blackbox"],
                        cwd=Path(tmpdir),
                        env={},
                        timeout=10,
                        memory_limit_bytes=0,
                        max_capture_bytes=1024,
                        max_run_dir_bytes=4096,
                        max_run_dir_entries=10,
                        process_fence=process_fence,
                        process_start_guard=FailingExitGuard(),
                    )

        self.assertIs(
            raised.exception.registration_error,
            epoch_drift_error,
        )
        self.assertIsInstance(
            raised.exception.__cause__,
            GuardReleaseError,
        )
        self.assertFalse(
            raised.exception.termination.confirmed_gone
        )
        self.assertEqual(
            events,
            [
                "guard-enter",
                ("fence", 1),
                ("fence", 2),
                ("killpg", 33445, signal.SIGTERM),
                ("killpg", 33445, signal.SIGKILL),
                "guard-exit",
            ],
        )

    def test_blackbox_callback_failure_kills_group_before_reraising(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import _run_process

        events: list[object] = []

        class RegistrationError(RuntimeError):
            pass

        class RecordingGuard:
            def __enter__(self):
                events.append("guard-enter")
                return self

            def __exit__(self, *_exc_info):
                events.append("guard-exit")

        class FakeProcess:
            pid = 22334
            returncode = None

            def __init__(self) -> None:
                self.wait_calls = 0

            def poll(self):
                events.append("poll")
                return self.returncode

            def wait(self, timeout=None):
                self.wait_calls += 1
                events.append(("wait", timeout))
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired(
                        cmd=["blackbox"],
                        timeout=timeout,
                    )
                self.returncode = -signal.SIGKILL
                return self.returncode

        def process_started(pid: int, pgid: int) -> None:
            events.append(("started", pid, pgid))
            raise RegistrationError("ledger write failed")

        def killpg(pgid: int, signum: int) -> None:
            events.append(("killpg", pgid, signum))

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch(
                    "scheduler.blackbox_v2_runner.subprocess.Popen",
                    return_value=FakeProcess(),
                ),
                patch(
                    "scheduler.blackbox_v2_runner.os.getpgid",
                    return_value=33445,
                ),
                patch(
                    "scheduler.blackbox_v2_runner.os.killpg",
                    side_effect=killpg,
                ),
                patch(
                    "scheduler.process_control."
                    "_wait_for_process_group_exit",
                    side_effect=(False, True),
                ),
            ):
                with self.assertRaisesRegex(
                    RegistrationError,
                    "ledger write failed",
                ):
                    _run_process(
                        ["blackbox"],
                        cwd=Path(tmpdir),
                        env={},
                        timeout=10,
                        memory_limit_bytes=0,
                        max_capture_bytes=1024,
                        max_run_dir_bytes=4096,
                        max_run_dir_entries=10,
                        process_started=process_started,
                        process_start_guard=RecordingGuard(),
                    )

        self.assertEqual(
            events,
            [
                "guard-enter",
                ("started", 22334, 33445),
                ("killpg", 33445, signal.SIGTERM),
                ("killpg", 33445, signal.SIGKILL),
                "guard-exit",
            ],
        )

    def test_blackbox_callback_failure_fences_when_group_survives_kill(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import _run_process
        from scheduler.process_control import (
            ProcessRegistrationCleanupError,
        )

        events: list[object] = []

        class RegistrationError(RuntimeError):
            pass

        class FakeProcess:
            pid = 22334
            returncode = None

            def poll(self):
                return self.returncode

        registration_error = RegistrationError("ledger write failed")

        def process_started(pid: int, pgid: int) -> None:
            events.append(("started", pid, pgid))
            raise registration_error

        def killpg(pgid: int, signum: int) -> None:
            events.append(("killpg", pgid, signum))

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch(
                    "scheduler.blackbox_v2_runner.subprocess.Popen",
                    return_value=FakeProcess(),
                ),
                patch(
                    "scheduler.blackbox_v2_runner.os.getpgid",
                    return_value=33445,
                ),
                patch(
                    "scheduler.blackbox_v2_runner.os.killpg",
                    side_effect=killpg,
                ),
                patch(
                    "scheduler.process_control."
                    "_wait_for_process_group_exit",
                    side_effect=(False, False),
                ),
            ):
                with self.assertRaises(
                    ProcessRegistrationCleanupError
                ) as raised:
                    _run_process(
                        ["blackbox"],
                        cwd=Path(tmpdir),
                        env={},
                        timeout=10,
                        memory_limit_bytes=0,
                        max_capture_bytes=1024,
                        max_run_dir_bytes=4096,
                        max_run_dir_entries=10,
                        process_started=process_started,
                    )

        self.assertIs(raised.exception.registration_error, registration_error)
        self.assertFalse(
            raised.exception.termination.confirmed_gone
        )
        self.assertEqual(
            raised.exception.termination.process_group_id,
            33445,
        )
        self.assertEqual(
            events,
            [
                ("started", 22334, 33445),
                ("killpg", 33445, signal.SIGTERM),
                ("killpg", 33445, signal.SIGKILL),
            ],
        )

    def test_blackbox_captures_process_group_before_failure_poll(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            BlackboxExecutionError,
            _run_process,
        )

        events: list[object] = []

        class FakeProcess:
            pid = 22334
            returncode = None

            def poll(self):
                events.append("poll")
                return None

        def getpgid(pid: int) -> int:
            events.append(("getpgid", pid))
            return 33445

        def killpg(pgid: int, signum: int) -> None:
            events.append(("killpg", pgid, signum))

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch(
                    "scheduler.blackbox_v2_runner.subprocess.Popen",
                    return_value=FakeProcess(),
                ),
                patch(
                    "scheduler.blackbox_v2_runner.os.getpgid",
                    side_effect=getpgid,
                ),
                patch(
                    "scheduler.blackbox_v2_runner.os.killpg",
                    side_effect=killpg,
                ),
                patch(
                    "scheduler.process_control."
                    "_wait_for_process_group_exit",
                    return_value=True,
                ),
            ):
                with self.assertRaisesRegex(
                    BlackboxExecutionError,
                    "timed out",
                ):
                    _run_process(
                        ["blackbox"],
                        cwd=Path(tmpdir),
                        env={},
                        timeout=0,
                        memory_limit_bytes=0,
                        max_capture_bytes=1024,
                        max_run_dir_bytes=4096,
                        max_run_dir_entries=10,
                    )

        self.assertEqual(
            events[:3],
            [
                ("getpgid", 22334),
                "poll",
                ("killpg", 33445, signal.SIGTERM),
            ],
        )

    def test_failed_process_removes_output_even_if_script_wrote_one(self) -> None:
        from scheduler.blackbox_v2_runner import (
            BlackboxExecutionError,
            RuntimeProfile,
            execute_blackbox_cli,
        )
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "trial.py", _FAIL_AFTER_OUTPUT_SCRIPT)
            request_path = write_request(_request("001"), root / "request.json")
            run_dir = root / "run"
            run_dir.mkdir()
            output_path = run_dir / "prediction.json"
            with self.assertRaises(BlackboxExecutionError):
                execute_blackbox_cli(
                    script_path=script,
                    mode="predict",
                    input_path=request_path,
                    data_dir=_write_data_dir(root),
                    output_path=output_path,
                    profile=RuntimeProfile.for_tests(),
                )
            self.assertFalse(output_path.exists())

    def test_backtest_splits_batches_and_preserves_order(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, run_blackbox_backtest

        requests = [_request(f"{index:03d}") for index in range(205)]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "trial.py", _SUCCESS_SCRIPT)
            records = run_blackbox_backtest(
                metadata=_metadata(),
                script_path=script,
                requests=requests,
                data_dir=_write_data_dir(root),
                data_snapshot_id="snapshot-test",
                profile=RuntimeProfile.for_tests(max_batch_requests=100),
            )

        self.assertEqual(len(records), 205)
        self.assertEqual([record.extra["request_id"] for record in records], [item.request_id for item in requests])

    def test_platform_evidence_is_not_shared_across_backtest_records(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            run_blackbox_backtest,
        )

        evidence = _platform_bundle_evidence()
        requests = [_request(f"{index:03d}") for index in range(3)]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = _write_data_dir(root)
            (data_dir / "api_wind_date.csv").write_text(
                "rdate,week_id\n2026-07-15,202627\n",
                encoding="utf-8",
            )
            records = run_blackbox_backtest(
                metadata=_metadata(),
                script_path=_write_script(
                    root / "trial.py",
                    _SUCCESS_SCRIPT,
                ),
                requests=requests,
                data_dir=data_dir,
                data_snapshot_id=evidence.combined_snapshot_id,
                platform_input_ids=("api-wind-date-v1",),
                parent_data_snapshot_id=evidence.parent_snapshot_id,
                input_identity_manifest=evidence.identity_manifest,
                input_audit_manifest=evidence.audit_manifest,
                profile=RuntimeProfile.for_tests(
                    max_batch_requests=1,
                ),
            )

        self.assertEqual(len(records), 3)
        first_identity = records[0].extra[
            "platform_input_identity_manifest"
        ]
        second_identity = records[1].extra[
            "platform_input_identity_manifest"
        ]
        self.assertIsNot(first_identity, second_identity)
        self.assertIsNot(
            first_identity["platform_inputs"][0],
            second_identity["platform_inputs"][0],
        )
        first_identity["platform_inputs"][0]["artifact_id"] = "tampered"
        self.assertEqual(
            second_identity["platform_inputs"][0]["artifact_id"],
            "api-wind-date-v1",
        )

    def test_real_1000_request_backtest_has_bounded_subprocess_count(self) -> None:
        from scheduler.blackbox_v2_runner import (
            BacktestExecutionBudget,
            RuntimeProfile,
            run_blackbox_backtest,
        )

        requests = [_request(f"bounded-{index:04d}") for index in range(1000)]
        budget = BacktestExecutionBudget(
            deadline_monotonic=time.monotonic() + 30,
            max_subprocesses=10,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            records = run_blackbox_backtest(
                metadata=_metadata(),
                script_path=_write_script(root / "trial.py", _SUCCESS_SCRIPT),
                requests=requests,
                data_dir=_write_data_dir(root),
                data_snapshot_id="snapshot-test",
                profile=RuntimeProfile.for_tests(max_batch_requests=100),
                budget=budget,
            )

        self.assertEqual(len(records), 1000)
        self.assertEqual(budget.subprocesses_started, 10)
        self.assertLessEqual(budget.subprocesses_started, budget.max_subprocesses)

    def test_real_1000_request_backtest_obeys_total_deadline(self) -> None:
        from scheduler.blackbox_v2_runner import (
            BacktestExecutionBudget,
            BlackboxExecutionError,
            RuntimeProfile,
            run_blackbox_backtest,
        )

        requests = [_request(f"deadline-{index:04d}") for index in range(1000)]
        budget = BacktestExecutionBudget(
            deadline_monotonic=time.monotonic() + 0.3,
            max_subprocesses=10,
        )
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaisesRegex(BlackboxExecutionError, "deadline"):
                run_blackbox_backtest(
                    metadata=_metadata(),
                    script_path=_write_script(root / "slow.py", _SLOW_BACKTEST_SCRIPT),
                    requests=requests,
                    data_dir=_write_data_dir(root),
                    data_snapshot_id="snapshot-test",
                    profile=RuntimeProfile.for_tests(
                        max_batch_requests=100,
                        backtest_timeout_sec=10,
                    ),
                    budget=budget,
                )

        self.assertLess(time.monotonic() - started, 2)
        self.assertLessEqual(budget.subprocesses_started, 2)

    def test_refuses_existing_output_path(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, execute_blackbox_cli
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "trial.py", _SUCCESS_SCRIPT)
            run_dir = root / "run"
            run_dir.mkdir()
            output_path = run_dir / "prediction.json"
            output_path.write_text("old", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fresh output"):
                execute_blackbox_cli(
                    script_path=script,
                    mode="predict",
                    input_path=write_request(_request("001"), root / "request.json"),
                    data_dir=_write_data_dir(root),
                    output_path=output_path,
                    profile=RuntimeProfile.for_tests(),
                )

    def test_excessive_process_logs_fail_without_output(self) -> None:
        from scheduler.blackbox_v2_runner import (
            BlackboxExecutionError,
            RuntimeProfile,
            execute_blackbox_cli,
        )
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "trial.py", _NOISY_SCRIPT)
            run_dir = root / "run"
            run_dir.mkdir()
            output_path = run_dir / "prediction.json"
            with self.assertRaisesRegex(BlackboxExecutionError, "log output exceeded"):
                execute_blackbox_cli(
                    script_path=script,
                    mode="predict",
                    input_path=write_request(_request("001"), root / "request.json"),
                    data_dir=_write_data_dir(root),
                    output_path=output_path,
                    profile=RuntimeProfile.for_tests(max_log_bytes=128),
                )
            self.assertFalse(output_path.exists())

    def test_refuses_symlinked_controlled_paths(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, execute_blackbox_cli
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "trial.py", _SUCCESS_SCRIPT)
            script_link = root / "trial-link.py"
            script_link.symlink_to(script)
            request = write_request(_request("001"), root / "request.json")
            request_link = root / "request-link.json"
            request_link.symlink_to(request)
            data_dir = _write_data_dir(root)
            data_link = root / "data-link"
            data_link.symlink_to(data_dir, target_is_directory=True)
            output_dir = root / "real-output"
            output_dir.mkdir()
            output_link = root / "output-link"
            output_link.symlink_to(output_dir, target_is_directory=True)

            cases = {
                "script": {"script_path": script_link},
                "request": {"input_path": request_link},
                "data-dir": {"data_dir": data_link},
                "output-parent": {"output_path": output_link / "prediction.json"},
            }
            base = {
                "script_path": script,
                "mode": "predict",
                "input_path": request,
                "data_dir": data_dir,
                "output_path": output_dir / "base.json",
                "profile": RuntimeProfile.for_tests(),
            }
            for index, (label, overrides) in enumerate(cases.items()):
                kwargs = {**base, **overrides}
                if "output_path" not in overrides:
                    kwargs["output_path"] = output_dir / f"{index}.json"
                with self.subTest(path=label):
                    with self.assertRaisesRegex(ValueError, "symlink"):
                        execute_blackbox_cli(**kwargs)

    def test_refuses_nonempty_or_control_overlapping_output_directory(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, execute_blackbox_cli
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "trial.py", _SUCCESS_SCRIPT)
            request = write_request(_request("001"), root / "request.json")
            data_dir = _write_data_dir(root)
            profile = RuntimeProfile.for_tests()

            with self.assertRaisesRegex(ValueError, "overlap"):
                execute_blackbox_cli(
                    script_path=script,
                    mode="predict",
                    input_path=request,
                    data_dir=data_dir,
                    output_path=root / "prediction.json",
                    profile=profile,
                )

            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / "stale.txt").write_text("stale", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fresh empty"):
                execute_blackbox_cli(
                    script_path=script,
                    mode="predict",
                    input_path=request,
                    data_dir=data_dir,
                    output_path=run_dir / "prediction.json",
                    profile=profile,
                )

    def test_invalid_output_directory_does_not_mask_process_error(self) -> None:
        from scheduler.blackbox_v2_runner import (
            BlackboxExecutionError,
            RuntimeProfile,
            execute_blackbox_cli,
        )
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "trial.py", _OUTPUT_DIRECTORY_FAILURE_SCRIPT)
            request = write_request(_request("001"), root / "request.json")
            run_dir = root / "run"
            run_dir.mkdir()
            with self.assertRaisesRegex(BlackboxExecutionError, "exited 2"):
                execute_blackbox_cli(
                    script_path=script,
                    mode="predict",
                    input_path=request,
                    data_dir=_write_data_dir(root),
                    output_path=run_dir / "prediction.json",
                    profile=RuntimeProfile.for_tests(),
                )
            self.assertFalse(run_dir.exists())

    def test_run_directory_quota_stops_sibling_and_oversized_files(self) -> None:
        from scheduler.blackbox_v2_runner import (
            BlackboxExecutionError,
            RuntimeProfile,
            execute_blackbox_cli,
        )
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            request = write_request(_request("001"), root / "request.json")
            data_dir = _write_data_dir(root)
            profile = RuntimeProfile.for_tests(
                max_output_bytes=64 * 1024,
                max_run_dir_bytes=128 * 1024,
            )
            for name, source in (
                ("siblings", _MANY_FILES_SCRIPT),
                ("oversized", _OVERSIZED_FILE_SCRIPT),
            ):
                script = _write_script(root / f"{name}.py", source)
                run_dir = root / f"run-{name}"
                run_dir.mkdir()
                started = time.monotonic()
                with self.subTest(case=name):
                    with self.assertRaisesRegex(BlackboxExecutionError, "run directory"):
                        execute_blackbox_cli(
                            script_path=script,
                            mode="predict",
                            input_path=request,
                            data_dir=data_dir,
                            output_path=run_dir / "prediction.json",
                            profile=profile,
                        )
                    self.assertLess(time.monotonic() - started, 5)
                    self.assertFalse(run_dir.exists())

    def test_run_directory_entry_quota_stops_empty_files_directories_and_symlinks(self) -> None:
        from scheduler.blackbox_v2_runner import (
            BlackboxExecutionError,
            RuntimeProfile,
            execute_blackbox_cli,
        )
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            request = write_request(_request("001"), root / "request.json")
            data_dir = _write_data_dir(root)
            profile = RuntimeProfile.for_tests(max_run_dir_entries=32)
            for name, source in (
                ("empty-files", _MANY_EMPTY_FILES_SCRIPT),
                ("directories-symlinks", _MANY_DIRECTORIES_AND_SYMLINKS_SCRIPT),
            ):
                script = _write_script(root / f"{name}.py", source)
                run_dir = root / f"run-{name}"
                run_dir.mkdir()
                started = time.monotonic()
                with self.subTest(case=name):
                    with self.assertRaisesRegex(BlackboxExecutionError, "entry limit"):
                        execute_blackbox_cli(
                            script_path=script,
                            mode="predict",
                            input_path=request,
                            data_dir=data_dir,
                            output_path=run_dir / "prediction.json",
                            profile=profile,
                        )
                    self.assertLess(time.monotonic() - started, 5)
                    self.assertFalse(run_dir.exists())

    def test_run_directory_quota_counts_allocated_blocks(self) -> None:
        from scheduler.blackbox_v2_runner import (
            BlackboxExecutionError,
            RuntimeProfile,
            execute_blackbox_cli,
        )
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "allocated.py", _SMALL_ALLOCATED_FILE_SCRIPT)
            request = write_request(_request("001"), root / "request.json")
            run_dir = root / "run"
            run_dir.mkdir()
            profile = RuntimeProfile.for_tests(
                max_output_bytes=512,
                max_run_dir_bytes=1024,
            )
            with self.assertRaisesRegex(BlackboxExecutionError, "allocated byte limit"):
                execute_blackbox_cli(
                    script_path=script,
                    mode="predict",
                    input_path=request,
                    data_dir=_write_data_dir(root),
                    output_path=run_dir / "prediction.json",
                    profile=profile,
                )
            self.assertFalse(run_dir.exists())

    @unittest.skipUnless(
        hasattr(os, "chflags") and hasattr(stat, "UF_IMMUTABLE"),
        "requires user immutable file flags",
    )
    def test_cleanup_clears_immutable_output_without_residue(self) -> None:
        from scheduler.blackbox_v2_runner import (
            BlackboxExecutionError,
            RuntimeProfile,
            execute_blackbox_cli,
        )
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "immutable.py", _IMMUTABLE_FAILURE_SCRIPT)
            request = write_request(_request("001"), root / "request.json")
            run_dir = root / "run"
            run_dir.mkdir()
            output = run_dir / "prediction.json"
            try:
                with self.assertRaisesRegex(BlackboxExecutionError, "exited 2"):
                    execute_blackbox_cli(
                        script_path=script,
                        mode="predict",
                        input_path=request,
                        data_dir=_write_data_dir(root),
                        output_path=output,
                        profile=RuntimeProfile.for_tests(),
                    )
                self.assertFalse(run_dir.exists())
            finally:
                if output.exists():
                    os.chflags(output, 0, follow_symlinks=False)
                    shutil.rmtree(run_dir)

    def test_cleanup_failure_preserves_execution_context(self) -> None:
        from scheduler.blackbox_v2_runner import (
            BlackboxExecutionError,
            RuntimeProfile,
            execute_blackbox_cli,
        )
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "trial.py", _FAIL_AFTER_OUTPUT_SCRIPT)
            request = write_request(_request("001"), root / "request.json")
            run_dir = root / "run"
            run_dir.mkdir()
            with patch(
                "scheduler.blackbox_v2_runner._cleanup_run_directory",
                side_effect=OSError("cleanup-denied"),
            ):
                with self.assertRaisesRegex(
                    BlackboxExecutionError,
                    "exited 2.*cleanup failed.*cleanup-denied",
                ):
                    execute_blackbox_cli(
                        script_path=script,
                        mode="predict",
                        input_path=request,
                        data_dir=_write_data_dir(root),
                        output_path=run_dir / "prediction.json",
                        profile=RuntimeProfile.for_tests(),
                    )

    def test_cleanup_failure_does_not_mask_process_group_cleanup_error(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import (
            RuntimeProfile,
            execute_blackbox_cli,
        )
        from scheduler.process_control import (
            ProcessGroupTerminationResult,
            ProcessRegistrationCleanupError,
        )
        from shared.blackbox_v2.requests import write_request

        process_error = ProcessRegistrationCleanupError(
            registration_error=RuntimeError(
                "process registration failed"
            ),
            termination=ProcessGroupTerminationResult(
                process_id=22334,
                process_group_id=33445,
                term_sent=True,
                kill_sent=True,
                confirmed_gone=False,
                failure_reason=(
                    "process group still exists after SIGKILL"
                ),
            ),
        )
        cleanup_error = OSError("cleanup-denied")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(
                root / "trial.py",
                _SUCCESS_SCRIPT,
            )
            request = write_request(
                _request("001"),
                root / "request.json",
            )
            run_dir = root / "run"
            run_dir.mkdir()
            with (
                patch(
                    "scheduler.blackbox_v2_runner._run_process",
                    side_effect=process_error,
                ),
                patch(
                    "scheduler.blackbox_v2_runner."
                    "_cleanup_run_directory",
                    side_effect=cleanup_error,
                ),
            ):
                with self.assertRaises(
                    ProcessRegistrationCleanupError
                ) as raised:
                    execute_blackbox_cli(
                        script_path=script,
                        mode="predict",
                        input_path=request,
                        data_dir=_write_data_dir(root),
                        output_path=(
                            run_dir / "prediction.json"
                        ),
                        profile=RuntimeProfile.for_tests(),
                    )

        self.assertIs(raised.exception, process_error)
        self.assertIs(raised.exception.__cause__, cleanup_error)
        self.assertTrue(
            any(
                "run directory cleanup failed" in note
                and "cleanup-denied" in note
                for note in getattr(
                    raised.exception,
                    "__notes__",
                    (),
                )
            )
        )

    def test_process_launcher_uses_bootstrap_without_preexec_or_shell(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, execute_blackbox_cli
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "rlimit.py", _RLIMIT_PROBE_SCRIPT)
            request = write_request(_request("001"), root / "request.json")
            run_dir = root / "run"
            run_dir.mkdir()
            output = run_dir / "result.json"
            real_popen = subprocess.Popen
            with patch(
                "scheduler.blackbox_v2_runner.subprocess.Popen",
                wraps=real_popen,
            ) as popen:
                execute_blackbox_cli(
                    script_path=script,
                    mode="predict",
                    input_path=request,
                    data_dir=_write_data_dir(root),
                    output_path=output,
                    profile=RuntimeProfile.for_tests(
                        max_output_bytes=32 * 1024,
                        max_run_dir_bytes=64 * 1024,
                    ),
                )
            launcher_calls = [
                call
                for call in popen.call_args_list
                if call.kwargs.get("start_new_session") is True
            ]
            self.assertEqual(len(launcher_calls), 1)
            self.assertNotIn("preexec_fn", launcher_calls[0].kwargs)
            self.assertNotIn("shell", launcher_calls[0].kwargs)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"rlimit": True})

    @unittest.skipUnless(shutil.which("sandbox-exec"), "requires macOS sandbox-exec")
    def test_macos_sandbox_denies_control_writes_root_listing_hardlinks_and_children(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, execute_blackbox_cli
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "control-probe.py", _CONTROL_PROBE_SCRIPT)
            request = write_request(_request("001"), root / "request.json")
            data_dir = _write_data_dir(root)
            script_before = script.read_bytes()
            request_before = request.read_bytes()
            data_before = {
                path.name: path.read_bytes()
                for path in data_dir.iterdir()
            }
            run_dir = root / "run"
            run_dir.mkdir()
            output = run_dir / "result.json"
            execute_blackbox_cli(
                script_path=script,
                mode="predict",
                input_path=request,
                data_dir=data_dir,
                output_path=output,
                profile=RuntimeProfile.for_tests(
                    conda_env=None,
                    sandbox_enabled=True,
                    cpu_threads=1,
                ),
            )
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                result,
                {
                    "child_execution_denied": True,
                    "data_write_denied": True,
                    "hardlink_denied": True,
                    "request_write_denied": True,
                    "root_listing_denied": True,
                    "script_write_denied": True,
                },
            )
            self.assertEqual(script.read_bytes(), script_before)
            self.assertEqual(request.read_bytes(), request_before)
            self.assertEqual(
                {path.name: path.read_bytes() for path in data_dir.iterdir()},
                data_before,
            )

    def test_sandbox_quote_rejects_control_characters_and_escapes_literals(self) -> None:
        from scheduler.blackbox_v2_runner import _sandbox_quote

        self.assertEqual(_sandbox_quote(Path('/tmp/a"b\\c')), '/tmp/a\\"b\\\\c')
        for value in ("/tmp/a\nb", "/tmp/a\rb", "/tmp/a\0b"):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(ValueError, "control character"):
                    _sandbox_quote(Path(value))

    @unittest.skipUnless(shutil.which("sandbox-exec"), "requires macOS sandbox-exec")
    def test_macos_sandbox_enforces_runtime_boundaries(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, execute_blackbox_cli
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script_dir = root / "delivery"
            script_dir.mkdir()
            script = _write_script(script_dir / "probe.py", _SANDBOX_PROBE_SCRIPT)
            request = write_request(_request("001"), root / "request.json")
            data_dir = _write_data_dir(root)
            secret_dir = root / "external"
            secret_dir.mkdir()
            secret = secret_dir / "secret.txt"
            secret.write_text("external-secret", encoding="utf-8")
            run_dir = root / "run"
            run_dir.mkdir()
            output = run_dir / "probe.json"

            with patch.dict(os.environ, {"BLACKBOX_TEST_SECRET": "parent-secret"}):
                execute_blackbox_cli(
                    script_path=script,
                    mode="predict",
                    input_path=request,
                    data_dir=data_dir,
                    output_path=output,
                    profile=RuntimeProfile.for_tests(
                        conda_env=None,
                        sandbox_enabled=True,
                        cpu_threads=1,
                    ),
                )

            result = json.loads(output.read_text(encoding="utf-8"))

        self.assertTrue(result["allowed_csv_read"])
        self.assertTrue(result["hosts_read_denied"])
        self.assertTrue(result["external_secret_read_denied"])
        self.assertTrue(result["inherited_secret_absent"])
        self.assertTrue(result["network_denied"])
        self.assertTrue(result["data_write_denied"])
        for result_key, path in _UNDECLARED_SYSTEM_READ_PROBES.items():
            with self.subTest(undeclared_read=str(path)):
                if path.is_file():
                    self.assertTrue(
                        result[result_key],
                        f"sandbox read unexpectedly allowed: {path}",
                    )

    @unittest.skipUnless(shutil.which("sandbox-exec"), "requires macOS sandbox-exec")
    def test_macos_sandbox_runs_frozen_scientific_environment(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, execute_blackbox_cli
        from shared.blackbox_v2.requests import write_request

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script_dir = root / "delivery"
            script_dir.mkdir()
            script = _write_script(script_dir / "scientific.py", _SCIENTIFIC_PROBE_SCRIPT)
            request = write_request(_request("001"), root / "request.json")
            run_dir = root / "run"
            run_dir.mkdir()
            output = run_dir / "scientific.json"
            execute_blackbox_cli(
                script_path=script,
                mode="predict",
                input_path=request,
                data_dir=_write_data_dir(root),
                output_path=output,
                profile=RuntimeProfile.for_tests(
                    conda_env="forecast_env_blackbox_v1",
                    sandbox_enabled=True,
                    cpu_threads=1,
                ),
            )
            result = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, {"features": 1, "rows": 1})

    @unittest.skipUnless(shutil.which("sandbox-exec"), "requires macOS sandbox-exec")
    def test_macos_sandbox_runs_frozen_scientific_backtest(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, execute_blackbox_cli
        from shared.blackbox_v2.requests import write_requests

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "scientific-backtest.py", _SCIENTIFIC_BACKTEST_SCRIPT)
            requests = write_requests(
                [_request("001"), _request("002")],
                root / "requests.csv",
            )
            run_dir = root / "run"
            run_dir.mkdir()
            output = run_dir / "backtest.csv"
            execute_blackbox_cli(
                script_path=script,
                mode="backtest",
                input_path=requests,
                data_dir=_write_data_dir(root),
                output_path=output,
                profile=RuntimeProfile.for_tests(
                    conda_env="forecast_env_blackbox_v1",
                    sandbox_enabled=True,
                    cpu_threads=1,
                ),
            )
            rows = output.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(rows), 3)

    def test_gray_replay_batch_reuses_one_session_and_preserves_requests(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile
        from scheduler.executor import run_blackbox_gray_replay_batch
        from shared.models import PredictionRecord

        session = _gray_replay_session()
        requests = [
            _gray_replay_request("gray-001", "2026-07-24", "202630"),
            _gray_replay_request("gray-002", "2026-07-31", "202631"),
        ]
        cfg = SimpleNamespace(
            scheme_id="blackbox_trial",
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
            frequency="daily",
            delivery_script=Path("trial.py"),
            delivery_metadata=Path("trial.json"),
            platform_inputs=(),
        )
        metadata = _metadata()
        raw_records = [
            PredictionRecord(
                scheme_id=metadata.scheme_id,
                target_tenor=metadata.target_tenor,
                horizon=metadata.horizon,
                predict_date=request.predict_date,
                feature_date=request.feature_date,
                target_date=request.target_date,
                predicted_direction=1,
                extra={"request_id": request.request_id},
            )
            for request in requests
        ]

        @contextmanager
        def open_runtime_view(bundle):
            yield SimpleNamespace(
                data_dir=Path("/tmp/gray-replay-runtime-view"),
                bundle=bundle,
            )

        with (
            patch("scheduler.executor.load_metadata", return_value=metadata),
            patch(
                "scheduler.executor.open_blackbox_runtime_view",
                side_effect=open_runtime_view,
            ) as runtime_view,
            patch(
                "scheduler.executor.run_blackbox_backtest",
                return_value=raw_records,
            ) as backtest,
        ):
            records = run_blackbox_gray_replay_batch(
                cfg,
                requests=requests,
                session=session,
                engine=object(),
                algo_env="forecast_env",
                timeout_sec=600,
                profile=RuntimeProfile.for_tests(),
            )

        runtime_view.assert_called_once()
        backtest.assert_called_once()
        self.assertEqual(
            backtest.call_args.kwargs["requests"],
            requests,
        )
        self.assertEqual(
            [
                (record.predict_date, record.feature_date, record.target_date)
                for record in records
            ],
            [
                (request.predict_date, request.feature_date, request.target_date)
                for request in requests
            ],
        )
        self.assertTrue(
            all(
                record.extra["gray_replay_session_id"] == "a" * 64
                for record in records
            )
        )
        self.assertTrue(
            all(
                record.extra["parent_data_snapshot_id"]
                == session.snapshot.snapshot_id
                for record in records
            )
        )

    def test_gray_replay_batch_keeps_one_runtime_view_above_contract_cap(
        self,
    ) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile
        from scheduler.executor import run_blackbox_gray_replay_batch
        from shared.models import PredictionRecord

        session = _gray_replay_session()
        requests = [
            _gray_replay_request(
                f"gray-{index:03d}",
                "2026-07-31",
                "202631",
            )
            for index in range(205)
        ]
        cfg = SimpleNamespace(
            scheme_id="blackbox_trial",
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
            frequency="daily",
            delivery_script=Path("trial.py"),
            delivery_metadata=Path("trial.json"),
            platform_inputs=(),
        )

        @contextmanager
        def open_runtime_view(bundle):
            yield SimpleNamespace(
                data_dir=Path("/tmp/gray-replay-runtime-view"),
                bundle=bundle,
            )

        records = [
            PredictionRecord(
                scheme_id="blackbox_trial",
                target_tenor="10Y",
                horizon=1,
                predict_date=request.predict_date,
                feature_date=request.feature_date,
                target_date=request.target_date,
                predicted_direction=1,
                extra={"request_id": request.request_id},
            )
            for request in requests
        ]
        with (
            patch("scheduler.executor.load_metadata", return_value=_metadata()),
            patch(
                "scheduler.executor.open_blackbox_runtime_view",
                side_effect=open_runtime_view,
            ) as runtime_view,
            patch(
                "scheduler.executor.run_blackbox_backtest",
                return_value=records,
            ) as backtest,
        ):
            result = run_blackbox_gray_replay_batch(
                cfg,
                requests=requests,
                session=session,
                engine=object(),
                algo_env="forecast_env",
                timeout_sec=600,
                profile=RuntimeProfile.for_tests(max_batch_requests=100),
            )

        runtime_view.assert_called_once()
        backtest.assert_called_once()
        self.assertEqual(
            backtest.call_args.kwargs["profile"].max_batch_requests,
            100,
        )
        self.assertEqual(len(result), 205)

    def test_gray_replay_batch_rejects_request_after_session_cutoff(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile
        from scheduler.executor import run_blackbox_gray_replay_batch

        cfg = SimpleNamespace(
            scheme_id="blackbox_trial",
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
            frequency="daily",
            delivery_script=Path("trial.py"),
            delivery_metadata=Path("trial.json"),
            platform_inputs=(),
        )
        with (
            patch("scheduler.executor.load_metadata", return_value=_metadata()),
            patch("scheduler.executor.open_blackbox_runtime_view") as runtime_view,
            patch("scheduler.executor.run_blackbox_backtest") as backtest,
            self.assertRaisesRegex(ValueError, "exceeds gray replay session"),
        ):
            run_blackbox_gray_replay_batch(
                cfg,
                requests=[
                    _gray_replay_request(
                        "gray-after-session",
                        "2026-08-06",
                        "202632",
                    ),
                ],
                session=_gray_replay_session(),
                engine=object(),
                algo_env="forecast_env",
                timeout_sec=600,
                profile=RuntimeProfile.for_tests(),
            )

        runtime_view.assert_not_called()
        backtest.assert_not_called()


def _metadata() -> BlackboxMetadata:
    return BlackboxMetadata(
        schema_version="1.0",
        scheme_id="blackbox_trial",
        name="Trial",
        algorithm_version="1.0.0",
        target_tenor="10Y",
        task_type="T+1",
        horizon=1,
        target_rule="target_date_yield_vs_feature_date_yield",
        frequency="daily",
    )


def _request(request_id: str) -> BlackboxRequest:
    return BlackboxRequest(
        request_id=request_id,
        predict_date="2026-07-15",
        feature_date="2026-07-15",
        target_date="2026-07-16",
        daily_cutoff_key="2026-07-15",
        weekly_cutoff_key="202627",
        monthly_cutoff_key="202606",
    )


def _gray_replay_session():
    from shared.blackbox_v2.snapshot import BlackboxSnapshot, CutoffKeys
    from shared.input_artifacts import BlackboxGrayReplaySession

    session_id = "a" * 64
    snapshot = BlackboxSnapshot(
        snapshot_id="snapshot-gray-parent",
        root_dir=Path("/tmp/gray-replay-parent"),
        data_dir=Path("/tmp/gray-replay-parent/data"),
        manifest_path=Path("/tmp/gray-replay-parent/manifest.json"),
        schema_version="data-bridge-v1",
        generation_id="gray-replay-generation-1",
        refresh_date="2026-08-06",
    )
    return BlackboxGrayReplaySession(
        snapshot=snapshot,
        manifest_path=Path("/tmp") / session_id / "manifest.json",
        manifest_sha256="b" * 64,
        source_identity={},
        max_cutoffs=CutoffKeys("2026-07-31", "202631", "202607"),
    )


def _gray_replay_request(
    request_id: str,
    daily_cutoff_key: str,
    weekly_cutoff_key: str,
) -> BlackboxRequest:
    return BlackboxRequest(
        request_id=request_id,
        predict_date="2026-08-01",
        feature_date="2026-07-31",
        target_date="2026-08-07",
        daily_cutoff_key=daily_cutoff_key,
        weekly_cutoff_key=weekly_cutoff_key,
        monthly_cutoff_key="202607",
    )


def _write_data_dir(root: Path) -> Path:
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    for name in ("daily_output.csv", "weekly_output.csv", "monthly_output.csv"):
        (data_dir / name).write_text("key,value\n1,1\n", encoding="utf-8")
    return data_dir


def _write_script(path: Path, source: str) -> Path:
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def _platform_bundle_evidence():
    import pandas as pd

    from shared.blackbox_v2.platform_inputs import (
        freeze_platform_input,
    )
    from shared.blackbox_v2.snapshot import (
        BlackboxSnapshot,
        compose_blackbox_input_bundle,
    )

    parent_snapshot = BlackboxSnapshot(
        snapshot_id="snapshot-parent",
        root_dir=Path("/tmp/snapshot-parent"),
        data_dir=Path("/tmp/snapshot-parent/data"),
        manifest_path=Path("/tmp/snapshot-parent/manifest.json"),
        schema_version="data-bridge-v1",
    )
    artifact = freeze_platform_input(
        "api-wind-date-v1",
        pd.DataFrame(
            {
                "rdate": ["2026-07-15"],
                "week_id": ["202627"],
            }
        ),
        weekly_cutoff_key="202627",
        audit_provenance={
            "source_kind": "harness_database",
            "generation_id": None,
            "manifest_sha256": None,
            "captured_at": "2026-07-24T00:00:00+00:00",
        },
    )
    return compose_blackbox_input_bundle(
        parent_snapshot,
        platform_input_ids=("api-wind-date-v1",),
        platform_input_artifacts=(artifact,),
    )


_SUCCESS_SCRIPT = r'''
import argparse
import csv
import json

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=("predict", "backtest"))
parser.add_argument("--request")
parser.add_argument("--requests")
parser.add_argument("--data-dir", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
fields = ["request_id", "predict_date", "feature_date", "target_date"]
if args.mode == "predict":
    with open(args.request, encoding="utf-8") as handle:
        request = json.load(handle)
    result = {field: request[field] for field in fields}
    result["predicted_direction"] = 1
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle)
else:
    with open(args.requests, encoding="utf-8", newline="") as handle:
        requests = list(csv.DictReader(handle))
    with open(args.output, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields + ["predicted_direction"])
        writer.writeheader()
        for request in requests:
            result = {field: request[field] for field in fields}
            result["predicted_direction"] = 1
            writer.writerow(result)
'''


_EXECUTION_TOKEN_SUCCESS_SCRIPT = r'''
import argparse
import json
import os

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=("predict",))
parser.add_argument("--request", required=True)
parser.add_argument("--data-dir", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
if os.environ.get("BOND_SCHEDULE_EXECUTION_TOKEN") != "scheduled-token_123":
    raise SystemExit(3)
with open(args.request, encoding="utf-8") as handle:
    request = json.load(handle)
result = {
    field: request[field]
    for field in ("request_id", "predict_date", "feature_date", "target_date")
}
result["predicted_direction"] = 1
with open(args.output, "w", encoding="utf-8") as handle:
    json.dump(result, handle)
'''


_EXECUTION_TOKEN_ABSENT_SCRIPT = r'''
import argparse
import json
import os

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=("predict",))
parser.add_argument("--request", required=True)
parser.add_argument("--data-dir", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
if "BOND_SCHEDULE_EXECUTION_TOKEN" in os.environ:
    raise SystemExit(3)
with open(args.request, encoding="utf-8") as handle:
    request = json.load(handle)
result = {
    field: request[field]
    for field in ("request_id", "predict_date", "feature_date", "target_date")
}
result["predicted_direction"] = 1
with open(args.output, "w", encoding="utf-8") as handle:
    json.dump(result, handle)
'''


_SLOW_BACKTEST_SCRIPT = r'''
import argparse
import time

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--requests")
parser.add_argument("--data-dir")
parser.add_argument("--output")
parser.parse_args()
time.sleep(5)
'''


_FAIL_AFTER_OUTPUT_SCRIPT = r'''
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--data-dir")
parser.add_argument("--output")
args = parser.parse_args()
with open(args.output, "w", encoding="utf-8") as handle:
    handle.write("{}")
raise SystemExit(2)
'''


_NOISY_SCRIPT = r'''
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--data-dir")
parser.add_argument("--output")
args = parser.parse_args()
print("x" * 4096)
with open(args.output, "w", encoding="utf-8") as handle:
    handle.write("{}")
'''


_OUTPUT_DIRECTORY_FAILURE_SCRIPT = r'''
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--data-dir")
parser.add_argument("--output")
args = parser.parse_args()
Path(args.output).mkdir()
raise SystemExit(2)
'''


_MANY_FILES_SCRIPT = r'''
import argparse
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--data-dir")
parser.add_argument("--output")
args = parser.parse_args()
root = Path(args.output).parent
for index in range(32):
    (root / f"sibling-{index:02d}.bin").write_bytes(b"x" * 16384)
time.sleep(10)
'''


_OVERSIZED_FILE_SCRIPT = r'''
import argparse
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--data-dir")
parser.add_argument("--output")
args = parser.parse_args()
try:
    (Path(args.output).parent / "oversized.bin").write_bytes(b"x" * 1048576)
except OSError:
    pass
time.sleep(10)
'''


_MANY_EMPTY_FILES_SCRIPT = r'''
import argparse
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--data-dir")
parser.add_argument("--output")
args = parser.parse_args()
root = Path(args.output).parent
for index in range(3000):
    (root / f"empty-{index:04d}").touch()
time.sleep(10)
'''


_MANY_DIRECTORIES_AND_SYMLINKS_SCRIPT = r'''
import argparse
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--data-dir")
parser.add_argument("--output")
args = parser.parse_args()
root = Path(args.output).parent
for index in range(500):
    directory = root / f"directory-{index:04d}"
    directory.mkdir()
    (root / f"symlink-{index:04d}").symlink_to(directory.name, target_is_directory=True)
time.sleep(10)
'''


_SMALL_ALLOCATED_FILE_SCRIPT = r'''
import argparse
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--data-dir")
parser.add_argument("--output")
args = parser.parse_args()
(Path(args.output).parent / "allocated.bin").write_bytes(b"x")
time.sleep(10)
'''


_IMMUTABLE_FAILURE_SCRIPT = r'''
import argparse
import os
import stat
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--data-dir")
parser.add_argument("--output")
args = parser.parse_args()
output = Path(args.output)
output.write_text("{}", encoding="utf-8")
os.chflags(output, stat.UF_IMMUTABLE, follow_symlinks=False)
raise SystemExit(2)
'''


_RLIMIT_PROBE_SCRIPT = r'''
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--data-dir")
parser.add_argument("--output")
args = parser.parse_args()
probe = Path(args.output).parent / "rlimit.bin"
try:
    probe.write_bytes(b"x" * 1048576)
    limited = False
except OSError:
    limited = True
finally:
    probe.unlink(missing_ok=True)
Path(args.output).write_text(json.dumps({"rlimit": limited}), encoding="utf-8")
'''


_CONTROL_PROBE_SCRIPT = r'''
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--data-dir")
parser.add_argument("--output")
args = parser.parse_args()

def write_is_denied(path):
    try:
        Path(path).write_text("overwritten", encoding="utf-8")
        return False
    except OSError:
        return True

result = {
    "script_write_denied": write_is_denied(__file__),
    "request_write_denied": write_is_denied(args.request),
    "data_write_denied": write_is_denied(Path(args.data_dir) / "daily_output.csv"),
}
try:
    hardlink = Path(args.output).parent / "request-hardlink"
    os.link(args.request, hardlink)
    result["hardlink_denied"] = write_is_denied(hardlink)
except OSError:
    result["hardlink_denied"] = True
try:
    os.listdir("/")
    result["root_listing_denied"] = False
except OSError:
    result["root_listing_denied"] = True
try:
    subprocess.run(
        [sys.executable, "-c", "print('child')"],
        check=True,
        capture_output=True,
        text=True,
    )
    result["child_execution_denied"] = False
except (OSError, subprocess.SubprocessError):
    result["child_execution_denied"] = True
Path(args.output).write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
'''


_SANDBOX_PROBE_SCRIPT = r'''
import argparse
import errno
import json
import os
import socket
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--data-dir", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()

request = json.loads(Path(args.request).read_text(encoding="utf-8"))
data_dir = Path(args.data_dir)
external_secret = data_dir.parent / "external" / "secret.txt"

try:
    allowed_csv_read = "key,value" in (data_dir / "daily_output.csv").read_text(
        encoding="utf-8"
    )
except OSError:
    allowed_csv_read = False

try:
    Path("/etc/hosts").read_text(encoding="utf-8")
    hosts_read_denied = False
except OSError:
    hosts_read_denied = True

try:
    external_secret.read_text(encoding="utf-8")
    external_secret_read_denied = False
except OSError:
    external_secret_read_denied = True

undeclared_system_reads = {}
for result_key, path in {
    "passwd_read_denied": "/private/etc/passwd",
    "language_assets_read_denied": (
        "/usr/share/com.apple.languageassetd/_CodeSignature/CodeResources"
    ),
    "calculator_info_read_denied": (
        "/System/Applications/Calculator.app/Contents/Info.plist"
    ),
}.items():
    try:
        Path(path).read_bytes()
        undeclared_system_reads[result_key] = False
    except OSError:
        undeclared_system_reads[result_key] = True

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.bind(("127.0.0.1", 0))
    network_denied = False
except OSError as exc:
    network_denied = exc.errno in {errno.EPERM, errno.EACCES}
finally:
    sock.close()

try:
    (data_dir / "forbidden.txt").write_text("forbidden", encoding="utf-8")
    data_write_denied = False
except OSError:
    data_write_denied = True

result = {
    "allowed_csv_read": allowed_csv_read and request["request_id"] == "001",
    "hosts_read_denied": hosts_read_denied,
    "external_secret_read_denied": external_secret_read_denied,
    "inherited_secret_absent": "BLACKBOX_TEST_SECRET" not in os.environ,
    "network_denied": network_denied,
    "data_write_denied": data_write_denied,
    **undeclared_system_reads,
}
Path(args.output).write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
'''


_SCIENTIFIC_PROBE_SCRIPT = r'''
import argparse
import json
from pathlib import Path

import lightgbm
import numpy
import pandas

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--data-dir", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()

json.loads(Path(args.request).read_text(encoding="utf-8"))
frame = pandas.read_csv(Path(args.data_dir) / "daily_output.csv")
dataset = lightgbm.Dataset(
    numpy.array([[0.0], [1.0]], dtype=float),
    label=numpy.array([0, 1]),
    params={"verbose": -1, "min_data_in_bin": 1, "min_data_in_leaf": 1},
).construct()
result = {"features": dataset.num_feature(), "rows": len(frame)}
Path(args.output).write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
'''


_SCIENTIFIC_BACKTEST_SCRIPT = r'''
import argparse
from pathlib import Path

import lightgbm
import numpy
import pandas

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--requests")
parser.add_argument("--data-dir", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()

requests = pandas.read_csv(args.requests)
pandas.read_csv(Path(args.data_dir) / "daily_output.csv")
lightgbm.Dataset(
    numpy.array([[0.0], [1.0]], dtype=float),
    label=numpy.array([0, 1]),
    params={"verbose": -1, "min_data_in_bin": 1, "min_data_in_leaf": 1},
).construct()
requests["predicted_direction"] = 1
requests[[
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
]].to_csv(args.output, index=False)
'''


if __name__ == "__main__":
    unittest.main()
