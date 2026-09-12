"""迁移期的只读身份转换证明；不执行算法，不生成新的回测成功记录。"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Mapping

from harness.native_successor_migration import (
    _REVIEWED_W3B_EVIDENCE,
    load_native_successor_waves,
)
from shared.blackbox_v2.contracts import load_metadata_bytes
from shared.blackbox_v2.intake import _script_violations


def verify_reviewed_w3b_delivery_change(**payloads) -> dict[str, object]:
    """核实 W3B 仅改身份或已验证的周频修订后缀补丁；不充当数值验收。"""
    source = payloads["source_script"]
    candidate = payloads["candidate_script"]
    if source == candidate:
        return verify_identity_only_delivery_change(**payloads)
    metadata = load_metadata_bytes(payloads["candidate_metadata"])
    approved = _REVIEWED_W3B_EVIDENCE.get(metadata.scheme_id)
    if approved is None or hashlib.sha256(source).hexdigest() != approved["code_sha256"]:
        raise ValueError("weekly revision requires the exact reviewed W3B source")
    before = b'            for name, proof in header["input_prefixes"].items():\n'
    after = before + (
        '                # 周频修订只影响完整特征；保留标签/日历保护并由下方特征指纹失效后缀。\n'
        '                if name == "weekly_df":\n'
        '                    continue\n'
    ).encode()
    if source.count(before) != 1 or candidate != source.replace(before, after):
        raise ValueError("W3B weekly revision contains unreviewed algorithm changes")
    # 复用身份/Metadata 白名单校验，再明确记录真正候选脚本摘要及算法改动类型。
    proof = verify_identity_only_delivery_change(**(payloads | {"candidate_script": source}))
    return proof | {
        "schema_version": "w3b-weekly-revision-delivery-conversion-v1",
        "candidate_code_sha256": hashlib.sha256(candidate).hexdigest(),
        "permitted_algorithm_change": "weekly_raw_prefix_to_feature_suffix_invalidation",
        "requires_independent_suffix_evidence": True,
    }


def verify_identity_only_delivery_change(
    *,
    project_root: Path,
    source_script: bytes,
    source_metadata: bytes,
    candidate_script: bytes,
    candidate_metadata: bytes,
) -> dict[str, object]:
    """证明批准清单内交付只改变方案 ID，保留真实的转换前后摘要。

    本证明只覆盖交付字节与 Metadata 的差异。调用方仍须独立核实原始
    数值等价证据、本机标准调用与状态身份；它不是可直接激活的 Gate。
    """
    for payload in (
        source_script, source_metadata, candidate_script, candidate_metadata,
    ):
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("identity conversion requires non-empty delivery bytes")
    source = load_metadata_bytes(source_metadata)
    candidate = load_metadata_bytes(candidate_metadata)
    waves = load_native_successor_waves(
        project_root / "deploy" / "native_to_blackbox_migration_v1.json"
    )
    matches = [
        target
        for wave in waves.values()
        for target in wave.targets
        if target.new_base_scheme_id == source.scheme_id
        and target.old_base_scheme_id == candidate.scheme_id
        and target.target_tenor == candidate.target_tenor
        and target.task_type == candidate.task_type
        and target.new_horizon == candidate.horizon
    ]
    if len(matches) != 1:
        raise ValueError("identity conversion is outside the approved migration mapping")
    if source_script != candidate_script:
        raise ValueError("identity-only conversion must preserve exact algorithm bytes")
    source_raw = json.loads(source_metadata)
    candidate_raw = json.loads(candidate_metadata)
    source_raw["scheme_id"] = candidate.scheme_id
    if source_raw != candidate_raw:
        raise ValueError("identity-only conversion may change only Metadata scheme_id")
    try:
        tree = ast.parse(candidate_script.decode("utf-8"))
    except (UnicodeError, SyntaxError) as exc:
        raise ValueError(f"invalid converted algorithm syntax: {exc}") from exc
    violations = _script_violations(tree)
    if violations:
        raise ValueError("unsafe converted algorithm: " + "; ".join(violations))

    return {
        "schema_version": "native-runtime-identity-conversion-v1",
        "source_scheme_id": source.scheme_id,
        "scheme_id": candidate.scheme_id,
        "target_tenor": candidate.target_tenor,
        "task_type": candidate.task_type,
        "execution_horizon": candidate.horizon,
        "fact_horizon": matches[0].old_horizon,
        "source_code_sha256": hashlib.sha256(source_script).hexdigest(),
        "candidate_code_sha256": hashlib.sha256(candidate_script).hexdigest(),
        "source_metadata_sha256": hashlib.sha256(source_metadata).hexdigest(),
        "candidate_metadata_sha256": hashlib.sha256(candidate_metadata).hexdigest(),
        "algorithm_executions": 0,
    }


def rebind_reviewed_w3b_state_metadata(
    *,
    source_state: bytes,
    source_metadata: bytes,
    candidate_metadata: bytes,
    expected_state_sha256: str,
    expected_algorithm_identity: Mapping[str, object],
    identity_conversion: Mapping[str, object],
) -> tuple[bytes, dict[str, object]]:
    """在内存中仅重绑定已核实 W3B 状态的 Metadata 摘要。

    所有数值数组的 NPY 原字节保持不变，输入前缀、cutoff、数值环境和
    payload 摘要保持不变。返回值不是平台状态发布许可；调用方须核实
    source receipt、本机环境并用正常算法读取校验，再走 StateSession。
    """
    import numpy as np

    approved = _REVIEWED_W3B_EVIDENCE.get(identity_conversion.get("scheme_id"))
    if (
        approved is None
        or identity_conversion.get("schema_version")
        != "native-runtime-identity-conversion-v1"
        or identity_conversion.get("source_code_sha256") != approved["code_sha256"]
        or identity_conversion.get("candidate_code_sha256") != approved["code_sha256"]
        or identity_conversion.get("source_metadata_sha256") != approved["metadata_sha256"]
    ):
        raise ValueError("state conversion requires the reviewed W3B identity proof")
    candidate_metadata_hash = identity_conversion.get("candidate_metadata_sha256")
    if (
        not isinstance(candidate_metadata_hash, str)
        or len(candidate_metadata_hash) != 64
        or any(char not in "0123456789abcdef" for char in candidate_metadata_hash)
    ):
        raise ValueError("candidate Metadata digest is invalid")
    source_description = load_metadata_bytes(source_metadata)
    candidate_description = load_metadata_bytes(candidate_metadata)
    source_document = json.loads(source_metadata)
    source_document["scheme_id"] = candidate_description.scheme_id
    if (
        hashlib.sha256(source_metadata).hexdigest() != approved["metadata_sha256"]
        or hashlib.sha256(candidate_metadata).hexdigest() != candidate_metadata_hash
        or source_description.scheme_id != identity_conversion.get("source_scheme_id")
        or candidate_description.scheme_id != identity_conversion.get("scheme_id")
        or source_document != json.loads(candidate_metadata)
    ):
        raise ValueError("state Metadata bytes do not match the identity-only conversion")
    if (
        not isinstance(source_state, bytes)
        or not 0 < len(source_state) <= 16 * 1024 * 1024
        or hashlib.sha256(source_state).hexdigest() != expected_state_sha256
    ):
        raise ValueError("reviewed state size or SHA-256 mismatch")
    expected_names = {"header.npy"} | {
        f"{family}_{field}.npy"
        for family in ("ten_y", "seven_y")
        for field in ("dates", "features", "preds", "probs")
    }
    with zipfile.ZipFile(io.BytesIO(source_state)) as source:
        entries = source.infolist()
        if (
            len(entries) != len(expected_names)
            or {item.filename for item in entries} != expected_names
            or sum(item.file_size for item in entries) > 16 * 1024 * 1024
            or source.getinfo("header.npy").file_size > 64 * 1024
        ):
            raise ValueError("reviewed W3B state archive layout is invalid")
        header_bytes = source.read("header.npy")
        header_stream = io.BytesIO(header_bytes)
        if np.lib.format.read_magic(header_stream) != (1, 0):
            raise ValueError("reviewed W3B state header must use NPY v1")
        shape, _fortran, dtype = np.lib.format.read_array_header_1_0(header_stream)
        if shape != () or dtype.kind != "U" or dtype.itemsize > 64 * 1024:
            raise ValueError("reviewed W3B state header must be a scalar Unicode array")
        header_array = np.load(io.BytesIO(header_bytes), allow_pickle=False)
        header = json.loads(str(header_array.item()))
        identity = header.get("identity")
        if (
            not isinstance(identity, dict)
            or identity != dict(expected_algorithm_identity)
            or identity.get("code") != approved["code_sha256"]
            or identity.get("metadata") != approved["metadata_sha256"]
        ):
            raise ValueError("reviewed state algorithm or environment identity mismatch")
        header["identity"] = identity | {"metadata": candidate_metadata_hash}
        encoded_header = io.BytesIO()
        np.save(encoded_header, np.array(json.dumps(header, sort_keys=True)), allow_pickle=False)
        result = io.BytesIO()
        array_hashes: dict[str, str] = {}
        with zipfile.ZipFile(result, "w") as destination:
            for entry in entries:
                payload = source.read(entry.filename)
                if entry.filename == "header.npy":
                    payload = encoded_header.getvalue()
                else:
                    array_hashes[entry.filename] = hashlib.sha256(payload).hexdigest()
                destination.writestr(entry, payload)
    converted = result.getvalue()
    if len(converted) > 16 * 1024 * 1024:
        raise ValueError("converted W3B state exceeds its size limit")
    return converted, {
        "schema_version": "native-runtime-state-identity-conversion-v1",
        "scheme_id": identity_conversion["scheme_id"],
        "source_state_sha256": expected_state_sha256,
        "converted_state_sha256": hashlib.sha256(converted).hexdigest(),
        "source_metadata_sha256": approved["metadata_sha256"],
        "candidate_metadata_sha256": candidate_metadata_hash,
        "unchanged_arrays_sha256": array_hashes,
        "cutoff": header.get("cutoff"),
        "algorithm_executions": 0,
    }
