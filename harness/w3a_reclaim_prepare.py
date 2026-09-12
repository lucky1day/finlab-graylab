"""临时 W3A 四锁准备：Full 一次零训练 warm，SDA 一次标准调用；不切 Writer。"""

from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from sqlalchemy import text

from harness import same_id_runtime_upgrade as control, writer_reclaim
from harness.blackbox_v2.gates import validate_canonical_blackbox_delivery
from harness.context import GateContext
from harness.native_successor_migration import (
    _json_sha256, _load_reviewed_w3a_comparison, _REVIEWED_W3A_EVIDENCE,
    load_native_successor_waves,
)
from harness.persistence import new_harness_run_id, persist_harness_run_start, persist_harness_run_complete
from harness.result import Evidence, GateResult, GateStatus
from harness.w2_reclaim_prepare import _request_date
from harness.w3b_prepare import _gate_proof, _ready_input, _write_receipt
from harness.w3a_revision_evidence import load_reviewed_w3a_revision
from harness.w3a_state_prepare import prepare_reviewed_w3a_states
from scheduler import repository
from scheduler.blackbox_state import _decode, _MAX_ENVELOPE_BYTES, read_regular_bytes
from scheduler.blackbox_v2_runner import DEFAULT_RUNTIME_PROFILE, execute_blackbox_cli, state_binding_for_scheme
from scheduler.process_control import ProcessGroupTerminationError
from scheduler.discovery import load_scheme_config
from shared.blackbox_v2.contracts import load_prediction_result, load_request_bytes
from shared.blackbox_v2.environment_manifest import load_environment_fingerprint
from shared.blackbox_v2.requests import build_live_request, resolve_live_context, write_request
from shared.blackbox_v2.snapshot import compose_blackbox_input_bundle
from shared.calendar_service import get_calendar
from shared.input_artifacts import open_blackbox_runtime_view, resolve_blackbox_input_cutoffs


_ROOT = Path(__file__).resolve().parents[1]
W3A_IDS = writer_reclaim.RECLAIM_WAVES["W3A"]
_FULL, _SDA = W3A_IDS
_LOCK_IDS = sorted((*W3A_IDS, *(key + "_bbv2" for key in W3A_IDS)))
_STAGE = "native-runtime-upgrade"
_FIELDS = ("scheme_version", "runtime_type", "code_hash", "config_hash", "manifest_hash")
# 路径由已批准原件定位，内容仍由既有 loader 的固定 SHA-256 闭包认证。
_COMPARISON_PATHS = {
    _SDA: (Path("/opt/bond-factor-lab/incoming/cons-native-reference-20260908.a0Jdr4"),
           Path("/var/lib/bond-factor-lab/state/artifacts/blackbox_v2/snapshots/generation_cache/snapshots/07d766d267338ab6c0014805a5030023b08306bb53c188b9d748d4f2cf189323/snapshot-1d335ad33e23ca7e7c8f5b64/data")),
    _FULL: (Path("/opt/bond-factor-lab/incoming/a4-offline-c8d102e.LP4qqK/native-evidence"),
            Path("/opt/bond-factor-lab/incoming/a3-d0366b9.htVJZn/snapshot-f42540ebc533428ca6c869e2/data")),
}


def _read_json(path: Path) -> dict:
    value = json.loads(read_regular_bytes(path, 4 * 1024 * 1024))
    if not isinstance(value, dict):
        raise ValueError("W3A receipt must be an object")
    return value


