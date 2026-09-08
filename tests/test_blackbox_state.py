"""Blackbox 派生状态的真实 CLI、完整性和原子发布合同。"""

from __future__ import annotations

import hashlib
import json
import os
import select
import stat
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from scheduler.blackbox_state import MAX_STATE_BYTES, StateBinding, StateSession
from scheduler.blackbox_v2_runner import (
    BlackboxExecutionError,
    RuntimeProfile,
    run_blackbox_backtest,
    run_blackbox_predict,
)
from shared.blackbox_v2.contracts import BlackboxMetadata, BlackboxRequest
from shared.exclusive_file_lock import ExclusiveFileLockUnavailable
from shared.exclusive_file_lock import ExclusiveFileLockPathChanged


_SCRIPT = '''
import argparse
import csv
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request")
parser.add_argument("--requests")
parser.add_argument("--data-dir")
parser.add_argument("--output")
parser.add_argument("--state-input")
parser.add_argument("--state-output")
args = parser.parse_args()
if args.mode == "predict":
    requests = [json.loads(Path(args.request).read_text())]
else:
    with open(args.requests, newline="") as source:
        requests = list(csv.DictReader(source))
state = json.loads(Path(args.state_input).read_bytes()) if args.state_input else {}
rows = []
for request in requests:
    key = request["request_id"]
    direction = state.setdefault(key, 1 if len(state) % 2 == 0 else -1)
    rows.append({**{name: request[name] for name in (
        "request_id", "predict_date", "feature_date", "target_date"
    )}, "predicted_direction": direction})
Path(args.state_output).write_text(json.dumps(state, sort_keys=True))
failure = requests[0]["request_id"]
if failure == "bad-result":
    rows[0]["unexpected"] = True
if failure == "change-state-input":
    Path(args.state_input).chmod(0o600)
    Path(args.state_input).write_text("tampered")
if failure == "change-data-input":
    Path(args.data_dir, "daily_output.csv").write_text("key,value\\n1,9\\n")
if failure == "change-request-input":
    Path(args.request or args.requests).write_text("changed request")
if failure == "state-symlink":
    Path(args.state_output).unlink()
    Path(args.state_output).symlink_to(args.request)
if failure == "state-hardlink":
    Path(args.state_output).unlink()
    os.link(args.request, args.state_output)
if failure == "state-large":
    with open(args.state_output, "wb") as target:
        target.truncate(16 * 1024 * 1024 + 1)
if failure == "state-missing":
    Path(args.state_output).unlink()
if args.mode == "predict":
    Path(args.output).write_text(json.dumps(rows[0]))
else:
    with open(args.output, "w", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
if failure == "nonzero":
    raise SystemExit(7)
'''


@pytest.fixture
def delivery(tmp_path: Path) -> dict:
    """创建只含脚本与 metadata 的轻量交付及独立四文件输入。"""
    tmp_path = tmp_path.resolve()
    package = tmp_path / "delivery"
    package.mkdir()
    metadata = BlackboxMetadata(
        schema_version="1.0", scheme_id="state_trial", name="State trial",
        algorithm_version="1.0.0", target_tenor="10Y", task_type="T+1",
        horizon=1, target_rule="target_date_yield_vs_feature_date_yield",
        frequency="daily",
    )
    script = package / "trial.py"
    script.write_text(_SCRIPT, encoding="utf-8")
    script.with_suffix(".json").write_text(json.dumps(asdict(metadata)))
    data = tmp_path / "data"
    data.mkdir()
    for name in ("daily_output.csv", "weekly_output.csv", "monthly_output.csv"):
        (data / name).write_text("key,value\n1,1\n")
    (data / "api_wind_date.csv").write_text("rdate,week_id\n2026-07-15,202627\n")
    return dict(metadata=metadata, script_path=script, data_dir=data,
                data_snapshot_id="snapshot-1", profile=RuntimeProfile.for_tests())


