"""W3A 状态准备边界；算法入口 mock，发布使用临时目录的真实 StateSession。"""

from dataclasses import asdict, replace
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest

from harness import w3a_revision_evidence as revision
from harness import w3a_state_prepare as prepare
from scheduler.blackbox_state import StateBinding, StateSession, _decode, _encode
from scheduler.discovery import load_scheme_config
from shared.blackbox_v2.contracts import BlackboxRequest, BlackboxResult


_ROOT = Path(__file__).resolve().parents[1]


def _payload(identity, value):
    arrays = {"dates": np.array(["2026-09-10"] * 653, dtype="U10"),
              "features": np.array([value * 64] * 3917, dtype="U64"),
              "preds": np.zeros((265, 653), dtype="int8"), "probs": np.full((265, 653), 0.5, dtype="float64")}
    digest = hashlib.sha256()
    for item in arrays.values():
        digest.update(json.dumps([item.dtype.str, item.shape]).encode())
        digest.update(item.tobytes())
    header = {"identity": identity, "authority": {"catalog": "fixture"}, "cutoff": "2026-09-10",
              "input_prefixes": {"weekly_df": value}, "payload_sha256": digest.hexdigest()}
    output = io.BytesIO()
    np.savez(output, header=np.array(json.dumps(header, sort_keys=True)), **arrays)
    return output.getvalue()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = load_scheme_config(_ROOT / "schemes" / (revision._BASE + "_bbv2") / "config.yaml")
    directory = tmp_path / "candidate-release" / "schemes" / revision._BASE
    shutil.copytree(_ROOT / "schemes" / revision._BASE, directory)
    code = source.delivery_script.read_bytes().replace(revision._BEFORE, revision._AFTER)
    (directory / "delivery" / (revision._BASE + ".py")).write_bytes(code)
    candidate = load_scheme_config(directory / "config.yaml")
    assert (source.scheme_version, candidate.scheme_version) == (prepare._SOURCE_VERSION, prepare._CANDIDATE_VERSION)
    state_root = tmp_path / "states"
    state_root.mkdir(mode=0o700)
    source_parent = state_root / source.scheme_id
    source_parent.mkdir(mode=0o700)
    data = tmp_path / "data"
    data.mkdir()
    for name in revision._FILES:
        (data / name).write_bytes((name + "-input").encode())
    files = {name: revision._sha((data / name).read_bytes()) for name in revision._FILES}
    new_input = {"schema": "data-bridge-v1", "snapshot_id": "new-snapshot", "generation_id": "new-generation", "files": files}
    old_input = {"schema": "data-bridge-v1", "snapshot_id": "old-snapshot", "generation_id": "old-generation",
                 "files": {name: "0" * 64 for name in revision._FILES}}
    controls = {"calls": 0, "context_calls": 0, "published": []}

    def identity(cfg):
        return {"script_sha256": cfg.code_hash, "metadata_sha256": cfg.manifest_hash,
                "runtime_sha256": "runtime" if not controls.get("runtime_drift") or cfg.scheme_id == source.scheme_id else "drift"}

    source_identity = {"schema": "full-oos-a2-private-4", "code": source.code_hash, "metadata": source.manifest_hash,
                       "numpy": "fixture-numeric-identity"}
    candidate_identity = source_identity | {"code": candidate.code_hash, "metadata": candidate.manifest_hash}
    old_payload = _payload(source_identity, "a")
    revised_payload = _payload(candidate_identity, "b")
    original = _encode({"identity": identity(source) | {"scheme_id": source.scheme_id, "scheme_version": source.scheme_version},
                        "input": old_input, "payload_sha256": revision._sha(old_payload)}, old_payload)
    source_path = source_parent / (source.scheme_version + ".state")
    candidate_path = state_root / candidate.scheme_id / (candidate.scheme_version + ".state")
    source_path.write_bytes(original)
    request = BlackboxRequest(revision._BASE + ":2026-09-11:2026-09-10:2026-09-17", "2026-09-11", "2026-09-10", "2026-09-17",
                             "2026-09-10", "202635", "202609")
    result = BlackboxResult(request.request_id, request.predict_date, request.feature_date, request.target_date, -1)
    reviewed = {"proof": {"reviewed": "synthetic-authenticated-fixture"}, "state_payload": revised_payload,
                "source_envelope_sha256": revision._sha(original), "source_input": old_input,
                "execution_files": files, "request": request, "result": result}
    monkeypatch.setattr(prepare, "_STATE_SHA", revision._sha(revised_payload))
    monkeypatch.setattr(revision, "load_reviewed_w3a_revision", lambda cfg: reviewed)

    def binding(cfg, *, generation_id, persistent, rebuild=False):
        assert persistent is True
        return StateBinding(cfg.scheme_id, cfg.scheme_version, generation_id, root=state_root, rebuild=rebuild)

    def session(binding, work, metadata, script, data_dir, snapshot, profile):
        cfg = source if binding.scheme_id == source.scheme_id else candidate
        return StateSession(binding, work_dir=work, identity=identity(cfg), input_identity=new_input)

    def verify(session, metadata, script, data_dir, snapshot, profile):
        assert profile.cpu_threads == 8 and profile.memory_limit_bytes == 4 * 1024**3 and profile.predict_timeout_sec == 120
        actual = {name: revision._sha((data_dir / name).read_bytes()) for name in revision._FILES}
        if actual != session.input_identity["files"]:
            raise ValueError("current input drift")
        cfg = source if metadata.scheme_id == source.scheme_id else candidate
        if revision._sha(script.read_bytes()) != cfg.code_hash or controls.get("identity_drift"):
            raise ValueError("exact identity drift")

    def warm(**kwargs):
        controls["calls"] += 1
        assert kwargs["script_path"] == source.delivery_script and kwargs["mode"] == "predict"
        assert kwargs["timeout_sec"] == 120
        kwargs["process_started"](101, 101)
        if controls.get("warm_error"):
            raise controls["warm_error"]
        payload = kwargs["state_input"].read_bytes()
        if controls.get("mutate_warm_state"):
            header, _, _ = revision._arrays(payload)
            payload = _payload(header["identity"], "c")
        kwargs["state_output"].write_bytes(payload)
        raw = asdict(result)
        if controls.get("wrong_result"):
            raw["predicted_direction"] = 1
        kwargs["output_path"].write_text(json.dumps(raw))
        if controls.get("input_drift"):
            (data / "weekly_output.csv").write_bytes(b"changed-input")
        event = {"trial_cutoff": "2026-09-10", "oos_rows": 653, "reused_rows": 653,
                 "trained_rows": 1 if controls.get("trained") else 0}
        return subprocess.CompletedProcess([], controls.get("returncode", 0), controls.get("stdout", ""), json.dumps(event))

    original_publish = StateSession.publish

    def publish(session):
        name = "source" if session.binding.scheme_id == source.scheme_id else "candidate"
        controls["published"].append(name)
        if controls.get("publish_failure") == name:
            raise OSError("injected " + name + " publish failure")
        audit = original_publish(session)
        if controls.get("uncertain_publish") == name:
            raise OSError("injected failure after atomic replacement")
        return audit

    def context():
        controls["context_calls"] += 1
        return {"scheduler": {"timer_fenced": not controls.get("unfenced")},
                "lifecycle_scheme_ids": prepare._W3A_IDS if not controls.get("missing_lock") else [],
                "current_release": "changed" if controls.get("context_drift") and controls["calls"] else "fixed",
                "source_exact": source.scheme_version, "candidate_exact": candidate.scheme_version,
                "ready_generation": "new-generation"}

    monkeypatch.setattr(prepare, "state_binding_for_scheme", binding)
    monkeypatch.setattr(prepare, "_state_session", session)
    monkeypatch.setattr(prepare, "_verify_state_execution", verify)
    monkeypatch.setattr(prepare, "execute_blackbox_cli", warm)
    monkeypatch.setattr(StateSession, "publish", publish)
    kwargs = dict(project_root=_ROOT, source_config=source, candidate_config=candidate, data_dir=data,
        data_snapshot_id="new-snapshot", generation_id="new-generation", expected_revision_proof=reviewed["proof"],
        work_dir=tmp_path / "work", approved_by="operator", verify_context=context)
    return kwargs, controls, reviewed, source_path, candidate_path, original


