"""W3B 临时迁移控制边界；所有现场接口由独立测试数据替代。"""

from dataclasses import replace
import os
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text

from harness import cli
from harness import same_id_runtime_upgrade as control
from scheduler.discovery import load_scheme_config


ROOT = Path(__file__).resolve().parents[1]


def _runs():
    return {key: f"run-{index}" for index, key in enumerate(control.W3B_IDS)}


def _args(action="preflight"):
    args = ["migrate-native-successor", action, "--wave", "W3B", "--project-root", "/candidate",
            "--reference-project-root", "/reference", "--expected-database-name", "test",
            "--expected-server-uuid", "test-only"]
    for key, value in _runs().items():
        args.extend(["--harness-run-id", f"{key}={value}"])
    if action != "preflight":
        args.extend(["--expected-plan-sha256", "a" * 64, "--approved-by", "tester"])
    return args


@pytest.mark.parametrize("action", ["preflight", "cutover", "rollback"])
def test_only_same_id_cli_route_is_reachable(monkeypatch, action):
    preflight = MagicMock(return_value={"passed": True})
    execute = MagicMock(return_value={"passed": True})
    engine = MagicMock()
    monkeypatch.setattr(cli, "build_same_id_preflight", preflight)
    monkeypatch.setattr(cli, "execute_same_id_upgrade", execute)
    monkeypatch.setattr(cli, "create_engine_from_env", lambda: engine)
    parsed = cli._build_parser().parse_args(_args(action))
    cli._run_native_successor_migration_command(parsed)
    called = preflight if action == "preflight" else execute
    assert called.call_count == 1
    assert called.call_args.kwargs["wave"] == "W3B"
    assert called.call_args.kwargs["harness_run_ids"] == _runs()
    engine.dispose.assert_called_once()


@pytest.mark.parametrize("args", [
    ["migrate-native-successor", "prepare-equivalence"],
    ["migrate-native-successor", "cutover", "--wave", "W3A"],
    _args() + ["--old-scheme-id", control.W3B_IDS[0]],
    _args() + ["--control-plane-json", "/fake.json"],
])
def test_old_cross_id_or_user_supplied_capture_routes_rejected(args):
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(args)


@pytest.mark.parametrize("values", [[], ["unapproved=run"],
    [f"{control.W3B_IDS[0]}=run"] * 3,
    [f"{key}=same-run" for key in control.W3B_IDS],
])
def test_only_complete_distinct_w3b_runs_accepted(values):
    with pytest.raises(ValueError):
        control.parse_w3b_harness_run_ids(values)


def test_unsupported_wave_blocks_before_any_capture(monkeypatch):
    capture = MagicMock(side_effect=AssertionError("must not capture"))
    monkeypatch.setattr(control, "_capture", capture)
    with pytest.raises(ValueError, match="complete W3B"):
        control.build_same_id_preflight(None, project_root=ROOT, reference_project_root=ROOT,
            wave="W3A", harness_run_ids=_runs(), action="cutover",
            expected_database_name="test", expected_server_uuid="test")
    capture.assert_not_called()


@pytest.fixture
def capture_environment(tmp_path, monkeypatch):
    root = tmp_path / "candidate"
    reference = tmp_path / "reference"
    root.mkdir()
    reference.mkdir()
    current = tmp_path / "current"
    current.symlink_to(root, target_is_directory=True)
    monkeypatch.setitem(control._CURRENT_LINKS, "aliyun-gray", current)
    monkeypatch.setattr(control, "_EXECUTION_PROJECT_ROOT", root)
    monkeypatch.setattr(control, "_assert_execution_modules", lambda *_: None)
    monkeypatch.setattr(control, "_capture_installed_locale", lambda *_: {"effective_locale": {"LANG": "en_US.UTF-8"}})
    monkeypatch.setenv("BFL_DEPLOYMENT_TARGET", "aliyun-gray")
    monkeypatch.setenv("BFL_RELEASE_COMMIT", "a" * 40)
    cfg = load_scheme_config(ROOT / "schemes" / (control.W3B_IDS[0] + "_bbv2") / "config.yaml")
    configs = {key: replace(cfg, scheme_id=key) for key in control.W3B_IDS}
    releases = {"candidate": {"commit": "a" * 40}, "reference": {"commit": "b" * 40}}
    monkeypatch.setattr(control, "_verified_pair", lambda *_: ({}, configs, releases))
    monkeypatch.setattr(control, "_assert_no_algorithm_process", lambda: None)
    monkeypatch.setattr(control, "_assert_no_temporary_writer", lambda *_: {"temporary_active_or_running_count": 0})
    monkeypatch.setattr(control, "_capture_scheduler_state", lambda **_: {"timer_fenced": True})
    monkeypatch.setattr(control, "load_environment_fingerprint", lambda *_args, **_kwargs: "e" * 64)
    data = tmp_path / "data"
    data.mkdir()
    files = {}
    for name in control._DATABRIDGE_FILES:
        (data / name).write_bytes(b"test\n")
        files[name] = {"sha256": control._sha256_file(data / name)}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": files}))
    snapshot = SimpleNamespace(manifest_path=manifest, data_dir=data, snapshot_id="snapshot", generation_id="generation", business_digest="b" * 64)
    monkeypatch.setattr(control, "get_ready_blackbox_snapshot", lambda **_: snapshot)
    monkeypatch.setattr(control, "_read_candidate_states", lambda *_: {"verified": "state-digest"})
    return root, reference, current, data


