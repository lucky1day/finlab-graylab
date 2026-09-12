"""临时 W2/W3A 原身份回收控制层；不计算算法、不导入历史或删除数据。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Mapping, Sequence

from harness import same_id_runtime_upgrade as control
from harness.runtime_upgrade_evidence import verify_identity_only_delivery_change
from scheduler import repository
from scheduler.discovery import load_scheme_config
from shared.blackbox_v2.environment_manifest import load_environment_fingerprint
from shared.input_artifacts import get_ready_blackbox_snapshot


RECLAIM_WAVES = {
    "W2": ("daily_5y_2_v28", "daily_7y_1_v28"),
    "W3A": ("liwei_0616_5y01_full_oos_k3_div_k10", "liwei_0616_cons_sda_k3_div_k10"),
}
_ROOT = Path(__file__).resolve().parents[1]
_IDENTITY_FIELDS = ("scheme_id", "scheme_version", "runtime_type", "code_hash", "config_hash", "manifest_hash")


def parse_reclaim_run_ids(wave: str, values: Sequence[str]) -> dict[str, str]:
    """只接受已批准完整批次的不同真实 Harness ID。"""
    if wave not in RECLAIM_WAVES:
        raise ValueError("writer reclaim supports only complete W2/W3A waves")
    parsed = {}
    for value in values:
        key, separator, run_id = value.partition("=")
        if (not separator or key not in RECLAIM_WAVES[wave] or key in parsed
                or not run_id or run_id != run_id.strip() or len(run_id) > 128):
            raise ValueError("writer reclaim requires distinct BASE=RUN evidence")
        parsed[key] = run_id
    if set(parsed) != set(RECLAIM_WAVES[wave]) or len(set(parsed.values())) != len(parsed):
        raise ValueError("writer reclaim requires complete distinct Harness evidence")
    return parsed


def _verified_pair(root: Path, reference: Path, wave: str):
    """绑定已安装旧实现、临时 Writer 和原 ID 的纯包装候选。"""
    if wave not in RECLAIM_WAVES:
        raise ValueError("writer reclaim supports only complete W2/W3A waves")
    installs = {"candidate": control._verified_install(root), "reference": control._verified_install(reference)}
    if root == reference:
        raise RuntimeError("writer reclaim requires distinct candidate/reference releases")
    matrix_path = root / "deploy/scheme_deployment_matrix_v1.json"
    matrix = json.loads(matrix_path.read_bytes())
    if matrix.get("schema_version") != "scheme-deployment-matrix-v1" or not isinstance(matrix.get("schemes"), dict):
        raise RuntimeError("invalid writer reclaim deployment matrix")
    old, sources, new, conversions = {}, {}, {}, {}
    for key in RECLAIM_WAVES[wave]:
        alias = key + "_bbv2"
        if "aliyun-gray" not in matrix["schemes"].get(key, []) or matrix["schemes"].get(alias) != []:
            raise RuntimeError("writer reclaim matrix must enable original and exclude temporary identity")
        old[key] = load_scheme_config(reference / "schemes" / key / "config.yaml")
        sources[key] = load_scheme_config(reference / "schemes" / alias / "config.yaml")
        new[key] = load_scheme_config(root / "schemes" / key / "config.yaml")
        retained = load_scheme_config(root / "schemes" / alias / "config.yaml")
        if (old[key].runtime_type != "native_adapter" or sources[key].runtime_type != "blackbox_v2"
                or new[key].runtime_type != "blackbox_v2"
                or sources[key].factor_input_mode != "algorithm_managed"
                or new[key].factor_input_mode != "algorithm_managed"
                or sources[key].incremental_state != new[key].incremental_state
                or any(getattr(sources[key], field) != getattr(retained, field) for field in _IDENTITY_FIELDS)):
            raise RuntimeError("writer reclaim reference/source/candidate identity mismatch")
        conversions[key] = verify_identity_only_delivery_change(
            project_root=root, source_script=sources[key].delivery_script.read_bytes(),
            source_metadata=sources[key].delivery_metadata.read_bytes(),
            candidate_script=new[key].delivery_script.read_bytes(),
            candidate_metadata=new[key].delivery_metadata.read_bytes(),
        )
    return old, sources, new, installs | {
        "identity_conversions": conversions, "deployment_matrix_sha256": control._sha256_file(matrix_path),
    }


def _assert_no_algorithm_process(wave: str) -> None:
    """围栏外仍有任何原/临时直接算法进程时拒绝接管。"""
    names = "|".join(key + "(_bbv2)?" for key in RECLAIM_WAVES[wave])
    pattern = (rf"(^|/)({names})\.py([[:space:]]|$)|schemes\.({names})\.predict"
               rf"|scheduler\.scheme_runner.*--scheme-id[ =]+({names})([[:space:]]|$)")
    result = subprocess.run(["/usr/bin/pgrep", "-f", pattern], check=False, capture_output=True, timeout=10)
    if result.returncode != 1:
        raise RuntimeError("writer reclaim algorithm process exists or inspection failed")
    # 旧 Harness compare 的 Python 入口不一定带 scheme_runner/predict 模块名。
    # 读取真实 Linux argv，避免把历史 running 审计行当作进程，也不漏掉手工调用。
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            continue
        if arguments and b"python" in Path(os.fsdecode(arguments[0])).name.encode():
            if any(key.encode() in value for key in RECLAIM_WAVES[wave] for value in arguments):
                raise RuntimeError("writer reclaim matching Python process is still running")


def _capture(project_root: Path, reference_project_root: Path, wave: str):
    """每次重新读取权威输入、安装树、围栏与状态，不接受外部成功 JSON。"""
    if wave not in RECLAIM_WAVES:
        raise ValueError("writer reclaim supports only complete W2/W3A waves")
    if wave == "W3A":
        raise RuntimeError("W3A state revision and rollback admission are not ready; no Writer change allowed")
    root, reference = project_root.resolve(strict=True), reference_project_root.resolve(strict=True)
    if root != _ROOT or os.environ.get("BFL_DEPLOYMENT_TARGET") != "aliyun-gray":
        raise RuntimeError("writer reclaim must execute from its ECS immutable candidate")
    control._assert_execution_modules(root)
    if root not in Path(__file__).resolve(strict=True).parents:
        raise RuntimeError("writer reclaim module differs from candidate release")
    old, sources, new, releases = _verified_pair(root, reference, wave)
    current = control._CURRENT_LINKS["aliyun-gray"]
    if not current.is_symlink() or current.resolve(strict=True) != root:
        raise RuntimeError("writer reclaim requires fenced candidate current")
    if os.environ.get("BFL_RELEASE_COMMIT") != releases["candidate"]["commit"]:
        raise RuntimeError("writer reclaim process commit differs from current")
    scheduler = control._capture_scheduler_state(project_root=root, deployment_target="aliyun-gray", cadence="daily")
    locale = control._capture_installed_locale(root)
    _assert_no_algorithm_process(wave)
    snapshot = get_ready_blackbox_snapshot(snapshot_date=date.today().isoformat(), require_fresh=False,
                                          factor_input_mode="algorithm_managed")
    manifest = json.loads(snapshot.manifest_path.read_bytes())
    if set(manifest.get("files", {})) != control._DATABRIDGE_FILES:
        raise RuntimeError("writer reclaim requires five-file ready generation")
    hashes = {name: control._sha256_file(snapshot.data_dir / name) for name in sorted(control._DATABRIDGE_FILES)}
    if any(manifest["files"][name].get("sha256") != digest for name, digest in hashes.items()):
        raise RuntimeError("writer reclaim input differs from ready manifest")
    fingerprint = load_environment_fingerprint(root, expected_runtime_profile="blackbox-v2-v1")
    new = {key: replace(cfg, environment_fingerprint=fingerprint, data_snapshot_id=snapshot.snapshot_id)
           for key, cfg in new.items()}
    stateful = {key: cfg for key, cfg in new.items() if cfg.incremental_state}
    return old, sources, new, {
        "schema_version": "same-id-writer-reclaim-control-plane-v1", "wave": wave,
        "deployment_target": "aliyun-gray", "release": releases, "current_release": str(root),
        "scheduler": scheduler, "execution_environment": locale,
        "blackbox_environment_fingerprint": fingerprint,
        "native_canonical_selection": {key: {field: getattr(cfg, field) for field in _IDENTITY_FIELDS}
                                       for key, cfg in old.items()},
        "source_canonical_selection": {key: {field: getattr(cfg, field) for field in _IDENTITY_FIELDS}
                                       for key, cfg in sources.items()},
        "databridge": {"generation_id": snapshot.generation_id, "data_snapshot_id": snapshot.snapshot_id,
                       "business_digest": snapshot.business_digest, "files": hashes},
        "candidate_states": control._read_candidate_states(root, stateful, snapshot) if stateful else {},
    }


def build_writer_reclaim_preflight(engine, *, project_root: Path, reference_project_root: Path,
                                   wave: str, harness_run_ids: Mapping[str, str], action: str,
                                   expected_database_name: str, expected_server_uuid: str) -> dict:
    """生成只读回收计划；缺少真实迁移 Gate 时保持失败。"""
    runs = parse_reclaim_run_ids(wave, [f"{key}={value}" for key, value in harness_run_ids.items()])
    old, sources, new, evidence = _capture(project_root, reference_project_root, wave)
    plan = repository.read_same_id_writer_reclaim_plan(
        engine, wave=wave, old_configs=old, source_configs=sources, new_configs=new,
        harness_run_ids=runs, action=action, control_plane_evidence=evidence,
        expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid,
    )
    return {"plan": plan, "plan_sha256": repository.native_successor_plan_sha256(plan)}


def execute_writer_reclaim(engine, *, project_root: Path, reference_project_root: Path,
                           wave: str, harness_run_ids: Mapping[str, str], action: str,
                           expected_plan_sha256: str, approved_by: str,
                           expected_database_name: str, expected_server_uuid: str) -> dict:
    """仅经仓储原子回收/恢复 Writer；不动文件 current、历史事实或服务。"""
    runs = parse_reclaim_run_ids(wave, [f"{key}={value}" for key, value in harness_run_ids.items()])
    old, sources, new, _evidence = _capture(project_root, reference_project_root, wave)
    return repository.apply_same_id_writer_reclaim(
        engine, wave=wave, old_configs=old, source_configs=sources, new_configs=new,
        harness_run_ids=runs, action=action, expected_plan_sha256=expected_plan_sha256,
        approved_by=approved_by, approved_at=datetime.now(timezone.utc),
        expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid,
        control_plane_evidence_reader=lambda: _capture(project_root, reference_project_root, wave)[3],
    )