def _database_snapshot(engine, *, old, sources, new, expected_database_name,
                       expected_server_uuid, permitted_runs=()):
    """保护四身份全部事实和审计，同时绑定无关 Registry，允许本次两个新 Gate。"""
    if engine.dialect.name != "mysql" or not expected_database_name or not expected_server_uuid:
        raise ValueError("W3A prepare requires explicit MySQL identity")
    params = {f"id_{index}": key for index, key in enumerate(_LOCK_IDS)}
    scope = "IN (" + ",".join(":" + key for key in params) + ")"
    with engine.connect() as conn:
        conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        try:
            identity = dict(conn.execute(text("SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid")).mappings().one())
            if identity != {"database_name": expected_database_name, "server_uuid": expected_server_uuid}:
                raise RuntimeError("W3A database identity mismatch")
            def rows(table, where, order, bindings=params):
                return repository._same_id_rows_conn(conn, table, where, bindings, order=order, for_update=False)
            migrations = rows("t_schema_migrations", "1=1", "version", {})
            if not migrations or max(int(row["version"]) for row in migrations) != 24 or any(row["state"] != "APPLIED" for row in migrations):
                raise RuntimeError("W3A requires applied schema 024")
            for table, order in (("t_scheme_runs", "run_id"), ("t_backtest_runs", "id")):
                if rows(table, f"scheme_id {scope} AND status='running'", order):
                    raise RuntimeError("W3A requires zero running execution")
            harness = rows("t_harness_runs", f"scheme_id {scope}", "harness_run_id")
            gates = rows("t_harness_gate_results", f"harness_run_id IN (SELECT harness_run_id FROM t_harness_runs WHERE scheme_id {scope})", "id")
            for row in harness:
                if row["harness_run_id"] in permitted_runs:
                    continue
                if row["status"] == "running" or (row["scheme_id"] in new
                        and row["scheme_version"] == new[row["scheme_id"]].scheme_version and row["stage"] == _STAGE):
                    raise RuntimeError("W3A preparation already exists or is running; inspect retained evidence")
            registry = rows("t_scheme_registry", f"base_scheme_id {scope}", "scheme_id")
            versions = rows("t_scheme_versions", f"scheme_id {scope}", "scheme_id,scheme_version")
            for key in W3A_IDS:
                for cfg, status, runtime in ((old[key], "archived", "native_adapter"), (sources[key], "active", "blackbox_v2")):
                    matches = [row for row in registry if row["base_scheme_id"] == cfg.scheme_id]
                    if len(matches) != 1 or any(matches[0].get(field) != value for field, value in {
                        "scheme_id": repository.registry_scheme_id(cfg.scheme_id, cfg.horizon, "5Y"),
                        "target_tenor": "5Y", "horizon": 5, "task_type": "T+5", "frequency": "daily",
                        "status": status, "runtime_type": runtime,
                        "schedule_cron": cfg.schedule.cron, "schedule_timezone": cfg.schedule.timezone,
                    }.items()):
                        raise RuntimeError("W3A original/source Registry differs")
                    exact = [row for row in versions if row["scheme_id"] == cfg.scheme_id and row["scheme_version"] == cfg.scheme_version]
                    if len(exact) != 1 or exact[0]["status"] != ("retired" if runtime == "native_adapter" else "active"):
                        raise RuntimeError("W3A original/source exact status differs")
                    repository._assert_migration_version_identity(cfg, exact[0])
                for row in versions:
                    if row["scheme_id"] == key + "_bbv2" and row["scheme_version"] != sources[key].scheme_version and row["status"] != "retired":
                        raise RuntimeError("W3A source has another Writer")
                    if row["scheme_id"] == key and row["scheme_version"] != old[key].scheme_version:
                        if row["runtime_type"] != "native_adapter" or row["status"] not in {"active", "paused", "retired"}:
                            raise RuntimeError("W3A candidate or second Writer already exists")
            facts = repository._same_id_fact_snapshot_conn(conn, _LOCK_IDS, for_update=False)
            facts.pop("t_harness_runs")
            facts.pop("t_harness_gate_results")
            return {"database_identity_sha256": repository.native_successor_plan_sha256(identity),
                "schema_sha256": repository.native_successor_plan_sha256({"rows": migrations}), "facts": facts,
                "historical_harness_sha256": repository.native_successor_plan_sha256({
                    "runs": [row for row in harness if row["harness_run_id"] not in permitted_runs],
                    "gates": [row for row in gates if row["harness_run_id"] not in permitted_runs]}),
                "registry_sha256": repository.native_successor_plan_sha256({"rows": registry}),
                "versions_sha256": repository.native_successor_plan_sha256({"rows": versions}),
                "unaffected_registry_sha256": repository.native_successor_plan_sha256({"rows": rows(
                    "t_scheme_registry", f"base_scheme_id NOT {scope}", "scheme_id")})}
        finally:
            conn.rollback()