def test_capture_rejects_missing_fence_and_wrong_current(capture_environment, monkeypatch):
    root, reference, current, _ = capture_environment
    current.unlink()
    current.symlink_to(reference, target_is_directory=True)
    with pytest.raises(RuntimeError, match="candidate current"):
        control._capture(None, root, reference)
    current.unlink()
    current.symlink_to(root, target_is_directory=True)
    monkeypatch.setattr(control, "_capture_scheduler_state", MagicMock(side_effect=RuntimeError("timer not fenced")))
    with pytest.raises(RuntimeError, match="not fenced"):
        control._capture(None, root, reference)


def test_capture_binds_actual_state_and_input_without_volatile_paths(capture_environment):
    root, reference, _, data = capture_environment
    first = control._capture(None, root, reference)[2]
    second = control._capture(None, root, reference)[2]
    assert first == second
    assert first["candidate_states"] == {"verified": "state-digest"}
    (data / "daily_output.csv").write_bytes(b"changed\n")
    with pytest.raises(RuntimeError, match="DataBridge file"):
        control._capture(None, root, reference)


def test_db_gate_failure_is_not_replaced_by_synthetic_success(capture_environment, monkeypatch):
    root, reference, _, _ = capture_environment
    monkeypatch.setattr(control, "read_same_id_runtime_upgrade_plan", MagicMock(side_effect=RuntimeError("Harness evidence is absent")))
    with pytest.raises(RuntimeError, match="evidence is absent"):
        control.build_same_id_preflight(None, project_root=root, reference_project_root=reference,
            wave="W3B", harness_run_ids=_runs(), action="cutover",
            expected_database_name="test", expected_server_uuid="test")


def test_apply_callback_rechecks_candidate_in_repository_transaction(capture_environment, monkeypatch):
    root, reference, _, _ = capture_environment
    capture = control._capture
    count = 0

    def changing_capture(*args):
        nonlocal count
        count += 1
        if count > 1:
            raise RuntimeError("candidate source tree changed")
        return capture(*args)

    def transaction(_engine, **kwargs):
        return kwargs["control_plane_evidence_reader"]()

    monkeypatch.setattr(control, "_capture", changing_capture)
    monkeypatch.setattr(control, "apply_same_id_runtime_upgrade", transaction)
    with pytest.raises(RuntimeError, match="source tree changed"):
        control.execute_same_id_upgrade(None, project_root=root, reference_project_root=reference,
            wave="W3B", harness_run_ids=_runs(), action="cutover", expected_plan_sha256="a" * 64,
            approved_by="tester", expected_database_name="test", expected_server_uuid="test")
    assert count == 2


