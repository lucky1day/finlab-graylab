"""固定修订证据的只读合同测试；全部使用临时合成原件，不依赖 outputs。"""

import io
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import yaml

from harness import w3a_revision_evidence as evidence
from scheduler.discovery import load_scheme_config


_ROOT = Path(__file__).resolve().parents[1]


def _json(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _payload(header, arrays):
    import hashlib
    digest = hashlib.sha256()
    for name in evidence._LAYOUT:
        value = arrays[name]
        digest.update(json.dumps([value.dtype.str, value.shape]).encode())
        digest.update(value.tobytes())
    header = header | {"payload_sha256": digest.hexdigest()}
    stream = io.BytesIO()
    np.savez(stream, header=np.array(json.dumps(header, sort_keys=True)), **arrays)
    return stream.getvalue(), header


@pytest.fixture
def trial(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    cfg_dir = tmp_path / "schemes" / evidence._BASE
    delivery = cfg_dir / "delivery"
    delivery.mkdir(parents=True)
    source = (b"def guard():\n    if True:\n        if True:\n            if True:\n"
              + evidence._BEFORE + b"                    pass\n")
    candidate = source.replace(evidence._BEFORE, evidence._AFTER)
    original_metadata = (_ROOT / "schemes" / (evidence._BASE + "_bbv2") / "delivery" / (evidence._BASE + "_bbv2.json")).read_bytes()
    metadata = json.loads(original_metadata)
    metadata["scheme_id"] = evidence._BASE
    candidate_metadata = _json(metadata)
    for name, value in (("_SOURCE_CODE_SHA", source), ("_CODE_SHA", candidate),
                        ("_SOURCE_METADATA_SHA", original_metadata), ("_METADATA_SHA", candidate_metadata)):
        monkeypatch.setattr(evidence, name, evidence._sha(value))
    (delivery / (evidence._BASE + ".py")).write_bytes(candidate)
    (delivery / (evidence._BASE + ".json")).write_bytes(candidate_metadata)
    config = yaml.safe_load((_ROOT / "schemes" / evidence._BASE / "config.yaml").read_bytes())
    config.pop("native_attachments", None)
    config["delivery"] = {"script": "delivery/" + evidence._BASE + ".py", "metadata": "delivery/" + evidence._BASE + ".json"}
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump(config))
    arrays = {"dates": np.array(["2026-09-10"] * 653, dtype="U10"),
              "features": np.array(["a" * 64] * 3917, dtype="U64"),
              "preds": np.zeros((265, 653), dtype="int8"), "probs": np.full((265, 653), 0.5, dtype="float64")}
    identity = {"schema": "full-oos-a2-private-4", "code": evidence._SOURCE_CODE_SHA, "metadata": evidence._SOURCE_METADATA_SHA}
    old_proofs, new_proofs = {"weekly_df": "old-fixture"}, {"weekly_df": "new-fixture"}
    original_header = {"identity": identity, "authority": {"catalog": "fixture"}, "cutoff": "2026-09-10", "input_prefixes": old_proofs}
    _, original_header = _payload(original_header, arrays)
    converted_header = original_header | {"identity": identity | {"code": evidence._CODE_SHA, "metadata": evidence._METADATA_SHA}}
    converted, _ = _payload(converted_header, arrays)
    _, _, member_sha = evidence._arrays(converted)
    new_arrays = {key: value.copy() for key, value in arrays.items()}
    new_arrays["features"][-1] = "b" * 64
    new_arrays["preds"][:, -1] = -1
    new_arrays["probs"][:, -1] = 0.25
    state, _ = _payload(converted_header | {"input_prefixes": new_proofs}, new_arrays)
    request = {"request_id": evidence._BASE + ":2026-09-11:2026-09-10:2026-09-17", "predict_date": "2026-09-11",
               "feature_date": "2026-09-10", "target_date": "2026-09-17", "daily_cutoff_key": "2026-09-10",
               "weekly_cutoff_key": "202635", "monthly_cutoff_key": "202609"}
    result = {key: request[key] for key in ("request_id", "predict_date", "feature_date", "target_date")} | {"predicted_direction": -1}
    old_input = {"schema": "data-bridge-v1", "snapshot_id": "old", "generation_id": "old-generation",
                 "files": {name: "0" * 64 for name in evidence._FILES}}
    execution = {"schema": "data-bridge-v1", "snapshot_id": "new", "files": {name: "1" * 64 for name in evidence._FILES}}
    bindings = {"new": {"files": execution["files"]}, "current_files": execution["files"]}
    analysis = {"safe_prefix_rows": 652, "affected_suffix_dates": ["2026-09-10"], "old_feature_fingerprints_reproduced": True,
        "training_calls": 0, "prediction_calls": 0, "state_schema": "full-oos-a2-private-4", "source_envelope_sha256": evidence._ENVELOPE_SHA,
        "source_payload_sha256": evidence._PAYLOAD_SHA, "old_input": old_input, "consumed_proofs": {"old": old_proofs, "new": new_proofs},
        "old_fingerprint_sha256": evidence._sha(arrays["features"].tobytes()), "new_fingerprint_sha256": evidence._sha(new_arrays["features"].tobytes())}
    driver, verifier = b"# synthetic fixed controller\n", b"# synthetic fixed independent verifier\n"
    started = {"controller_sha256": evidence._sha(driver), "source_envelope_sha256": evidence._ENVELOPE_SHA,
        "source_payload_sha256": evidence._PAYLOAD_SHA, "source_input": old_input, "execution_input": execution, "input_bindings": bindings,
        "production_state_published": False, "business_facts_written": False, "candidate_code_sha256": evidence._CODE_SHA,
        "candidate_metadata_sha256": evidence._METADATA_SHA, "request": request, "request_sha256": evidence._sha(_json(request)),
        "converted_payload_sha256": evidence._sha(converted), "source_internal_header": original_header, "unchanged_npy_sha256": member_sha}
    started["source_runtime"] = {"script_sha256": evidence._SOURCE_CODE_SHA, "runtime_sha256": "runtime-fixture"}
    started["execution_runtime"] = {"script_sha256": evidence._CODE_SHA, "runtime_sha256": "runtime-fixture"}
    complete = started | {"status": "standard_call_passed", "result": result, "result_sha256": evidence._sha(_json(result)),
        "state_output_sha256": evidence._sha(state), "retained_prefix_sha256": {name: evidence._sha(arrays[name][:, :652].tobytes()) for name in ("preds", "probs")}}
    values = {name: new_arrays[name][:, -1].tolist() for name in ("preds", "probs")} | {"result": result}
    comparisons = {name: {"shape": [265], "dtype": new_arrays[name].dtype.str, "bytes_equal": True, "numerically_equal": True,
        "expected_sha256": evidence._sha(new_arrays[name][:, -1].tobytes()), "trial_sha256": evidence._sha(new_arrays[name][:, -1].tobytes()),
        "source_prefix_sha256": complete["retained_prefix_sha256"][name]} for name in ("preds", "probs")}
    worker = {"status": "independent_suffix_passed", "production_state_published": False, "business_facts_written": False,
        "full_history_training_repeated": False, "trial_values_used_for_expected": False, "training_dates": ["2026-09-10"], "grid_configs": 265,
        "result": result, "source_internal_identity": identity, "reference_code_sha256": evidence._SOURCE_CODE_SHA, "comparisons": comparisons}
    independent_started = {key: started[key] for key in ("source_envelope_sha256", "source_payload_sha256", "source_input", "execution_input", "input_bindings", "request", "source_runtime")}
    verified = independent_started | worker | {"controller_sha256": evidence._sha(verifier), "helper_sha256": evidence._sha(driver),
        "trial_receipt_sha256": evidence._sha(_json(complete)), "worker_proof_sha256": evidence._sha(_json(worker)),
        "independent_values_sha256": evidence._sha(_json(values))}
    content = {
        evidence._ANALYSIS + "/analyze.py": b"# fixture analysis\n", evidence._ANALYSIS + "/evidence.json": _json(analysis),
        evidence._ANALYSIS + "/run_private_trial.py": driver, evidence._ANALYSIS + "/independent_suffix_check.py": verifier,
        evidence._TRIAL + "/started.json": _json(started), evidence._TRIAL + "/complete.json": _json(complete),
        evidence._INDEPENDENT + "/started.json": _json(independent_started), evidence._INDEPENDENT + "/complete.json": _json(verified),
        evidence._INDEPENDENT + "/worker-proof.json": _json(worker), evidence._INDEPENDENT + "/independent-values.json": _json(values),
        evidence._TRIAL + "/request.json": _json(request), evidence._TRIAL + "/output/prediction.json": _json(result),
        evidence._TRIAL + "/state-input.bin": converted, evidence._TRIAL + "/output/state-output.bin": state,
    }
    for relative, raw in content.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    monkeypatch.setattr(evidence, "_PINNED", {name: evidence._sha(content[name]) for name in evidence._PINNED})
    monkeypatch.setattr(evidence, "_EVIDENCE_ROOT", root)
    return load_scheme_config(cfg_dir / "config.yaml"), root, {
        "project_root": _ROOT, "source_script": source, "candidate_script": candidate,
        "source_metadata": original_metadata, "candidate_metadata": candidate_metadata}


def test_reads_authentic_four_array_evidence_without_execution(trial):
    cfg, root, payloads = trial
    loaded = evidence.load_reviewed_w3a_revision(cfg)
    assert loaded["state_payload"] == (root / evidence._TRIAL / "output/state-output.bin").read_bytes()
    assert loaded["result"].predicted_direction == -1
    assert loaded["source_input"]["snapshot_id"] == "old"
    assert set(loaded["source_input"]["files"].values()) == {"0" * 64}
    assert set(loaded["execution_files"].values()) == {"1" * 64}
    assert loaded["proof"]["scheme_version"] == cfg.scheme_version
    assert loaded["proof"]["algorithm_executions"] == 0
    assert loaded["proof"]["publication_authorized"] is False
    assert loaded["algorithm_identity"]["schema"] == "full-oos-a2-private-4"
    json.dumps(loaded["proof"])
    assert evidence.verify_reviewed_w3a_delivery_change(**payloads)["weekly_length_guard_preserved"] is True


def test_runtime_context_does_not_allow_fake_exact_version(trial):
    cfg, _, _ = trial
    runtime = replace(cfg, environment_fingerprint="environment", data_snapshot_id="snapshot")
    assert evidence.load_reviewed_w3a_revision(runtime)["proof"]["scheme_version"] == cfg.scheme_version
    for key in ("scheme_version", "code_hash", "config_hash", "manifest_hash"):
        with pytest.raises(ValueError, match="candidate config changed"):
            evidence.load_reviewed_w3a_revision(replace(runtime, **{key: "forged"}))


@pytest.mark.parametrize("relative", tuple(evidence._PINNED) + (
    evidence._TRIAL + "/request.json", evidence._TRIAL + "/output/prediction.json",
    evidence._TRIAL + "/state-input.bin", evidence._TRIAL + "/output/state-output.bin"))
def test_any_authenticated_artifact_drift_fails_closed(trial, relative):
    cfg, root, _ = trial
    path = root / relative
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        evidence.load_reviewed_w3a_revision(cfg)


@pytest.mark.parametrize("kind", ("failure", "missing", "symlink", "directory_symlink"))
def test_failure_missing_and_unsafe_paths_are_rejected(trial, kind):
    cfg, root, _ = trial
    path = root / evidence._TRIAL / "complete.json"
    if kind == "failure":
        (path.parent / "failure.json").write_text("{}")
    elif kind == "missing":
        path.unlink()
    else:
        if kind == "directory_symlink":
            path = path.parent
        moved = path.with_name("moved")
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises((ValueError, OSError)):
        evidence.load_reviewed_w3a_revision(cfg)


@pytest.mark.parametrize("field", ("source_script", "candidate_script", "source_metadata", "candidate_metadata"))
def test_delivery_conversion_does_not_expand_reviewed_byte_scope(trial, field):
    _, _, payloads = trial
    with pytest.raises(ValueError, match="exact reviewed Full"):
        evidence.verify_reviewed_w3a_delivery_change(**(payloads | {field: payloads[field] + b"\n"}))


def test_even_repinned_receipt_cannot_claim_trial_cache_as_expected(trial, monkeypatch):
    cfg, root, _ = trial
    for relative in (evidence._INDEPENDENT + "/worker-proof.json", evidence._INDEPENDENT + "/complete.json"):
        path = root / relative
        value = json.loads(path.read_bytes())
        value["trial_values_used_for_expected"] = True
        path.write_bytes(_json(value))
        monkeypatch.setitem(evidence._PINNED, relative, evidence._sha(path.read_bytes()))
    path = root / evidence._INDEPENDENT / "complete.json"
    value = json.loads(path.read_bytes())
    value["worker_proof_sha256"] = evidence._PINNED[evidence._INDEPENDENT + "/worker-proof.json"]
    path.write_bytes(_json(value))
    monkeypatch.setitem(evidence._PINNED, evidence._INDEPENDENT + "/complete.json", evidence._sha(path.read_bytes()))
    with pytest.raises(ValueError, match="independent execution boundary"):
        evidence.load_reviewed_w3a_revision(cfg)


@pytest.mark.parametrize("mutation", ("extra_array", "wrong_dtype", "object_array", "wrong_shape"))
def test_four_array_parser_rejects_expanded_or_unsafe_npy(trial, mutation):
    _, root, _ = trial
    raw = (root / evidence._TRIAL / "output/state-output.bin").read_bytes()
    with np.load(io.BytesIO(raw), allow_pickle=False) as state:
        arrays = {name: state[name] for name in state.files}
    if mutation == "extra_array":
        arrays["ten_y_preds"] = arrays["preds"]
    elif mutation == "wrong_dtype":
        arrays["preds"] = arrays["preds"].astype("int32")
    elif mutation == "object_array":
        arrays["preds"] = arrays["preds"].astype(object)
    else:
        arrays["preds"] = arrays["preds"][:, :-1]
    stream = io.BytesIO()
    np.savez(stream, **arrays)
    with pytest.raises(ValueError, match="layout|shape/dtype"):
        evidence._arrays(stream.getvalue())


def test_rejects_other_scheme_and_changed_canonical_delivery(trial):
    cfg, _, _ = trial
    with pytest.raises(ValueError, match="fixed original Full ID"):
        evidence.load_reviewed_w3a_revision(replace(cfg, scheme_id=cfg.scheme_id + "_bbv2"))
    cfg.delivery_script.write_bytes(cfg.delivery_script.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="candidate config changed"):
        evidence.load_reviewed_w3a_revision(cfg)
