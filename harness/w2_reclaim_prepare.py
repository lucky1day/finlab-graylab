"""W2/W1B 原 ID 无状态准备：复用等价证明，每方案只调用一次。"""

from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import time as timing
from zoneinfo import ZoneInfo

from sqlalchemy import text

from harness import same_id_runtime_upgrade as control, writer_reclaim
from harness.blackbox_v2.gates import validate_canonical_blackbox_delivery
from harness.context import GateContext
from harness.native_successor_migration import _json_sha256, _sha256_file
from harness.persistence import new_harness_run_id, persist_harness_run_start, persist_harness_run_complete
from harness.result import Evidence, GateResult, GateStatus
from harness.w3b_prepare import _gate_proof, _ready_input, _write_receipt
from scheduler.blackbox_v2_runner import DEFAULT_RUNTIME_PROFILE, execute_blackbox_cli
from scheduler.blackbox_state import read_regular_bytes
from scheduler.process_control import ProcessGroupTerminationError
from scheduler.repository import (
    _blackbox_activation_advisory_lock, _same_id_fact_snapshot_conn,
    _same_id_rows_conn, _is_preserved_w2_historical_compare, native_successor_plan_sha256,
)
from shared.blackbox_v2.contracts import load_prediction_result
from shared.blackbox_v2.environment_manifest import load_environment_fingerprint
from shared.blackbox_v2.requests import build_live_request, resolve_live_context, write_request
from shared.blackbox_v2.snapshot import compose_blackbox_input_bundle
from shared.calendar_service import get_calendar
from shared.input_artifacts import open_blackbox_runtime_view, resolve_blackbox_input_cutoffs


_ROOT = Path(__file__).resolve().parents[1]
W2_IDS = writer_reclaim.RECLAIM_WAVES["W2"]
_STAGE = "native-runtime-upgrade"
_REPORT = Path("/opt/bond-factor-lab/incoming/w2-four-worker-20260909.rpZUXS/W2.json")
_REPORT_SHA = "337c6d9483e00721cefb1c43cedf647a4def04bc1313584371cbdc143dd7df1e"
_FIELDS = ("scheme_version", "runtime_type", "code_hash", "config_hash", "manifest_hash")


def _equivalence(old, sources, new, conversions, *, wave="W2") -> dict:
    """核实固定原件及真实旧/新字节，保留原报告输入和数值环境。"""
    ids = _stateless_ids(wave)
    report_path = _REPORT if wave == "W2" else _ROOT / "deploy/native_successor_equivalence/W1B.json"
    report_sha = _REPORT_SHA if wave == "W2" else "5f5b95da6dea3d6b008a4fa73853c47f40cdfaf9534a63f31e6fe8645b079960"
    count, task, old_horizon, new_horizon = (333, "T+5", 5, 5) if wave == "W2" else (72, "weekly_point", 6, 1)
    payload = read_regular_bytes(report_path, 1024 * 1024)
    if hashlib.sha256(payload).hexdigest() != report_sha:
        raise RuntimeError("W2 reviewed equivalence report changed")
    report = json.loads(payload)
    targets = report.get("targets", [])
    if (report.get("wave") != wave or len(targets) != len(ids)
            or {item.get("old_base_scheme_id") for item in targets} != set(ids)):
        raise RuntimeError("W2 equivalence requires both approved targets")
    proofs = {}
    for item in targets:
        key = item["old_base_scheme_id"]
        if (item.get("new_base_scheme_id") != key + "_bbv2"
                or item.get("request_count") != count
                or any(item.get(field) != 0 for field in (
                    "request_id_mismatch_count", "predict_date_mismatch_count",
                    "feature_date_mismatch_count", "target_date_mismatch_count", "direction_mismatch_count"))
                or item.get("native_result_sha256") != item.get("successor_result_sha256")
                or item.get("old_code_hash") != old[key].code_hash
                or item.get("new_code_hash") != sources[key].code_hash
                or sources[key].code_hash != new[key].code_hash
                or item.get("target_tenor") != new[key].tenors[0]
                or item.get("task_type") != task
                or item.get("old_horizon") != old_horizon or item.get("new_horizon") != new_horizon):
            raise RuntimeError("W2 reviewed equivalence differs from canonical identities")
        proofs[key] = {"report_path": str(report_path), "report_sha256": report_sha,
                       "comparison": item, "producer": report["producer"],
                       "runtime_environment_fingerprint": report["runtime_environment_fingerprint"],
                       "identity_conversion": conversions[key], "algorithm_executions": 0}
    return proofs