@pytest.mark.parametrize("table,column,status", [
    ("t_scheme_registry", "base_scheme_id", "active"),
    ("t_scheme_versions", "scheme_id", "active"),
    ("t_scheme_runs", "scheme_id", "running"),
    ("t_backtest_runs", "scheme_id", "running"),
    ("t_harness_runs", "scheme_id", "running"),
])
def test_temporary_second_writer_blocks_even_without_original_id_conflict(table, column, status):
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as conn:
            for name in ("t_scheme_registry", "t_scheme_versions", "t_scheme_runs", "t_backtest_runs", "t_harness_runs"):
                identity = "base_scheme_id" if name == "t_scheme_registry" else "scheme_id"
                conn.exec_driver_sql(f"CREATE TABLE {name} ({identity} TEXT, status TEXT)")
            conn.execute(text(f"INSERT INTO {table} VALUES (:id,:status)"),
                         {"id": control.W3B_IDS[0] + "_bbv2", "status": status})
        with pytest.raises(RuntimeError, match="second Writer"):
            control._assert_no_temporary_writer(engine)
    finally:
        engine.dispose()


def test_immutable_install_tamper_and_writable_tree_rejected(tmp_path, monkeypatch):
    commit = "a" * 40
    root = tmp_path / "releases" / commit
    root.mkdir(parents=True)
    source = root / "source.py"
    source.write_bytes(b"pass\n")
    record = {"schema_version": "bfl-source-release-install-v1", "commit": commit,
              "archive_sha256": "b" * 64, "source_tree_sha256": control._source_tree_sha256(root),
              "runtime_root": "/isolated/state"}
    receipt = root / ".bfl-release-install.json"
    receipt.write_text(json.dumps(record))
    from scripts.install_source_release import _release_environment
    environment = root / ".bfl-release.env"
    environment.write_text(_release_environment(commit=commit, runtime_root=Path("/isolated/state")))
    monkeypatch.setitem(control._CURRENT_LINKS, "aliyun-gray", tmp_path / "current")
    monkeypatch.setenv("BFL_RUNTIME_ROOT", "/isolated/state")
    with pytest.raises(RuntimeError, match="read-only"):
        control._verified_install(root)
    source.chmod(0o444)
    receipt.chmod(0o444)
    environment.chmod(0o444)
    root.chmod(0o555)
    try:
        assert control._verified_install(root)["commit"] == commit
        source.chmod(0o644)
        source.write_bytes(b"changed\n")
        source.chmod(0o444)
        with pytest.raises(RuntimeError, match="source tree changed"):
            control._verified_install(root)
    finally:
        root.chmod(0o755)
        source.chmod(0o644)
        receipt.chmod(0o644)
        environment.chmod(0o644)


@pytest.mark.parametrize("command,blocked", [
    (f"python -m scheduler.scheme_runner --scheme-id {control.W3B_IDS[0]} --predict-date 2026-09-12", True),
    (f"python /release/delivery/{control.W3B_IDS[0]}_bbv2.py predict --request /request.json", True),
    (f"python -m harness migrate-native-successor preflight --harness-run-id {control.W3B_IDS[0]}=run", False),
])
def test_actual_algorithm_argv_checked_without_matching_migration_cli(monkeypatch, command, blocked):
    def inspect(args, **kwargs):
        pattern = args[-1].replace("[[:space:]]", r"\s")
        found = re.search(pattern, command) is not None
        return SimpleNamespace(returncode=0 if found else 1)
    monkeypatch.setattr(control.subprocess, "run", inspect)
    if blocked:
        with pytest.raises(RuntimeError, match="algorithm process"):
            control._assert_no_algorithm_process()
    else:
        control._assert_no_algorithm_process()


def test_worktree_modules_cannot_claim_installed_candidate(tmp_path):
    with pytest.raises(RuntimeError, match="modules must all originate"):
        control._assert_execution_modules(tmp_path)


