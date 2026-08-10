from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from harness.signal_gap_plan import PLAN_SCHEMA_VERSION, plan_signal_gaps
from scheduler.discovery import load_scheme_config
from scheduler.executor import (
    run_blackbox_gray_replay_batch,
    run_configured_scheme,
)
from shared.blackbox_v2.contracts import BlackboxRequest
from shared.blackbox_v2.requests import build_request
from shared.blackbox_v2.snapshot import CutoffKeys
from shared.data_bridge.authority import (
    _normalize_blackbox_gray_replay_source_identity,
)
from shared.data_bridge.refresh import (
    DataBridgeRefreshConfig,
    _ensure_private_directory,
)
from shared.exclusive_file_lock import (
    ExclusiveFileLock,
    ExclusiveFileLockUnavailable,
)
from shared.input_artifacts import (
    BlackboxGrayReplaySession,
    build_blackbox_gray_replay_session,
)
from shared.models import PredictionRecord
from shared.runtime_paths import resolve_runtime_artifact_root


VINTAGE_DISCLAIMER = "current_snapshot_as_of_not_historical_vintage"
_TARGET_KEY_FIELDS = (
    "registry_scheme_id",
    "base_scheme_id",
    "target_tenor",
    "horizon",
    "task_type",
    "predict_date",
    "feature_date",
    "target_date",
    "prediction_phase",
)
_DUE_ACTIONS = frozenset({"GRAY_LIVE_GAP", "SKIP_PRESENT"})


@dataclass(frozen=True, slots=True)
class _GapGroup:
    base_scheme_id: str
    predict_date: str
    runtime_type: str
    scheme_version: str
    actions: tuple[dict[str, Any], ...]
    missing_actions: tuple[dict[str, Any], ...]
    expected_target_keys: tuple[dict[str, Any], ...]
    missing_target_keys: tuple[dict[str, Any], ...]
    input_authority: dict[str, Any] | None
    source_authority: dict[str, Any] | None

    @property
    def identity(self) -> tuple[str, str]:
        return self.base_scheme_id, self.predict_date


@dataclass(slots=True)
class _Execution:
    group: _GapGroup
    cfg: Any
    run_id: int
    started: float
    records: list[PredictionRecord] | None = None
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _BlackboxReplayBatch:
    source_identity: dict[str, Any]
    session_id: str
    cfg: Any
    executions: tuple[_Execution, ...]
    requests: tuple[BlackboxRequest, ...]


