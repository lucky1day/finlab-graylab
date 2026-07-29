from __future__ import annotations

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from harness.authorization import (
    Authorization,
    mark_token_used,
    parse_token,
    used_tokens_path,
    verify_signal_gap_fill_authorization,
    write_authorization_audit,
)
from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.gates.gray_backfill_gate import (
    _validate_gray_backfill_records,
)
from harness.result import Evidence, GateResult, GateStatus
from harness.signal_gap_plan import (
    PLAN_SCHEMA_VERSION,
    canonical_plan_sha256,
    plan_signal_gaps,
)
from scheduler.daily_coordinator import (
    OccurrenceFileLock,
    OccurrenceLockUnavailable,
)
from scheduler.discovery import load_scheme_config
from scheduler.executor import (
    BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF,
    run_configured_scheme,
)
from shared.daily_coordinator_mode import resolve_daily_runtime_root
from shared.data_bridge.refresh import (
    DataBridgeRefreshConfig,
    _ensure_private_directory,
)
from shared.input_artifacts import open_native_generation
from shared.models import PredictionRecord


AUTHORIZATION_ACTION = "signal_gap_fill_write"
VINTAGE_DISCLAIMER = (
    "current_snapshot_as_of_not_historical_vintage"
)
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


@dataclass(frozen=True, slots=True)
class _GapGroup:
    base_scheme_id: str
    predict_date: str
    runtime_type: str
    scheme_version: str
    code_sha256: str
    config_sha256: str
    input_mode: str
    actions: tuple[dict[str, Any], ...]
    expected_target_keys: tuple[dict[str, Any], ...]
    input_authority: dict[str, Any]
    source_authority: dict[str, Any]

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


class SignalGapFillGate(Gate):
    """仅按冻结 signal-gap plan 补齐历史 gray_live 日信号。"""

    name = "signal-gap-fill"
    requires_authorization = True
    authorization_action = AUTHORIZATION_ACTION

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(
            self.name,
            lambda started_at: self._run(ctx, started_at),
        )

    def _run(
        self,
        ctx: GateContext,
        started_at: str,
    ) -> GateResult:
        if ctx.signal_gap_plan_path is None:
            return _gate_result(
                started_at,
                status=GateStatus.BLOCKED,
                failure_code="PLAN_PATH_REQUIRED",
                detail="signal-gap-fill requires a frozen plan path",
            )
        databridge_config = (
            ctx.signal_gap_databridge_config
            or DataBridgeRefreshConfig.from_env()
        )
        report = run_signal_gap_fill(
            plan_path=ctx.signal_gap_plan_path,
            authorizations=ctx.signal_gap_authorizations,
            project_root=ctx.project_root,
            engine_factory=ctx.engine_factory,
            databridge_config=databridge_config,
            algo_env=ctx.algo_env,
            timeout_sec=ctx.timeout_sec,
        )
        status = {
            "PASSED": GateStatus.PASSED,
            "SKIP_PRESENT": GateStatus.SKIPPED,
            "BLOCKED": GateStatus.BLOCKED,
            "FAILED": GateStatus.FAILED,
        }[str(report["status"])]
        return GateResult(
            gate_name=self.name,
            status=status,
            passed=status in {GateStatus.PASSED, GateStatus.SKIPPED},
            evidence=[Evidence("signal_gap_fill", report)],
            errors=(
                []
                if status in {GateStatus.PASSED, GateStatus.SKIPPED}
                else [str(report.get("failure_code") or "unknown")]
            ),
            started_at=started_at,
            finished_at=utc_now(),
        )


