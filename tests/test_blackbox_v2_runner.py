from __future__ import annotations

import tempfile
import textwrap
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shared.blackbox_v2.contracts import BlackboxMetadata, BlackboxRequest


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


    def test_runner_rejects_missing_declared_platform_input(self) -> None:
        from scheduler.blackbox_v2_runner import _validate_data_dir

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = _write_data_dir(Path(tmpdir))

            with self.assertRaisesRegex(ValueError, "exactly"):
                _validate_data_dir(
                    data_dir,
                    platform_input_ids=("api-wind-date-v1",),
                )







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
            3600,
        )
        self.assertEqual(
            predict.call_args.kwargs["timeout_sec"],
            300,
        )
        self.assertIs(
            predict.call_args.kwargs["process_start_guard"],
            process_start_guard,
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


_DATA_DIR_MUTATION_SCRIPT = r'''
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--data-dir", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()

# 故意改写平台输入，用来验证运行后指纹复验会判定本次执行不可信。
target = Path(args.data_dir) / "daily_output.csv"
target.write_text(target.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
Path(args.output).write_text(json.dumps({"direction": 1}), encoding="utf-8")
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


_RUNTIME_ENV_PROBE_SCRIPT = r'''
import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--data-dir", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()

data_dir = Path(args.data_dir)
try:
    allowed_csv_read = "key,value" in (data_dir / "daily_output.csv").read_text(
        encoding="utf-8"
    )
except OSError:
    allowed_csv_read = False

result = {
    "allowed_csv_read": allowed_csv_read,
    "inherited_secret_absent": os.environ.get("BLACKBOX_TEST_SECRET") is None,
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