def _verify_comparisons_in_source_locale(reference: Path, root: Path) -> dict:
    """仅供只读子进程：在原真实 locale 下完整复验旧 333 点原件。"""
    if os.environ.get("LANG") != "en_US.UTF-8" or os.environ.get("LC_ALL") != "C.UTF-8" or "TZ" in os.environ:
        raise RuntimeError("W3A comparison requires its original isolated locale")
    wave = load_native_successor_waves(root / "deploy/native_to_blackbox_migration_v1.json")["W3A"]
    fingerprint = load_environment_fingerprint(reference, expected_runtime_profile="blackbox-v2-v1")
    proofs = {}
    for target in wave.targets:
        key = target.old_base_scheme_id
        if key not in _COMPARISON_PATHS:
            raise RuntimeError("W3A fixed comparison location is unavailable")
        evidence_dir, data_dir = _COMPARISON_PATHS[key]
        source = load_scheme_config(reference / "schemes" / target.new_base_scheme_id / "config.yaml")
        requests, native, results, lineage = _load_reviewed_w3a_comparison(
            project_root=reference, target=target, new_config=replace(source, environment_fingerprint=fingerprint),
            evidence_dir=evidence_dir, data_dir=data_dir)
        if native != results:
            raise RuntimeError("W3A reviewed Native/source comparison differs")
        proofs[key] = {"scheme_id": key, "request_count": len(requests),
            "identity_sha256": _REVIEWED_W3A_EVIDENCE[key]["identity_sha256"],
            "summary_sha256": _REVIEWED_W3A_EVIDENCE[key]["summary_sha256"], "input": lineage,
            "requests_sha256": _json_sha256(requests), "results_sha256": _json_sha256(results),
            "equivalence_verification_environment": {"LANG": "en_US.UTF-8", "LC_ALL": "C.UTF-8",
                "TZ": "runtime_profile_default", "runtime_sha256": "cff7095b5e6c7669b77a3335e12164d35cd59cf52b6180ee22cf6f659bbe6ce6"},
            "algorithm_executions": 0}
    return proofs


def _equivalence(reference, root, old, sources, new, fingerprint, conversions):
    """旧证据只在隔离原 locale 子进程复验；父进程及当前执行环境保持不变。"""
    environment = dict(os.environ, LANG="en_US.UTF-8", LC_ALL="C.UTF-8",
                       PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(root))
    environment.pop("TZ", None)
    program = ("import json,sys; from pathlib import Path; "
               "from harness.w3a_reclaim_prepare import _verify_comparisons_in_source_locale; "
               "print(json.dumps(_verify_comparisons_in_source_locale(Path(sys.argv[1]),Path(sys.argv[2])),sort_keys=True))")
    result = subprocess.run([sys.executable, "-c", program, str(reference), str(root)],
        env=environment, cwd=root, check=False, capture_output=True, timeout=120)
    if result.returncode or result.stderr.strip() or len(result.stdout) > 1024 * 1024:
        raise RuntimeError("W3A isolated original-locale comparison verification failed")
    proofs = json.loads(result.stdout)
    if not isinstance(proofs, dict) or set(proofs) != set(W3A_IDS):
        raise RuntimeError("W3A original-locale comparison coverage differs")
    for key in W3A_IDS:
        proof = proofs[key]
        if (old[key].code_hash != _REVIEWED_W3A_EVIDENCE[key]["old_code_hash"]
                or not isinstance(proof, dict) or proof.get("scheme_id") != key or proof.get("request_count") != 333
                or proof.get("algorithm_executions") != 0
                or any(proof.get(field) != _REVIEWED_W3A_EVIDENCE[key][field] for field in ("identity_sha256", "summary_sha256"))):
            raise RuntimeError("W3A reviewed Native source differs")
        proofs[key]["identity_conversion"] = conversions[key]
    reviewed = load_reviewed_w3a_revision(new[_FULL])
    proofs[_FULL]["reviewed_revision"] = reviewed["proof"]
    return proofs, reviewed