def run_signal_gap_fill(
    *,
    plan: Mapping[str, Any],
    project_root: Path,
    engine_factory: Callable[[], Any],
    databridge_config: DataBridgeRefreshConfig,
    algo_env: str = "forecast_env",
    timeout_sec: int = 600,
) -> dict[str, Any]:
    """按一次内存单日计划协调缺口计算、原子提交与最终权威读回。"""
    try:
        normalized_plan = _normalize_plan(plan)
        groups = _build_groups(normalized_plan)
    except (KeyError, TypeError, ValueError) as exc:
        return _report(
            "BLOCKED",
            plan,
            groups=(),
            failure_code="PLAN_GROUP_INVALID",
            errors=[str(exc)],
        )
    if normalized_plan["status"] == "BLOCKED":
        return _report(
            "BLOCKED",
            normalized_plan,
            groups=groups,
            failure_code=str(
                normalized_plan.get("failure_code")
                or "SIGNAL_GAP_PLAN_BLOCKED"
            ),
        )
    if not groups:
        status = (
            "SKIP_NOT_DUE"
            if int(normalized_plan["counts"].get("expected", 0)) == 0
            else "SKIP_PRESENT"
        )
        return _report(
            status,
            normalized_plan,
            groups=(),
            failure_code=None,
        )

    try:
        singleton_lock = _signal_gap_fill_singleton_lock()
        singleton_lock.acquire()
    except ExclusiveFileLockUnavailable:
        return _report(
            "BLOCKED",
            normalized_plan,
            groups=groups,
            failure_code="SIGNAL_GAP_FILL_ALREADY_RUNNING",
        )
    except Exception as exc:  # noqa: BLE001
        return _report(
            "BLOCKED",
            normalized_plan,
            groups=groups,
            failure_code="SIGNAL_GAP_FILL_LOCK_INVALID",
            errors=[f"{type(exc).__name__}: {exc}"],
        )

    engine = None
    try:
        try:
            engine = engine_factory()
        except Exception as exc:  # noqa: BLE001
            return _report(
                "BLOCKED",
                normalized_plan,
                groups=groups,
                failure_code="ENGINE_FACTORY_FAILED",
                errors=[f"{type(exc).__name__}: {exc}"],
                remaining=_remaining(groups),
            )
        repository = _repository_module()
        try:
            configs = _load_and_validate_configs(
                groups,
                project_root=Path(project_root),
            )
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            return _report(
                "BLOCKED",
                normalized_plan,
                groups=groups,
                failure_code="DISCOVERY_IDENTITY_DRIFT",
                errors=[str(exc)],
            )
        run_creation_cleanup_errors: list[str] = []
        try:
            executions = _create_runs(
                groups,
                configs=configs,
                engine=engine,
                repository=repository,
                cleanup_errors=run_creation_cleanup_errors,
            )
        except Exception as exc:  # noqa: BLE001
            return _report(
                "FAILED",
                normalized_plan,
                groups=groups,
                failure_code="RUN_CREATION_FAILED",
                errors=[
                    f"{type(exc).__name__}: {exc}",
                    *run_creation_cleanup_errors,
                ],
                remaining=_remaining(groups),
            )

        try:
            _run_algorithms(
                executions,
                engine=engine,
                algo_env=algo_env,
                timeout_sec=timeout_sec,
                data_bridge_config=databridge_config,
            )
        except Exception as exc:  # noqa: BLE001
            failure_errors = _fail_executions(
                executions,
                engine=engine,
                repository=repository,
                default_error="ALGORITHM_COORDINATOR_FAILED",
            )
            return _report(
                "FAILED",
                normalized_plan,
                groups=groups,
                failure_code="ALGORITHM_COORDINATOR_FAILED",
                errors=[f"{type(exc).__name__}: {exc}", *failure_errors],
                remaining=_remaining(groups),
            )
        failed = [item for item in executions if item.error is not None]
        if failed:
            failure_errors = _fail_executions(
                executions,
                engine=engine,
                repository=repository,
                default_error="SIGNAL_GAP_FILL_BATCH_ABORTED",
            )
            return _report(
                "FAILED",
                normalized_plan,
                groups=groups,
                failure_code="ALGORITHM_EXECUTION_FAILED",
                errors=[
                    *(
                        f"{item.group.base_scheme_id}: "
                        f"{type(item.error).__name__}: {item.error}"
                        for item in failed
                    ),
                    *failure_errors,
                ],
                remaining=_remaining(groups),
            )

        completed: list[dict[str, Any]] = []
        completed_ids: set[tuple[str, str]] = set()
        for index, item in enumerate(executions):
            try:
                records = item.records or []
                written = repository.complete_gray_gap_run(
                    engine,
                    item.cfg,
                    run_id=item.run_id,
                    records=records,
                    expected_target_keys=list(
                        item.group.missing_target_keys
                    ),
                    source_authority=item.group.source_authority,
                    records_returned=len(records),
                    run_date=item.group.predict_date,
                    duration_sec=time.monotonic() - item.started,
                )
            except Exception as exc:  # noqa: BLE001
                item.error = exc
                unfinished = executions[index:]
                failure_errors = _fail_executions(
                    unfinished,
                    engine=engine,
                    repository=repository,
                    default_error="GRAY_GAP_COMMIT_FAILED",
                )
                remaining_groups = [
                    group
                    for group in groups
                    if group.identity not in completed_ids
                ]
                return _report(
                    "FAILED",
                    normalized_plan,
                    groups=groups,
                    failure_code="GRAY_GAP_COMMIT_FAILED",
                    errors=[
                        f"{type(exc).__name__}: {exc}",
                        *failure_errors,
                    ],
                    completed=completed,
                    remaining=_remaining(remaining_groups),
                )
            completed.append(
                {
                    "base_scheme_id": item.group.base_scheme_id,
                    "predict_date": item.group.predict_date,
                    "run_id": item.run_id,
                    "records_written": int(written),
                }
            )
            completed_ids.add(item.group.identity)

        try:
            readback = plan_signal_gaps(
                engine,
                predict_date=str(normalized_plan["predict_date"]),
                base_scheme_id=normalized_plan.get("base_scheme_id"),
                databridge_config=databridge_config,
            )
        except Exception as exc:  # noqa: BLE001
            return _report(
                "FAILED",
                normalized_plan,
                groups=groups,
                failure_code="POSTFILL_READBACK_FAILED",
                errors=[f"{type(exc).__name__}: {exc}"],
                completed=completed,
                remaining=_remaining(groups),
            )
        remaining_groups = _remaining_groups_after_readback(groups, readback)
        if remaining_groups:
            return _report(
                "FAILED",
                normalized_plan,
                groups=groups,
                failure_code="POSTFILL_GAPS_REMAIN",
                completed=completed,
                remaining=_remaining(remaining_groups),
            )
        return _report(
            "PASSED",
            normalized_plan,
            groups=groups,
            failure_code=None,
            completed=completed,
        )
    finally:
        try:
            if engine is not None and hasattr(engine, "dispose"):
                engine.dispose()
        finally:
            singleton_lock.release()


