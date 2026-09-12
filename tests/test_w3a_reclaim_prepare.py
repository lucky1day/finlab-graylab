"""W3A 准备编排的独立合同测试；所有算法与生产控制面均用替身。"""

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness import cli, w3a_reclaim_prepare as prepare, writer_reclaim
from scheduler.discovery import load_scheme_config
from scheduler.blackbox_state import _encode
from shared.blackbox_v2.contracts import BlackboxRequest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    root, reference, rollback = (tmp_path / "releases" / item for item in ("candidate", "native", "rollback"))
    for path in (root, reference, rollback):
        path.mkdir(parents=True)
    new = {key: replace(load_scheme_config(ROOT / "schemes" / key / "config.yaml"),
        environment_fingerprint="e" * 64, data_snapshot_id="current") for key in prepare.W3A_IDS}
    sources = {key: load_scheme_config(ROOT / "schemes" / (key + "_bbv2") / "config.yaml") for key in new}
    old = {key: replace(cfg, runtime_type="native_adapter", scheme_version="native") for key, cfg in new.items()}
    request = BlackboxRequest("original-sda-request", "2026-09-11", "2026-09-10", "2026-09-17", "2026-09-10", "202635", "202609")
    snapshot = SimpleNamespace(snapshot_id="current", generation_id="generation", data_dir=tmp_path / "input")
    snapshot.data_dir.mkdir()
    plan = {"input": {"generation_id": "generation", "data_snapshot_id": "current"},
        "equivalence": {key: {"source": "real-prior-evidence"} for key in new},
        "releases": {"rollback": {"path": str(rollback), "install": {"commit": writer_reclaim.W3A_ROLLBACK_RELEASE}}},
        "scheduler": {"timer_fenced": True}, "requests": {key: {"predict_date": "2026-09-11"} for key in new}}
    reviewed = {"proof": {"reviewed": "source"}}
    capture = MagicMock(return_value=(old, sources, new, snapshot, request, reviewed, plan))
    monkeypatch.setattr(prepare, "_capture_inputs", capture)
    monkeypatch.setattr(prepare, "state_binding_for_scheme", lambda *_a, **_k: SimpleNamespace(root=tmp_path / "state"))
    events = []
    @contextmanager
    def lock(_engine, *, scheme_id):
        events.append(("acquire", scheme_id))
        try:
            yield
        finally:
            events.append(("release", scheme_id))
    monkeypatch.setattr(prepare.repository, "_blackbox_activation_advisory_lock", lock)
    view = SimpleNamespace(data_dir=snapshot.data_dir, bundle=SimpleNamespace(combined_snapshot_id="current"), mark_termination_uncertain=MagicMock())
    @contextmanager
    def opened(_bundle):
        yield view
    monkeypatch.setattr(prepare, "compose_blackbox_input_bundle", lambda *_a, **_k: None)
    monkeypatch.setattr(prepare, "open_blackbox_runtime_view", opened)
    def states(**kwargs):
        events.append(("warm",))
        assert kwargs["verify_context"]()["lifecycle_scheme_ids"] == prepare._LOCK_IDS
        assert kwargs["expected_revision_proof"] == reviewed["proof"]
        return {"status": "ready", "algorithm_executions": 1, "training_rows": 0}
    state = MagicMock(side_effect=states)
    monkeypatch.setattr(prepare, "prepare_reviewed_w3a_states", state)
    def execute(**kwargs):
        events.append(("sda",))
        assert kwargs["script_path"] == new[prepare._SDA].delivery_script
        assert kwargs["profile"].predict_timeout_sec == kwargs["timeout_sec"] == 120
        assert kwargs["profile"].cpu_threads == 8 and kwargs["profile"].memory_limit_bytes == 4 * 1024**3
        assert "state_input" not in kwargs and "state_output" not in kwargs
        kwargs["process_started"](123, 123)
        raw = json.loads(kwargs["input_path"].read_bytes())
        result = {key: raw[key] for key in ("request_id", "predict_date", "feature_date", "target_date")}
        kwargs["output_path"].parent.mkdir()
        kwargs["output_path"].write_text(json.dumps(result | {"predicted_direction": 1}))
        return SimpleNamespace(returncode=0, stdout="", stderr="diagnostic")
    algorithm = MagicMock(side_effect=execute)
    monkeypatch.setattr(prepare, "execute_blackbox_cli", algorithm)
    start, complete, gates = MagicMock(return_value=True), MagicMock(return_value=True), MagicMock()
    monkeypatch.setattr(prepare, "persist_harness_run_start", start)
    monkeypatch.setattr(prepare, "persist_harness_run_complete", complete)
    monkeypatch.setattr(prepare, "_verify_gates", gates)
    monkeypatch.setattr(prepare, "_record_failed_gate", MagicMock())
    monkeypatch.setattr(prepare, "read_w3a_readiness", MagicMock(return_value={"status": "ready"}))
    kwargs = dict(project_root=root, reference_project_root=reference, rollback_project_root=rollback,
        predict_date="2026-09-11", expected_database_name="isolated", expected_server_uuid="test-only",
        expected_plan_sha256=prepare._json_sha256(plan), approved_by="tester", work_dir=tmp_path / "work")
    return SimpleNamespace(kwargs=kwargs, new=new, old=old, sources=sources, plan=plan, state=state,
        start=start, complete=complete, gates=gates, algorithm=algorithm, events=events, capture=capture, view=view)


