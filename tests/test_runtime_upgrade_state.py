"""迁移期状态接纳的发布边界；不重复运行重型 W3B 算法。"""

from dataclasses import asdict, replace
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest

from harness import runtime_upgrade_state as admission
from scheduler import blackbox_v2_runner as runner
from scheduler.blackbox_state import _MAX_ENVELOPE_BYTES, _decode, read_regular_bytes
from scheduler.discovery import load_scheme_config
from shared.blackbox_v2.contracts import BlackboxRequest, BlackboxResult


ROOT = Path(__file__).resolve().parents[1]
BASE = "liwei_0616_10y02_cons_say_k3_div_k5"


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    source_dir = root / (BASE + "_bbv2")
    shutil.copytree(ROOT / "schemes" / source_dir.name, source_dir)
    candidate_dir = root / BASE
    delivery = candidate_dir / "delivery"
    delivery.mkdir(parents=True)
    script = source_dir / "delivery" / (source_dir.name + ".py")
    shutil.copyfile(script, delivery / (BASE + ".py"))
    metadata = json.loads(script.with_suffix(".json").read_bytes())
    metadata["scheme_id"] = BASE
    (delivery / (BASE + ".json")).write_text(json.dumps(metadata))
    (candidate_dir / "config.yaml").write_text(
        (source_dir / "config.yaml").read_text().replace(source_dir.name, BASE)
    )
    source = load_scheme_config(source_dir / "config.yaml")
    candidate = load_scheme_config(candidate_dir / "config.yaml")
    monkeypatch.setenv("BFL_RUNTIME_ROOT", str(root / "runtime"))
    profile = runner.RuntimeProfile.for_tests(cpu_threads=8, memory_limit_bytes=4 * 1024**3,
                                               predict_timeout_sec=120)
    monkeypatch.setattr(admission, "DEFAULT_RUNTIME_PROFILE", profile)
    data = root / "data"
    data.mkdir()
    from shared.blackbox_v2.snapshot import SNAPSHOT_FILENAMES
    for name in SNAPSHOT_FILENAMES:
        (data / name).write_text("key,value\n1,1\n")
    algorithm_identity = {
        "code": source.code_hash, "metadata": source.manifest_hash,
        "schema": "10y02-full-oos-private-1", "machine": "test-runtime",
    }
    buffer = io.BytesIO()
    np.savez(buffer, header=np.array(json.dumps({
        "identity": algorithm_identity, "cutoff": "2026-09-10", "input_prefixes": {},
    })), **{f"{family}_{field}": np.array([1, 2, 3])
            for family in ("ten_y", "seven_y")
            for field in ("dates", "features", "preds", "probs")})
    binding = runner.state_binding_for_scheme(source, generation_id="generation-1",
                                             persistent=True, rebuild=True)
    work = root / "initialize"
    (work / "output").mkdir(parents=True)
    with runner._state_session(binding, work, source.blackbox_metadata, source.delivery_script,
                               data, "snapshot-1", profile) as session:
        session.output_path.write_bytes(buffer.getvalue())
        session.publish()
    source_path = binding.root / source.scheme_id / f"{source.scheme_version}.state"
    original = read_regular_bytes(source_path, _MAX_ENVELOPE_BYTES)
    request = BlackboxRequest(
        request_id="reviewed-request", predict_date="2026-09-11",
        feature_date="2026-09-10", target_date="2026-09-17",
        daily_cutoff_key="2026-09-10", weekly_cutoff_key="202636", monthly_cutoff_key="202608",
    )
    result = BlackboxResult(request.request_id, request.predict_date, request.feature_date,
                            request.target_date, 1)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        kwargs["output_path"].parent.mkdir(mode=0o700)
        kwargs["output_path"].write_text(json.dumps(asdict(result)))
        shutil.copyfile(kwargs["state_input"], kwargs["state_output"])
        return subprocess.CompletedProcess([], 0, "", "test algorithm boundary")

    monkeypatch.setattr(admission, "execute_blackbox_cli", execute)
    return {
        "args": dict(project_root=ROOT, source_config=source, candidate_config=candidate,
                     data_dir=data, data_snapshot_id="snapshot-1", generation_id="generation-1",
                     request=request, expected_result=result,
                     expected_source_envelope_sha256=hashlib.sha256(original).hexdigest(),
                     expected_algorithm_identity=algorithm_identity,
                     work_dir=root / "admit", approved_by="isolated-test"),
        "source": source_path, "original": original, "calls": calls, "execute": execute,
        "destination": binding.root / BASE / f"{candidate.scheme_version}.state",
    }


