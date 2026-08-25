from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock

import pytest

from shared.exclusive_file_lock import ExclusiveFileLockUnavailable
from shared.models import PredictionRecord


PREDICT_DATE = "2026-08-10"
FEATURE_DATE = "2026-08-07"
TARGET_DATE = "2026-08-14"


def _source_identity() -> dict[str, object]:
    files = []
    for filename, min_key, max_key, character in (
        ("daily_output.csv", "2026-08-01", FEATURE_DATE, "a"),
        ("monthly_output.csv", "202607", "202608", "c"),
        ("weekly_output.csv", "202631", "202632", "b"),
    ):
        files.append(
            {
                "filename": filename,
                "rows": 2,
                "columns": 3,
                "min_key": min_key,
                "max_key": max_key,
                "sha256": character * 64,
                "business_hash": character * 64,
            }
        )
    return {
        "generation_id": "generation-1",
        "refresh_date": PREDICT_DATE,
        "schema_version": "data-bridge-v1",
        "business_digest": "d" * 64,
        "stable_identity_sha256": "e" * 64,
        "files": files,
    }


def _action(
    base_scheme_id: str = "demo_native",
    *,
    tenor: str = "5Y",
    action: str = "GRAY_LIVE_GAP",
    runtime_type: str = "native_adapter",
) -> dict[str, object]:
    authority = None
    if runtime_type == "blackbox_v2" and action == "GRAY_LIVE_GAP":
        authority = {
            **_source_identity(),
            "cutoff": {
                "feature_date": FEATURE_DATE,
                "daily_cutoff_key": FEATURE_DATE,
                "weekly_cutoff_key": "202632",
                "monthly_cutoff_key": "202608",
                "source_weekly_cutoff_key": None,
                "source_monthly_cutoff_key": None,
            },
        }
    return {
        "registry_scheme_id": f"{base_scheme_id}__h5__{tenor}",
        "base_scheme_id": base_scheme_id,
        "runtime_type": runtime_type,
        "frequency": "daily",
        "task_type": "T+5",
        "target_tenor": tenor,
        "horizon": 5,
        "predict_date": PREDICT_DATE,
        "feature_date": FEATURE_DATE,
        "target_date": TARGET_DATE,
        "prediction_phase": "gray_live",
        "scheme_version": "version-1",
        "business_key": [base_scheme_id, tenor, 5, TARGET_DATE],
        "action": action,
        "reason": (
            "LIVE_BUSINESS_KEY_MISSING"
            if action == "GRAY_LIVE_GAP"
            else (
                "VALID_LIVE_RESULT_PRESENT"
                if action == "SKIP_PRESENT"
                else "SCHEME_NOT_DUE"
            )
        ),
        "business_key_present": action == "SKIP_PRESENT",
        "input_authority": authority,
    }


def _plan(
    actions: list[dict[str, object]],
    *,
    base_scheme_id: str | None = None,
    status: str = "READY",
) -> dict[str, object]:
    counts = {
        "active_target": len(actions),
        "expected": sum(row["action"] != "SKIP_NOT_DUE" for row in actions),
        "present": sum(row["action"] == "SKIP_PRESENT" for row in actions),
        "actionable": sum(row["action"] == "GRAY_LIVE_GAP" for row in actions),
        "blocked": int(status == "BLOCKED"),
        "SKIP_PRESENT": sum(row["action"] == "SKIP_PRESENT" for row in actions),
        "SKIP_NOT_DUE": sum(row["action"] == "SKIP_NOT_DUE" for row in actions),
        "GRAY_LIVE_GAP": sum(row["action"] == "GRAY_LIVE_GAP" for row in actions),
        "BLOCKED_NO_GENERATION": 0,
        "BLOCKED_DATA_CONTRACT": 0,
    }
    return {
        "schema_version": "single-date-active-live-gap-plan-v1",
        "status": status,
        "failure_code": "PLAN_BLOCKED" if status == "BLOCKED" else None,
        "predict_date": PREDICT_DATE,
        "base_scheme_id": base_scheme_id,
        "counts": counts,
        "actions": actions,
    }