def _capture_inputs(engine, *, project_root, reference_project_root, rollback_project_root,
                    predict_date=None, expected_database_name, expected_server_uuid,
                    permitted_runs=(), check_initial_states=True):
    """只读 fresh 计划；daily 从准备开始直到接管始终 fenced。"""
    root, reference = project_root.resolve(strict=True), reference_project_root.resolve(strict=True)
    if root != _ROOT or os.environ.get("BFL_DEPLOYMENT_TARGET") != "aliyun-gray":
        raise RuntimeError("W3A must execute from the immutable ECS candidate")
    control._assert_execution_modules(root)
    old, sources, new, releases = writer_reclaim._verified_pair(root, reference, "W3A")
    releases["rollback"] = writer_reclaim._verified_w3a_rollback(root, rollback_project_root, sources)
    rollback = Path(releases["rollback"]["path"])
    current = control._CURRENT_LINKS["aliyun-gray"]
    if not current.is_symlink() or current.resolve(strict=True) != rollback:
        raise RuntimeError("W3A prepare requires unchanged pre-cutover rollback current")
    if os.environ.get("BFL_RELEASE_COMMIT") != releases["candidate"]["commit"]:
        raise RuntimeError("W3A process commit differs from candidate")
    scheduler = control._capture_scheduler_state(project_root=rollback, deployment_target="aliyun-gray", cadence="daily")
    locale = control._capture_installed_locale(root, installed_current_root=rollback)
    writer_reclaim._assert_no_algorithm_process("W3A")
    snapshot, inputs = _ready_input()
    fingerprint = load_environment_fingerprint(root, expected_runtime_profile="blackbox-v2-v1")
    new = {key: replace(cfg, environment_fingerprint=fingerprint, data_snapshot_id=snapshot.snapshot_id) for key, cfg in new.items()}
    for key, cfg in new.items():
        validate_canonical_blackbox_delivery(cfg)
        if (cfg.incremental_state != (key == _FULL) or cfg.horizon != 5 or cfg.tenors != ["5Y"]
                or cfg.task_type != "T+5" or cfg.schedule.cron != "3 7 * * 1-5" or cfg.schedule.timezone != "Asia/Shanghai"):
            raise RuntimeError("W3A requires the fixed daily Full/SDA contract")
    database = _database_snapshot(engine, old=old, sources=sources, new=new,
        expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid, permitted_runs=permitted_runs)
    equivalence, reviewed = _equivalence(reference, root, old, sources, new, fingerprint, releases["identity_conversions"])
    if reviewed["execution_files"] != inputs["files"]:
        raise RuntimeError("W3A current input differs from the reviewed revision")
    if check_initial_states:
        source_state = control._read_candidate_states(root, {sources[_FULL].scheme_id: sources[_FULL]}, snapshot)
        if source_state[sources[_FULL].scheme_id]["envelope_sha256"] != reviewed["source_envelope_sha256"]:
            raise RuntimeError("W3A source state changed before preparation")
        binding = state_binding_for_scheme(new[_FULL], generation_id=snapshot.generation_id, persistent=True)
        if binding is None or binding.root is None or os.path.lexists(binding.root / _FULL / f"{new[_FULL].scheme_version}.state"):
            raise RuntimeError("W3A candidate state already exists; inspect preparation, never repeat")
    calendar = get_calendar(engine)
    day = _request_date(calendar, predict_date)
    metadata = new[_SDA].blackbox_metadata
    feature = resolve_live_context(metadata, predict_date=day, calendar=calendar).feature_date
    request = build_live_request(metadata, predict_date=day, calendar=calendar,
        cutoffs=resolve_blackbox_input_cutoffs(snapshot, feature_date=feature, engine=engine))
    plan = {"schema_version": "w3a-writer-reclaim-prepare-v1", "wave": "W3A", "releases": releases,
        "current": releases["rollback"], "scheduler": scheduler, "locale": locale, "database": database,
        "input": inputs, "environment_fingerprint": fingerprint, "equivalence": equivalence,
        "source_before_envelope_sha256": reviewed["source_envelope_sha256"],
        "candidate_versions": {key: cfg.scheme_version for key, cfg in new.items()},
        "source_identities": {key: {field: getattr(cfg, field) for field in writer_reclaim._IDENTITY_FIELDS}
                              for key, cfg in sources.items()},
        "requests": {_FULL: asdict(reviewed["request"]), _SDA: asdict(request)}}
    return old, sources, new, snapshot, request, reviewed, plan


