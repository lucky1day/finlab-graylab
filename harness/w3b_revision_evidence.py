"""只读复验三份固定 W3B 周频修订证据；不执行算法或授予状态发布权限。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from harness.native_successor_migration import _REVIEWED_W3B_EVIDENCE
from scheduler.blackbox_state import MAX_STATE_BYTES, read_regular_bytes
from scheduler.discovery import SchemeConfig, load_scheme_config
from shared.blackbox_v2.contracts import (
    _require_result_echo, load_metadata_bytes, request_from_mapping, result_from_mapping,
)


_EVIDENCE_ROOT = Path("/opt/bond-factor-lab/incoming/w3b-weekly-revision-trial-20260912.sXv2QO")
_SOURCES = {
    "liwei_0616_10y01_full_oos_k3_div_k10": {
        "execution": "full-execution-separated", "verification": "independent-output-stderr",
        "started": "6e7099a95649c827e7ef9398f70e05a140684256265abc390195d9c957c42f15",
        "complete": "dbab28078df0c29f7c2978eb19345b940653de976a62190ed874fbb7ede173db",
        "state": "e98f5dddf2781ae3307608b6b7a34ce1245e702b518fdfa106d7b39ec50f9ca1",
        "verified": "88ff602851289a5986d79794cc1c6f0b13a9829c7497bfff7cc6952c4b78b993",
        "driver": "run_trial_output_checked.py",
        "driver_sha": "0b85e884b02a121452a3b4aad7db40a8974f801dd03905bb7e4b44a5f884693d",
        "verifier": "verify_suffix_stderr.py",
        "verifier_sha": "241ccf05fcc4c793261741dd5737b713f561c2c9d1588a0b5e9bc5e47c03b05b",
    },
    "liwei_0616_10y02_cons_say_k3_div_k5": {
        "execution": "k5-execution", "verification": "k5-independent-output-checked",
        "started": "306722f0bd47a0f0249499fed3e93a15ee832f741396602bc304d036a64af9f8",
        "complete": "ec916b44c2a31bf9d219f71ede33501a13dc19bb1b0b011742425938f54aa55f",
        "state": "45c22119c06d2d86a143ccf479fba7b2e832b45b2e56f0c3036eb77c6c6dfa6a",
        "verified": "2d8d735b7116d8ce262c99023198a6455756741c5f7bfde3de2e91e1fcd03553",
        "driver": "run_three_trial.py",
        "driver_sha": "245cf7a3384fbd8157a2c49c9971d74b039620e43ad44c3efd83157165de3cee",
        "verifier": "verify_three_suffix_from_request.py",
        "verifier_sha": "800d7cf35d0a31880f635888aea29b1f684cea5a602d217a2ee946555b7101a5",
    },
    "liwei_0616_10y01_cons_say_k3_div_k10": {
        "execution": "say-execution", "verification": "say-independent-output",
        "started": "be925e1ac400ab24a0ade815703df856eddf848bb3964bb99b8893082d4ae811",
        "complete": "9f4a839e10e2a08f0bf53ddc1bdd6e6b8ed6aba095dba21492ba8723c80832fb",
        "state": "0ae64f3d9902b41e870dcbcaa53279140ae0680b138507fe7f536344842c77ea",
        "verified": "94e031bd255b919cd8711c3a25f465e87d6e5d68c35e77ec7bb4aea8fa61b916",
        "driver": "run_three_trial.py",
        "driver_sha": "245cf7a3384fbd8157a2c49c9971d74b039620e43ad44c3efd83157165de3cee",
        "verifier": "verify_three_suffix_from_request.py",
        "verifier_sha": "800d7cf35d0a31880f635888aea29b1f684cea5a602d217a2ee946555b7101a5",
    },
}
_INPUT_FILES = {"api_wind_date.csv", "daily_output.csv", "weekly_output.csv",
                "monthly_output.csv", "factor_catalog.csv"}


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError("reviewed W3B revision: " + message)


def _read(relative: str, artifacts: dict[str, str], expected: str | None = None,
          limit: int = 1024 * 1024) -> bytes:
    path = _EVIDENCE_ROOT / relative
    payload = read_regular_bytes(path, limit)
    digest = _sha(payload)
    _require(expected is None or digest == expected, f"artifact SHA-256 mismatch: {relative}")
    artifacts[str(path)] = digest
    return payload


def _empty_stdout(relative: str, artifacts: dict[str, str]) -> None:
    """普通状态读取器拒绝空文件；这里仅接受稳定的零字节常规文件。"""
    path = _EVIDENCE_ROOT / relative
    _require(not any(parent.is_symlink() for parent in path.parents), "stdout traverses a symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                 and before.st_size == 0 and stream.read(1) == b"", "stdout must be empty")
        fingerprint = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
                                    item.st_ctime_ns, item.st_nlink)
        _require(fingerprint(before) == fingerprint(os.fstat(stream.fileno()))
                 == fingerprint(path.lstat()), "stdout changed while reading")
    artifacts[str(path)] = _sha(b"")


def load_reviewed_w3b_revision(candidate_config: SchemeConfig) -> dict[str, Any]:
    """返回已认证的原始 NPZ 与调用结果；本机环境及发布权限由调用方另验。"""
    spec = _SOURCES.get(candidate_config.scheme_id)
    _require(spec is not None, "candidate is outside the fixed three original IDs")
    approved = _REVIEWED_W3B_EVIDENCE[candidate_config.scheme_id]
    artifacts: dict[str, str] = {}
    execution, verification = spec["execution"], spec["verification"]
    for directory in (execution, verification):
        _require(not any(os.path.lexists(_EVIDENCE_ROOT / directory / name)
                         for name in ("failure.json", "failed.json")), "failure receipt exists")
    started = json.loads(_read(execution + "/started.json", artifacts, spec["started"]))
    complete = json.loads(_read(execution + "/complete.json", artifacts, spec["complete"]))
    verified = json.loads(_read(verification + "/verification.json", artifacts, spec["verified"]))
    _read(spec["driver"], artifacts, spec["driver_sha"])
    _read(spec["verifier"], artifacts, spec["verifier_sha"])
    _require(all(complete.get(key) == value for key, value in started.items())
             and started["schema"] == "w3b-weekly-revision-private-trial-v1"
             and complete["status"] == "standard_call_passed"
             and verified["status"] == "passed", "receipt chain or status mismatch")
    _require(all(receipt[flag] is False for receipt in (started, complete, verified)
                 for flag in ("production_state_published", "business_facts_written"))
             and verified["full_history_training_repeated"] is False,
             "private execution boundary mismatch")
    _require(started["source_code_sha256"] == approved["code_sha256"]
             == verified["reference_code_sha256"]
             and started["trial_code_sha256"] == verified["trial_code_sha256"],
             "reviewed source/trial identity mismatch")
    _require(complete["retained"] == {family: {"suffix_rows": 1, "unchanged_prefix_rows": 652}
                                     for family in ("ten_y", "seven_y")}
             and verified["checked"] == {family: {"all_preds_probs_equal": True, "configs": 265,
                                                  "independent_rows": 1}
                                          for family in ("ten_y", "seven_y")},
             "independent suffix/prefix proof mismatch")

    config_bytes = read_regular_bytes(candidate_config.path / "config.yaml", 1024 * 1024)
    cfg = load_scheme_config(candidate_config.path / "config.yaml")
    runtime_cfg = replace(
        cfg, environment_fingerprint=candidate_config.environment_fingerprint,
        data_snapshot_id=candidate_config.data_snapshot_id,
    )
    _require(runtime_cfg == candidate_config, "candidate config changed or caller identity is stale")
    _require(read_regular_bytes(cfg.path / "config.yaml", 1024 * 1024) == config_bytes,
             "candidate config changed during validation")
    _require(cfg.runtime_type == "blackbox_v2" and cfg.incremental_state is True
             and cfg.runtime_profile == "blackbox-v2-v1" and cfg.data_schema_version == "data-bridge-v1"
             and cfg.input_source == "data_bridge_current", "candidate runtime contract mismatch")
    script = read_regular_bytes(cfg.delivery_script, MAX_STATE_BYTES)
    metadata_bytes = read_regular_bytes(cfg.delivery_metadata, 1024 * 1024)
    metadata = load_metadata_bytes(metadata_bytes)
    _require(_sha(script) == cfg.code_hash == started["trial_code_sha256"]
             and _sha(metadata_bytes) == cfg.manifest_hash == started["trial_metadata_sha256"]
             and metadata == cfg.blackbox_metadata and metadata.scheme_id == cfg.scheme_id,
             "candidate delivery differs from the authenticated trial")
    before = b'            for name, proof in header["input_prefixes"].items():\n'
    after = before + ('                # 周频修订只影响完整特征；保留标签/日历保护并由下方特征指纹失效后缀。\n'
                      '                if name == "weekly_df":\n'
                      '                    continue\n').encode()
    _require(script.count(after) == 1 and _sha(script.replace(after, before)) == approved["code_sha256"],
             "trial is not the reviewed weekly-guard-only change")
    artifacts[str(cfg.delivery_script)] = _sha(script)
    artifacts[str(cfg.delivery_metadata)] = _sha(metadata_bytes)

    request_raw = json.loads(_read(execution + "/request.json", artifacts))
    result_raw = json.loads(_read(execution + "/output/prediction.json", artifacts))
    independent_raw = json.loads(_read(verification + "/prediction.json", artifacts))
    _require(request_raw == started["request"] and result_raw == complete["result"]
             == verified["result"] == independent_raw, "original Request/Result mismatch")
    request, result = request_from_mapping(request_raw), result_from_mapping(result_raw)
    _require_result_echo(result, request)
    _require(request.request_id == cfg.scheme_id + ":weekly-revision:2026-09-11",
             "original Request identity mismatch")
    for directory in (execution, verification):
        _empty_stdout(directory + "/stdout.txt", artifacts)
    _read(execution + "/state-input.bin", artifacts, started["converted_input_sha256"], MAX_STATE_BYTES)
    payload = _read(execution + "/output/state-output.bin", artifacts, spec["state"], MAX_STATE_BYTES)
    _require(_sha(payload) == complete["state_output_sha256"], "output state receipt mismatch")
    _require(set(started["execution_input"]) == set(started["source_input"]["files"]) == _INPUT_FILES
             and started["source_input"]["schema"] == "data-bridge-v1", "input provenance mismatch")

    # 固定成功凭据与原始 SHA 认证后只读标量头，不加载、解释或改写任何数值数组。
    import numpy as np

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.infolist()
        expected_names = {"header.npy"} | {f"{family}_{field}.npy"
            for family in ("ten_y", "seven_y") for field in ("dates", "features", "preds", "probs")}
        _require(len(entries) == len(expected_names) and {entry.filename for entry in entries} == expected_names
                 and sum(entry.file_size for entry in entries) <= MAX_STATE_BYTES
                 and archive.getinfo("header.npy").file_size <= 64 * 1024, "NPZ layout mismatch")
        header_bytes = archive.read("header.npy")
        header_stream = io.BytesIO(header_bytes)
        _require(np.lib.format.read_magic(header_stream) == (1, 0), "NPY header version mismatch")
        shape, _fortran, dtype = np.lib.format.read_array_header_1_0(header_stream)
        _require(shape == () and dtype.kind == "U" and dtype.itemsize <= 64 * 1024,
                 "NPY header is not bounded scalar Unicode")
        header = json.loads(np.load(io.BytesIO(header_bytes), allow_pickle=False).item())
    identity = header["identity"]
    _require(identity["code"] == cfg.code_hash and identity["metadata"] == cfg.manifest_hash
             and header["cutoff"] == request.feature_date, "algorithm state identity/cutoff mismatch")
    return {
        "state_payload": payload, "request": request, "result": result,
        "source_envelope_sha256": started["source_envelope_sha256"],
        "source_input": started["source_input"], "execution_files": started["execution_input"],
        "algorithm_identity": identity,
        "proof": {
            "schema_version": "reviewed-w3b-weekly-revision-source-v1",
            "scheme_id": cfg.scheme_id, "scheme_version": cfg.scheme_version,
            "artifacts_sha256": artifacts, "state_payload_sha256": _sha(payload),
            "source_envelope_sha256": started["source_envelope_sha256"],
            "source_code_sha256": approved["code_sha256"],
            "source_input": started["source_input"], "execution_files": started["execution_input"],
            "request": request_raw, "result": result_raw, "retained": complete["retained"],
            "independent_checks": verified["checked"], "algorithm_executions": 0,
            "publication_authorized": False, "request_reconstructed": False,
            "algorithm_identity_origin": "NPZ header authenticated by fixed successful receipts and raw SHA-256",
        },
    }