def test_reverse_conversion_changes_only_header_identity(setup):
    kwargs, _, reviewed, _, _, _ = setup
    converted, audit = prepare.rebind_reviewed_w3a_state_for_source(payload=reviewed["state_payload"],
        **{key: kwargs[key] for key in ("project_root", "source_config", "candidate_config")})
    before, _, members = revision._arrays(reviewed["state_payload"])
    after, _, new_members = revision._arrays(converted)
    assert after == before | {"identity": before["identity"] | {
        "code": kwargs["source_config"].code_hash, "metadata": kwargs["source_config"].manifest_hash}}
    assert members == new_members == audit["unchanged_npy_sha256"]
    assert audit["algorithm_executions"] == 0


def test_one_warm_then_source_and_candidate_publish_current_input(setup):
    kwargs, controls, reviewed, source, candidate, original = setup
    ready = prepare.prepare_reviewed_w3a_states(**kwargs)
    assert controls["calls"] == 1 and controls["published"] == ["source", "candidate"]
    assert ready["algorithm_executions"] == 1 and ready["training_rows"] == ready["candidate_algorithm_executions"] == 0
    assert ready["status"] == "ready" and ready["registry_changed"] is ready["prediction_written"] is False
    assert (kwargs["work_dir"] / "source-before.state").read_bytes() == original
    assert source.read_bytes() != original
    source_header, source_payload = _decode(source.read_bytes())
    candidate_header, candidate_payload = _decode(candidate.read_bytes())
    assert source_header["input"] == candidate_header["input"] == ready["input"]
    assert source_header["input"]["generation_id"] == "new-generation"
    assert source_header["identity"]["scheme_version"] == "3ee3dd2334fd"
    assert candidate_payload == reviewed["state_payload"]
    assert revision._arrays(source_payload)[2] == revision._arrays(candidate_payload)[2]
    assert (kwargs["work_dir"] / "process.json").is_file()
    assert (kwargs["work_dir"] / "warm/complete.json").is_file()
    with pytest.raises(ValueError, match="fresh private work"):
        prepare.prepare_reviewed_w3a_states(**kwargs)
    assert controls["calls"] == 1