def test_installed_locale_and_conda_must_match_shell(tmp_path, monkeypatch):
    from scheduler import blackbox_v2_runner as runner
    root = tmp_path / "release"
    root.mkdir()
    (root / ".bfl-release.env").write_text("BFL_RELEASE_COMMIT=ignored\n")
    env = tmp_path / "service.env"
    env.write_text("BOND_DB_PASSWORD=private-test-not-used\n")
    conda = tmp_path / "conda"
    conda.write_text("test-only")
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    monkeypatch.setattr(control, "_SERVICE_ENV_FILE", env)
    monkeypatch.setattr(control, "_ECS_BLACKBOX_PREFIX", prefix)
    profile = SimpleNamespace(environment_defaults={"LANG": "C.UTF-8", "TZ": "Asia/Shanghai"}, environment_allowlist=("LANG", "LC_ALL", "TZ"))
    monkeypatch.setattr(runner, "_load_runtime_profile", lambda *_: profile)
    monkeypatch.setattr(runner, "_python_runtime", lambda *_: SimpleNamespace(prefix=prefix, executable=prefix / "python"))
    unit = "Environment=PATH=/installed/bin\nEnvironmentFiles=/etc/bond-factor-lab/bond-factor-lab.env (ignore_errors=no) /opt/bond-factor-lab/current/.bfl-release.env (ignore_errors=no)\nPassEnvironment=\nUnsetEnvironment=\n"
    monkeypatch.setattr(control.subprocess, "check_output", lambda args, **_: "LANG=en_US.UTF-8\n" if "show-environment" in args else unit)
    monkeypatch.setattr(control.shutil, "which", lambda *_, **__: str(conda))
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    first = control._capture_installed_locale(root)
    assert first["effective_locale"] == {"LANG": "en_US.UTF-8", "TZ": "Asia/Shanghai"}
    assert "PASSWORD" not in json.dumps(first)
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    with pytest.raises(RuntimeError, match="shell locale"):
        control._capture_installed_locale(root)
    monkeypatch.delenv("LC_ALL")
    monkeypatch.setattr(control.shutil, "which", lambda *_, **kwargs: str(conda) if "path" in kwargs else None)
    with pytest.raises(RuntimeError, match="shell conda"):
        control._capture_installed_locale(root)


def test_hardlinked_snapshot_uses_private_view_and_real_readonly_state_session(tmp_path, monkeypatch):
    from scheduler import blackbox_v2_runner as runner
    from scheduler.blackbox_state import StateBinding, _encode
    from shared.blackbox_v2.snapshot import create_snapshot_from_frames
    from shared.input_artifacts import open_blackbox_runtime_view
    from test_blackbox_v2_harness_gates import _snapshot_frames

    frames = _snapshot_frames()
    snapshot = create_snapshot_from_frames(frames, output_root=tmp_path / "snapshots",
        expected_columns={name: list(frame.columns) for name, frame in frames.items()}, schema_version="data-bridge-v1")
    snapshot = replace(snapshot, generation_id="generation-test")
    os.link(snapshot.data_dir / "daily_output.csv", tmp_path / "producer-link")
    with pytest.raises(ValueError, match="hardlink"):
        runner._validate_data_dir(snapshot.data_dir)
    base = control.W3B_IDS[0]
    state_root = tmp_path / "states"
    state_root.mkdir(mode=0o700)
    parent = state_root / base
    parent.mkdir(mode=0o700)
    (parent / "state.lock").touch(mode=0o600)
    identity = {"runtime_sha256": "runtime"}
    payload = b"reviewed-derived-state"
    import hashlib
    encoded = _encode({"identity": identity | {"scheme_id": base, "scheme_version": "version-test"},
        "input": {"schema": "data-bridge-v1"}, "payload_sha256": hashlib.sha256(payload).hexdigest()}, payload)
    destination = parent / "version-test.state"
    destination.write_bytes(encoded)
    binding = StateBinding(scheme_id=base, scheme_version="version-test", generation_id="generation-test", root=state_root)
    monkeypatch.setattr(runner, "state_binding_for_scheme", lambda *_args, **_kwargs: binding)
    monkeypatch.setattr(runner, "_load_runtime_profile", lambda *_: object())
    observed = []

    def identities(_metadata, _script, data_dir, snapshot_id, _profile):
        runner._validate_data_dir(data_dir)
        assert data_dir != snapshot.data_dir
        assert snapshot_id == snapshot.snapshot_id
        observed.append(data_dir)
        return identity, {"schema": "data-bridge-v1"}

    monkeypatch.setattr(runner, "_state_identities", identities)
    monkeypatch.setattr(control, "open_blackbox_runtime_view", lambda bundle: open_blackbox_runtime_view(bundle, runtime_root=tmp_path / "views"))
    cfg = SimpleNamespace(scheme_version="version-test", blackbox_metadata=SimpleNamespace(scheme_id=base), delivery_script=tmp_path / "not-executed.py")
    first = control._read_candidate_states(tmp_path, {base: cfg}, snapshot)
    second = control._read_candidate_states(tmp_path, {base: cfg}, snapshot)
    assert first == second
    assert first[base]["envelope_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert destination.read_bytes() == encoded
    assert len(observed) == 2 and all(not path.exists() for path in observed)
