"""临时 W3B prepare 验收：复用成果、真实凭据与失败保全，不执行重型算法。"""

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness import cli, w3b_prepare as prepare
from scheduler.discovery import load_scheme_config
from scheduler.process_control import ProcessGroupTerminationError, ProcessGroupTerminationResult
from shared.blackbox_v2.contracts import BlackboxRequest, BlackboxResult


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepared_inputs(tmp_path, monkeypatch):
    ids = prepare.control.W3B_IDS
    old = {key: replace(load_scheme_config(ROOT / "schemes" / key / "config.yaml"),
                        runtime_type="native_adapter", scheme_version="old-native") for key in ids}
    source_cfg = {key: load_scheme_config(ROOT / "schemes" / (key + "_bbv2") / "config.yaml") for key in ids}
    new = {key: replace(cfg, scheme_id=key, environment_fingerprint="e" * 64,
                        data_snapshot_id="snapshot") for key, cfg in source_cfg.items()}
    request = BlackboxRequest("source-id", "2026-09-11", "2026-09-10", "2026-09-17",
                              "2026-09-10", "202635", "202609")
    result = BlackboxResult(request.request_id, request.predict_date, request.feature_date, request.target_date, 1)
    sources = {key: (source_cfg[key], {"request": request, "result": result,
                "source_envelope_sha256": "a" * 64, "expected_algorithm_identity": {"kind": "verified"}})
               for key in ids}
    snapshot = SimpleNamespace(generation_id="generation", snapshot_id="snapshot")
    current = tmp_path / "current"
    current.symlink_to(ROOT, target_is_directory=True)
    monkeypatch.setitem(prepare.control._CURRENT_LINKS, "aliyun-gray", current)
    plan = {"current": {"path": str(ROOT)}, "database": {"protected": "unchanged"},
            "input": {"generation_id": "generation"}, "equivalence": {key: {"verified": True} for key in ids}}
    capture = MagicMock(return_value=(old, new, snapshot, sources, plan))
    monkeypatch.setattr(prepare, "_capture_inputs", capture)
    monkeypatch.setattr(prepare, "_ready_input", lambda: (snapshot, plan["input"]))
    monkeypatch.setattr(prepare, "_database_snapshot", lambda *_a, **_k: plan["database"])
    locks = []

    @contextmanager
    def lock(_engine, *, scheme_id):
        locks.append(("acquire", scheme_id))
        try:
            yield
        finally:
            locks.append(("release", scheme_id))

    monkeypatch.setattr(prepare, "_blackbox_activation_advisory_lock", lock)
    view = SimpleNamespace(data_dir=tmp_path, bundle=SimpleNamespace(combined_snapshot_id="snapshot"),
                           mark_termination_uncertain=MagicMock())

    @contextmanager
    def runtime_view(_bundle):
        yield view

    monkeypatch.setattr(prepare, "compose_blackbox_input_bundle", lambda *_a, **_k: object())
    monkeypatch.setattr(prepare, "open_blackbox_runtime_view", runtime_view)
    admission = MagicMock(side_effect=lambda **kw: {"scheme_id": kw["candidate_config"].scheme_id,
                          "source_envelope_sha256": kw["expected_source_envelope_sha256"], "algorithm_executions": 1})
    start = MagicMock(return_value=True)
    complete = MagicMock(return_value=True)
    monkeypatch.setattr(prepare, "admit_reviewed_w3b_state", admission)
    monkeypatch.setattr(prepare, "persist_harness_run_start", start)
    monkeypatch.setattr(prepare, "persist_harness_run_complete", complete)
    kwargs = dict(project_root=ROOT, reference_project_root=ROOT, expected_database_name="test",
                  expected_server_uuid="test-only", expected_plan_sha256=prepare._json_sha256(plan),
                  approved_by="tester", work_dir=tmp_path / "receipt")
    return SimpleNamespace(kwargs=kwargs, plan=plan, capture=capture, admission=admission,
                           start=start, complete=complete, locks=locks, view=view, current=current)