def test_admission_publishes_new_exact_envelope_without_modifying_source(prepared):
    receipt = admission.admit_reviewed_w3b_state(**prepared["args"])
    header, _ = _decode(prepared["destination"].read_bytes())
    assert header["identity"] == receipt["identity"]
    assert header["identity"]["scheme_id"] == BASE
    assert header["input"] == receipt["input"]
    assert receipt["algorithm_executions"] == 1
    assert receipt["prediction_written"] is receipt["registry_changed"] is False
    assert prepared["source"].read_bytes() == prepared["original"]
    assert len(receipt["state_conversion"]["unchanged_arrays_sha256"]) == 8
    assert len(prepared["calls"]) == 1


def test_admission_never_reinitializes_existing_candidate(prepared):
    admission.admit_reviewed_w3b_state(**prepared["args"])
    published = prepared["destination"].read_bytes()
    prepared["args"]["work_dir"] = prepared["args"]["work_dir"].with_name("second")
    with pytest.raises(ValueError, match="already exists"):
        admission.admit_reviewed_w3b_state(**prepared["args"])
    assert prepared["destination"].read_bytes() == published
    assert len(prepared["calls"]) == 1


@pytest.mark.parametrize("cutoff", ["2026-09-09", "2026-09-11"])
def test_identity_admission_rejects_different_cutoff_before_algorithm(prepared, cutoff):
    prepared["args"]["request"] = replace(
        prepared["args"]["request"], feature_date=cutoff, daily_cutoff_key=cutoff,
    )
    with pytest.raises(ValueError, match="without training"):
        admission.admit_reviewed_w3b_state(**prepared["args"])
    assert prepared["calls"] == []
    assert not prepared["destination"].exists()
    assert prepared["source"].read_bytes() == prepared["original"]


@pytest.mark.parametrize("failure", ["source_digest", "input", "algorithm_identity", "result", "config_drift", "termination"])
def test_admission_failure_never_publishes_or_modifies_source(prepared, monkeypatch, failure):
    if failure == "source_digest":
        prepared["args"]["expected_source_envelope_sha256"] = "0" * 64
    elif failure == "input":
        prepared["args"]["data_snapshot_id"] = "different-snapshot"
    elif failure == "algorithm_identity":
        prepared["args"]["expected_algorithm_identity"]["machine"] = "different"
    else:
        def fail(**kwargs):
            completed = prepared["execute"](**kwargs)
            if failure == "result":
                wrong = asdict(prepared["args"]["expected_result"]) | {"predicted_direction": -1}
                kwargs["output_path"].write_text(json.dumps(wrong))
            elif failure == "config_drift":
                config = prepared["args"]["candidate_config"].path / "config.yaml"
                config.write_text(config.read_text().replace("timeout_sec: 3600", "timeout_sec: 3500"))
            else:
                from scheduler.process_control import (
                    ProcessGroupTerminationError, ProcessGroupTerminationResult,
                )
                raise ProcessGroupTerminationError(
                    context="test termination uncertainty",
                    termination=ProcessGroupTerminationResult(
                        process_id=123, process_group_id=123, term_sent=True,
                        kill_sent=True, confirmed_gone=False, failure_reason="test",
                    ),
                )
            return completed
        monkeypatch.setattr(admission, "execute_blackbox_cli", fail)
    with pytest.raises((ValueError, runner.BlackboxExecutionError, runner.ProcessGroupTerminationError)):
        admission.admit_reviewed_w3b_state(**prepared["args"])
    assert not prepared["destination"].exists()
    assert prepared["source"].read_bytes() == prepared["original"]
    assert len(prepared["calls"]) <= 1


@pytest.fixture
def revision_prepared(prepared, monkeypatch):
    """复用真实状态会话，仅替换本测试范围以外的固定证据读取与交付证明。"""
    original_args = prepared["args"]
    source_header, payload = _decode(prepared["original"])
    reviewed = {
        "state_payload": payload,
        "request": original_args["request"], "result": original_args["expected_result"],
        "source_envelope_sha256": original_args["expected_source_envelope_sha256"],
        "source_input": json.loads(json.dumps(source_header["input"])),
        "execution_files": dict(source_header["input"]["files"]),
        "proof": {"schema_version": "isolated-reviewed-revision", "receipt_sha256": "a" * 64},
    }
    monkeypatch.setattr(admission, "load_reviewed_w3b_revision", lambda _cfg: reviewed)
    monkeypatch.setattr(admission, "verify_reviewed_w3b_delivery_change",
                        lambda **_kwargs: {"schema_version": "isolated-reviewed-delivery"})
    args = {key: original_args[key] for key in (
        "project_root", "source_config", "candidate_config", "data_dir",
        "data_snapshot_id", "generation_id", "work_dir", "approved_by",
    )}
    args["expected_revision_proof"] = dict(reviewed["proof"])
    return prepared | {"args": args, "reviewed": reviewed}