def _normalize_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise TypeError("signal gap plan must be a mapping")
    normalized = dict(plan)
    if normalized.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ValueError("signal gap plan schema is invalid")
    if normalized.get("status") not in {"READY", "BLOCKED"}:
        raise ValueError("signal gap plan status is invalid")
    if not isinstance(normalized.get("predict_date"), str):
        raise ValueError("signal gap plan predict_date is invalid")
    if normalized.get("base_scheme_id") is not None and not isinstance(
        normalized.get("base_scheme_id"), str
    ):
        raise ValueError("signal gap plan base_scheme_id is invalid")
    if not isinstance(normalized.get("counts"), Mapping):
        raise ValueError("signal gap plan counts are invalid")
    if not isinstance(normalized.get("actions"), list):
        raise ValueError("signal gap plan actions are invalid")
    if normalized["status"] == "READY" and int(
        normalized["counts"].get("blocked", 0)
    ):
        raise ValueError("ready signal gap plan contains blockers")
    return normalized


def _build_groups(plan: Mapping[str, Any]) -> tuple[_GapGroup, ...]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw in plan["actions"]:
        if not isinstance(raw, Mapping):
            raise ValueError("signal gap plan action must be a mapping")
        row = dict(raw)
        action = row.get("action")
        if action == "SKIP_NOT_DUE":
            continue
        if action not in _DUE_ACTIONS:
            raise ValueError(f"signal gap plan action is not executable: {action}")
        if row.get("prediction_phase") != "gray_live":
            raise ValueError("signal gap action must use gray_live")
        if action == "GRAY_LIVE_GAP" and row.get(
            "business_key_present"
        ) is not False:
            raise ValueError("signal gap action is not missing")
        if action == "SKIP_PRESENT" and row.get(
            "business_key_present"
        ) is not True:
            raise ValueError("present signal action is invalid")
        identity = (
            str(row.get("base_scheme_id") or ""),
            str(row.get("predict_date") or ""),
        )
        if not all(identity) or identity[1] != plan["predict_date"]:
            raise ValueError("signal gap action group identity is invalid")
        grouped.setdefault(identity, []).append(row)

    result: list[_GapGroup] = []
    for identity, rows in sorted(grouped.items()):
        ordered = tuple(sorted(rows, key=_action_sort_key))
        for field in (
            "runtime_type",
            "scheme_version",
            "frequency",
            "task_type",
            "horizon",
            "feature_date",
            "target_date",
        ):
            if len({str(row.get(field)) for row in ordered}) != 1:
                raise ValueError(f"signal gap group {field} is not atomic")
        runtime_type = str(ordered[0].get("runtime_type") or "")
        if runtime_type not in {"native_adapter", "blackbox_v2"}:
            raise ValueError("signal gap runtime_type is invalid")
        expected = tuple(_expected_target_key(row) for row in ordered)
        if len({_target_identity(row) for row in expected}) != len(expected):
            raise ValueError("signal gap group target set contains duplicates")
        missing_actions = tuple(
            row for row in ordered if row["action"] == "GRAY_LIVE_GAP"
        )
        if not missing_actions:
            continue
        missing = tuple(_expected_target_key(row) for row in missing_actions)
        if runtime_type == "native_adapter":
            if any(row.get("input_authority") is not None for row in ordered):
                raise ValueError("Native signal gap authority must be null")
            input_authority = None
            source_authority = None
        else:
            authorities = {
                _canonical_json(row.get("input_authority"))
                for row in missing_actions
            }
            if len(authorities) != 1:
                raise ValueError("Blackbox signal gap authority is not atomic")
            raw_authority = json.loads(next(iter(authorities)))
            input_authority = _normalize_blackbox_action_authority(
                raw_authority,
                feature_date=str(ordered[0]["feature_date"]),
                predict_date=identity[1],
            )
            source_authority = _repository_source_authority(
                input_authority,
                feature_date=str(ordered[0]["feature_date"]),
            )
        result.append(
            _GapGroup(
                base_scheme_id=identity[0],
                predict_date=identity[1],
                runtime_type=runtime_type,
                scheme_version=str(ordered[0]["scheme_version"]),
                actions=ordered,
                missing_actions=missing_actions,
                expected_target_keys=expected,
                missing_target_keys=missing,
                input_authority=input_authority,
                source_authority=source_authority,
            )
        )
    return tuple(result)