def _request(request_id: str = "first") -> BlackboxRequest:
    return BlackboxRequest(
        request_id=request_id, predict_date="2026-07-15",
        feature_date="2026-07-15", target_date="2026-07-16",
        daily_cutoff_key="2026-07-15", weekly_cutoff_key="202627",
        monthly_cutoff_key="202606",
    )


def _binding(tmp_path: Path, *, rebuild: bool = False) -> StateBinding:
    return StateBinding("state_trial", "version-1", "generation-1",
                        root=tmp_path.resolve() / "state", rebuild=rebuild)


def _canonical(binding: StateBinding) -> Path:
    assert binding.root is not None
    return binding.root / binding.scheme_id / f"{binding.scheme_version}.state"


def _initialize(delivery: dict, tmp_path: Path) -> StateBinding:
    binding = _binding(tmp_path, rebuild=True)
    run_blackbox_backtest(**delivery, requests=[_request()], state=binding)
    return replace(binding, rebuild=False)


def test_explicit_rebuild_predict_reuse_and_retry(delivery: dict, tmp_path: Path) -> None:
    binding = _initialize(delivery, tmp_path)
    first = run_blackbox_predict(**delivery, request=_request(), state=binding)
    assert first.predicted_direction == 1
    assert first.extra["state_scope"] == "persistent"
    before = _canonical(binding).read_bytes()
    retry = run_blackbox_predict(**delivery, request=_request(), state=binding)
    assert retry.predicted_direction == first.predicted_direction
    assert _canonical(binding).read_bytes() == before
    second = run_blackbox_predict(**delivery, request=_request("second"), state=binding)
    assert second.predicted_direction == -1
    assert second.extra["state_input_sha256"] == first.extra["state_output_sha256"]
    assert _canonical(binding).read_bytes() != before