def test_prepare_read_only_preflight(prepared):
    env = prepared
    kwargs = {k: v for k, v in env.kwargs.items() if k not in {"work_dir", "expected_plan_sha256", "approved_by"}}
    assert prepare.build_w3a_reclaim_prepare_preflight(None, **kwargs)["plan_sha256"] == env.kwargs["expected_plan_sha256"]
    env.state.assert_not_called()
    env.algorithm.assert_not_called()
    env.start.assert_not_called()
    assert not env.kwargs["work_dir"].exists()


def test_four_locks_two_real_gates_and_complete_only_recovery(prepared):
    env = prepared
    result = prepare.execute_w3a_reclaim_prepare(None, **env.kwargs)
    assert result["algorithm_executions"] == 2 and result["full_training_rows"] == 0
    assert env.events[:4] == [("acquire", key) for key in prepare._LOCK_IDS]
    assert env.events[4:6] == [("warm",), ("sda",)]
    assert env.events[-4:] == [("release", key) for key in reversed(prepare._LOCK_IDS)]
    assert env.start.call_count == env.complete.call_count == 2
    assert len(set(result["harness_run_ids"].values())) == 2
    for call in env.complete.call_args_list:
        gate = call.kwargs["results"][0]
        proof = gate.evidence[0].value
        assert proof["local_execution_sha256"] == result["local_execution_sha256"][proof["scheme_id"]]
        assert proof["new_identity"]["scheme_version"] == env.new[proof["scheme_id"]].scheme_version
    assert prepare.execute_w3a_reclaim_prepare(None, **env.kwargs) == result
    env.state.assert_called_once()
    env.algorithm.assert_called_once()
    assert env.start.call_count == env.complete.call_count == 2
    assert env.gates.call_count == 2