def _load_and_validate_configs(
    groups: Sequence[_GapGroup],
    *,
    project_root: Path,
) -> dict[tuple[str, str], Any]:
    configs: dict[tuple[str, str], Any] = {}
    for group in groups:
        cfg = load_scheme_config(
            project_root
            / "schemes"
            / group.base_scheme_id
            / "config.yaml"
        )
        actual = {
            "scheme_id": str(getattr(cfg, "scheme_id", "")),
            "scheme_version": str(getattr(cfg, "scheme_version", "")),
            "runtime_type": str(getattr(cfg, "runtime_type", "")),
            "status": str(getattr(cfg, "status", "")),
            "version_status": str(getattr(cfg, "version_status", "")),
            "tenors": sorted(str(item) for item in getattr(cfg, "tenors", ())),
        }
        expected = {
            "scheme_id": group.base_scheme_id,
            "scheme_version": group.scheme_version,
            "runtime_type": group.runtime_type,
            "status": "active",
            "version_status": "active",
            "tenors": sorted(
                str(row["target_tenor"])
                for row in group.expected_target_keys
            ),
        }
        if actual != expected:
            raise ValueError(
                f"discovery identity drift for {group.base_scheme_id}"
            )
        configs[group.identity] = cfg
    return configs


def _create_runs(
    groups: Sequence[_GapGroup],
    *,
    configs: Mapping[tuple[str, str], Any],
    engine: Any,
    repository: Any,
    cleanup_errors: list[str],
) -> list[_Execution]:
    executions: list[_Execution] = []
    try:
        for group in groups:
            cfg = configs[group.identity]
            run_id = repository.create_scheme_run(
                engine,
                scheme_id=group.base_scheme_id,
                predict_date=group.predict_date,
                scheme_version=group.scheme_version,
                runtime_type=group.runtime_type,
                run_type="active",
                prediction_phase="gray_live",
                records_expected=len(group.missing_target_keys),
            )
            executions.append(
                _Execution(
                    group=group,
                    cfg=cfg,
                    run_id=int(run_id),
                    started=time.monotonic(),
                )
            )
    except Exception as exc:
        cleanup_errors.extend(
            _fail_executions(
                executions,
                engine=engine,
                repository=repository,
                default_error=f"RUN_CREATION_ABORTED:{type(exc).__name__}",
            )
        )
        raise
    return executions