@pytest.mark.parametrize("failure", ("warm_error", "wrong_result", "mutate_warm_state", "trained", "stdout", "returncode"))
def test_failed_warm_never_publishes(setup, failure):
    kwargs, controls, _, source, candidate, original = setup
    controls[failure] = RuntimeError("mock timeout/runner failure") if failure == "warm_error" else "noise" if failure == "stdout" else True
    with pytest.raises((ValueError, RuntimeError)):
        prepare.prepare_reviewed_w3a_states(**kwargs)
    assert controls["calls"] == 1 and controls["published"] == []
    assert source.read_bytes() == original and not candidate.exists()
    receipt = json.loads((kwargs["work_dir"] / "failure.json").read_bytes())
    assert receipt["algorithm_started"] is True and receipt["publish_attempted"] == []
    assert receipt["keep_daily_fenced"] is True


@pytest.mark.parametrize("failure", ("unfenced", "missing_lock", "runtime_drift", "identity_drift", "context_drift", "input_drift"))
def test_identity_input_or_control_drift_fails_closed(setup, failure):
    kwargs, controls, _, source, candidate, original = setup
    controls[failure] = True
    with pytest.raises(ValueError):
        prepare.prepare_reviewed_w3a_states(**kwargs)
    assert controls["published"] == [] and source.read_bytes() == original and not candidate.exists()
    assert controls["calls"] == (1 if failure in {"context_drift", "input_drift"} else 0)


@pytest.mark.parametrize("after_replace", (False, True))
def test_candidate_failure_keeps_new_source_state(setup, after_replace):
    kwargs, controls, _, source, candidate, original = setup
    controls["uncertain_publish" if after_replace else "publish_failure"] = "candidate"
    with pytest.raises(OSError):
        prepare.prepare_reviewed_w3a_states(**kwargs)
    assert controls["published"] == ["source", "candidate"] and source.read_bytes() != original
    assert candidate.exists() is after_replace
    receipt = json.loads((kwargs["work_dir"] / "failure.json").read_bytes())
    assert receipt["publish_attempted"] == ["source", "candidate"]
    assert receipt["automatic_restore_attempted"] is False and "source" in receipt["published_audits"]
    assert receipt["source_observed_envelope_sha256"] == revision._sha(source.read_bytes())
    assert (kwargs["work_dir"] / "source-before.state").read_bytes() == original


