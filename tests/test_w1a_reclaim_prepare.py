"""W1A 准备的调用预算、真实进程计数和失败留痕；算法均为 mock。"""

from contextlib import nullcontext
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from harness import w1a_reclaim_prepare as prepare, w1a_writer_reclaim as control
from scheduler import repository as repo
from scheduler.blackbox_v2_runner import _validate_runtime_profile
from scheduler.discovery import load_scheme_config
from shared.models import PredictionRecord
from test_blackbox_multi_target import _scheme


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    candidate = tmp_path / "releases" / "candidate"
    new = {key: replace(load_scheme_config(_scheme(candidate, key)[0]), environment_fingerprint="env",
                        data_snapshot_id="snapshot") for key in control.IDS}
    old = {key: replace(cfg, runtime_type="native_adapter", scheme_version="native", code_hash="a" * 64) for key, cfg in new.items()}
    snapshot = SimpleNamespace(snapshot_id="snapshot", generation_id="generation", refresh_date="2026-09-12",
                               root_dir=tmp_path / "input/generation", data_dir=tmp_path / "input/generation/data")
    requests = {key: {tenor: {"request_id": key + ":request", "predict_date": "2026-09-11", "feature_date": "2026-09-10",
                             "target_date": "2026-09-11" if key == "t1_daily" else "2026-09-17",
                             "daily_cutoff_key": "2026-09-10", "weekly_cutoff_key": "202636", "monthly_cutoff_key": "202608"}
                     for tenor in cfg.tenors} for key, cfg in new.items()}
    evidence = {"databridge": {"generation_id": "generation", "data_snapshot_id": "snapshot"},
                "equivalence": {key: {"original_vintage": "old-input"} for key in new},
                "release": {"reference": {"commit": control.NATIVE_REFERENCE}, "rollback": {"commit": control.ROLLBACK_RELEASE}},
                "blackbox_environment_fingerprint": "env"}
    plan = {"predict_date": "2026-09-11", "requests": requests, "control": evidence,
            "database_identity_sha256": "identity"}
    capture = Mock(return_value=(old, new, snapshot, plan))
    monkeypatch.setattr(prepare, "_capture", capture)
    locks = []
    monkeypatch.setattr(repo, "_blackbox_activation_advisory_lock", lambda engine, scheme_id: (locks.append(scheme_id), nullcontext())[1])
    starts, completions = Mock(return_value=True), Mock(return_value=True)
    monkeypatch.setattr(prepare, "persist_harness_run_start", starts)
    monkeypatch.setattr(prepare, "persist_harness_run_complete", completions)
    failed = Mock()
    monkeypatch.setattr(prepare, "_record_failed_gate", failed)
    calls = []

    def execute(cfg, day, **kwargs):
        _validate_runtime_profile(kwargs["profile"], source="W1A fixture")
        assert kwargs["profile"].memory_limit_bytes == 4 * 1024**3
        assert kwargs["profile"].cpu_threads == 8
        assert kwargs["profile"].predict_timeout_sec == kwargs["timeout_sec"] == 120
        assert kwargs["expected_generation_id"] == snapshot.generation_id
        calls.append(cfg.scheme_id)
        records = []
        for index, tenor in enumerate(cfg.tenors):
            kwargs["process_started"](100 + index, 100 + index)
            request = requests[cfg.scheme_id][tenor]
            records.append(PredictionRecord(scheme_id=cfg.scheme_id, target_tenor=tenor, horizon=cfg.horizon,
                predict_date=request["predict_date"], feature_date=request["feature_date"], target_date=request["target_date"],
                predicted_direction=-1, extra={"request_id": request["request_id"], "data_snapshot_id": "snapshot"}))
        return records

    runner = Mock(side_effect=execute)
    monkeypatch.setattr(prepare, "run_blackbox_scheme_subprocess", runner)
    kwargs = {"project_root": candidate, "reference_project_root": tmp_path / "releases/reference",
              "rollback_project_root": tmp_path / "releases/rollback", "predict_date": None,
              "expected_database_name": "isolated", "expected_server_uuid": "isolated"}
    return SimpleNamespace(new=new, plan=plan, kwargs=kwargs, locks=locks, capture=capture, starts=starts,
                           completions=completions, failed=failed, runner=runner, calls=calls, execute=execute,
                           work=tmp_path / "private/work", snapshot=snapshot)


def _apply(fixture, **overrides):
    fixture.work.parent.mkdir(exist_ok=True)
    return prepare.execute_w1a_reclaim_prepare(object(), **fixture.kwargs,
        **({"expected_plan_sha256": repo.native_successor_plan_sha256(fixture.plan), "approved_by": "test",
            "work_dir": fixture.work} | overrides))


