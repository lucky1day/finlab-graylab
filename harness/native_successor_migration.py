from __future__ import annotations

import csv
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping

from harness.blackbox_v2.gates import verify_passed_blackbox_backtest
from scheduler.discovery import SchemeConfig, load_scheme_config
from scheduler.repository import (
    NativeSuccessorBacktestEvidence,
    NativeSuccessorTarget,
    apply_native_successor_migration,
    native_successor_plan_sha256,
    read_native_successor_migration_plan,
)
from shared.blackbox_v2.contracts import REQUEST_FIELDS, load_requests
from shared.blackbox_v2.environment_manifest import load_environment_fingerprint


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
_EQUIVALENCE_RECEIPT_DIR = "native_successor_equivalence"
_COMPARISON_INPUT_FIELDS = {
    "schema_version",
    "wave",
    "generation_id",
    "data_snapshot_id",
    "data_dir",
    "comparisons",
}
_COMPARISON_FIELDS = {
    "old_base_scheme_id",
    "new_base_scheme_id",
    "target_tenor",
    "requests_path",
}
_STANDARD_RESULT_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
)
_NATIVE_COMPARISON_TIMEOUT_SEC = 7200
_SUCCESSOR_COMPARISON_TIMEOUT_SEC = 7200


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


def build_native_successor_preflight(
    engine,
    *,
    project_root: Path,
    wave: NativeSuccessorWave,
    expected_database_name: str,
    expected_server_uuid: str,
    expected_action: str | None = None,
) -> dict[str, object]:
    """加载 canonical config/回测证据并生成只读事务计划。"""
    captured_control_plane = capture_native_successor_control_plane(
        engine,
        project_root=project_root,
        wave=wave,
    )
    old_configs, new_configs, backtests = _load_execution_inputs(
        engine,
        project_root=project_root,
        wave=wave,
    )
    equivalence_evidence = load_native_successor_equivalence_evidence(
        project_root,
        wave=wave,
    )
    verified_control_plane = validate_control_plane_evidence(
        captured_control_plane,
        wave=wave,
    )
    plan = read_native_successor_migration_plan(
        engine,
        wave=wave.wave,
        targets=wave.targets,
        old_configs=old_configs,
        new_configs=new_configs,
        backtests=backtests,
        equivalence_evidence=equivalence_evidence,
        control_plane_evidence=verified_control_plane,
        expected_database_name=expected_database_name,
        expected_server_uuid=expected_server_uuid,
        expected_action=expected_action,
    )
    return {
        "plan": plan,
        "plan_sha256": native_successor_plan_sha256(plan),
    }


def execute_native_successor_migration(
    engine,
    *,
    project_root: Path,
    wave: NativeSuccessorWave,
    action: str,
    expected_plan_sha256: str,
    approved_by: str,
    expected_database_name: str,
    expected_server_uuid: str,
) -> dict[str, object]:
    """使用相同 canonical 输入执行摘要绑定的原子迁移。"""
    old_configs, new_configs, backtests = _load_execution_inputs(
        engine,
        project_root=project_root,
        wave=wave,
    )
    equivalence_evidence = load_native_successor_equivalence_evidence(
        project_root,
        wave=wave,
    )
    def recapture() -> Mapping[str, object]:
        return capture_native_successor_control_plane(
            engine,
            project_root=project_root,
            wave=wave,
        )

    return apply_native_successor_migration(
        engine,
        wave=wave.wave,
        action=action,
        expected_plan_sha256=expected_plan_sha256,
        approved_by=approved_by,
        approved_at=datetime.now(timezone.utc),
        targets=wave.targets,
        old_configs=old_configs,
        new_configs=new_configs,
        backtests=backtests,
        equivalence_evidence=equivalence_evidence,
        control_plane_evidence_reader=recapture,
        expected_database_name=expected_database_name,
        expected_server_uuid=expected_server_uuid,
    )