def _database_snapshot(engine, *, sources, new, expected_database_name: str,
                       expected_server_uuid: str, permitted_runs: tuple[str, ...] = (), wave="W2") -> dict:
    """只读取四个身份的保护摘要，不借用 W3B 的固定身份快照。"""
    if engine.dialect.name != "mysql" or not expected_database_name or not expected_server_uuid:
        raise ValueError("W2 prepare requires explicit MySQL identity")
    wave_ids = _stateless_ids(wave)
    ids = sorted((*wave_ids, *(key + "_bbv2" for key in wave_ids)))
    params = {f"id_{i}": key for i, key in enumerate(ids)}
    scope = "IN (" + ",".join(":" + key for key in params) + ")"
    with engine.connect() as conn:
        conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        try:
            identity = dict(conn.execute(text("SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid")).mappings().one())
            if identity != {"database_name": expected_database_name, "server_uuid": expected_server_uuid}:
                raise RuntimeError("W2 prepare database identity mismatch")
            migrations = _same_id_rows_conn(conn, "t_schema_migrations", "1=1", {}, order="version", for_update=False)
            if not migrations or max(int(row["version"]) for row in migrations) != 24 or any(row["state"] != "APPLIED" for row in migrations):
                raise RuntimeError("W2 prepare requires applied schema 024")
            for table in ("t_scheme_runs", "t_backtest_runs"):
                if conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE scheme_id {scope} AND status='running'"), params).scalar_one():
                    raise RuntimeError("W2 prepare requires idle scheme and backtest execution")
            harness = _same_id_rows_conn(conn, "t_harness_runs", f"scheme_id {scope}", params,
                                         order="harness_run_id", for_update=False)
            gates = _same_id_rows_conn(conn, "t_harness_gate_results", "harness_run_id IN "
                f"(SELECT harness_run_id FROM t_harness_runs WHERE scheme_id {scope})", params,
                order="id", for_update=False)
            for row in harness:
                if row["harness_run_id"] in permitted_runs:
                    continue
                if wave == "W2" and row["status"] == "running" and _is_preserved_w2_historical_compare(conn, row):
                    continue
                if (row["status"] == "running" or row["scheme_id"] in new
                        and row["scheme_version"] == new[row["scheme_id"]].scheme_version
                        and row["stage"] == _STAGE):
                    raise RuntimeError("W2 preparation already exists or is running; inspect retained evidence, do not repeat")
            registry = _same_id_rows_conn(conn, "t_scheme_registry", f"base_scheme_id {scope}", params,
                                          order="scheme_id", for_update=False)
            versions = _same_id_rows_conn(conn, "t_scheme_versions", f"scheme_id {scope}", params,
                                          order="scheme_id, scheme_version", for_update=False)
            for key in wave_ids:
                original = [row for row in registry if row["base_scheme_id"] == key]
                alias = [row for row in registry if row["base_scheme_id"] == key + "_bbv2"]
                active = [row for row in versions if row["scheme_id"] == key + "_bbv2" and row["status"] == "active"]
                if (len(original) != 1 or original[0]["status"] != "archived"
                        or original[0]["runtime_type"] != "native_adapter"
                        or len(alias) != 1 or alias[0]["status"] != "active"
                        or alias[0]["runtime_type"] != "blackbox_v2"
                        or len(active) != 1 or any(active[0].get(field) != getattr(sources[key], field) for field in _FIELDS)):
                    raise RuntimeError("W2 prepare requires the exact temporary Blackbox Writer")
            facts = _same_id_fact_snapshot_conn(conn, ids, for_update=False)
            facts.pop("t_harness_runs")
            facts.pop("t_harness_gate_results")
            return {"database_identity_sha256": _json_sha256(identity),
                    "schema_sha256": native_successor_plan_sha256({"rows": migrations}), "facts": facts,
                    "historical_harness_sha256": native_successor_plan_sha256({
                        "runs": [row for row in harness if row["harness_run_id"] not in permitted_runs],
                        "gates": [row for row in gates if row["harness_run_id"] not in permitted_runs],
                    }),
                    "registry_sha256": native_successor_plan_sha256({"rows": registry}),
                    "versions_sha256": native_successor_plan_sha256({"rows": versions})}
        finally:
            conn.rollback()