def test_two_base_calls_six_processes_two_gates_no_business_writes(prepared):
    result = _apply(prepared)
    assert prepared.locks == list(control.ALL_IDS)
    assert prepared.calls == list(control.IDS)
    assert result["algorithm_executions"] == 6
    assert result["prediction_written"] is result["state_written"] is False
    assert len(list(prepared.work.glob("*.process.json"))) == 6
    assert prepared.completions.call_count == 2
    for call in prepared.completions.call_args_list:
        gate = call.kwargs["results"][0]
        assert gate.passed
        proof = gate.evidence[0].value
        assert proof["old_identity"]["scheme_version"] == "native"
        assert proof["new_identity"]["runtime_type"] == "blackbox_v2"
    readiness = control.read_readiness(prepared.work, prepared.new, prepared.plan["control"])
    assert readiness["harness_run_ids"] == result["harness_run_ids"]
    assert readiness["local_execution_sha256"] == result["local_execution_sha256"]
    prepared.failed.assert_not_called()


def test_prepare_persists_mysql_plan_types_without_changing_approved_digest(prepared):
    prepared.plan['database_rows'] = [{'created_at': datetime(2026, 9, 12, 5, 0, 1),
                                     'deployed_at': date(2026, 9, 12), 'score': Decimal('0.50')}]
    result = _apply(prepared)
    saved = json.loads((prepared.work / 'plan.json').read_text())
    assert repo.native_successor_plan_sha256(saved) == result['plan_sha256']
    assert saved['database_rows'][0] == {'created_at': '2026-09-12 05:00:01',
                                       'deployed_at': '2026-09-12', 'score': '0.50'}
    assert control.read_readiness(prepared.work, prepared.new, prepared.plan['control'])['harness_run_ids'] == result['harness_run_ids']


@pytest.mark.parametrize("failure", ["bad_result", "missing_target", "timeout", "start", "commit"])
def test_failure_keeps_original_receipts_and_does_not_run_next_base(prepared, failure):
    def execute(cfg, day, **kwargs):
        records = prepared.execute(cfg, day, **kwargs)
        if failure == "timeout":
            raise TimeoutError("bounded algorithm stderr: timeout")
        if failure == "bad_result":
            return [replace(records[0], predicted_direction=7), *records[1:]]
        if failure == "missing_target":
            return records[:1]
        return records
    prepared.runner.side_effect = execute
    if failure == "start":
        prepared.starts.return_value = False
    if failure == "commit":
        prepared.completions.return_value = False
    with pytest.raises((RuntimeError, TimeoutError)):
        _apply(prepared)
    assert prepared.calls == ([] if failure == "start" else ["t1_daily"])
    prepared.failed.assert_called_once()
    receipt = json.loads((prepared.work / "t1_daily.failed.json").read_text())
    assert receipt["algorithm_started"] is (failure != "start")
    assert bool(receipt["processes"]) is (failure != "start")
    assert not (prepared.work / "complete.json").exists()


@pytest.mark.parametrize("invalid", ["plan", "release", "input", "exists"])
def test_preexecution_rejections_create_no_process_or_gate(prepared, invalid):
    overrides = {}
    if invalid == "plan":
        overrides["expected_plan_sha256"] = "0" * 64
    elif invalid == "release":
        overrides["work_dir"] = prepared.kwargs["project_root"] / "outputs/private"
    elif invalid == "input":
        overrides["work_dir"] = prepared.snapshot.root_dir / "private"
    else:
        prepared.work.mkdir(parents=True)
    with pytest.raises((ValueError, RuntimeError)):
        _apply(prepared, **overrides)
    prepared.runner.assert_not_called()
    prepared.starts.assert_not_called()
    if invalid != "exists":
        assert not Path(overrides.get("work_dir", prepared.work)).exists()


def test_failure_receipt_error_does_not_hide_failure_or_skip_audit(prepared, monkeypatch):
    writer = prepare._write_receipt
    def fail(path, payload):
        if path.name.endswith("execution.json") or path.name.endswith("failed.json"):
            raise OSError("disk full")
        return writer(path, payload)
    monkeypatch.setattr(prepare, "_write_receipt", fail)
    with pytest.raises(OSError, match="disk full") as error:
        _apply(prepared)
    prepared.failed.assert_called_once()
    assert error.value.__notes__


@pytest.mark.parametrize("drift", ["input", "environment", "release", "execution"])
def test_readiness_rejects_drift_after_success(prepared, drift):
    _apply(prepared)
    evidence = json.loads(json.dumps(prepared.plan["control"]))
    if drift == "input":
        evidence["databridge"]["data_snapshot_id"] = "changed"
    elif drift == "environment":
        evidence["blackbox_environment_fingerprint"] = "changed"
    elif drift == "release":
        evidence["release"]["rollback"]["commit"] = "changed"
    else:
        path = prepared.work / "t5_daily.execution.json"
        path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(RuntimeError):
        control.read_readiness(prepared.work, prepared.new, evidence)


