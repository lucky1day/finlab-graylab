"""临时 W2 准备边界验收；算法调用全部替身，不创建生产执行。"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness import w2_reclaim_prepare as prepare
from harness.persistence import _result_summary
from scheduler import repository
from scheduler.discovery import load_scheme_config
from scheduler.process_control import ProcessGroupTerminationError, ProcessGroupTerminationResult
from shared.blackbox_v2.contracts import BlackboxRequest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    new = {key: replace(load_scheme_config(ROOT / "schemes" / key / "config.yaml"),
                         environment_fingerprint="e" * 64, data_snapshot_id="current-snapshot")
           for key in prepare.W2_IDS}
    old = {key: replace(cfg, runtime_type="native_adapter", scheme_version="native-version")
           for key, cfg in new.items()}
    requests = {key: BlackboxRequest(f"{key}:request", "2026-09-11", "2026-09-10", "2026-09-17",
                                     "2026-09-10", "202635", "202609") for key in new}
    snapshot = SimpleNamespace(snapshot_id="current-snapshot", generation_id="current-generation")
    plan = {"input": {"generation_id": "current-generation", "data_snapshot_id": "current-snapshot"},
            "equivalence": {key: {"generation_id": "older-generation"} for key in new}}
    capture = MagicMock(return_value=(old, new, snapshot, requests, plan))
    monkeypatch.setattr(prepare, "_capture_inputs", capture)
    locks = []

    @contextmanager
    def lock(_engine, *, scheme_id):
        locks.append(("acquire", scheme_id))
        try:
            yield
        finally:
            locks.append(("release", scheme_id))

    monkeypatch.setattr(prepare, "_blackbox_activation_advisory_lock", lock)
    view = SimpleNamespace(data_dir=tmp_path, bundle=SimpleNamespace(combined_snapshot_id="current-snapshot"),
                           mark_termination_uncertain=MagicMock())

    @contextmanager
    def open_view(_bundle):
        yield view

    monkeypatch.setattr(prepare, "compose_blackbox_input_bundle", lambda *_a, **_k: object())
    monkeypatch.setattr(prepare, "open_blackbox_runtime_view", open_view)

    def execute(**kwargs):
        assert kwargs["mode"] == "predict"
        assert kwargs["profile"].predict_timeout_sec == 120
        assert kwargs["profile"].cpu_threads == 8
        assert kwargs["profile"].memory_limit_bytes == 4 * 1024**3
        assert "state_input" not in kwargs and "state_output" not in kwargs
        kwargs["process_started"](123, 123)
        request = json.loads(kwargs["input_path"].read_text())
        result = {key: request[key] for key in ("request_id", "predict_date", "feature_date", "target_date")}
        result["predicted_direction"] = 1
        kwargs["output_path"].parent.mkdir()
        kwargs["output_path"].write_text(json.dumps(result))
        return SimpleNamespace(returncode=0, stdout="", stderr="diagnostic")

    algorithm = MagicMock(side_effect=execute)
    monkeypatch.setattr(prepare, "execute_blackbox_cli", algorithm)
    start, complete = MagicMock(return_value=True), MagicMock(return_value=True)
    monkeypatch.setattr(prepare, "persist_harness_run_start", start)
    monkeypatch.setattr(prepare, "persist_harness_run_complete", complete)
    kwargs = dict(project_root=ROOT, reference_project_root=ROOT, predict_date="2026-09-11",
                  expected_database_name="isolated", expected_server_uuid="test-only",
                  expected_plan_sha256=prepare._json_sha256(plan), approved_by="tester",
                  work_dir=(tmp_path / "receipts").resolve())
    return SimpleNamespace(kwargs=kwargs, capture=capture, algorithm=algorithm, start=start,
                           complete=complete, locks=locks, view=view, old=old, new=new, plan=plan)


def test_preflight_is_read_only(prepared):
    env = prepared
    kwargs = {key: value for key, value in env.kwargs.items()
              if key not in {"expected_plan_sha256", "approved_by", "work_dir"}}
    result = prepare.build_w2_reclaim_prepare_preflight(None, **kwargs)
    assert result["plan_sha256"] == env.kwargs["expected_plan_sha256"]
    env.algorithm.assert_not_called()
    env.start.assert_not_called()
    assert not env.kwargs["work_dir"].exists()


def test_two_standard_calls_produce_repository_accepted_real_gate_shape(prepared, monkeypatch):
    env = prepared
    result = prepare.execute_w2_reclaim_prepare(None, **env.kwargs)
    assert result["algorithm_executions"] == 2
    assert not result["prediction_written"] and not result["state_written"]
    assert env.algorithm.call_count == 2
    ids = sorted((*prepare.W2_IDS, *(key + "_bbv2" for key in prepare.W2_IDS)))
    assert env.locks == [("acquire", key) for key in ids] + [("release", key) for key in reversed(ids)]
    assert set(result["harness_run_ids"]) == set(prepare.W2_IDS)
    for key, call in zip(prepare.W2_IDS, env.complete.call_args_list):
        assert call.kwargs["status"] == "passed"
        gate, = call.kwargs["results"]
        proof = gate.evidence[0].value
        payload = (env.kwargs["work_dir"] / f"{key}.execution.json").read_bytes()
        assert proof["local_execution_sha256"] == hashlib.sha256(payload).hexdigest()
        assert proof["equivalence_sha256"] == prepare._json_sha256({"generation_id": "older-generation"})
        assert proof["generation_id"] == "current-generation"
        cfg = env.new[key]
        run = {"scheme_id": key, "scheme_version": cfg.scheme_version, "code_hash": cfg.code_hash,
               "config_hash": cfg.config_hash, "stage": "native-runtime-upgrade", "status": "passed",
               "finished_at": gate.finished_at}
        stored = {"status": "passed", "finished_at": gate.finished_at, "summary_json": _result_summary(gate)}
        monkeypatch.setattr(repository, "_same_id_rows_conn", lambda _c, table, *_a, **_k: [run if table == "t_harness_runs" else stored])
        evidence = repository._same_id_evidence_conn(None, env.old[key], cfg, result["harness_run_ids"][key],
            {"databridge": env.plan["input"], "blackbox_environment_fingerprint": cfg.environment_fingerprint}, for_update=False)
        assert evidence["harness_run_id"] == result["harness_run_ids"][key]


@pytest.mark.parametrize("invalid", ["stale", "existing", "symlink", "operator"])
def test_bad_plan_or_work_path_never_executes(prepared, invalid):
    env = prepared
    kwargs = dict(env.kwargs)
    if invalid == "stale":
        kwargs["expected_plan_sha256"] = "0" * 64
    elif invalid == "existing":
        kwargs["work_dir"].mkdir()
    elif invalid == "symlink":
        alias = kwargs["work_dir"].parent / "alias"
        alias.symlink_to(ROOT, target_is_directory=True)
        kwargs["work_dir"] = alias / "receipts"
    else:
        kwargs["approved_by"] = " "
    with pytest.raises((RuntimeError, ValueError)):
        prepare.execute_w2_reclaim_prepare(None, **kwargs)
    env.algorithm.assert_not_called()
    env.start.assert_not_called()


@pytest.mark.parametrize("failure", ["start", "execute", "nonzero", "timeout", "stdout", "result", "request", "drift", "completion", "termination"])
def test_failure_preserves_receipts_and_never_runs_second_scheme(prepared, failure):
    env = prepared
    execute = env.algorithm.side_effect
    if failure == "start":
        env.start.return_value = False
    elif failure == "drift":
        values = env.capture.return_value
        env.capture.side_effect = [values, values, (*values[:-1], {"changed": True})]
    elif failure == "completion":
        env.complete.side_effect = [False, True]
    elif failure == "termination":
        env.algorithm.side_effect = ProcessGroupTerminationError(
            termination=ProcessGroupTerminationResult(123, 123, True, True, False, "test"), context="test")
    else:
        def broken(**kwargs):
            if failure in {"execute", "timeout"}:
                kwargs["process_started"](123, 123)
                if failure == "timeout":
                    raise subprocess.TimeoutExpired("test algorithm", 120)
                raise RuntimeError("test execution failure with retained diagnostic")
            completed = execute(**kwargs)
            if failure == "nonzero":
                completed.returncode = 2
            elif failure == "stdout":
                completed.stdout = "business output"
            elif failure == "request":
                kwargs["input_path"].write_text("{}")
            else:
                data = json.loads(kwargs["output_path"].read_text())
                data["target_date"] = "2026-09-18"
                kwargs["output_path"].write_text(json.dumps(data))
            return completed
        env.algorithm.side_effect = broken
    with pytest.raises((RuntimeError, ValueError, subprocess.TimeoutExpired)):
        prepare.execute_w2_reclaim_prepare(None, **env.kwargs)
    assert env.algorithm.call_count == (0 if failure == "start" else 1)
    assert env.complete.call_args.kwargs["status"] == "failed"
    assert len(list(env.kwargs["work_dir"].glob("*.failed.json"))) == 1
    failed = json.loads(next(env.kwargs["work_dir"].glob("*.failed.json")).read_text())
    assert failed["error"] and failed["error_type"]
    if failure not in {"start", "termination"}:
        assert failed["algorithm_started"]
        assert failed["process"]["process_id"] == 123
        assert failed["elapsed_seconds"] >= 0
        assert len(list(env.kwargs["work_dir"].glob("*.process.json"))) == 1
    elif failure == "start":
        assert not failed["algorithm_started"]
    if failure == "result":
        assert len(failed["result_sha256"]) == 64
    assert not (env.kwargs["work_dir"] / "complete.json").exists()
    if failure == "termination":
        env.view.mark_termination_uncertain.assert_called_once()


def test_date_selects_only_due_trading_trigger(monkeypatch):
    calendar = SimpleNamespace(covers=lambda _day: True,
        is_trading_day=lambda day: day in {"2026-09-10", "2026-09-11", "2026-09-14"},
        previous_trading_day=lambda day: {"2026-09-12": "2026-09-11", "2026-09-14": "2026-09-11"}[day])

    class Clock(datetime):
        value = (2026, 9, 12, 10, 0)

        @classmethod
        def now(cls, tz=None):
            return cls(*cls.value, tzinfo=tz)

    monkeypatch.setattr(prepare, "datetime", Clock)
    assert prepare._request_date(calendar, None) == "2026-09-11"
    assert prepare._request_date(calendar, "2026-09-10") == "2026-09-10"
    for invalid in ("2026-09-12", "2026-09-14", "20260911"):
        with pytest.raises(ValueError):
            prepare._request_date(calendar, invalid)
    Clock.value = (2026, 9, 14, 7, 2)
    assert prepare._request_date(calendar, None) == "2026-09-11"
    with pytest.raises(ValueError):
        prepare._request_date(calendar, "2026-09-14")
    Clock.value = (2026, 9, 14, 7, 3)
    assert prepare._request_date(calendar, None) == "2026-09-14"


def test_equivalence_preserves_original_vintage_and_checks_fixed_bytes(tmp_path, monkeypatch):
    report = {"wave": "W2", "producer": {"tool": "reviewed-test"},
              "runtime_environment_fingerprint": "e" * 64, "targets": [
        {"old_base_scheme_id": key, "new_base_scheme_id": key + "_bbv2", "old_code_hash": "a" * 64,
         "new_code_hash": "b" * 64, "request_count": 333, "task_type": "T+5",
         "target_tenor": tenor, "old_horizon": 5, "new_horizon": 5,
         "native_result_sha256": "c" * 64, "successor_result_sha256": "c" * 64,
         "request_id_mismatch_count": 0, "predict_date_mismatch_count": 0,
         "feature_date_mismatch_count": 0, "target_date_mismatch_count": 0, "direction_mismatch_count": 0,
         "input_identity": {"generation_id": "reviewed-old-generation", "data_snapshot_id": "reviewed-old-snapshot"}}
        for key, tenor in zip(prepare.W2_IDS, ("5Y", "7Y"))]}
    source = tmp_path / "W2.json"
    source.write_text(json.dumps(report))
    monkeypatch.setattr(prepare, "_REPORT", source)
    monkeypatch.setattr(prepare, "_REPORT_SHA", hashlib.sha256(source.read_bytes()).hexdigest())
    old = {item["old_base_scheme_id"]: SimpleNamespace(code_hash=item["old_code_hash"]) for item in report["targets"]}
    new = {item["old_base_scheme_id"]: SimpleNamespace(code_hash=item["new_code_hash"], tenors=[item["target_tenor"]]) for item in report["targets"]}
    proof = prepare._equivalence(old, new, new, dict.fromkeys(prepare.W2_IDS, {"verified": True}))
    assert all(value["comparison"]["request_count"] == 333 for value in proof.values())
    assert all(value["comparison"]["input_identity"]["generation_id"] == "reviewed-old-generation" for value in proof.values())
    source.write_text("{}")
    with pytest.raises(RuntimeError, match="report changed"):
        prepare._equivalence(old, new, new, {})


def test_failure_receipt_disk_error_does_not_hide_error_or_skip_failed_gate(prepared, monkeypatch):
    env = prepared
    original = prepare._write_receipt

    def failing_receipt(path, value):
        if path.name.endswith((".execution.json", ".failed.json")):
            raise OSError("disk full")
        return original(path, value)

    monkeypatch.setattr(prepare, "_write_receipt", failing_receipt)
    with pytest.raises(OSError, match="disk full") as caught:
        prepare.execute_w2_reclaim_prepare(None, **env.kwargs)
    assert env.algorithm.call_count == 1
    assert env.complete.call_args.kwargs["status"] == "failed"
    assert any("Failed file receipt" in note for note in caught.value.__notes__)
    assert len(list(env.kwargs["work_dir"].glob("*.process.json"))) == 1


def test_database_checks_identity_schema_writer_and_duplicate_preparation(monkeypatch):
    engine = MagicMock()
    engine.dialect.name = "mysql"
    conn = engine.connect.return_value.__enter__.return_value
    conn.execute.return_value.mappings.return_value.one.return_value = {"database_name": "test", "server_uuid": "isolated"}
    conn.execute.return_value.scalar_one.return_value = 0
    new = {key: SimpleNamespace(scheme_version="new") for key in prepare.W2_IDS}
    sources = {key: SimpleNamespace(**{field: "source" for field in prepare._FIELDS}) for key in prepare.W2_IDS}
    registry = [dict(base_scheme_id=key + suffix, status=status, runtime_type=runtime)
        for key in prepare.W2_IDS for suffix, status, runtime in (("", "archived", "native_adapter"), ("_bbv2", "active", "blackbox_v2"))]
    tables = {"t_schema_migrations": [{"version": 24, "state": "APPLIED"}], "t_harness_runs": [],
              "t_scheme_registry": registry, "t_scheme_versions": [dict(scheme_id=key + "_bbv2", status="active",
                **{field: "source" for field in prepare._FIELDS}) for key in prepare.W2_IDS]}
    monkeypatch.setattr(prepare, "_same_id_rows_conn", lambda _c, table, *_a, **_k: tables[table])
    monkeypatch.setattr(prepare, "_same_id_fact_snapshot_conn", lambda *_a, **_k: {"t_harness_runs": {}, "t_harness_gate_results": {}, "facts": "frozen"})
    kwargs = dict(sources=sources, new=new, expected_database_name="test", expected_server_uuid="isolated")
    assert prepare._database_snapshot(engine, **kwargs)["facts"] == {"facts": "frozen"}
    for status in ("passed", "failed", "running"):
        tables["t_harness_runs"] = [dict(harness_run_id="existing", scheme_id=prepare.W2_IDS[0],
                                          scheme_version="new", stage=prepare._STAGE, status=status)]
        with pytest.raises(RuntimeError, match="already exists"):
            prepare._database_snapshot(engine, **kwargs)
    tables["t_harness_runs"] = []
    with pytest.raises(RuntimeError, match="identity mismatch"):
        prepare._database_snapshot(engine, **(kwargs | {"expected_server_uuid": "wrong"}))
    tables["t_schema_migrations"][0]["state"] = "APPLYING"
    with pytest.raises(RuntimeError, match="schema 024"):
        prepare._database_snapshot(engine, **kwargs)
