"""迁移专用原身份回收控制边界；不连接生产或启动算法。"""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness import cli, writer_reclaim as reclaim
from scheduler.discovery import load_scheme_config


ROOT = Path(__file__).resolve().parents[1]


def _runs(wave):
    return {key: f"reclaim-{index}" for index, key in enumerate(reclaim.RECLAIM_WAVES[wave])}


@pytest.mark.parametrize("wave", ["W2", "W3A"])
@pytest.mark.parametrize("action", ["preflight", "cutover", "rollback"])
def test_reclaim_uses_only_same_id_route(monkeypatch, wave, action):
    args = ["migrate-native-successor", action, "--wave", wave,
            "--reference-project-root", "/reference", "--expected-database-name", "test",
            "--expected-server-uuid", "test-only"]
    for key, run in _runs(wave).items():
        args.extend(["--harness-run-id", f"{key}={run}"])
    if action != "preflight":
        args.extend(["--expected-plan-sha256", "a" * 64, "--approved-by", "test"])
    target = "build_writer_reclaim_preflight" if action == "preflight" else "execute_writer_reclaim"
    called = MagicMock(return_value={"checked": True})
    engine = MagicMock()
    monkeypatch.setattr(cli, target, called)
    monkeypatch.setattr(cli, "create_engine_from_env", lambda: engine)
    assert cli._run_native_successor_migration_command(cli._build_parser().parse_args(args)) == {"checked": True}
    assert called.call_args.kwargs["harness_run_ids"] == _runs(wave)
    assert called.call_args.kwargs["wave"] == wave
    engine.dispose.assert_called_once()


def test_incomplete_or_fake_evidence_never_opens_database(monkeypatch):
    create = MagicMock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(cli, "create_engine_from_env", create)
    common = ["--wave", "W2", "--reference-project-root", "/reference",
              "--expected-database-name", "test", "--expected-server-uuid", "test-only"]
    args = cli._build_parser().parse_args(["migrate-native-successor", "preflight", *common])
    with pytest.raises(ValueError):
        cli._run_native_successor_migration_command(args)
    for values in [[], ["daily_5y_2_v28=x"], [f"{key}=same" for key in reclaim.RECLAIM_WAVES["W2"]]]:
        with pytest.raises(ValueError):
            reclaim.parse_reclaim_run_ids("W2", values)
    create.assert_not_called()


@pytest.mark.parametrize("action", ["preflight", "prepare"])
def test_w2_prepare_routes_without_caller_supplied_success(monkeypatch, action):
    called = MagicMock(return_value={"prepare": True})
    engine = MagicMock()
    monkeypatch.setattr(cli, "create_engine_from_env", lambda: engine)
    monkeypatch.setattr(cli, "build_w2_reclaim_prepare_preflight", called)
    monkeypatch.setattr(cli, "execute_w2_reclaim_prepare", called)
    args = ["migrate-native-successor", action, "--wave", "W2",
            "--reference-project-root", "/reference", "--expected-database-name", "test",
            "--expected-server-uuid", "test-only", "--predict-date", "2026-09-11"]
    args += ["--action", "prepare"] if action == "preflight" else [
        "--work-dir", "/fresh", "--expected-plan-sha256", "a" * 64, "--approved-by", "tester"]
    assert cli._run_native_successor_migration_command(cli._build_parser().parse_args(args)) == {"prepare": True}
    assert called.call_args.kwargs["predict_date"] == "2026-09-11"
    assert "harness_run_ids" not in called.call_args.kwargs
    if action == "preflight":
        parsed = cli._build_parser().parse_args(args + ["--harness-run-id", "daily_5y_2_v28=fake"])
        with pytest.raises(ValueError, match="creates its own real"):
            cli._run_native_successor_migration_command(parsed)


def test_w3a_prepare_remains_closed_before_database(monkeypatch):
    create = MagicMock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(cli, "create_engine_from_env", create)
    args = cli._build_parser().parse_args([
        "migrate-native-successor", "preflight", "--action", "prepare", "--wave", "W3A",
        "--reference-project-root", "/reference", "--expected-database-name", "test",
        "--expected-server-uuid", "test-only"])
    with pytest.raises(ValueError, match="W3A state preparation"):
        cli._run_native_successor_migration_command(args)
    create.assert_not_called()


def test_w3a_cannot_bypass_missing_state_and_rollback_evidence(monkeypatch):
    inspect = MagicMock(side_effect=AssertionError("must not inspect"))
    monkeypatch.setattr(reclaim, "_verified_pair", inspect)
    with pytest.raises(RuntimeError, match="state revision and rollback admission"):
        reclaim._capture(ROOT, ROOT, "W3A")
    inspect.assert_not_called()