def test_equivalence_binds_all_six_targets_without_relabeling_old_vintage(tmp_path, monkeypatch):
    old, sources, new, conversions, comparisons = {}, {}, {}, {}, []
    for key, targets in control.MULTI_TARGET_DELIVERIES.items():
        horizon, task = (1, "T+1") if key == "t1_daily" else (5, "T+5")
        old[key] = SimpleNamespace(code_hash="a" * 64, horizon=horizon, task_type=task)
        new[key] = SimpleNamespace(horizon=horizon)
        conversions[key] = {}
        for tenor, alias in targets.items():
            sources[alias] = SimpleNamespace(code_hash="b" * 64)
            conversions[key][alias] = {"source_scheme_id": alias, "scheme_id": key}
            comparisons.append({"old_base_scheme_id": key, "new_base_scheme_id": alias, "target_tenor": tenor,
                "task_type": task, "old_horizon": horizon, "new_horizon": horizon,
                "request_count": 337 if horizon == 1 else 333, "old_code_hash": "a" * 64, "new_code_hash": "b" * 64,
                "native_result_sha256": "c" * 64, "successor_result_sha256": "c" * 64,
                **{field: 0 for field in ("request_id_mismatch_count", "predict_date_mismatch_count", "feature_date_mismatch_count",
                                         "target_date_mismatch_count", "direction_mismatch_count")}})
    report = {"wave": "W1A", "targets": comparisons, "producer": {"tool": "original-comparator"},
              "generation_id": "original-vintage", "data_snapshot_id": "original-snapshot",
              "data_files_sha256": {"daily_output.csv": "d" * 64},
              "runtime_environment_fingerprint": "original-environment"}
    path = tmp_path / "deploy/native_successor_equivalence/W1A.json"
    path.parent.mkdir(parents=True)
    payload = json.dumps(report).encode()
    path.write_bytes(payload)
    monkeypatch.setattr(control, "REPORT_SHA", hashlib.sha256(payload).hexdigest())
    proof = control._equivalence(tmp_path, old, sources, new, conversions)
    assert sum(len(item["comparisons"]) for item in proof.values()) == 6
    assert all(item["input_identity"] == {field: report[field] for field in (
        "generation_id", "data_snapshot_id", "data_files_sha256")} for item in proof.values())
    assert proof["t5_daily"]["runtime_environment_fingerprint"] == "original-environment"
    sources[control.ALIASES[-1]].code_hash = "changed"
    with pytest.raises(RuntimeError, match="original algorithms"):
        control._equivalence(tmp_path, old, sources, new, conversions)
    path.write_bytes(payload + b"\n")
    with pytest.raises(RuntimeError, match="report changed"):
        control._equivalence(tmp_path, old, sources, new, conversions)


@pytest.mark.parametrize("action", ["prepare", "cutover", "rollback"])
def test_cli_routes_w1a_only_to_its_closed_entrypoint(tmp_path, monkeypatch, action):
    from harness import cli

    args = ["migrate-native-successor", "preflight", "--wave", "W1A", "--action", action,
            "--project-root", str(tmp_path / "candidate"), "--reference-project-root", str(tmp_path / "native"),
            "--rollback-project-root", str(tmp_path / "rollback"), "--expected-database-name", "isolated",
            "--expected-server-uuid", "isolated"]
    if action != "prepare":
        args += ["--work-dir", str(tmp_path / "work"), "--harness-run-id", "t1_daily=one", "--harness-run-id", "t5_daily=two"]
    operation = Mock(return_value={"plan": "mock"})
    monkeypatch.setattr(prepare if action == "prepare" else control,
                        "build_w1a_reclaim_prepare_preflight" if action == "prepare" else "build_w1a_reclaim_preflight", operation)
    engine = Mock()
    monkeypatch.setattr(cli, "create_engine_from_env", Mock(return_value=engine))
    assert cli._run_native_successor_migration_command(cli._build_parser().parse_args(args)) == {"plan": "mock"}
    operation.assert_called_once()
    assert operation.call_args.kwargs["rollback_project_root"] == tmp_path / "rollback"
    if action != "prepare":
        assert operation.call_args.kwargs["harness_run_ids"] == {"t1_daily": "one", "t5_daily": "two"}
    engine.dispose.assert_called_once()


