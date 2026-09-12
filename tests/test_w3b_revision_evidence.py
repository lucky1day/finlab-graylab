"""临时 W3B 证据验收；本机私有原件缺失时跳过，不运行算法或写生产。"""

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from harness import w3b_revision_evidence as evidence
from scheduler.discovery import load_scheme_config


_ROOT = Path(__file__).resolve().parents[1]
_LOCAL = _ROOT / "outputs/w3b-weekly-revision-20260912/ecs-evidence"
_FULL = "liwei_0616_10y01_full_oos_k3_div_k10"


@pytest.fixture
def copied_trial(tmp_path, monkeypatch):
    def prepare(scheme_id=_FULL):
        spec = evidence._SOURCES[scheme_id]
        required = [spec["execution"], spec["verification"], spec["driver"], spec["verifier"], "delivery"]
        if any(not (_LOCAL / relative).exists() for relative in required):
            pytest.skip("private reviewed ECS evidence is unavailable")
        root = tmp_path / "evidence"
        root.mkdir()
        for relative in required:
            source = _LOCAL / relative
            if source.is_dir():
                shutil.copytree(source, root / relative)
            else:
                shutil.copyfile(source, root / relative)
        candidate = tmp_path / "schemes" / scheme_id
        shutil.copytree(_ROOT / "schemes" / scheme_id, candidate)
        for suffix in ("py", "json"):
            shutil.copyfile(root / "delivery" / f"{scheme_id}.{suffix}",
                            candidate / "delivery" / f"{scheme_id}.{suffix}")
        monkeypatch.setattr(evidence, "_EVIDENCE_ROOT", root)
        return load_scheme_config(candidate / "config.yaml"), root, spec
    return prepare


@pytest.mark.parametrize("scheme_id", tuple(evidence._SOURCES))
def test_fixed_original_request_and_state_are_loaded_without_execution(copied_trial, scheme_id):
    cfg, root, spec = copied_trial(scheme_id)
    loaded = evidence.load_reviewed_w3b_revision(cfg)
    receipt = json.loads((root / spec["execution"] / "complete.json").read_bytes())
    assert hashlib.sha256(loaded["state_payload"]).hexdigest() == spec["state"]
    assert loaded["request"].request_id == receipt["request"]["request_id"]
    assert loaded["result"].request_id == loaded["request"].request_id
    assert loaded["algorithm_identity"]["code"] == cfg.code_hash
    assert loaded["execution_files"] == receipt["execution_input"]
    assert loaded["source_input"] == receipt["source_input"]
    assert loaded["source_envelope_sha256"] == receipt["source_envelope_sha256"]
    assert loaded["proof"]["publication_authorized"] is False
    assert loaded["proof"]["algorithm_executions"] == 0
    json.dumps(loaded["proof"])


def test_runtime_context_is_allowed_without_weakening_canonical_identity(copied_trial):
    cfg, _root, _spec = copied_trial()
    runtime_cfg = replace(cfg, environment_fingerprint="caller-runtime-fingerprint",
                          data_snapshot_id="caller-snapshot")
    loaded = evidence.load_reviewed_w3b_revision(runtime_cfg)
    assert loaded["proof"]["scheme_version"] == cfg.scheme_version
    for field in ("code_hash", "manifest_hash", "scheme_version", "config_hash"):
        with pytest.raises(ValueError, match="candidate config changed"):
            evidence.load_reviewed_w3b_revision(replace(runtime_cfg, **{field: "changed"}))


@pytest.mark.parametrize("target", [
    "code", "metadata", "started", "complete", "verification", "driver", "verifier",
    "state", "state_input", "request", "result", "independent_result", "stdout",
])
def test_tampered_delivery_or_provenance_is_rejected(copied_trial, target):
    cfg, root, spec = copied_trial()
    paths = {
        "code": cfg.delivery_script, "metadata": cfg.delivery_metadata,
        "started": root / spec["execution"] / "started.json",
        "complete": root / spec["execution"] / "complete.json",
        "verification": root / spec["verification"] / "verification.json",
        "driver": root / spec["driver"], "verifier": root / spec["verifier"],
        "state": root / spec["execution"] / "output/state-output.bin",
        "state_input": root / spec["execution"] / "state-input.bin",
        "request": root / spec["execution"] / "request.json",
        "result": root / spec["execution"] / "output/prediction.json",
        "independent_result": root / spec["verification"] / "prediction.json",
        "stdout": root / spec["execution"] / "stdout.txt",
    }
    path = paths[target]
    path.chmod(0o600)
    if target in {"request", "result", "independent_result"}:
        raw = json.loads(path.read_bytes())
        raw["request_id"] += ":renamed"
        path.write_text(json.dumps(raw))
    else:
        path.write_bytes(path.read_bytes() + b"\n ")
    with pytest.raises((ValueError, OSError)):
        evidence.load_reviewed_w3b_revision(cfg)


@pytest.mark.parametrize("kind", ["failure", "missing_complete", "file_symlink", "directory_symlink"])
def test_missing_success_or_unsafe_paths_are_rejected(copied_trial, kind):
    cfg, root, spec = copied_trial()
    execution = root / spec["execution"]
    if kind == "failure":
        (execution / "failure.json").write_text("{}")
    elif kind == "missing_complete":
        (execution / "complete.json").unlink()
    elif kind == "file_symlink":
        original = execution / "output/state-output.bin"
        moved = execution / "output/moved.bin"
        original.rename(moved)
        original.symlink_to(moved)
    else:
        moved = root / "moved-execution"
        execution.rename(moved)
        execution.symlink_to(moved, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        evidence.load_reviewed_w3b_revision(cfg)
