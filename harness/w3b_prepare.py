"""临时 W3B 原 ID 准备入口：复用既有证据，只写状态与真实 Harness 回执。"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import text

from harness import same_id_runtime_upgrade as control
from harness.context import GateContext
from harness.native_successor_migration import (
    _DATABRIDGE_FILES, _REVIEWED_W3B_EVIDENCE, _W3B_SOURCE,
    _json_sha256, _load_reviewed_w3b_comparison, _sha256_file,
    load_native_successor_waves,
)
from harness.persistence import (
    new_harness_run_id, persist_harness_run_start, persist_harness_run_complete,
)
from harness.result import Evidence, GateResult, GateStatus
from harness.runtime_upgrade_state import admit_reviewed_w3b_state
from harness.w3b_state_source import load_reviewed_w3b_state_source
from scheduler.discovery import load_scheme_config
from scheduler.process_control import ProcessGroupTerminationError
from scheduler.repository import (
    _blackbox_activation_advisory_lock, _same_id_fact_snapshot_conn,
    _same_id_rows_conn, native_successor_plan_sha256,
)
from shared.blackbox_v2.environment_manifest import load_environment_fingerprint
from shared.blackbox_v2.snapshot import compose_blackbox_input_bundle
from shared.input_artifacts import get_ready_blackbox_snapshot, open_blackbox_runtime_view


_ROOT = Path(__file__).resolve().parents[1]
_IDENTITY_FIELDS = ("scheme_version", "runtime_type", "code_hash", "config_hash", "manifest_hash")
_STAGE = "native-runtime-upgrade"


def _database_snapshot(engine, *, expected_database_name: str, expected_server_uuid: str,
                       require_idle: bool) -> dict:
    """准备阶段的只读保护快照；Harness 新凭据不属于需保持不变的业务事实。"""
    if engine.dialect.name != "mysql" or not expected_database_name or not expected_server_uuid:
        raise ValueError("W3B prepare requires explicit MySQL database identity")
    ids = sorted((*control.W3B_IDS, *(key + "_bbv2" for key in control.W3B_IDS)))
    params = {f"id_{index}": key for index, key in enumerate(ids)}
    scope = "IN (" + ",".join(":" + key for key in params) + ")"
    with engine.connect() as conn:
        conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        try:
            identity = dict(conn.execute(text("SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid")).mappings().one())
            if identity != {"database_name": expected_database_name, "server_uuid": expected_server_uuid}:
                raise RuntimeError("W3B prepare database identity mismatch")
            migrations = _same_id_rows_conn(conn, "t_schema_migrations", "1=1", {}, order="version", for_update=False)
            if not migrations or max(int(row["version"]) for row in migrations) != 24 or any(row["state"] != "APPLIED" for row in migrations):
                raise RuntimeError("W3B prepare requires applied schema 024")
            for table in ("t_scheme_runs", "t_backtest_runs", "t_harness_runs"):
                if table == "t_harness_runs" and not require_idle:
                    continue
                if conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE scheme_id {scope} AND status='running'"), params).scalar_one():
                    raise RuntimeError("W3B prepare requires no in-flight scheme/backtest/Harness work")
            facts = _same_id_fact_snapshot_conn(conn, ids, for_update=False)
            for table in ("t_harness_runs", "t_harness_gate_results"):
                facts.pop(table)
            return {
                "database_identity_sha256": _json_sha256(identity),
                "schema_sha256": native_successor_plan_sha256({"rows": migrations}),
                "facts": facts,
                "registry_sha256": native_successor_plan_sha256({"rows": _same_id_rows_conn(
                    conn, "t_scheme_registry", "1=1", {}, order="scheme_id", for_update=False)}),
                "versions_sha256": native_successor_plan_sha256({"rows": _same_id_rows_conn(
                    conn, "t_scheme_versions", f"scheme_id {scope}", params,
                    order="scheme_id, scheme_version", for_update=False)}),
            }
        finally:
            conn.rollback()


def _ready_input():
    snapshot = get_ready_blackbox_snapshot(snapshot_date=date.today().isoformat(), require_fresh=False,
                                          factor_input_mode="algorithm_managed")
    manifest = json.loads(snapshot.manifest_path.read_bytes())
    if set(manifest.get("files", {})) != _DATABRIDGE_FILES:
        raise RuntimeError("W3B prepare requires a ready five-file generation")
    hashes = {name: _sha256_file(snapshot.data_dir / name) for name in sorted(_DATABRIDGE_FILES)}
    if any(manifest["files"][name].get("sha256") != digest for name, digest in hashes.items()):
        raise RuntimeError("W3B prepare DataBridge file differs from manifest")
    return snapshot, {"generation_id": snapshot.generation_id, "data_snapshot_id": snapshot.snapshot_id,
                      "files": hashes, "manifest_sha256": _sha256_file(snapshot.manifest_path)}


def _verify_current_native(current: Path, candidate: Path, reference: Path, old: dict) -> dict:
    """当前执行实现必须仍为已验收的 Native；reference 不能替代现场身份。"""
    if current == candidate:
        raise RuntimeError("W3B prepare requires the old Native current, not the candidate")
    closure = {}
    for key in control.W3B_IDS:
        cfg = load_scheme_config(current / "schemes" / key / "config.yaml")
        if cfg.runtime_type != "native_adapter" or any(
            getattr(cfg, field) != getattr(old[key], field) for field in _IDENTITY_FIELDS
        ):
            raise RuntimeError("W3B current Native identity differs from reviewed reference")
        closure[key] = {}
        for name in ("inference.py", "core/__init__.py", "core/data_alignment.py", "core/v31_common.py"):
            digest = _sha256_file(current / "schemes" / key / name)
            if digest != _sha256_file(reference / "schemes" / key / name):
                raise RuntimeError("W3B current Native source differs from reviewed reference")
            closure[key][name] = digest
    return closure


def _capture_inputs(engine, *, project_root: Path, reference_project_root: Path,
                    expected_database_name: str, expected_server_uuid: str):
    """复验真正来源；不要求 candidate 已成为 current，也不停止 timer。"""
    root, reference = project_root.resolve(strict=True), reference_project_root.resolve(strict=True)
    if root != _ROOT or os.environ.get("BFL_DEPLOYMENT_TARGET") != "aliyun-gray":
        raise RuntimeError("W3B prepare must execute from its ECS candidate release")
    control._assert_execution_modules(root)
    old, new, releases = control._verified_pair(root, reference)
    if os.environ.get("BFL_RELEASE_COMMIT") != releases["candidate"]["commit"]:
        raise RuntimeError("W3B prepare candidate commit differs from process environment")
    current_link = control._CURRENT_LINKS["aliyun-gray"]
    if not current_link.is_symlink():
        raise RuntimeError("W3B prepare requires an installed current symlink")
    current = current_link.resolve(strict=True)
    installed = control._verified_install(current)
    current_native = _verify_current_native(current, root, reference, old)
    locale = control._capture_installed_locale(root, installed_current_root=current)
    control._assert_no_algorithm_process()
    control._assert_no_temporary_writer(engine)
    database = _database_snapshot(engine, expected_database_name=expected_database_name,
                                  expected_server_uuid=expected_server_uuid, require_idle=True)
    snapshot, inputs = _ready_input()
    fingerprint = load_environment_fingerprint(root, expected_runtime_profile="blackbox-v2-v1")
    new = {key: replace(cfg, environment_fingerprint=fingerprint, data_snapshot_id=snapshot.snapshot_id)
           for key, cfg in new.items()}
    wave = load_native_successor_waves(root / "deploy/native_to_blackbox_migration_v1.json")["W3B"]
    sources, equivalence = {}, {}
    for target in wave.targets:
        key = target.old_base_scheme_id
        cfg = load_scheme_config(reference / "schemes" / target.new_base_scheme_id / "config.yaml")
        source = load_reviewed_w3b_state_source(cfg)
        if (source["input"]["generation_id"] != inputs["generation_id"]
                or source["input"]["snapshot_id"] != inputs["data_snapshot_id"]
                or source["input"]["files"] != inputs["files"]):
            raise RuntimeError("W3B prepare source and ready input differ; do not relabel evidence or reinitialize")
        approved = _REVIEWED_W3B_EVIDENCE[key]
        requests, _native, results, lineage = _load_reviewed_w3b_comparison(
            project_root=reference, target=target, new_config=cfg,
            evidence_dir=Path(approved["execution_dir"]), data_dir=_W3B_SOURCE / "data",
        )
        equivalence[key] = {"report_sha256": approved["report_sha256"], "input": lineage,
                            "request_sha256": _json_sha256(requests), "result_sha256": _json_sha256(results),
                            "identity_conversion": releases["identity_conversions"][key], "algorithm_executions": 0}
        sources[key] = (cfg, source)
        destination = Path(os.environ["BFL_RUNTIME_ROOT"]) / "blackbox-state" / key / f"{new[key].scheme_version}.state"
        if os.path.lexists(destination):
            raise RuntimeError("W3B candidate state already exists; inspect prior preparation, never repeat it")
    source_states = control._read_candidate_states(root, {cfg.scheme_id: cfg for cfg, _ in sources.values()}, snapshot)
    if any(source_states[cfg.scheme_id]["envelope_sha256"] != source["source_envelope_sha256"]
           for cfg, source in sources.values()):
        raise RuntimeError("W3B source state changed during read-only preparation")
    plan = {"schema_version": "w3b-same-id-prepare-v1", "wave": "W3B",
            "releases": releases, "current": {"path": str(current), "install": installed,
                                               "native_source_closure": current_native},
            "locale": locale, "database": database, "input": inputs,
            "environment_fingerprint": fingerprint, "equivalence": equivalence,
            "source_states": source_states,
            "sources": {key: source["source_evidence"] for key, (_, source) in sources.items()},
            "candidate_versions": {key: cfg.scheme_version for key, cfg in new.items()}}
    return old, new, snapshot, sources, plan


def build_w3b_prepare_preflight(engine, **kwargs) -> dict:
    """仅返回待批准准备计划；不执行算法、不创建 Harness run 或状态。"""
    plan = _capture_inputs(engine, **kwargs)[-1]
    return {"plan": plan, "plan_sha256": _json_sha256(plan)}


def _write_receipt(path: Path, value: dict) -> str:
    payload = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()
    with path.open("xb") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())
    return hashlib.sha256(payload).hexdigest()


def _gate_proof(old, new, plan: dict, key: str, local_execution_sha256: str) -> dict:
    return {"schema_version": "same-id-runtime-upgrade-evidence-v1", "scheme_id": key,
            "old_identity": {field: getattr(old, field) for field in _IDENTITY_FIELDS},
            "new_identity": {field: getattr(new, field) for field in _IDENTITY_FIELDS},
            "environment_fingerprint": new.environment_fingerprint,
            "data_snapshot_id": new.data_snapshot_id, "generation_id": plan["input"]["generation_id"],
            "equivalence_sha256": _json_sha256(plan["equivalence"][key]),
            "local_execution_sha256": local_execution_sha256}


def execute_w3b_prepare(engine, *, project_root: Path, reference_project_root: Path,
                        expected_database_name: str, expected_server_uuid: str,
                        expected_plan_sha256: str, approved_by: str, work_dir: Path) -> dict:
    """锁内重验计划，逐方案一次短调用并记录真实 Gate；不激活或写业务事实。

    若任一步失败，保留已生成的状态与回执并停止；不删除产物或自动重跑。
    状态发布后 Harness 写入失败时必须先核验留下的 admission 原件，再处理
    回执恢复；不能把失败返回等同于没有发布状态。
    """
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError("W3B prepare requires explicit approved_by")
    if not isinstance(expected_plan_sha256, str) or len(expected_plan_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_plan_sha256):
        raise ValueError("W3B prepare requires approved lowercase plan SHA-256")
    work_dir = Path(work_dir)
    if not work_dir.is_absolute() or work_dir != work_dir.resolve(strict=False) or os.path.lexists(work_dir):
        raise ValueError("W3B prepare requires a fresh absolute symlink-free work directory")
    kwargs = dict(project_root=project_root, reference_project_root=reference_project_root,
                  expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid)
    with ExitStack() as locks:
        for key in sorted((*control.W3B_IDS, *(key + "_bbv2" for key in control.W3B_IDS))):
            locks.enter_context(_blackbox_activation_advisory_lock(engine, scheme_id=key))
        old, new, snapshot, sources, plan = _capture_inputs(engine, **kwargs)
        if _json_sha256(plan) != expected_plan_sha256:
            raise RuntimeError("W3B prepare plan changed; obtain a fresh read-only preflight")
        work_dir.mkdir(mode=0o700)
        _write_receipt(work_dir / "plan.json", plan | {"approved_by": approved_by.strip()})
        runs = {}
        bundle = compose_blackbox_input_bundle(snapshot, factor_input_mode="algorithm_managed")
        with open_blackbox_runtime_view(bundle) as view:
            try:
                for key in control.W3B_IDS:
                    cfg, source = sources[key]
                    original_request = source["request"]
                    request_id = key + ":" + ":".join((original_request.predict_date, original_request.feature_date, original_request.target_date))
                    request = replace(original_request, request_id=request_id)
                    expected_result = replace(source["result"], request_id=request_id)
                    run_id = new_harness_run_id()
                    started = datetime.now(timezone.utc).isoformat()
                    ctx = GateContext(key, request.predict_date, project_root, config=new[key], engine_factory=lambda: engine)
                    _write_receipt(work_dir / f"{key}.started.json", {"harness_run_id": run_id, "started_at": started,
                                   "plan_sha256": expected_plan_sha256, "request": asdict(request)})
                    if not persist_harness_run_start(ctx, harness_run_id=run_id, stage=_STAGE, started_at=started):
                        raise RuntimeError("W3B prepare Harness start failed; no algorithm was started for this scheme")
                    try:
                        admission = admit_reviewed_w3b_state(
                            project_root=project_root, source_config=cfg, candidate_config=new[key],
                            data_dir=view.data_dir, data_snapshot_id=view.bundle.combined_snapshot_id,
                            generation_id=snapshot.generation_id, request=request, expected_result=expected_result,
                            expected_source_envelope_sha256=source["source_envelope_sha256"],
                            expected_algorithm_identity=source["expected_algorithm_identity"],
                            work_dir=work_dir / key, approved_by=approved_by,
                        )
                        local_sha = _write_receipt(work_dir / f"{key}.admission.json", admission)
                        if (_ready_input()[1] != plan["input"]
                                or control._CURRENT_LINKS["aliyun-gray"].resolve(strict=True) != Path(plan["current"]["path"])
                                or _database_snapshot(engine, expected_database_name=expected_database_name,
                                    expected_server_uuid=expected_server_uuid, require_idle=False) != plan["database"]):
                            raise RuntimeError("W3B prepare input/current/database changed; preserve state and stop")
                        proof = _gate_proof(old[key], new[key], plan, key, local_sha)
                        gate = GateResult(_STAGE, GateStatus.PASSED, [Evidence("runtime_upgrade", proof)], [],
                                          started, datetime.now(timezone.utc).isoformat())
                    except BaseException as error:
                        if isinstance(error, ProcessGroupTerminationError):
                            view.mark_termination_uncertain()
                        failed = GateResult(_STAGE, GateStatus.FAILED, [], ["preparation failed; inspect retained local artifacts"],
                                            started, datetime.now(timezone.utc).isoformat())
                        try:
                            persisted = persist_harness_run_complete(ctx, harness_run_id=run_id, status="failed",
                                                                     finished_at=failed.finished_at, results=[failed])
                            if not persisted:
                                error.add_note("Failed Harness receipt was not persisted; inspect retained artifacts.")
                        except BaseException:
                            error.add_note("Failed Harness receipt persistence raised; original execution error preserved.")
                        raise
                    if not persist_harness_run_complete(ctx, harness_run_id=run_id, status="passed",
                                                        finished_at=gate.finished_at, results=[gate]):
                        raise RuntimeError("state published but Harness completion failed; preserve receipt, do not reinitialize")
                    runs[key] = run_id
            except ProcessGroupTerminationError:
                view.mark_termination_uncertain()
                raise
        result = {"harness_run_ids": runs, "plan_sha256": expected_plan_sha256,
                  "algorithm_executions": len(runs), "prediction_written": False, "registry_changed": False}
        _write_receipt(work_dir / "complete.json", result)
        return result
