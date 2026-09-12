"""W1A 两个原 base / 六目标的封闭回收控制；历史事实始终只读。"""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from harness import same_id_runtime_upgrade as control
from harness.runtime_upgrade_evidence import verify_identity_only_delivery_change
from harness.w3b_prepare import _ready_input
from scheduler import repository
from scheduler.blackbox_state import read_regular_bytes
from scheduler.discovery import load_scheme_config
from shared.blackbox_v2.environment_manifest import load_environment_fingerprint
from shared.scheme_config_schema import MULTI_TARGET_DELIVERIES


ROOT = Path(__file__).resolve().parents[1]
IDS = tuple(MULTI_TARGET_DELIVERIES)
ALIASES = tuple(alias for targets in MULTI_TARGET_DELIVERIES.values() for alias in targets.values())
ALL_IDS = tuple(sorted((*IDS, *ALIASES)))
NATIVE_REFERENCE = "025f153e20865137df22ea1b464a5ef02d123787"
ROLLBACK_RELEASE = "0ba331f76374f27a7434f2d2732a743f7b3c66b1"
REPORT_SHA = "5d88340495f103adba2d112171dbab7ecff85df6a01c1edfffb6d25de9c0909e"
FIELDS = ("scheme_id", "scheme_version", "runtime_type", "code_hash", "config_hash", "manifest_hash")


def parse_w1a_run_ids(values):
    """只允许两个不同的原 base Harness ID。"""
    result = {}
    for value in values:
        key, separator, run = value.partition("=")
        if not separator or key not in IDS or key in result or not run or run != run.strip() or len(run) > 128:
            raise ValueError("W1A requires exactly two distinct BASE=RUN entries")
        result[key] = run
    if set(result) != set(IDS) or len(set(result.values())) != 2:
        raise ValueError("W1A requires exactly two distinct BASE=RUN entries")
    return result


def _identity(cfg):
    return {field: getattr(cfg, field) for field in FIELDS}


def _verified_pair(root, reference, rollback):
    installs = {name: control._verified_install(path) for name, path in (
        ("candidate", root), ("reference", reference), ("rollback", rollback))}
    if (len({root, reference, rollback}) != 3 or installs["reference"]["commit"] != NATIVE_REFERENCE
            or installs["rollback"]["commit"] != ROLLBACK_RELEASE):
        raise RuntimeError("W1A requires distinct candidate, fixed Native reference and actual rollback releases")
    matrix_path = root / "deploy/scheme_deployment_matrix_v1.json"
    matrix = json.loads(matrix_path.read_bytes())["schemes"]
    prior = json.loads((rollback / "deploy/scheme_deployment_matrix_v1.json").read_bytes())["schemes"]
    if (any(matrix.get(key) != ["mac3-production", "aliyun-gray"] for key in IDS)
            or any(matrix.get(key) != [] for key in ALIASES)
            or {key: value for key, value in matrix.items() if key not in ALL_IDS}
            != {key: value for key, value in prior.items() if key not in ALL_IDS}):
        raise RuntimeError("W1A matrix must change only the eight approved identities")
    old = {key: load_scheme_config(reference / "schemes" / key / "config.yaml") for key in IDS}
    new = {key: load_scheme_config(root / "schemes" / key / "config.yaml") for key in IDS}
    sources = {key: load_scheme_config(rollback / "schemes" / key / "config.yaml") for key in ALIASES}
    conversions = {}
    for key in IDS:
        cfg = new[key]
        if (old[key].runtime_type != "native_adapter" or cfg.runtime_type != "blackbox_v2"
                or cfg.incremental_state or cfg.factor_input_mode != "algorithm_managed"
                or cfg.tenors != list(MULTI_TARGET_DELIVERIES[key])):
            raise RuntimeError("W1A canonical base/target contract differs")
        conversions[key] = {}
        for delivery in cfg.blackbox_deliveries:
            alias = MULTI_TARGET_DELIVERIES[key][delivery.metadata.target_tenor]
            source = sources[alias]
            if (source.incremental_state or source.factor_input_mode != "algorithm_managed"
                    or _identity(source) != _identity(load_scheme_config(reference / "schemes" / alias / "config.yaml"))
                    or _identity(source) != _identity(load_scheme_config(root / "schemes" / alias / "config.yaml"))):
                raise RuntimeError("W1A source exact differs between reference, rollback and candidate")
            conversions[key][alias] = verify_identity_only_delivery_change(
                project_root=root, source_script=source.delivery_script.read_bytes(),
                source_metadata=source.delivery_metadata.read_bytes(),
                candidate_script=delivery.script_path.read_bytes(), candidate_metadata=delivery.metadata_path.read_bytes())
    # 不回退已完成批次，也不夹带其它方案版本变化。
    for path in sorted((rollback / "schemes").glob("*/config.yaml")):
        if path.parent.name not in ALL_IDS and not path.parent.name.startswith("_"):
            if _identity(load_scheme_config(path)) != _identity(load_scheme_config(root / path.relative_to(rollback))):
                raise RuntimeError("W1A must preserve every unrelated canonical exact")
    return old, sources, new, installs | {
        "paths": {"candidate": str(root), "reference": str(reference), "rollback": str(rollback)},
        "identity_conversions": conversions, "deployment_matrix_sha256": control._sha256_file(matrix_path)}


