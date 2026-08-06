from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from harness.gates import signal_gap_fill_gate
from harness.signal_gap_plan import (
    PLAN_SCHEMA_VERSION,
    SignalGapPlanScope,
    canonical_plan_sha256,
)
from shared.models import PredictionRecord


def _databridge_authority(
    feature_date: str,
    *,
    generation_id: str = "current-1",
) -> dict[str, Any]:
    return {
        "authority_type": "stable_databridge_current",
        "authority_schema_version": (
            "stable-databridge-current-authority-v1"
        ),
        "stable_identity_sha256": "c" * 64,
        "generation_id": generation_id,
        "refresh_date": "2026-08-06",
        "schema_version": "data-bridge-v1",
        "business_digest": "d" * 64,
        "publication_capability": None,
        "files": [
            {
                "filename": "daily_output.csv",
                "rows": 20,
                "columns": 2,
                "min_key": "2026-06-01",
                "max_key": "2026-08-06",
                "sha256": "1" * 64,
                "business_hash": "4" * 64,
            },
            {
                "filename": "monthly_output.csv",
                "rows": 3,
                "columns": 2,
                "min_key": "202606",
                "max_key": "202608",
                "sha256": "2" * 64,
                "business_hash": "5" * 64,
            },
            {
                "filename": "weekly_output.csv",
                "rows": 10,
                "columns": 2,
                "min_key": "202622",
                "max_key": "202632",
                "sha256": "3" * 64,
                "business_hash": "6" * 64,
            },
        ],
        "cutoff": {
            "feature_date": feature_date,
            "daily_cutoff_key": feature_date,
            "weekly_cutoff_key": "202631",
            "monthly_cutoff_key": "202607",
        },
    }


def _blackbox_action(
    *,
    scheme_id: str,
    frequency: str,
    task_type: str,
    target_tenor: str,
    horizon: int,
    predict_date: str,
    feature_date: str,
    target_date: str,
    weekly_cutoff_key: str,
    monthly_cutoff_key: str,
    generation_id: str = "current-1",
) -> dict[str, Any]:
    authority = _databridge_authority(
        feature_date,
        generation_id=generation_id,
    )
    authority["cutoff"] = {
        "feature_date": feature_date,
        "daily_cutoff_key": feature_date,
        "weekly_cutoff_key": weekly_cutoff_key,
        "monthly_cutoff_key": monthly_cutoff_key,
    }
    return {
        "registry_scheme_id": (
            f"{scheme_id}__h{horizon}__{target_tenor}"
        ),
        "base_scheme_id": scheme_id,
        "runtime_type": "blackbox_v2",
        "frequency": frequency,
        "task_type": task_type,
        "target_tenor": target_tenor,
        "horizon": horizon,
        "predict_date": predict_date,
        "feature_date": feature_date,
        "target_date": target_date,
        "segment": "live",
        "prediction_phase": "gray_live",
        "scheme_version": f"{scheme_id}-v1",
        "code_sha256": "a" * 64,
        "config_sha256": "b" * 64,
        "input_mode": "databridge_v1",
        "business_key_present": False,
        "action": "GRAY_LIVE_GAP",
        "input_authority": authority,
    }


def _blackbox_groups(
    *,
    monthly_generation_id: str = "current-1",
) -> tuple[signal_gap_fill_gate._GapGroup, ...]:
    actions = [
        _blackbox_action(
            scheme_id="daily_trial",
            frequency="daily",
            task_type="T+1",
            target_tenor="1Y",
            horizon=1,
            predict_date="2026-07-02",
            feature_date="2026-07-01",
            target_date="2026-07-02",
            weekly_cutoff_key="202626",
            monthly_cutoff_key="202606",
        ),
        _blackbox_action(
            scheme_id="weekly_trial",
            frequency="weekly",
            task_type="weekly_point",
            target_tenor="5Y",
            horizon=1,
            predict_date="2026-07-06",
            feature_date="2026-07-03",
            target_date="2026-07-10",
            weekly_cutoff_key="202627",
            monthly_cutoff_key="202606",
        ),
        _blackbox_action(
            scheme_id="monthly_trial",
            frequency="monthly",
            task_type="monthly",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-07-15",
            feature_date="2026-07-15",
            target_date="2026-08-14",
            weekly_cutoff_key="202628",
            monthly_cutoff_key="202607",
            generation_id=monthly_generation_id,
        ),
    ]
    return signal_gap_fill_gate._build_groups({"actions": actions})