def run_signal_gap_fill(
    *,
    plan_path: Path,
    authorizations: Sequence[str],
    project_root: Path,
    engine_factory: Callable[[], Any] | None,
    databridge_config: DataBridgeRefreshConfig,
    algo_env: str = "forecast_env",
    timeout_sec: int = 600,
    planner: Callable[..., dict[str, Any]] = plan_signal_gaps,
    config_loader: Callable[[Path], Any] = load_scheme_config,
    algorithm_runner: Callable[..., list[PredictionRecord]] = (
        run_configured_scheme
    ),
    repository_module: Any | None = None,
    native_generation_opener: Callable[..., Any] = (
        open_native_generation
    ),
    singleton_lock_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """以单一协调写入者执行冻结计划中的原子算法组。"""
    frozen = _load_frozen_plan(plan_path)
    try:
        groups = _build_groups(frozen)
    except ValueError as exc:
        return _report(
            "BLOCKED",
            frozen,
            groups=(),
            failure_code="PLAN_GROUP_INVALID",
            errors=[str(exc)],
        )
    if not groups:
        return _report(
            "SKIP_PRESENT",
            frozen,
            groups=(),
            failure_code=None,
        )
    if engine_factory is None:
        return _report(
            "BLOCKED",
            frozen,
            groups=groups,
            failure_code="ENGINE_FACTORY_REQUIRED",
        )
    try:
        singleton_lock = (
            singleton_lock_factory
            or _signal_gap_fill_singleton_lock
        )()
        singleton_lock.acquire()
    except OccurrenceLockUnavailable:
        return _report(
            "BLOCKED",
            frozen,
            groups=groups,
            failure_code="SIGNAL_GAP_FILL_ALREADY_RUNNING",
        )
    except Exception as exc:  # noqa: BLE001
        return _report(
            "BLOCKED",
            frozen,
            groups=groups,
            failure_code="SIGNAL_GAP_FILL_LOCK_INVALID",
            errors=[f"{type(exc).__name__}: {exc}"],
        )
    engine = None
    try:
        engine = engine_factory()
        repository = repository_module or _repository_module()
        current = planner(
            engine,
            start_date=frozen["start_date"],
            as_of_date=frozen["as_of_date"],
            databridge_config=databridge_config,
        )
        classification = _classify_current_plan(
            frozen,
            current,
            groups,
        )
        if classification == "ALL_PRESENT":
            return _report(
                "SKIP_PRESENT",
                frozen,
                groups=groups,
                failure_code=None,
            )
        if classification != "EXACT":
            return _report(
                "BLOCKED",
                frozen,
                groups=groups,
                failure_code=classification,
            )
        try:
            configs = _load_and_validate_configs(
                groups,
                project_root=project_root,
                config_loader=config_loader,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            return _report(
                "BLOCKED",
                frozen,
                groups=groups,
                failure_code="DISCOVERY_IDENTITY_DRIFT",
                errors=[str(exc)],
            )
        auth_by_group, auth_errors = (
            _verify_group_authorizations(
                groups,
                authorizations,
                plan_sha256=str(frozen["plan_sha256"]),
                project_root=project_root,
            )
        )
        if auth_errors:
            return _report(
                "BLOCKED",
                frozen,
                groups=groups,
                failure_code="AUTHORIZATION_INVALID",
                errors=auth_errors,
            )
        executions = _create_runs(
            groups,
            configs=configs,
            engine=engine,
            repository=repository,
        )
        try:
            _run_algorithms(
                executions,
                engine=engine,
                algo_env=algo_env,
                timeout_sec=timeout_sec,
                algorithm_runner=algorithm_runner,
                native_generation_opener=native_generation_opener,
            )
        except Exception as exc:  # noqa: BLE001
            _fail_executions(
                executions,
                engine=engine,
                repository=repository,
                default_error="ALGORITHM_COORDINATOR_FAILED",
            )
            return _report(
                "FAILED",
                frozen,
                groups=groups,
                failure_code="ALGORITHM_COORDINATOR_FAILED",
                errors=[f"{type(exc).__name__}: {exc}"],
            )
        failed = [item for item in executions if item.error is not None]
        if failed:
            _fail_executions(
                executions,
                engine=engine,
                repository=repository,
                default_error="SIGNAL_GAP_FILL_BATCH_ABORTED",
            )
            return _report(
                "FAILED",
                frozen,
                groups=groups,
                failure_code="ALGORITHM_EXECUTION_FAILED",
                errors=[
                    f"{item.group.base_scheme_id}/"
                    f"{item.group.predict_date}: "
                    f"{type(item.error).__name__}: {item.error}"
                    for item in failed
                ],
            )
        try:
            postflight = planner(
                engine,
                start_date=frozen["start_date"],
                as_of_date=frozen["as_of_date"],
                databridge_config=databridge_config,
            )
        except Exception as exc:  # noqa: BLE001
            _fail_executions(
                executions,
                engine=engine,
                repository=repository,
                default_error="POSTFLIGHT_AUTHORITY_UNAVAILABLE",
            )
            return _report(
                "FAILED",
                frozen,
                groups=groups,
                failure_code="POSTFLIGHT_AUTHORITY_UNAVAILABLE",
                errors=[f"{type(exc).__name__}: {exc}"],
            )
        if postflight.get("plan_sha256") != frozen["plan_sha256"]:
            _fail_executions(
                executions,
                engine=engine,
                repository=repository,
                default_error="PLAN_AUTHORITY_DRIFT_AFTER_EXECUTION",
            )
            return _report(
                "FAILED",
                frozen,
                groups=groups,
                failure_code="PLAN_AUTHORITY_DRIFT_AFTER_EXECUTION",
            )
        auth_by_group, auth_errors = (
            _verify_group_authorizations(
                groups,
                authorizations,
                plan_sha256=str(frozen["plan_sha256"]),
                project_root=project_root,
            )
        )
        if auth_errors:
            _fail_executions(
                executions,
                engine=engine,
                repository=repository,
                default_error="AUTHORIZATION_REVALIDATION_FAILED",
            )
            return _report(
                "FAILED",
                frozen,
                groups=groups,
                failure_code="AUTHORIZATION_REVALIDATION_FAILED",
                errors=auth_errors,
            )
        try:
            for item in executions:
                auth = auth_by_group[item.group.identity]
                mark_token_used(auth, used_tokens_path(project_root))
                write_authorization_audit(
                    auth,
                    (
                        project_root
                        / "reports"
                        / "harness"
                        / "signal-gap-fill"
                        / str(item.run_id)
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            _fail_executions(
                executions,
                engine=engine,
                repository=repository,
                default_error="AUTHORIZATION_CONSUMPTION_FAILED",
            )
            return _report(
                "FAILED",
                frozen,
                groups=groups,
                failure_code="AUTHORIZATION_CONSUMPTION_FAILED",
                errors=[f"{type(exc).__name__}: {exc}"],
            )
        completed: list[dict[str, Any]] = []
        completed_run_ids: set[int] = set()
        for item in executions:
            try:
                records = item.records or []
                duration = time.monotonic() - item.started
                written = repository.complete_gray_gap_run(
                    engine,
                    item.cfg,
                    run_id=item.run_id,
                    records=records,
                    expected_target_keys=list(
                        item.group.expected_target_keys
                    ),
                    plan_sha256=str(frozen["plan_sha256"]),
                    source_authority=item.group.source_authority,
                    records_returned=len(records),
                    run_date=item.group.predict_date,
                    duration_sec=duration,
                )
                completed.append(
                    {
                        "base_scheme_id":
                            item.group.base_scheme_id,
                        "predict_date": item.group.predict_date,
                        "run_id": item.run_id,
                        "records_written": int(written),
                    }
                )
                completed_run_ids.add(item.run_id)
            except Exception as exc:  # noqa: BLE001
                item.error = exc
                unfinished = tuple(
                    execution
                    for execution in executions
                    if execution.run_id not in completed_run_ids
                )
                _fail_executions(
                    unfinished,
                    engine=engine,
                    repository=repository,
                    default_error="GRAY_GAP_COMMIT_FAILED",
                )
                return _report(
                    "FAILED",
                    frozen,
                    groups=groups,
                    failure_code="GRAY_GAP_COMMIT_FAILED",
                    errors=[f"{type(exc).__name__}: {exc}"],
                    completed=completed,
                )
        return _report(
            "PASSED",
            frozen,
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


def _signal_gap_fill_singleton_lock() -> OccurrenceFileLock:
    """返回机器级 signal-gap-fill 非阻塞 owner 锁。"""
    lock_root = (
        resolve_daily_runtime_root()
        / "signal-gap-fill-locks"
    )
    _ensure_private_directory(
        lock_root,
        label="signal-gap-fill lock root",
    )
    return OccurrenceFileLock(
        lock_root / "signal-gap-fill.lock"
    )


def _load_frozen_plan(path: Path) -> dict[str, Any]:
    plan_path = Path(path)
    raw = plan_path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("frozen signal gap plan JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("frozen signal gap plan must be an object")
    if payload.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ValueError("frozen signal gap plan schema is invalid")
    plan_sha256 = payload.get("plan_sha256")
    if (
        not isinstance(plan_sha256, str)
        or canonical_plan_sha256(payload) != plan_sha256
    ):
        raise ValueError("frozen signal gap plan SHA-256 is invalid")
    if not isinstance(payload.get("actions"), list):
        raise ValueError("frozen signal gap plan actions are invalid")
    return payload


def _build_groups(plan: Mapping[str, Any]) -> tuple[_GapGroup, ...]:
    rows: list[dict[str, Any]] = []
    non_actionable_live = []
    live_actions_by_group: dict[
        tuple[str, str],
        list[Mapping[str, Any]],
    ] = {}
    for raw in plan["actions"]:
        if not isinstance(raw, dict):
            raise ValueError("signal gap plan action must be an object")
        if raw.get("segment") == "live":
            identity = (
                str(raw.get("base_scheme_id") or ""),
                str(raw.get("predict_date") or ""),
            )
            live_actions_by_group.setdefault(identity, []).append(raw)
        if raw.get("segment") == "live" and raw.get("action") in {
            "BLOCKED_NO_GENERATION",
            "BLOCKED_DATA_CONTRACT",
        }:
            non_actionable_live.append(raw)
        if raw.get("action") != "GRAY_LIVE_GAP":
            continue
        if (
            raw.get("segment") != "live"
            or raw.get("prediction_phase") != "gray_live"
            or raw.get("business_key_present") is not False
        ):
            raise ValueError(
                "signal-gap-fill only accepts missing gray_live actions"
            )
        rows.append(dict(raw))
    if non_actionable_live:
        raise ValueError(
            "frozen plan contains blocked live signal groups"
        )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        identity = (
            str(row.get("base_scheme_id") or ""),
            str(row.get("predict_date") or ""),
        )
        if not all(identity):
            raise ValueError("signal gap action group identity is invalid")
        grouped.setdefault(identity, []).append(row)
    result: list[_GapGroup] = []
    for identity, group_rows in sorted(grouped.items()):
        if any(
            row.get("action") != "GRAY_LIVE_GAP"
            for row in live_actions_by_group.get(identity, ())
        ):
            raise ValueError(
                "frozen plan contains a partial present live group"
            )
        ordered = tuple(sorted(group_rows, key=_action_sort_key))
        invariant_fields = (
            "runtime_type",
            "scheme_version",
            "code_sha256",
            "config_sha256",
            "input_mode",
            "task_type",
            "horizon",
            "feature_date",
            "target_date",
        )
        for field in invariant_fields:
            if len({str(row.get(field)) for row in ordered}) != 1:
                raise ValueError(
                    f"signal gap group {field} is not atomic"
                )
        authorities = {
            _canonical_json(row.get("input_authority"))
            for row in ordered
        }
        if len(authorities) != 1:
            raise ValueError(
                "signal gap group input authority is not atomic"
            )
        input_authority = json.loads(next(iter(authorities)))
        expected = tuple(_expected_target_key(row) for row in ordered)
        result.append(
            _GapGroup(
                base_scheme_id=identity[0],
                predict_date=identity[1],
                runtime_type=str(ordered[0]["runtime_type"]),
                scheme_version=str(ordered[0]["scheme_version"]),
                code_sha256=str(ordered[0]["code_sha256"]),
                config_sha256=str(ordered[0]["config_sha256"]),
                input_mode=str(ordered[0]["input_mode"]),
                actions=ordered,
                expected_target_keys=expected,
                input_authority=input_authority,
                source_authority=_repository_source_authority(
                    ordered[0],
                    input_authority,
                ),
            )
        )
    return tuple(result)


def _classify_current_plan(
    frozen: Mapping[str, Any],
    current: Mapping[str, Any],
    groups: Sequence[_GapGroup],
) -> str:
    if current.get("plan_sha256") == frozen.get("plan_sha256"):
        return "EXACT"
    current_by_key = {
        _action_identity(row): row
        for row in current.get("actions", ())
        if isinstance(row, dict)
    }
    group_states: list[str] = []
    for group in groups:
        rows = [
            current_by_key.get(_action_identity(row))
            for row in group.actions
        ]
        present = [
            row is not None
            and row.get("action") == "SKIP_PRESENT"
            and row.get("business_key_present") is True
            for row in rows
        ]
        if all(present):
            group_states.append("PRESENT")
        elif any(present):
            return "PARTIAL_GROUP_PRESENT"
        else:
            group_states.append("PENDING")
    if group_states and all(state == "PRESENT" for state in group_states):
        return "ALL_PRESENT"
    return "PLAN_SHA256_DRIFT"


def _load_and_validate_configs(
    groups: Sequence[_GapGroup],
    *,
    project_root: Path,
    config_loader: Callable[[Path], Any],
) -> dict[tuple[str, str], Any]:
    result: dict[tuple[str, str], Any] = {}
    for group in groups:
        cfg = config_loader(
            project_root
            / "schemes"
            / group.base_scheme_id
            / "config.yaml"
        )
        actual = {
            "scheme_version": str(getattr(cfg, "scheme_version", "")),
            "runtime_type": str(getattr(cfg, "runtime_type", "")),
            "code_sha256": str(getattr(cfg, "code_hash", "")),
            "config_sha256": str(getattr(cfg, "config_hash", "")),
        }
        expected = {
            "scheme_version": group.scheme_version,
            "runtime_type": group.runtime_type,
            "code_sha256": group.code_sha256,
            "config_sha256": group.config_sha256,
        }
        if actual != expected:
            raise ValueError(
                f"discovery identity drift for {group.base_scheme_id}"
            )
        result[group.identity] = cfg
    return result


def _verify_group_authorizations(
    groups: Sequence[_GapGroup],
    tokens: Sequence[str],
    *,
    plan_sha256: str,
    project_root: Path,
) -> tuple[
    dict[tuple[str, str], Authorization],
    list[str],
]:
    token_by_group: dict[tuple[str, str], str] = {}
    errors: list[str] = []
    for token in tokens:
        try:
            parsed = parse_token(token)
            identity = (parsed.scheme_id, str(parsed.predict_date))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"invalid signal gap authorization: {exc}")
            continue
        if identity in token_by_group:
            errors.append(
                f"duplicate authorization for {identity}"
            )
        token_by_group[identity] = token
    auth_by_group: dict[tuple[str, str], Authorization] = {}
    for group in groups:
        token = token_by_group.get(group.identity)
        auth, group_errors = verify_signal_gap_fill_authorization(
            token,
            plan_sha256=plan_sha256,
            base_scheme_id=group.base_scheme_id,
            predict_date=group.predict_date,
            target_keys=group.expected_target_keys,
            scheme_version=group.scheme_version,
            source_authority=group.source_authority,
            used_store_path=used_tokens_path(project_root),
        )
        errors.extend(
            f"{group.identity}: {error}"
            for error in group_errors
        )
        if auth is not None:
            auth_by_group[group.identity] = auth
    extra = sorted(set(token_by_group) - {group.identity for group in groups})
    if extra:
        errors.append(f"authorization scope has extra groups: {extra}")
    return auth_by_group, errors


def _create_runs(
    groups: Sequence[_GapGroup],
    *,
    configs: Mapping[tuple[str, str], Any],
    engine: Any,
    repository: Any,
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
                records_expected=len(group.expected_target_keys),
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
        _fail_executions(
            executions,
            engine=engine,
            repository=repository,
            default_error=f"RUN_CREATION_ABORTED:{type(exc).__name__}",
        )
        raise
    return executions


def _run_algorithms(
    executions: Sequence[_Execution],
    *,
    engine: Any,
    algo_env: str,
    timeout_sec: int,
    algorithm_runner: Callable[..., list[PredictionRecord]],
    native_generation_opener: Callable[..., Any],
) -> None:
    native_pool = ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="signal-gap-native",
    )
    blackbox_pool = ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="signal-gap-v2",
    )
    futures: dict[Future[list[PredictionRecord]], _Execution] = {}
    try:
        for item in executions:
            pool = (
                blackbox_pool
                if item.group.runtime_type == "blackbox_v2"
                else native_pool
            )
            futures[
                pool.submit(
                    _run_algorithm,
                    item,
                    engine=engine,
                    algo_env=algo_env,
                    timeout_sec=timeout_sec,
                    algorithm_runner=algorithm_runner,
                    native_generation_opener=native_generation_opener,
                )
            ] = item
        for future, item in futures.items():
            try:
                item.records = future.result()
            except BaseException as exc:  # noqa: BLE001
                item.error = exc
    finally:
        native_pool.shutdown(wait=True, cancel_futures=True)
        blackbox_pool.shutdown(wait=True, cancel_futures=True)


def _run_algorithm(
    item: _Execution,
    *,
    engine: Any,
    algo_env: str,
    timeout_sec: int,
    algorithm_runner: Callable[..., list[PredictionRecord]],
    native_generation_opener: Callable[..., Any],
) -> list[PredictionRecord]:
    group = item.group
    kwargs: dict[str, Any] = {
        "engine": engine,
        "algo_env": algo_env,
        "timeout_sec": timeout_sec,
    }
    context = None
    try:
        if group.runtime_type == "native_adapter":
            artifact = _native_artifact(group.input_authority)
            if artifact["feature_date"] != group.actions[0]["feature_date"]:
                raise ValueError("Native artifact feature cutoff drift")
            context = native_generation_opener(
                Path(artifact["manifest_uri"]),
                expected_generation_id=artifact["generation_id"],
                expected_manifest_sha256=artifact["manifest_sha256"],
                expected_business_date=group.predict_date,
                expected_feature_date=group.actions[0]["feature_date"],
            )
            kwargs["native_generation"] = context
            if group.input_mode == "live_source_0629":
                package_sha256 = group.actions[0].get(
                    "source_package_sha256"
                )
                if not _is_sha256(package_sha256):
                    raise ValueError(
                        "0629 source package SHA-256 is not frozen"
                    )
                kwargs["live_source_compatibility"] = True
                kwargs["live_source_package_sha256"] = package_sha256
        elif group.runtime_type == "blackbox_v2":
            authority = group.source_authority
            if authority["refresh_date"] <= group.predict_date:
                raise ValueError(
                    "DataBridge refresh_date must be after predict_date"
                )
            kwargs.update(
                {
                    "blackbox_snapshot_mode":
                        BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF,
                    "expected_generation_id":
                        authority["generation_id"],
                    "expected_refresh_date":
                        authority["refresh_date"],
                }
            )
        else:
            raise ValueError(
                f"unsupported runtime_type: {group.runtime_type}"
            )
        records = algorithm_runner(
            item.cfg,
            group.predict_date,
            **kwargs,
        )
        normalized = [
            replace(record, prediction_phase="gray_live")
            for record in records
        ]
        _validate_group_records(group, normalized)
        return normalized
    finally:
        if context is not None and hasattr(context, "dispose"):
            context.dispose()


def _validate_group_records(
    group: _GapGroup,
    records: list[PredictionRecord],
) -> None:
    expected = {
        (
            row["target_tenor"],
            row["horizon"],
            row["predict_date"],
            row["feature_date"],
            row["target_date"],
        )
        for row in group.expected_target_keys
    }
    actual = {
        (
            record.target_tenor,
            record.horizon,
            record.predict_date,
            record.feature_date,
            record.target_date,
        )
        for record in records
        if (
            record.scheme_id == group.base_scheme_id
            and record.scheme_version == group.scheme_version
            and record.prediction_phase == "gray_live"
        )
    }
    if len(records) != len(expected) or actual != expected:
        raise ValueError(
            "algorithm records do not match the atomic target group"
        )
    if group.runtime_type == "blackbox_v2":
        authority = group.source_authority
        _validate_gray_backfill_records(
            records,
            expected_generation_id=authority["generation_id"],
            expected_refresh_date=authority["refresh_date"],
            expected_feature_date=group.actions[0]["feature_date"],
            frequency=str(group.actions[0]["frequency"]),
        )


def _fail_executions(
    executions: Sequence[_Execution],
    *,
    engine: Any,
    repository: Any,
    default_error: str,
) -> None:
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
                    len(item.records)
                    if item.records is not None
                    else None
                ),
                error_message=error,
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"run_id={item.run_id}: {type(exc).__name__}: {exc}"
            )
    if failures:
        raise RuntimeError(
            "failed to preserve all nonterminal signal-gap runs: "
            + "; ".join(failures)
        )


def _repository_source_authority(
    action: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> dict[str, Any]:
    feature_date = str(action["feature_date"])
    if action["runtime_type"] == "native_adapter":
        artifact = _native_artifact(authority)
        return {
            "authority_type": "native_current_snapshot_artifact",
            "artifact_id": artifact["generation_id"],
            "manifest_sha256": artifact["manifest_sha256"],
            "feature_date": feature_date,
            "cutoff_date": feature_date,
            "vintage_disclaimer": VINTAGE_DISCLAIMER,
        }
    cutoff = authority.get("cutoff")
    if not isinstance(cutoff, dict):
        raise ValueError("DataBridge cutoff authority is missing")
    refresh_date = str(authority.get("refresh_date") or "")
    manifest_sha256 = (
        authority.get("manifest_sha256")
        or authority.get("stable_identity_sha256")
    )
    if (
        str(cutoff.get("daily_cutoff_key") or "") != feature_date
        or not _is_sha256(manifest_sha256)
    ):
        raise ValueError("DataBridge cutoff authority is invalid")
    if refresh_date <= str(action["predict_date"]):
        raise ValueError(
            "DataBridge refresh_date must be after predict_date"
        )
    return {
        "authority_type": "databridge_current_generation",
        "generation_id": str(authority.get("generation_id") or ""),
        "manifest_sha256": str(manifest_sha256),
        "refresh_date": refresh_date,
        "cutoff_date": feature_date,
        "replay_mode": "historical_as_of_replay",
        "vintage_disclaimer": VINTAGE_DISCLAIMER,
    }


def _native_artifact(
    authority: Mapping[str, Any],
) -> dict[str, Any]:
    artifact = authority.get("artifact")
    if not isinstance(artifact, dict):
        raise ValueError("Native frozen artifact authority is missing")
    required = (
        "generation_id",
        "manifest_uri",
        "manifest_sha256",
        "feature_date",
    )
    if any(not str(artifact.get(field) or "") for field in required):
        raise ValueError("Native frozen artifact authority is incomplete")
    if not _is_sha256(artifact["manifest_sha256"]):
        raise ValueError("Native artifact manifest SHA-256 is invalid")
    return dict(artifact)


def _expected_target_key(row: Mapping[str, Any]) -> dict[str, Any]:
    return {field: row[field] for field in _TARGET_KEY_FIELDS}


def _action_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("registry_scheme_id"),
        row.get("base_scheme_id"),
        row.get("target_tenor"),
        row.get("horizon"),
        row.get("predict_date"),
        row.get("feature_date"),
        row.get("target_date"),
        row.get("segment"),
    )


def _action_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("registry_scheme_id") or ""),
        str(row.get("target_tenor") or ""),
        int(row.get("horizon") or 0),
        str(row.get("target_date") or ""),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


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
            "scheduler.repository signal-gap contract is unavailable: "
            f"{missing}"
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
) -> dict[str, Any]:
    return {
        "schema_version": "signal-gap-fill-report-v1",
        "status": status,
        "failure_code": failure_code,
        "plan_sha256": plan.get("plan_sha256"),
        "group_count": len(groups),
        "errors": list(errors),
        "completed": [dict(item) for item in completed],
    }


def _gate_result(
    started_at: str,
    *,
    status: GateStatus,
    failure_code: str,
    detail: str,
) -> GateResult:
    return GateResult(
        gate_name="signal-gap-fill",
        status=status,
        passed=False,
        evidence=[Evidence("failure_code", failure_code)],
        errors=[detail],
        started_at=started_at,
        finished_at=utc_now(),
    )
