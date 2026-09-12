"""固定 W3A Full 状态准备：私有 alias warm 后刷新 source，再接纳原 ID；不写业务库。"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import time
from typing import Callable, Mapping
import zipfile

from harness.blackbox_v2.gates import validate_canonical_blackbox_delivery
from harness import w3a_revision_evidence as revision
from scheduler.blackbox_state import MAX_STATE_BYTES, _MAX_ENVELOPE_BYTES, _decode, read_regular_bytes
from scheduler.blackbox_v2_runner import (
    DEFAULT_RUNTIME_PROFILE, _state_session, _verify_state_execution,
    execute_blackbox_cli, state_binding_for_scheme,
)
from scheduler.discovery import SchemeConfig
from shared.blackbox_v2.contracts import load_prediction_result
from shared.blackbox_v2.requests import write_request


_SOURCE_VERSION = "3ee3dd2334fd"
_CANDIDATE_VERSION = "be34f35f233b"
_STATE_SHA = "8a7c80b1b67caf08aefbca90304c02c9f59b514c3b1d04ed66d1920e5ef084e4"
_W3A_IDS = sorted((revision._BASE, revision._BASE + "_bbv2",
                  "liwei_0616_cons_sda_k3_div_k10", "liwei_0616_cons_sda_k3_div_k10_bbv2"))


def _write(path: Path, payload: bytes, mode: int = 0o600) -> str:
    """证据首次创建并同步落盘；失败不覆盖、不重试。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    return revision._sha(payload)