@pytest.mark.parametrize("boundary", ["plan", "gate_start", "warm", "sda", "gate_complete", "context", "readback"])
def test_failure_never_repeats_or_creates_a_success(boundary, prepared, monkeypatch):
    env = prepared
    if boundary == "plan":
        env.kwargs["expected_plan_sha256"] = "0" * 64
    elif boundary == "gate_start":
        env.start.return_value = False
    elif boundary == "warm":
        env.state.side_effect = RuntimeError("warm failure")
    elif boundary == "sda":
        env.algorithm.side_effect = RuntimeError("SDA failure")
    elif boundary == "gate_complete":
        env.complete.return_value = False
    elif boundary == "readback":
        env.gates.side_effect = RuntimeError("Gate read-back failure")
    else:
        original = env.capture.return_value
        env.capture.side_effect = [original, (*original[:-1], original[-1] | {"drift": True})]
    with pytest.raises(RuntimeError):
        prepare.execute_w3a_reclaim_prepare(None, **env.kwargs)
    counts = env.state.call_count, env.algorithm.call_count, env.start.call_count
    with pytest.raises((RuntimeError, FileNotFoundError, StopIteration)):
        prepare.execute_w3a_reclaim_prepare(None, **env.kwargs)
    assert counts == (env.state.call_count, env.algorithm.call_count, env.start.call_count)
    if boundary in {"plan", "gate_start"}:
        env.state.assert_not_called()
        env.algorithm.assert_not_called()


def test_prepare_rejects_release_sibling_workdir_before_mkdir(prepared):
    prepared.kwargs["work_dir"] = prepared.kwargs["project_root"].parent / "other-release"
    with pytest.raises(ValueError, match="outside releases"):
        prepare.execute_w3a_reclaim_prepare(None, **prepared.kwargs)
    assert not prepared.kwargs["work_dir"].exists()
    prepared.start.assert_not_called()


@pytest.mark.parametrize("action", ["preflight", "prepare"])
def test_w3a_cli_distinguishes_reference_and_actual_rollback(monkeypatch, action):
    name = "build_w3a_reclaim_prepare_preflight" if action == "preflight" else "execute_w3a_reclaim_prepare"
    called, engine = MagicMock(return_value={"ready": True}), MagicMock()
    monkeypatch.setattr(cli, name, called)
    monkeypatch.setattr(cli, "create_engine_from_env", lambda: engine)
    args = ["migrate-native-successor", action, "--wave", "W3A", "--reference-project-root", "/native",
        "--rollback-project-root", "/actual-rollback", "--expected-database-name", "isolated", "--expected-server-uuid", "test"]
    args += ["--action", "prepare"] if action == "preflight" else ["--work-dir", "/work", "--expected-plan-sha256", "a" * 64, "--approved-by", "test"]
    assert cli._run_native_successor_migration_command(cli._build_parser().parse_args(args)) == {"ready": True}
    assert called.call_args.kwargs["reference_project_root"] == Path("/native")
    assert called.call_args.kwargs["rollback_project_root"] == Path("/actual-rollback")
    assert "harness_run_ids" not in called.call_args.kwargs


def test_old_locale_is_isolated_and_missing_comparison_fails_closed(monkeypatch):
    monkeypatch.setenv("LANG", "current-lang")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.setenv("TZ", "current-tz")
    prior = dict(os.environ)
    child = MagicMock(return_value=SimpleNamespace(returncode=1, stderr=b"old evidence changed", stdout=b""))
    monkeypatch.setattr(prepare.subprocess, "run", child)
    with pytest.raises(RuntimeError, match="original-locale"):
        prepare._equivalence(ROOT, ROOT, {}, {}, {}, "", {})
    assert dict(os.environ) == prior
    env = child.call_args.kwargs["env"]
    assert env["LANG"] == "en_US.UTF-8" and env["LC_ALL"] == "C.UTF-8" and "TZ" not in env
    assert child.call_args.kwargs["timeout"] == 120


