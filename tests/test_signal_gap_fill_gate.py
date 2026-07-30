from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from harness.authorization import (
    issue_signal_gap_fill_token,
    parse_token,
)
from harness.gates.signal_gap_fill_gate import run_signal_gap_fill
from harness.signal_gap_plan import canonical_plan_sha256
from shared.models import PredictionRecord


def _target(tenor: str, horizon: int, target_date: str) -> dict:
    return {
        "registry_scheme_id": f"demo__h{horizon}__{tenor}",
        "base_scheme_id": "demo",
        "runtime_type": "native_adapter",
        "frequency": "daily",
        "task_type": f"T+{horizon}",
        "target_tenor": tenor,
        "horizon": horizon,
        "predict_date": "2026-07-28",
        "feature_date": "2026-07-27",
        "target_date": target_date,
        "segment": "live",
        "prediction_phase": "gray_live",
        "scheme_version": "version-1",
        "code_sha256": "a" * 64,
        "config_sha256": "b" * 64,
        "input_mode": "generation_v1",
        "business_key": ["demo", tenor, horizon, target_date],
        "action": "GRAY_LIVE_GAP",
        "reason": "LIVE_BUSINESS_KEY_MISSING",
        "business_key_present": False,
        "rebuild_group_id": None,
        "input_authority": {
            "database": {
                "generation_id": "native-" + "c" * 24,
                "manifest_sha256": "d" * 64,
            },
            "artifact": {
                "generation_id": "native-" + "c" * 24,
                "manifest_uri": "/frozen/native/manifest.json",
                "manifest_sha256": "d" * 64,
                "dataset_content_id": "e" * 64,
                "source_commit_token": "f" * 64,
                "business_date": "2026-07-30",
                "feature_date": "2026-07-27",
                "exporter_version":
                    "native-signal-gap-current-snapshot-v1",
            },
        },
    }


def _blackbox_target() -> dict:
    return {
        **_target("10Y", 1, "2026-07-29"),
        "runtime_type": "blackbox_v2",
        "input_mode": "databridge_v1",
        "input_authority": {
            "authority_type": "stable_databridge_current",
            "generation_id": "current-20260729",
            "refresh_date": "2026-07-29",
            "stable_identity_sha256": "f" * 64,
            "cutoff": {
                "feature_date": "2026-07-27",
                "daily_cutoff_key": "2026-07-27",
                "weekly_cutoff_key": "202630",
                "monthly_cutoff_key": "202607",
            },
        },
    }


def _archived_native_target() -> dict:
    action = _target("10Y", 5, "2026-08-03")
    artifact = {
        **action["input_authority"]["artifact"],
        "business_date": "2026-07-28",
        "exporter_version": "native-generation-exporter-v1",
    }
    return {
        **action,
        "input_authority": {
            "database": {
                **action["input_authority"]["database"],
                "business_date": "2026-07-28",
                "feature_date": "2026-07-27",
                "exporter_version": "native-generation-exporter-v1",
            },
            "artifact": artifact,
        },
    }


def _blackbox_record(
    *,
    scheme_version: str | None = None,
) -> PredictionRecord:
    return PredictionRecord(
        scheme_id="demo",
        target_tenor="10Y",
        horizon=1,
        predict_date="2026-07-28",
        feature_date="2026-07-27",
        target_date="2026-07-29",
        predicted_direction=1,
        scheme_version=scheme_version,
        model_version="1.0.0",
        extra={
            "data_generation_id": "current-20260729",
            "source_refresh_date": "2026-07-29",
            "daily_cutoff_key": "2026-07-27",
            "weekly_cutoff_key": "202630",
            "monthly_cutoff_key": "202607",
        },
    )


def _run_blackbox_record(
    record: PredictionRecord,
    *,
    engine,
) -> list[PredictionRecord]:
    from harness.gates.signal_gap_fill_gate import (
        _Execution,
        _build_groups,
        _run_algorithm,
    )

    group = _build_groups(_plan(actions=[_blackbox_target()]))[0]
    item = _Execution(
        group=group,
        cfg=SimpleNamespace(
            scheme_id="demo",
            runtime_type="blackbox_v2",
        ),
        run_id=1,
        started=time.monotonic(),
    )
    return _run_algorithm(
        item,
        engine=engine,
        algo_env="forecast_env",
        timeout_sec=600,
        algorithm_runner=lambda *_args, **_kwargs: [record],
        native_generation_opener=Mock(),
    )