def _equivalence(root, old, sources, new, conversions):
    report_path = root / "deploy/native_successor_equivalence/W1A.json"
    payload = read_regular_bytes(report_path, 1024 * 1024)
    if hashlib.sha256(payload).hexdigest() != REPORT_SHA:
        raise RuntimeError("W1A reviewed equivalence report changed")
    report = json.loads(payload)
    targets = report.get("targets", [])
    if report.get("wave") != "W1A" or len(targets) != 6 or {row.get("new_base_scheme_id") for row in targets} != set(ALIASES):
        raise RuntimeError("W1A equivalence requires all six approved targets")
    result = {}
    for key in IDS:
        comparisons = []
        for tenor, alias in MULTI_TARGET_DELIVERIES[key].items():
            item = next(row for row in targets if row["new_base_scheme_id"] == alias)
            source = sources[alias]
            if (item.get("old_base_scheme_id") != key or item.get("target_tenor") != tenor
                    or item.get("request_count") != (337 if key == "t1_daily" else 333)
                    or item.get("task_type") != old[key].task_type
                    or item.get("old_horizon") != old[key].horizon or item.get("new_horizon") != new[key].horizon
                    or item.get("old_code_hash") != old[key].code_hash or item.get("new_code_hash") != source.code_hash
                    or item.get("native_result_sha256") != item.get("successor_result_sha256")
                    or any(item.get(field) != 0 for field in ("request_id_mismatch_count", "predict_date_mismatch_count",
                        "feature_date_mismatch_count", "target_date_mismatch_count", "direction_mismatch_count"))):
                raise RuntimeError("W1A equivalence differs from original algorithms or standard results")
            comparisons.append(item)
        result[key] = {"report_sha256": REPORT_SHA, "comparisons": comparisons,
                       "input_identity": {field: report[field] for field in (
                           "data_snapshot_id", "generation_id", "data_files_sha256")},
                       "producer": report["producer"], "runtime_environment_fingerprint": report["runtime_environment_fingerprint"],
                       "identity_conversions": conversions[key], "algorithm_executions": 0}
    return result


def _assert_no_algorithm_process():
    """只读 Linux argv；不遗漏按旧 basename 选择目标的交付进程。"""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            continue
        if args and b"python" in Path(os.fsdecode(args[0])).name.encode():
            if any(key.encode() in arg for key in ALL_IDS for arg in args):
                raise RuntimeError("W1A matching algorithm process exists")


def capture(project_root, reference_project_root, rollback_project_root, *, preparing=False, work_dir=None):
    """重新读取真实 release、daily 围栏、五文件和环境，绝不切 current。"""
    if rollback_project_root is None:
        raise ValueError("W1A requires explicit rollback-project-root")
    root, reference, rollback = (Path(path).resolve(strict=True) for path in
                                 (project_root, reference_project_root, rollback_project_root))
    if root != ROOT or os.environ.get("BFL_DEPLOYMENT_TARGET") != "aliyun-gray":
        raise RuntimeError("W1A must execute from its ECS immutable candidate")
    control._assert_execution_modules(root)
    from harness import w1a_reclaim_prepare
    from scheduler import executor

    if any(root not in Path(module.__file__).resolve(strict=True).parents for module in (w1a_reclaim_prepare, executor)):
        raise RuntimeError("W1A execution modules differ from candidate")
    old, sources, new, releases = _verified_pair(root, reference, rollback)
    if os.environ.get("BFL_RELEASE_COMMIT") != releases["candidate"]["commit"]:
        raise RuntimeError("W1A caller release differs")
    current = rollback if preparing else root
    link = control._CURRENT_LINKS["aliyun-gray"]
    if not link.is_symlink() or link.resolve(strict=True) != current:
        raise RuntimeError("W1A current differs from the expected preparation/cutover release")
    scheduler = control._capture_scheduler_state(project_root=current, deployment_target="aliyun-gray", cadence="daily")
    locale = control._capture_installed_locale(root, installed_current_root=current)
    _assert_no_algorithm_process()
    snapshot, inputs = _ready_input()
    fingerprint = load_environment_fingerprint(root, expected_runtime_profile="blackbox-v2-v1")
    new = {key: replace(cfg, environment_fingerprint=fingerprint, data_snapshot_id=snapshot.snapshot_id) for key, cfg in new.items()}
    equivalence = _equivalence(root, old, sources, new, releases["identity_conversions"])
    evidence = {"schema_version": "w1a-writer-reclaim-control-v1", "wave": "W1A", "deployment_target": "aliyun-gray",
                "release": releases, "current_release": str(current), "scheduler": scheduler,
                "execution_environment": locale, "blackbox_environment_fingerprint": fingerprint,
                "native_canonical_selection": {key: _identity(cfg) for key, cfg in old.items()},
                "source_canonical_selection": {key: _identity(cfg) for key, cfg in sources.items()},
                "candidate_canonical_selection": {key: _identity(cfg) for key, cfg in new.items()},
                "databridge": inputs, "equivalence": equivalence}
    if not preparing:
        evidence["readiness"] = read_readiness(work_dir, new, evidence)
    return old, sources, new, snapshot, evidence


