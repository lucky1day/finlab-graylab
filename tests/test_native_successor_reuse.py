"""临时迁移凭据复用合同；随 Native 迁移工具一起删除。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from harness import native_successor_migration as migration
from scheduler import blackbox_v2_runner as runner
from scheduler.discovery import load_scheme_config
from shared.blackbox_v2.contracts import REQUEST_FIELDS, RESULT_FIELDS


ROOT = Path(__file__).resolve().parents[1]


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json(path: Path, value: object) -> str:
    payload = json.dumps(value, sort_keys=True).encode()
    path.write_bytes(payload)
    return _sha(payload)


def _csv(fields, rows) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


@pytest.fixture
def reviewed(tmp_path, monkeypatch):
    wave = migration.load_native_successor_waves(ROOT / "deploy/native_to_blackbox_migration_v1.json")["W3A"]
    entries, approvals, identities = [], {}, {}
    for index, target in enumerate(wave.targets):
        cfg = load_scheme_config(ROOT / "schemes" / target.new_base_scheme_id / "config.yaml")
        evidence = tmp_path / target.new_base_scheme_id
        work = evidence / "work"
        data = evidence / "data"
        work.mkdir(parents=True)
        data.mkdir()
        files = {}
        for name in migration._DATABRIDGE_FILES:
            payload = (name + str(index)).encode()
            (data / name).write_bytes(payload)
            files[name] = _sha(payload)
        requests, results = [], []
        for offset in range(333):
            feature = (date(2025, 1, 1) + timedelta(days=offset)).isoformat()
            target_date = (date.fromisoformat(feature) + timedelta(days=7)).isoformat()
            request = {"request_id": f"{cfg.scheme_id}:{feature}:{feature}:{target_date}",
                       "predict_date": feature, "feature_date": feature, "target_date": target_date,
                       "daily_cutoff_key": feature, "weekly_cutoff_key": "202501", "monthly_cutoff_key": "202501"}
            requests.append(request)
            results.append({**{key: request[key] for key in RESULT_FIELDS[:-1]}, "predicted_direction": 1})
        request_bytes, result_bytes = _csv(REQUEST_FIELDS, requests), _csv(RESULT_FIELDS, results)
        (work / "formal.csv").write_bytes(request_bytes)
        (work / "native-compared333.csv").write_bytes(result_bytes)
        (work / "full333.result.csv").write_bytes(result_bytes)
        runtime = {"script_sha256": cfg.code_hash, "metadata_sha256": "m" * 64, "runtime_sha256": "r" * 64}
        frozen = {"schema": "data-bridge-v1", "snapshot_id": f"snapshot-{index}", "files": files}
        dependency_sha = _json(work / "dependency-report.json", {
            "passed": 333, "failed": 0, "request_sha256": _sha(request_bytes), "files": files,
            "requests": [{"passed": True, "checks": {"prefix": True}} for _ in requests],
        })
        phase_sha = _json(work / "native-cold-phase.identity.json", {
            "execution_identity": {"dependency_sha256": dependency_sha,
                                   "input": {"files": files, "runtime_identity": runtime}},
        })
        report_sha = _json(work / "native-comparison.report.json", {
            "matched": 333, "mismatches": 0, "native_phase_identity_sha256": phase_sha,
            "request_sha256": _sha(request_bytes), "candidate_result_sha256": _sha(result_bytes),
        })
        old = load_scheme_config(ROOT / "schemes" / target.old_base_scheme_id / "config.yaml")
        core = ROOT / "schemes" / target.old_base_scheme_id / "core/v31_common.py"
        identity = {"generation_id": f"generation-{index}", "snapshot_id": frozen["snapshot_id"],
                    "source_closure": {str(core): _sha(core.read_bytes())},
                    "input_identity": frozen, "runtime_identity": runtime}
        if index == 0:
            identity["formal_admission_sha256"] = _json(evidence / "formal-admission.json", {
                "scheme_version": cfg.scheme_version, "code_hash": cfg.code_hash,
                "config_hash": cfg.config_hash, "manifest_hash": cfg.manifest_hash,
                "environment_fingerprint": "e" * 64, "generation_id": identity["generation_id"],
                "data_snapshot_id": frozen["snapshot_id"], "requests_sha256": _sha(request_bytes),
                "result_sha256": _sha(result_bytes),
            })
        else:
            identity.update(requests_sha256=_sha(request_bytes), candidate_result_sha256=_sha(result_bytes))
        summary_sha = _json(evidence / "summary.json", {
            "count": 333, "mismatches": 0, "seconds": 10,
            "resources_sampled": {"peak_group_rss_bytes": 1024, "errors": []},
            "comparison_report_sha256": report_sha, "native_result_sha256": _sha(result_bytes),
        })
        approvals[old.scheme_id] = {"old_code_hash": old.code_hash, "scheme_version": cfg.scheme_version,
                                   "identity_sha256": _json(evidence / "identity.json", identity),
                                   "summary_sha256": summary_sha}
        identities[cfg.scheme_id] = runtime, frozen
        entries.append({"old_base_scheme_id": old.scheme_id, "new_base_scheme_id": cfg.scheme_id,
                        "target_tenor": "5Y", "evidence_dir": str(evidence), "data_dir": str(data)})
    bundle = tmp_path / "bundle.json"
    _json(bundle, {"schema_version": "native-successor-reviewed-input-v1", "wave": "W3A", "comparisons": entries})
    monkeypatch.setattr(migration, "_REVIEWED_W3A_EVIDENCE", approvals)
    monkeypatch.setattr(migration, "load_environment_fingerprint", lambda *a, **k: "e" * 64)

    def actual_identity(metadata, script, data, snapshot, profile):
        runtime, frozen = identities[metadata.scheme_id]
        return runtime, {**frozen, "files": {name: _sha((data / name).read_bytes()) for name in frozen["files"]}}

    def no_algorithm(*args, **kwargs):
        raise AssertionError("reviewed reuse must never launch a comparison")

    monkeypatch.setattr(runner, "_state_identities", actual_identity)
    monkeypatch.setattr(migration, "_execute_controlled_comparison", no_algorithm)
    return wave, bundle, entries, identities


def _build(reviewed):
    wave, bundle, _, _ = reviewed
    return migration.build_native_successor_equivalence_receipt(project_root=ROOT, wave=wave,
                                                               comparison_bundle_path=bundle)


def test_reviewed_wave_reuses_both_originals_without_algorithm_execution(reviewed):
    receipt = _build(reviewed)
    assert "generation_id" not in receipt
    assert {target["input_identity"]["generation_id"] for target in receipt["targets"]} == {"generation-0", "generation-1"}
    assert all(target["request_count"] == 333 and target["native_result_sha256"] == target["successor_result_sha256"]
               and target["native_runtime_profile"] == "blackbox-v2-v1"
               and target["native_runtime_environment_fingerprint"] == "e" * 64 for target in receipt["targets"])


@pytest.mark.parametrize("filename", ["identity.json", "summary.json", "formal-admission.json",
    "work/formal.csv", "work/native-compared333.csv", "work/full333.result.csv",
    "work/native-comparison.report.json", "work/native-cold-phase.identity.json", "work/dependency-report.json"])
def test_reviewed_wave_rejects_changed_original_bytes(reviewed, filename):
    path = Path(reviewed[2][0]["evidence_dir"]) / filename
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _build(reviewed)


def test_reviewed_wave_rejects_input_and_actual_runtime_change(reviewed):
    path = Path(reviewed[2][0]["data_dir"]) / "daily_output.csv"
    original = path.read_bytes()
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="runtime or frozen five-file input"):
        _build(reviewed)
    path.write_bytes(original)
    # 返回不同的实际环境，已批准的旧 evidence 不随之变化。
    name = reviewed[2][0]["new_base_scheme_id"]
    runtime, frozen = reviewed[3][name]
    reviewed[3][name] = {**runtime, "runtime_sha256": "changed"}, frozen
    with pytest.raises(ValueError, match="runtime or frozen five-file input"):
        _build(reviewed)


def test_reviewed_wave_rejects_symlink_even_when_bytes_match(reviewed):
    path = Path(reviewed[2][0]["evidence_dir"]) / "summary.json"
    saved = path.with_name("saved-summary.json")
    path.rename(saved)
    path.symlink_to(saved)
    with pytest.raises(ValueError, match="symlinks"):
        _build(reviewed)


def test_reviewed_wave_rejects_partial_family_and_external_result_fields(reviewed):
    wave, bundle, entries, _ = reviewed
    _json(bundle, {"schema_version": "native-successor-reviewed-input-v1", "wave": "W3A", "comparisons": entries[:1]})
    with pytest.raises(ValueError, match="target coverage"):
        _build(reviewed)
    entries[0]["native_result_sha256"] = "0" * 64
    _json(bundle, {"schema_version": "native-successor-reviewed-input-v1", "wave": "W3A", "comparisons": entries})
    with pytest.raises(ValueError, match="entry fields"):
        _build(reviewed)
