"""W3B 迁移期状态接纳：复用已验收状态，不重训、不写业务事实。"""

from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import os
from pathlib import Path
import time
from typing import Mapping

from harness.blackbox_v2.gates import validate_canonical_blackbox_delivery
from harness.runtime_upgrade_evidence import (
    rebind_reviewed_w3b_state_metadata,
    verify_identity_only_delivery_change,
)
from scheduler.blackbox_state import (
    MAX_STATE_BYTES, _MAX_ENVELOPE_BYTES, _decode, read_regular_bytes,
)
from scheduler.blackbox_v2_runner import (
    DEFAULT_RUNTIME_PROFILE, _state_session, _verify_state_execution,
    execute_blackbox_cli, state_binding_for_scheme,
)
from scheduler.discovery import SchemeConfig
from shared.blackbox_v2.contracts import (
    BlackboxRequest, BlackboxResult, load_prediction_result,
)
from shared.blackbox_v2.requests import write_request


def admit_reviewed_w3b_state(
    *, project_root: Path, source_config: SchemeConfig, candidate_config: SchemeConfig,
    data_dir: Path, data_snapshot_id: str, generation_id: str,
    request: BlackboxRequest, expected_result: BlackboxResult,
    expected_source_envelope_sha256: str,
    expected_algorithm_identity: Mapping[str, object],
    work_dir: Path, approved_by: str,
) -> dict[str, object]:
    """一次标准短调用后，通过既有 StateSession 发布原 ID 的新状态。

    调用方须先核验源初始化/执行回执与等价产物，从中提供预期结果、
    源封装摘要和数值环境身份。本函数再绑定实际 canonical、运行环境和
    同代五文件。返回的是状态接纳证据，不是 Gate 或 Registry 激活许可。
    work_dir 必须是调用方提供的全新私有目录；失败产物不自动清除，尤其
    进程终止不确定时，调用方必须保留输入视图并停止本批，禁止自动重试。
    """
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError("state admission requires an explicit operator")
    profile = replace(DEFAULT_RUNTIME_PROFILE, cpu_threads=8,
                      memory_limit_bytes=4 * 1024**3, predict_timeout_sec=120)
    source_metadata = validate_canonical_blackbox_delivery(source_config)
    candidate_metadata = validate_canonical_blackbox_delivery(candidate_config)
    conversion = verify_identity_only_delivery_change(
        project_root=project_root,
        source_script=source_config.delivery_script.read_bytes(),
        source_metadata=source_config.delivery_metadata.read_bytes(),
        candidate_script=candidate_config.delivery_script.read_bytes(),
        candidate_metadata=candidate_config.delivery_metadata.read_bytes(),
    )
    if (source_config.factor_input_mode != "algorithm_managed"
            or candidate_config.factor_input_mode != "algorithm_managed"):
        raise ValueError("W3B state admission requires five-file algorithm-managed input")
    source_binding = state_binding_for_scheme(
        source_config, generation_id=generation_id, persistent=True,
    )
    candidate_binding = state_binding_for_scheme(
        candidate_config, generation_id=generation_id, persistent=True, rebuild=True,
    )
    if (source_binding is None or candidate_binding is None
            or source_binding.root != candidate_binding.root):
        raise ValueError("W3B state admission requires local persistent exact bindings")
    # 原 ID 是临时 ID 的前缀，按稳定顺序取得两把既有方案锁。
    if candidate_binding.scheme_id + "_bbv2" != source_binding.scheme_id:
        raise ValueError("state admission is restricted to the original W3B identity")
    work_dir = Path(work_dir)
    if not work_dir.is_absolute() or work_dir != work_dir.resolve(strict=False):
        raise ValueError("state admission work directory must be absolute and symlink-free")
    work_dir.mkdir(mode=0o700)
    source_work = work_dir / "source"
    candidate_work = work_dir / "candidate"
    source_work.mkdir(mode=0o700)
    candidate_work.mkdir(mode=0o700)
    source_path = source_binding.root / source_binding.scheme_id / f"{source_binding.scheme_version}.state"
    candidate_path = candidate_binding.root / candidate_binding.scheme_id / f"{candidate_binding.scheme_version}.state"
    with (
        _state_session(candidate_binding, candidate_work, candidate_metadata,
                       candidate_config.delivery_script, data_dir,
                       data_snapshot_id, profile) as candidate,
        _state_session(source_binding, source_work, source_metadata,
                       source_config.delivery_script, data_dir,
                       data_snapshot_id, profile) as source,
    ):
        # StateSession 的通用 rebuild 可替换旧状态；迁移接纳只允许首次创建。
        if os.path.lexists(candidate_path):
            raise ValueError("candidate state already exists; inspect it instead of rebuilding")
        original = read_regular_bytes(source_path, _MAX_ENVELOPE_BYTES)
        if hashlib.sha256(original).hexdigest() != expected_source_envelope_sha256:
            raise ValueError("reviewed source state envelope SHA-256 mismatch")
        header, payload = _decode(original)
        if (header["identity"] != source.identity
                or header["input"] != source.input_identity
                or source.input_identity != candidate.input_identity):
            raise ValueError("source receipt state and local execution require identical input")
        converted, state_conversion = rebind_reviewed_w3b_state_metadata(
            source_state=payload,
            source_metadata=source_config.delivery_metadata.read_bytes(),
            candidate_metadata=candidate_config.delivery_metadata.read_bytes(),
            expected_state_sha256=header["payload_sha256"],
            expected_algorithm_identity=expected_algorithm_identity,
            identity_conversion=conversion,
        )
        if (request.feature_date != state_conversion["cutoff"]
                or request.daily_cutoff_key != state_conversion["cutoff"]):
            raise ValueError("identity admission must reuse the reviewed cutoff without training")
        state_input = candidate_work / "converted-state.bin"
        with state_input.open("xb") as target:
            target.write(converted)
        state_input.chmod(0o400)
        request_path = write_request(request, candidate_work / "request.json")
        output_path = candidate.output_path.parent / "prediction.json"
        started = time.monotonic()
        completed = execute_blackbox_cli(
            script_path=candidate_config.delivery_script, mode="predict",
            input_path=request_path, data_dir=data_dir, output_path=output_path,
            profile=profile, timeout_sec=120,
            state_input=state_input, state_output=candidate.output_path,
        )
        elapsed = time.monotonic() - started
        result = load_prediction_result(output_path, request)
        if result != expected_result:
            raise ValueError("original-ID standard Result differs from reviewed source result")
        for session, metadata, config in (
            (source, source_metadata, source_config),
            (candidate, candidate_metadata, candidate_config),
        ):
            _verify_state_execution(session, metadata, config.delivery_script,
                                    data_dir, data_snapshot_id, profile)
        if read_regular_bytes(source_path, _MAX_ENVELOPE_BYTES) != original:
            raise ValueError("source state changed during identity admission")
        if read_regular_bytes(state_input, MAX_STATE_BYTES) != converted:
            raise ValueError("converted state input changed during identity admission")
        audit = candidate.publish()
        published = read_regular_bytes(candidate_path, _MAX_ENVELOPE_BYTES)
        published_header, _ = _decode(published)
        if (hashlib.sha256(published).hexdigest() != audit["state_envelope_sha256"]
                or published_header["identity"] != candidate.identity
                or published_header["input"] != candidate.input_identity):
            raise ValueError("published original-ID state read-back mismatch")
        return {
            "schema_version": "w3b-original-id-state-admission-v1",
            "approved_by": approved_by.strip(),
            "scheme_id": candidate_binding.scheme_id,
            "scheme_version": candidate_binding.scheme_version,
            "source_scheme_version": source_binding.scheme_version,
            "source_envelope_sha256": expected_source_envelope_sha256,
            "identity_conversion": conversion, "state_conversion": state_conversion,
            "request": asdict(request), "result": asdict(result),
            "identity": candidate.identity, "input": candidate.input_identity,
            "state": audit, "seconds": elapsed,
            "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
            "algorithm_executions": 1, "prediction_written": False,
            "registry_changed": False,
        }