def _run_algorithms(
    executions: Sequence[_Execution],
    *,
    engine: Any,
    algo_env: str,
    timeout_sec: int,
    data_bridge_config: DataBridgeRefreshConfig,
) -> None:
    blackbox = [
        item
        for item in executions
        if item.group.runtime_type == "blackbox_v2"
    ]
    _run_blackbox_gray_replay_batches(
        blackbox,
        engine=engine,
        algo_env=algo_env,
        timeout_sec=timeout_sec,
        data_bridge_config=data_bridge_config,
    )
    if any(item.error is not None for item in blackbox):
        return

    native = [
        item
        for item in executions
        if item.group.runtime_type == "native_adapter"
    ]
    if not native:
        return
    with tempfile.TemporaryDirectory(prefix="bfl-native-gap-") as temporary:
        os.chmod(temporary, 0o700)
        root = Path(temporary).resolve(strict=True)
        futures: dict[Future[list[PredictionRecord]], _Execution] = {}
        with ThreadPoolExecutor(
            max_workers=min(2, len(native)),
            thread_name_prefix="signal-gap-native",
        ) as pool:
            for item in native:
                scheme_root = root / item.group.base_scheme_id
                scheme_root.mkdir(mode=0o700)
                futures[
                    pool.submit(
                        _run_native_algorithm,
                        item,
                        engine=engine,
                        algo_env=algo_env,
                        timeout_sec=timeout_sec,
                        ephemeral_native_runtime_root=scheme_root,
                    )
                ] = item
            for future, item in futures.items():
                try:
                    item.records = future.result()
                except Exception as exc:  # noqa: BLE001
                    item.error = exc


def _run_native_algorithm(
    item: _Execution,
    *,
    engine: Any,
    algo_env: str,
    timeout_sec: int,
    ephemeral_native_runtime_root: Path,
) -> list[PredictionRecord]:
    records = run_configured_scheme(
        item.cfg,
        item.group.predict_date,
        engine=engine,
        algo_env=algo_env,
        timeout_sec=timeout_sec,
        ephemeral_native_runtime_root=ephemeral_native_runtime_root,
    )
    normalized = [
        replace(
            record,
            prediction_phase="gray_live",
            scheme_version=(
                item.group.scheme_version
                if record.scheme_version is None
                else record.scheme_version
            ),
            extra=_strip_native_ephemeral_provenance(record.extra),
        )
        for record in records
    ]
    return _validate_and_select_missing_records(item.group, normalized)


def _run_blackbox_gray_replay_batches(
    executions: Sequence[_Execution],
    *,
    engine: Any,
    algo_env: str,
    timeout_sec: int,
    data_bridge_config: DataBridgeRefreshConfig,
) -> None:
    batches = _build_blackbox_replay_batches(executions)
    if any(item.error is not None for item in executions):
        return
    by_source: dict[str, list[_BlackboxReplayBatch]] = {}
    for batch in batches:
        by_source.setdefault(_canonical_json(batch.source_identity), []).append(
            batch
        )
    sessions: dict[str, BlackboxGrayReplaySession] = {}
    for source_key in sorted(by_source):
        source_batches = by_source[source_key]
        try:
            sessions[source_key] = build_blackbox_gray_replay_session(
                session_id=source_batches[0].session_id,
                source_identity=source_batches[0].source_identity,
                request_cutoffs=tuple(
                    cutoff
                    for batch in source_batches
                    for cutoff in _cutoffs_for_requests(batch.requests)
                ),
                data_bridge_config=data_bridge_config,
            )
        except Exception as exc:  # noqa: BLE001
            for batch in source_batches:
                for item in batch.executions:
                    item.error = exc
            return

    for source_key in sorted(by_source):
        session = sessions[source_key]
        for batch in by_source[source_key]:
            try:
                records = run_blackbox_gray_replay_batch(
                    batch.cfg,
                    requests=batch.requests,
                    session=session,
                    engine=engine,
                    algo_env=algo_env,
                    timeout_sec=timeout_sec,
                )
                _fan_out_blackbox_batch_records(batch, records)
            except Exception as exc:  # noqa: BLE001
                for item in batch.executions:
                    item.error = exc


