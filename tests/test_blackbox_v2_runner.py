from __future__ import annotations

import tempfile
import textwrap
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shared.blackbox_v2.contracts import BlackboxMetadata, BlackboxRequest


class BlackboxV2RunnerTests(unittest.TestCase):
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
            output_path = root / "prediction.json"
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
            output_path = root / "prediction.json"
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
            output_path = root / "prediction.json"
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


if __name__ == "__main__":
    unittest.main()
