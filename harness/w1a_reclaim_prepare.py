"""W1A 两次通用多目标标准调用、六进程的本机准备；不写业务事实。"""

from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from harness import w1a_writer_reclaim as control
from harness.blackbox_v2.gates import validate_canonical_blackbox_delivery
from harness.context import GateContext
from harness.persistence import new_harness_run_id, persist_harness_run_start, persist_harness_run_complete
from harness.result import Evidence, GateResult, GateStatus
from harness.w2_reclaim_prepare import _record_failed_gate, _request_date
from harness.w3b_prepare import _gate_proof, _write_receipt
from scheduler import repository
from scheduler.blackbox_v2_runner import DEFAULT_RUNTIME_PROFILE
from scheduler.executor import BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF, run_blackbox_scheme_subprocess
from shared.blackbox_v2.requests import build_live_request, resolve_live_context
from shared.calendar_service import get_calendar
from shared.input_artifacts import resolve_blackbox_input_cutoffs


def _capture(engine, *, project_root, reference_project_root, rollback_project_root, predict_date,
             expected_database_name, expected_server_uuid, permitted_runs=()):
    old, sources, new, snapshot, evidence = control.capture(project_root, reference_project_root,
                                                          rollback_project_root, preparing=True)
    calendar = get_calendar(engine)
    day = _request_date(calendar, predict_date)
    requests = {}
    for key in control.IDS:
        validate_canonical_blackbox_delivery(new[key])
        requests[key] = {}
        for delivery in new[key].blackbox_deliveries:
            metadata = delivery.metadata
            feature = resolve_live_context(metadata, predict_date=day, calendar=calendar).feature_date
            cutoffs = resolve_blackbox_input_cutoffs(snapshot, feature_date=feature, engine=engine)
            requests[key][metadata.target_tenor] = asdict(build_live_request(metadata,
                predict_date=day, calendar=calendar, cutoffs=cutoffs))
    plan = repository.read_w1a_writer_reclaim_plan(engine, old_configs=old, source_configs=sources, new_configs=new,
        harness_run_ids={}, action="prepare", control_plane_evidence=evidence,
        expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid, permitted_runs=permitted_runs)
    plan["requests"] = requests
    plan["predict_date"] = day
    return old, new, snapshot, plan


def build_w1a_reclaim_prepare_preflight(engine, **kwargs):
    """只读预检，不启动算法、修改 current 或创建 Harness。"""
    plan = _capture(engine, **kwargs)[-1]
    return {"plan": plan, "plan_sha256": repository.native_successor_plan_sha256(plan)}


