from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping

from scheduler.discovery import SchemeConfig, load_scheme_config
from scheduler.repository import NativeSuccessorTarget
from shared.blackbox_v2.contracts import REQUEST_FIELDS, request_from_mapping


MAPPING_SCHEMA_VERSION = "native-to-blackbox-migration-v1"
MAPPING_SHA256 = "370706acf55d436f4e420cd7be54d1c3254b2e3d5caa2c80d8beb4e6322c2045"
_WAVE_FIELDS = {
    "wave",
    "cadence",
    "ecs_mode",
    "targets",
    "atomic_family",
    "switch_mode",
}
_TARGET_FIELDS = {
    "old_base_scheme_id",
    "new_base_scheme_id",
    "task_type",
    "target_tenor",
    "target_rule",
    "old_horizon",
    "new_horizon",
}
_CONTROL_PLANE_FIELDS = {
    "schema_version",
    "wave",
    "deployment_target",
    "release",
    "databridge",
    "native_runtime",
    "scheduler",
    "dashboard",
    "host",
    "captured_at",
    "verified_by",
    "_capture_sha256",
}
_DATABRIDGE_FILES = {
    "daily_output.csv",
    "weekly_output.csv",
    "monthly_output.csv",
    "api_wind_date.csv",
    "factor_catalog.csv",
}
_CURRENT_LINKS = {
    "aliyun-gray": Path("/opt/bond-factor-lab/current"),
    "mac3-production": Path(
        "/Users/macstudio0/bond-factor-lab-production/current"
    ),
}
_SYSTEMD_UNITS = {
    cadence: (
        f"bond-factor-lab-prediction-{cadence}.timer",
        f"bond-factor-lab-prediction-{cadence}.service",
    )
    for cadence in ("daily", "weekly", "monthly")
}
_LAUNCHD_LABELS = {
    cadence: f"com.bond-factor-lab.{cadence}-predictions"
    for cadence in ("daily", "weekly", "monthly")
}
# 仅复用已完成独立审查和现场读回的 W3A 原件；不是可由操作者填成功摘要的接口。
_REVIEWED_W3A_EVIDENCE = {
    "liwei_0616_cons_sda_k3_div_k10": {
        "old_code_hash": "2ffdcfac682fe51ff0f196bfdce52be627652b89f2a061ced09ddda1f404908a",
        "scheme_version": "b5db363bbe17",
        "identity_sha256": "61af2028b5222be9b1aa4ce60798c003d68334fef4b257c8a0acab3c78986a31",
        "summary_sha256": "84cc36e8948527509481daa1416bd1cb163ca6bad9d29f191d6e465083c2f7d2",
    },
    "liwei_0616_5y01_full_oos_k3_div_k10": {
        "old_code_hash": "fe12a8559756be63092764223e0f75ec37c488bea746a41dc4eaf84baaa4ef0c",
        "scheme_version": "3ee3dd2334fd",
        "identity_sha256": "208b7c0540137cab5f2e75c546dd1d14e3bc2f80a27680a169c941be6400d45a",
        "summary_sha256": "e45e5f85d7afa3602276e480e5f532995b68c049e8f03d4382c8ac9055893015",
    },
}
# 本次 W3B 已批准 ECS 原件的封闭集合；不接受操作者提供的成功摘要或新结果。
_W3B_SOURCE = Path("/opt/bond-factor-lab/incoming/w3b-state-20260909.HFFFwd/native-migration-w3b-reference-draft/execution")
_W3B_STAGED = Path("/opt/bond-factor-lab/incoming/w3b-staged-reference-20260910.hejFGE")
_W3B_BINDING_SHA256 = "bdc68765192aaa9fb73a4bcc52f4cd9d1bf73b14ece9338c4d0525830333b5ce"
_REVIEWED_W3B_EVIDENCE = {
    "liwei_0616_10y01_cons_say_k3_div_k10": {
        "family": "say",
        "old_code_hash": "32c64f6a8d17d7920fa5e9b7eb6d152e52c4f5591d2962bf8e93f137ccc889b0",
        "code_sha256": "93798b4183fab8e02f974c72a4cf7602df9b410227fbc909608c74fe6ba1e9ce",
        "metadata_sha256": "ceba9f3a222decadafcdc7375aba94f4e160fe84c20ffd4ae9028fa42bf4fe49",
        "report_sha256": "a96b3191868c0d2141d15be6a6cb47a8c23bb4e44c9d1d1b675f39899e84cb4e",
        "execution_dir": "/opt/bond-factor-lab/incoming/w3b-weeklyfix-20260909.fZmImh/full-comparison-execution",
    },
    "liwei_0616_10y01_full_oos_k3_div_k10": {
        "family": "full",
        "old_code_hash": "67099dd38b64a551a8616f8db95d4922bad35f5edb0b722a04581bbde538b168",
        "code_sha256": "4636722b3502f9a94942843a99b01170c193fa89784944237825e8e67261d0ad",
        "metadata_sha256": "c5440c0eda7a02ce2456539eec80f26750f5bce8963eb89e3db9fc99bca7a69a",
        "report_sha256": "6018e0e6240b996a1a201e742fa063b0dc191786d63e4e29f0393a03a50339fe",
        "execution_dir": "/opt/bond-factor-lab/incoming/w3b-model-reuse-full-20260911.KOoIYB/full-execution",
    },
    "liwei_0616_10y02_cons_say_k3_div_k5": {
        "family": "k5",
        "old_code_hash": "2b84c06d80a051167da27802cc1cd36d3bbe817ed990318917632591b0dd970a",
        "code_sha256": "8e0df532e3c34fd778ef2ee38d66282ae219004edb9abe1b6d8925a7f28754d6",
        "metadata_sha256": "f9a04607e978c386312bf351c5443144e170fb2b0abb31d288239e1286094b6f",
        "report_sha256": "3f63aa9afc091845534aab49815d07130b955ec8e5f047ab197ee2851121a621",
        "execution_dir": "/opt/bond-factor-lab/incoming/w3b-k5-model-reuse-20260911.72TZ9d/k5-execution",
    },
}
_STANDARD_RESULT_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
)