def _request_date(calendar, predict_date: str | None) -> str:
    """只选择上海日频触发已到期的交易日，不执行未来自然触发。"""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    today = now.date().isoformat()
    if not calendar.covers(today):
        raise RuntimeError("W2 calendar does not cover today")
    latest = today if calendar.is_trading_day(today) and now.time() >= time(7, 3) else calendar.previous_trading_day(today)
    while date.fromisoformat(latest).weekday() > 4:
        latest = calendar.previous_trading_day(latest)
    selected = latest if predict_date is None else predict_date
    if (not isinstance(selected, str) or date.fromisoformat(selected).isoformat() != selected
            or selected > latest or not calendar.is_trading_day(selected)
            or date.fromisoformat(selected).weekday() > 4):
        raise ValueError("W2 predict_date must be an already-due weekday trading date")
    return selected


def _stateless_ids(wave):
    """只支持两个已批准的无状态迁移批次，不作为通用算法框架。"""
    if wave not in {"W2", "W1B"}:
        raise ValueError("stateless reclaim preparation supports W2/W1B only")
    return writer_reclaim.RECLAIM_WAVES[wave]


def _weekly_request_date(predict_date):
    """周六 11:30 上海调度已到期才允许模拟该次 Request。"""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    latest = now.date() - timedelta(days=(now.weekday() - 5) % 7)
    if now.date() == latest and now.time() < time(11, 30):
        latest -= timedelta(days=7)
    selected = latest.isoformat() if predict_date is None else predict_date
    if (not isinstance(selected, str) or date.fromisoformat(selected).isoformat() != selected
            or date.fromisoformat(selected).weekday() != 5 or selected > latest.isoformat()):
        raise ValueError("W1B predict_date must be an already-due Saturday trigger")
    return selected


