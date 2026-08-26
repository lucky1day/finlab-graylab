from __future__ import annotations

import ast
import hashlib
import json
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harness.context import GateContext
from harness.operation import (
    DEFAULT_BACKTEST_START_DATE,
    operation_scope_sha256,
    verify_direct_operation,
)
from backtests.blackbox_v2 import run_blackbox_historical_backtest
from backtests.repository import persist_backtest_output_atomic, snapshot_backtest_scope_counts
from harness.gates.base import Gate, create_default_engine, guarded_result, utc_now
from harness.probes.table_guard import diff_snapshots
from harness.result import Evidence, GateResult, GateStatus
from scheduler.blackbox_v2_runner import (
    BacktestExecutionBudget,
    DEFAULT_RUNTIME_PROFILE,
    RuntimeProfile,
    run_blackbox_backtest,
    run_blackbox_predict,
)
from scheduler.process_control import ProcessGroupTerminationError
from scheduler.discovery import SchemeConfig, load_scheme_config
from scheduler.repository import register_blackbox_draft_identity
from scheduler.repository import BlackboxLifecycleIdentityAbsent
from shared.blackbox_v2.contracts import (
    BlackboxMetadata,
    BlackboxRequest,
    load_metadata,
)
from shared.blackbox_v2.history import CURRENT_SNAPSHOT_REPLAY, build_historical_cases
from shared.blackbox_v2.requests import (
    build_live_request,
    build_request,
)
from shared.blackbox_v2.snapshot import (
    BlackboxInputBundle,
    BlackboxSnapshot,
    SNAPSHOT_FILENAMES,
    compose_blackbox_input_bundle,
)
from shared.calendar_service import get_calendar
from shared.data_bridge.refresh import DataBridgeRefreshConfig, DataBridgeStore
from shared.input_artifacts import (
    capture_blackbox_platform_inputs_from_connection,
    get_blackbox_generation_snapshot,
    open_blackbox_runtime_view,
    resolve_blackbox_input_cutoffs,
)
from shared.blackbox_v2.lifecycle import (
    LifecycleOperationError,
    LifecycleState,
    assert_lifecycle_clear,
    perform_lifecycle_transition,
)

if TYPE_CHECKING:
    from scheduler.repository import BlackboxLifecycleState


FORBIDDEN_IMPORT_ROOTS = {
    "aiohttp",
    "ftplib",
    "http",
    "mysql",
    "pymysql",
    "psycopg",
    "requests",
    "socket",
    "sqlalchemy",
    "sqlite3",
    "subprocess",
    "urllib",
}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_QUALIFIED_CALLS = {"os.system", "os.popen", "os.spawnl", "os.spawnv"}
ABSOLUTE_PATH_PATTERN = re.compile(r"^(?:/Users/|/home/|[A-Za-z]:[\\/])")
# 只把被路径分隔符或字符串边界包围的完整 ".." 段判为穿越，避免误伤散文里的省略号。
RELATIVE_TRAVERSAL_PATTERN = re.compile(r"(?:^|[/\\])\.\.(?:[/\\]|$)")
@dataclass(frozen=True)
class InputState:
    snapshot: BlackboxSnapshot
    request: BlackboxRequest
    bundle: BlackboxInputBundle | None = None
    provenance: dict[str, str] | None = None


@dataclass(frozen=True)
class PassedAllRun:
    harness_run_id: str
    data_snapshot_id: str
    generation_id: str | None = None
    runtime_profile: str | None = None
    environment_fingerprint: str | None = None


class _BlackboxGate(Gate):
    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        raise NotImplementedError


def _visible_entry_names(directory: Path) -> set[str]:
    """列出参与交付结构精确比较的条目名。

    只忽略 CPython 在 import 交付模块时生成的 `__pycache__`，且必须是本地
    真实目录：按名字一刀切会让普通文件或指向外部目录的 symlink 只要叫
    `__pycache__` 就整体隐身，把严格的两文件交付边界放宽成任意内容可藏。
    因此这里用 `lstat` 判定——非目录或 symlink 的同名条目保留在集合中，
    使精确集合比较照常失败。
    """
    names: set[str] = set()
    for entry in directory.iterdir():
        if entry.name == "__pycache__":
            try:
                info = entry.lstat()
            except OSError:
                names.add(entry.name)
                continue
            if stat.S_ISDIR(info.st_mode):
                continue
        names.add(entry.name)
    return names