def _build_blackbox_replay_batches(
    executions: Sequence[_Execution],
) -> tuple[_BlackboxReplayBatch, ...]:
    sources: dict[
        str,
        tuple[dict[str, Any], list[tuple[_Execution, BlackboxRequest]]],
    ] = {}
    for item in executions:
        try:
            authority = item.group.input_authority
            if authority is None:
                raise ValueError("Blackbox signal gap authority is missing")
            source_identity = {
                key: authority[key]
                for key in (
                    "generation_id",
                    "refresh_date",
                    "schema_version",
                    "business_digest",
                    "stable_identity_sha256",
                    "files",
                )
            }
            request = _blackbox_request(item.group, authority)
        except Exception as exc:  # noqa: BLE001
            item.error = exc
            continue
        source_key = _canonical_json(source_identity)
        if source_key not in sources:
            sources[source_key] = (source_identity, [])
        sources[source_key][1].append((item, request))

    result: list[_BlackboxReplayBatch] = []
    for source_key in sorted(sources):
        source_identity, entries = sources[source_key]
        session_id = _blackbox_replay_session_id(
            source_identity,
            [request for _, request in entries],
        )
        by_config: dict[str, list[tuple[_Execution, BlackboxRequest]]] = {}
        for item, request in entries:
            by_config.setdefault(item.group.base_scheme_id, []).append(
                (item, request)
            )
        for base_scheme_id in sorted(by_config):
            batch_entries = by_config[base_scheme_id]
            result.append(
                _BlackboxReplayBatch(
                    source_identity=source_identity,
                    session_id=session_id,
                    cfg=batch_entries[0][0].cfg,
                    executions=tuple(item for item, _ in batch_entries),
                    requests=tuple(request for _, request in batch_entries),
                )
            )
    return tuple(result)


def _fan_out_blackbox_batch_records(
    batch: _BlackboxReplayBatch,
    records: Sequence[PredictionRecord],
) -> None:
    expected = {
        request.request_id: item
        for item, request in zip(batch.executions, batch.requests)
    }
    by_request: dict[str, PredictionRecord] = {}
    for record in records:
        request_id = (record.extra or {}).get("request_id")
        if not isinstance(request_id, str) or request_id not in expected:
            raise ValueError("Blackbox replay returned unexpected request_id")
        if request_id in by_request:
            raise ValueError("Blackbox replay returned duplicate request_id")
        by_request[request_id] = record
    if set(by_request) != set(expected):
        raise ValueError("Blackbox replay returned missing request_id")
    for request_id, item in expected.items():
        normalized = replace(
            by_request[request_id],
            prediction_phase="gray_live",
            scheme_version=(
                item.group.scheme_version
                if by_request[request_id].scheme_version is None
                else by_request[request_id].scheme_version
            ),
        )
        item.records = _validate_and_select_missing_records(
            item.group,
            [normalized],
        )


def _validate_and_select_missing_records(
    group: _GapGroup,
    records: Sequence[PredictionRecord],
) -> list[PredictionRecord]:
    expected = {_target_identity(row) for row in group.expected_target_keys}
    actual = {_record_identity(record) for record in records}
    if len(records) != len(expected) or actual != expected:
        raise ValueError(
            "algorithm records do not match complete active target group"
        )
    for record in records:
        if (
            record.scheme_id != group.base_scheme_id
            or record.scheme_version != group.scheme_version
            or record.prediction_phase != "gray_live"
        ):
            raise ValueError("algorithm record execution identity is invalid")
        if group.runtime_type == "blackbox_v2":
            _validate_blackbox_record(record, group)
    missing = {
        _target_identity(row) for row in group.missing_target_keys
    }
    return [record for record in records if _record_identity(record) in missing]


def _validate_blackbox_record(
    record: PredictionRecord,
    group: _GapGroup,
) -> None:
    authority = group.input_authority or {}
    cutoff = authority.get("cutoff") or {}
    extra = dict(record.extra or {})
    expected = {
        "data_generation_id": authority.get("generation_id"),
        "source_refresh_date": authority.get("refresh_date"),
        "daily_cutoff_key": cutoff.get("daily_cutoff_key"),
        "weekly_cutoff_key": cutoff.get("weekly_cutoff_key"),
        "monthly_cutoff_key": cutoff.get("monthly_cutoff_key"),
    }
    if any(extra.get(field) != value for field, value in expected.items()):
        raise ValueError("Blackbox replay record provenance is invalid")
    if extra["daily_cutoff_key"] != record.feature_date:
        raise ValueError("Blackbox replay daily cutoff must equal feature_date")