def _capture_inputs(engine, *, project_root: Path, reference_project_root: Path,
                    predict_date: str | None, expected_database_name: str,
                    expected_server_uuid: str, permitted_runs: tuple[str, ...] = (), wave="W2"):
    """从 immutable 候选、真实临时 Writer、现场输入和四身份读取准备计划。"""
    root, reference = project_root.resolve(strict=True), reference_project_root.resolve(strict=True)
    if root != _ROOT or os.environ.get("BFL_DEPLOYMENT_TARGET") != "aliyun-gray":
        raise RuntimeError("W2 prepare must execute from its ECS immutable candidate")
    control._assert_execution_modules(root)
    _stateless_ids(wave)
    old, sources, new, releases = writer_reclaim._verified_pair(root, reference, wave)
    if os.environ.get("BFL_RELEASE_COMMIT") != releases["candidate"]["commit"]:
        raise RuntimeError("W2 prepare process commit differs from candidate")
    current = control._CURRENT_LINKS["aliyun-gray"]
    if not current.is_symlink() or current.resolve(strict=True) != reference:
        raise RuntimeError("W2 prepare requires the unchanged temporary-Writer reference current")
    locale = control._capture_installed_locale(root, installed_current_root=reference)
    writer_reclaim._assert_no_algorithm_process(wave)
    weekly_options = {"wave": wave} if wave == "W1B" else {}
    scheduler = (control._capture_scheduler_state(project_root=reference, deployment_target="aliyun-gray", cadence="weekly")
                 if wave == "W1B" else None)
    snapshot, inputs = _ready_input()
    fingerprint = load_environment_fingerprint(root, expected_runtime_profile="blackbox-v2-v1")
    new = {key: replace(cfg, environment_fingerprint=fingerprint, data_snapshot_id=snapshot.snapshot_id)
           for key, cfg in new.items()}
    database = _database_snapshot(engine, sources=sources, new=new, expected_database_name=expected_database_name,
                                  expected_server_uuid=expected_server_uuid, permitted_runs=permitted_runs, **weekly_options)
    calendar = get_calendar(engine)
    day = _weekly_request_date(predict_date) if wave == "W1B" else _request_date(calendar, predict_date)
    requests = {}
    for key, cfg in new.items():
        metadata = validate_canonical_blackbox_delivery(cfg)
        expected_contract = (6, "weekly_point", "30 11 * * 6", 1) if wave == "W1B" else (5, "T+5", "3 7 * * 1-5", 5)
        if (cfg.incremental_state or (cfg.horizon, cfg.task_type, cfg.schedule.cron, metadata.horizon) != expected_contract
                or cfg.schedule.timezone != "Asia/Shanghai"):
            raise RuntimeError("W2 preparation only supports the approved stateless daily contract")
        feature = resolve_live_context(metadata, predict_date=day, calendar=calendar).feature_date
        cutoffs = resolve_blackbox_input_cutoffs(snapshot, feature_date=feature, engine=engine)
        requests[key] = build_live_request(metadata, predict_date=day, calendar=calendar, cutoffs=cutoffs)
    plan = {"schema_version": wave.lower() + "-writer-reclaim-prepare-v1", "wave": wave, "releases": releases,
            "current": {"path": str(reference), "install": releases["reference"]}, "locale": locale,
            "database": database, "input": inputs, "environment_fingerprint": fingerprint,
            "equivalence": _equivalence(old, sources, new, releases["identity_conversions"], **weekly_options),
            "old_identities": {key: {field: getattr(cfg, field) for field in _FIELDS} for key, cfg in old.items()},
            "source_identities": {key: {field: getattr(cfg, field) for field in _FIELDS} for key, cfg in sources.items()},
            "candidate_versions": {key: cfg.scheme_version for key, cfg in new.items()},
            "requests": {key: asdict(request) for key, request in requests.items()}}
    if scheduler is not None:
        plan["scheduler"] = scheduler
    return old, new, snapshot, requests, plan


def build_w2_reclaim_prepare_preflight(engine, *, project_root: Path, reference_project_root: Path,
                                       predict_date: str | None, expected_database_name: str,
                                       expected_server_uuid: str, wave="W2") -> dict:
    """只读预检不执行算法或写入凭据。"""
    plan = _capture_inputs(engine, project_root=project_root, reference_project_root=reference_project_root,
                           predict_date=predict_date, expected_database_name=expected_database_name,
                           expected_server_uuid=expected_server_uuid, **({"wave": wave} if wave != "W2" else {}))[-1]
    return {"plan": plan, "plan_sha256": _json_sha256(plan)}