class BlackboxStaticGate(_BlackboxGate):
    name = "static"

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        metadata = _metadata(cfg)
        scheme_dir = cfg.path
        delivery_dir = scheme_dir / "delivery"
        errors: list[str] = []
        scheme_entries = _visible_entry_names(scheme_dir)
        delivery_entries = (
            _visible_entry_names(delivery_dir) if delivery_dir.is_dir() else set()
        )
        expected_delivery = {f"{ctx.scheme_id}.py", f"{ctx.scheme_id}.json"}
        if scheme_entries != {"config.yaml", "delivery"}:
            errors.append(f"scheme directory must contain only config.yaml and delivery: {sorted(scheme_entries)}")
        if delivery_entries != expected_delivery:
            errors.append(
                f"delivery must contain exactly {sorted(expected_delivery)}: got={sorted(delivery_entries)}"
            )
        for path in (cfg.delivery_script, cfg.delivery_metadata):
            if path is None or not path.is_file() or path.is_symlink():
                errors.append(f"delivery file must be regular and not a symlink: {path}")

        violations: list[str] = []
        if cfg.delivery_script is not None and cfg.delivery_script.is_file():
            try:
                tree = ast.parse(cfg.delivery_script.read_text(encoding="utf-8"), filename=str(cfg.delivery_script))
            except (OSError, UnicodeError, SyntaxError) as exc:
                errors.append(f"script syntax error: {exc}")
            else:
                violations = _script_violations(tree)
                errors.extend(violations)
        if cfg.runtime_profile != DEFAULT_RUNTIME_PROFILE.name:
            errors.append(
                f"runtime_profile must be {DEFAULT_RUNTIME_PROFILE.name}: got={cfg.runtime_profile}"
            )
        if cfg.data_schema_version != "data-bridge-v1":
            errors.append(f"data_schema_version must be data-bridge-v1: got={cfg.data_schema_version}")

        evidence = [
            Evidence("runtime_type", cfg.runtime_type),
            Evidence("scheme_version", cfg.scheme_version),
            Evidence("algorithm_version", metadata.algorithm_version),
            Evidence("delivery_entries", sorted(delivery_entries)),
            Evidence("script_violations", violations),
            Evidence("metadata", asdict(metadata)),
            Evidence("platform_inputs", list(cfg.platform_inputs)),
        ]
        return _finish(self.name, started_at, evidence, errors)


class BlackboxInputGate(_BlackboxGate):
    name = "input"

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        state = _ensure_input_state(ctx, force=True)
        manifest = json.loads(state.snapshot.manifest_path.read_text(encoding="utf-8"))
        bundle = _input_bundle(state)
        platform_files = {
            artifact.filename: {
                "artifact_id": artifact.artifact_id,
                "provider_version": artifact.provider_version,
                "row_count": artifact.row_count,
                "column_count": len(artifact.columns),
                "sha256": artifact.sha256,
            }
            for artifact in bundle.platform_input_artifacts
        }
        base_snapshot_files = {
            name: {
                "row_count": manifest["files"][name]["row_count"],
                "column_count": len(
                    manifest["files"][name]["columns"]
                ),
                "sha256": manifest["files"][name]["sha256"],
            }
            for name in SNAPSHOT_FILENAMES
        }
        evidence = [
            *_bundle_evidence(bundle),
            *(
                Evidence(key, value)
                for key, value in (state.provenance or {}).items()
            ),
            Evidence("data_schema_version", state.snapshot.schema_version),
            Evidence("base_snapshot_files", base_snapshot_files),
            Evidence("platform_input_files", platform_files),
            Evidence("input_identity_manifest", bundle.identity_manifest),
            Evidence("input_audit_manifest", bundle.audit_manifest),
            Evidence("request", asdict(state.request)),
        ]
        return _finish(self.name, started_at, evidence, [])


class BlackboxCompareGate(_BlackboxGate):
    name = "compare"

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        metadata = _metadata(cfg)
        state = _ensure_input_state(ctx)
        profile = _profile(ctx)
        bundle = _input_bundle(state)
        with _open_runtime_input(ctx, state) as runtime_view:
            runtime_kwargs = _runner_bundle_kwargs(runtime_view.bundle)
            # 冒烟：交付在平台喂进去的输入下能否产出合法 Result。这是本 Gate 唯一
            # 的算法调用——交付自身的性质（确定性、两入口一致、截止隔离、跨请求无状态）
            # 由上游按其交付契约保证，平台不重验。
            baseline = run_blackbox_predict(
                metadata=metadata,
                script_path=_script(cfg),
                request=state.request,
                data_dir=runtime_view.data_dir,
                profile=profile,
                **runtime_kwargs,
            )

        # dry-run 段已并入本 Gate：baseline 与原 dry-run 是逐参数相同的同一次 predict。
        # 结果直接进入 Harness evidence，不再保存第二份本地 JSON。
        evidence = [
            *_bundle_evidence(bundle),
            Evidence("prediction_record", asdict(baseline)),
            Evidence("business_tables_written", False),
        ]
        return _finish(self.name, started_at, evidence, [])