def _normalize_blackbox_action_authority(
    authority: Any,
    *,
    feature_date: str,
    predict_date: str,
) -> dict[str, Any]:
    if not isinstance(authority, Mapping):
        raise ValueError("Blackbox input authority is missing")
    cutoff = authority.get("cutoff")
    if not isinstance(cutoff, Mapping):
        raise ValueError("Blackbox input authority cutoff is missing")
    source = _normalize_blackbox_gray_replay_source_identity(
        {key: value for key, value in authority.items() if key != "cutoff"}
    )
    expected_cutoff_fields = {
        "feature_date",
        "daily_cutoff_key",
        "weekly_cutoff_key",
        "monthly_cutoff_key",
        "source_weekly_cutoff_key",
        "source_monthly_cutoff_key",
    }
    if set(cutoff) != expected_cutoff_fields:
        raise ValueError("Blackbox input authority cutoff schema is invalid")
    if (
        cutoff["feature_date"] != feature_date
        or cutoff["daily_cutoff_key"] != feature_date
        or source["refresh_date"] < predict_date
    ):
        raise ValueError("Blackbox input authority cutoff is invalid")
    return json.loads(
        _canonical_json({**source, "cutoff": dict(cutoff)})
    )


def _repository_source_authority(
    authority: Mapping[str, Any],
    *,
    feature_date: str,
) -> dict[str, Any]:
    return {
        "authority_type": "databridge_current_generation",
        "generation_id": str(authority["generation_id"]),
        "manifest_sha256": str(authority["stable_identity_sha256"]),
        "refresh_date": str(authority["refresh_date"]),
        "cutoff_date": feature_date,
        "replay_mode": "historical_as_of_replay",
        "vintage_disclaimer": VINTAGE_DISCLAIMER,
    }


def _blackbox_request(
    group: _GapGroup,
    authority: Mapping[str, Any],
) -> BlackboxRequest:
    action = group.actions[0]
    cutoff = authority["cutoff"]
    return build_request(
        scheme_id=group.base_scheme_id,
        predict_date=group.predict_date,
        feature_date=str(action["feature_date"]),
        target_date=str(action["target_date"]),
        cutoffs=CutoffKeys(
            daily_cutoff_key=str(cutoff["daily_cutoff_key"]),
            weekly_cutoff_key=str(cutoff["weekly_cutoff_key"]),
            monthly_cutoff_key=str(cutoff["monthly_cutoff_key"]),
        ),
    )