@pytest.mark.parametrize("status,existing_gate,written", [("running", False, True), ("passed", True, False), ("running", True, False), ("failed", True, False)])
def test_failed_gate_only_finishes_known_current_running_run(monkeypatch, status, existing_gate, written):
    cfg = load_scheme_config(ROOT / "schemes" / prepare._SDA / "config.yaml")
    ctx = SimpleNamespace(scheme_id=cfg.scheme_id, config=cfg)
    row = {"scheme_id": cfg.scheme_id, "scheme_version": cfg.scheme_version, "code_hash": cfg.code_hash,
           "config_hash": cfg.config_hash, "stage": prepare._STAGE, "status": status}
    engine = MagicMock()
    monkeypatch.setattr(prepare.repository, "_same_id_rows_conn", MagicMock(side_effect=[[row], [{}] if existing_gate else []]))
    complete = MagicMock(return_value=True)
    monkeypatch.setattr(prepare, "persist_harness_run_complete", complete)
    error = RuntimeError("original execution failure")
    prepare._record_failed_gate(engine, ctx, "current-run", "2026-09-12T00:00:00Z", error)
    assert complete.called is written
    if written:
        assert complete.call_args.kwargs["status"] == "failed"
        assert complete.call_args.kwargs["harness_run_id"] == "current-run"


def test_unknown_gate_commit_outcome_does_not_blindly_rewrite(monkeypatch):
    engine = MagicMock()
    engine.connect.side_effect = RuntimeError("database unavailable")
    complete = MagicMock()
    monkeypatch.setattr(prepare, "persist_harness_run_complete", complete)
    error = RuntimeError("original")
    prepare._record_failed_gate(engine, SimpleNamespace(), "current", "started", error)
    complete.assert_not_called()
    assert str(error) == "original" and error.__notes__


def test_failure_receipt_error_does_not_mask_original(prepared, monkeypatch):
    env = prepared
    env.state.side_effect = RuntimeError("original warm error")
    real_write = prepare._write_receipt
    def write(path, value):
        if path.name == "failure.json":
            raise OSError("receipt disk error")
        return real_write(path, value)
    monkeypatch.setattr(prepare, "_write_receipt", write)
    with pytest.raises(RuntimeError, match="original warm error") as caught:
        prepare.execute_w3a_reclaim_prepare(None, **env.kwargs)
    assert any("failure receipt unavailable" in note for note in caught.value.__notes__)