def _receipt(path: Path, value: Mapping[str, object]) -> str:
    return _write(path, (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode())


def rebind_reviewed_w3a_state_for_source(
    *, payload: bytes, project_root: Path, source_config: SchemeConfig,
    candidate_config: SchemeConfig,
) -> tuple[bytes, dict[str, object]]:
    """仅把认证的新四数组 payload 头反向绑定真实 alias，绝不改旧源状态或输入证明。"""
    import numpy as np

    revision._require(source_config.scheme_id == revision._BASE + "_bbv2"
                      and candidate_config.scheme_id == revision._BASE
                      and source_config.scheme_version == _SOURCE_VERSION
                      and candidate_config.scheme_version == _CANDIDATE_VERSION
                      and source_config.code_hash == revision._SOURCE_CODE_SHA
                      and source_config.manifest_hash == revision._SOURCE_METADATA_SHA
                      and candidate_config.code_hash == revision._CODE_SHA
                      and candidate_config.manifest_hash == revision._METADATA_SHA,
                      "state preparation requires the fixed source/candidate exact versions")
    conversion = revision.verify_reviewed_w3a_delivery_change(
        project_root=project_root,
        source_script=read_regular_bytes(source_config.delivery_script, MAX_STATE_BYTES),
        source_metadata=read_regular_bytes(source_config.delivery_metadata, 1024 * 1024),
        candidate_script=read_regular_bytes(candidate_config.delivery_script, MAX_STATE_BYTES),
        candidate_metadata=read_regular_bytes(candidate_config.delivery_metadata, 1024 * 1024),
    )
    revision._require(revision._sha(payload) == _STATE_SHA, "state preparation requires the authenticated revision payload")
    header, _, members = revision._arrays(payload)
    revision._require(header["identity"]["code"] == candidate_config.code_hash
                      and header["identity"]["metadata"] == candidate_config.manifest_hash
                      and header["cutoff"] == "2026-09-10", "revision payload identity/cutoff differs")
    rebound_header = header | {"identity": header["identity"] | {
        "code": source_config.code_hash, "metadata": source_config.manifest_hash}}
    encoded_header = io.BytesIO()
    np.save(encoded_header, np.array(json.dumps(rebound_header, sort_keys=True)), allow_pickle=False)
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as source, zipfile.ZipFile(result, "w") as target:
        for entry in source.infolist():
            target.writestr(entry, encoded_header.getvalue() if entry.filename == "header.npy" else source.read(entry.filename))
    converted = result.getvalue()
    checked, _, converted_members = revision._arrays(converted)
    revision._require(checked == rebound_header and converted_members == members,
                      "reverse identity conversion changed the four NPY members")
    return converted, {
        "schema_version": "w3a-reviewed-revision-source-state-rebinding-v1",
        "source_scheme_id": candidate_config.scheme_id, "source_scheme_version": candidate_config.scheme_version,
        "scheme_id": source_config.scheme_id, "scheme_version": source_config.scheme_version,
        "source_payload_sha256": _STATE_SHA, "converted_payload_sha256": revision._sha(converted),
        "before_algorithm_identity": header["identity"], "after_algorithm_identity": checked["identity"],
        "unchanged_npy_sha256": members, "input_prefixes": checked["input_prefixes"],
        "cutoff": checked["cutoff"], "delivery_conversion": conversion, "algorithm_executions": 0,
    }


def prepare_reviewed_w3a_states(
    *, project_root: Path, source_config: SchemeConfig, candidate_config: SchemeConfig,
    data_dir: Path, data_snapshot_id: str, generation_id: str,
    expected_revision_proof: Mapping[str, object], work_dir: Path, approved_by: str,
    verify_context: Callable[[], Mapping[str, object]],
) -> dict[str, object]:
    """在调用方持有 daily 围栏和四把 lifecycle 锁期间，只做一次 warm 和两次状态接纳。

    verify_context 必须真实复验围栏、四锁、current/ready 输入、环境和版本，
    返回稳定且可 JSON 化的证据，包含 scheduler.timer_fenced=True 及精确
    lifecycle_scheme_ids。此函数不取得 DB 锁、不持久化 Gate、不切换 Writer。
    失败保留已发布的新 source 状态，不自动还原不兼容当前输入的旧备份。
    进程终止不确定异常原样传播；调用方必须保留 runtime input view，不可自动清理。
    """
    revision._require(isinstance(approved_by, str) and bool(approved_by.strip()) and callable(verify_context),
                      "state preparation requires explicit operator and live context verifier")
    work = Path(work_dir)
    # canonical 固定位于 release/schemes/<id>；用显式所属树，不根据 cwd 推测。
    protected_roots = (Path(project_root), source_config.path.parent.parent,
                       candidate_config.path.parent.parent, Path(data_dir))
    revision._require(work.is_absolute() and work == work.resolve(strict=False) and not os.path.lexists(work)
                      and not any(work.is_relative_to(path.resolve()) for path in protected_roots),
                      "state preparation requires fresh private work outside project/source/candidate release trees and inputs")

    def context_snapshot():
        context = json.loads(json.dumps(verify_context(), sort_keys=True, allow_nan=False))
        revision._require(isinstance(context, dict) and context.get("scheduler", {}).get("timer_fenced") is True
                          and context.get("lifecycle_scheme_ids") == _W3A_IDS,
                          "daily fence and all four held lifecycle locks are required")
        return context

    context = context_snapshot()
    reviewed = revision.load_reviewed_w3a_revision(candidate_config)
    revision._require(reviewed["proof"] == expected_revision_proof, "revision proof changed after preflight")
    converted, conversion = rebind_reviewed_w3a_state_for_source(payload=reviewed["state_payload"],
        project_root=project_root, source_config=source_config, candidate_config=candidate_config)
    profile = replace(DEFAULT_RUNTIME_PROFILE, cpu_threads=8, memory_limit_bytes=4 * 1024**3, predict_timeout_sec=120)
    source_metadata = validate_canonical_blackbox_delivery(source_config)
    candidate_metadata = validate_canonical_blackbox_delivery(candidate_config)
    source_binding = state_binding_for_scheme(source_config, generation_id=generation_id, persistent=True)
    candidate_binding = state_binding_for_scheme(candidate_config, generation_id=generation_id, persistent=True, rebuild=True)
    revision._require(source_binding is not None and candidate_binding is not None
                      and source_binding.root is not None and source_binding.root == candidate_binding.root
                      and source_config.factor_input_mode == candidate_config.factor_input_mode == "algorithm_managed",
                      "state preparation requires local persistent five-file bindings")
    revision._require(not work.is_relative_to(source_binding.root.resolve()),
                      "state preparation requires fresh private work outside persistent state")
    source_path = source_binding.root / source_binding.scheme_id / f"{source_binding.scheme_version}.state"
    candidate_path = candidate_binding.root / candidate_binding.scheme_id / f"{candidate_binding.scheme_version}.state"
    work.mkdir(mode=0o700)
    process, published, attempted = {}, {}, []
    completed, begin = None, None
    try:
        source_work, candidate_work, warm = (work / name for name in ("source", "candidate", "warm"))
        for directory in (source_work, candidate_work, warm):
            directory.mkdir(mode=0o700)
        # 原 ID 在 alias 前，沿用现有 state 锁的稳定顺序；不再取得调用方已持有的 DB 锁。
        with (
            _state_session(candidate_binding, candidate_work, candidate_metadata, candidate_config.delivery_script,
                           data_dir, data_snapshot_id, profile) as candidate,
            _state_session(source_binding, source_work, source_metadata, source_config.delivery_script,
                           data_dir, data_snapshot_id, profile) as source,
        ):
            revision._require(not os.path.lexists(candidate_path), "candidate state already exists; inspect, never repeat preparation")
            original = read_regular_bytes(source_path, _MAX_ENVELOPE_BYTES)
            old_header, _ = _decode(original)
            revision._require(revision._sha(original) == reviewed["source_envelope_sha256"]
                              and old_header["identity"] == source.identity
                              and old_header["input"] == reviewed["source_input"], "reviewed source envelope/local identity differs")
            revision._require(source.input_identity == candidate.input_identity
                              and source.input_identity["files"] == reviewed["execution_files"]
                              and source.identity["runtime_sha256"] == candidate.identity["runtime_sha256"],
                              "source/candidate current input or numeric runtime differs")

            def verify_boundaries():
                revision._require(context_snapshot() == context, "fenced execution context changed")
                for session, metadata, config in ((source, source_metadata, source_config), (candidate, candidate_metadata, candidate_config)):
                    _verify_state_execution(session, metadata, config.delivery_script, data_dir, data_snapshot_id, profile)
                revision._require(revision.load_reviewed_w3a_revision(candidate_config)["proof"] == expected_revision_proof,
                                  "reviewed evidence changed during preparation")

            _write(work / "source-before.state", original, 0o400)
            state_input = warm / "state-input.bin"
            _write(state_input, converted, 0o400)
            request = reviewed["request"]
            request_path = write_request(request, warm / "request.json")
            request_sha = revision._sha(read_regular_bytes(request_path, 64 * 1024))
            started = {"schema_version": "w3a-state-prepare-v1", "approved_by": approved_by.strip(),
                "context": context, "revision_proof": reviewed["proof"], "state_conversion": conversion,
                "source_before_envelope_sha256": revision._sha(original), "source_before_input": old_header["input"],
                "source_identity": source.identity, "candidate_identity": candidate.identity, "input": source.input_identity,
                "request": asdict(request), "request_sha256": request_sha, "profile": asdict(profile),
                "prediction_written": False, "registry_changed": False}
            _receipt(work / "started.json", started)
            verify_boundaries()

            def process_started(pid, pgid):
                process.update(process_id=pid, process_group_id=pgid, started_at=datetime.now(timezone.utc).isoformat())
                _receipt(work / "process.json", process)

            output = warm / "output" / "prediction.json"
            warm_state = output.with_name("state-output.bin")
            output.parent.mkdir(mode=0o700)
            begin = time.monotonic()
            completed = execute_blackbox_cli(script_path=source_config.delivery_script, mode="predict",
                input_path=request_path, data_dir=data_dir, output_path=output, profile=profile, timeout_sec=120,
                state_input=state_input, state_output=warm_state, process_started=process_started)
            elapsed = time.monotonic() - begin
            _write(warm / "stdout.txt", completed.stdout.encode())
            _write(warm / "stderr.txt", completed.stderr.encode())
            revision._require(completed.returncode == 0 and not completed.stdout.strip(), "alias warm did not complete standard CLI")
            result = load_prediction_result(output, request)
            revision._require(result == reviewed["result"] and result.predicted_direction == -1, "alias warm standard Result differs")
            warm_payload = read_regular_bytes(warm_state, MAX_STATE_BYTES)
            warm_header, _, warm_members = revision._arrays(warm_payload)
            converted_header, _, converted_members = revision._arrays(converted)
            revision._require(warm_header == converted_header and warm_members == converted_members,
                              "alias warm changed state header or four NPY members")
            events = []
            for line in completed.stderr.splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and "trial_cutoff" in item:
                    events.append(item)
            revision._require(len(events) == 1 and all(events[0].get(key) == value for key, value in
                {"trial_cutoff": "2026-09-10", "oos_rows": 653, "reused_rows": 653, "trained_rows": 0}.items()),
                "alias warm must be one 653-row cache hit without training")
            revision._require(read_regular_bytes(source_path, _MAX_ENVELOPE_BYTES) == original
                              and read_regular_bytes(state_input, MAX_STATE_BYTES) == converted
                              and revision._sha(read_regular_bytes(request_path, 64 * 1024)) == request_sha,
                              "source/control bytes changed during alias warm")
            warm_receipt = {"status": "standard_call_passed", "source_identity": source.identity,
                "input": source.input_identity, "request": asdict(request), "result": asdict(result), "process": process,
                "state_input_sha256": revision._sha(converted), "state_output_sha256": revision._sha(warm_payload),
                "result_sha256": revision._sha(read_regular_bytes(output, profile.max_output_bytes)),
                "stdout_sha256": revision._sha(completed.stdout.encode()), "stderr_sha256": revision._sha(completed.stderr.encode()),
                "unchanged_npy_sha256": warm_members, "event": events[0], "seconds": elapsed,
                "algorithm_executions": 1, "training_rows": 0, "prediction_written": False}
            _receipt(warm / "complete.json", warm_receipt)
            for name, session, path, payload in (("source", source, source_path, warm_payload),
                                                    ("candidate", candidate, candidate_path, reviewed["state_payload"])):
                verify_boundaries()
                session.output_path.parent.mkdir(mode=0o700)
                _write(session.output_path, payload, 0o400)
                intent = {"identity": session.identity, "input": session.input_identity,
                    "payload_sha256": revision._sha(payload), "path": str(path),
                    "source_before_envelope_sha256": revision._sha(original)}
                _receipt(work / (name + ".publish-intent.json"), intent)
                attempted.append(name)
                audit = session.publish()
                published[name] = audit
                raw = read_regular_bytes(path, _MAX_ENVELOPE_BYTES)
                actual_header, actual_payload = _decode(raw)
                revision._require(revision._sha(raw) == audit["state_envelope_sha256"]
                                  and actual_header["identity"] == session.identity
                                  and actual_header["input"] == session.input_identity and actual_payload == payload,
                                  name + " publication read-back mismatch")
                _receipt(work / (name + ".published.json"), intent | {"state": audit})
            verify_boundaries()
            readiness = started | {"status": "ready", "source_state": published["source"],
                "candidate_state": published["candidate"], "alias_warm": warm_receipt,
                "algorithm_executions": 1, "candidate_algorithm_executions": 0, "training_rows": 0,
                "rollback_basis": "same current-input source exact state verified by one cache-hit standard call",
                "old_source_backup": str(work / "source-before.state")}
            _receipt(work / "complete.json", readiness)
            return readiness
    except BaseException as error:
        failure = {"status": "failed", "error_type": type(error).__name__, "error": str(error)[:16384],
            "process": process, "algorithm_started": bool(process), "publish_attempted": attempted,
            "published_audits": published, "automatic_restore_attempted": False,
            "seconds": time.monotonic() - begin if begin is not None else None,
            "stderr": completed.stderr[-16384:] if completed is not None else None,
            "keep_daily_fenced": True, "prediction_written": False, "registry_changed": False}
        for name, path in (("source", source_path), ("candidate", candidate_path)):
            try:
                if os.path.lexists(path):
                    failure[name + "_observed_envelope_sha256"] = revision._sha(read_regular_bytes(path, _MAX_ENVELOPE_BYTES))
            except BaseException:
                error.add_note("Could not read back " + name + " state; publication outcome requires inspection.")
        try:
            _receipt(work / "failure.json", failure)
        except BaseException:
            error.add_note("Failure receipt write failed; retain work/state and inspect, never blindly restore or retry.")
        raise