@pytest.mark.parametrize("invalid", ["rollback_missing", "history_import", "run_coverage"])
def test_cli_rejects_missing_scope_before_engine(tmp_path, monkeypatch, invalid):
    from harness import cli

    args = ["migrate-native-successor", "preflight", "--wave", "W1A", "--action", "prepare",
            "--reference-project-root", str(tmp_path), "--expected-database-name", "isolated", "--expected-server-uuid", "isolated"]
    if invalid != "rollback_missing":
        args += ["--rollback-project-root", str(tmp_path / "rollback")]
    if invalid == "history_import":
        args[args.index("prepare")] = "preserve-live"
    elif invalid == "run_coverage":
        args[args.index("prepare")] = "cutover"
        args += ["--work-dir", str(tmp_path / "work"), "--harness-run-id", "t1_daily=one"]
    engine = Mock()
    monkeypatch.setattr(cli, "create_engine_from_env", engine)
    with pytest.raises(SystemExit if invalid == "history_import" else ValueError):
        cli._run_native_successor_migration_command(cli._build_parser().parse_args(args))
    engine.assert_not_called()


@pytest.mark.parametrize("drift", [None, "reference", "rollback", "matrix", "alias_bytes", "candidate_metadata"])
def test_release_pair_binds_reference_rollback_matrix_and_each_delivery(tmp_path, monkeypatch, drift):
    from shared.blackbox_v2.intake import intake_delivery

    candidate, reference, rollback = [tmp_path / name for name in ("candidate", "reference", "rollback")]
    original_mapping = Path(__file__).resolve().parents[1] / "deploy/native_to_blackbox_migration_v1.json"
    (candidate / "deploy").mkdir(parents=True)
    shutil.copyfile(original_mapping, candidate / "deploy/native_to_blackbox_migration_v1.json")
    for key, targets in control.MULTI_TARGET_DELIVERIES.items():
        cfg = load_scheme_config(_scheme(candidate / "schemes", key)[0])
        native = {"scheme_id": key, "runtime_type": "native_adapter", "name": "Native fixture", "description": "Fixture",
                  "horizon": cfg.horizon, "task_type": cfg.task_type, "frequency": "daily", "tenors": cfg.tenors,
                  "schedule": {"cron": "3 7 * * 1-5", "timezone": "Asia/Shanghai"}, "status": "active",
                  "input_spec": {"data_version": "fixture", "required_columns": ["date"]}}
        native_path = reference / "schemes" / key / "config.yaml"
        native_path.parent.mkdir(parents=True)
        native_path.write_text(yaml.safe_dump(native))
        for delivery in cfg.blackbox_deliveries:
            alias = targets[delivery.metadata.target_tenor]
            incoming = tmp_path / "incoming" / alias
            incoming.mkdir(parents=True)
            shutil.copyfile(delivery.script_path, incoming / f"{alias}.py")
            metadata = json.loads(delivery.metadata_path.read_text())
            metadata["scheme_id"] = alias
            (incoming / f"{alias}.json").write_text(json.dumps(metadata))
            for root in (candidate, reference, rollback):
                received = intake_delivery(incoming, schemes_root=root / "schemes")
                path = received / "config.yaml"
                path.write_text(path.read_text() + "factor_input_mode: algorithm_managed\n")
    matrix = {key: ["mac3-production", "aliyun-gray"] for key in control.IDS} | {key: [] for key in control.ALIASES} | {"unrelated": ["aliyun-gray"]}
    prior = matrix | {key: ["mac3-production"] for key in control.IDS} | {key: ["mac3-production", "aliyun-gray"] for key in control.ALIASES}
    for root, value in ((candidate, matrix), (rollback, prior)):
        (root / "deploy").mkdir(exist_ok=True)
        (root / "deploy/scheme_deployment_matrix_v1.json").write_text(json.dumps({"schemes": value}))
    commits = {candidate: "candidate-commit", reference: control.NATIVE_REFERENCE, rollback: control.ROLLBACK_RELEASE}
    monkeypatch.setattr(control.control, "_verified_install", lambda path: {"commit": commits[path]})
    if drift in {"reference", "rollback"}:
        commits[reference if drift == "reference" else rollback] = "wrong"
    elif drift == "matrix":
        path = candidate / "deploy/scheme_deployment_matrix_v1.json"
        matrix["unrelated"] = []
        path.write_text(json.dumps({"schemes": matrix}))
    elif drift == "alias_bytes":
        path = candidate / "schemes/t1_daily_5y_bbv2/delivery/t1_daily_5y_bbv2.py"
        path.chmod(0o644)
        path.write_text("# changed alias\n")
    elif drift == "candidate_metadata":
        path = candidate / "schemes/t1_daily/delivery/t1_daily_5y_bbv2.json"
        metadata = json.loads(path.read_text())
        metadata["description"] = "changed beyond identity"
        path.write_text(json.dumps(metadata))
    if drift:
        with pytest.raises((RuntimeError, ValueError)):
            control._verified_pair(candidate, reference, rollback)
    else:
        old, sources, new, release = control._verified_pair(candidate, reference, rollback)
        assert set(old) == set(new) == set(control.IDS)
        assert set(sources) == set(control.ALIASES)
        assert sum(len(items) for items in release["identity_conversions"].values()) == 6