@pytest.fixture
def ready(tmp_path, monkeypatch):
    new = {key: replace(load_scheme_config(ROOT / "schemes" / key / "config.yaml"),
        environment_fingerprint="e" * 64, data_snapshot_id="snapshot") for key in prepare.W3A_IDS}
    sources = {key: load_scheme_config(ROOT / "schemes" / (key + "_bbv2") / "config.yaml") for key in new}
    work, state_root = tmp_path / "work", tmp_path / "state"
    work.mkdir()
    (work / "states").mkdir()
    inp = {"schema": "data-bridge-v1", "snapshot_id": "snapshot", "files": {"daily": "hash"}}
    releases = {"candidate": "new", "reference": "native", "rollback": "actual"}
    plan = {"input": {"generation_id": "generation", "data_snapshot_id": "snapshot", "files": inp["files"]},
        "releases": releases, "candidate_versions": {key: cfg.scheme_version for key, cfg in new.items()},
        "environment_fingerprint": "e" * 64, "equivalence": {key: {"prior": "verified"} for key in new},
        "database": {"database_identity_sha256": "isolated-test-digest"}}
    sha = prepare._json_sha256(plan)
    full = {"status": "ready", "revision_proof": {"real": "proof"}, "context": {"plan_sha256": sha},
            "input": inp, "training_rows": 0, "algorithm_executions": 1}
    observed, paths = {}, []
    for role, cfg in (("source", sources[prepare._FULL]), ("candidate", new[prepare._FULL])):
        identity = {"exact": cfg.scheme_version, "runtime": "current"}
        payload = role.encode()
        header = {"identity": identity, "input": inp, "payload_sha256": hashlib.sha256(payload).hexdigest()}
        raw = _encode(header, payload)
        path = state_root / cfg.scheme_id / f"{cfg.scheme_version}.state"
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
        paths.append(path)
        audit = {"state_envelope_sha256": hashlib.sha256(raw).hexdigest(), "state_output_sha256": hashlib.sha256(payload).hexdigest()}
        full[role + "_state"], full[role + "_identity"] = audit, identity
        observed[cfg.scheme_id] = {"envelope_sha256": audit["state_envelope_sha256"]}
    prepare._write_receipt(work / "plan.json", plan)
    prepare._write_receipt(work / "states/complete.json", full)
    sda_request = BlackboxRequest("sda-request", "2026-09-11", "2026-09-10", "2026-09-17", "2026-09-10", "202635", "202609")
    request_path = prepare.write_request(sda_request, work / "sda.request.json")
    (work / "sda-output").mkdir()
    request_raw = json.loads(request_path.read_bytes())
    result = {key: request_raw[key] for key in ("request_id", "predict_date", "feature_date", "target_date")}
    result["predicted_direction"] = 1
    result_sha = prepare._write_receipt(work / "sda-output/result.json", result)
    logs_sha = prepare._write_receipt(work / "sda.logs.json", {"stdout": "", "stderr": ""})
    execution = {prepare._FULL: {"state_readiness_sha256": prepare._json_sha256(full)}, prepare._SDA: {
        "scheme_id": prepare._SDA, "scheme_version": new[prepare._SDA].scheme_version,
        "environment_fingerprint": "e" * 64, "input": plan["input"], "request": request_raw, "result": result,
        "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(), "result_sha256": result_sha,
        "logs_sha256": logs_sha}}
    digests = {key: prepare._write_receipt(work / f"{key}.execution.json", value) for key, value in execution.items()}
    prepare._write_receipt(work / "complete.json", {"status": "ready", "plan_sha256": sha,
        "harness_run_ids": {key: "run-" + key for key in new}, "local_execution_sha256": digests})
    monkeypatch.setattr(prepare, "load_reviewed_w3a_revision", lambda _cfg: {"proof": full["revision_proof"]})
    monkeypatch.setattr(prepare.control, "_read_candidate_states", lambda *_a: observed)
    monkeypatch.setattr(prepare, "state_binding_for_scheme", lambda *_a, **_k: SimpleNamespace(root=state_root))
    kwargs = dict(sources=sources, new=new, snapshot=SimpleNamespace(snapshot_id="snapshot", generation_id="generation"),
                  work_dir=work, releases=releases)
    return kwargs, paths, work


def test_readiness_validates_both_states_without_warm_or_publish(ready):
    kwargs, paths, _ = ready
    before = [path.read_bytes() for path in paths]
    result = prepare.read_w3a_readiness(ROOT, **kwargs)
    assert result["source_and_candidate_current_input_verified"] is True
    assert before == [path.read_bytes() for path in paths]


@pytest.mark.parametrize("drift", ["source", "candidate", "environment", "input", "release", "execution", "failure", "sda_result", "sda_request", "sda_logs"])
def test_readiness_rejects_current_state_and_provenance_drift(ready, drift):
    kwargs, paths, work = ready
    if drift in {"source", "candidate"}:
        path = paths[0 if drift == "source" else 1]
        path.write_bytes(path.read_bytes() + b"drift")
    elif drift == "environment":
        kwargs["new"][prepare._FULL] = replace(kwargs["new"][prepare._FULL], environment_fingerprint="changed")
    elif drift == "input":
        kwargs["snapshot"].snapshot_id = "changed"
    elif drift == "release":
        kwargs["releases"] = {"rollback": "native-reference-not-actual"}
    elif drift == "failure":
        (work / "failure.json").write_text("{}")
    elif drift.startswith("sda_"):
        relative = {"sda_request": "sda.request.json", "sda_result": "sda-output/result.json", "sda_logs": "sda.logs.json"}[drift]
        (work / relative).write_text("{}")
    else:
        (work / f"{prepare._SDA}.execution.json").write_text('{"request":"fake"}')
    with pytest.raises((RuntimeError, ValueError)):
        prepare.read_w3a_readiness(ROOT, **kwargs)
