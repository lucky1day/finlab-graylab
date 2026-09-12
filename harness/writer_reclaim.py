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
W3A_NATIVE_REFERENCE = "3a805922667de41340942edce9941c7f902bb8fb"
W3A_ROLLBACK_RELEASE = "ee92b2d62a9e04faa59e2dc1f5e16a55ce9bdfa8"


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
    if wave == "W3A" and installs["reference"]["commit"] != W3A_NATIVE_REFERENCE:
        raise RuntimeError("W3A Native reference must be the reviewed pre-conversion release")
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
        verifier = verify_identity_only_delivery_change
        if wave == "W3A" and key == RECLAIM_WAVES["W3A"][0]:
            from harness.w3a_revision_evidence import verify_reviewed_w3a_delivery_change
            verifier = verify_reviewed_w3a_delivery_change
        conversions[key] = verifier(
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


def _verified_w3a_rollback(root: Path, rollback_project_root: Path | None, sources):
    """回滚绑定实际接管前 release，绝不把 Native 源码参考当作回滚代码。"""
    if rollback_project_root is None:
        raise ValueError("W3A requires an explicit rollback-project-root")
    rollback = rollback_project_root.resolve(strict=True)
    installed = control._verified_install(rollback)
    if rollback == root or installed["commit"] != W3A_ROLLBACK_RELEASE:
        raise RuntimeError("W3A rollback release is not the reviewed pre-cutover current")
    for cfg in sources.values():
        retained = load_scheme_config(rollback / "schemes" / cfg.scheme_id / "config.yaml")
        if any(getattr(cfg, field) != getattr(retained, field) for field in _IDENTITY_FIELDS):
            raise RuntimeError("W3A rollback source exact differs")
    candidate_matrix = json.loads((root / "deploy/scheme_deployment_matrix_v1.json").read_bytes())["schemes"]
    rollback_matrix = json.loads((rollback / "deploy/scheme_deployment_matrix_v1.json").read_bytes())["schemes"]
    scope = set(RECLAIM_WAVES["W3A"]) | {key + "_bbv2" for key in RECLAIM_WAVES["W3A"]}
    if ({key: value for key, value in candidate_matrix.items() if key not in scope}
            != {key: value for key, value in rollback_matrix.items() if key not in scope}):
        raise RuntimeError("W3A must preserve every unrelated deployment scope")
    for key in (*RECLAIM_WAVES["W2"], *control.W3B_IDS):
        before = load_scheme_config(rollback / "schemes" / key / "config.yaml")
        after = load_scheme_config(root / "schemes" / key / "config.yaml")
        if any(getattr(before, field) != getattr(after, field) for field in _IDENTITY_FIELDS):
            raise RuntimeError("W3A must preserve completed W2/W3B exact identities")
    return {"path": str(rollback), "install": installed}


def _capture(project_root: Path, reference_project_root: Path, wave: str, *,
             rollback_project_root: Path | None = None, work_dir: Path | None = None):
    """每次重新读取权威输入、安装树、围栏与状态，不接受外部成功 JSON。"""
    if wave not in RECLAIM_WAVES:
        raise ValueError("writer reclaim supports only complete W2/W3A waves")
    root, reference = project_root.resolve(strict=True), reference_project_root.resolve(strict=True)
    if root != _ROOT or os.environ.get("BFL_DEPLOYMENT_TARGET") != "aliyun-gray":
        raise RuntimeError("writer reclaim must execute from its ECS immutable candidate")
    control._assert_execution_modules(root)
    if root not in Path(__file__).resolve(strict=True).parents:
        raise RuntimeError("writer reclaim module differs from candidate release")
    old, sources, new, releases = _verified_pair(root, reference, wave)
    if wave == "W3A":
        releases["rollback"] = _verified_w3a_rollback(root, rollback_project_root, sources)
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
    readiness = {}
    if wave == "W3A":
        from harness.w3a_reclaim_prepare import read_w3a_readiness
        readiness = read_w3a_readiness(root, sources=sources, new=new, snapshot=snapshot,
                                       work_dir=work_dir, releases=releases)
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
        **({"w3a_readiness": readiness} if wave == "W3A" else {}),
    }


def build_writer_reclaim_preflight(engine, *, project_root: Path, reference_project_root: Path,
                                   wave: str, harness_run_ids: Mapping[str, str], action: str,
                                   expected_database_name: str, expected_server_uuid: str,
                                   rollback_project_root: Path | None = None, work_dir: Path | None = None) -> dict:
    """生成只读回收计划；缺少真实迁移 Gate 时保持失败。"""
    runs = parse_reclaim_run_ids(wave, [f"{key}={value}" for key, value in harness_run_ids.items()])
    extra = {"rollback_project_root": rollback_project_root, "work_dir": work_dir} if wave == "W3A" else {}
    old, sources, new, evidence = _capture(project_root, reference_project_root, wave, **extra)
    plan = repository.read_same_id_writer_reclaim_plan(
        engine, wave=wave, old_configs=old, source_configs=sources, new_configs=new,
        harness_run_ids=runs, action=action, control_plane_evidence=evidence,
        expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid,
    )
    return {"plan": plan, "plan_sha256": repository.native_successor_plan_sha256(plan)}


def execute_writer_reclaim(engine, *, project_root: Path, reference_project_root: Path,
                           wave: str, harness_run_ids: Mapping[str, str], action: str,
                           expected_plan_sha256: str, approved_by: str,
                           expected_database_name: str, expected_server_uuid: str,
                           rollback_project_root: Path | None = None, work_dir: Path | None = None) -> dict:
    """仅经仓储原子回收/恢复 Writer；不动文件 current、历史事实或服务。"""
    runs = parse_reclaim_run_ids(wave, [f"{key}={value}" for key, value in harness_run_ids.items()])
    extra = {"rollback_project_root": rollback_project_root, "work_dir": work_dir} if wave == "W3A" else {}
    old, sources, new, _evidence = _capture(project_root, reference_project_root, wave, **extra)
    return repository.apply_same_id_writer_reclaim(
        engine, wave=wave, old_configs=old, source_configs=sources, new_configs=new,
        harness_run_ids=runs, action=action, expected_plan_sha256=expected_plan_sha256,
        approved_by=approved_by, approved_at=datetime.now(timezone.utc),
        expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid,
        control_plane_evidence_reader=lambda: _capture(project_root, reference_project_root, wave, **extra)[3],
    )