def test_actual_runtime_environment_change_rejects_state(delivery, tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    binding = _initialize(delivery, tmp_path)
    before = _canonical(binding).read_bytes()
    monkeypatch.setenv("TZ", "UTC")
    with pytest.raises(ValueError, match="identity mismatch"):
        run_blackbox_predict(**delivery, request=_request(), state=binding)
    assert _canonical(binding).read_bytes() == before


def test_replaced_lock_cannot_publish_over_new_holder(tmp_path):
    binding = _seed_session(tmp_path)
    before = _canonical(binding).read_bytes()
    with _session(binding, tmp_path.resolve() / "first-holder") as first:
        lock = _canonical(binding).parent / "state.lock"
        lock.rename(lock.with_name("displaced.lock"))
        with _session(binding, tmp_path.resolve() / "second-holder"):
            first.output_path.write_bytes(b"must not publish")
            with pytest.raises(ExclusiveFileLockPathChanged):
                first.publish()
    assert _canonical(binding).read_bytes() == before


def test_canonical_binding_rejects_symlink_root_and_version_drift(tmp_path, monkeypatch):
    from scheduler.blackbox_v2_runner import state_binding_for_scheme
    from scheduler.discovery import load_scheme_config
    from shared.blackbox_v2.intake import intake_delivery

    root = tmp_path.resolve()
    incoming = root / "incoming"
    incoming.mkdir()
    (incoming / "state_trial.py").write_text("pass\n")
    metadata = dict(schema_version="1.0", scheme_id="state_trial", name="试点", owner="测试方",
                    description="状态合同验证", algorithm_version="1", target_tenor="10Y",
                    task_type="T+1", horizon=1, target_rule="target_date_yield_vs_feature_date_yield")
    (incoming / "state_trial.json").write_text(json.dumps(metadata))
    scheme = intake_delivery(incoming, schemes_root=root / "schemes", incremental_state=True)
    cfg = load_scheme_config(scheme / "config.yaml")
    runtime = root / "runtime"
    runtime.mkdir()
    target = root / "elsewhere"
    target.mkdir(mode=0o700)
    (runtime / "blackbox-state").symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("BFL_RUNTIME_ROOT", str(runtime))
    with pytest.raises(ValueError, match="symlink"):
        state_binding_for_scheme(cfg, generation_id="generation-1", persistent=True)
    config_path = scheme / "config.yaml"
    config_path.write_text(config_path.read_text().replace("timeout_sec: 3600", "timeout_sec: 3599"))
    with pytest.raises(ValueError, match="exact version changed"):
        state_binding_for_scheme(cfg, generation_id="generation-1")


def test_private_backtest_leaves_persistent_state_untouched(delivery: dict, tmp_path: Path) -> None:
    binding = _initialize(delivery, tmp_path)
    before = _canonical(binding).read_bytes()
    private = replace(binding, root=None)
    records = run_blackbox_backtest(**delivery, requests=[_request("second")], state=private)
    assert records[0].predicted_direction == 1
    assert records[0].extra["state_scope"] == "private"
    assert records[0].extra["state_input_sha256"] is None
    assert _canonical(binding).read_bytes() == before
    with pytest.raises(ValueError, match="backtest cannot advance"):
        run_blackbox_backtest(**delivery, requests=[_request("second")], state=binding)
    assert _canonical(binding).read_bytes() == before


@pytest.mark.parametrize("mode,rebuild,count,predict_budget,offline_budget,caller_budget,expected", [
    ("backtest", False, 1, 3600, 14400, None, 7200),
    ("backtest", False, 101, 3600, 14400, None, 7200),
    ("backtest", True, 1, 3600, 14400, None, 7200),
    ("backtest", False, 1, 3600, 300, None, 300),
    ("predict", False, 1, 3600, 14400, 7200, 120),
    ("predict", True, 1, 3600, 14400, None, 7200),
    ("predict", True, 1, 3600, 300, None, 300),
    ("predict", True, 1, 3600, 14400, 60, 60),
    ("predict", False, 1, 60, 14400, None, 60),
])
def test_stateful_execution_budgets(
    delivery, tmp_path, monkeypatch, mode, rebuild, count,
    predict_budget, offline_budget, caller_budget, expected,
):
    """真实 CLI 使用离线安全预算，且不放宽每日与调用方资源限制。"""
    from scheduler import blackbox_v2_runner as runner

    binding = _initialize(delivery, tmp_path)
    if mode == "backtest" and not rebuild:
        binding = replace(binding, root=None)
    binding = replace(binding, rebuild=rebuild)
    profile = replace(delivery["profile"], predict_timeout_sec=predict_budget,
                      backtest_timeout_sec=offline_budget, cpu_threads=16,
                      memory_limit_bytes=8 * 1024**3)
    run_process = runner._run_process
    observed = []

    def execute(command, **kwargs):
        observed.append(kwargs["timeout"])
        assert kwargs["memory_limit_bytes"] == 4 * 1024**3
        assert kwargs["env"]["OMP_NUM_THREADS"] == "8"
        return run_process(command, **kwargs)

    monkeypatch.setattr(runner, "_run_process", execute)
    arguments = {**delivery, "profile": profile, "state": binding}
    if caller_budget is not None:
        arguments["timeout_sec"] = caller_budget
    if mode == "predict":
        # 资源预算不进入状态算法身份；显式重建与常规调用各自受限。
        runner.run_blackbox_predict(**arguments, request=_request())
    else:
        records = runner.run_blackbox_backtest(
            **arguments, requests=[_request(f"item-{index}") for index in range(count)],
        )
        assert len(records) == count
    assert observed == [expected]


@pytest.mark.parametrize("request_id", ["bad-result", "nonzero", "state-missing", "change-request-input"])
def test_failed_initial_rebuild_does_not_create_canonical_state(
    delivery: dict, tmp_path: Path, request_id: str,
) -> None:
    binding = _binding(tmp_path, rebuild=True)
    with pytest.raises((ValueError, OSError, BlackboxExecutionError)):
        run_blackbox_backtest(**delivery, requests=[_request(request_id)], state=binding)
    assert not _canonical(binding).exists()
    assert not list(_canonical(binding).parent.glob(".state-*"))
    with pytest.raises(ValueError, match="missing"):
        run_blackbox_predict(**delivery, request=_request(), state=replace(binding, rebuild=False))


@pytest.mark.parametrize("request_id", [
    "bad-result", "nonzero", "change-state-input", "change-data-input", "change-request-input",
    "state-symlink", "state-hardlink", "state-large", "state-missing",
])
def test_cli_failure_never_publishes_state(
    delivery: dict, tmp_path: Path, request_id: str,
) -> None:
    binding = _initialize(delivery, tmp_path)
    before = _canonical(binding).read_bytes()
    with pytest.raises((ValueError, OSError, BlackboxExecutionError)):
        run_blackbox_predict(**delivery, request=_request(request_id), state=binding)
    assert _canonical(binding).read_bytes() == before
    assert not list(_canonical(binding).parent.glob(".state-*"))


@pytest.mark.parametrize("damage", ["checksum", "missing", "identity", "symlink", "hardlink", "large"])
def test_invalid_canonical_state_is_rejected(
    delivery: dict, tmp_path: Path, damage: str,
) -> None:
    binding = _initialize(delivery, tmp_path)
    canonical = _canonical(binding)
    if damage == "checksum":
        content = canonical.read_bytes()
        canonical.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    elif damage == "missing":
        canonical.unlink()
    elif damage == "identity":
        delivery = {**delivery, "metadata": replace(delivery["metadata"], name="Changed")}
    elif damage in {"symlink", "hardlink"}:
        source = tmp_path / "other.state"
        canonical.rename(source)
        if damage == "symlink":
            canonical.symlink_to(source)
        else:
            os.link(source, canonical)
    else:
        with canonical.open("wb") as target:
            target.truncate(MAX_STATE_BYTES + 128 * 1024)
    before = canonical.read_bytes() if canonical.exists() else None
    with pytest.raises((ValueError, OSError)):
        run_blackbox_predict(**delivery, request=_request(), state=binding)
    assert (canonical.read_bytes() if canonical.exists() else None) == before


def _session(binding: StateBinding, work: Path) -> StateSession:
    work.mkdir(parents=True)
    (work / "output").mkdir()
    return StateSession(binding, work_dir=work, identity={"script_sha256": "a" * 64},
                        input_identity={"schema": "data-bridge-v1", "snapshot_id": "snapshot-1"})


def _seed_session(tmp_path: Path) -> StateBinding:
    binding = _binding(tmp_path, rebuild=True)
    with _session(binding, tmp_path.resolve() / "seed") as session:
        assert session.input_path is None
        session.output_path.write_bytes(b"opaque\x00old")
        session.publish()
    return replace(binding, rebuild=False)


@pytest.mark.parametrize("root_kind", ["relative", "symlink", "public"])
def test_persistent_root_must_be_absolute_private_and_symlink_free(
    tmp_path: Path, root_kind: str,
) -> None:
    root = tmp_path.resolve() / "state"
    if root_kind == "relative":
        root = Path("relative-state")
    elif root_kind == "symlink":
        target = tmp_path.resolve() / "target"
        target.mkdir(mode=0o700)
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir(mode=0o755)
        root.chmod(0o755)
    binding = replace(_binding(tmp_path, rebuild=True), root=root)
    with pytest.raises(ValueError):
        with _session(binding, tmp_path.resolve() / "work"):
            pytest.fail("unsafe state root was accepted")


def test_failure_before_replace_preserves_old_state(tmp_path: Path, monkeypatch) -> None:
    binding = _seed_session(tmp_path)
    before = _canonical(binding).read_bytes()
    with _session(binding, tmp_path.resolve() / "attempt") as session:
        assert session.input_path.read_bytes() == b"opaque\x00old"
        session.output_path.write_bytes(b"opaque\x00new")
        with monkeypatch.context() as fault:
            def fail_replace(*args, **kwargs):
                raise OSError("injected before replace")
            fault.setattr("scheduler.blackbox_state.os.replace", fail_replace)
            with pytest.raises(OSError, match="injected before replace"):
                session.publish()
    assert _canonical(binding).read_bytes() == before
    assert not list(_canonical(binding).parent.glob(".state-*"))
    with _session(binding, tmp_path.resolve() / "retry") as retry:
        assert retry.input_path.read_bytes() == b"opaque\x00old"


def test_directory_fsync_failure_leaves_complete_new_state_for_retry(
    tmp_path: Path, monkeypatch,
) -> None:
    binding = _seed_session(tmp_path)
    before = _canonical(binding).read_bytes()
    real_fsync = os.fsync
    with _session(binding, tmp_path.resolve() / "attempt") as session:
        session.output_path.write_bytes(b"opaque\x00new")
        with monkeypatch.context() as fault:
            def fail_directory_fsync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("injected directory fsync failure")
                return real_fsync(fd)
            fault.setattr("scheduler.blackbox_state.os.fsync", fail_directory_fsync)
            with pytest.raises(OSError, match="injected directory fsync"):
                session.publish()
    assert _canonical(binding).read_bytes() != before
    assert not list(_canonical(binding).parent.glob(".state-*"))
    with _session(binding, tmp_path.resolve() / "retry") as retry:
        assert retry.input_path.read_bytes() == b"opaque\x00new"
        retry.output_path.write_bytes(retry.input_path.read_bytes())
        audit = retry.publish()
    assert audit["state_output_sha256"] == hashlib.sha256(b"opaque\x00new").hexdigest()


def test_hardlinked_lock_file_is_rejected_without_touching_link_target(tmp_path: Path) -> None:
    binding = _seed_session(tmp_path)
    lock = _canonical(binding).parent / "state.lock"
    target = tmp_path.resolve() / "linked-lock"
    os.link(lock, target)
    target.chmod(0o640)
    before = target.stat()
    with pytest.raises((ValueError, OSError)):
        with _session(binding, tmp_path.resolve() / "attempt"):
            pytest.fail("hardlinked state lock was accepted")
    after = target.stat()
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)