def build_w3a_reclaim_prepare_preflight(engine, **kwargs):
    """不创建目录、状态、Harness 或算法进程的只读预检。"""
    plan = _capture_inputs(engine, **kwargs)[-1]
    return {"plan": plan, "plan_sha256": _json_sha256(plan)}


def read_w3a_readiness(root, *, sources, new, snapshot, work_dir, releases):
    """只读复验真实完整回执和两份当前输入状态；不预热、不 publish。"""
    if work_dir is None:
        raise ValueError("W3A requires its real preparation work-dir")
    work = Path(work_dir)
    if not work.is_absolute() or work != work.resolve(strict=True) or os.path.lexists(work / "failure.json"):
        raise RuntimeError("W3A preparation failed or work path is unsafe")
    complete, plan = _read_json(work / "complete.json"), _read_json(work / "plan.json")
    if (complete.get("status") != "ready" or complete.get("plan_sha256") != _json_sha256(plan)
            or plan.get("releases") != releases or plan.get("candidate_versions") != {key: cfg.scheme_version for key, cfg in new.items()}
            or plan.get("environment_fingerprint") != new[_FULL].environment_fingerprint
            or plan.get("input", {}).get("data_snapshot_id") != snapshot.snapshot_id
            or plan.get("input", {}).get("generation_id") != snapshot.generation_id):
        raise RuntimeError("W3A readiness release/input/environment differs")
    runs = writer_reclaim.parse_reclaim_run_ids("W3A", [f"{key}={value}" for key, value in complete.get("harness_run_ids", {}).items()])
    execution = {key: _read_json(work / f"{key}.execution.json") for key in W3A_IDS}
    digests = {key: hashlib.sha256(read_regular_bytes(work / f"{key}.execution.json", 4 * 1024 * 1024)).hexdigest()
               for key in W3A_IDS}
    if complete.get("local_execution_sha256") != digests:
        raise RuntimeError("W3A execution receipt changed")
    sda = execution[_SDA]
    request_raw = read_regular_bytes(work / "sda.request.json", 64 * 1024)
    request = load_request_bytes(request_raw)
    result = load_prediction_result(work / "sda-output/result.json", request)
    if (sda.get("request") != asdict(request) or sda.get("result") != asdict(result)
            or sda.get("scheme_id") != _SDA or sda.get("scheme_version") != new[_SDA].scheme_version
            or sda.get("environment_fingerprint") != new[_SDA].environment_fingerprint
            or sda.get("input") != plan["input"]
            or hashlib.sha256(request_raw).hexdigest() != sda.get("request_sha256")
            or hashlib.sha256(read_regular_bytes(work / "sda-output/result.json", 64 * 1024)).hexdigest() != sda.get("result_sha256")
            or hashlib.sha256(read_regular_bytes(work / "sda.logs.json", 4 * 1024 * 1024)).hexdigest() != sda.get("logs_sha256")):
        raise RuntimeError("W3A SDA standard execution files differ")
    full = _read_json(work / "states/complete.json")
    if execution[_FULL].get("state_readiness_sha256") != _json_sha256(full) or full.get("status") != "ready":
        raise RuntimeError("W3A Full state preparation differs")
    if (full.get("revision_proof") != load_reviewed_w3a_revision(new[_FULL])["proof"]
            or full.get("context", {}).get("plan_sha256") != complete["plan_sha256"]
            or full.get("input", {}).get("files") != plan["input"]["files"]
            or full.get("input", {}).get("snapshot_id") != snapshot.snapshot_id
            or full.get("training_rows") != 0 or full.get("algorithm_executions") != 1):
        raise RuntimeError("W3A reviewed state or current input proof differs")
    states = control._read_candidate_states(root, {cfg.scheme_id: cfg for cfg in (sources[_FULL], new[_FULL])}, snapshot)
    for role, cfg in (("source", sources[_FULL]), ("candidate", new[_FULL])):
        binding = state_binding_for_scheme(cfg, generation_id=snapshot.generation_id, persistent=True)
        header, payload = _decode(read_regular_bytes(binding.root / cfg.scheme_id / f"{cfg.scheme_version}.state", _MAX_ENVELOPE_BYTES))
        if (states[cfg.scheme_id]["envelope_sha256"] != full[role + "_state"]["state_envelope_sha256"]
                or header["identity"] != full[role + "_identity"] or header["input"] != full["input"]
                or hashlib.sha256(payload).hexdigest() != full[role + "_state"]["state_output_sha256"]):
            raise RuntimeError("W3A latest source/candidate state differs from readiness")
    return {"status": "ready", "source_and_candidate_current_input_verified": True,
        "prepare_database_identity_sha256": plan["database"]["database_identity_sha256"],
        "harness_run_ids": runs, "local_execution_sha256": digests,
        "equivalence_sha256": {key: _json_sha256(plan["equivalence"][key]) for key in W3A_IDS},
        "prepare_plan_sha256": complete["plan_sha256"], "complete_sha256": _json_sha256(complete),
        "states": states}