def test_process_inspection_catches_manual_harness_without_predict_module(tmp_path, monkeypatch):
    process = tmp_path / "123"
    process.mkdir()
    (process / "cmdline").write_bytes(b"/env/bin/python3.13\0-m\0harness\0gate\0compare\0--scheme-id\0daily_5y_2_v28\0")
    monkeypatch.setattr(reclaim, "Path", lambda value: tmp_path if value == "/proc" else Path(value))
    monkeypatch.setattr(reclaim.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="matching Python process"):
        reclaim._assert_no_algorithm_process("W2")
    (process / "cmdline").write_bytes(b"/env/bin/python3.13\0-m\0harness\0--scheme-id\0unrelated\0")
    reclaim._assert_no_algorithm_process("W2")
    monkeypatch.setattr(reclaim.os, "getpid", lambda: 123)
    (process / "cmdline").write_bytes(b"/env/bin/python3.12\0-m\0harness\0migrate-native-successor\0cutover\0--wave\0W2\0--harness-run-id\0daily_5y_2_v28=run-1\0--harness-run-id\0daily_7y_1_v28=run-2\0")
    reclaim._assert_no_algorithm_process("W2")
    other = tmp_path / "124"
    other.mkdir()
    (other / "cmdline").write_bytes(b"/env/bin/python3.13\0-m\0harness\0gate\0compare\0--scheme-id\0daily_5y_2_v28_bbv2\0")
    with pytest.raises(RuntimeError, match="matching Python process"):
        reclaim._assert_no_algorithm_process("W2")


@pytest.fixture
def capture(tmp_path, monkeypatch):
    root, reference = tmp_path / "candidate", tmp_path / "reference"
    root.mkdir()
    reference.mkdir()
    module = root / "harness/writer_reclaim.py"
    module.parent.mkdir()
    module.write_text("# test module\n")
    monkeypatch.setattr(reclaim, "__file__", str(module))
    monkeypatch.setattr(reclaim, "_ROOT", root)
    current = tmp_path / "current"
    current.symlink_to(root, target_is_directory=True)
    monkeypatch.setitem(reclaim.control._CURRENT_LINKS, "aliyun-gray", current)
    monkeypatch.setattr(reclaim.control, "_assert_execution_modules", lambda *_: None)
    monkeypatch.setattr(reclaim.control, "_capture_installed_locale", lambda *_: {"verified": True})
    monkeypatch.setattr(reclaim.control, "_capture_scheduler_state", lambda **_: {"timer_fenced": True})
    monkeypatch.setattr(reclaim, "_assert_no_algorithm_process", lambda *_: None)
    monkeypatch.setenv("BFL_DEPLOYMENT_TARGET", "aliyun-gray")
    monkeypatch.setenv("BFL_RELEASE_COMMIT", "a" * 40)
    configs = {key: load_scheme_config(ROOT / "schemes" / key / "config.yaml")
               for key in reclaim.RECLAIM_WAVES["W2"]}
    old = {key: replace(cfg, runtime_type="native_adapter") for key, cfg in configs.items()}
    sources = {key: replace(cfg, scheme_id=key + "_bbv2") for key, cfg in configs.items()}
    monkeypatch.setattr(reclaim, "_verified_pair", lambda *_: (old, sources, configs, {"candidate": {"commit": "a" * 40}}))
    monkeypatch.setattr(reclaim, "load_environment_fingerprint", lambda *_args, **_kwargs: "e" * 64)
    data = tmp_path / "data"
    data.mkdir()
    files = {}
    for name in reclaim.control._DATABRIDGE_FILES:
        (data / name).write_bytes(b"fixture\n")
        files[name] = {"sha256": reclaim.control._sha256_file(data / name)}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": files}))
    snapshot = SimpleNamespace(data_dir=data, manifest_path=manifest, snapshot_id="snapshot",
                               generation_id="generation", business_digest="b" * 64)
    monkeypatch.setattr(reclaim, "get_ready_blackbox_snapshot", lambda **_: snapshot)
    return root, reference, current, data


def test_capture_requires_real_current_fence_and_input(capture, monkeypatch):
    root, reference, current, data = capture
    first = reclaim._capture(root, reference, "W2")[3]
    assert first == reclaim._capture(root, reference, "W2")[3]
    assert first["candidate_states"] == {}
    assert set(first["source_canonical_selection"]) == set(reclaim.RECLAIM_WAVES["W2"])
    current.unlink()
    current.symlink_to(reference, target_is_directory=True)
    with pytest.raises(RuntimeError, match="candidate current"):
        reclaim._capture(root, reference, "W2")
    current.unlink()
    current.symlink_to(root, target_is_directory=True)
    (data / "daily_output.csv").write_bytes(b"drift\n")
    with pytest.raises(RuntimeError, match="ready manifest"):
        reclaim._capture(root, reference, "W2")
    monkeypatch.setattr(reclaim.control, "_capture_scheduler_state", MagicMock(side_effect=RuntimeError("no fence")))
    with pytest.raises(RuntimeError, match="no fence"):
        reclaim._capture(root, reference, "W2")


def test_actual_gate_failure_and_in_transaction_drift_not_bypassed(capture, monkeypatch):
    root, reference, _current, _data = capture
    kwargs = dict(project_root=root, reference_project_root=reference, wave="W2",
                  harness_run_ids=_runs("W2"), action="cutover",
                  expected_database_name="test", expected_server_uuid="test")
    monkeypatch.setattr(reclaim.repository, "read_same_id_writer_reclaim_plan", MagicMock(side_effect=RuntimeError("missing Gate")))
    with pytest.raises(RuntimeError, match="missing Gate"):
        reclaim.build_writer_reclaim_preflight(None, **kwargs)
    def transaction(_engine, **inputs):
        monkeypatch.setenv("BFL_RELEASE_COMMIT", "b" * 40)
        return inputs["control_plane_evidence_reader"]()
    monkeypatch.setattr(reclaim.repository, "apply_same_id_writer_reclaim", transaction)
    with pytest.raises(RuntimeError, match="process commit"):
        reclaim.execute_writer_reclaim(None, **kwargs, expected_plan_sha256="a" * 64, approved_by="tester")
