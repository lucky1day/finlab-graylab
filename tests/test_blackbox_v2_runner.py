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
    def test_gray_replay_rejects_request_calendar_mismatch(self) -> None:
        from scheduler.executor import (
            _validate_gray_replay_request_within_session,
        )

        with self.assertRaisesRegex(ValueError, "frozen calendar"):
            _validate_gray_replay_request_within_session(
                _gray_replay_request(
                    "gray-calendar-mismatch",
                    "2026-07-24",
                    "202631",
                ),
                _gray_replay_session(),
            )

    def test_runner_requires_exact_four_standard_files(self) -> None:
        from scheduler.blackbox_v2_runner import _validate_data_dir

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = _write_data_dir(root)
            _validate_data_dir(data_dir)

            (data_dir / "api_wind_date.csv").unlink()
            with self.assertRaisesRegex(ValueError, "exactly"):
                _validate_data_dir(data_dir)
            (data_dir / "api_wind_date.csv").write_text(
                "rdate,week_id\n2026-07-24,202629\n",
                encoding="utf-8",
            )

            (data_dir / "unexpected.csv").write_text(
                "x\n1\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "exactly"):
                _validate_data_dir(data_dir)


    def test_scheduled_blackbox_uses_ready_snapshot(self) -> None:
        from scheduler.executor import run_blackbox_scheme_subprocess
        from scheduler.process_control import ProcessStartGuard
        from shared.blackbox_v2.snapshot import BlackboxSnapshot, CutoffKeys

        snapshot = BlackboxSnapshot(
            snapshot_id="snapshot-current",
            root_dir=Path("/tmp/snapshot-current"),
            data_dir=Path("/tmp/snapshot-current/data"),
            manifest_path=Path("/tmp/snapshot-current/manifest.json"),
            schema_version="data-bridge-v1",
        )

        cfg = SimpleNamespace(
            scheme_id="blackbox_trial",
            input_source="data_bridge_current",
            delivery_script=Path("trial.py"),
            delivery_metadata=Path("trial.json"),
        )

        def process_started(_pid: int, _pgid: int) -> None:
            return None

        process_start_guard = ProcessStartGuard()
        trusted_bundle = SimpleNamespace(
            combined_snapshot_id="snapshot-trusted",
            parent_snapshot_id="snapshot-parent-trusted",
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
                "scheduler.executor.get_ready_blackbox_snapshot",
                return_value=snapshot,
            ) as ready_snapshot,
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
                process_started=process_started,
                process_start_guard=process_start_guard,
            )

        self.assertEqual(result, ["record"])
        ready_snapshot.assert_called_once_with(
            snapshot_date="2026-07-16",
            require_fresh=True,
        )
        self.assertIs(
            predict.call_args.kwargs["process_started"],
            process_started,
        )
        self.assertEqual(
            predict.call_args.kwargs["data_dir"],
            Path("/tmp/private-runtime-view"),
        )
        self.assertEqual(
            predict.call_args.kwargs["data_snapshot_id"],
            "snapshot-trusted",
        )
        self.assertNotIn("platform_input_ids", predict.call_args.kwargs)
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

        snapshot = BlackboxSnapshot(
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
            patch(
                "scheduler.executor.get_ready_blackbox_snapshot",
                return_value=snapshot,
            ),
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
        self.assertEqual(
            record.extra["data_snapshot_id"],
            "snapshot-test",
        )


    def test_backtest_splits_batches_and_preserves_order(self) -> None:
        from scheduler.blackbox_v2_runner import RuntimeProfile, run_blackbox_backtest

        requests = [_request(f"{index:03d}") for index in range(3)]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = _write_script(root / "trial.py", _SUCCESS_SCRIPT)
            records = run_blackbox_backtest(
                metadata=_metadata(),
                script_path=script,
                requests=requests,
                data_dir=_write_data_dir(root),
                data_snapshot_id="snapshot-test",
                profile=RuntimeProfile.for_tests(max_batch_requests=2),
            )

        self.assertEqual(len(records), 3)
        self.assertEqual([record.extra["request_id"] for record in records], [item.request_id for item in requests])


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
        daily_cutoff_keys=("2026-07-24", "2026-07-31"),
        weekly_cutoff_keys=("202630", "202631"),
        monthly_cutoff_keys=("202607",),
        calendar_week_ids_by_date={
            "2026-07-24": "202630",
            "2026-07-31": "202631",
        },
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
    (data_dir / "api_wind_date.csv").write_text(
        "rdate,week_id\n2026-07-15,202627\n",
        encoding="utf-8",
    )
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