def execute_w3a_reclaim_prepare(engine, *, project_root, reference_project_root, rollback_project_root,
        predict_date=None, expected_database_name, expected_server_uuid, expected_plan_sha256, approved_by, work_dir):
    """四锁内只执行一次 warm 和 SDA predict；失败保全、完整完成只读恢复回执。"""
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError("W3A prepare requires approved_by")
    if not isinstance(expected_plan_sha256, str) or len(expected_plan_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_plan_sha256):
        raise ValueError("W3A prepare requires approved SHA-256")
    work = Path(work_dir)
    if not work.is_absolute() or work != work.resolve(strict=False):
        raise ValueError("W3A prepare requires an absolute symlink-free work-dir")
    kwargs = dict(project_root=project_root, reference_project_root=reference_project_root,
        rollback_project_root=rollback_project_root, predict_date=predict_date,
        expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid)
    with ExitStack() as locks:
        for key in _LOCK_IDS:
            locks.enter_context(repository._blackbox_activation_advisory_lock(engine, scheme_id=key))
        if os.path.lexists(work):
            receipt = _read_json(work / "complete.json")
            if receipt.get("plan_sha256") != expected_plan_sha256:
                raise RuntimeError("W3A existing preparation belongs to another approved plan")
            runs = writer_reclaim.parse_reclaim_run_ids("W3A", [f"{key}={value}" for key, value in receipt.get("harness_run_ids", {}).items()])
            old, sources, new, snapshot, _, _, plan = _capture_inputs(engine, **kwargs,
                permitted_runs=tuple(runs.values()), check_initial_states=False)
            if _json_sha256(plan) != expected_plan_sha256:
                raise RuntimeError("W3A completed preparation context changed")
            readiness = read_w3a_readiness(project_root, sources=sources, new=new, snapshot=snapshot,
                work_dir=work, releases=plan["releases"])
            _verify_gates(engine, old, new, plan, readiness)
            return receipt
        old, sources, new, snapshot, request, reviewed, plan = _capture_inputs(engine, **kwargs)
        if _json_sha256(plan) != expected_plan_sha256:
            raise RuntimeError("W3A prepare plan changed; obtain fresh preflight")
        binding = state_binding_for_scheme(new[_FULL], generation_id=snapshot.generation_id, persistent=True)
        forbidden = (project_root.parent, reference_project_root, rollback_project_root,
                     snapshot.data_dir, binding.root if binding is not None else project_root)
        if any(work.is_relative_to(Path(path).resolve()) for path in forbidden if path is not None):
            raise ValueError("W3A work-dir must stay outside releases, inputs and persistent states")
        work.mkdir(mode=0o700)
        _write_receipt(work / "plan.json", plan)
        runs, local = {}, {}
        ctx, run_id, started = None, None, None
        bundle = compose_blackbox_input_bundle(snapshot, factor_input_mode="algorithm_managed")
        with open_blackbox_runtime_view(bundle) as view:
            if view.bundle.combined_snapshot_id != snapshot.snapshot_id:
                raise RuntimeError("W3A runtime view differs from approved input")
            def verify_context():
                fresh = _capture_inputs(engine, **kwargs, permitted_runs=tuple(runs.values()), check_initial_states=False)[-1]
                if fresh != plan:
                    raise RuntimeError("W3A preparation context changed")
                return {"plan_sha256": expected_plan_sha256, "scheduler": plan["scheduler"],
                        "lifecycle_scheme_ids": _LOCK_IDS, "rollback": plan["releases"]["rollback"]}
            try:
                for key in W3A_IDS:
                    run_id = new_harness_run_id()
                    runs[key] = run_id
                    started = datetime.now(timezone.utc).isoformat()
                    ctx = GateContext(key, plan["requests"][key]["predict_date"], project_root,
                                      config=new[key], engine_factory=lambda: engine)
                    _write_receipt(work / f"{key}.started.json", {"harness_run_id": run_id,
                        "started_at": started, "approved_by": approved_by.strip(), "plan_sha256": expected_plan_sha256})
                    if not persist_harness_run_start(ctx, harness_run_id=run_id, stage=_STAGE, started_at=started):
                        raise RuntimeError("W3A Harness start failed; no algorithm started")
                    verify_context()
                    if key == _FULL:
                        state = prepare_reviewed_w3a_states(project_root=project_root,
                            source_config=sources[key], candidate_config=new[key], data_dir=view.data_dir,
                            data_snapshot_id=snapshot.snapshot_id, generation_id=snapshot.generation_id,
                            expected_revision_proof=reviewed["proof"], work_dir=work / "states",
                            approved_by=approved_by, verify_context=verify_context)
                        execution = {"scheme_id": key, "scheme_version": new[key].scheme_version,
                            "state_readiness_sha256": _json_sha256(state), "reviewed_revision": reviewed["proof"],
                            "algorithm_executions": 1, "candidate_algorithm_executions": 0, "training_rows": 0}
                    else:
                        execution = _execute_sda(new[key], request, view.data_dir, work, plan, approved_by)
                    local[key] = _write_receipt(work / f"{key}.execution.json", execution)
                    verify_context()
                    gate = GateResult(_STAGE, GateStatus.PASSED,
                        [Evidence("runtime_upgrade", _gate_proof(old[key], new[key], plan, key, local[key]))], [],
                        started, datetime.now(timezone.utc).isoformat())
                    if not persist_harness_run_complete(ctx, harness_run_id=run_id, status="passed",
                            finished_at=gate.finished_at, results=[gate]):
                        raise RuntimeError("W3A Gate completion failed; inspect retained execution, never repeat")
                verify_context()
                receipt = {"status": "ready", "harness_run_ids": runs, "plan_sha256": expected_plan_sha256,
                    "local_execution_sha256": local, "algorithm_executions": 2, "full_training_rows": 0,
                    "prediction_written": False, "registry_changed": False, "keep_daily_fenced": True}
                _write_receipt(work / "complete.json", receipt)
                readiness = read_w3a_readiness(project_root, sources=sources, new=new, snapshot=snapshot,
                    work_dir=work, releases=plan["releases"])
                _verify_gates(engine, old, new, plan, readiness)
                return receipt
            except BaseException as error:
                if isinstance(error, ProcessGroupTerminationError):
                    view.mark_termination_uncertain()
                try:
                    _write_receipt(work / "failure.json", {"status": "failed", "error_type": type(error).__name__,
                        "error": str(error), "harness_run_ids": runs, "keep_daily_fenced": True,
                        "inspect_retained_artifacts": True, "automatic_retry": False})
                except BaseException:
                    error.add_note("W3A failure receipt unavailable; retain all state/work for inspection.")
                _record_failed_gate(engine, ctx, run_id, started, error)
                raise