def execute_w2_reclaim_prepare(engine, *, project_root: Path, reference_project_root: Path,
                               predict_date: str | None, expected_database_name: str,
                               expected_server_uuid: str, expected_plan_sha256: str,
                               approved_by: str, work_dir: Path, wave="W2") -> dict:
    """四锁内执行两次标准调用并持久化真实 Gate；任何失败保全后停止。"""
    ids = _stateless_ids(wave)
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError("W2 prepare requires explicit approved_by")
    if not isinstance(expected_plan_sha256, str) or len(expected_plan_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_plan_sha256):
        raise ValueError("W2 prepare requires an approved lowercase plan SHA-256")
    work_dir = Path(work_dir)
    if not work_dir.is_absolute() or work_dir != work_dir.resolve(strict=False) or os.path.lexists(work_dir):
        raise ValueError("W2 prepare requires a fresh absolute symlink-free work directory")
    kwargs = dict(project_root=project_root, reference_project_root=reference_project_root,
                  predict_date=predict_date, expected_database_name=expected_database_name,
                  expected_server_uuid=expected_server_uuid, **({"wave": wave} if wave != "W2" else {}))
    with ExitStack() as locks:
        for key in sorted((*ids, *(key + "_bbv2" for key in ids))):
            locks.enter_context(_blackbox_activation_advisory_lock(engine, scheme_id=key))
        old, new, snapshot, requests, plan = _capture_inputs(engine, **kwargs)
        if _json_sha256(plan) != expected_plan_sha256:
            raise RuntimeError("W2 prepare plan changed; obtain a fresh preflight")
        data_root = Path(getattr(snapshot, "data_dir", project_root))
        forbidden = (project_root.resolve().parent, reference_project_root.resolve().parent,
                     getattr(snapshot, "root_dir", data_root.parent), data_root)
        if any(work_dir.is_relative_to(Path(path).resolve()) for path in forbidden):
            raise ValueError("stateless preparation work directory must be outside releases and input data")
        work_dir.mkdir(mode=0o700)
        _write_receipt(work_dir / "plan.json", plan | {"approved_by": approved_by.strip()})
        runs = {}
        profile = replace(DEFAULT_RUNTIME_PROFILE, predict_timeout_sec=120,
                          cpu_threads=8, memory_limit_bytes=4 * 1024**3)
        bundle = compose_blackbox_input_bundle(snapshot, factor_input_mode="algorithm_managed")
        with open_blackbox_runtime_view(bundle) as view:
            if view.bundle.combined_snapshot_id != snapshot.snapshot_id:
                raise RuntimeError("W2 runtime view differs from the approved input")
            for key in ids:
                run_id = new_harness_run_id()
                started = datetime.now(timezone.utc).isoformat()
                request = requests[key]
                ctx = GateContext(key, request.predict_date, project_root, config=new[key], engine_factory=lambda: engine)
                _write_receipt(work_dir / f"{key}.started.json", {"harness_run_id": run_id, "started_at": started,
                               "plan_sha256": expected_plan_sha256, "request": asdict(request)})
                process = {}
                started_clock = None
                completed = None
                output = work_dir / key / "result.json"

                def record_process(process_id: int, process_group_id: int) -> None:
                    process.update(process_id=process_id, process_group_id=process_group_id,
                                   started_at=datetime.now(timezone.utc).isoformat())
                    _write_receipt(work_dir / f"{key}.process.json", process)

                try:
                    if not persist_harness_run_start(ctx, harness_run_id=run_id, stage=_STAGE, started_at=started):
                        raise RuntimeError("W2 Harness start failed; no algorithm was started")
                    permitted = tuple((*runs.values(), run_id))
                    if _capture_inputs(engine, **kwargs, permitted_runs=permitted)[-1] != plan:
                        raise RuntimeError("W2 prepare input/current/database changed before execution")
                    request_path = write_request(request, work_dir / f"{key}.request.json")
                    request_sha = _sha256_file(request_path)
                    started_clock = timing.monotonic()
                    completed = execute_blackbox_cli(script_path=new[key].delivery_script, mode="predict",
                        input_path=request_path, data_dir=view.data_dir, output_path=output,
                        profile=profile, process_started=record_process)
                    elapsed = timing.monotonic() - started_clock
                    if completed.returncode or completed.stdout.strip() or _sha256_file(request_path) != request_sha:
                        raise RuntimeError("W2 standard CLI output or Request integrity failed")
                    result = load_prediction_result(output, request)
                    execution = {"scheme_id": key, "scheme_version": new[key].scheme_version,
                                 "request": asdict(request), "result": asdict(result), "request_sha256": request_sha,
                                 "result_sha256": _sha256_file(output), "input": plan["input"],
                                 "environment_fingerprint": new[key].environment_fingerprint,
                                 "elapsed_seconds": elapsed, "stderr": completed.stderr[:8192], "process": process,
                                 "algorithm_executions": 1, "prediction_written": False, "state_written": False}
                    local_sha = _write_receipt(work_dir / f"{key}.execution.json", execution)
                    if _capture_inputs(engine, **kwargs, permitted_runs=permitted)[-1] != plan:
                        raise RuntimeError("W2 prepare input/current/database changed; retain execution and stop")
                    proof = _gate_proof(old[key], new[key], plan, key, local_sha)
                    gate = GateResult(_STAGE, GateStatus.PASSED, [Evidence("runtime_upgrade", proof)], [],
                                      started, datetime.now(timezone.utc).isoformat())
                    if not persist_harness_run_complete(ctx, harness_run_id=run_id, status="passed",
                                                        finished_at=gate.finished_at, results=[gate]):
                        raise RuntimeError("W2 Harness completion failed; retain execution, never repeat")
                    runs[key] = run_id
                except BaseException as error:
                    if isinstance(error, ProcessGroupTerminationError):
                        view.mark_termination_uncertain()
                    failure = {"harness_run_id": run_id, "error_type": type(error).__name__,
                               "error": str(error)[:16384], "algorithm_started": bool(process), "process": process,
                               "elapsed_seconds": timing.monotonic() - started_clock if started_clock is not None else None,
                               "stderr": completed.stderr[:8192] if completed is not None else None,
                               "inspect_retained_artifacts": True}
                    try:
                        if output.is_file() and not output.is_symlink() and output.stat().st_size <= profile.max_output_bytes:
                            failure["result_sha256"] = _sha256_file(output)
                        _write_receipt(work_dir / f"{key}.failed.json", failure)
                    except BaseException:
                        error.add_note("Failed file receipt could not be written; original failure retained.")
                    _record_failed_gate(engine, ctx, run_id, started, error)
                    raise
        result = {"harness_run_ids": runs, "plan_sha256": expected_plan_sha256,
                  "algorithm_executions": len(ids), "prediction_written": False, "state_written": False,
                  "registry_changed": False}
        _write_receipt(work_dir / "complete.json", result)
        return result