def test_revision_admission_publishes_exact_reviewed_payload_without_algorithm(revision_prepared):
    prepared = revision_prepared
    receipt = admission.admit_reviewed_w3b_revision(**prepared["args"])
    envelope = prepared["destination"].read_bytes()
    header, payload = _decode(envelope)
    assert payload == prepared["reviewed"]["state_payload"]
    assert header["identity"] == receipt["identity"]
    assert header["identity"]["scheme_id"] == BASE
    assert header["input"] == receipt["input"]
    assert header["payload_sha256"] == receipt["state"]["state_output_sha256"]
    assert hashlib.sha256(envelope).hexdigest() == receipt["state"]["state_envelope_sha256"]
    assert receipt["request"] == asdict(prepared["reviewed"]["request"])
    assert receipt["result"] == asdict(prepared["reviewed"]["result"])
    assert receipt["reused_standard_execution"] == prepared["args"]["expected_revision_proof"]
    assert receipt["algorithm_executions"] == 0
    assert receipt["prediction_written"] is receipt["registry_changed"] is False
    assert prepared["calls"] == []
    assert prepared["source"].read_bytes() == prepared["original"]
    assert not list(prepared["destination"].parent.glob(".state-*"))


@pytest.mark.parametrize("failure", [
    "execution_files", "actual_input_file", "source_input", "source_envelope", "runtime", "plan_proof",
])
def test_revision_admission_drift_never_publishes(revision_prepared, monkeypatch, failure):
    prepared = revision_prepared
    reviewed, args = prepared["reviewed"], prepared["args"]
    if failure == "execution_files":
        reviewed["execution_files"]["daily_output.csv"] = "0" * 64
    elif failure == "actual_input_file":
        (args["data_dir"] / "daily_output.csv").write_text("key,value\n1,2\n")
    elif failure == "source_input":
        reviewed["source_input"]["snapshot_id"] = "different-source-snapshot"
    elif failure == "source_envelope":
        reviewed["source_envelope_sha256"] = "0" * 64
    elif failure == "runtime":
        profile = admission.DEFAULT_RUNTIME_PROFILE
        monkeypatch.setattr(admission, "DEFAULT_RUNTIME_PROFILE",
                            replace(profile, name=profile.name + "-runtime-drift"))
    else:
        args["expected_revision_proof"]["receipt_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        admission.admit_reviewed_w3b_revision(**args)
    assert not prepared["destination"].exists()
    assert prepared["source"].read_bytes() == prepared["original"]
    assert prepared["calls"] == []


def test_revision_admission_refuses_existing_candidate(revision_prepared):
    prepared = revision_prepared
    admission.admit_reviewed_w3b_revision(**prepared["args"])
    published = prepared["destination"].read_bytes()
    prepared["args"]["work_dir"] = prepared["args"]["work_dir"].with_name("second-revision")
    with pytest.raises(ValueError, match="already exists"):
        admission.admit_reviewed_w3b_revision(**prepared["args"])
    assert prepared["destination"].read_bytes() == published
    assert prepared["source"].read_bytes() == prepared["original"]
    assert prepared["calls"] == []


def test_revision_admission_atomic_replace_failure_leaves_no_candidate(revision_prepared, monkeypatch):
    from scheduler import blackbox_state

    prepared = revision_prepared
    original_replace = blackbox_state.os.replace

    def fail_candidate_replace(source, destination, **kwargs):
        if destination == prepared["destination"].name:
            raise OSError("isolated atomic publication failure")
        return original_replace(source, destination, **kwargs)

    monkeypatch.setattr(blackbox_state.os, "replace", fail_candidate_replace)
    with pytest.raises(OSError, match="atomic publication failure"):
        admission.admit_reviewed_w3b_revision(**prepared["args"])
    assert not prepared["destination"].exists()
    assert not list(prepared["destination"].parent.glob(".state-*"))
    assert prepared["source"].read_bytes() == prepared["original"]
    assert prepared["calls"] == []