def _plan(*, actions=None) -> dict:
    rows = actions or [
        _target("1Y", 1, "2026-07-29"),
        _target("5Y", 1, "2026-07-29"),
    ]
    unsigned = {
        "schema_version": "active-signal-gap-plan-v2",
        "status": "READY",
        "start_date": "2026-07-28",
        "as_of_date": "2026-07-29",
        "control_plane": {"blockers": [], "read_only": True},
        "counts": {"blocked": 0},
        "actions": rows,
    }
    return {**unsigned, "plan_sha256": canonical_plan_sha256(unsigned)}


def _expected_target_keys(plan: dict) -> tuple[dict, ...]:
    return tuple(
        {
            key: (
                "gray_live"
                if key == "prediction_phase"
                else row[key]
            )
            for key in (
                "registry_scheme_id",
                "base_scheme_id",
                "target_tenor",
                "horizon",
                "task_type",
                "predict_date",
                "feature_date",
                "target_date",
                "prediction_phase",
            )
        }
        for row in plan["actions"]
    )


def _source_authority() -> dict:
    return {
        "authority_type": "native_current_snapshot_artifact",
        "artifact_id": "native-" + "c" * 24,
        "manifest_sha256": "d" * 64,
        "feature_date": "2026-07-27",
        "cutoff_date": "2026-07-27",
        "vintage_disclaimer":
            "current_snapshot_as_of_not_historical_vintage",
    }


class _Repository:
    def __init__(self):
        self.created = []
        self.completed = []
        self.failed = []

    def create_scheme_run(self, engine, **kwargs):
        self.created.append(kwargs)
        return 101

    def complete_gray_gap_run(self, engine, cfg, **kwargs):
        self.completed.append(kwargs)
        return len(kwargs["records"])

    def fail_scheme_run_atomic(self, engine, **kwargs):
        self.failed.append(kwargs)


class SignalGapFillGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.frozen = _plan()
        self.plan_path = self.root / "plan.json"
        self.plan_path.write_text(
            json.dumps(self.frozen),
            encoding="utf-8",
        )
        self.secret = patch.dict(
            os.environ,
            {"HARNESS_AUTH_SECRET": "test-signal-gap-secret"},
            clear=False,
        )
        self.secret.start()
        self.token = issue_signal_gap_fill_token(
            plan_sha256=self.frozen["plan_sha256"],
            base_scheme_id="demo",
            predict_date="2026-07-28",
            target_keys=_expected_target_keys(self.frozen),
            scheme_version="version-1",
            source_authority=_source_authority(),
        )
        self.engine = Mock()
        self.config = SimpleNamespace(
            scheme_id="demo",
            scheme_version="version-1",
            runtime_type="native_adapter",
            code_hash="a" * 64,
            config_hash="b" * 64,
            frequency="daily",
        )

    def tearDown(self) -> None:
        self.secret.stop()
        self.tempdir.cleanup()

    def _records(self):
        return [
            PredictionRecord(
                scheme_id="demo",
                target_tenor=row["target_tenor"],
                horizon=row["horizon"],
                predict_date=row["predict_date"],
                feature_date=row["feature_date"],
                target_date=row["target_date"],
                predicted_direction=1,
                prediction_phase="gray_live",
                scheme_version="version-1",
                extra={},
            )
            for row in self.frozen["actions"]
        ]

    def _run(self, *, current=None, runner=None, repository=None):
        current_plan = current or self.frozen
        algorithm_runner = runner or Mock(return_value=self._records())
        repo = repository or _Repository()
        report = run_signal_gap_fill(
            plan_path=self.plan_path,
            authorizations=(self.token,),
            project_root=self.root,
            engine_factory=lambda: self.engine,
            databridge_config=Mock(),
            planner=Mock(return_value=current_plan),
            config_loader=Mock(return_value=self.config),
            algorithm_runner=algorithm_runner,
            repository_module=repo,
            native_generation_opener=Mock(
                return_value=SimpleNamespace(dispose=Mock())
            ),
        )
        return report, algorithm_runner, repo

    def test_multi_target_group_runs_algorithm_once_then_commits_atomically(
        self,
    ) -> None:
        report, runner, repository = self._run()

        self.assertEqual(report["status"], "PASSED")
        runner.assert_called_once()
        self.assertEqual(
            runner.call_args.kwargs["native_execution_mode"],
            "signal_gap_current_snapshot",
        )
        self.assertEqual(
            runner.call_args.kwargs["expected_native_feature_date"],
            "2026-07-27",
        )
        self.assertEqual(len(repository.created), 1)
        self.assertEqual(len(repository.completed), 1)
        completed = repository.completed[0]
        self.assertEqual(
            completed["expected_target_keys"],
            list(_expected_target_keys(self.frozen)),
        )
        self.assertEqual(completed["plan_sha256"], self.frozen["plan_sha256"])
        self.assertEqual(completed["source_authority"], _source_authority())
        self.assertEqual(completed["records_returned"], 2)

    def test_t1_and_t5_multi_target_plans_each_form_one_execution(
        self,
    ) -> None:
        from harness.gates.signal_gap_fill_gate import _build_groups

        for horizon in (1, 5):
            with self.subTest(horizon=horizon):
                target_date = (
                    "2026-07-29"
                    if horizon == 1
                    else "2026-08-04"
                )
                plan = _plan(
                    actions=[
                        _target("1Y", horizon, target_date),
                        _target("5Y", horizon, target_date),
                    ]
                )
                groups = _build_groups(plan)
                self.assertEqual(len(groups), 1)
                self.assertEqual(
                    len(groups[0].expected_target_keys),
                    2,
                )

    def test_0629_runner_binds_exact_package_and_feature_cutoff(
        self,
    ) -> None:
        from harness.gates.signal_gap_fill_gate import (
            _Execution,
            _build_groups,
            _run_algorithm,
        )

        package_sha256 = "9" * 64
        action = {
            **self.frozen["actions"][0],
            "input_mode": "live_source_0629",
            "source_package_sha256": package_sha256,
        }
        group = _build_groups(_plan(actions=[action]))[0]
        item = _Execution(
            group=group,
            cfg=self.config,
            run_id=1,
            started=time.monotonic(),
        )
        runner = Mock(return_value=[self._records()[0]])
        opener = Mock(
            return_value=SimpleNamespace(dispose=Mock())
        )

        _run_algorithm(
            item,
            engine=self.engine,
            algo_env="forecast_env",
            timeout_sec=600,
            algorithm_runner=runner,
            native_generation_opener=opener,
        )

        opener.assert_called_once_with(
            Path("/frozen/native/manifest.json"),
            expected_generation_id="native-" + "c" * 24,
            expected_manifest_sha256="d" * 64,
            expected_business_date="2026-07-30",
            expected_feature_date="2026-07-27",
        )
        kwargs = runner.call_args.kwargs
        self.assertTrue(kwargs["live_source_compatibility"])
        self.assertEqual(
            kwargs["live_source_package_sha256"],
            package_sha256,
        )
        self.assertEqual(
            kwargs["native_execution_mode"],
            "signal_gap_current_snapshot",
        )
        self.assertEqual(
            kwargs["expected_native_feature_date"],
            "2026-07-27",
        )

    def test_archived_native_runner_uses_exact_hit_only_execution_mode(
        self,
    ) -> None:
        from harness.gates.signal_gap_fill_gate import (
            _Execution,
            _build_groups,
            _repository_source_authority,
            _run_algorithm,
        )

        action = _archived_native_target()
        group = _build_groups(_plan(actions=[action]))[0]
        item = _Execution(
            group=group,
            cfg=self.config,
            run_id=1,
            started=time.monotonic(),
        )
        record = PredictionRecord(
            scheme_id="demo",
            target_tenor="10Y",
            horizon=5,
            predict_date="2026-07-28",
            feature_date="2026-07-27",
            target_date="2026-08-03",
            predicted_direction=1,
        )
        runner = Mock(return_value=[record])
        opener = Mock(
            return_value=SimpleNamespace(dispose=Mock())
        )

        records = _run_algorithm(
            item,
            engine=self.engine,
            algo_env="forecast_env",
            timeout_sec=600,
            algorithm_runner=runner,
            native_generation_opener=opener,
        )

        kwargs = runner.call_args.kwargs
        self.assertEqual(
            kwargs["native_execution_mode"],
            "signal_gap_archived",
        )
        self.assertEqual(
            kwargs["expected_native_feature_date"],
            "2026-07-27",
        )
        self.assertEqual(records[0].prediction_phase, "gray_live")
        self.assertEqual(records[0].scheme_version, "version-1")
        self.assertEqual(
            _repository_source_authority(
                action,
                action["input_authority"],
            ),
            {
                "authority_type": "native_archived_generation",
                "generation_id": "native-" + "c" * 24,
                "manifest_sha256": "d" * 64,
                "business_date": "2026-07-28",
                "feature_date": "2026-07-27",
                "cutoff_date": "2026-07-27",
                "replay_mode": "historical_sealed_generation_replay",
            },
        )

    def test_current_snapshot_native_record_without_version_uses_frozen(
        self,
    ) -> None:
        from harness.gates.signal_gap_fill_gate import (
            _Execution,
            _build_groups,
            _run_algorithm,
        )

        action = _target("10Y", 5, "2026-08-03")
        group = _build_groups(_plan(actions=[action]))[0]
        item = _Execution(
            group=group,
            cfg=self.config,
            run_id=1,
            started=time.monotonic(),
        )
        record = PredictionRecord(
            scheme_id="demo",
            target_tenor="10Y",
            horizon=5,
            predict_date="2026-07-28",
            feature_date="2026-07-27",
            target_date="2026-08-03",
            predicted_direction=1,
        )

        records = _run_algorithm(
            item,
            engine=self.engine,
            algo_env="forecast_env",
            timeout_sec=600,
            algorithm_runner=Mock(return_value=[record]),
            native_generation_opener=Mock(
                return_value=SimpleNamespace(dispose=Mock())
            ),
        )

        self.assertEqual(records[0].prediction_phase, "gray_live")
        self.assertEqual(records[0].scheme_version, "version-1")

    def test_native_record_with_wrong_scheme_version_is_rejected(
        self,
    ) -> None:
        from harness.gates.signal_gap_fill_gate import (
            _Execution,
            _build_groups,
            _run_algorithm,
        )

        group = _build_groups(
            _plan(actions=[_archived_native_target()])
        )[0]
        item = _Execution(
            group=group,
            cfg=self.config,
            run_id=1,
            started=time.monotonic(),
        )
        record = PredictionRecord(
            scheme_id="demo",
            target_tenor="10Y",
            horizon=5,
            predict_date="2026-07-28",
            feature_date="2026-07-27",
            target_date="2026-08-03",
            predicted_direction=1,
            scheme_version="wrong-version",
        )

        with self.assertRaisesRegex(
            ValueError,
            "algorithm records do not match the atomic target group",
        ):
            _run_algorithm(
                item,
                engine=self.engine,
                algo_env="forecast_env",
                timeout_sec=600,
                algorithm_runner=Mock(return_value=[record]),
                native_generation_opener=Mock(
                    return_value=SimpleNamespace(dispose=Mock())
                ),
            )

    def test_partial_present_group_blocks_before_algorithm_or_write(
        self,
    ) -> None:
        actions = [
            {
                **self.frozen["actions"][0],
                "action": "SKIP_PRESENT",
                "business_key_present": True,
                "input_authority": None,
            },
            self.frozen["actions"][1],
        ]
        current = _plan(actions=actions)

        report, runner, repository = self._run(current=current)

        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["failure_code"], "PARTIAL_GROUP_PRESENT")
        runner.assert_not_called()
        self.assertEqual(repository.created, [])
        self.assertEqual(repository.completed, [])

    def test_all_present_reentry_is_skip_without_token_consumption(
        self,
    ) -> None:
        current = _plan(
            actions=[
                {
                    **row,
                    "action": "SKIP_PRESENT",
                    "business_key_present": True,
                    "input_authority": None,
                }
                for row in self.frozen["actions"]
            ]
        )

        report, runner, repository = self._run(current=current)

        self.assertEqual(report["status"], "SKIP_PRESENT")
        runner.assert_not_called()
        self.assertEqual(repository.created, [])

    def test_stale_sha_or_authority_drift_blocks_all_groups(self) -> None:
        changed = _plan(
            actions=[
                {
                    **row,
                    "input_authority": {
                        **row["input_authority"],
                        "artifact": {
                            **row["input_authority"]["artifact"],
                            "manifest_sha256": "e" * 64,
                        },
                    },
                }
                for row in self.frozen["actions"]
            ]
        )

        report, runner, repository = self._run(current=changed)

        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["failure_code"], "PLAN_SHA256_DRIFT")
        runner.assert_not_called()
        self.assertEqual(repository.created, [])

    def test_code_or_config_drift_blocks_before_execution_and_write(
        self,
    ) -> None:
        runner = Mock()
        repository = _Repository()
        drifted = SimpleNamespace(
            **{
                **vars(self.config),
                "config_hash": "f" * 64,
            }
        )

        report = run_signal_gap_fill(
            plan_path=self.plan_path,
            authorizations=(self.token,),
            project_root=self.root,
            engine_factory=lambda: self.engine,
            databridge_config=Mock(),
            planner=Mock(return_value=self.frozen),
            config_loader=Mock(return_value=drifted),
            algorithm_runner=runner,
            repository_module=repository,
        )

        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(
            report["failure_code"],
            "DISCOVERY_IDENTITY_DRIFT",
        )
        runner.assert_not_called()
        self.assertEqual(repository.created, [])

    def test_algorithm_failure_preserves_failed_run(self) -> None:
        runner = Mock(side_effect=RuntimeError("algorithm failed"))
        repository = _Repository()

        report, _, repository = self._run(
            runner=runner,
            repository=repository,
        )

        self.assertEqual(report["status"], "FAILED")
        self.assertEqual(len(repository.created), 1)
        self.assertEqual(repository.completed, [])
        self.assertEqual(len(repository.failed), 1)
        self.assertEqual(repository.failed[0]["run_id"], 101)

    def test_current_authority_change_after_algorithm_fails_run_without_commit(
        self,
    ) -> None:
        changed = _plan(
            actions=[
                {
                    **row,
                    "input_authority": {
                        **row["input_authority"],
                        "artifact": {
                            **row["input_authority"]["artifact"],
                            "manifest_sha256": "e" * 64,
                        },
                    },
                }
                for row in self.frozen["actions"]
            ]
        )
        repository = _Repository()

        report = run_signal_gap_fill(
            plan_path=self.plan_path,
            authorizations=(self.token,),
            project_root=self.root,
            engine_factory=lambda: self.engine,
            databridge_config=Mock(),
            planner=Mock(side_effect=[self.frozen, changed]),
            config_loader=Mock(return_value=self.config),
            algorithm_runner=Mock(return_value=self._records()),
            repository_module=repository,
            native_generation_opener=Mock(
                return_value=SimpleNamespace(dispose=Mock())
            ),
        )

        self.assertEqual(
            report["failure_code"],
            "PLAN_AUTHORITY_DRIFT_AFTER_EXECUTION",
        )
        self.assertEqual(repository.completed, [])
        self.assertEqual(len(repository.failed), 1)

    def test_token_expiring_during_algorithm_is_rejected_after_postflight(
        self,
    ) -> None:
        auth = parse_token(self.token)
        issued_at = datetime.fromisoformat(str(auth.issued_at))
        expires_at = datetime.fromisoformat(str(auth.expires_at))
        before_expiry = issued_at + timedelta(seconds=1)
        after_expiry = expires_at + timedelta(seconds=1)
        repository = _Repository()

        with patch(
            "harness.authorization.datetime",
            wraps=datetime,
        ) as datetime_mock:
            datetime_mock.now.side_effect = [
                before_expiry,
                before_expiry,
                after_expiry,
                after_expiry,
            ]
            report = run_signal_gap_fill(
                plan_path=self.plan_path,
                authorizations=(self.token,),
                project_root=self.root,
                engine_factory=lambda: self.engine,
                databridge_config=Mock(),
                planner=Mock(return_value=self.frozen),
                config_loader=Mock(return_value=self.config),
                algorithm_runner=Mock(return_value=self._records()),
                repository_module=repository,
                native_generation_opener=Mock(
                    return_value=SimpleNamespace(dispose=Mock())
                ),
            )

        self.assertEqual(
            report["failure_code"],
            "AUTHORIZATION_REVALIDATION_FAILED",
        )
        self.assertEqual(repository.completed, [])
        self.assertEqual(len(repository.failed), 1)

    def test_postflight_read_error_does_not_leave_running_run(self) -> None:
        repository = _Repository()

        report = run_signal_gap_fill(
            plan_path=self.plan_path,
            authorizations=(self.token,),
            project_root=self.root,
            engine_factory=lambda: self.engine,
            databridge_config=Mock(),
            planner=Mock(
                side_effect=[
                    self.frozen,
                    RuntimeError("authority unavailable"),
                ]
            ),
            config_loader=Mock(return_value=self.config),
            algorithm_runner=Mock(return_value=self._records()),
            repository_module=repository,
            native_generation_opener=Mock(
                return_value=SimpleNamespace(dispose=Mock())
            ),
        )

        self.assertEqual(
            report["failure_code"],
            "POSTFLIGHT_AUTHORITY_UNAVAILABLE",
        )
        self.assertEqual(len(repository.failed), 1)

    def test_cli_is_explicit_and_has_no_generic_write_switch(self) -> None:
        from harness.cli import _build_parser
        from harness.registry import AUTO_SEQUENCE

        parser = _build_parser()
        args = parser.parse_args(
            [
                "signal-gap-fill",
                "--plan",
                str(self.plan_path),
                "--authorize",
                self.token,
            ]
        )

        self.assertEqual(args.command, "signal-gap-fill")
        self.assertFalse(hasattr(args, "persist"))
        self.assertFalse(hasattr(args, "prediction_phase"))
        self.assertFalse(hasattr(args, "report_dir"))
        self.assertNotIn("signal-gap-fill", AUTO_SEQUENCE)
        with (
            patch("sys.stderr"),
            self.assertRaises(SystemExit),
        ):
            parser.parse_args(
                [
                    "signal-gap-fill",
                    "--plan",
                    str(self.plan_path),
                    "--authorize",
                    self.token,
                    "--report-dir",
                    str(self.root / "unused"),
                ]
            )

    def test_singleton_lock_blocks_second_fill_before_engine_run_or_algorithm(
        self,
    ) -> None:
        from scheduler.daily_coordinator import OccurrenceFileLock

        lock_root = self.root / "locks"
        lock_root.mkdir(mode=0o700)
        lock_root.chmod(0o700)
        lock_path = (lock_root / "signal-gap-fill.lock").resolve()
        owner = OccurrenceFileLock(lock_path).acquire()
        engine_factory = Mock(return_value=self.engine)
        algorithm_runner = Mock()
        repository = _Repository()
        try:
            report = run_signal_gap_fill(
                plan_path=self.plan_path,
                authorizations=(self.token,),
                project_root=self.root,
                engine_factory=engine_factory,
                databridge_config=Mock(),
                planner=Mock(return_value=self.frozen),
                config_loader=Mock(return_value=self.config),
                algorithm_runner=algorithm_runner,
                repository_module=repository,
                singleton_lock_factory=(
                    lambda: OccurrenceFileLock(lock_path)
                ),
            )
        finally:
            owner.release()

        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(
            report["failure_code"],
            "SIGNAL_GAP_FILL_ALREADY_RUNNING",
        )
        engine_factory.assert_not_called()
        algorithm_runner.assert_not_called()
        self.assertEqual(repository.created, [])

    def test_singleton_lock_is_held_through_algorithm_execution(self) -> None:
        from scheduler.daily_coordinator import (
            OccurrenceFileLock,
            OccurrenceLockUnavailable,
        )

        lock_root = self.root / "locks"
        lock_root.mkdir(mode=0o700)
        lock_root.chmod(0o700)
        lock_path = (lock_root / "signal-gap-fill.lock").resolve()

        def runner(*_args, **_kwargs):
            contender = OccurrenceFileLock(lock_path)
            with self.assertRaises(OccurrenceLockUnavailable):
                contender.acquire()
            return self._records()

        report = run_signal_gap_fill(
            plan_path=self.plan_path,
            authorizations=(self.token,),
            project_root=self.root,
            engine_factory=lambda: self.engine,
            databridge_config=Mock(),
            planner=Mock(return_value=self.frozen),
            config_loader=Mock(return_value=self.config),
            algorithm_runner=runner,
            repository_module=_Repository(),
            native_generation_opener=Mock(
                return_value=SimpleNamespace(dispose=Mock())
            ),
            singleton_lock_factory=lambda: OccurrenceFileLock(lock_path),
        )

        self.assertEqual(report["status"], "PASSED")

    def test_default_singleton_lock_uses_private_owned_symlink_free_root(
        self,
    ) -> None:
        from harness.gates.signal_gap_fill_gate import (
            _signal_gap_fill_singleton_lock,
        )

        with patch(
            "harness.gates.signal_gap_fill_gate."
            "resolve_daily_runtime_root",
            return_value=self.root.resolve(),
        ):
            lock = _signal_gap_fill_singleton_lock()
        try:
            lock.acquire()
            root = lock.path.parent
            self.assertEqual(
                root,
                self.root.resolve() / "signal-gap-fill-locks",
            )
            self.assertEqual(root.stat().st_uid, os.getuid())
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(lock.path.stat().st_uid, os.getuid())
            self.assertEqual(lock.path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(root.is_symlink())
            self.assertFalse(lock.path.is_symlink())
            self.assertEqual(lock.path, lock.path.resolve())
        finally:
            lock.release()

        real_root = self.root / "real-runtime-root"
        real_root.mkdir(mode=0o700)
        symlink_root = self.root / "symlink-runtime-root"
        symlink_root.symlink_to(real_root, target_is_directory=True)
        with (
            patch(
                "harness.gates.signal_gap_fill_gate."
                "resolve_daily_runtime_root",
                return_value=symlink_root,
            ),
            self.assertRaisesRegex(ValueError, "symlink-free"),
        ):
            _signal_gap_fill_singleton_lock()

    def test_repository_resolution_error_still_disposes_engine(self) -> None:
        singleton_lock = SimpleNamespace(
            acquire=Mock(),
            release=Mock(),
        )

        with (
            patch(
                "harness.gates.signal_gap_fill_gate._repository_module",
                side_effect=RuntimeError("repository contract missing"),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "repository contract missing",
            ),
        ):
            run_signal_gap_fill(
                plan_path=self.plan_path,
                authorizations=(self.token,),
                project_root=self.root,
                engine_factory=lambda: self.engine,
                databridge_config=Mock(),
                planner=Mock(return_value=self.frozen),
                config_loader=Mock(return_value=self.config),
                algorithm_runner=Mock(),
                singleton_lock_factory=lambda: singleton_lock,
            )

        self.engine.dispose.assert_called_once_with()
        singleton_lock.release.assert_called_once_with()

    def test_blackbox_refresh_not_after_predict_date_is_rejected(self) -> None:
        action = {
            **_target("10Y", 1, "2026-07-29"),
            "runtime_type": "blackbox_v2",
            "input_mode": "databridge_v1",
            "input_authority": {
                "authority_type": "stable_databridge_current",
                "generation_id": "current-20260728",
                "refresh_date": "2026-07-28",
                "stable_identity_sha256": "f" * 64,
                "cutoff": {
                    "feature_date": "2026-07-27",
                    "daily_cutoff_key": "2026-07-27",
                    "weekly_cutoff_key": "202630",
                    "monthly_cutoff_key": "202607",
                },
            },
        }
        plan = _plan(actions=[action])
        path = self.root / "blackbox-plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")

        report = run_signal_gap_fill(
            plan_path=path,
            authorizations=(),
            project_root=self.root,
            engine_factory=lambda: self.engine,
            databridge_config=Mock(),
            planner=Mock(return_value=plan),
            config_loader=Mock(return_value=self.config),
            algorithm_runner=Mock(),
            repository_module=_Repository(),
        )

        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn(
            "refresh_date must be after predict_date",
            report["errors"][0],
        )

    def test_blackbox_record_without_scheme_version_uses_frozen_version(
        self,
    ) -> None:
        try:
            records = _run_blackbox_record(
                _blackbox_record(),
                engine=self.engine,
            )
        except ValueError as exc:
            self.fail(
                "Blackbox runner records without scheme_version must be "
                f"stamped from the frozen group: {exc}"
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].prediction_phase, "gray_live")
        self.assertEqual(records[0].scheme_version, "version-1")

    def test_blackbox_record_with_wrong_scheme_version_is_rejected(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "algorithm records do not match the atomic target group",
        ):
            _run_blackbox_record(
                _blackbox_record(scheme_version="wrong-version"),
                engine=self.engine,
            )

    def test_internal_pools_cap_native_and_v2_at_two_each(self) -> None:
        from harness.gates.signal_gap_fill_gate import (
            _Execution,
            _build_groups,
            _run_algorithms,
        )

        actions = []
        for runtime_type in ("native_adapter", "blackbox_v2"):
            for index in range(3):
                base = f"{runtime_type}-{index}"
                row = {
                    **_target("10Y", 1, "2026-07-29"),
                    "registry_scheme_id": f"{base}__h1__10Y",
                    "base_scheme_id": base,
                    "runtime_type": runtime_type,
                    "input_mode": (
                        "databridge_v1"
                        if runtime_type == "blackbox_v2"
                        else "generation_v1"
                    ),
                }
                if runtime_type == "blackbox_v2":
                    row["input_authority"] = {
                        "authority_type": "stable_databridge_current",
                        "generation_id": "current-20260729",
                        "refresh_date": "2026-07-29",
                        "stable_identity_sha256": f"{index + 3:064x}",
                        "cutoff": {
                            "feature_date": "2026-07-27",
                            "daily_cutoff_key": "2026-07-27",
                            "weekly_cutoff_key": "202630",
                            "monthly_cutoff_key": "202607",
                        },
                    }
                actions.append(row)
        groups = _build_groups(_plan(actions=actions))
        executions = [
            _Execution(
                group=group,
                cfg=SimpleNamespace(
                    scheme_id=group.base_scheme_id,
                    runtime_type=group.runtime_type,
                    action=group.actions[0],
                ),
                run_id=index + 1,
                started=time.monotonic(),
            )
            for index, group in enumerate(groups)
        ]
        lock = threading.Lock()
        active = {"native_adapter": 0, "blackbox_v2": 0, "all": 0}
        maximum = {"native_adapter": 0, "blackbox_v2": 0, "all": 0}

        def runner(cfg, predict_date, **_kwargs):
            runtime_type = cfg.runtime_type
            with lock:
                active[runtime_type] += 1
                active["all"] += 1
                maximum[runtime_type] = max(
                    maximum[runtime_type],
                    active[runtime_type],
                )
                maximum["all"] = max(maximum["all"], active["all"])
            time.sleep(0.03)
            row = cfg.action
            extra = {}
            if runtime_type == "blackbox_v2":
                extra = {
                    "data_generation_id": "current-20260729",
                    "source_refresh_date": "2026-07-29",
                    "daily_cutoff_key": "2026-07-27",
                    "weekly_cutoff_key": "202630",
                    "monthly_cutoff_key": "202607",
                }
            record = PredictionRecord(
                scheme_id=cfg.scheme_id,
                target_tenor="10Y",
                horizon=1,
                predict_date=predict_date,
                feature_date="2026-07-27",
                target_date="2026-07-29",
                predicted_direction=1,
                prediction_phase="gray_live",
                scheme_version="version-1",
                extra=extra,
            )
            with lock:
                active[runtime_type] -= 1
                active["all"] -= 1
            return [record]

        _run_algorithms(
            executions,
            engine=Mock(),
            algo_env="test",
            timeout_sec=30,
            algorithm_runner=runner,
            native_generation_opener=Mock(
                return_value=SimpleNamespace(dispose=Mock())
            ),
        )

        self.assertLessEqual(maximum["native_adapter"], 2)
        self.assertLessEqual(maximum["blackbox_v2"], 2)
        self.assertLessEqual(maximum["all"], 4)
        self.assertTrue(all(item.error is None for item in executions))

    def test_commit_failure_marks_every_nonterminal_run_failed(self) -> None:
        second = {
            **_target("10Y", 1, "2026-07-29"),
            "registry_scheme_id": "demo2__h1__10Y",
            "base_scheme_id": "demo2",
            "business_key": [
                "demo2",
                "10Y",
                1,
                "2026-07-29",
            ],
        }
        plan = _plan(actions=[self.frozen["actions"][0], second])
        path = self.root / "two-groups.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        from harness.gates.signal_gap_fill_gate import _build_groups

        groups = _build_groups(plan)
        tokens = tuple(
            issue_signal_gap_fill_token(
                plan_sha256=plan["plan_sha256"],
                base_scheme_id=group.base_scheme_id,
                predict_date=group.predict_date,
                target_keys=group.expected_target_keys,
                scheme_version=group.scheme_version,
                source_authority=group.source_authority,
            )
            for group in groups
        )

        class FailingRepository(_Repository):
            def create_scheme_run(self, engine, **kwargs):
                self.created.append(kwargs)
                return 100 + len(self.created)

            def complete_gray_gap_run(self, engine, cfg, **kwargs):
                raise RuntimeError("commit failed")

        repository = FailingRepository()

        def load_config(path):
            return SimpleNamespace(
                scheme_id=path.parent.name,
                scheme_version="version-1",
                runtime_type="native_adapter",
                code_hash="a" * 64,
                config_hash="b" * 64,
                frequency="daily",
            )

        def run_algorithm(cfg, predict_date, **_kwargs):
            group = next(
                item for item in groups
                if item.base_scheme_id == cfg.scheme_id
            )
            row = group.actions[0]
            return [
                PredictionRecord(
                    scheme_id=cfg.scheme_id,
                    target_tenor=row["target_tenor"],
                    horizon=row["horizon"],
                    predict_date=predict_date,
                    feature_date=row["feature_date"],
                    target_date=row["target_date"],
                    predicted_direction=1,
                    prediction_phase="gray_live",
                    scheme_version="version-1",
                    extra={},
                )
            ]

        report = run_signal_gap_fill(
            plan_path=path,
            authorizations=tokens,
            project_root=self.root,
            engine_factory=lambda: self.engine,
            databridge_config=Mock(),
            planner=Mock(return_value=plan),
            config_loader=load_config,
            algorithm_runner=run_algorithm,
            repository_module=repository,
            native_generation_opener=Mock(
                return_value=SimpleNamespace(dispose=Mock())
            ),
        )

        self.assertEqual(report["failure_code"], "GRAY_GAP_COMMIT_FAILED")
        self.assertEqual(
            {row["run_id"] for row in repository.failed},
            {101, 102},
        )