def _present(plan: dict[str, object]) -> dict[str, object]:
    actions = []
    for raw in plan["actions"]:
        row = dict(raw)
        if row["action"] == "GRAY_LIVE_GAP":
            row.update(
                action="SKIP_PRESENT",
                reason="VALID_LIVE_RESULT_PRESENT",
                business_key_present=True,
                input_authority=None,
            )
        actions.append(row)
    return _plan(
        actions,
        base_scheme_id=plan.get("base_scheme_id"),
    )


def _config(
    base_scheme_id: str,
    *,
    runtime_type: str = "native_adapter",
    tenors: tuple[str, ...] = ("5Y",),
) -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id=base_scheme_id,
        scheme_version="version-1",
        runtime_type=runtime_type,
        frequency="daily",
        task_type="T+5",
        horizon=5,
        tenors=list(tenors),
        status="active",
        version_status="active",
        input_source=(
            "data_bridge_current" if runtime_type == "blackbox_v2" else None
        ),
    )


def _record(action: dict[str, object]) -> PredictionRecord:
    extra: dict[str, object] = {"vote_score": 0.7}
    authority = action.get("input_authority")
    if authority:
        cutoff = authority["cutoff"]
        extra.update(
            {
                "data_generation_id": authority["generation_id"],
                "source_refresh_date": authority["refresh_date"],
                "daily_cutoff_key": cutoff["daily_cutoff_key"],
                "weekly_cutoff_key": cutoff["weekly_cutoff_key"],
                "monthly_cutoff_key": cutoff["monthly_cutoff_key"],
                "request_id": (
                    f"{action['base_scheme_id']}:{PREDICT_DATE}:"
                    f"{FEATURE_DATE}:{TARGET_DATE}"
                ),
                "data_snapshot_id": "snapshot-1",
            }
        )
    return PredictionRecord(
        scheme_id=str(action["base_scheme_id"]),
        target_tenor=str(action["target_tenor"]),
        horizon=int(action["horizon"]),
        predict_date=str(action["predict_date"]),
        feature_date=str(action["feature_date"]),
        target_date=str(action["target_date"]),
        prediction_phase="gray_live",
        scheme_version="version-1",
        predicted_direction=1,
        extra=extra,
    )


def _repository(*, commit_side_effect: object = 1) -> SimpleNamespace:
    run_ids = iter(range(101, 1000))
    complete = (
        Mock(return_value=1)
        if commit_side_effect == 1
        else Mock(side_effect=commit_side_effect)
    )
    return SimpleNamespace(
        create_scheme_run=Mock(side_effect=lambda *_a, **_k: next(run_ids)),
        complete_gray_gap_run=complete,
        fail_scheme_run_atomic=Mock(),
    )


def _lock() -> SimpleNamespace:
    return SimpleNamespace(acquire=Mock(), release=Mock())


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    repository: SimpleNamespace,
    plan: dict[str, object],
    runner: Mock | object,
    configs: dict[str, SimpleNamespace] | None = None,
    lock: SimpleNamespace | None = None,
    readback: Mock | None = None,
) -> tuple[SimpleNamespace, Mock]:
    from harness import signal_gap_fill

    resolved_configs = configs or {
        str(row["base_scheme_id"]): _config(str(row["base_scheme_id"]))
        for row in plan["actions"]
    }
    readback_mock = readback or Mock(return_value=_present(plan))
    monkeypatch.setattr(
        signal_gap_fill,
        "load_scheme_config",
        lambda path: resolved_configs[path.parent.name],
    )
    monkeypatch.setattr(signal_gap_fill, "run_configured_scheme", runner)
    monkeypatch.setattr(signal_gap_fill, "_repository_module", lambda: repository)
    monkeypatch.setattr(
        signal_gap_fill,
        "_signal_gap_fill_singleton_lock",
        lambda: lock or _lock(),
    )
    monkeypatch.setattr(signal_gap_fill, "plan_signal_gaps", readback_mock)
    return SimpleNamespace(dispose=Mock()), readback_mock