def _blackbox_replay_session_id(
    source_identity: Mapping[str, Any],
    requests: Sequence[BlackboxRequest],
) -> str:
    payload = _canonical_json(
        {
            "source_identity": source_identity,
            "cutoff_signatures": sorted(
                {
                    (
                        request.daily_cutoff_key,
                        request.weekly_cutoff_key,
                        request.monthly_cutoff_key,
                    )
                    for request in requests
                }
            ),
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cutoffs_for_requests(
    requests: Sequence[BlackboxRequest],
) -> tuple[CutoffKeys, ...]:
    return tuple(
        CutoffKeys(
            daily_cutoff_key=request.daily_cutoff_key,
            weekly_cutoff_key=request.weekly_cutoff_key,
            monthly_cutoff_key=request.monthly_cutoff_key,
        )
        for request in requests
    )


_NATIVE_EPHEMERAL_PROVENANCE_FIELDS = frozenset(
    {
        "input_artifact_path",
        "input_artifact_source",
        "input_artifact_data_version",
        "input_artifact_watermark",
        "input_cutoff_date",
        "phase_a_cache",
    }
)
_NATIVE_EPHEMERAL_PROVENANCE_PREFIXES = (
    "daily_input_artifact_",
    "weekly_input_artifact_",
    "monthly_input_artifact_",
    "phase_a_cache_",
)


def _strip_native_ephemeral_provenance(
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(extra or {}).items()
        if key not in _NATIVE_EPHEMERAL_PROVENANCE_FIELDS
        and not key.startswith(_NATIVE_EPHEMERAL_PROVENANCE_PREFIXES)
    }


def _fail_executions(
    executions: Sequence[_Execution],
    *,
    engine: Any,
    repository: Any,
    default_error: str,
) -> list[str]:
    failures: list[str] = []
    for item in executions:
        error = (
            f"{type(item.error).__name__}: {item.error}"
            if item.error is not None
            else default_error
        )
        try:
            repository.fail_scheme_run_atomic(
                engine,
                run_id=item.run_id,
                scheme_id=item.group.base_scheme_id,
                run_date=item.group.predict_date,
                duration_sec=time.monotonic() - item.started,
                records_returned=(
                    len(item.records) if item.records is not None else None
                ),
                error_message=error,
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"run_id={item.run_id}: {type(exc).__name__}: {exc}"
            )
    return failures


def _remaining_groups_after_readback(
    groups: Sequence[_GapGroup],
    readback: Mapping[str, Any],
) -> list[_GapGroup]:
    if (
        readback.get("schema_version") != PLAN_SCHEMA_VERSION
        or readback.get("status") != "READY"
        or not isinstance(readback.get("actions"), list)
    ):
        return list(groups)
    rows = {
        _target_identity(row): row
        for row in readback["actions"]
        if isinstance(row, Mapping)
    }
    remaining: list[_GapGroup] = []
    for group in groups:
        if any(
            not (
                (row := rows.get(_target_identity(target)))
                and row.get("action") == "SKIP_PRESENT"
                and row.get("business_key_present") is True
                and row.get("predict_date") == target["predict_date"]
                and row.get("feature_date") == target["feature_date"]
                and row.get("scheme_version") == group.scheme_version
            )
            for target in group.missing_target_keys
        ):
            remaining.append(group)
    return remaining


def _remaining(groups: Sequence[_GapGroup]) -> list[dict[str, Any]]:
    return [
        {
            "base_scheme_id": group.base_scheme_id,
            "predict_date": group.predict_date,
            "target_keys": [
                dict(target) for target in group.missing_target_keys
            ],
        }
        for group in groups
    ]


def _expected_target_key(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return {field: row[field] for field in _TARGET_KEY_FIELDS}
    except KeyError as exc:
        raise ValueError(f"signal gap action is missing {exc.args[0]}") from exc


def _target_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("base_scheme_id"),
        row.get("target_tenor"),
        row.get("horizon"),
        row.get("predict_date"),
        row.get("feature_date"),
        row.get("target_date"),
    )


def _record_identity(record: PredictionRecord) -> tuple[Any, ...]:
    return (
        record.scheme_id,
        record.target_tenor,
        record.horizon,
        record.predict_date,
        record.feature_date,
        record.target_date,
    )


def _action_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("base_scheme_id") or ""),
        str(row.get("target_tenor") or ""),
        int(row.get("horizon") or 0),
        str(row.get("target_date") or ""),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _signal_gap_fill_singleton_lock() -> ExclusiveFileLock:
    lock_root = resolve_runtime_artifact_root() / "signal-gap-fill-locks"
    _ensure_private_directory(lock_root, label="signal-gap-fill lock root")
    return ExclusiveFileLock(lock_root / "signal-gap-fill.lock")


def _repository_module() -> Any:
    from scheduler import repository

    required = (
        "create_scheme_run",
        "complete_gray_gap_run",
        "fail_scheme_run_atomic",
    )
    missing = [name for name in required if not hasattr(repository, name)]
    if missing:
        raise RuntimeError(
            f"scheduler.repository signal-gap contract is unavailable: {missing}"
        )
    return repository


def _report(
    status: str,
    plan: Mapping[str, Any],
    *,
    groups: Sequence[_GapGroup],
    failure_code: str | None,
    errors: Sequence[str] = (),
    completed: Sequence[Mapping[str, Any]] = (),
    remaining: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "schema_version": "single-date-signal-gap-fill-v2",
        "status": status,
        "failure_code": failure_code,
        "predict_date": plan.get("predict_date"),
        "base_scheme_id": plan.get("base_scheme_id"),
        "group_count": len(groups),
        "errors": list(errors),
        "completed": [dict(item) for item in completed],
        "remaining": [dict(item) for item in remaining],
    }