def test_state_lock_excludes_a_real_second_process_and_releases(tmp_path: Path) -> None:
    binding = _seed_session(tmp_path)
    child_source = '''
import sys
from pathlib import Path
from scheduler.blackbox_state import StateBinding, StateSession
root, work = map(Path, sys.argv[1:])
work.mkdir()
(work / "output").mkdir()
binding = StateBinding("state_trial", "version-1", "generation-1", root=root)
with StateSession(binding, work_dir=work, identity={"script_sha256": "a" * 64},
                  input_identity={"schema": "data-bridge-v1", "snapshot_id": "snapshot-1"}):
    print("locked", flush=True)
    sys.stdin.readline()
'''
    child = subprocess.Popen(
        [sys.executable, "-c", child_source, str(binding.root), str(tmp_path.resolve() / "child")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        ready, _, _ = select.select([child.stdout], [], [], 15)
        assert ready, "child did not acquire the lock"
        assert child.stdout.readline().strip() == "locked"
        before = _canonical(binding).read_bytes()
        with pytest.raises(ExclusiveFileLockUnavailable):
            with _session(binding, tmp_path.resolve() / "contender"):
                pytest.fail("second process entered a locked state session")
        assert _canonical(binding).read_bytes() == before
        _, stderr = child.communicate("release\n", timeout=15)
        assert child.returncode == 0, stderr
        with _session(binding, tmp_path.resolve() / "after-release") as session:
            assert session.input_path.read_bytes() == b"opaque\x00old"
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=15)