def read_readiness(work_dir, new, control_evidence):
    """绑定本机准备原件与两个真实 Gate，六目标均通过才允许接管。"""
    if work_dir is None:
        raise ValueError("W1A cutover/rollback requires completed preparation work-dir")
    root = Path(work_dir).resolve(strict=True)
    complete = json.loads(read_regular_bytes(root / "complete.json", 1024 * 1024))
    plan = json.loads(read_regular_bytes(root / "plan.json", 4 * 1024 * 1024))
    if (complete.get("plan_sha256") != repository.native_successor_plan_sha256(plan)
            or complete.get("algorithm_executions") != 6 or complete.get("prediction_written") is not False
            or complete.get("state_written") is not False
            or plan["control"]["databridge"] != control_evidence["databridge"]
            or plan["control"]["equivalence"] != control_evidence["equivalence"]
            or plan["control"]["release"] != control_evidence["release"]
            or plan["control"]["blackbox_environment_fingerprint"] != control_evidence["blackbox_environment_fingerprint"]):
        raise RuntimeError("W1A readiness input/release/equivalence differs")
    runs = parse_w1a_run_ids([f"{key}={value}" for key, value in complete["harness_run_ids"].items()])
    hashes = {}
    for key in IDS:
        payload = read_regular_bytes(root / f"{key}.execution.json", 1024 * 1024)
        item = json.loads(payload)
        hashes[key] = hashlib.sha256(payload).hexdigest()
        if (hashes[key] != complete["local_execution_sha256"][key]
                or item["scheme_version"] != new[key].scheme_version
                or item["algorithm_executions"] != len(new[key].tenors)
                or {row["target_tenor"] for row in item["records"]} != set(new[key].tenors)
                or len(item["records"]) != len(new[key].tenors)):
            raise RuntimeError("W1A execution receipt is incomplete")
    return {"harness_run_ids": runs, "local_execution_sha256": hashes,
            "equivalence_sha256": {key: repository.native_successor_plan_sha256(control_evidence["equivalence"][key]) for key in IDS},
            "prepare_database_identity_sha256": plan["database_identity_sha256"]}


def build_w1a_reclaim_preflight(engine, *, project_root, reference_project_root, rollback_project_root,
                               harness_run_ids, action, expected_database_name, expected_server_uuid, work_dir):
    old, sources, new, _, evidence = capture(project_root, reference_project_root, rollback_project_root, work_dir=work_dir)
    plan = repository.read_w1a_writer_reclaim_plan(engine, old_configs=old, source_configs=sources, new_configs=new,
        harness_run_ids=harness_run_ids, action=action, control_plane_evidence=evidence,
        expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid)
    return {"plan": plan, "plan_sha256": repository.native_successor_plan_sha256(plan)}


def execute_w1a_reclaim(engine, *, project_root, reference_project_root, rollback_project_root, harness_run_ids,
                         action, expected_database_name, expected_server_uuid, work_dir,
                         expected_plan_sha256, approved_by):
    reader = lambda: capture(project_root, reference_project_root, rollback_project_root, work_dir=work_dir)
    old, sources, new, _, _evidence = reader()
    return repository.apply_w1a_writer_reclaim(engine, old_configs=old, source_configs=sources, new_configs=new,
        harness_run_ids=harness_run_ids, action=action, expected_plan_sha256=expected_plan_sha256,
        approved_by=approved_by, approved_at=datetime.now(timezone.utc),
        expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid,
        control_plane_evidence_reader=lambda: reader()[-1])