def load_native_successor_equivalence_evidence(
    project_root: Path,
    *,
    wave: NativeSuccessorWave,
) -> Mapping[str, object]:
    """读取随 immutable release 发布的临时同输入等价凭据。"""
    if wave.switch_mode == "per_scheme":
        old_ids = sorted({item.old_base_scheme_id for item in wave.targets})
        if len(old_ids) != 1:
            raise RuntimeError("per-scheme migration must select exactly one Native base")
        filename = f"{wave.wave}--{old_ids[0]}.json"
    else:
        filename = f"{wave.wave}.json"
    path = project_root / "deploy" / _EQUIVALENCE_RECEIPT_DIR / filename
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"migration equivalence receipt is unavailable: {filename}"
        ) from exc
    if not isinstance(raw, dict):
        raise RuntimeError("migration equivalence receipt must be a JSON object")
    producer = raw.get("producer")
    if not isinstance(producer, Mapping) or (
        producer.get("comparator_source_sha256") != _comparator_source_sha256()
    ):
        raise RuntimeError(
            "migration equivalence receipt comparator differs from current release"
        )
    return raw


def build_native_successor_equivalence_receipt(
    *,
    project_root: Path,
    wave: NativeSuccessorWave,
    comparison_bundle_path: Path,
) -> dict[str, object]:
    """从同输入标准结果生成可复验的 Native/successor 等价凭据。"""
    bundle_path = comparison_bundle_path.resolve(strict=True)
    raw = json.loads(bundle_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != _COMPARISON_INPUT_FIELDS:
        raise ValueError("comparison bundle fields are invalid")
    if (
        raw.get("schema_version") != "native-successor-comparison-input-v1"
        or raw.get("wave") != wave.wave
    ):
        raise ValueError("comparison bundle identity mismatch")
    generation_id = _nonempty(raw.get("generation_id"), "generation_id")
    data_snapshot_id = _nonempty(raw.get("data_snapshot_id"), "data_snapshot_id")
    data_dir = _resolve_evidence_path(bundle_path, raw.get("data_dir"), "data_dir")
    if not data_dir.is_dir():
        raise ValueError("comparison data_dir must be a directory")
    data_files_sha256 = {
        filename: _sha256_file(data_dir / filename)
        for filename in sorted(_DATABRIDGE_FILES)
    }

    old_configs = {
        target.old_base_scheme_id: load_scheme_config(
            project_root / "schemes" / target.old_base_scheme_id / "config.yaml"
        )
        for target in wave.targets
    }
    new_configs = {
        target.new_base_scheme_id: load_scheme_config(
            project_root / "schemes" / target.new_base_scheme_id / "config.yaml"
        )
        for target in wave.targets
    }
    runtime_profiles = {config.runtime_profile for config in new_configs.values()}
    if len(runtime_profiles) != 1 or None in runtime_profiles:
        raise ValueError("successor wave must use one explicit runtime profile")
    successor_runtime_fingerprint = load_environment_fingerprint(
        project_root,
        expected_runtime_profile=str(next(iter(runtime_profiles))),
    )
    native_runtime_fingerprint = _capture_native_runtime_identity()[
        "environment_fingerprint"
    ]

    comparisons = raw.get("comparisons")
    if not isinstance(comparisons, list):
        raise ValueError("comparison bundle comparisons must be a list")
    by_identity: dict[tuple[str, str, str], Mapping[str, object]] = {}
    for comparison in comparisons:
        if not isinstance(comparison, Mapping) or set(comparison) != _COMPARISON_FIELDS:
            raise ValueError("comparison entry fields are invalid")
        identity = (
            str(comparison.get("old_base_scheme_id") or ""),
            str(comparison.get("new_base_scheme_id") or ""),
            str(comparison.get("target_tenor") or ""),
        )
        if identity in by_identity:
            raise ValueError("comparison bundle contains duplicate targets")
        by_identity[identity] = comparison
    expected_identities = {
        (
            target.old_base_scheme_id,
            target.new_base_scheme_id,
            target.target_tenor,
        )
        for target in wave.targets
    }
    if set(by_identity) != expected_identities:
        raise ValueError("comparison bundle target coverage mismatch")

    receipt_targets: list[dict[str, object]] = []
    for target in wave.targets:
        identity = (
            target.old_base_scheme_id,
            target.new_base_scheme_id,
            target.target_tenor,
        )
        comparison = by_identity[identity]
        requests_path = _resolve_evidence_path(
            bundle_path,
            comparison.get("requests_path"),
            "requests_path",
        )
        request_rows = [asdict(request) for request in load_requests(requests_path)]
        native_rows, successor_rows = _execute_controlled_comparison(
            project_root=project_root,
            target=target,
            new_config=new_configs[target.new_base_scheme_id],
            requests_path=requests_path,
            data_dir=data_dir,
        )
        if len(native_rows) != len(successor_rows) or not native_rows:
            raise ValueError("comparison result row counts differ or are empty")
        if len(request_rows) != len(successor_rows):
            raise ValueError("comparison Request and Result row counts differ")
        for request, result in zip(request_rows, successor_rows, strict=True):
            if any(
                request[field] != result[field]
                for field in _STANDARD_RESULT_FIELDS[:-1]
            ):
                raise ValueError(
                    "successor Result order or echo differs from Request artifact"
                )
        mismatch_counts = {
            "request_id_mismatch_count": 0,
            "predict_date_mismatch_count": 0,
            "feature_date_mismatch_count": 0,
            "target_date_mismatch_count": 0,
            "direction_mismatch_count": 0,
        }
        for native, successor in zip(native_rows, successor_rows, strict=True):
            for field, counter in (
                ("request_id", "request_id_mismatch_count"),
                ("predict_date", "predict_date_mismatch_count"),
                ("feature_date", "feature_date_mismatch_count"),
                ("target_date", "target_date_mismatch_count"),
                ("predicted_direction", "direction_mismatch_count"),
            ):
                if native[field] != successor[field]:
                    mismatch_counts[counter] += 1
        if any(mismatch_counts.values()):
            raise ValueError(
                "Native/successor comparison contains non-zero mismatches: "
                f"{mismatch_counts}"
            )
        native_rows = sorted(native_rows, key=_standard_result_sort_key)
        successor_rows = sorted(successor_rows, key=_standard_result_sort_key)
        request_by_id = {row["request_id"]: row for row in request_rows}
        if len(request_by_id) != len(request_rows):
            raise ValueError("comparison Request artifact contains duplicate request_id")
        full_requests = [
            {field: request_by_id[str(row["request_id"])][field] for field in REQUEST_FIELDS}
            for row in successor_rows
        ]
        requests = [
            {key: row[key] for key in _STANDARD_RESULT_FIELDS[:-1]}
            for row in successor_rows
        ]
        request_sha256 = _json_sha256({"requests": requests})
        native_result_sha256 = _json_sha256({"results": native_rows})
        successor_result_sha256 = _json_sha256({"results": successor_rows})
        receipt_targets.append(
            {
                "old_base_scheme_id": target.old_base_scheme_id,
                "new_base_scheme_id": target.new_base_scheme_id,
                "task_type": target.task_type,
                "target_tenor": target.target_tenor,
                "old_horizon": target.old_horizon,
                "new_horizon": target.new_horizon,
                "old_code_hash": old_configs[target.old_base_scheme_id].code_hash,
                "new_code_hash": new_configs[target.new_base_scheme_id].code_hash,
                "native_runtime_environment_fingerprint": (
                    native_runtime_fingerprint
                ),
                "request_artifact_sha256": _json_sha256(
                    {"requests": full_requests}
                ),
                "request_sha256": request_sha256,
                "request_count": len(successor_rows),
                "native_result_sha256": native_result_sha256,
                "successor_result_sha256": successor_result_sha256,
                **mismatch_counts,
            }
        )
    return {
        "schema_version": "native-successor-equivalence-v2",
        "wave": wave.wave,
        "producer": {
            "tool": "native-successor-controlled-comparator",
            "tool_version": "2",
            "comparator_source_sha256": _comparator_source_sha256(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "generation_id": generation_id,
        "data_snapshot_id": data_snapshot_id,
        "data_files_sha256": data_files_sha256,
        "runtime_environment_fingerprint": successor_runtime_fingerprint,
        "targets": sorted(
            receipt_targets,
            key=lambda item: (
                str(item["old_base_scheme_id"]),
                str(item["new_base_scheme_id"]),
                str(item["target_tenor"]),
            ),
        ),
    }


def write_native_successor_equivalence_receipt(
    *,
    project_root: Path,
    wave: NativeSuccessorWave,
    comparison_bundle_path: Path,
) -> dict[str, object]:
    """独占写入 canonical receipt 路径，拒绝覆盖既有证据。"""
    receipt = build_native_successor_equivalence_receipt(
        project_root=project_root,
        wave=wave,
        comparison_bundle_path=comparison_bundle_path,
    )
    if wave.switch_mode == "per_scheme":
        old_ids = sorted({item.old_base_scheme_id for item in wave.targets})
        if len(old_ids) != 1:
            raise ValueError("per-scheme comparison must select one Native base")
        filename = f"{wave.wave}--{old_ids[0]}.json"
    else:
        filename = f"{wave.wave}.json"
    output = project_root / "deploy" / _EQUIVALENCE_RECEIPT_DIR / filename
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(receipt, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    return {
        "receipt_path": str(output),
        "receipt_sha256": _sha256_file(output),
        "request_count": sum(
            int(item["request_count"]) for item in receipt["targets"]
        ),
    }


def _resolve_evidence_path(bundle: Path, raw: object, field: str) -> Path:
    value = _nonempty(raw, field)
    path = Path(value)
    if not path.is_absolute():
        path = bundle.parent / path
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"comparison evidence path is unavailable: {field}") from exc


def _execute_controlled_comparison(
    *,
    project_root: Path,
    target: NativeSuccessorTarget,
    new_config: SchemeConfig,
    requests_path: Path,
    data_dir: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """用固定 old/new 命令执行同一 Request artifact，拒绝外部结果注入。"""
    if not requests_path.is_file() or requests_path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("comparison Request artifact is not a bounded regular file")
    if new_config.delivery_script is None:
        raise ValueError("successor delivery script is missing")
    native_env = str(os.environ.get("BOND_ALGO_CONDA_ENV") or "forecast_env").strip()
    conda = _conda_executable()
    with tempfile.TemporaryDirectory(prefix="bfl-native-successor-compare-") as raw_tmp:
        temp_root = Path(raw_tmp)
        native_output = temp_root / "native" / "result.csv"
        native_output.parent.mkdir(mode=0o700)
        successor_output = temp_root / "successor" / "result.csv"
        native_command = [
            str(conda),
            "run",
            "--no-capture-output",
            "-n",
            native_env,
            "python",
            "-m",
            "harness.native_successor_comparator_runner",
            "--old-scheme-id",
            target.old_base_scheme_id,
            "--new-scheme-id",
            target.new_base_scheme_id,
            "--target-tenor",
            target.target_tenor,
            "--requests",
            str(requests_path),
            "--data-dir",
            str(data_dir),
            "--output",
            str(native_output),
        ]
        completed = subprocess.run(
            native_command,
            cwd=project_root,
            env={**os.environ, "PYTHONNOUSERSITE": "1"},
            check=False,
            capture_output=True,
            text=True,
            timeout=_NATIVE_COMPARISON_TIMEOUT_SEC,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "")[-2000:]
            raise RuntimeError(f"controlled Native comparison failed: {stderr}")
        from scheduler.blackbox_v2_runner import (
            DEFAULT_RUNTIME_PROFILE,
            execute_blackbox_cli,
        )

        state_kwargs = (
            {"state_output": successor_output.parent / "state.bin"}
            if getattr(new_config, "incremental_state", False) else {}
        )
        execute_blackbox_cli(
            script_path=new_config.delivery_script,
            mode="backtest",
            input_path=requests_path,
            data_dir=data_dir,
            output_path=successor_output,
            profile=DEFAULT_RUNTIME_PROFILE,
            timeout_sec=_SUCCESSOR_COMPARISON_TIMEOUT_SEC,
            **state_kwargs,
        )
        return (
            _load_standard_result_rows(native_output, label="Native"),
            _load_standard_result_rows(successor_output, label="successor"),
        )


def _load_standard_result_rows(path: Path, *, label: str) -> list[dict[str, object]]:
    if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError(f"{label} result evidence is not a bounded regular file")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
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


def _standard_result_sort_key(row: Mapping[str, object]) -> tuple[str, str, str, str]:
    return (
        str(row["target_date"]),
        str(row["predict_date"]),
        str(row["feature_date"]),
        str(row["request_id"]),
    )


def _comparator_source_sha256() -> str:
    root = Path(__file__).resolve().parent
    project_root = root.parent
    sources = {
        "native_successor_migration.py": _sha256_file(Path(__file__)),
        "native_successor_comparator_runner.py": _sha256_file(
            root / "native_successor_comparator_runner.py"
        ),
        "schemes/t5_daily/latest_prediction.py": _sha256_file(
            project_root / "schemes" / "t5_daily" / "latest_prediction.py"
        ),
        "schemes/daily_5y_2_v28/inference.py": _sha256_file(
            project_root / "schemes" / "daily_5y_2_v28" / "inference.py"
        ),
        "schemes/daily_5y_2_v28/core/v28_common.py": _sha256_file(
            project_root
            / "schemes"
            / "daily_5y_2_v28"
            / "core"
            / "v28_common.py"
        ),
        "schemes/daily_5y_2_v28/core/data_alignment.py": _sha256_file(
            project_root
            / "schemes"
            / "daily_5y_2_v28"
            / "core"
            / "data_alignment.py"
        ),
        "schemes/daily_7y_1_v28/inference.py": _sha256_file(
            project_root / "schemes" / "daily_7y_1_v28" / "inference.py"
        ),
        "schemes/daily_7y_1_v28/core/v28_common.py": _sha256_file(
            project_root
            / "schemes"
            / "daily_7y_1_v28"
            / "core"
            / "v28_common.py"
        ),
        "schemes/daily_7y_1_v28/core/data_alignment.py": _sha256_file(
            project_root
            / "schemes"
            / "daily_7y_1_v28"
            / "core"
            / "data_alignment.py"
        ),
    }
    return _json_sha256(sources)


def _conda_executable() -> Path:
    configured_conda = str(os.environ.get("CONDA_EXE") or "").strip()
    candidates = [
        Path(configured_conda) if configured_conda else None,
        *(
            parent / "bin" / "conda"
            for parent in Path(sys.executable).resolve().parents
        ),
    ]
    conda = next(
        (
            candidate
            for candidate in candidates
            if candidate is not None and candidate.is_file()
        ),
        None,
    )
    if conda is None:
        raise RuntimeError("conda executable is unavailable")
    return conda


def capture_native_successor_control_plane(
    engine,
    *,
    project_root: Path,
    wave: NativeSuccessorWave,
) -> dict[str, object]:
    """从当前 release、DataBridge、调度器和 Dashboard 直接采集只读证据。"""
    deployment_target = str(os.environ.get("BFL_DEPLOYMENT_TARGET") or "").strip()
    if deployment_target not in _CURRENT_LINKS:
        raise RuntimeError(
            "controlled migration preflight requires BFL_DEPLOYMENT_TARGET"
        )
    root = project_root.resolve(strict=True)
    current_link = _CURRENT_LINKS[deployment_target]
    if not current_link.is_symlink() or current_link.resolve(strict=True) != root:
        raise RuntimeError("migration command is not running from current release")
    release_record_path = root / ".bfl-release-install.json"
    release_record_bytes = release_record_path.read_bytes()
    release_record = json.loads(release_record_bytes.decode("utf-8"))
    expected_record_fields = {
        "schema_version",
        "commit",
        "archive_sha256",
        "source_tree_sha256",
        "runtime_root",
    }
    if not isinstance(release_record, dict) or set(release_record) != expected_record_fields:
        raise RuntimeError("current release install record is invalid")
    release_commit = _sha256(
        release_record.get("commit"),
        "release commit",
        lengths={40, 64},
    )
    ambient_commit = str(os.environ.get("BFL_RELEASE_COMMIT") or "").strip()
    if ambient_commit != release_commit:
        raise RuntimeError("current release environment commit mismatch")
    if _source_tree_sha256(root) != release_record.get("source_tree_sha256"):
        raise RuntimeError("current release source tree differs from install record")
    _validate_cutover_deployment_matrix(
        root,
        wave=wave,
        deployment_target=deployment_target,
    )

    from shared.input_artifacts import get_ready_blackbox_snapshot

    snapshot = get_ready_blackbox_snapshot(
        snapshot_date=date.today().isoformat(),
        require_fresh=False,
        factor_input_mode="algorithm_managed",
    )
    manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
    manifest_files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(manifest_files, Mapping) or set(manifest_files) != _DATABRIDGE_FILES:
        raise RuntimeError("current Blackbox snapshot is not a five-file generation")
    file_hashes: dict[str, str] = {}
    for filename in sorted(_DATABRIDGE_FILES):
        actual = _sha256_file(snapshot.data_dir / filename)
        profile = manifest_files.get(filename)
        if not isinstance(profile, Mapping) or profile.get("sha256") != actual:
            raise RuntimeError(f"current DataBridge snapshot file changed: {filename}")
        file_hashes[filename] = actual

    scheduler = _capture_scheduler_state(
        project_root=root,
        deployment_target=deployment_target,
        cadence=wave.cadence,
    )
    from backend.factor_lab_dashboard import build_factor_lab_dashboard

    dashboard = build_factor_lab_dashboard(engine)
    stable_dashboard = {
        key: value
        for key, value in dashboard.items()
        if key not in {"snapshot_id", "generated_at"}
    }
    active_ids = sorted(
        str(item.get("scheme_id"))
        for item in dashboard.get("schemes", [])
        if isinstance(item, Mapping)
    )
    evidence: dict[str, object] = {
        "schema_version": "native-successor-control-plane-evidence-v1",
        "wave": wave.wave,
        "deployment_target": deployment_target,
        "host": {
            "hostname": socket.gethostname(),
            "current_link": str(current_link),
            "release_root": str(root),
        },
        "release": {
            "current_commit": release_commit,
            "archive_sha256": _sha256(
                release_record.get("archive_sha256"),
                "release.archive_sha256",
            ),
            "install_record_sha256": hashlib.sha256(
                release_record_bytes
            ).hexdigest(),
            "comparator_source_sha256": _comparator_source_sha256(),
        },
        "databridge": {
            "generation_id": snapshot.generation_id,
            "data_snapshot_id": snapshot.snapshot_id,
            "business_digest": snapshot.business_digest,
            "files": file_hashes,
        },
        "native_runtime": _capture_native_runtime_identity(),
        "scheduler": scheduler,
        "dashboard": {
            "active_scheme_set_sha256": _json_sha256(active_ids),
            "response_sha256": _json_sha256(stable_dashboard),
        },
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "verified_by": "native-successor-controlled-capture-v1",
    }
    evidence["_capture_sha256"] = _json_sha256(evidence)
    return validate_control_plane_evidence(evidence, wave=wave)


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


def _capture_native_runtime_identity() -> dict[str, str]:
    """对当前 Native conda 环境的 explicit package 集生成现场指纹。"""
    conda_env = str(os.environ.get("BOND_ALGO_CONDA_ENV") or "forecast_env").strip()
    if not conda_env:
        raise RuntimeError("Native algorithm conda environment is empty")
    conda = _conda_executable()
    completed = subprocess.run(
        [str(conda), "list", "-n", conda_env, "--explicit"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError("failed to capture Native conda environment")
    explicit = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return {
        "conda_env": conda_env,
        "environment_fingerprint": _json_sha256(
            {"conda_env": conda_env, "explicit": explicit}
        ),
    }


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


def _validate_cutover_deployment_matrix(
    project_root: Path,
    *,
    wave: NativeSuccessorWave,
    deployment_target: str,
) -> None:
    path = project_root / "deploy" / "scheme_deployment_matrix_v1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    schemes = raw.get("schemes") if isinstance(raw, dict) else None
    if (
        raw.get("schema_version") != "scheme-deployment-matrix-v1"
        or not isinstance(schemes, Mapping)
    ):
        raise RuntimeError("deployment matrix is invalid")
    old_ids = {target.old_base_scheme_id for target in wave.targets}
    new_ids = {target.new_base_scheme_id for target in wave.targets}
    still_deployed = sorted(
        scheme_id
        for scheme_id in old_ids
        if deployment_target in schemes.get(scheme_id, [])
    )
    missing_successors = sorted(
        scheme_id
        for scheme_id in new_ids
        if deployment_target not in schemes.get(scheme_id, [])
    )
    if still_deployed or missing_successors:
        raise RuntimeError(
            "deployment matrix is not cutover-ready: "
            f"old_still_deployed={still_deployed} "
            f"new_missing={missing_successors}"
        )


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


def _load_execution_inputs(
    engine,
    *,
    project_root: Path,
    wave: NativeSuccessorWave,
) -> tuple[
    dict[str, SchemeConfig],
    dict[str, SchemeConfig],
    dict[str, NativeSuccessorBacktestEvidence],
]:
    old_ids = sorted({item.old_base_scheme_id for item in wave.targets})
    new_ids = sorted({item.new_base_scheme_id for item in wave.targets})
    old_configs = {
        scheme_id: load_scheme_config(
            project_root / "schemes" / scheme_id / "config.yaml"
        )
        for scheme_id in old_ids
    }
    loaded_new = {
        scheme_id: load_scheme_config(
            project_root / "schemes" / scheme_id / "config.yaml"
        )
        for scheme_id in new_ids
    }
    runtime_profiles = {cfg.runtime_profile for cfg in loaded_new.values()}
    if len(runtime_profiles) != 1:
        raise RuntimeError("successor wave must use exactly one runtime profile")
    current_environment_fingerprint = load_environment_fingerprint(
        project_root,
        expected_runtime_profile=next(iter(runtime_profiles)),
    )
    backtests: dict[str, NativeSuccessorBacktestEvidence] = {}
    new_configs: dict[str, SchemeConfig] = {}
    for scheme_id, cfg in loaded_new.items():
        published_backtest_run_id = _published_successor_backtest_run_id(
            engine,
            scheme_id=scheme_id,
        )
        passed = verify_passed_blackbox_backtest(
            engine,
            cfg,
            backtest_run_id=published_backtest_run_id,
        )
        _validate_successor_execution_evidence(
            cfg,
            passed,
            current_environment_fingerprint=current_environment_fingerprint,
        )
        new_configs[scheme_id] = replace(
            cfg,
            environment_fingerprint=passed.environment_fingerprint,
            data_snapshot_id=passed.data_snapshot_id,
        )
        backtests[scheme_id] = NativeSuccessorBacktestEvidence(
            backtest_run_id=passed.backtest_run_id,
            benchmark_id=passed.benchmark_id,
            data_snapshot_id=passed.data_snapshot_id,
            generation_id=passed.generation_id,
            runtime_profile=passed.runtime_profile,
            environment_fingerprint=passed.environment_fingerprint,
            code_hash=passed.code_hash,
            config_hash=passed.config_hash,
            manifest_hash=passed.manifest_hash,
            validator_policy_digest=passed.script_validator_policy_digest,
        )
    return old_configs, new_configs, backtests


def _published_successor_backtest_run_id(
    engine,
    *,
    scheme_id: str,
) -> int | None:
    from sqlalchemy import text

    with engine.connect() as connection:
        values = connection.execute(
            text(
                "SELECT DISTINCT backtest_run_id FROM t_scheme_predictions "
                "WHERE scheme_id = :scheme_id AND backtest_run_id IS NOT NULL "
                "ORDER BY backtest_run_id"
            ),
            {"scheme_id": scheme_id},
        ).scalars().all()
    if len(values) > 1:
        raise RuntimeError(
            f"successor has facts from multiple backtest runs: {scheme_id}"
        )
    return int(values[0]) if values else None


def _validate_successor_execution_evidence(
    cfg: SchemeConfig,
    passed: object,
    *,
    current_environment_fingerprint: str,
) -> None:
    if getattr(passed, "runtime_profile", None) != cfg.runtime_profile:
        raise RuntimeError(
            f"successor backtest runtime profile mismatch: {cfg.scheme_id}"
        )
    if (
        getattr(passed, "environment_fingerprint", None)
        != current_environment_fingerprint
    ):
        raise RuntimeError(
            f"successor backtest environment fingerprint mismatch: {cfg.scheme_id}"
        )


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
