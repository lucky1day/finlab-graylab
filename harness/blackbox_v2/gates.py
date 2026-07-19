from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from harness.context import GateContext
from harness.authorization import (
    authorization_token_hash,
    mark_token_used,
    used_tokens_path,
    verify_authorization,
    write_authorization_audit,
)
from harness.gates.base import Gate, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus
from scheduler.blackbox_v2_runner import (
    DEFAULT_RUNTIME_PROFILE,
    BlackboxExecutionError,
    RuntimeProfile,
    execute_blackbox_cli,
    probe_blackbox_help,
    run_blackbox_backtest,
    run_blackbox_predict,
)
from scheduler.discovery import SchemeConfig, load_scheme_config
from shared.blackbox_v2.contracts import BlackboxMetadata, BlackboxRequest, load_metadata, load_request
from shared.blackbox_v2.requests import build_live_request, write_request
from shared.blackbox_v2.snapshot import BlackboxSnapshot, SNAPSHOT_FILENAMES
from shared.calendar_service import get_calendar
from shared.input_artifacts import build_blackbox_input_snapshot, resolve_blackbox_input_cutoffs
from shared.blackbox_v2.lifecycle import (
    LifecycleOperationError,
    LifecycleState,
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


@dataclass(frozen=True)
class InputState:
    snapshot: BlackboxSnapshot
    request_path: Path
    request: BlackboxRequest


@dataclass(frozen=True)
class PassedAllRun:
    harness_run_id: str
    report_uri: Path
    data_snapshot_id: str
    generation_id: str | None = None
    runtime_profile: str | None = None
    environment_fingerprint: str | None = None


class _BlackboxGate(Gate):
    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        raise NotImplementedError


class BlackboxStaticGate(_BlackboxGate):
    name = "static"

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        metadata = _metadata(cfg)
        scheme_dir = cfg.path
        delivery_dir = scheme_dir / "delivery"
        errors: list[str] = []
        scheme_entries = {path.name for path in scheme_dir.iterdir()}
        delivery_entries = {path.name for path in delivery_dir.iterdir()} if delivery_dir.is_dir() else set()
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
        ]
        return _finish(self.name, started_at, evidence, errors)


class BlackboxInputGate(_BlackboxGate):
    name = "input"

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        state = _ensure_input_state(ctx, force=True)
        manifest = json.loads(state.snapshot.manifest_path.read_text(encoding="utf-8"))
        evidence = [
            Evidence("data_snapshot_id", state.snapshot.snapshot_id),
            Evidence("data_schema_version", state.snapshot.schema_version),
            Evidence(
                "files",
                {
                    name: {
                        "row_count": manifest["files"][name]["row_count"],
                        "column_count": len(manifest["files"][name]["columns"]),
                        "sha256": manifest["files"][name]["sha256"],
                    }
                    for name in SNAPSHOT_FILENAMES
                },
            ),
            Evidence("request", asdict(state.request)),
        ]
        return _finish(self.name, started_at, evidence, [])


class BlackboxUnitGate(_BlackboxGate):
    name = "unit"

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        state = _ensure_input_state(ctx)
        root = _gate_root(ctx) / "unit"
        request_dir = root / "request"
        request_dir.mkdir(parents=True, exist_ok=True)
        invalid_request = request_dir / "invalid_request.json"
        invalid_request.write_text("{}\n", encoding="utf-8")
        output_dir = root / "output"
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir()
        output = output_dir / "invalid_output.json"
        help_output = probe_blackbox_help(_script(cfg), profile=_profile(ctx))
        rejected = False
        try:
            execute_blackbox_cli(
                script_path=_script(cfg),
                mode="predict",
                input_path=invalid_request,
                data_dir=state.snapshot.data_dir,
                output_path=output,
                profile=_profile(ctx),
            )
        except BlackboxExecutionError:
            rejected = True
        errors: list[str] = []
        if not rejected:
            errors.append("script must reject an invalid Request with a non-zero exit")
        if output.exists():
            errors.append("failed execution must not leave an Output file")
        evidence = [
            Evidence("help_exposes_modes", {"predict": "predict" in help_output, "backtest": "backtest" in help_output}),
            Evidence("invalid_request_rejected", rejected),
            Evidence("failed_output_absent", not output.exists()),
        ]
        return _finish(self.name, started_at, evidence, errors)