def _verify_gates(engine, old, new, plan, readiness):
    """恢复回执前通过仓储原合同核验真实 passed Gate，不伪造审计状态。"""
    evidence = {"wave": "W3A", "databridge": {"generation_id": plan["input"]["generation_id"],
        "data_snapshot_id": plan["input"]["data_snapshot_id"]},
        "blackbox_environment_fingerprint": plan["environment_fingerprint"], "w3a_readiness": readiness}
    with engine.connect() as conn:
        for key in W3A_IDS:
            repository._same_id_evidence_conn(conn, old[key], new[key], readiness["harness_run_ids"][key], evidence, for_update=False)


def _record_failed_gate(engine, ctx, run_id, started, error):
    """先只读判定提交结果；只结束本次确定 running 且尚无 Gate 的新审计。"""
    if ctx is None or run_id is None:
        return
    try:
        with engine.connect() as conn:
            runs = repository._same_id_rows_conn(conn, "t_harness_runs", "harness_run_id=:run",
                {"run": run_id}, order="harness_run_id", for_update=False)
            gates = repository._same_id_rows_conn(conn, "t_harness_gate_results", "harness_run_id=:run",
                {"run": run_id}, order="id", for_update=False)
        if len(runs) != 1 or runs[0]["status"] != "running" or gates:
            return
        row = runs[0]
        expected = {"scheme_id": ctx.scheme_id, "scheme_version": ctx.config.scheme_version,
                    "code_hash": ctx.config.code_hash, "config_hash": ctx.config.config_hash, "stage": _STAGE}
        if any(row.get(key) != value for key, value in expected.items()):
            raise RuntimeError("W3A current failed-run identity differs")
        failed = GateResult(_STAGE, GateStatus.FAILED, [], ["preparation failed; inspect retained artifacts"],
                           started, datetime.now(timezone.utc).isoformat())
        if not persist_harness_run_complete(ctx, harness_run_id=run_id, status="failed",
                finished_at=failed.finished_at, results=[failed]):
            error.add_note("W3A failed Harness completion unavailable; inspect retained evidence.")
    except BaseException:
        error.add_note("W3A Harness commit outcome unavailable; no blind audit rewrite attempted.")