@dataclass(frozen=True)
class NativeSuccessorWave:
    """静态迁移映射中的一个原子执行范围。"""

    wave: str
    cadence: str
    ecs_mode: str
    targets: tuple[NativeSuccessorTarget, ...]
    atomic_family: bool = False
    switch_mode: str = "wave"


def load_native_successor_waves(path: Path) -> Mapping[str, NativeSuccessorWave]:
    """严格读取临时 Native successor 迁移映射。"""
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != MAPPING_SHA256:
        raise ValueError("migration mapping differs from the locked 26-to-30 plan")
    raw = json.loads(source.decode("utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "waves"}:
        raise ValueError("migration mapping top-level fields are invalid")
    if raw.get("schema_version") != MAPPING_SCHEMA_VERSION:
        raise ValueError("migration mapping schema_version is unsupported")
    raw_waves = raw.get("waves")
    if not isinstance(raw_waves, list) or not raw_waves:
        raise ValueError("migration mapping waves must be a non-empty list")
    waves: dict[str, NativeSuccessorWave] = {}
    all_targets: list[NativeSuccessorTarget] = []
    for raw_wave in raw_waves:
        if not isinstance(raw_wave, dict) or not set(raw_wave).issubset(_WAVE_FIELDS):
            raise ValueError("migration wave contains unknown fields")
        if not {"wave", "cadence", "ecs_mode", "targets"}.issubset(raw_wave):
            raise ValueError("migration wave is missing required fields")
        wave_id = _nonempty(raw_wave["wave"], "wave")
        cadence = _choice(raw_wave["cadence"], "cadence", {"daily", "weekly", "monthly"})
        ecs_mode = _choice(
            raw_wave["ecs_mode"],
            "ecs_mode",
            {"active", "not_deployed_mac3_only"},
        )
        raw_targets = raw_wave["targets"]
        if not isinstance(raw_targets, list) or not raw_targets:
            raise ValueError(f"migration wave has no targets: {wave_id}")
        targets = tuple(_parse_target(value) for value in raw_targets)
        switch_mode = raw_wave.get("switch_mode", "wave")
        if switch_mode not in {"wave", "per_scheme"}:
            raise ValueError(f"invalid switch_mode for {wave_id}")
        atomic_family = raw_wave.get("atomic_family", False)
        if not isinstance(atomic_family, bool):
            raise ValueError(f"atomic_family must be boolean for {wave_id}")
        if wave_id in waves:
            raise ValueError(f"duplicate migration wave: {wave_id}")
        waves[wave_id] = NativeSuccessorWave(
            wave=wave_id,
            cadence=cadence,
            ecs_mode=ecs_mode,
            targets=targets,
            atomic_family=atomic_family,
            switch_mode=str(switch_mode),
        )
        all_targets.extend(targets)
    old_ids = {item.old_base_scheme_id for item in all_targets}
    new_ids = {item.new_base_scheme_id for item in all_targets}
    if len(all_targets) != 30 or len(old_ids) != 26 or len(new_ids) != 30:
        raise ValueError(
            "migration mapping must contain the locked 26-to-30 identity set"
        )
    if len(all_targets) != len(set(all_targets)) or old_ids.intersection(new_ids):
        raise ValueError("migration mapping identities overlap or targets repeat")
    old_grids = {
        (
            item.old_base_scheme_id,
            item.task_type,
            item.target_tenor,
            item.target_rule,
        )
        for item in all_targets
    }
    if len(old_grids) != len(all_targets):
        raise ValueError("migration mapping contains duplicate Native business grids")
    return waves


def select_native_successor_wave(
    waves: Mapping[str, NativeSuccessorWave],
    wave_id: str,
    *,
    old_scheme_id: str | None = None,
) -> NativeSuccessorWave:
    """选择整批或明确的 per-scheme 子范围。"""
    try:
        wave = waves[wave_id]
    except KeyError as exc:
        raise ValueError(f"unknown migration wave: {wave_id}") from exc
    if wave.switch_mode == "per_scheme":
        if old_scheme_id is None:
            raise ValueError(
                f"wave {wave_id} requires --old-scheme-id for per-scheme cutover"
            )
        targets = tuple(
            item for item in wave.targets
            if item.old_base_scheme_id == old_scheme_id
        )
        if not targets:
            raise ValueError(
                f"old scheme {old_scheme_id} is not part of wave {wave_id}"
            )
        return replace(wave, targets=targets)
    if old_scheme_id is not None:
        raise ValueError(f"wave {wave_id} does not accept --old-scheme-id")
    return wave


def _read_reviewed_w3b_bytes(path: Path, expected: str) -> bytes:
    """按已批准摘要读取同一份有界原件，拒绝路径穿越与符号链接。"""
    if not path.is_absolute() or ".." in path.parts or any(
        part.is_symlink() for part in (path, *path.parents)
    ):
        raise ValueError("reviewed W3B evidence path is unsafe")
    if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError(f"reviewed W3B evidence is not a bounded regular file: {path}")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError(f"reviewed W3B evidence SHA-256 mismatch: {path}")
    return payload


def _verify_reviewed_w3b_runtime(binding: Mapping[str, object], evidence_dir: Path) -> None:
    """复验原 ECS 的两套解释器、完整包记录及实际 locale，不在 Mac 代签环境。"""
    from scheduler.blackbox_v2_runner import DEFAULT_RUNTIME_PROFILE, _python_runtime, _runtime_environment

    if sys.platform != "linux" or str(os.environ.get("BOND_ALGO_CONDA_ENV") or "forecast_env").strip() != "forecast_env":
        raise ValueError("reviewed W3B evidence requires the captured ECS Native runtime")
    for kind, env_name in (("native", "forecast_env"), ("successor", "forecast_env_blackbox_v1")):
        profile = replace(DEFAULT_RUNTIME_PROFILE, conda_env=env_name)
        if kind == "native":
            profile = replace(profile, environment_defaults=tuple(sorted(binding["installed_locale"].items())))
        runtime = _python_runtime(profile)
        expected = binding["environment"][kind]
        if str(runtime.executable) != expected["executable"] or str(runtime.prefix) != expected["prefix"]:
            raise ValueError("reviewed W3B runtime path differs from original ECS execution")
        paths = [runtime.executable, *sorted(runtime.prefix.glob("conda-meta/*.json")),
                 *sorted(runtime.prefix.glob("lib/python*/site-packages/*.dist-info/METADATA")),
                 *sorted(runtime.prefix.glob("lib/python*/site-packages/*.dist-info/RECORD"))]
        if {str(path) for path in paths} != set(expected["files"]):
            raise ValueError("reviewed W3B runtime package inventory changed")
        for path in paths:
            _read_reviewed_w3b_bytes(path, expected["files"][str(path)])
        environment = _runtime_environment(profile, evidence_dir, python_executable=runtime.executable)
        if {key: environment[key] for key in ("LANG", "LC_ALL", "TZ") if key in environment} != expected["effective_locale"]:
            raise ValueError("reviewed W3B effective runtime locale changed")


def _load_reviewed_w3b_comparison(
    *,
    project_root: Path,
    target: NativeSuccessorTarget,
    new_config: SchemeConfig,
    evidence_dir: Path,
    data_dir: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    """只读复验三份已批准 ECS 333 条原件，复用完整参考链而不启动算法。"""
    approved = _REVIEWED_W3B_EVIDENCE.get(target.old_base_scheme_id)
    if (approved is None or target.new_base_scheme_id != target.old_base_scheme_id + "_bbv2"
            or target.target_tenor != "10Y" or target.task_type != "T+5"
            or target.old_horizon != 5 or target.new_horizon != 5 or target.target_rule is not None
            or new_config.scheme_id != target.new_base_scheme_id
            or new_config.incremental_state is not True
            or new_config.code_hash != approved["code_sha256"]
            or load_scheme_config(project_root / "schemes" / target.old_base_scheme_id / "config.yaml").code_hash != approved["old_code_hash"]):
        raise ValueError("reviewed W3B target or exact code identity mismatch")
    bound = _read_reviewed_w3b_bytes
    bound(new_config.delivery_script, approved["code_sha256"])
    bound(new_config.delivery_metadata, approved["metadata_sha256"])
    report = json.loads(bound(evidence_dir / "comparison-report.json", approved["report_sha256"]))
    binding = json.loads(bound(_W3B_SOURCE / "binding.json", _W3B_BINDING_SHA256))
    if report["status"] != "passed" or report["all_five_fields_equal_in_request_order"] is not True:
        raise ValueError("reviewed W3B comparison did not pass all five fields")
    files = binding["files"]
    input_files = {name: files[str(_W3B_SOURCE / "data" / name)] for name in sorted(_DATABRIDGE_FILES)}
    for name, digest in input_files.items():
        bound(data_dir / name, digest)

    family = approved["family"]
    if family == "say":
        request_path = _W3B_SOURCE / "requests.csv"
        request_payload = bound(request_path, files[str(request_path)])
        native_dir = Path("/opt/bond-factor-lab/incoming/w3b-remaining-20260909.Aj6bv8/remaining-execution")
        native_payload = bound(native_dir / "native-run/native.csv", report["native_csv_sha256"])
        result_digest = report["output_sha256"]
        started = json.loads(bound(evidence_dir / "started.json", "c85c0dc62727e752dccfd31662c3f1e997f9069202632e77ee40794b7d2ac57b"))
        execution = json.loads(bound(evidence_dir / "execution.json", "ce758a0a2915d7a2150da1b6d265ea7b3160b342659ae06daec3b45e90d571c6"))
        native_execution = json.loads(bound(native_dir / "native-execution.json", "f3e9a9cc6a7b8c315df8b3270139e8899024ab7b69260bac8f599aa957b90156"))
        native_started = json.loads(bound(native_dir / "started.json", "c6d918a3c4c212a7747b69a39bf5693e8f9062479651d4b642721dff0d1598c0"))
        bound(native_dir.parent / "native_reference_v2.py", native_started["reference_v2_sha256"])
        bound(native_dir.parent / "run_remaining_comparison.py", native_started["driver_sha256"])
        bound(Path(approved["execution_dir"]).parent / "run_fixed_full_comparison.py", started["driver_sha256"])
        if (report["binding_sha256"] != _W3B_BINDING_SHA256 or report["request_count"] != 333
                or execution["returncode"] != 0 or native_execution["returncode"] != 0):
            raise ValueError("reviewed W3B SAY execution identity mismatch")
        source_prefix = _W3B_SOURCE / "native-source/schemes" / target.old_base_scheme_id
        source_files = files
    else:
        original_dir = Path(approved["execution_dir"])
        request_path = _W3B_STAGED / "reference-inputs" / (family + "-requests.csv")
        native_path = _W3B_STAGED / "native-execution/results" / (target.old_base_scheme_id + ".csv")
        artifacts = report["artifacts"]
        bound(original_dir.parent / ("run_" + family + "_comparison.py"), report["identity"]["driver_sha256"])
        native_report = json.loads(bound(_W3B_STAGED / "native-execution/comparison-report.json", report["native_report_sha256"]))
        bound(_W3B_STAGED / "native_reference.py", native_report["identity"]["reference_sha256"])
        bound(_W3B_STAGED / "run_family_comparison.py", native_report["identity"]["driver_sha256"])
        payloads = {}
        for original, digest in artifacts.items():
            path = Path(original)
            if ".." in path.parts or not (path.is_relative_to(original_dir) or path.is_relative_to(_W3B_STAGED)):
                raise ValueError("reviewed W3B report artifact escaped approved roots")
            local = evidence_dir / path.relative_to(original_dir) if path.is_relative_to(original_dir) else path
            payload = bound(local, digest)
            if path in (request_path, native_path):
                payloads[path] = payload
        request_payload, native_payload = payloads[request_path], payloads[native_path]
        result_digest = artifacts[str(original_dir / "candidate.csv")]
        source_prefix = _W3B_STAGED / "reference-inputs/native-source/schemes" / target.old_base_scheme_id
        source_files = artifacts
        if report["identity"]["binding_sha256"] != _W3B_BINDING_SHA256 or report["identity"]["request_count"] != 333:
            raise ValueError("reviewed W3B family execution identity mismatch")

    # Native code_hash 未覆盖 inference.py；额外锁住参考实际读取的四文件完整集合。
    source_hashes = {Path(path).relative_to(source_prefix).as_posix(): digest
                     for path, digest in source_files.items() if Path(path).is_relative_to(source_prefix)}
    if set(source_hashes) != {"inference.py", "core/__init__.py", "core/data_alignment.py", "core/v31_common.py"}:
        raise ValueError("reviewed W3B Native source closure is incomplete")
    for relative, digest in source_hashes.items():
        bound(source_prefix / relative, digest)
        bound(project_root / "schemes" / target.old_base_scheme_id / relative, digest)
    successor_payload = bound(evidence_dir / "candidate.csv", result_digest)
    reader = csv.DictReader(io.StringIO(request_payload.decode("utf-8-sig"), newline=""))
    if reader.fieldnames != list(REQUEST_FIELDS):
        raise ValueError("reviewed W3B Request fields mismatch")
    requests = [asdict(request_from_mapping(dict(row))) for row in reader]
    native = _standard_result_rows(native_payload, label="Native")
    successor = _standard_result_rows(successor_payload, label="successor")
    if (len(requests) != 333 or len({row["request_id"] for row in requests}) != 333
            or len(native) != 333 or native != successor
            or any(row["request_id"] != target.new_base_scheme_id + ":" + ":".join(
                row[field] for field in ("predict_date", "feature_date", "target_date")) for row in requests)):
        raise ValueError("reviewed W3B must contain all 333 matching standard results")
    _verify_reviewed_w3b_runtime(binding, evidence_dir)
    return requests, native, successor, {
        "generation_id": binding["snapshot"]["generation_id"],
        "data_snapshot_id": binding["snapshot"]["snapshot_id"],
        "data_files_sha256": input_files,
    }


def _load_reviewed_w3a_comparison(
    *,
    project_root: Path,
    target: NativeSuccessorTarget,
    new_config: SchemeConfig,
    evidence_dir: Path,
    data_dir: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    """复验已审查的两份 W3A 原件；只读且不运行算法，不接受外部成功声明。"""
    approved = _REVIEWED_W3A_EVIDENCE.get(target.old_base_scheme_id)
    if (
        approved is None
        or target.new_base_scheme_id != target.old_base_scheme_id + "_bbv2"
        or target.target_tenor != "5Y"
        or new_config.scheme_version != approved["scheme_version"]
        or load_scheme_config(
            project_root / "schemes" / target.old_base_scheme_id / "config.yaml"
        ).code_hash != approved["old_code_hash"]
    ):
        raise ValueError("reviewed W3A target or exact code identity mismatch")

    def bound(relative: str, expected: str) -> bytes:
        path = evidence_dir / relative
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("reviewed evidence must not contain symlinks")
        if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError(f"reviewed evidence is not a bounded regular file: {relative}")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError(f"reviewed evidence SHA-256 mismatch: {relative}")
        return payload

    identity = json.loads(bound("identity.json", approved["identity_sha256"]))
    summary = json.loads(bound("summary.json", approved["summary_sha256"]))
    report = json.loads(bound("work/native-comparison.report.json", summary["comparison_report_sha256"]))
    phase = json.loads(bound("work/native-cold-phase.identity.json", report["native_phase_identity_sha256"]))
    dependency = json.loads(bound("work/dependency-report.json", phase["execution_identity"]["dependency_sha256"]))
    request_payload = bound("work/formal.csv", report["request_sha256"])
    native_payload = bound("work/native-compared333.csv", summary["native_result_sha256"])
    successor_payload = bound("work/full333.result.csv", report["candidate_result_sha256"])
    if (
        summary["count"] != 333 or summary["mismatches"] != 0
        or report["matched"] != 333 or report["mismatches"] != 0
        or dependency["passed"] != 333 or dependency["failed"] != 0
        or len(dependency["requests"]) != 333
        or not all(item["passed"] and all(item["checks"].values()) for item in dependency["requests"])
        or dependency["request_sha256"] != report["request_sha256"]
        or not 0 < summary["seconds"] <= 7200
        or not 0 < summary["resources_sampled"]["peak_group_rss_bytes"] <= 4 * 1024**3
        or summary["resources_sampled"]["errors"]
    ):
        raise ValueError("reviewed reference is not a complete passed bounded comparison")
    # 哈希已锁定原始闭包；只把其中方案源码相对路径映射到候选 release。
    prefix = f"/schemes/{target.old_base_scheme_id}/"
    for original, expected in identity["source_closure"].items():
        if prefix in original:
            relative = Path("schemes") / target.old_base_scheme_id / original.split(prefix, 1)[1]
            if _sha256_file(project_root / relative) != expected:
                raise ValueError(f"reviewed Native source changed: {relative}")

    frozen = identity["input_identity"]
    runtime = identity["runtime_identity"]
    if (
        set(frozen["files"]) != _DATABRIDGE_FILES
        or frozen["snapshot_id"] != identity["snapshot_id"]
        or dependency["files"] != frozen["files"]
        or phase["execution_identity"]["input"]["files"] != frozen["files"]
        or phase["execution_identity"]["input"]["runtime_identity"] != runtime
    ):
        raise ValueError("reviewed reference input or runtime closure mismatch")
    from scheduler.blackbox_v2_runner import DEFAULT_RUNTIME_PROFILE, _state_identities

    # 与原参考相同的实际 Python/包/环境指纹，不把环境名称相似当作等价。
    current_runtime, current_input = _state_identities(
        new_config.blackbox_metadata, new_config.delivery_script, data_dir,
        identity["snapshot_id"], DEFAULT_RUNTIME_PROFILE,
    )
    if (current_runtime, current_input) != (runtime, frozen):
        raise ValueError("reviewed reference differs from candidate runtime or frozen five-file input")
    if "formal_admission_sha256" in identity:
        admission = json.loads(bound("formal-admission.json", identity["formal_admission_sha256"]))
        if (
            admission["scheme_version"] != new_config.scheme_version
            or admission["code_hash"] != new_config.code_hash
            or admission["config_hash"] != new_config.config_hash
            or admission["manifest_hash"] != new_config.manifest_hash
            or admission["environment_fingerprint"] != new_config.environment_fingerprint
            or admission["generation_id"] != identity["generation_id"]
            or admission["data_snapshot_id"] != identity["snapshot_id"]
            or admission["requests_sha256"] != report["request_sha256"]
            or admission["result_sha256"] != report["candidate_result_sha256"]
        ):
            raise ValueError("reviewed formal Result provenance mismatch")
    elif identity["requests_sha256"] != report["request_sha256"] or (
        identity["candidate_result_sha256"] != report["candidate_result_sha256"]
    ):
        raise ValueError("reviewed offline Result provenance mismatch")

    reader = csv.DictReader(io.StringIO(request_payload.decode("utf-8-sig"), newline=""))
    if reader.fieldnames != list(REQUEST_FIELDS):
        raise ValueError("reviewed Request fields mismatch")
    request_rows = [asdict(request_from_mapping(dict(row))) for row in reader]
    if len(request_rows) != 333 or len({row["request_id"] for row in request_rows}) != 333:
        raise ValueError("reviewed Request count or uniqueness mismatch")
    return (
        request_rows,
        _standard_result_rows(native_payload, label="Native"),
        _standard_result_rows(successor_payload, label="successor"),
        {"generation_id": identity["generation_id"], "data_snapshot_id": identity["snapshot_id"],
         "data_files_sha256": dict(frozen["files"])},
    )


def _standard_result_rows(payload: bytes, *, label: str) -> list[dict[str, object]]:
    """从已校验的同一份字节解析标准结果，避免哈希后重新打开原件。"""
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8"), newline=""))
    if reader.fieldnames != list(_STANDARD_RESULT_FIELDS):
        raise ValueError(f"{label} Result fields differ from the five-field contract")
    raw = list(reader)
    if not raw:
        raise ValueError(f"{label} result evidence must be non-empty")
    rows: list[dict[str, object]] = []
    request_ids: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != set(_STANDARD_RESULT_FIELDS):
            raise ValueError(f"{label} Result fields differ from the five-field contract")
        request_id = _nonempty(item.get("request_id"), f"{label}.request_id")
        if request_id in request_ids:
            raise ValueError(f"{label} Result contains duplicate request_id")
        request_ids.add(request_id)
        normalized: dict[str, object] = {"request_id": request_id}
        for field in ("predict_date", "feature_date", "target_date"):
            value = _nonempty(item.get(field), f"{label}.{field}")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"{label}.{field} must be an ISO date") from exc
            normalized[field] = value
        direction_text = str(item.get("predicted_direction") or "")
        try:
            direction = int(direction_text)
        except ValueError as exc:
            raise ValueError(f"{label}.predicted_direction is invalid") from exc
        if direction_text != str(direction) or direction not in {-1, 0, 1}:
            raise ValueError(f"{label}.predicted_direction is invalid")
        normalized["predicted_direction"] = direction
        rows.append(normalized)
    return rows


def validate_control_plane_evidence(
    raw: Mapping[str, object],
    *,
    wave: NativeSuccessorWave,
    now: datetime | None = None,
) -> dict[str, object]:
    """验证并规范化外部现场证据；缺项时禁止生成可授权摘要。"""
    if set(raw) != _CONTROL_PLANE_FIELDS:
        raise ValueError("control-plane evidence fields are invalid")
    _sha256(
        raw.get("_capture_sha256"),
        "control-plane capture SHA-256",
    )
    capture_payload = dict(raw)
    capture_digest = str(capture_payload.pop("_capture_sha256"))
    if _json_sha256(capture_payload) != capture_digest:
        raise ValueError("control-plane capture SHA-256 mismatch")
    if raw.get("schema_version") != "native-successor-control-plane-evidence-v1":
        raise ValueError("control-plane evidence schema_version is unsupported")
    if raw.get("wave") != wave.wave:
        raise ValueError("control-plane evidence wave mismatch")
    host = raw.get("host")
    if not isinstance(host, Mapping) or set(host) != {
        "hostname",
        "current_link",
        "release_root",
    }:
        raise ValueError("control-plane host evidence is invalid")
    for field in ("hostname", "current_link", "release_root"):
        _nonempty(host.get(field), f"host.{field}")
    deployment_target = _choice(
        raw.get("deployment_target"),
        "deployment_target",
        {"aliyun-gray", "mac3-production"},
    )
    if host.get("current_link") != str(_CURRENT_LINKS[deployment_target]):
        raise ValueError("control-plane current link does not match deployment target")
    if (
        deployment_target == "aliyun-gray"
        and wave.ecs_mode != "active"
    ):
        raise ValueError(
            "Mac3-only waves cannot perform an ECS lifecycle cutover"
        )
    release = raw.get("release")
    if not isinstance(release, Mapping) or set(release) != {
        "current_commit",
        "archive_sha256",
        "install_record_sha256",
        "comparator_source_sha256",
    }:
        raise ValueError("control-plane release evidence is invalid")
    _sha256(release.get("current_commit"), "release.current_commit", lengths={40, 64})
    _sha256(release.get("archive_sha256"), "release.archive_sha256")
    _sha256(release.get("install_record_sha256"), "release.install_record_sha256")
    _sha256(
        release.get("comparator_source_sha256"),
        "release.comparator_source_sha256",
    )
    databridge = raw.get("databridge")
    if not isinstance(databridge, Mapping) or set(databridge) != {
        "generation_id",
        "data_snapshot_id",
        "business_digest",
        "files",
    }:
        raise ValueError("control-plane DataBridge evidence is invalid")
    _nonempty(databridge.get("generation_id"), "databridge.generation_id")
    _nonempty(databridge.get("data_snapshot_id"), "databridge.data_snapshot_id")
    _sha256(databridge.get("business_digest"), "databridge.business_digest")
    files = databridge.get("files")
    if not isinstance(files, Mapping) or set(files) != _DATABRIDGE_FILES:
        raise ValueError("control-plane evidence must bind all five DataBridge files")
    for filename in sorted(files):
        _sha256(files[filename], f"databridge.files.{filename}")
    native_runtime = raw.get("native_runtime")
    if not isinstance(native_runtime, Mapping) or set(native_runtime) != {
        "conda_env",
        "environment_fingerprint",
    }:
        raise ValueError("control-plane Native runtime evidence is invalid")
    _nonempty(native_runtime.get("conda_env"), "native_runtime.conda_env")
    _sha256(
        native_runtime.get("environment_fingerprint"),
        "native_runtime.environment_fingerprint",
    )
    scheduler = raw.get("scheduler")
    if not isinstance(scheduler, Mapping) or set(scheduler) != {
        "control_plane",
        "cadence",
        "timer_fenced",
        "unique_writer",
        "installed_unit_hash",
        "observed_state_sha256",
    }:
        raise ValueError("control-plane scheduler evidence is invalid")
    expected_control_plane = (
        "systemd_one_shot"
        if deployment_target == "aliyun-gray"
        else "launchd_one_shot"
    )
    if scheduler.get("control_plane") != expected_control_plane:
        raise ValueError("control-plane scheduler type mismatch")
    if scheduler.get("cadence") != wave.cadence:
        raise ValueError("control-plane scheduler cadence mismatch")
    if scheduler.get("timer_fenced") is not True:
        raise ValueError("migration requires the cadence timer to be fenced")
    if scheduler.get("unique_writer") is not True:
        raise ValueError("migration requires proof of a unique Writer")
    _sha256(scheduler.get("installed_unit_hash"), "scheduler.installed_unit_hash")
    _sha256(
        scheduler.get("observed_state_sha256"),
        "scheduler.observed_state_sha256",
    )
    dashboard = raw.get("dashboard")
    if not isinstance(dashboard, Mapping) or set(dashboard) != {
        "active_scheme_set_sha256",
        "response_sha256",
    }:
        raise ValueError("control-plane Dashboard evidence is invalid")
    _sha256(
        dashboard.get("active_scheme_set_sha256"),
        "dashboard.active_scheme_set_sha256",
    )
    _sha256(dashboard.get("response_sha256"), "dashboard.response_sha256")
    captured_at = _utc_timestamp(raw.get("captured_at"), "captured_at")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("control-plane evidence validation clock must be timezone-aware")
    current = current.astimezone(timezone.utc)
    age_seconds = (current - captured_at).total_seconds()
    if age_seconds < 0:
        raise ValueError("control-plane evidence captured_at must not be in the future")
    if age_seconds > 900:
        raise ValueError("control-plane evidence is older than 15 minutes")
    if raw.get("verified_by") != "native-successor-controlled-capture-v1":
        raise ValueError("control-plane evidence was not produced by controlled capture")
    return json.loads(json.dumps(raw, ensure_ascii=False, sort_keys=True))


def _capture_scheduler_state(
    *,
    project_root: Path,
    deployment_target: str,
    cadence: str,
) -> dict[str, object]:
    if deployment_target == "aliyun-gray":
        timer_name, service_name = _SYSTEMD_UNITS[cadence]
        installed_root = Path("/etc/systemd/system")
        installed = [installed_root / timer_name, installed_root / service_name]
        expected = [
            project_root / "deploy" / "systemd" / timer_name,
            project_root / "deploy" / "systemd" / service_name,
        ]
        _require_matching_installed_controls(installed, expected)
        timer = _systemctl_show(timer_name)
        service = _systemctl_show(service_name)
        if (
            timer.get("LoadState") != "loaded"
            or timer.get("ActiveState") != "inactive"
            or timer.get("FragmentPath") != str(installed[0])
            or timer.get("NeedDaemonReload") != "no"
            or timer.get("DropInPaths")
        ):
            raise RuntimeError("migration requires the systemd timer to be fenced")
        if (
            service.get("LoadState") != "loaded"
            or service.get("ActiveState") != "inactive"
            or service.get("MainPID") not in {"", "0"}
            or service.get("FragmentPath") != str(installed[1])
            or service.get("NeedDaemonReload") != "no"
            or service.get("DropInPaths")
        ):
            raise RuntimeError("migration requires the systemd one-shot to be idle")
        _assert_no_prediction_process(cadence, control_plane="systemd")
        return {
            "control_plane": "systemd_one_shot",
            "cadence": cadence,
            "timer_fenced": True,
            "unique_writer": True,
            "installed_unit_hash": _paths_sha256(installed),
            "observed_state_sha256": _json_sha256(
                {"timer": timer, "service": service}
            ),
        }

    label = _LAUNCHD_LABELS[cadence]
    installed = [
        Path("/Users/macstudio0/Library/LaunchAgents") / f"{label}.plist"
    ]
    expected = [project_root / "deploy" / "launchd" / f"{label}.plist"]
    _require_matching_installed_controls(installed, expected)
    completed = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode == 0:
        raise RuntimeError("migration requires the launchd prediction job to be fenced")
    _assert_no_prediction_process(cadence, control_plane="launchd")
    return {
        "control_plane": "launchd_one_shot",
        "cadence": cadence,
        "timer_fenced": True,
        "unique_writer": True,
        "installed_unit_hash": _paths_sha256(installed),
        "observed_state_sha256": _json_sha256(
            {
                "launchctl_returncode": completed.returncode,
                "launchctl_stdout": completed.stdout,
                "launchctl_stderr": completed.stderr,
            }
        ),
    }


def _systemctl_show(unit: str) -> dict[str, str]:
    completed = subprocess.run(
        [
            "/usr/bin/systemctl",
            "show",
            unit,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            "--property=UnitFileState",
            "--property=NextElapseUSecRealtime",
            "--property=FragmentPath",
            "--property=NeedDaemonReload",
            "--property=DropInPaths",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"systemctl read failed for {unit}")
    return {
        key: value
        for line in completed.stdout.splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }


def _assert_no_prediction_process(cadence: str, *, control_plane: str) -> None:
    pattern = (
        f"scheduler.{control_plane}_prediction_runner --cadence {cadence}"
        if cadence in {"daily", "weekly"}
        else f"scripts/run_close_predictions.py --control-plane {control_plane}"
    )
    completed = subprocess.run(
        ["/usr/bin/pgrep", "-f", pattern],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode == 0:
        raise RuntimeError("migration detected a running prediction process")
    if completed.returncode != 1:
        raise RuntimeError("prediction process inspection failed")


def _require_matching_installed_controls(
    installed: list[Path],
    expected: list[Path],
) -> None:
    for actual, canonical in zip(installed, expected, strict=True):
        if actual.is_symlink() or not actual.is_file():
            raise RuntimeError(f"installed scheduler control is missing: {actual}")
        if canonical.is_symlink() or not canonical.is_file():
            raise RuntimeError(f"release scheduler control is missing: {canonical}")
        if actual.read_bytes() != canonical.read_bytes():
            raise RuntimeError(f"installed scheduler control differs: {actual}")


def _paths_sha256(paths: list[Path]) -> str:
    return _json_sha256(
        [
            {"name": path.name, "sha256": _sha256_file(path)}
            for path in sorted(paths, key=lambda item: item.name)
        ]
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_tree_sha256(root: Path) -> str:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative in {".bfl-release.env", ".bfl-release-install.json"}:
            continue
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise RuntimeError("release source tree contains an unsupported entry")
        if path.is_dir():
            continue
        entries.append(
            {
                "path": relative,
                "executable": bool(stat.S_IMODE(path.stat().st_mode) & 0o111),
                "sha256": _sha256_file(path),
            }
        )
    return _json_sha256(entries)


def _json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _parse_target(raw: object) -> NativeSuccessorTarget:
    if not isinstance(raw, dict) or set(raw) != _TARGET_FIELDS:
        raise ValueError("migration target fields are invalid")
    old_horizon = raw["old_horizon"]
    new_horizon = raw["new_horizon"]
    if (
        isinstance(old_horizon, bool)
        or not isinstance(old_horizon, int)
        or old_horizon <= 0
        or isinstance(new_horizon, bool)
        or not isinstance(new_horizon, int)
        or new_horizon <= 0
    ):
        raise ValueError("migration target horizons must be positive integers")
    target_rule = raw["target_rule"]
    if target_rule is not None:
        target_rule = _nonempty(target_rule, "target_rule")
    return NativeSuccessorTarget(
        old_base_scheme_id=_nonempty(
            raw["old_base_scheme_id"], "old_base_scheme_id"
        ),
        new_base_scheme_id=_nonempty(
            raw["new_base_scheme_id"], "new_base_scheme_id"
        ),
        task_type=_nonempty(raw["task_type"], "task_type"),
        target_tenor=_nonempty(raw["target_tenor"], "target_tenor"),
        target_rule=target_rule,
        old_horizon=old_horizon,
        new_horizon=new_horizon,
    )


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a trimmed non-empty string")
    return value


def _choice(value: object, field: str, choices: set[str]) -> str:
    normalized = _nonempty(value, field)
    if normalized not in choices:
        raise ValueError(f"{field} has unsupported value: {normalized}")
    return normalized


def _sha256(
    value: object,
    field: str,
    *,
    lengths: set[int] | None = None,
) -> str:
    normalized = _nonempty(value, field)
    allowed_lengths = lengths or {64}
    if len(normalized) not in allowed_lengths:
        raise ValueError(f"{field} must be a hexadecimal digest")
    try:
        int(normalized, 16)
    except ValueError as exc:
        raise ValueError(f"{field} must be a hexadecimal digest") from exc
    if normalized != normalized.lower():
        raise ValueError(f"{field} must use lowercase hexadecimal")
    return normalized


def _utc_timestamp(value: object, field: str) -> datetime:
    raw = _nonempty(value, field)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field} must be an ISO-8601 UTC timestamp")
    return parsed.astimezone(timezone.utc)