class BlackboxDryRunGate(_BlackboxGate):
    name = "dry-run"

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        metadata = _metadata(cfg)
        state = _ensure_input_state(ctx)
        record = run_blackbox_predict(
            metadata=metadata,
            script_path=_script(cfg),
            request=state.request,
            data_dir=state.snapshot.data_dir,
            data_snapshot_id=state.snapshot.snapshot_id,
            profile=_profile(ctx),
        )
        result_path = _gate_root(ctx) / "dry_run_prediction_record.json"
        result_path.write_text(json.dumps(asdict(record), ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        evidence = [
            Evidence("prediction_record", asdict(record)),
            Evidence("result_path", str(result_path)),
            Evidence("business_tables_written", False),
        ]
        return _finish(self.name, started_at, evidence, [])


class BlackboxCompareGate(_BlackboxGate):
    name = "compare"

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        metadata = _metadata(cfg)
        state = _ensure_input_state(ctx)
        profile = _profile(ctx)
        baseline = run_blackbox_predict(
            metadata=metadata,
            script_path=_script(cfg),
            request=state.request,
            data_dir=state.snapshot.data_dir,
            data_snapshot_id=state.snapshot.snapshot_id,
            profile=profile,
        )
        repeated = run_blackbox_predict(
            metadata=metadata,
            script_path=_script(cfg),
            request=state.request,
            data_dir=state.snapshot.data_dir,
            data_snapshot_id=state.snapshot.snapshot_id,
            profile=profile,
        )

        batch = _comparison_requests(state.request, state.snapshot.data_dir)
        earlier = run_blackbox_predict(
            metadata=metadata,
            script_path=_script(cfg),
            request=batch[0],
            data_dir=state.snapshot.data_dir,
            data_snapshot_id=state.snapshot.snapshot_id,
            profile=profile,
        )
        unsplit = run_blackbox_backtest(
            metadata=metadata,
            script_path=_script(cfg),
            requests=batch,
            data_dir=state.snapshot.data_dir,
            data_snapshot_id=state.snapshot.snapshot_id,
            profile=profile,
        )
        split = run_blackbox_backtest(
            metadata=metadata,
            script_path=_script(cfg),
            requests=batch,
            data_dir=state.snapshot.data_dir,
            data_snapshot_id=state.snapshot.snapshot_id,
            profile=replace(profile, max_batch_requests=1),
        )
        reversed_records = run_blackbox_backtest(
            metadata=metadata,
            script_path=_script(cfg),
            requests=list(reversed(batch)),
            data_dir=state.snapshot.data_dir,
            data_snapshot_id=state.snapshot.snapshot_id,
            profile=profile,
        )

        with tempfile.TemporaryDirectory(prefix="blackbox-v2-future-isolation-") as tmpdir:
            mutated_data = Path(tmpdir) / "data"
            shutil.copytree(state.snapshot.data_dir, mutated_data)
            appended_rows = _append_future_rows(mutated_data)
            future_record = run_blackbox_predict(
                metadata=metadata,
                script_path=_script(cfg),
                request=state.request,
                data_dir=mutated_data,
                data_snapshot_id=f"{state.snapshot.snapshot_id}-future-probe",
                profile=profile,
            )

        baseline_direction = baseline.predicted_direction
        errors: list[str] = []
        if repeated.predicted_direction != baseline_direction:
            errors.append("repeated predict result is not deterministic")
        direct_directions = {
            batch[0].request_id: earlier.predicted_direction,
            state.request.request_id: baseline_direction,
        }
        if _direction_map(unsplit) != direct_directions:
            errors.append("predict and backtest produce different directions")
        if _direction_map(unsplit) != _direction_map(split):
            errors.append("backtest result changes when platform splits batches")
        if _direction_map(unsplit) != _direction_map(reversed_records):
            errors.append("backtest result changes when Request order changes")
        if future_record.predicted_direction != baseline_direction:
            errors.append("prediction changes after rows beyond cutoff keys are appended")
        evidence = [
            Evidence("repeat_deterministic", repeated.predicted_direction == baseline_direction),
            Evidence("predict_backtest_equal", _direction_map(unsplit) == direct_directions),
            Evidence(
                "distinct_cutoff_requests",
                [
                    {
                        "daily": item.daily_cutoff_key,
                        "weekly": item.weekly_cutoff_key,
                        "monthly": item.monthly_cutoff_key,
                    }
                    for item in batch
                ],
            ),
            Evidence("batch_split_invariant", _direction_map(unsplit) == _direction_map(split)),
            Evidence("request_order_invariant", _direction_map(unsplit) == _direction_map(reversed_records)),
            Evidence("future_row_isolation", future_record.predicted_direction == baseline_direction),
            Evidence("future_rows_appended", appended_rows),
        ]
        return _finish(self.name, started_at, evidence, errors)


class BlackboxBacktestGate(_BlackboxGate):
    name = "backtest"

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        metadata = _metadata(cfg)
        state = _ensure_input_state(ctx)
        templates = _comparison_requests(state.request, state.snapshot.data_dir)
        requests = [
            replace(
                templates[index % len(templates)],
                request_id=f"{state.request.request_id}:batch:{index:03d}",
            )
            for index in range(100)
        ]
        records = run_blackbox_backtest(
            metadata=metadata,
            script_path=_script(cfg),
            requests=requests,
            data_dir=state.snapshot.data_dir,
            data_snapshot_id=state.snapshot.snapshot_id,
            profile=_profile(ctx),
        )
        errors: list[str] = []
        if len(records) != 100:
            errors.append(f"100-row no-persist backtest returned {len(records)} records")
        if [record.extra["request_id"] for record in records] != [item.request_id for item in requests]:
            errors.append("100-row backtest did not preserve one-to-one Request order")
        evidence = [
            Evidence("requests", len(requests)),
            Evidence("records", len(records)),
            Evidence("distinct_cutoff_sets", len(templates)),
            Evidence("persist", False),
            Evidence("business_tables_written", False),
        ]
        return _finish(self.name, started_at, evidence, errors)


class BlackboxApiReadinessGate(_BlackboxGate):
    name = "api-readiness"

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        metadata = _metadata(cfg)
        state = _ensure_input_state(ctx)
        record = run_blackbox_predict(
            metadata=metadata,
            script_path=_script(cfg),
            request=state.request,
            data_dir=state.snapshot.data_dir,
            data_snapshot_id=state.snapshot.snapshot_id,
            profile=_profile(ctx),
        )
        registry_id = f"{cfg.scheme_id}__h{cfg.horizon}__{metadata.target_tenor}"
        errors: list[str] = []
        if record.scheme_id != cfg.scheme_id:
            errors.append("PredictionRecord scheme_id does not match base scheme identity")
        if record.target_tenor != metadata.target_tenor or record.horizon != metadata.horizon:
            errors.append("PredictionRecord target identity does not match Metadata")
        if cfg.status != "paused":
            errors.append("Blackbox V2 trial config must remain paused before shadow registration")
        evidence = [
            Evidence("prediction_record", asdict(record)),
            Evidence("registry_id", registry_id),
            Evidence("registry_status", cfg.status),
            Evidence("scheduler_eligible", False),
            Evidence("api_visible", False),
        ]
        return _finish(self.name, started_at, evidence, errors)


class BlackboxShadowRegisterGate(_BlackboxGate):
    name = "shadow-register"
    requires_authorization = True

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        try:
            from harness.blackbox_v2.activation import reconcile_incomplete_before_authorization

            cfg = reconcile_incomplete_before_authorization(ctx, cfg)
        except RuntimeError as exc:
            return _blocked(self.name, started_at, [str(exc)])
        auth, auth_errors = verify_authorization(
            ctx.authorization,
            scheme_id=ctx.scheme_id,
            action="shadow_register",
            predict_date=ctx.predict_date,
            used_store_path=used_tokens_path(ctx.project_root),
        )
        if auth_errors or auth is None:
            return _blocked(self.name, started_at, auth_errors)
        if auth.scheme_version != cfg.scheme_version:
            return _blocked(
                self.name,
                started_at,
                [
                    "authorization scheme_version must match the validated delivery: "
                    f"token={auth.scheme_version}, current={cfg.scheme_version}"
                ],
            )

        engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
        config_path = cfg.path / "config.yaml"
        audit_path: Path | None = None
        registered_state = None
        try:
            passed_run = _verify_passed_all(engine, cfg)
            if auth.harness_run_id != passed_run.harness_run_id:
                return _blocked(
                    self.name,
                    started_at,
                    [
                        "authorization harness_run_id must match the passed all-stage run: "
                        f"token={auth.harness_run_id}, passed={passed_run.harness_run_id}"
                    ],
                )
            environment_fingerprint = _environment_fingerprint(ctx.project_root)
            validated_cfg = replace(
                cfg,
                version_status="validated",
                environment_fingerprint=environment_fingerprint,
                data_snapshot_id=passed_run.data_snapshot_id,
            )
            db_before = _read_shadow_state(engine, cfg)
            previous = LifecycleState(cfg.status, db_before.version_status, db_before.registry_status)
            if previous.config_status != "paused" or previous.registry_status != "paused":
                raise ValueError(f"shadow registration requires paused safe state, got {previous}")
            target = LifecycleState("paused", "shadow", "paused")

            def consume() -> None:
                nonlocal audit_path
                mark_token_used(auth, used_tokens_path(ctx.project_root))
                audit_path = write_authorization_audit(auth, ctx.report_dir / "shadow_authorization")

            def enriched_current():
                return replace(
                    load_scheme_config(config_path),
                    environment_fingerprint=environment_fingerprint,
                    data_snapshot_id=passed_run.data_snapshot_id,
                )

            def apply_database(state: LifecycleState) -> None:
                nonlocal registered_state
                current = enriched_current()
                if state == target:
                    registered_state = _register_shadow(engine, validated_cfg, current)
                    return
                from scheduler.repository import apply_blackbox_lifecycle_state

                apply_blackbox_lifecycle_state(
                    engine,
                    current,
                    version_status=state.version_status,
                    registry_status=state.registry_status,
                )

            def read_state() -> LifecycleState:
                current = enriched_current()
                db_state = _read_shadow_state(engine, current)
                return LifecycleState(current.status, db_state.version_status, db_state.registry_status)

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
                token_hash=authorization_token_hash(auth),
                consume_authorization=consume,
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
            Evidence("authorization_audit_path", str(audit_path)),
            Evidence("journal_path", str(journal_path)),
            Evidence("journal_phase", "verified"),
        ]
        result = _finish(self.name, started_at, evidence, [])
        return replace(result, report_path=audit_path)


BLACKBOX_GATES: dict[str, type[Gate]] = {
    "static": BlackboxStaticGate,
    "input": BlackboxInputGate,
    "unit": BlackboxUnitGate,
    "dry-run": BlackboxDryRunGate,
    "compare": BlackboxCompareGate,
    "backtest": BlackboxBacktestGate,
    "api-readiness": BlackboxApiReadinessGate,
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


def _ensure_input_state(ctx: GateContext, *, force: bool = False) -> InputState:
    root = _gate_root(ctx)
    state_path = root / "input_state.json"
    if state_path.exists() and not force:
        try:
            return _read_input_state(state_path)
        except (OSError, ValueError):
            pass
    cfg = _config(ctx)
    metadata = _metadata(cfg)
    engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
    try:
        snapshot = build_blackbox_input_snapshot(
            snapshot_date=ctx.predict_date,
            output_root=root / "runtime_snapshot",
            require_fresh=False,
        )
        cutoffs = resolve_blackbox_input_cutoffs(
            snapshot,
            feature_date=_feature_date(metadata, ctx.predict_date, engine),
            engine=engine,
        )
        request = build_live_request(
            metadata,
            predict_date=ctx.predict_date,
            calendar=get_calendar(engine),
            cutoffs=cutoffs,
        )
    finally:
        if hasattr(engine, "dispose"):
            engine.dispose()
    request_path = write_request(request, root / "request.json")
    provenance = _data_bridge_provenance(ctx, snapshot)
    payload = {
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_root": str(snapshot.root_dir),
        "data_dir": str(snapshot.data_dir),
        "manifest_path": str(snapshot.manifest_path),
        "schema_version": snapshot.schema_version,
        "request_path": str(request_path),
        **provenance,
    }
    _atomic_write(state_path, json.dumps(payload, ensure_ascii=True, indent=2) + "\n")
    return InputState(snapshot=snapshot, request_path=request_path, request=request)


def _data_bridge_provenance(ctx: GateContext, snapshot: BlackboxSnapshot) -> dict[str, str]:
    state_path = ctx.project_root / "backtest_artifacts" / "data_bridge_refresh" / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Blackbox all-stage requires readable DataBridge provenance: {exc}") from exc
    required = ("generation_id", "refresh_date", "refreshed_at", "business_digest")
    missing = [key for key in required if not isinstance(state.get(key), str) or not state[key].strip()]
    if missing:
        raise ValueError(f"DataBridge provenance is missing fields: {missing}")
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
    """删除 Harness 临时三频副本，保留 input_state.json 审计记录。"""
    runtime_root = ctx.report_dir / "blackbox_v2" / "runtime_snapshot"
    if not runtime_root.exists():
        return
    for current_root, directories, files in os.walk(runtime_root):
        Path(current_root).chmod(0o755)
        for directory in directories:
            (Path(current_root) / directory).chmod(0o755)
        for filename in files:
            (Path(current_root) / filename).chmod(0o644)
    shutil.rmtree(runtime_root)


def _feature_date(metadata: BlackboxMetadata, predict_date: str, engine: Any) -> str:
    calendar = get_calendar(engine)
    if metadata.frequency == "daily":
        return calendar.previous_trading_day(predict_date)
    if metadata.frequency == "weekly":
        from shared.prediction_context import build_weekly_live_context

        return build_weekly_live_context(calendar, predict_date).feature_date
    from shared.prediction_context import build_monthly_live_context

    return build_monthly_live_context(calendar, predict_date).feature_date


def _read_input_state(path: Path) -> InputState:
    raw = json.loads(path.read_text(encoding="utf-8"))
    snapshot = BlackboxSnapshot(
        snapshot_id=str(raw["snapshot_id"]),
        root_dir=Path(raw["snapshot_root"]),
        data_dir=Path(raw["data_dir"]),
        manifest_path=Path(raw["manifest_path"]),
        schema_version=str(raw["schema_version"]),
    )
    request_path = Path(raw["request_path"])
    if not snapshot.manifest_path.is_file() or not snapshot.data_dir.is_dir() or not request_path.is_file():
        raise ValueError(f"Blackbox V2 input state references missing artifacts: {path}")
    return InputState(snapshot=snapshot, request_path=request_path, request=load_request(request_path))


def _create_engine():
    from scheduler.repository import create_engine_from_env

    return create_engine_from_env()


def _gate_root(ctx: GateContext) -> Path:
    root = ctx.report_dir / "blackbox_v2"
    root.mkdir(parents=True, exist_ok=True)
    return root


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
            if ABSOLUTE_PATH_PATTERN.match(node.value.strip()):
                violations.append(f"line {node.lineno}: hard-coded absolute path is forbidden")
    return sorted(set(violations))


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _append_future_rows(data_dir: Path) -> dict[str, int]:
    key_values = {
        "daily_output.csv": ("date", "2999-12-31"),
        "weekly_output.csv": ("week_id", "999998"),
        "monthly_output.csv": ("month_id", "999998"),
    }
    counts: dict[str, int] = {}
    for filename, (key, future_value) in key_values.items():
        path = data_dir / filename
        path.chmod(0o644)
        frame = pd.read_csv(path, dtype=str)
        if frame.empty or key not in frame.columns:
            raise ValueError(f"cannot build future-row probe for {filename}")
        row = frame.iloc[-1].copy()
        row[key] = future_value
        for column in frame.columns:
            if column != key:
                row[column] = "9.999e99"
        frame = pd.concat([frame, row.to_frame().T], ignore_index=True)
        frame.to_csv(path, index=False, lineterminator="\n")
        counts[filename] = 1
    return counts


def _comparison_requests(request: BlackboxRequest, data_dir: Path) -> list[BlackboxRequest]:
    cutoff_specs = (
        ("daily_output.csv", "date", "daily_cutoff_key"),
        ("weekly_output.csv", "week_id", "weekly_cutoff_key"),
        ("monthly_output.csv", "month_id", "monthly_cutoff_key"),
    )
    previous: dict[str, str] = {}
    for filename, key_column, request_field in cutoff_specs:
        frame = pd.read_csv(data_dir / filename, dtype=str, keep_default_na=False)
        if filename == "daily_output.csv":
            try:
                values = pd.to_datetime(frame[key_column], errors="raise").dt.date.astype(str).tolist()
            except (TypeError, ValueError) as exc:
                raise ValueError("daily_output.csv contains an invalid date") from exc
        else:
            values = [str(value).strip().removesuffix(".0") for value in frame[key_column].tolist()]
        current = str(getattr(request, request_field))
        try:
            current_index = values.index(current)
        except ValueError as exc:
            raise ValueError(f"Request {request_field}={current} is absent from {filename}") from exc
        if current_index == 0:
            raise ValueError(
                f"CompareGate requires one earlier {key_column} before {current} in {filename}"
            )
        previous[request_field] = values[current_index - 1]
    earlier = replace(
        request,
        request_id=f"{request.request_id}:prior-cutoffs",
        **previous,
    )
    return [earlier, request]


def _direction_map(records) -> dict[str, int]:
    return {str(record.extra["request_id"]): int(record.predicted_direction) for record in records}


def _verify_passed_all(engine, cfg: SchemeConfig) -> PassedAllRun:
    from sqlalchemy import text

    required = {
        "static",
        "input",
        "unit",
        "dry-run",
        "compare",
        "backtest",
        "api-readiness",
    }
    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT harness_run_id, report_uri
                FROM t_harness_runs
                WHERE scheme_id = :scheme_id
                  AND scheme_version = :scheme_version
                  AND stage = 'all'
                  AND status = 'passed'
                ORDER BY finished_at DESC
                LIMIT 1
                """
            ),
            {"scheme_id": cfg.scheme_id, "scheme_version": cfg.scheme_version},
        ).mappings().one_or_none()
        if row is None:
            raise ValueError(
                f"no passed all-stage harness run for {cfg.scheme_id} version {cfg.scheme_version}"
            )
        statuses = {
            str(item[0]): str(item[1])
            for item in connection.execute(
                text(
                    """
                    SELECT gate_name, status
                    FROM t_harness_gate_results
                    WHERE harness_run_id = :harness_run_id
                    """
                ),
                {"harness_run_id": row["harness_run_id"]},
            ).fetchall()
        }
    missing = sorted(name for name in required if statuses.get(name) != "passed")
    if missing:
        raise ValueError(f"all-stage run has missing or non-passed Blackbox V2 gates: {missing}")
    report_uri = Path(str(row["report_uri"]))
    report_dir = report_uri.parent if report_uri.is_file() or report_uri.suffix == ".json" else report_uri
    state_path = report_dir / "blackbox_v2" / "input_state.json"
    if not state_path.is_file():
        raise ValueError(f"passed all-stage run has no Blackbox V2 input state: {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    return PassedAllRun(
        harness_run_id=str(row["harness_run_id"]),
        report_uri=report_dir,
        data_snapshot_id=str(state["snapshot_id"]),
        generation_id=(str(state["generation_id"]) if state.get("generation_id") else None),
        runtime_profile=(str(state["runtime_profile"]) if state.get("runtime_profile") else None),
        environment_fingerprint=(
            str(state["environment_fingerprint"]) if state.get("environment_fingerprint") else None
        ),
    )


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


def _read_shadow_state(engine, cfg: SchemeConfig):
    from scheduler.repository import read_blackbox_lifecycle_state

    return read_blackbox_lifecycle_state(engine, cfg)


def _environment_fingerprint(project_root: Path) -> str:
    path = project_root / "deploy" / "blackbox_v2" / "environment_manifest.json"
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
    computed = hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if computed != fingerprint:
        raise ValueError("environment manifest fingerprint does not match explicit_packages")
    return fingerprint


def _atomic_write(path: Path, text_value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text_value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


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
        passed=status == GateStatus.PASSED,
        evidence=evidence,
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
    )


def _blocked(gate_name: str, started_at: str, errors: list[str]) -> GateResult:
    return GateResult(
        gate_name=gate_name,
        status=GateStatus.BLOCKED,
        passed=False,
        evidence=[Evidence("authorization_required", True)],
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
    )