def _execute_sda(cfg, request, data_dir, work, plan, approved_by):
    """仅执行一次本机标准 SDA predict，真实输出保留，不写业务表。"""
    profile = replace(DEFAULT_RUNTIME_PROFILE, predict_timeout_sec=120, cpu_threads=8, memory_limit_bytes=4 * 1024**3)
    path = write_request(request, work / "sda.request.json")
    original = read_regular_bytes(path, 64 * 1024)
    output, process = work / "sda-output/result.json", {}
    def started(pid, pgid):
        process.update(process_id=pid, process_group_id=pgid)
        _write_receipt(work / "sda.process.json", process)
    begin = time.monotonic()
    completed = None
    try:
        completed = execute_blackbox_cli(script_path=cfg.delivery_script, mode="predict", input_path=path,
            data_dir=data_dir, output_path=output, profile=profile, timeout_sec=120, process_started=started)
        _write_receipt(work / "sda.logs.json", {"stdout": completed.stdout, "stderr": completed.stderr})
        if completed.returncode or completed.stdout.strip() or read_regular_bytes(path, 64 * 1024) != original:
            raise RuntimeError("W3A SDA CLI or Request integrity failed")
        result = load_prediction_result(output, request)
    except BaseException as error:
        try:
            _write_receipt(work / "sda.failed.json", {"error_type": type(error).__name__, "error": str(error),
                "process": process, "seconds": time.monotonic() - begin,
                "stdout": completed.stdout if completed is not None else None,
                "stderr": completed.stderr if completed is not None else None, "keep_daily_fenced": True})
        except BaseException:
            error.add_note("W3A SDA failure receipt unavailable; original failure retained.")
        raise
    return {"scheme_id": cfg.scheme_id, "scheme_version": cfg.scheme_version, "approved_by": approved_by.strip(),
        "request": asdict(request), "result": asdict(result), "request_sha256": hashlib.sha256(original).hexdigest(),
        "result_sha256": hashlib.sha256(read_regular_bytes(output, profile.max_output_bytes)).hexdigest(),
        "input": plan["input"], "environment_fingerprint": cfg.environment_fingerprint, "profile": asdict(profile),
        "seconds": time.monotonic() - begin, "process": process,
        "logs_sha256": hashlib.sha256(read_regular_bytes(work / "sda.logs.json", 4 * 1024 * 1024)).hexdigest(),
        "algorithm_executions": 1, "prediction_written": False, "state_written": False}
