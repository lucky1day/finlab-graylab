from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import json

from harness.gates.signal_gap_fill_gate import (
    _Execution,
    _GapGroup,
    _run_algorithms,
    run_signal_gap_fill,
    signal_gap_fill_authorization_claims,
)
from harness.authorization import issue_signal_gap_fill_token
from shared.models import PredictionRecord
from tests.test_signal_gap_plan import _build_native_plan


def _native_execution(base_scheme_id: str) -> _Execution:
    action = {
        "registry_scheme_id": f"{base_scheme_id}__h1__5Y",
        "base_scheme_id": base_scheme_id,
        "runtime_type": "native_adapter",
        "frequency": "daily",
        "task_type": "T+1",
        "target_tenor": "5Y",
        "horizon": 1,
        "predict_date": "2026-07-20",
        "feature_date": "2026-07-17",
        "target_date": "2026-07-21",
        "prediction_phase": "gray_live",
        "scheme_version": "version-1",
        "code_sha256": "a" * 64,
        "config_sha256": "b" * 64,
        "input_authority": None,
    }
    group = _GapGroup(
        base_scheme_id=base_scheme_id,
        predict_date="2026-07-20",
        runtime_type="native_adapter",
        scheme_version="version-1",
        code_sha256="a" * 64,
        config_sha256="b" * 64,
        actions=(action,),
        expected_target_keys=(
            {
                "registry_scheme_id": action["registry_scheme_id"],
                "base_scheme_id": base_scheme_id,
                "target_tenor": "5Y",
                "horizon": 1,
                "task_type": "T+1",
                "predict_date": "2026-07-20",
                "feature_date": "2026-07-17",
                "target_date": "2026-07-21",
                "prediction_phase": "gray_live",
            },
        ),
        input_authority=None,
        source_authority=None,
    )
    return _Execution(
        group=group,
        cfg=SimpleNamespace(
            scheme_id=base_scheme_id,
            runtime_type="native_adapter",
        ),
        run_id=1,
        started=0.0,
    )


def _record(base_scheme_id: str) -> PredictionRecord:
    return PredictionRecord(
        scheme_id=base_scheme_id,
        target_tenor="5Y",
        horizon=1,
        predict_date="2026-07-20",
        feature_date="2026-07-17",
        target_date="2026-07-21",
        predicted_direction=1,
        extra={
            "input_artifact_path": "/tmp/input.csv",
            "input_artifact_source": "database",
            "daily_input_artifact_hash": "temporary",
            "weekly_input_artifact_watermark": "temporary",
            "monthly_input_artifact_path": "/tmp/monthly.csv",
            "input_cutoff_date": "2026-07-17",
            "phase_a_cache": {"temporary": True},
            "phase_a_cache_generation_id": "temporary",
            "source_package_hash": "keep-source-package",
            "source_model_id": "keep-model",
            "vote_score": 0.7,
            "probability": 0.8,
        },
    )


def test_native_groups_run_once_in_distinct_private_workspaces() -> None:
    executions = [
        _native_execution("native_one"),
        _native_execution("native_two"),
    ]
    calls: list[tuple[str, Path, dict[str, object]]] = []

    def runner(cfg, predict_date, **kwargs):
        root = Path(kwargs["ephemeral_native_runtime_root"])
        assert root.is_absolute()
        assert root.exists()
        calls.append((cfg.scheme_id, root, kwargs))
        return [_record(cfg.scheme_id)]

    _run_algorithms(
        executions,
        engine=object(),
        algo_env="forecast_env",
        timeout_sec=600,
        data_bridge_config=SimpleNamespace(),
        algorithm_runner=runner,
        blackbox_session_builder=Mock(),
        blackbox_batch_runner=Mock(),
    )

    assert sorted(scheme_id for scheme_id, _, _ in calls) == [
        "native_one",
        "native_two",
    ]
    roots = [root for _, root, _ in calls]
    assert len(set(roots)) == 2
    assert all(not root.exists() for root in roots)
    for _, _, kwargs in calls:
        assert "native_generation" not in kwargs
        assert "native_execution_mode" not in kwargs
        assert "live_source_compatibility" not in kwargs
        assert "phase_a_cache_root" not in kwargs
    for execution in executions:
        assert execution.error is None
        extra = execution.records[0].extra
        for removed in (
            "input_artifact_path",
            "input_artifact_source",
            "daily_input_artifact_hash",
            "weekly_input_artifact_watermark",
            "monthly_input_artifact_path",
            "input_cutoff_date",
            "phase_a_cache",
            "phase_a_cache_generation_id",
        ):
            assert removed not in extra
        assert extra["source_package_hash"] == "keep-source-package"
        assert extra["source_model_id"] == "keep-model"
        assert extra["vote_score"] == 0.7
        assert extra["probability"] == 0.8


def test_native_algorithm_failure_is_exposed_before_any_commit() -> None:
    executions = [
        _native_execution("native_ok"),
        _native_execution("native_failed"),
    ]

    def runner(cfg, predict_date, **kwargs):
        if cfg.scheme_id == "native_failed":
            raise RuntimeError("algorithm failed")
        return [_record(cfg.scheme_id)]

    _run_algorithms(
        executions,
        engine=object(),
        algo_env="forecast_env",
        timeout_sec=600,
        data_bridge_config=SimpleNamespace(),
        algorithm_runner=runner,
        blackbox_session_builder=Mock(),
        blackbox_batch_runner=Mock(),
    )

    assert executions[0].records is not None
    assert executions[1].error is not None


def test_algorithm_failure_commits_no_predictions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HARNESS_AUTH_SECRET", "test-secret")
    plan = _build_native_plan()
    plan_path = tmp_path / "frozen_plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    claim = signal_gap_fill_authorization_claims(plan)[0]
    token = issue_signal_gap_fill_token(
        plan_sha256=plan["plan_sha256"],
        base_scheme_id=claim.base_scheme_id,
        predict_date=claim.predict_date,
        target_keys=claim.target_keys,
        scheme_version=claim.scheme_version,
        source_authority=claim.source_authority,
    )
    repository = SimpleNamespace(
        create_scheme_run=Mock(return_value=101),
        fail_scheme_run_atomic=Mock(),
        complete_gray_gap_run=Mock(
            side_effect=AssertionError("prediction commit must not run")
        ),
    )
    lock = SimpleNamespace(acquire=Mock(), release=Mock())
    action = plan["actions"][0]
    config = SimpleNamespace(
        scheme_id=action["base_scheme_id"],
        scheme_version=action["scheme_version"],
        runtime_type=action["runtime_type"],
        code_hash=action["code_sha256"],
        config_hash=action["config_sha256"],
    )

    report = run_signal_gap_fill(
        plan_path=plan_path,
        authorizations=(token,),
        project_root=tmp_path,
        engine_factory=lambda: object(),
        databridge_config=SimpleNamespace(),
        planner=lambda *_args, **_kwargs: plan,
        config_loader=lambda _path: config,
        algorithm_runner=Mock(side_effect=RuntimeError("algorithm failed")),
        repository_module=repository,
        blackbox_session_builder=Mock(),
        blackbox_batch_runner=Mock(),
        singleton_lock_factory=lambda: lock,
    )

    assert report["status"] == "FAILED"
    assert report["failure_code"] == "ALGORITHM_EXECUTION_FAILED"
    repository.complete_gray_gap_run.assert_not_called()
    repository.fail_scheme_run_atomic.assert_called_once()
