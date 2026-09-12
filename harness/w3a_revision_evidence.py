"""只读复验固定 W3A Full 周频修订证据；不计算算法或授予发布权限。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from harness.runtime_upgrade_evidence import verify_identity_only_delivery_change
from scheduler.blackbox_state import MAX_STATE_BYTES, read_regular_bytes
from scheduler.discovery import SchemeConfig, load_scheme_config
from shared.blackbox_v2.contracts import (
    _require_result_echo, load_metadata_bytes, request_from_mapping, result_from_mapping,
)


_BASE = "liwei_0616_5y01_full_oos_k3_div_k10"
_EVIDENCE_ROOT = Path("/opt/bond-factor-lab/incoming")
_ANALYSIS = "w3a-full-weekly-analysis-20260912.wpLu6e"
_TRIAL = "w3a-full-weekly-private-20260912-938529"
_INDEPENDENT = "w3a-full-weekly-independent-20260912-938529"
_SOURCE_CODE_SHA = "ad9bdacf5063a427ecc8b70852e045f4822ba9af1b6d8fcd171cd2d779e95103"
_SOURCE_METADATA_SHA = "96d46ee4b2fb16f3b7da0c5485808f52f8f7ef14607721fbe00d26bc55d74982"
_CODE_SHA = "0fa034ca6fcd9ad358895ccd4c6617a43191cffaff1176823f8dd0ad7b8cbc39"
_METADATA_SHA = "ae7bfee67c9eae88c10b57cb32901c7ad206febfebde5435f5ba3c4346c6da69"
_ENVELOPE_SHA = "e042499088b2ee29c343d8cef78dc972115705589766314427c55e6cd0ace421"
_PAYLOAD_SHA = "6dce41ba24794b17025b78d2e59d0258aa8aac69bffffde8ea52f8ac300f51c3"
_PINNED = {
    _ANALYSIS + "/analyze.py": "bc4df52d7dec859f5b81c707c9fd91872b9e873dd0d0717f68a740cfa02bd945",
    _ANALYSIS + "/evidence.json": "d63eb3f43674466926034f11780f56f9caebe377c0f9894ec743c918d823d210",
    _ANALYSIS + "/run_private_trial.py": "a52648b4d2953ff7ed4251a2ca6d80d7fb6d9c444ca7c9bfc7ef27c9843b2e52",
    _ANALYSIS + "/independent_suffix_check.py": "657c99fb1923de01551ba913114c630666fa2deec559bfd05cf0d42919bf2328",
    _TRIAL + "/started.json": "4610a1aa29e91c4362cac1a6690123b7b92784d21b2f610e31f997d340619e55",
    _TRIAL + "/complete.json": "b7d597d17d467b0a7fb3b03912441d624eaa604f952e424cb1e9b66e9b1d0596",
    _INDEPENDENT + "/started.json": "8b5e4a4bbd6ef15e6beb1bf7d8bcd43d0f0422391f38d595d5333dbd5956a6f6",
    _INDEPENDENT + "/complete.json": "6c37299e795612bff2d8b76745fe9d2dc981cb38db6e4041403f7a3c8b58f813",
    _INDEPENDENT + "/worker-proof.json": "c53e4cbb3fbf335616e973eeb5f4041045796d453e21706a47c4d770ae6324b6",
    _INDEPENDENT + "/independent-values.json": "4ac9db9acdcbaff28f7cbe56754b36acc5c4a002f6eaa7250c17df4419f5d9ff",
}
_BEFORE = b'                if len(previous_frame) < proof["rows"] or _digest_frame(previous_frame.iloc[:proof["rows"]]) != proof["sha256"]:\n'
_AFTER = (b'                if (len(previous_frame) < proof["rows"]\n'
          b'                        or (name != "weekly_df"\n'
          b'                            and _digest_frame(previous_frame.iloc[:proof["rows"]]) != proof["sha256"])):\n')
_LAYOUT = {"dates": ((653,), "U10"), "features": ((3917,), "U64"),
           "preds": ((265, 653), "int8"), "probs": ((265, 653), "float64")}
_FILES = {"api_wind_date.csv", "daily_output.csv", "weekly_output.csv", "monthly_output.csv", "factor_catalog.csv"}


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError("reviewed W3A revision: " + message)


def verify_reviewed_w3a_delivery_change(**payloads) -> dict[str, object]:
    """只接受固定 Full 周频 guard 改动及 Metadata 原 ID 转换，不替代数值验收。"""
    source, candidate = payloads["source_script"], payloads["candidate_script"]
    _require(_sha(source) == _SOURCE_CODE_SHA and _sha(candidate) == _CODE_SHA
             and _sha(payloads["source_metadata"]) == _SOURCE_METADATA_SHA
             and _sha(payloads["candidate_metadata"]) == _METADATA_SHA,
             "delivery is not the exact reviewed Full source/candidate")
    _require(source.count(_BEFORE) == 1 and candidate == source.replace(_BEFORE, _AFTER),
             "unreviewed algorithm change or weekly length guard removed")
    _require(load_metadata_bytes(payloads["candidate_metadata"]).scheme_id == _BASE,
             "candidate is outside the fixed original Full ID")
    proof = verify_identity_only_delivery_change(**(payloads | {"candidate_script": source}))
    return proof | {
        "schema_version": "w3a-weekly-revision-delivery-conversion-v1",
        "candidate_code_sha256": _sha(candidate),
        "permitted_algorithm_change": "weekly_raw_prefix_to_feature_suffix_invalidation",
        "weekly_length_guard_preserved": True, "requires_independent_suffix_evidence": True,
    }


def _read(relative: str, artifacts: dict[str, str], expected: str | None = None,
          limit: int = 1024 * 1024) -> bytes:
    path = _EVIDENCE_ROOT / relative
    raw = read_regular_bytes(path, limit)
    digest = _sha(raw)
    _require(expected is None or digest == expected, "artifact SHA-256 mismatch: " + relative)
    artifacts[str(path)] = digest
    return raw


def _arrays(payload: bytes):
    """固定四数组格式先验 NPY 尺寸，再只读核验字节；不运行算法。"""
    import numpy as np

    members = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.infolist()
        _require(len(entries) == 5 and {entry.filename for entry in entries} == {"header.npy", *(name + ".npy" for name in _LAYOUT)}
                 and sum(entry.file_size for entry in entries) <= MAX_STATE_BYTES, "four-array NPZ layout mismatch")
        for entry in entries:
            _require(entry.compress_type == zipfile.ZIP_STORED and not entry.flag_bits & 1,
                     "state members must be bounded uncompressed NPY")
            raw = archive.read(entry.filename)
            stream = io.BytesIO(raw)
            _require(np.lib.format.read_magic(stream) == (1, 0), "NPY version mismatch")
            shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream)
            name = entry.filename.removesuffix(".npy")
            if name == "header":
                _require(shape == () and dtype.kind == "U" and dtype.itemsize <= 64 * 1024,
                         "NPY header must be bounded scalar Unicode")
            else:
                _require(shape == _LAYOUT[name][0] and dtype == np.dtype(_LAYOUT[name][1]), "NPY shape/dtype mismatch")
            _require(not fortran and not dtype.hasobject, "unsafe NPY array")
            members[name] = raw
    values = {name: np.load(io.BytesIO(raw), allow_pickle=False) for name, raw in members.items()}
    header = json.loads(values.pop("header").item())
    digest = hashlib.sha256()
    for name in _LAYOUT:
        value = values[name]
        digest.update(json.dumps([value.dtype.str, value.shape]).encode())
        digest.update(value.tobytes())
    _require(header["payload_sha256"] == digest.hexdigest()
             and header["identity"]["schema"] == "full-oos-a2-private-4", "state integrity/schema mismatch")
    return header, values, {name: _sha(members[name]) for name in _LAYOUT}


def load_reviewed_w3a_revision(candidate_config: SchemeConfig) -> dict[str, Any]:
    """返回认证私有 NPZ 与真实 Request/Result；环境与发布权限由调用方另验。"""
    _require(candidate_config.scheme_id == _BASE, "candidate is outside the fixed original Full ID")
    artifacts: dict[str, str] = {}
    for directory in (_TRIAL, _INDEPENDENT):
        _require(not any(os.path.lexists(_EVIDENCE_ROOT / directory / name)
                         for name in ("failure.json", "failed.json")), "failure receipt exists")
    originals = {name: _read(name, artifacts, digest) for name, digest in _PINNED.items()}
    analysis = json.loads(originals[_ANALYSIS + "/evidence.json"])
    started, complete = (json.loads(originals[_TRIAL + "/" + name + ".json"]) for name in ("started", "complete"))
    independent_started, verified, worker, values = (json.loads(originals[_INDEPENDENT + "/" + name + ".json"])
        for name in ("started", "complete", "worker-proof", "independent-values"))
    _require(all(complete.get(key) == value for key, value in started.items())
             and all(verified.get(key) == value for key, value in independent_started.items())
             and all(verified.get(key) == value for key, value in worker.items())
             and complete["status"] == "standard_call_passed" and verified["status"] == "independent_suffix_passed",
             "success receipt chain mismatch")
    _require(started["controller_sha256"] == _PINNED[_ANALYSIS + "/run_private_trial.py"]
             and verified["controller_sha256"] == _PINNED[_ANALYSIS + "/independent_suffix_check.py"]
             and verified["helper_sha256"] == started["controller_sha256"]
             and verified["trial_receipt_sha256"] == _PINNED[_TRIAL + "/complete.json"]
             and verified["worker_proof_sha256"] == _PINNED[_INDEPENDENT + "/worker-proof.json"]
             and verified["independent_values_sha256"] == _PINNED[_INDEPENDENT + "/independent-values.json"], "controller/proof chain mismatch")
    _require(all(receipt[flag] is False for receipt in (started, complete, verified, worker)
                 for flag in ("production_state_published", "business_facts_written"))
             and worker["full_history_training_repeated"] is False and worker["trial_values_used_for_expected"] is False
             and worker["training_dates"] == ["2026-09-10"] and worker["grid_configs"] == 265,
             "private independent execution boundary mismatch")
    _require(analysis["safe_prefix_rows"] == 652 and analysis["affected_suffix_dates"] == ["2026-09-10"]
             and analysis["old_feature_fingerprints_reproduced"] is True
             and analysis["training_calls"] == analysis["prediction_calls"] == 0
             and analysis["state_schema"] == "full-oos-a2-private-4", "dependency analysis mismatch")
    _require(all(receipt["source_envelope_sha256"] == _ENVELOPE_SHA
                 and receipt["source_payload_sha256"] == _PAYLOAD_SHA for receipt in (analysis, started, verified))
             and analysis["old_input"] == started["source_input"] == verified["source_input"]
             and started["execution_input"] == verified["execution_input"]
             and started["input_bindings"] == verified["input_bindings"]
             and started["source_runtime"] == verified["source_runtime"]
             and started["source_runtime"]["script_sha256"] == _SOURCE_CODE_SHA
             and started["execution_runtime"]["script_sha256"] == _CODE_SHA
             and started["source_runtime"]["runtime_sha256"] == started["execution_runtime"]["runtime_sha256"],
             "source/execution provenance mismatch")
    execution = started["execution_input"]
    _require(execution["schema"] == started["source_input"]["schema"] == "data-bridge-v1"
             and set(execution["files"]) == set(started["source_input"]["files"]) == _FILES
             and execution["files"] == started["input_bindings"]["new"]["files"]
             == started["input_bindings"]["current_files"], "fixed five-file input mismatch")

    config_raw = read_regular_bytes(candidate_config.path / "config.yaml", 1024 * 1024)
    cfg = load_scheme_config(candidate_config.path / "config.yaml")
    _require(replace(cfg, environment_fingerprint=candidate_config.environment_fingerprint,
                     data_snapshot_id=candidate_config.data_snapshot_id) == candidate_config,
             "candidate config changed or caller identity is stale")
    _require(read_regular_bytes(cfg.path / "config.yaml", 1024 * 1024) == config_raw,
             "candidate config changed during validation")
    _require(cfg.runtime_type == "blackbox_v2" and cfg.incremental_state is True
             and cfg.runtime_profile == "blackbox-v2-v1" and cfg.data_schema_version == "data-bridge-v1"
             and cfg.input_source == "data_bridge_current", "candidate runtime contract mismatch")
    script = read_regular_bytes(cfg.delivery_script, MAX_STATE_BYTES)
    metadata_raw = read_regular_bytes(cfg.delivery_metadata, 1024 * 1024)
    _require(_sha(script) == cfg.code_hash == _CODE_SHA == started["candidate_code_sha256"]
             and _sha(metadata_raw) == cfg.manifest_hash == _METADATA_SHA == started["candidate_metadata_sha256"]
             and load_metadata_bytes(metadata_raw) == cfg.blackbox_metadata, "candidate delivery differs from trial")
    _require(script.count(_AFTER) == 1 and _sha(script.replace(_AFTER, _BEFORE)) == _SOURCE_CODE_SHA,
             "candidate contains unreviewed algorithm changes")
    artifacts[str(cfg.delivery_script)] = _sha(script)
    artifacts[str(cfg.delivery_metadata)] = _sha(metadata_raw)

    request_raw = json.loads(_read(_TRIAL + "/request.json", artifacts, started["request_sha256"]))
    result_raw = json.loads(_read(_TRIAL + "/output/prediction.json", artifacts, complete["result_sha256"]))
    _require(request_raw == started["request"] == verified["request"]
             and result_raw == complete["result"] == verified["result"] == values["result"], "original Request/Result mismatch")
    request, result = request_from_mapping(request_raw), result_from_mapping(result_raw)
    _require_result_echo(result, request)
    _require(request.request_id == _BASE + ":2026-09-11:2026-09-10:2026-09-17"
             and (request.predict_date, request.feature_date, request.target_date) == ("2026-09-11", "2026-09-10", "2026-09-17"),
             "fixed Request dates/identity mismatch")
    before_payload = _read(_TRIAL + "/state-input.bin", artifacts, started["converted_payload_sha256"], MAX_STATE_BYTES)
    payload = _read(_TRIAL + "/output/state-output.bin", artifacts, complete["state_output_sha256"], MAX_STATE_BYTES)
    old_header, old, member_sha = _arrays(before_payload)
    header, arrays, _ = _arrays(payload)
    original_header = started["source_internal_header"]
    identity = original_header["identity"] | {"code": _CODE_SHA, "metadata": _METADATA_SHA}
    _require(member_sha == started["unchanged_npy_sha256"] and old_header == original_header | {"identity": identity}
             and original_header["identity"] == worker["source_internal_identity"]
             and worker["reference_code_sha256"] == original_header["identity"]["code"] == _SOURCE_CODE_SHA
             and original_header["identity"]["metadata"] == _SOURCE_METADATA_SHA,
             "original four-array/identity conversion mismatch")
    _require(header["identity"] == identity and header["authority"] == original_header["authority"]
             and header["cutoff"] == request.feature_date and header["input_prefixes"] == analysis["consumed_proofs"]["new"]
             and original_header["input_prefixes"] == analysis["consumed_proofs"]["old"], "state identity/consumed input mismatch")
    _require(arrays["dates"].tobytes() == old["dates"].tobytes()
             and arrays["features"][:3916].tobytes() == old["features"][:3916].tobytes()
             and _sha(old["features"].tobytes()) == analysis["old_fingerprint_sha256"]
             and _sha(arrays["features"].tobytes()) == analysis["new_fingerprint_sha256"], "date/feature prefix mismatch")
    import numpy as np

    for name in ("preds", "probs"):
        comparison = worker["comparisons"][name]
        expected = np.asarray(values[name], dtype=arrays[name].dtype)
        _require(expected.shape == (265,) and comparison["shape"] == [265]
                 and comparison["dtype"] == expected.dtype.str
                 and comparison["bytes_equal"] is comparison["numerically_equal"] is True
                 and expected.tobytes() == arrays[name][:, -1].tobytes()
                 and _sha(expected.tobytes()) == comparison["expected_sha256"] == comparison["trial_sha256"],
                 "independent 265-grid suffix mismatch: " + name)
        _require(old[name][:, :652].tobytes() == arrays[name][:, :652].tobytes()
                 and _sha(old[name][:, :652].tobytes()) == comparison["source_prefix_sha256"]
                 == complete["retained_prefix_sha256"][name], "unchanged 652-point prefix mismatch: " + name)
    return {
        "state_payload": payload, "request": request, "result": result, "algorithm_identity": identity,
        "source_envelope_sha256": _ENVELOPE_SHA, "source_input": started["source_input"], "execution_files": execution["files"],
        "proof": {"schema_version": "reviewed-w3a-weekly-revision-source-v1",
            "scheme_id": cfg.scheme_id, "scheme_version": cfg.scheme_version, "artifacts_sha256": artifacts,
            "state_payload_sha256": _sha(payload), "source_envelope_sha256": _ENVELOPE_SHA,
            "source_payload_sha256": _PAYLOAD_SHA, "source_code_sha256": _SOURCE_CODE_SHA,
            "source_input": started["source_input"], "execution_files": execution["files"],
            "request": request_raw, "result": result_raw, "retained": {"unchanged_prefix_rows": 652, "suffix_rows": 1},
            "independent_checks": worker["comparisons"], "algorithm_executions": 0,
            "publication_authorized": False, "request_reconstructed": False,
            "algorithm_identity_origin": "four-array NPZ authenticated by fixed successful receipts and raw SHA-256"},
    }