def test_public_api_accepts_only_in_memory_plan_without_tokens() -> None:
    from harness import signal_gap_fill

    signature = inspect.signature(signal_gap_fill.run_signal_gap_fill)

    assert list(signature.parameters) == [
        "plan",
        "project_root",
        "engine_factory",
        "databridge_config",
        "algo_env",
        "timeout_sec",
    ]
    assert signature.parameters["plan"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["algo_env"].default == "forecast_env"
    assert signature.parameters["timeout_sec"].default == 600
    for removed in (
        "SignalGapFillGate",
        "signal_gap_fill_authorization_claims",
        "mark_token_used",
    ):
        assert not hasattr(signal_gap_fill, removed)


@pytest.mark.parametrize(
    ("actions", "expected_status"),
    [
        ([_action(action="SKIP_NOT_DUE")], "SKIP_NOT_DUE"),
        ([_action(action="SKIP_PRESENT")], "SKIP_PRESENT"),
    ],
)
def test_non_actionable_plan_skips_without_engine_or_algorithm(
    tmp_path: Path,
    actions: list[dict[str, object]],
    expected_status: str,
) -> None:
    from harness.signal_gap_fill import run_signal_gap_fill

    engine_factory = Mock(side_effect=AssertionError("engine must not open"))

    report = run_signal_gap_fill(
        plan=_plan(actions, base_scheme_id="demo_native"),
        project_root=tmp_path,
        engine_factory=engine_factory,
        databridge_config=SimpleNamespace(),
    )

    assert report["status"] == expected_status
    engine_factory.assert_not_called()


def test_multi_target_scheme_runs_once_and_commits_only_missing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness.signal_gap_fill import run_signal_gap_fill

    present = _action(tenor="5Y", action="SKIP_PRESENT")
    missing = _action(tenor="10Y")
    plan = _plan([present, missing])
    algorithm = Mock(return_value=[_record(present), _record(missing)])
    repository = _repository()
    lock = _lock()
    engine, readback = _install(
        monkeypatch,
        repository=repository,
        plan=plan,
        runner=algorithm,
        configs={"demo_native": _config("demo_native", tenors=("5Y", "10Y"))},
        lock=lock,
    )

    report = run_signal_gap_fill(
        plan=plan,
        project_root=tmp_path,
        engine_factory=lambda: engine,
        databridge_config=SimpleNamespace(),
    )

    assert report["status"] == "PASSED", report["errors"]
    algorithm.assert_called_once()
    workspace = Path(algorithm.call_args.kwargs["ephemeral_native_runtime_root"])
    assert workspace.is_absolute()
    assert not workspace.exists()
    assert "native_generation" not in algorithm.call_args.kwargs
    commit = repository.complete_gray_gap_run.call_args
    assert [record.target_tenor for record in commit.kwargs["records"]] == ["10Y"]
    assert [row["target_tenor"] for row in commit.kwargs["expected_target_keys"]] == [
        "10Y"
    ]
    readback.assert_called_once()
    lock.release.assert_called_once()






def test_any_algorithm_failure_commits_zero_predictions_and_fails_all_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness.signal_gap_fill import run_signal_gap_fill

    plan = _plan([_action("a_native"), _action("b_native")])

    def execute(cfg: SimpleNamespace, _date: str, **_kwargs: object):
        if cfg.scheme_id == "b_native":
            raise RuntimeError("algorithm failed")
        return [_record(_action(cfg.scheme_id))]

    repository = _repository()
    engine, readback = _install(
        monkeypatch,
        repository=repository,
        plan=plan,
        runner=Mock(side_effect=execute),
    )

    report = run_signal_gap_fill(
        plan=plan,
        project_root=tmp_path,
        engine_factory=lambda: engine,
        databridge_config=SimpleNamespace(),
    )

    assert report["status"] == "FAILED"
    assert report["failure_code"] == "ALGORITHM_EXECUTION_FAILED"
    repository.complete_gray_gap_run.assert_not_called()
    assert repository.fail_scheme_run_atomic.call_count == 2
    readback.assert_not_called()


def test_commit_conflict_returns_completed_and_remaining_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness.signal_gap_fill import run_signal_gap_fill

    first = _action("a_native")
    second = _action("b_native")
    plan = _plan([first, second])

    def execute(cfg: SimpleNamespace, _date: str, **_kwargs: object):
        return [_record(first if cfg.scheme_id == "a_native" else second)]

    repository = _repository(
        commit_side_effect=[1, RuntimeError("business key conflict")]
    )
    engine, readback = _install(
        monkeypatch,
        repository=repository,
        plan=plan,
        runner=Mock(side_effect=execute),
    )

    report = run_signal_gap_fill(
        plan=plan,
        project_root=tmp_path,
        engine_factory=lambda: engine,
        databridge_config=SimpleNamespace(),
    )

    assert report["status"] == "FAILED"
    assert report["failure_code"] == "GRAY_GAP_COMMIT_FAILED"
    assert [item["base_scheme_id"] for item in report["completed"]] == [
        "a_native"
    ]
    assert [item["base_scheme_id"] for item in report["remaining"]] == [
        "b_native"
    ]
    assert repository.complete_gray_gap_run.call_count == 2
    repository.fail_scheme_run_atomic.assert_called_once()
    readback.assert_not_called()


def test_only_one_final_authoritative_readback_can_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness.signal_gap_fill import run_signal_gap_fill

    action = _action()
    plan = _plan([action])
    repository = _repository()
    readback = Mock(return_value=_present(plan))
    engine, _ = _install(
        monkeypatch,
        repository=repository,
        plan=plan,
        runner=Mock(return_value=[_record(action)]),
        readback=readback,
    )

    report = run_signal_gap_fill(
        plan=plan,
        project_root=tmp_path,
        engine_factory=lambda: engine,
        databridge_config=SimpleNamespace(),
    )

    assert report["status"] == "PASSED"
    readback.assert_called_once_with(
        engine,
        predict_date=PREDICT_DATE,
        base_scheme_id=None,
        databridge_config=ANY,
    )




def test_singleton_contention_blocks_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness.signal_gap_fill import run_signal_gap_fill

    action = _action()
    plan = _plan([action])
    lock = _lock()
    lock.acquire.side_effect = ExclusiveFileLockUnavailable("held")
    repository = _repository()
    engine, _ = _install(
        monkeypatch,
        repository=repository,
        plan=plan,
        runner=Mock(return_value=[_record(action)]),
        lock=lock,
    )
    engine_factory = Mock(return_value=engine)

    report = run_signal_gap_fill(
        plan=plan,
        project_root=tmp_path,
        engine_factory=engine_factory,
        databridge_config=SimpleNamespace(),
    )

    assert report["status"] == "BLOCKED"
    assert report["failure_code"] == "SIGNAL_GAP_FILL_ALREADY_RUNNING"
    engine_factory.assert_not_called()
    repository.create_scheme_run.assert_not_called()




def test_blackbox_schemes_with_same_source_share_immutable_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness import signal_gap_fill

    first = _action("a_blackbox", runtime_type="blackbox_v2")
    second = _action("b_blackbox", runtime_type="blackbox_v2")
    plan = _plan([first, second])
    repository = _repository()
    engine, _ = _install(
        monkeypatch,
        repository=repository,
        plan=plan,
        runner=Mock(side_effect=AssertionError("Native runner must not run")),
        configs={
            "a_blackbox": _config("a_blackbox", runtime_type="blackbox_v2"),
            "b_blackbox": _config("b_blackbox", runtime_type="blackbox_v2"),
        },
    )
    session = SimpleNamespace(source_identity=_source_identity())
    session_builder = Mock(return_value=session)

    def batch_runner(cfg: SimpleNamespace, *, requests, **_kwargs):
        action = first if cfg.scheme_id == "a_blackbox" else second
        record = _record(action)
        return [
            replace(
                record,
                extra={
                    **dict(record.extra or {}),
                    "request_id": requests[0].request_id,
                },
            )
        ]

    monkeypatch.setattr(
        signal_gap_fill,
        "build_blackbox_gray_replay_session",
        session_builder,
    )
    monkeypatch.setattr(
        signal_gap_fill,
        "run_blackbox_gray_replay_batch",
        Mock(side_effect=batch_runner),
    )

    report = signal_gap_fill.run_signal_gap_fill(
        plan=plan,
        project_root=tmp_path,
        engine_factory=lambda: engine,
        databridge_config=SimpleNamespace(),
    )

    assert report["status"] == "PASSED", report["errors"]
    session_builder.assert_called_once()
    assert signal_gap_fill.run_blackbox_gray_replay_batch.call_count == 2