def test_prepare_one_call_each_retains_true_receipts_and_gate_hashes(prepared_inputs):
    env = prepared_inputs
    result = prepare.execute_w3b_prepare(None, **env.kwargs)
    ids = prepare.control.W3B_IDS
    assert set(result["harness_run_ids"]) == set(ids)
    assert len(set(result["harness_run_ids"].values())) == 3
    assert result["algorithm_executions"] == env.admission.call_count == 3
    assert not result["prediction_written"] and not result["registry_changed"]
    ordered = sorted((*ids, *(key + "_bbv2" for key in ids)))
    assert env.locks == [("acquire", key) for key in ordered] + [("release", key) for key in reversed(ordered)]
    for call, key in zip(env.complete.call_args_list, ids):
        assert call.kwargs["status"] == "passed"
        gate, = call.kwargs["results"]
        evidence, = gate.evidence
        assert evidence.key == "runtime_upgrade"
        proof = evidence.value
        payload = (env.kwargs["work_dir"] / f"{key}.admission.json").read_bytes()
        assert proof["local_execution_sha256"] == hashlib.sha256(payload).hexdigest()
        assert proof["scheme_id"] == key
        assert proof["old_identity"]["runtime_type"] == "native_adapter"
        assert proof["new_identity"]["runtime_type"] == "blackbox_v2"
        assert proof["generation_id"] == "generation"
    for call in env.admission.call_args_list:
        request = call.kwargs["request"]
        assert request.feature_date == request.daily_cutoff_key == "2026-09-10"
        assert request.request_id == call.kwargs["expected_result"].request_id
    assert json.loads((env.kwargs["work_dir"] / "complete.json").read_text()) == result


def test_preflight_never_executes_or_persists(prepared_inputs):
    env = prepared_inputs
    assert prepare.build_w3b_prepare_preflight(None)["plan_sha256"] == env.kwargs["expected_plan_sha256"]
    env.admission.assert_not_called()
    env.start.assert_not_called()
    env.complete.assert_not_called()
    assert not env.kwargs["work_dir"].exists()


@pytest.mark.parametrize("invalid", ["stale", "existing", "symlink", "operator"])
def test_bad_approval_or_destination_stops_before_first_side_effect(prepared_inputs, invalid):
    env = prepared_inputs
    kwargs = dict(env.kwargs)
    if invalid == "stale":
        kwargs["expected_plan_sha256"] = "0" * 64
    elif invalid == "existing":
        kwargs["work_dir"].mkdir()
    elif invalid == "symlink":
        alias = kwargs["work_dir"].parent / "alias"
        alias.symlink_to(ROOT, target_is_directory=True)
        kwargs["work_dir"] = alias / "new-receipt"
    else:
        kwargs["approved_by"] = " "
    with pytest.raises((ValueError, RuntimeError)):
        prepare.execute_w3b_prepare(None, **kwargs)
    env.admission.assert_not_called()
    env.start.assert_not_called()


def test_harness_start_failure_does_not_start_algorithm(prepared_inputs):
    env = prepared_inputs
    env.start.return_value = False
    with pytest.raises(RuntimeError, match="Harness start failed"):
        prepare.execute_w3b_prepare(None, **env.kwargs)
    env.admission.assert_not_called()
    env.complete.assert_not_called()


@pytest.mark.parametrize("drift", ["input", "current", "database"])
def test_post_publication_drift_never_becomes_passed(prepared_inputs, monkeypatch, drift):
    env = prepared_inputs
    if drift == "input":
        monkeypatch.setattr(prepare, "_ready_input", lambda: (None, {"changed": True}))
    elif drift == "database":
        monkeypatch.setattr(prepare, "_database_snapshot", lambda *_a, **_k: {"changed": True})
    else:
        env.current.unlink()
        env.current.symlink_to(ROOT.parent, target_is_directory=True)
    with pytest.raises(RuntimeError, match="changed; preserve state"):
        prepare.execute_w3b_prepare(None, **env.kwargs)
    assert env.admission.call_count == 1
    assert env.complete.call_args.kwargs["status"] == "failed"
    assert len(list(env.kwargs["work_dir"].glob("*.admission.json"))) == 1
    assert not (env.kwargs["work_dir"] / "complete.json").exists()


def test_published_state_and_receipt_survive_gate_write_failure(prepared_inputs):
    env = prepared_inputs
    env.complete.return_value = False
    with pytest.raises(RuntimeError, match="state published but Harness completion failed"):
        prepare.execute_w3b_prepare(None, **env.kwargs)
    assert env.admission.call_count == 1
    assert len(list(env.kwargs["work_dir"].glob("*.admission.json"))) == 1
    with pytest.raises(ValueError, match="fresh absolute"):
        prepare.execute_w3b_prepare(None, **env.kwargs)
    assert env.admission.call_count == 1


