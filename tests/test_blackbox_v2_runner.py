from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    def test_runtime_environment_does_not_inherit_parent_secrets(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, _runtime_environment

        inherited = {
            "LANG": "zh_CN.UTF-8",
            "BOND_DB_PASSWORD": "db-secret",
            "DATABRIDGE_API_PASSWORD": "bridge-secret",
            "HARNESS_AUTH_SECRET": "auth-secret",
            "AWS_SECRET_ACCESS_KEY": "cloud-secret",
            "HTTPS_PROXY": "http://proxy.invalid",
            "BLACKBOX_TEST_SECRET": "user-secret",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            with patch.dict(os.environ, inherited, clear=True):
                env = _runtime_environment(RuntimeProfile.for_tests(), run_dir)

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

    def test_scheduled_blackbox_uses_fresh_temporary_current_snapshot(self) -> None:
        from scheduler.executor import run_blackbox_scheme_subprocess
        from shared.blackbox_v2.snapshot import BlackboxSnapshot, CutoffKeys

        events: list[str] = []

        @contextmanager
        def open_snapshot(**kwargs):
            self.assertEqual(kwargs["snapshot_date"], "2026-07-16")
            self.assertTrue(kwargs["require_fresh"])
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
        with patch("scheduler.executor.load_metadata", return_value=_metadata()):
            with patch("scheduler.executor.open_blackbox_input_snapshot", side_effect=open_snapshot):
                with patch("scheduler.executor.get_calendar", return_value="calendar"):
                    with patch(
                        "scheduler.executor.build_daily_live_context",
                        return_value=SimpleNamespace(feature_date="2026-07-15"),
                    ):
                        with patch(
                            "scheduler.executor.resolve_blackbox_input_cutoffs",
                            return_value=CutoffKeys("2026-07-15", "202627", "202606"),
                        ):
                            with patch("scheduler.executor.build_live_request", return_value=_request("001")):
                                with patch(
                                    "scheduler.blackbox_v2_runner.run_blackbox_predict",
                                    return_value="record",
                                ):
                                    result = run_blackbox_scheme_subprocess(
                                        cfg,
                                        "2026-07-16",
                                        engine="engine",
                                        algo_env="forecast_env_blackbox_v1",
                                        timeout_sec=3600,
                                    )

        self.assertEqual(result, ["record"])
        self.assertEqual(events, ["opened", "closed"])

    def test_predict_converts_valid_result_to_prediction_record(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, run_blackbox_predict

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "trial.py", _SUCCESS_SCRIPT)
            data_dir = _write_data_dir(root)
            record = run_blackbox_predict(
                metadata=_metadata(),
                script_path=script,
                request=_request("001"),
                data_dir=data_dir,
                data_snapshot_id="snapshot-test",
                profile=RuntimeProfile.for_tests(),
            )

        self.assertEqual(record.scheme_id, "blackbox_trial")
        self.assertEqual(record.target_tenor, "10Y")
        self.assertEqual(record.predicted_direction, 1)
        self.assertEqual(record.extra["runtime_type"], "blackbox_v2")
        self.assertEqual(record.extra["request_id"], "001")
        self.assertEqual(record.extra["data_snapshot_id"], "snapshot-test")

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


def _write_data_dir(root: Path) -> Path:
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    for name in ("daily_output.csv", "weekly_output.csv", "monthly_output.csv"):
        (data_dir / name).write_text("key,value\n1,1\n", encoding="utf-8")
    return data_dir


def _write_script(path: Path, source: str) -> Path:
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


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
