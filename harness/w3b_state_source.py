"""临时只读加载三份已核实的 ECS W3B 状态来源；不授予接纳或发布权限。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any

from harness.native_successor_migration import _REVIEWED_W3B_EVIDENCE
from scheduler.blackbox_state import _decode, _MAX_ENVELOPE_BYTES, read_regular_bytes
from scheduler.discovery import SchemeConfig, load_scheme_config
from shared.blackbox_v2.contracts import (
    _require_result_echo, load_metadata_bytes, request_from_mapping, result_from_mapping,
)


_EVIDENCE_ROOT = Path("/opt/bond-factor-lab/incoming/w3b-all-candidate-20260911.7A2igD")
_INITIALIZER_SHA = "c88091a771e8b8bfec8b7f7c53f298eb69946aedc45aa48b091b6fb8ae7e41a7"
_FULL = "liwei_0616_10y01_full_oos_k3_div_k10_bbv2"
_SAY = "liwei_0616_10y01_cons_say_k3_div_k10_bbv2"
_SOURCES = {
    _FULL: {
        "scheme_version": "66b4fe52fef1",
        "config_hash": "82be206a6135a93879832efb76c66668d2d0b3128491e3a79fc49b0240d06a3a",
        "directory": "state-init-" + _FULL,
        "started": "e396dd1a895dd70c8f4114cc8eace3f29dabefc15436bf3734805cb4a33e081a",
        "complete": "0fff1304774e7fa6ef1553b494d7a9a64a2168e9944b3ef6bea7a71f8e976445",
    },
    "liwei_0616_10y02_cons_say_k3_div_k5_bbv2": {
        "scheme_version": "d18725b8f920",
        "config_hash": "6853c63502466b22f416c7c6b9a54b53d96edf5a61cae9012429839a6769fe86",
        "directory": "state-init-liwei_0616_10y02_cons_say_k3_div_k5_bbv2",
        "started": "b5b3918c88e9a0c9e46ad313c34e46d1a81b8ad0c80bff345adb7f7e637debe3",
        "complete": "b9e99d9fa882dde4e36a3d8f6d454e0e8b2988423b150569e9d667e04e3c1988",
    },
    _SAY: {
        "scheme_version": "0bc86751d50e",
        "config_hash": "b1a25705d176dc7137e3e82cbbc589d8eb9f4f131a2ea5367cd7c00b478d6d27",
        "directory": "simulate-adopted-say-state-20260911",
        "started": "30ca43f0ed72d08cb07d3daedbdc78210711210e7552e80e7c4785c678f81495",
        "complete": "32e192c392df95460b84359d4eef63d7c796c23137431864022e8de46dc3533a",
    },
}
_ADOPTION_STARTED_SHA = "f812511e1ba7e17cb1615dc0561674123182c9d8c321d9425f211275d4070af7"
_ADOPTION_COMPLETE_SHA = "879ee972a31b8146350dfffae2465527dae9e2afa50ff092d0ece81560599a77"
_ADOPTION_DRIVER_SHA = "46d8a09c13235a75bbf2df893397a21be6f10bb225d71b8a2b36d276490f3d5b"
_SAY_DRIVER_SHA = "734d78ed7291f730ad9714e0c14b94f1a1d2abd55d7902b11f8f43e8492cede4"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _plan_sha(plan: dict) -> str:
    return _sha(json.dumps(plan, sort_keys=True, default=str, separators=(",", ":")).encode())


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _bound(relative: str, expected: str, evidence: dict[str, str]) -> bytes:
    path = _EVIDENCE_ROOT / relative
    payload = read_regular_bytes(path, 1024 * 1024)
    _require(_sha(payload) == expected, "reviewed W3B source artifact SHA-256 mismatch")
    evidence[str(path)] = expected
    return payload


def _require_no_failure(directory: str) -> None:
    _require(not any(os.path.lexists(_EVIDENCE_ROOT / directory / name)
                     for name in ("failure.json", "failed.json")),
             "reviewed W3B source has a failure receipt")


def _initialization(spec: dict, evidence: dict[str, str]) -> tuple[dict, dict, dict, str, str]:
    _require_no_failure(spec["directory"])
    started = json.loads(_bound(spec["directory"] + "/started.json", spec["started"], evidence))
    complete = json.loads(_bound(spec["directory"] + "/complete.json", spec["complete"], evidence))
    _bound("initialize_w3b_ecs_state.py", _INITIALIZER_SHA, evidence)
    plan = started["plan"]
    cold, warm = complete["results"]["cold"], complete["results"]["warm"]
    _require(
        _plan_sha(plan) == started["plan_sha256"] == complete["plan_sha256"]
        and plan["driver_sha256"] == _INITIALIZER_SHA
        and complete["algorithm_executions"] == 2
        and complete["target_scheme_facts_unchanged"] is True
        and complete["prediction_written"] is False
        and cold["result"] == warm["result"]
        and warm["state_audit"]["state_input_sha256"] == cold["state"]["payload_sha256"],
        "reviewed W3B initialization chain mismatch",
    )
    header = {"identity": plan["expected_state_identity"], "input": plan["expected_state_input"],
              "payload_sha256": warm["state"]["payload_sha256"]}
    return plan["request"], warm["result"], header, warm["state"]["envelope_sha256"], plan["state_destination"]


def _say_source(spec: dict, evidence: dict[str, str]) -> tuple[dict, dict, dict, str, str]:
    _require_no_failure(spec["directory"])
    _require_no_failure("adopt-reviewed-say-state")
    started = json.loads(_bound(spec["directory"] + "/started.json", spec["started"], evidence))
    complete = json.loads(_bound(spec["directory"] + "/completed.json", spec["complete"], evidence))
    _bound("simulate_adopted_say_state.py", _SAY_DRIVER_SHA, evidence)
    _bound("adopt_reviewed_say_state.py", _ADOPTION_DRIVER_SHA, evidence)
    adopted_start = json.loads(_bound("adopt-reviewed-say-state/started.json", _ADOPTION_STARTED_SHA, evidence))
    adopted = json.loads(_bound("adopt-reviewed-say-state/completed.json", _ADOPTION_COMPLETE_SHA, evidence))
    _require(
        _plan_sha(adopted_start["plan"]) == adopted_start["plan_sha256"]
        == adopted["plan_sha256"] == started["adoption_plan_sha256"]
        and adopted["status"] == complete["status"] == "passed"
        and adopted["algorithm_executions"] == 0 and complete["algorithm_executions"] == 1
        and started["driver_sha256"] == _SAY_DRIVER_SHA
        and started["before_envelope_sha256"] == adopted["state"]["state_envelope_sha256"]
        and complete["state"]["state_input_sha256"] == adopted["state"]["state_output_sha256"]
        and complete["after_facts"] == started["before_facts"]
        and complete["prediction_written"] is False,
        "reviewed W3B SAY adoption/execution chain mismatch",
    )
    header = complete["envelope_header"]
    _require(
        header["identity"] == adopted["envelope_header"]["identity"]
        and header["input"]["generation_id"] == started["ready_generation_id"]
        and header["input"]["snapshot_id"] == started["ready_snapshot_id"]
        and header["payload_sha256"] == complete["state"]["state_output_sha256"],
        "reviewed W3B SAY current state identity mismatch",
    )
    # SAY 未保存七字段 Request 原件；只从同 snapshot 已批准的 FULL Request 取得截止键。
    full = _SOURCES[_FULL]
    full_start = json.loads(_bound(full["directory"] + "/started.json", full["started"], evidence))
    _require(
        full_start["plan"]["generation_id"] == header["input"]["generation_id"]
        and full_start["plan"]["snapshot_id"] == header["input"]["snapshot_id"],
        "SAY Request reconstruction requires the same approved FULL snapshot",
    )
    result = complete["result"]
    request = dict(full_start["plan"]["request"])
    _require(all(request[field] == result[field] for field in
                 ("predict_date", "feature_date", "target_date")), "SAY Request date mismatch")
    request["request_id"] = result["request_id"]
    return request, result, header, complete["state"]["state_envelope_sha256"], adopted_start["plan"]["destination"]


def load_reviewed_w3b_state_source(source_config: SchemeConfig) -> dict[str, Any]:
    """复验固定 ECS 原件并返回可信来源；当前环境与接纳授权由调用方另行验证。"""
    spec = _SOURCES.get(source_config.scheme_id)
    _require(spec is not None, "source is outside the reviewed W3B identity set")
    approved = _REVIEWED_W3B_EVIDENCE[source_config.scheme_id.removesuffix("_bbv2")]
    cfg = load_scheme_config(source_config.path / "config.yaml")
    expected = {"scheme_id": source_config.scheme_id, "scheme_version": spec["scheme_version"],
                "config_hash": spec["config_hash"], "code_hash": approved["code_sha256"],
                "manifest_hash": approved["metadata_sha256"], "runtime_type": "blackbox_v2",
                "incremental_state": True}
    _require(all(getattr(value, key) == wanted for value in (cfg, source_config)
                 for key, wanted in expected.items()), "reviewed W3B canonical exact identity mismatch")
    script = read_regular_bytes(cfg.delivery_script, 16 * 1024 * 1024)
    metadata_bytes = read_regular_bytes(cfg.delivery_metadata, 1024 * 1024)
    metadata = load_metadata_bytes(metadata_bytes)
    _require(_sha(script) == cfg.code_hash and _sha(metadata_bytes) == cfg.manifest_hash
             and metadata == cfg.blackbox_metadata, "reviewed W3B delivery bytes mismatch")
    evidence: dict[str, str] = {}
    if cfg.scheme_id == _SAY:
        request_raw, result_raw, expected_header, envelope_sha, destination = _say_source(spec, evidence)
    else:
        request_raw, result_raw, expected_header, envelope_sha, destination = _initialization(spec, evidence)
    request, result = request_from_mapping(request_raw), result_from_mapping(result_raw)
    _require_result_echo(result, request)
    _require(request.request_id == cfg.scheme_id + ":" + ":".join(
        (request.predict_date, request.feature_date, request.target_date)), "reviewed W3B Request identity mismatch")
    runtime_root = Path(os.environ.get("BFL_RUNTIME_ROOT", "/var/lib/bond-factor-lab/state"))
    state_path = runtime_root / "blackbox-state" / cfg.scheme_id / (cfg.scheme_version + ".state")
    _require(state_path.is_absolute() and str(state_path) == destination,
             "reviewed W3B state destination mismatch")
    envelope = read_regular_bytes(state_path, _MAX_ENVELOPE_BYTES)
    _require(_sha(envelope) == envelope_sha, "reviewed W3B canonical state SHA-256 mismatch")
    header, payload = _decode(envelope)
    _require(header == expected_header, "reviewed W3B platform identity/input mismatch")
    identity = header["identity"]
    _require(identity["scheme_id"] == cfg.scheme_id and identity["scheme_version"] == cfg.scheme_version
             and identity["script_sha256"] == cfg.code_hash and identity["manifest_sha256"] == cfg.manifest_hash,
             "reviewed W3B state/config identity mismatch")
    # payload 已由固定 SHA 认证的成功 receipt 和 envelope 校验；此时才读取可信算法头。
    import numpy as np

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        _require(archive.getinfo("header.npy").file_size <= 64 * 1024, "reviewed W3B algorithm header too large")
        algorithm_header = json.loads(np.load(io.BytesIO(archive.read("header.npy")), allow_pickle=False).item())
    algorithm_identity = algorithm_header["identity"]
    _require(algorithm_identity["code"] == cfg.code_hash and algorithm_identity["metadata"] == cfg.manifest_hash
             and algorithm_header["cutoff"] == request.feature_date, "reviewed W3B algorithm state identity mismatch")
    return {
        "request": request, "result": result, "source_envelope_sha256": envelope_sha,
        "source_payload_sha256": _sha(payload), "source_state_path": state_path,
        "expected_algorithm_identity": algorithm_identity, "identity": identity, "input": header["input"],
        "source_evidence": {
            "schema_version": "reviewed-w3b-state-source-v1", "scheme_id": cfg.scheme_id,
            "scheme_version": cfg.scheme_version, "artifacts_sha256": evidence,
            "state_path": str(state_path), "state_envelope_sha256": envelope_sha,
            "state_payload_sha256": _sha(payload), "request_reconstructed": cfg.scheme_id == _SAY,
            "request_origin": ("same-snapshot approved FULL cutoff keys; SAY Result echo"
                               if cfg.scheme_id == _SAY else "original started.plan.request"),
            "algorithm_identity_origin": "NPZ header authenticated by fixed successful receipt and envelope SHA-256",
            "algorithm_executions": 0, "publication_authorized": False,
        },
    }