def _record_failed_gate(engine, ctx, run_id, started, error):
    """先读提交结果；只结束确定 running 且尚无 Gate 的本次审计。"""
    try:
        with engine.connect() as conn:
            runs = _same_id_rows_conn(conn, "t_harness_runs", "harness_run_id=:run",
                {"run": run_id}, order="harness_run_id", for_update=False)
            gates = _same_id_rows_conn(conn, "t_harness_gate_results", "harness_run_id=:run",
                {"run": run_id}, order="id", for_update=False)
        if len(runs) != 1 or runs[0]["status"] != "running" or gates:
            return
        expected = {"scheme_id": ctx.scheme_id, "scheme_version": ctx.config.scheme_version,
                    "code_hash": ctx.config.code_hash, "config_hash": ctx.config.config_hash, "stage": _STAGE}
        if any(runs[0].get(key) != value for key, value in expected.items()):
            raise RuntimeError("failed preparation audit identity differs")
        failed = GateResult(_STAGE, GateStatus.FAILED, [], ["preparation failed; inspect retained artifacts"],
                           started, datetime.now(timezone.utc).isoformat())
        if not persist_harness_run_complete(ctx, harness_run_id=run_id, status="failed",
                                           finished_at=failed.finished_at, results=[failed]):
            error.add_note("Failed Harness completion unavailable; inspect retained evidence.")
    except BaseException:
        error.add_note("Harness commit outcome unavailable; no blind audit rewrite attempted.")