def execute_w1a_reclaim_prepare(engine, *, expected_plan_sha256, approved_by, work_dir, **kwargs):
    """daily 围栏与八锁内逐 base 调用；失败保全并停止，不自动重试。"""
    if (not isinstance(approved_by, str) or not approved_by.strip()
            or not isinstance(expected_plan_sha256, str) or len(expected_plan_sha256) != 64
            or any(c not in "0123456789abcdef" for c in expected_plan_sha256)):
        raise ValueError("W1A preparation requires operator and exact approved plan SHA-256")
    work = Path(work_dir)
    if not work.is_absolute() or work != work.resolve(strict=False) or os.path.lexists(work):
        raise ValueError("W1A requires a fresh absolute symlink-free work directory")
    with ExitStack() as locks:
        for key in control.ALL_IDS:
            locks.enter_context(repository._blackbox_activation_advisory_lock(engine, scheme_id=key))
        old, new, snapshot, plan = _capture(engine, **kwargs)
        if repository.native_successor_plan_sha256(plan) != expected_plan_sha256:
            raise RuntimeError("W1A preparation plan changed before execution")
        forbidden = [Path(kwargs[key]).resolve().parent for key in
                     ("project_root", "reference_project_root", "rollback_project_root")]
        forbidden += [snapshot.root_dir.resolve(), snapshot.data_dir.resolve()]
        if any(work.is_relative_to(path) for path in forbidden):
            raise ValueError("W1A work directory must be outside every release and input generation")
        work.mkdir(mode=0o700)
        _write_receipt(work / "plan.json", json.loads(repository.canonical_native_successor_plan(plan)))
        _write_receipt(work / "operator.json", {"approved_by": approved_by.strip()})
        runs, execution_hashes = {}, {}
        profile = replace(DEFAULT_RUNTIME_PROFILE, predict_timeout_sec=120, cpu_threads=8, memory_limit_bytes=4 * 1024**3)
        for key in control.IDS:
            run_id = new_harness_run_id()
            started = datetime.now(timezone.utc).isoformat()
            ctx = GateContext(key, plan["predict_date"], Path(kwargs["project_root"]), config=new[key], engine_factory=lambda: engine)
            processes = []
            started_clock = None
            records = None

            def record_process(pid, pgid):
                index = len(processes)
                if index >= len(new[key].blackbox_deliveries):
                    raise RuntimeError("W1A unexpected extra algorithm process")
                process = {"pid": pid, "pgid": pgid, "target_tenor": new[key].tenors[index],
                           "started_at": datetime.now(timezone.utc).isoformat()}
                processes.append(process)
                _write_receipt(work / f"{key}.{index}.process.json", process)

            try:
                _write_receipt(work / f"{key}.started.json", {"harness_run_id": run_id, "started_at": started,
                               "requests": plan["requests"][key], "plan_sha256": expected_plan_sha256})
                if not persist_harness_run_start(ctx, harness_run_id=run_id, stage="native-runtime-upgrade", started_at=started):
                    raise RuntimeError("W1A Harness start failed; no algorithm started")
                permitted = tuple((*runs.values(), run_id))
                if _capture(engine, **kwargs, permitted_runs=permitted)[-1] != plan:
                    raise RuntimeError("W1A preparation context changed before execution")
                started_clock = time.monotonic()
                records = run_blackbox_scheme_subprocess(new[key], plan["predict_date"], engine=engine,
                    algo_env=profile.conda_env, timeout_sec=120, profile=profile,
                    snapshot_mode=BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF,
                    expected_generation_id=snapshot.generation_id, expected_refresh_date=snapshot.refresh_date,
                    process_started=record_process)
                if len(processes) != len(new[key].tenors) or len(records) != len(new[key].tenors):
                    raise RuntimeError("W1A incomplete target execution")
                for record, tenor in zip(records, new[key].tenors, strict=True):
                    request = plan["requests"][key][tenor]
                    if (record.scheme_id != key or record.target_tenor != tenor or record.horizon != new[key].horizon
                            or type(record.predicted_direction) is not int or record.predicted_direction not in {-1, 0, 1}
                            or any(getattr(record, field) != request[field] for field in ("predict_date", "feature_date", "target_date"))
                            or record.extra.get("request_id") != request["request_id"]
                            or record.extra.get("data_snapshot_id") != snapshot.snapshot_id):
                        raise RuntimeError("W1A standard Result or target/input echo differs")
                execution = {"scheme_id": key, "scheme_version": new[key].scheme_version,
                             "requests": plan["requests"][key], "records": [asdict(record) for record in records],
                             "input": plan["control"]["databridge"], "environment_fingerprint": new[key].environment_fingerprint,
                             "elapsed_seconds": time.monotonic() - started_clock, "processes": processes,
                             "algorithm_executions": len(processes), "prediction_written": False, "state_written": False}
                execution_hashes[key] = _write_receipt(work / f"{key}.execution.json", execution)
                if _capture(engine, **kwargs, permitted_runs=permitted)[-1] != plan:
                    raise RuntimeError("W1A preparation context changed; retain artifacts and stop")
                proof = _gate_proof(old[key], new[key], {"input": plan["control"]["databridge"],
                                   "equivalence": plan["control"]["equivalence"]}, key, execution_hashes[key])
                gate = GateResult("native-runtime-upgrade", GateStatus.PASSED, [Evidence("runtime_upgrade", proof)], [],
                                  started, datetime.now(timezone.utc).isoformat())
                if not persist_harness_run_complete(ctx, harness_run_id=run_id, status="passed", finished_at=gate.finished_at, results=[gate]):
                    raise RuntimeError("W1A Harness completion uncertain; retain artifacts, never repeat")
                runs[key] = run_id
            except BaseException as error:
                try:
                    _write_receipt(work / f"{key}.failed.json", {"harness_run_id": run_id, "error_type": type(error).__name__,
                        "error": str(error)[:16384], "algorithm_started": bool(processes), "processes": processes,
                        "elapsed_seconds": time.monotonic() - started_clock if started_clock is not None else None,
                        "records": [asdict(record) for record in records] if records is not None else None})
                except BaseException:
                    error.add_note("Failed file receipt unavailable; original error retained.")
                _record_failed_gate(engine, ctx, run_id, started, error)
                raise
        result = {"harness_run_ids": runs, "local_execution_sha256": execution_hashes,
                  "plan_sha256": expected_plan_sha256, "algorithm_executions": 6,
                  "prediction_written": False, "state_written": False, "registry_changed": False}
        _write_receipt(work / "complete.json", result)
        return result