def _executions_for(
    groups: tuple[signal_gap_fill_gate._GapGroup, ...],
) -> list[signal_gap_fill_gate._Execution]:
    return [
        signal_gap_fill_gate._Execution(
            group=group,
            cfg=SimpleNamespace(
                scheme_id=group.base_scheme_id,
                runtime_type="blackbox_v2",
                input_source="data_bridge_current",
                frequency=group.actions[0]["frequency"],
            ),
            run_id=index,
            started=0.0,
        )
        for index, group in enumerate(groups, start=1)
    ]


def _record_for_request(
    group: signal_gap_fill_gate._GapGroup,
    request: Any,
) -> PredictionRecord:
    return PredictionRecord(
        scheme_id=group.base_scheme_id,
        target_tenor=str(group.actions[0]["target_tenor"]),
        horizon=int(group.actions[0]["horizon"]),
        predict_date=request.predict_date,
        feature_date=request.feature_date,
        target_date=request.target_date,
        predicted_direction=1,
        extra={
            "request_id": request.request_id,
            "data_generation_id": group.input_authority["generation_id"],
            "source_refresh_date": group.input_authority["refresh_date"],
            "daily_cutoff_key": request.daily_cutoff_key,
            "weekly_cutoff_key": request.weekly_cutoff_key,
            "monthly_cutoff_key": request.monthly_cutoff_key,
        },
    )


class SignalGapFillPlanScopeTests(unittest.TestCase):
    def test_fill_replays_the_frozen_scope_during_preflight(self) -> None:
        action = {
            "registry_scheme_id": "alpha__h1__1Y",
            "base_scheme_id": "alpha",
            "runtime_type": "blackbox_v2",
            "frequency": "daily",
            "task_type": "T+1",
            "target_tenor": "1Y",
            "horizon": 1,
            "predict_date": "2026-08-04",
            "feature_date": "2026-08-03",
            "target_date": "2026-08-04",
            "segment": "live",
            "prediction_phase": "gray_live",
            "scheme_version": "version-1",
            "code_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "input_mode": "databridge_v1",
            "business_key_present": False,
            "action": "GRAY_LIVE_GAP",
            "input_authority": _databridge_authority("2026-08-03"),
        }
        payload = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "start_date": "2026-08-04",
            "as_of_date": "2026-08-05",
            "selection": {
                "target_date_start": "2026-08-04",
                "target_date_end": "2026-08-05",
                "task_types": ["T+1"],
            },
            "actions": [action],
        }
        payload["plan_sha256"] = canonical_plan_sha256(payload)
        scopes: list[SignalGapPlanScope] = []
        frozen_overrides: list[dict[str, Any]] = []

        class _Lock:
            def acquire(self) -> None:
                return None

            def release(self) -> None:
                return None

        def planner(*_args, **kwargs):
            scopes.append(kwargs["scope"])
            frozen_overrides.append(
                kwargs["databridge_authority_overrides"]
            )
            return payload

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(payload), encoding="utf-8")
            report = signal_gap_fill_gate.run_signal_gap_fill(
                plan_path=plan_path,
                authorizations=(),
                project_root=root,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
                databridge_config=object(),
                planner=planner,
                config_loader=lambda _path: (_ for _ in ()).throw(
                    ValueError("stop before authorization")
                ),
                singleton_lock_factory=_Lock,
            )

        self.assertEqual(report["failure_code"], "DISCOVERY_IDENTITY_DRIFT")
        self.assertEqual(
            scopes,
            [
                SignalGapPlanScope(
                    target_date_start="2026-08-04",
                    target_date_end="2026-08-05",
                    task_types=("T+1",),
                )
            ],
        )
        self.assertEqual(
            frozen_overrides,
            [{"2026-08-03": _databridge_authority("2026-08-03")}],
        )

    def test_frozen_plan_restores_hash_bound_scope_for_replay(self) -> None:
        payload = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "start_date": "2026-08-04",
            "as_of_date": "2026-08-05",
            "selection": {
                "target_date_start": "2026-08-04",
                "target_date_end": "2026-08-05",
                "task_types": ["T+1"],
            },
            "actions": [],
        }
        payload["plan_sha256"] = canonical_plan_sha256(payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "plan.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            frozen = signal_gap_fill_gate._load_frozen_plan(path)

        self.assertEqual(
            signal_gap_fill_gate._scope_from_frozen_plan(frozen),
            SignalGapPlanScope(
                target_date_start="2026-08-04",
                target_date_end="2026-08-05",
                task_types=("T+1",),
            ),
        )

    def test_frozen_plan_rejects_missing_or_malformed_selection(self) -> None:
        payload = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "start_date": "2026-08-04",
            "as_of_date": "2026-08-05",
            "actions": [],
        }
        payload["plan_sha256"] = canonical_plan_sha256(payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "plan.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "scope is invalid",
            ):
                signal_gap_fill_gate._load_frozen_plan(path)

    def test_frozen_plan_rejects_noncanonical_selection_payload(self) -> None:
        payload = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "start_date": "2026-08-04",
            "as_of_date": "2026-08-05",
            "selection": {
                "target_date_start": "2026-08-04",
                "target_date_end": "2026-08-05",
                "task_types": ["T+1", "T+1"],
            },
            "actions": [],
        }
        payload["plan_sha256"] = canonical_plan_sha256(payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "plan.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "scope is invalid",
            ):
                signal_gap_fill_gate._load_frozen_plan(path)