class BlackboxBacktestGate(_BlackboxGate):
    name = "backtest"

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        if not ctx.persist_backtest:
            return _blocked(
                self.name,
                started_at,
                ["Blackbox backtest requires --persist"],
            )
        return self._run_persist(ctx, started_at)

    def _run_persist(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        if (
            cfg.frequency == "weekly"
            and ctx.backtest_start_date != DEFAULT_BACKTEST_START_DATE
        ):
            return _blocked(
                self.name,
                started_at,
                [
                    "weekly persisted backtest requires "
                    f"backtest_start_date={DEFAULT_BACKTEST_START_DATE}"
                ],
            )
        engine = ctx.engine_factory() if ctx.engine_factory is not None else create_default_engine()
        try:
            cfg = _reload_pinned_blackbox_config(cfg, phase="persisted backtest preflight")
            passed_run = _verify_passed_all(engine, cfg)
            operation, operation_errors = verify_direct_operation(
                ctx.operation,
                scheme_id=ctx.scheme_id,
                action="backtest_persist",
                predict_date=ctx.predict_date,
                scheme_version=cfg.scheme_version,
                harness_run_id=passed_run.harness_run_id,
                backtest_start_date=ctx.backtest_start_date,
            )
            if operation is None or operation_errors:
                return _blocked(self.name, started_at, operation_errors)
            state = _ensure_input_state(ctx)
            provenance = state.provenance
            if provenance is None:
                raise ValueError("Blackbox input state has no DataBridge provenance")
            generation_id = str(provenance.get("generation_id", "")).strip()
            if not generation_id:
                return _blocked(
                    self.name,
                    started_at,
                    ["Blackbox persisted backtest requires DataBridge generation_id evidence"],
                )
            provenance_errors = _persisted_backtest_provenance_errors(
                cfg,
                passed_run,
                state,
                provenance,
                environment_fingerprint=_environment_fingerprint(ctx.project_root),
            )
            if provenance_errors:
                return _blocked(self.name, started_at, provenance_errors)
            metadata = _metadata(cfg)
            cases = build_historical_cases(
                metadata,
                state.snapshot,
                engine,
                target_date_before=ctx.predict_date,
                predict_date_from=ctx.backtest_start_date,
            )
            profile = _profile(ctx)
            batch_sizes = [
                min(profile.max_batch_requests, len(cases) - start)
                for start in range(0, len(cases), profile.max_batch_requests)
            ]
            budget = BacktestExecutionBudget(
                deadline_monotonic=time.monotonic() + ctx.timeout_sec,
                max_subprocesses=len(batch_sizes),
            )
            benchmark_id = f"bbv2-{cfg.scheme_id}-{passed_run.harness_run_id}"
            bundle = _input_bundle(state)
            with _open_runtime_input(ctx, state) as runtime_view:
                output = run_blackbox_historical_backtest(
                    metadata=metadata,
                    script_path=_script(cfg),
                    cases=cases,
                    snapshot=state.snapshot,
                    input_bundle=runtime_view.bundle,
                    runtime_data_dir=runtime_view.data_dir,
                    scheme_version=cfg.scheme_version,
                    generation_id=generation_id,
                    benchmark_id=benchmark_id,
                    harness_run_id=passed_run.harness_run_id,
                    run_delivery=run_blackbox_backtest,
                    profile=profile,
                    budget=budget,
                    backtest_start_date=ctx.backtest_start_date,
                    target_date_before=ctx.predict_date,
                    total_deadline_sec=ctx.timeout_sec,
                )
            if len(output.rows) != len(cases) or not output.monthly_metrics:
                raise ValueError(
                    "Blackbox persisted backtest requires one Result per historical Request "
                    "and non-empty monthly metrics: "
                    f"requests={len(cases)}, records={len(output.rows)}, "
                    f"monthly_metrics={len(output.monthly_metrics)}"
                )
            cfg = _reload_pinned_blackbox_config(cfg, phase="persisted backtest commit")
            before = snapshot_backtest_scope_counts(engine, benchmark_id)
            run_id = persist_backtest_output_atomic(engine, output, benchmark_id=benchmark_id)
            after = snapshot_backtest_scope_counts(engine, benchmark_id)
            deltas = diff_snapshots(before, after)
            errors = _persisted_backtest_delta_errors(
                deltas,
                expected_predictions=len(cases),
                expected_metrics=len(output.monthly_metrics),
            )
        finally:
            if hasattr(engine, "dispose"):
                engine.dispose()

        evidence = [
            Evidence("persist", True),
            Evidence("run_id", run_id),
            Evidence("benchmark_id", benchmark_id),
            Evidence("scheme_version", cfg.scheme_version),
            Evidence("harness_run_id", passed_run.harness_run_id),
            Evidence("generation_id", generation_id),
            Evidence(
                "data_snapshot_id",
                bundle.combined_snapshot_id,
            ),
            Evidence("backtest_start_date", ctx.backtest_start_date),
            Evidence("target_date_before", ctx.predict_date),
            Evidence("requests", len(cases)),
            Evidence("records", len(output.rows)),
            Evidence("monthly_metrics", len(output.monthly_metrics)),
            Evidence("max_batch_requests", profile.max_batch_requests),
            Evidence("batch_count", len(batch_sizes)),
            Evidence("batch_sizes", batch_sizes),
            Evidence("subprocesses_started", budget.subprocesses_started),
            Evidence("max_subprocesses", budget.max_subprocesses),
            Evidence("total_deadline_sec", ctx.timeout_sec),
            Evidence("protected_table_deltas", deltas),
            Evidence("operator", operation.issued_by),
            Evidence("operation_scope_sha256", operation_scope_sha256(operation)),
            Evidence("replay_semantics", CURRENT_SNAPSHOT_REPLAY),
        ]
        return _finish(self.name, started_at, evidence, errors)


class BlackboxShadowRegisterGate(_BlackboxGate):
    name = "shadow-register"

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        try:
            assert_lifecycle_clear(ctx.project_root, cfg.scheme_id)
        except RuntimeError as exc:
            return _blocked(self.name, started_at, [str(exc)])
        engine = ctx.engine_factory() if ctx.engine_factory is not None else create_default_engine()
        config_path = cfg.path / "config.yaml"
        registered_state = None
        try:
            passed_run = _verify_passed_all(engine, cfg)
            operation, operation_errors = verify_direct_operation(
                ctx.operation,
                scheme_id=ctx.scheme_id,
                action="shadow_register",
                predict_date=ctx.predict_date,
                scheme_version=cfg.scheme_version,
                harness_run_id=passed_run.harness_run_id,
            )
            if operation is None or operation_errors:
                return _blocked(self.name, started_at, operation_errors)
            environment_fingerprint = _environment_fingerprint(ctx.project_root)
            validated_cfg = replace(
                cfg,
                version_status="validated",
                environment_fingerprint=environment_fingerprint,
                data_snapshot_id=passed_run.data_snapshot_id,
            )
            original_identity = replace(
                cfg,
                environment_fingerprint=environment_fingerprint,
                data_snapshot_id=passed_run.data_snapshot_id,
            )
            # 身份尚不存在时先按 insert-only 语义创建 draft+paused，再走既有迁移。
            # 对新方案这两步永远背靠背发生，中间的 draft 状态不被任何人观察；
            # 身份已存在（revision 路径）时行为完全不变。
            identity_created = False
            try:
                db_before = _read_shadow_state(engine, cfg)
            except BlackboxLifecycleIdentityAbsent:
                pinned_cfg = _reload_pinned_blackbox_config(
                    cfg,
                    phase="draft registration",
                )
                register_blackbox_draft_identity(
                    engine,
                    replace(
                        pinned_cfg,
                        environment_fingerprint=environment_fingerprint,
                        data_snapshot_id=passed_run.data_snapshot_id,
                    ),
                    expected_harness_run_id=passed_run.harness_run_id,
                )
                identity_created = True
                db_before = _read_shadow_state(engine, cfg)
            previous = LifecycleState(
                cfg.status,
                db_before.version_status,
                db_before.registry_status,
                config_version_status=cfg.version_status,
            )
            safe_version_statuses = {"draft", "validated"}
            if (
                previous.config_status != "paused"
                or previous.registry_status != "paused"
                or previous.config_version_status not in safe_version_statuses
                or previous.version_status not in safe_version_statuses
            ):
                raise ValueError(
                    "shadow registration requires paused state and both config/DB exact "
                    f"version status draft or validated, got {previous}"
                )
            target = LifecycleState("paused", "shadow", "paused")
            compensating = False

            def enriched_current():
                current = load_scheme_config(config_path)
                if current.scheme_version != cfg.scheme_version:
                    raise _CanonicalSchemeVersionChanged(
                        "Blackbox canonical version changed during shadow registration: "
                        f"expected={cfg.scheme_version}, current={current.scheme_version}"
                    )
                return replace(
                    current,
                    environment_fingerprint=environment_fingerprint,
                    data_snapshot_id=passed_run.data_snapshot_id,
                )

            def original_identity_current() -> SchemeConfig:
                current = load_scheme_config(config_path)
                return replace(
                    original_identity,
                    status=current.status,
                    version_status=current.version_status,
                )

            def apply_database(state: LifecycleState) -> None:
                nonlocal compensating, registered_state
                if state == target:
                    current = enriched_current()
                    registered_state = _register_shadow(engine, validated_cfg, current)
                    return
                compensating = True
                try:
                    current = enriched_current()
                except _CanonicalSchemeVersionChanged:
                    current = original_identity_current()
                from scheduler.repository import apply_blackbox_lifecycle_state

                apply_blackbox_lifecycle_state(
                    engine,
                    current,
                    version_status=state.version_status,
                    registry_status=state.registry_status,
                )

            def read_state() -> LifecycleState:
                if compensating:
                    try:
                        current = enriched_current()
                    except _CanonicalSchemeVersionChanged:
                        current = original_identity_current()
                else:
                    current = enriched_current()
                db_state = _read_shadow_state(engine, current)
                return LifecycleState(
                    current.status,
                    db_state.version_status,
                    db_state.registry_status,
                    config_version_status=current.version_status,
                )

            _, journal_path = perform_lifecycle_transition(
                project_root=ctx.project_root,
                config_path=config_path,
                action="shadow_register",
                scheme_id=cfg.scheme_id,
                scheme_version=cfg.scheme_version,
                harness_run_id=passed_run.harness_run_id,
                previous=previous,
                target=target,
                compensation=previous,
                operation_scope_sha256=operation_scope_sha256(operation),
                apply_database=apply_database,
                read_state=read_state,
            )
        except LifecycleOperationError as exc:
            return _finish(
                self.name,
                started_at,
                [
                    Evidence("journal_path", str(exc.journal_path)),
                    Evidence("compensated", exc.compensated),
                ],
                [str(exc)],
            )
        finally:
            if hasattr(engine, "dispose"):
                engine.dispose()

        if registered_state is None:
            raise RuntimeError("shadow lifecycle completed without database state evidence")
        evidence = [
            Evidence("harness_run_id", passed_run.harness_run_id),
            Evidence("identity_created", identity_created),
            Evidence("validated_scheme_version", validated_cfg.scheme_version),
            Evidence("shadow_scheme_version", registered_state.scheme_version),
            Evidence("registry_status", registered_state.registry_status),
            Evidence("version_status", registered_state.version_status),
            Evidence("runtime_type", registered_state.runtime_type),
            Evidence("data_snapshot_id", registered_state.data_snapshot_id),
            Evidence("environment_fingerprint", registered_state.environment_fingerprint),
            Evidence("code_hash", registered_state.code_hash),
            Evidence("config_hash", registered_state.config_hash),
            Evidence("manifest_hash", registered_state.manifest_hash),
            Evidence("business_tables_written", False),
            Evidence("operator", operation.issued_by),
            Evidence("operation_scope_sha256", operation_scope_sha256(operation)),
            Evidence("journal_path", str(journal_path)),
            Evidence("journal_phase", "verified"),
        ]
        return _finish(self.name, started_at, evidence, [])


BLACKBOX_GATES: dict[str, type[Gate]] = {
    "static": BlackboxStaticGate,
    "input": BlackboxInputGate,
    "compare": BlackboxCompareGate,
    "backtest": BlackboxBacktestGate,
    "shadow-register": BlackboxShadowRegisterGate,
}


def _config(ctx: GateContext) -> SchemeConfig:
    cfg = ctx.config or load_scheme_config(ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml")
    if cfg.runtime_type != "blackbox_v2":
        raise ValueError(f"Blackbox V2 gate requires runtime_type=blackbox_v2, got {cfg.runtime_type}")
    return cfg


def _metadata(cfg: SchemeConfig) -> BlackboxMetadata:
    if cfg.delivery_metadata is None:
        raise ValueError(f"Blackbox V2 metadata path missing for {cfg.scheme_id}")
    return load_metadata(cfg.delivery_metadata)


def _script(cfg: SchemeConfig) -> Path:
    if cfg.delivery_script is None:
        raise ValueError(f"Blackbox V2 script path missing for {cfg.scheme_id}")
    return cfg.delivery_script


def _profile(ctx: GateContext) -> RuntimeProfile:
    allowed_cli_values = {"forecast_env", DEFAULT_RUNTIME_PROFILE.conda_env}
    if ctx.algo_env and ctx.algo_env not in allowed_cli_values:
        raise ValueError(
            "Blackbox V2 runtime environment is fixed by runtime_profile; "
            f"CLI override is forbidden: {ctx.algo_env}"
        )
    return DEFAULT_RUNTIME_PROFILE


def _input_bundle(state: InputState) -> BlackboxInputBundle:
    if state.bundle is not None:
        return state.bundle
    return compose_blackbox_input_bundle(state.snapshot)


@contextmanager
def _open_runtime_input(ctx: GateContext, state: InputState):
    del ctx
    with open_blackbox_runtime_view(_input_bundle(state)) as runtime_view:
        try:
            yield runtime_view
        except ProcessGroupTerminationError:
            runtime_view.mark_termination_uncertain()
            raise


def _runner_bundle_kwargs(bundle: BlackboxInputBundle) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "data_snapshot_id": bundle.combined_snapshot_id,
        "platform_input_ids": bundle.platform_input_ids,
    }
    if bundle.platform_input_ids:
        kwargs.update(
            {
                "parent_data_snapshot_id": bundle.parent_snapshot_id,
                "input_identity_manifest": bundle.identity_manifest,
                "input_audit_manifest": bundle.audit_manifest,
            }
        )
    return kwargs


def _bundle_evidence(bundle: BlackboxInputBundle) -> list[Evidence]:
    return [
        Evidence("data_snapshot_id", bundle.combined_snapshot_id),
        Evidence("parent_data_snapshot_id", bundle.parent_snapshot_id),
        Evidence("platform_input_ids", list(bundle.platform_input_ids)),
        Evidence("platform_input_hashes", _platform_hashes(bundle)),
    ]


def _platform_hashes(bundle: BlackboxInputBundle) -> dict[str, str]:
    return {
        artifact.artifact_id: artifact.sha256
        for artifact in bundle.platform_input_artifacts
    }


def _ensure_input_state(ctx: GateContext, *, force: bool = False) -> InputState:
    state_key = "blackbox_v2.input_state"
    existing = ctx.runtime_state.get(state_key)
    if not force and existing is not None:
        if not isinstance(existing, InputState):
            raise ValueError("Blackbox runtime input state is invalid")
        return existing
    cfg = _config(ctx)
    metadata = _metadata(cfg)
    engine = ctx.engine_factory() if ctx.engine_factory is not None else create_default_engine()
    try:
        snapshot = get_blackbox_generation_snapshot(
            snapshot_date=ctx.predict_date,
            require_fresh=False,
        )
        calendar = get_calendar(engine)
        feature_date = (
            str(calendar.previous_trading_day(ctx.predict_date))[:10]
            if ctx.persist_backtest
            else _feature_date(metadata, ctx.predict_date, calendar)
        )
        cutoffs = resolve_blackbox_input_cutoffs(
            snapshot,
            feature_date=feature_date,
            engine=engine,
        )
        if ctx.persist_backtest:
            request = build_request(
                scheme_id=metadata.scheme_id,
                predict_date=feature_date,
                feature_date=feature_date,
                target_date=ctx.predict_date,
                cutoffs=cutoffs,
            )
        else:
            request = build_live_request(
                metadata,
                predict_date=ctx.predict_date,
                calendar=calendar,
                cutoffs=cutoffs,
            )
        platform_input_ids = tuple(cfg.platform_inputs)
        if platform_input_ids:
            with engine.connect() as connection:
                connection.exec_driver_sql(
                    "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
                )
                try:
                    platform_input_artifacts = (
                        capture_blackbox_platform_inputs_from_connection(
                            platform_input_ids,
                            connection=connection,
                            weekly_cutoff_key=cutoffs.weekly_cutoff_key,
                        )
                    )
                finally:
                    connection.rollback()
        else:
            platform_input_artifacts = ()
    finally:
        if hasattr(engine, "dispose"):
            engine.dispose()
    bundle = compose_blackbox_input_bundle(
        snapshot,
        platform_input_ids=platform_input_ids,
        platform_input_artifacts=platform_input_artifacts,
    )
    provenance = _data_bridge_provenance(ctx, snapshot)
    state = InputState(
        snapshot=snapshot,
        request=request,
        bundle=bundle,
        provenance=provenance,
    )
    ctx.runtime_state[state_key] = state
    return state


def _data_bridge_provenance(ctx: GateContext, snapshot: BlackboxSnapshot) -> dict[str, str]:
    # DataBridge state 由 refresh 生产者按 runtime root 解析；此处必须复用同一 canonical
    # 入口，不得复制其 development_default。不可变 release 中 backtest_artifacts/ 不存在。
    databridge_config = DataBridgeRefreshConfig.from_env()
    state_path = DataBridgeStore(
        data_root=databridge_config.data_root,
        runtime_root=databridge_config.runtime_root,
    ).state_path
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Blackbox all-stage requires readable DataBridge provenance: {exc}") from exc
    required = ("generation_id", "refresh_date", "refreshed_at", "business_digest")
    missing = [key for key in required if not isinstance(state.get(key), str) or not state[key].strip()]
    if missing:
        raise ValueError(f"DataBridge provenance is missing fields: {missing}")
    snapshot_identity = {
        "generation_id": snapshot.generation_id,
        "refresh_date": snapshot.refresh_date,
        "business_digest": snapshot.business_digest,
    }
    identity_mismatches = [
        key
        for key, expected in snapshot_identity.items()
        if not expected or state.get(key) != expected
    ]
    if identity_mismatches:
        raise ValueError(
            "DataBridge current identity changed after Harness snapshot: "
            f"{identity_mismatches}"
        )
    state_files = state.get("files")
    manifest_files = manifest.get("files")
    if not isinstance(state_files, dict) or not isinstance(manifest_files, dict):
        raise ValueError("DataBridge provenance file evidence is missing")
    mismatches = [
        name
        for name in SNAPSHOT_FILENAMES
        if not isinstance(state_files.get(name), dict)
        or not isinstance(manifest_files.get(name), dict)
        or state_files[name].get("sha256") != manifest_files[name].get("sha256")
    ]
    if mismatches:
        raise ValueError(f"DataBridge generation does not match Harness snapshot: {mismatches}")
    cfg = _config(ctx)
    return {
        "generation_id": str(state["generation_id"]),
        "refresh_date": str(state["refresh_date"]),
        "refreshed_at": str(state["refreshed_at"]),
        "business_digest": str(state["business_digest"]),
        "runtime_profile": str(cfg.runtime_profile),
        "environment_fingerprint": _environment_fingerprint(ctx.project_root),
    }


def cleanup_runtime_input(ctx: GateContext) -> None:
    """清除本次命令的进程内 Blackbox 输入状态。"""
    ctx.runtime_state.pop("blackbox_v2.input_state", None)


def _feature_date(metadata: BlackboxMetadata, predict_date: str, calendar: Any) -> str:
    from shared.blackbox_v2.requests import resolve_live_context

    return resolve_live_context(
        metadata,
        predict_date=predict_date,
        calendar=calendar,
    ).feature_date


def _script_violations(tree: ast.AST) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in FORBIDDEN_IMPORT_ROOTS:
                    violations.append(f"line {node.lineno}: forbidden import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root in FORBIDDEN_IMPORT_ROOTS:
                violations.append(f"line {node.lineno}: forbidden import {node.module}")
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in FORBIDDEN_CALLS or name in FORBIDDEN_QUALIFIED_CALLS:
                violations.append(f"line {node.lineno}: forbidden call {name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            literal = node.value.strip()
            if ABSOLUTE_PATH_PATTERN.match(literal):
                violations.append(f"line {node.lineno}: hard-coded absolute path is forbidden")
            if RELATIVE_TRAVERSAL_PATTERN.search(literal):
                violations.append(
                    f"line {node.lineno}: relative path traversal literal is forbidden"
                )
    return sorted(set(violations))


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _persisted_backtest_delta_errors(
    deltas: dict[str, int],
    *,
    expected_predictions: int,
    expected_metrics: int,
) -> list[str]:
    expected = {
        "t_backtest_runs": 1,
        "t_backtest_predictions": expected_predictions,
        "t_backtest_monthly_metrics": expected_metrics,
    }
    errors = [
        f"{table} delta must be {value}, got {deltas.get(table)}"
        for table, value in expected.items()
        if deltas.get(table) != value
    ]
    if expected_metrics <= 0:
        errors.append("persisted Blackbox backtest must produce monthly metrics")
    return errors


def _reload_pinned_blackbox_config(cfg: SchemeConfig, *, phase: str) -> SchemeConfig:
    current = load_scheme_config(cfg.path / "config.yaml")
    if current.scheme_version != cfg.scheme_version:
        raise _CanonicalSchemeVersionChanged(
            "Blackbox canonical version changed during "
            f"{phase}: expected={cfg.scheme_version}, current={current.scheme_version}"
        )
    if current.runtime_profile != DEFAULT_RUNTIME_PROFILE.name:
        raise ValueError(
            "Blackbox runtime_profile changed during "
            f"{phase}: expected={DEFAULT_RUNTIME_PROFILE.name}, current={current.runtime_profile}"
        )
    return current


def _persisted_backtest_provenance_errors(
    cfg: SchemeConfig,
    passed_run: PassedAllRun,
    state: InputState,
    provenance: dict[str, str],
    *,
    environment_fingerprint: str,
) -> list[str]:
    expected = {
        "data_snapshot_id": passed_run.data_snapshot_id,
        "generation_id": passed_run.generation_id,
        "runtime_profile": passed_run.runtime_profile,
        "environment_fingerprint": passed_run.environment_fingerprint,
    }
    current = {
        "data_snapshot_id": _input_bundle(state).combined_snapshot_id,
        "generation_id": str(provenance.get("generation_id", "")).strip() or None,
        "runtime_profile": cfg.runtime_profile,
        "environment_fingerprint": environment_fingerprint,
    }
    errors = [
        "persisted backtest provenance mismatch for "
        f"{field}: passed_all={expected[field]}, current={current[field]}"
        for field in expected
        if not expected[field] or current[field] != expected[field]
    ]
    if cfg.runtime_profile != DEFAULT_RUNTIME_PROFILE.name:
        errors.append(
            "persisted backtest runtime_profile must match frozen profile: "
            f"current={cfg.runtime_profile}, frozen={DEFAULT_RUNTIME_PROFILE.name}"
        )
    provenance_profile = str(provenance.get("runtime_profile", "")).strip() or None
    if provenance_profile != cfg.runtime_profile:
        errors.append(
            "persisted backtest runtime_profile provenance mismatch: "
            f"DataBridge={provenance_profile}, config={cfg.runtime_profile}"
        )
    provenance_environment = str(provenance.get("environment_fingerprint", "")).strip() or None
    if provenance_environment != environment_fingerprint:
        errors.append(
            "persisted backtest environment_fingerprint provenance mismatch: "
            f"DataBridge={provenance_environment}, current={environment_fingerprint}"
        )
    return errors


def _verify_passed_all(engine, cfg: SchemeConfig) -> PassedAllRun:
    from sqlalchemy import text

    # 从唯一的序列定义派生，不再写第二份。写死过一次已经导致：自动段缩短后，
    # 依赖本函数的 shadow-register / backtest --persist / activate 三条路径全部
    # 因「missing=['backtest','dry-run']」而阻断。
    from harness.registry import BLACKBOX_AUTO_SEQUENCE

    required = set(BLACKBOX_AUTO_SEQUENCE)
    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT harness_run_id
                FROM t_harness_runs
                WHERE scheme_id = :scheme_id
                  AND scheme_version = :scheme_version
                  AND stage = 'all'
                  AND status = 'passed'
                ORDER BY finished_at DESC, harness_run_id DESC
                LIMIT 1
                """
            ),
            {"scheme_id": cfg.scheme_id, "scheme_version": cfg.scheme_version},
        ).mappings().one_or_none()
        if row is None:
            raise ValueError(
                f"no passed all-stage harness run for {cfg.scheme_id} version {cfg.scheme_version}"
            )
        gate_rows = [
            (str(item[0]), str(item[1]), item[2])
            for item in connection.execute(
                text(
                    """
                    SELECT gate_name, status, summary_json
                    FROM t_harness_gate_results
                    WHERE harness_run_id = :harness_run_id
                    """
                ),
                {"harness_run_id": row["harness_run_id"]},
            ).fetchall()
        ]
    names = [name for name, _status, _summary in gate_rows]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    missing = sorted(required - set(names))
    extra = sorted(set(names) - required)
    non_passed = sorted(
        f"{name}={status}"
        for name, status, _summary in gate_rows
        if status != "passed"
    )
    if (
        len(gate_rows) != len(required)
        or duplicates
        or missing
        or extra
        or non_passed
    ):
        raise ValueError(
            "all-stage run must have exact persisted Blackbox V2 gate set: "
            f"row_count={len(gate_rows)}, duplicates={duplicates}, "
            f"missing={missing}, extra={extra}, non_passed={non_passed}"
        )
    input_summaries = [
        summary for name, _status, summary in gate_rows if name == "input"
    ]
    if len(input_summaries) != 1:
        raise ValueError("passed all-stage run has no unique input evidence")
    evidence = _decode_harness_evidence(input_summaries[0])
    required_evidence = (
        "data_snapshot_id",
        "generation_id",
        "runtime_profile",
        "environment_fingerprint",
    )
    missing_evidence = [
        key
        for key in required_evidence
        if not isinstance(evidence.get(key), str)
        or not str(evidence[key]).strip()
    ]
    if missing_evidence:
        raise ValueError(
            "passed all-stage input evidence is incomplete: "
            f"missing={missing_evidence}"
        )
    return PassedAllRun(
        harness_run_id=str(row["harness_run_id"]),
        data_snapshot_id=str(evidence["data_snapshot_id"]),
        generation_id=str(evidence["generation_id"]),
        runtime_profile=str(evidence["runtime_profile"]),
        environment_fingerprint=str(evidence["environment_fingerprint"]),
    )


def _decode_harness_evidence(value: object) -> dict[str, object]:
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Harness gate evidence is malformed") from exc
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Harness gate evidence is malformed") from exc
    if not isinstance(value, dict) or not isinstance(value.get("evidence"), list):
        raise ValueError("Harness gate evidence is malformed")
    evidence: dict[str, object] = {}
    for item in value["evidence"]:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            raise ValueError("Harness gate evidence is malformed")
        key = item["key"]
        if key in evidence or "value" not in item:
            raise ValueError("Harness gate evidence is malformed")
        evidence[key] = item["value"]
    return evidence


def _register_shadow(
    engine,
    validated_cfg: SchemeConfig,
    shadow_cfg: SchemeConfig,
) -> BlackboxLifecycleState:
    from scheduler.repository import apply_blackbox_lifecycle_state

    canonical_fields = (
        "scheme_id",
        "scheme_version",
        "code_hash",
        "config_hash",
        "manifest_hash",
    )
    mismatches = [
        field
        for field in canonical_fields
        if getattr(validated_cfg, field) != getattr(shadow_cfg, field)
    ]
    if mismatches:
        raise ValueError(f"shadow registration changed canonical identity: {mismatches}")
    return apply_blackbox_lifecycle_state(
        engine,
        shadow_cfg,
        version_status="shadow",
        registry_status="paused",
    )


class _CanonicalSchemeVersionChanged(RuntimeError):
    pass


def _read_shadow_state(engine, cfg: SchemeConfig):
    from scheduler.repository import read_blackbox_lifecycle_state

    return read_blackbox_lifecycle_state(engine, cfg)


def _environment_fingerprint(project_root: Path) -> str:
    from shared.blackbox_v2.environment_manifest import (
        environment_manifest_path,
        runtime_environment_platform,
    )

    expected_platform = runtime_environment_platform()
    path = environment_manifest_path(project_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Blackbox V2 environment manifest {path}: {exc}") from exc
    fingerprint = raw.get("environment_fingerprint") if isinstance(raw, dict) else None
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("environment manifest must contain a SHA-256 environment_fingerprint")
    payload = raw.get("explicit_packages")
    if not isinstance(payload, list):
        raise ValueError("environment manifest must contain explicit_packages")
    if raw.get("runtime_profile") != DEFAULT_RUNTIME_PROFILE.name:
        raise ValueError(
            "environment manifest runtime_profile must match frozen Blackbox runtime profile"
        )
    if raw.get("platform") != expected_platform:
        raise ValueError(
            "environment manifest platform must match runtime platform"
        )
    computed = hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if computed != fingerprint:
        raise ValueError("environment manifest fingerprint does not match explicit_packages")
    return fingerprint


def _finish(
    gate_name: str,
    started_at: str,
    evidence: list[Evidence],
    errors: list[str],
) -> GateResult:
    status = GateStatus.PASSED if not errors else GateStatus.FAILED
    return GateResult(
        gate_name=gate_name,
        status=status,
        evidence=evidence,
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
    )


def _blocked(gate_name: str, started_at: str, errors: list[str]) -> GateResult:
    return GateResult(
        gate_name=gate_name,
        status=GateStatus.BLOCKED,
        evidence=[Evidence("operation_required", True)],
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
    )