def test_uncertain_process_keeps_inputs_even_if_failure_persistence_interrupts(prepared_inputs):
    env = prepared_inputs
    error = ProcessGroupTerminationError(termination=ProcessGroupTerminationResult(
        process_id=999999, process_group_id=999999, term_sent=True, kill_sent=True, confirmed_gone=False), context="test")
    env.admission.side_effect = error

    def interrupted(*_a, **_k):
        assert env.view.mark_termination_uncertain.called
        raise KeyboardInterrupt("failure recording interrupted")

    env.complete.side_effect = interrupted
    with pytest.raises(ProcessGroupTerminationError) as raised:
        prepare.execute_w3b_prepare(None, **env.kwargs)
    assert raised.value is error
    assert "original execution error preserved" in error.__notes__[0]
    assert env.admission.call_count == 1


def test_current_must_match_reference_native_identity_and_closure(tmp_path, monkeypatch):
    key = prepare.control.W3B_IDS[0]
    monkeypatch.setattr(prepare.control, "W3B_IDS", (key,))
    cfg = replace(load_scheme_config(ROOT / "schemes" / key / "config.yaml"),
                  runtime_type="native_adapter", scheme_version="old-native")
    old = {key: cfg}
    monkeypatch.setattr(prepare, "load_scheme_config", lambda *_: cfg)
    current, candidate, reference = (tmp_path / name for name in ("current", "candidate", "reference"))
    for base in (current, reference):
        directory = base / "schemes" / key
        (directory / "core").mkdir(parents=True)
        for name in ("inference.py", "core/__init__.py", "core/data_alignment.py", "core/v31_common.py"):
            (directory / name).write_bytes(b"verified source\n")
    assert key in prepare._verify_current_native(current, candidate, reference, old)
    with pytest.raises(RuntimeError, match="not the candidate"):
        prepare._verify_current_native(candidate, candidate, reference, old)
    monkeypatch.setattr(prepare, "load_scheme_config", lambda *_: replace(cfg, scheme_version="changed"))
    with pytest.raises(RuntimeError, match="identity differs"):
        prepare._verify_current_native(current, candidate, reference, old)
    monkeypatch.setattr(prepare, "load_scheme_config", lambda *_: cfg)
    (current / "schemes" / key / "inference.py").write_bytes(b"different inference\n")
    with pytest.raises(RuntimeError, match="source differs"):
        prepare._verify_current_native(current, candidate, reference, old)


def _cli_args(action):
    args = ["migrate-native-successor", action, "--wave", "W3B", "--project-root", "/candidate",
            "--reference-project-root", "/reference", "--expected-database-name", "test",
            "--expected-server-uuid", "test-only"]
    return args + (["--action", "prepare"] if action == "preflight" else [
        "--expected-plan-sha256", "a" * 64, "--approved-by", "tester", "--work-dir", "/fresh"])


@pytest.mark.parametrize("action", ["preflight", "prepare"])
def test_prepare_cli_creates_own_evidence_not_caller_run_ids(monkeypatch, action):
    endpoint = MagicMock(return_value={"passed": True})
    engine = MagicMock()
    monkeypatch.setattr(cli, "create_engine_from_env", lambda: engine)
    monkeypatch.setattr(cli, "build_w3b_prepare_preflight" if action == "preflight" else "execute_w3b_prepare", endpoint)
    args = cli._build_parser().parse_args(_cli_args(action))
    assert cli._run_native_successor_migration_command(args) == {"passed": True}
    assert "harness_run_ids" not in endpoint.call_args.kwargs
    if action == "prepare":
        assert endpoint.call_args.kwargs["work_dir"] == Path("/fresh")
    engine.dispose.assert_called_once()


def test_prepare_preflight_rejects_supplied_harness_ids_before_engine(monkeypatch):
    engine = MagicMock()
    monkeypatch.setattr(cli, "create_engine_from_env", engine)
    args = cli._build_parser().parse_args(_cli_args("preflight") + ["--harness-run-id", "untrusted=run"])
    with pytest.raises(ValueError, match="creates its own real Harness"):
        cli._run_native_successor_migration_command(args)
    engine.assert_not_called()