def test_source_publication_uncertainty_is_recorded_without_restore(setup):
    kwargs, controls, _, source, candidate, original = setup
    controls["uncertain_publish"] = "source"
    with pytest.raises(OSError):
        prepare.prepare_reviewed_w3a_states(**kwargs)
    assert source.read_bytes() != original and not candidate.exists()
    receipt = json.loads((kwargs["work_dir"] / "failure.json").read_bytes())
    assert receipt["publish_attempted"] == ["source"] and receipt["published_audits"] == {}
    assert receipt["source_observed_envelope_sha256"] == revision._sha(source.read_bytes())
    assert receipt["automatic_restore_attempted"] is False


def test_receipt_failure_does_not_hide_original_error(setup, monkeypatch):
    kwargs, controls, _, source, _, original = setup
    controls["warm_error"] = RuntimeError("original mock runner failure")
    write = prepare._receipt

    def injected(path, value):
        if path.name == "failure.json":
            raise OSError("disk full")
        return write(path, value)

    monkeypatch.setattr(prepare, "_receipt", injected)
    with pytest.raises(RuntimeError, match="original mock runner failure") as caught:
        prepare.prepare_reviewed_w3a_states(**kwargs)
    assert "Failure receipt write failed" in caught.value.__notes__[-1]
    assert source.read_bytes() == original


@pytest.mark.parametrize("field", ("scheme_version", "code_hash", "manifest_hash"))
def test_reverse_conversion_rejects_stale_source_identity(setup, field):
    kwargs, controls, reviewed, _, _, _ = setup
    values = {key: kwargs[key] for key in ("project_root", "source_config", "candidate_config")}
    values["source_config"] = replace(values["source_config"], **{field: "stale"})
    with pytest.raises(ValueError, match="fixed source/candidate"):
        prepare.rebind_reviewed_w3a_state_for_source(payload=reviewed["state_payload"], **values)
    assert controls["calls"] == 0


def test_failed_source_receipt_after_publish_keeps_updated_state(setup, monkeypatch):
    kwargs, controls, _, source, candidate, original = setup
    write = prepare._receipt

    def injected(path, value):
        if path.name == "source.published.json":
            raise OSError("published receipt disk full")
        return write(path, value)

    monkeypatch.setattr(prepare, "_receipt", injected)
    with pytest.raises(OSError, match="published receipt disk full"):
        prepare.prepare_reviewed_w3a_states(**kwargs)
    assert controls["published"] == ["source"] and source.read_bytes() != original and not candidate.exists()
    failure = json.loads((kwargs["work_dir"] / "failure.json").read_bytes())
    assert "source" in failure["published_audits"] and failure["automatic_restore_attempted"] is False


@pytest.mark.parametrize("target", ("project_root", "source_release", "candidate_release"))
def test_work_inside_any_immutable_release_is_rejected_before_creation(setup, target):
    kwargs, controls, _, source, candidate, original = setup
    if target == "project_root":
        # 独立 project 根证明检查不是仅靠 source/candidate 同路径间接覆盖。
        kwargs["project_root"] = kwargs["work_dir"].parent / "separate-project"
        release = kwargs["project_root"]
    else:
        cfg = kwargs["source_config" if target == "source_release" else "candidate_config"]
        release = cfg.path.parent.parent
    forbidden = release / "outputs" / ("forbidden-w3a-" + kwargs["work_dir"].parent.name)
    assert not forbidden.exists()
    kwargs["work_dir"] = forbidden
    with pytest.raises(ValueError, match="outside project/source/candidate release trees"):
        prepare.prepare_reviewed_w3a_states(**kwargs)
    assert not forbidden.exists() and controls["calls"] == 0 and controls["published"] == []
    assert controls["context_calls"] == 0
    assert source.read_bytes() == original and not candidate.exists()