class BlackboxGrayReplayBatchCoordinatorTests(unittest.TestCase):
    def test_fill_replays_frozen_authority_in_preflight_and_postflight(
        self,
    ) -> None:
        groups = _blackbox_groups()
        actions = [action for group in groups for action in group.actions]
        payload = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "start_date": "2026-07-01",
            "as_of_date": "2026-07-15",
            "selection": {
                "target_date_start": "2026-07-01",
                "target_date_end": "2026-08-14",
                "task_types": ["T+1", "monthly", "weekly_point"],
            },
            "actions": actions,
        }
        payload["plan_sha256"] = canonical_plan_sha256(payload)
        planner_calls: list[dict[str, Any]] = []
        completed: list[dict[str, Any]] = []

        class _Lock:
            def acquire(self) -> None:
                return None

            def release(self) -> None:
                return None

        class _Repository:
            def __init__(self) -> None:
                self._next_run_id = 1

            def create_scheme_run(self, *_args, **_kwargs) -> int:
                run_id = self._next_run_id
                self._next_run_id += 1
                return run_id

            def complete_gray_gap_run(self, _engine, cfg, **kwargs) -> int:
                completed.append(
                    {
                        "scheme_id": cfg.scheme_id,
                        "records": kwargs["records"],
                        "source_authority": kwargs["source_authority"],
                    }
                )
                return len(kwargs["records"])

            def fail_scheme_run_atomic(self, *_args, **_kwargs) -> None:
                raise AssertionError("successful fill must not fail a run")

        def planner(*_args, **kwargs):
            planner_calls.append(kwargs)
            return payload

        def config_loader(path: Path):
            scheme_id = path.parent.name
            group = next(
                item for item in groups if item.base_scheme_id == scheme_id
            )
            return SimpleNamespace(
                scheme_id=scheme_id,
                scheme_version=group.scheme_version,
                runtime_type="blackbox_v2",
                code_hash=group.code_sha256,
                config_hash=group.config_sha256,
                input_source="data_bridge_current",
                frequency=group.actions[0]["frequency"],
            )

        def batch_runner(cfg, *, requests, **_kwargs):
            group = next(
                item for item in groups if item.base_scheme_id == cfg.scheme_id
            )
            return [
                _record_for_request(group, request)
                for request in requests
            ]

        expected_overrides = {
            group.actions[0]["feature_date"]: group.input_authority
            for group in groups
        }
        fake_authorizations = {
            group.identity: object() for group in groups
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(payload), encoding="utf-8")
            with (
                patch.object(
                    signal_gap_fill_gate,
                    "_verify_group_authorizations",
                    return_value=(fake_authorizations, []),
                ),
                patch.object(signal_gap_fill_gate, "mark_token_used"),
                patch.object(signal_gap_fill_gate, "write_authorization_audit"),
            ):
                report = signal_gap_fill_gate.run_signal_gap_fill(
                    plan_path=plan_path,
                    authorizations=(),
                    project_root=root,
                    engine_factory=lambda: SimpleNamespace(
                        dispose=lambda: None
                    ),
                    databridge_config=object(),
                    planner=planner,
                    config_loader=config_loader,
                    repository_module=_Repository(),
                    singleton_lock_factory=_Lock,
                    blackbox_session_builder=(
                        lambda **kwargs: SimpleNamespace(
                            session_id=kwargs["session_id"]
                        )
                    ),
                    blackbox_batch_runner=batch_runner,
                )

        self.assertEqual(report["status"], "PASSED")
        self.assertEqual(len(planner_calls), 2)
        self.assertEqual(
            [
                call["databridge_authority_overrides"]
                for call in planner_calls
            ],
            [expected_overrides, expected_overrides],
        )
        self.assertEqual(len(completed), 3)

    def test_mixed_frequency_groups_share_one_session_and_fan_out(
        self,
    ) -> None:
        groups = _blackbox_groups()
        executions = _executions_for(groups)
        sessions: list[dict[str, Any]] = []
        batch_calls: list[tuple[str, list[Any], Any]] = []

        def session_builder(**kwargs):
            sessions.append(kwargs)
            return SimpleNamespace(session_id=kwargs["session_id"])

        def batch_runner(cfg, *, requests, session, **_kwargs):
            request_rows = list(requests)
            batch_calls.append((cfg.scheme_id, request_rows, session))
            group = next(
                item.group
                for item in executions
                if item.group.base_scheme_id == cfg.scheme_id
            )
            return [
                _record_for_request(group, request)
                for request in request_rows
            ]

        signal_gap_fill_gate._run_algorithms(
            executions,
            engine=object(),
            algo_env="forecast_env",
            timeout_sec=600,
            data_bridge_config=object(),
            algorithm_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("legacy runner must not receive Blackbox replay")
            ),
            native_generation_opener=lambda *_args, **_kwargs: None,
            blackbox_session_builder=session_builder,
            blackbox_batch_runner=batch_runner,
        )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(
            [
                (
                    cutoff.daily_cutoff_key,
                    cutoff.weekly_cutoff_key,
                    cutoff.monthly_cutoff_key,
                )
                for cutoff in sessions[0]["request_cutoffs"]
            ],
            [
                ("2026-07-01", "202626", "202606"),
                ("2026-07-15", "202628", "202607"),
                ("2026-07-03", "202627", "202606"),
            ],
        )
        self.assertEqual(
            {scheme_id for scheme_id, _, _ in batch_calls},
            {"daily_trial", "weekly_trial", "monthly_trial"},
        )
        self.assertTrue(
            all(call_session is batch_calls[0][2] for _, _, call_session in batch_calls)
        )
        self.assertTrue(all(item.error is None for item in executions))
        self.assertEqual(
            [item.records[0].predict_date for item in executions],
            [
                "2026-07-02",
                "2026-07-15",
                "2026-07-06",
            ],
        )

    def test_different_source_identity_creates_separate_sessions(self) -> None:
        groups = _blackbox_groups(monthly_generation_id="current-2")
        executions = _executions_for(groups)
        sessions: list[dict[str, Any]] = []

        def session_builder(**kwargs):
            sessions.append(kwargs)
            return SimpleNamespace(session_id=kwargs["session_id"])

        def batch_runner(cfg, *, requests, **_kwargs):
            group = next(
                item.group
                for item in executions
                if item.group.base_scheme_id == cfg.scheme_id
            )
            return [
                _record_for_request(group, request)
                for request in requests
            ]

        signal_gap_fill_gate._run_algorithms(
            executions,
            engine=object(),
            algo_env="forecast_env",
            timeout_sec=600,
            data_bridge_config=object(),
            algorithm_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("legacy runner must not receive Blackbox replay")
            ),
            native_generation_opener=lambda *_args, **_kwargs: None,
            blackbox_session_builder=session_builder,
            blackbox_batch_runner=batch_runner,
        )

        self.assertEqual(len(sessions), 2)
        self.assertEqual(
            {
                item["source_identity"]["generation_id"]
                for item in sessions
            },
            {"current-1", "current-2"},
        )
        self.assertTrue(all(item.error is None for item in executions))

    def test_session_build_failure_marks_each_source_execution_failed(
        self,
    ) -> None:
        executions = _executions_for(_blackbox_groups())
        batch_calls: list[str] = []

        def session_builder(**_kwargs):
            raise RuntimeError("frozen current no longer matches authority")

        def batch_runner(cfg, **_kwargs):
            batch_calls.append(cfg.scheme_id)
            return []

        signal_gap_fill_gate._run_algorithms(
            executions,
            engine=object(),
            algo_env="forecast_env",
            timeout_sec=600,
            data_bridge_config=object(),
            algorithm_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("legacy runner must not receive Blackbox replay")
            ),
            native_generation_opener=lambda *_args, **_kwargs: None,
            blackbox_session_builder=session_builder,
            blackbox_batch_runner=batch_runner,
        )

        self.assertEqual(batch_calls, [])
        self.assertTrue(
            all(
                isinstance(item.error, RuntimeError)
                for item in executions
            )
        )

    def test_session_failure_prevents_native_execution(self) -> None:
        blackbox_execution = _executions_for(_blackbox_groups())[0]
        native_group = signal_gap_fill_gate._GapGroup(
            base_scheme_id="native_trial",
            predict_date="2026-07-02",
            runtime_type="native_adapter",
            scheme_version="native-trial-v1",
            code_sha256="a" * 64,
            config_sha256="b" * 64,
            input_mode="generation_v1",
            actions=(
                {
                    "feature_date": "2026-07-01",
                    "target_date": "2026-07-02",
                    "frequency": "daily",
                },
            ),
            expected_target_keys=(
                {
                    "target_tenor": "1Y",
                    "horizon": 1,
                    "predict_date": "2026-07-02",
                    "feature_date": "2026-07-01",
                    "target_date": "2026-07-02",
                },
            ),
            input_authority={
                "artifact": {
                    "generation_id": "native-generation-1",
                    "manifest_uri": "/tmp/native-manifest.json",
                    "manifest_sha256": "1" * 64,
                    "dataset_content_id": "2" * 64,
                    "source_commit_token": "3" * 64,
                    "business_date": "2026-07-03",
                    "feature_date": "2026-07-01",
                    "exporter_version": (
                        signal_gap_fill_gate.SIGNAL_GAP_NATIVE_EXPORTER_VERSION
                    ),
                }
            },
            source_authority={},
        )
        native_execution = signal_gap_fill_gate._Execution(
            group=native_group,
            cfg=SimpleNamespace(scheme_id="native_trial"),
            run_id=2,
            started=0.0,
        )
        native_runner_calls: list[str] = []

        signal_gap_fill_gate._run_algorithms(
            [native_execution, blackbox_execution],
            engine=object(),
            algo_env="forecast_env",
            timeout_sec=600,
            data_bridge_config=object(),
            algorithm_runner=lambda cfg, *_args, **_kwargs: (
                native_runner_calls.append(cfg.scheme_id) or []
            ),
            native_generation_opener=lambda *_args, **_kwargs: SimpleNamespace(
                dispose=lambda: None
            ),
            blackbox_session_builder=lambda **_kwargs: (_ for _ in ()).throw(
                RuntimeError("frozen current no longer matches authority")
            ),
            blackbox_batch_runner=lambda *_args, **_kwargs: [],
        )

        self.assertEqual(native_runner_calls, [])
        self.assertIsInstance(blackbox_execution.error, RuntimeError)

    def test_any_source_session_failure_prevents_all_blackbox_batches(
        self,
    ) -> None:
        groups = _blackbox_groups(monthly_generation_id="current-2")
        third_authority = json.loads(
            json.dumps(groups[2].input_authority)
        )
        third_authority["generation_id"] = "current-3"
        executions = _executions_for(
            (
                groups[0],
                groups[1],
                replace(groups[2], input_authority=third_authority),
            )
        )
        batch_calls: list[str] = []
        session_builds: list[str] = []

        def session_builder(*, source_identity, **_kwargs):
            session_builds.append(source_identity["generation_id"])
            if source_identity["generation_id"] == "current-2":
                raise RuntimeError("second source is no longer current")
            return SimpleNamespace()

        signal_gap_fill_gate._run_algorithms(
            executions,
            engine=object(),
            algo_env="forecast_env",
            timeout_sec=600,
            data_bridge_config=object(),
            algorithm_runner=lambda *_args, **_kwargs: [],
            native_generation_opener=lambda *_args, **_kwargs: None,
            blackbox_session_builder=session_builder,
            blackbox_batch_runner=lambda cfg, **_kwargs: (
                batch_calls.append(cfg.scheme_id) or []
            ),
        )

        self.assertEqual(batch_calls, [])
        self.assertNotIn("current-3", session_builds)
        self.assertTrue(
            any(
                isinstance(item.error, RuntimeError)
                for item in executions
            )
        )

    def test_invalid_blackbox_authority_prevents_all_session_builds(
        self,
    ) -> None:
        groups = _blackbox_groups()
        invalid_group = replace(
            groups[0],
            input_authority={"cutoff": {"feature_date": "2026-07-01"}},
        )
        executions = _executions_for((invalid_group, groups[1]))
        session_builds: list[str] = []
        batch_calls: list[str] = []

        signal_gap_fill_gate._run_algorithms(
            executions,
            engine=object(),
            algo_env="forecast_env",
            timeout_sec=600,
            data_bridge_config=object(),
            algorithm_runner=lambda *_args, **_kwargs: [],
            native_generation_opener=lambda *_args, **_kwargs: None,
            blackbox_session_builder=lambda **_kwargs: (
                session_builds.append("built") or SimpleNamespace()
            ),
            blackbox_batch_runner=lambda cfg, **_kwargs: (
                batch_calls.append(cfg.scheme_id) or []
            ),
        )

        self.assertEqual(session_builds, [])
        self.assertEqual(batch_calls, [])
        self.assertIsNotNone(executions[0].error)

    def test_interrupt_is_not_converted_into_a_failed_run(self) -> None:
        executions = _executions_for(_blackbox_groups())[:1]

        def session_builder(**_kwargs):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            signal_gap_fill_gate._run_algorithms(
                executions,
                engine=object(),
                algo_env="forecast_env",
                timeout_sec=600,
                data_bridge_config=object(),
                algorithm_runner=lambda *_args, **_kwargs: [],
                native_generation_opener=lambda *_args, **_kwargs: None,
                blackbox_session_builder=session_builder,
                blackbox_batch_runner=lambda *_args, **_kwargs: [],
            )


if __name__ == "__main__":
    unittest.main()
