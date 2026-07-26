from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from harness.context import GateContext
from harness.authorization import (
    authorization_signing_enabled,
    authorization_token_hash,
    mark_token_used,
    required_future_expiry_errors,
    used_tokens_path,
    verify_authorization,
    write_authorization_audit,
)
from backtests.blackbox_v2 import run_blackbox_historical_backtest
from backtests.repository import persist_backtest_output_atomic, snapshot_backtest_scope_counts
from harness.gates.base import Gate, guarded_result, utc_now
from harness.probes.table_guard import diff_snapshots
from harness.result import Evidence, GateResult, GateStatus
from scheduler.blackbox_v2_runner import (
    BacktestExecutionBudget,
    DEFAULT_RUNTIME_PROFILE,
    BlackboxExecutionError,
    RuntimeProfile,
    execute_blackbox_cli,
    probe_blackbox_help,
    run_blackbox_backtest,
    run_blackbox_predict,
)
from scheduler.process_control import ProcessGroupTerminationError
from scheduler.discovery import SchemeConfig, load_scheme_config
from shared.blackbox_v2.contracts import (
    BlackboxMetadata,
    BlackboxRequest,
    load_metadata,
    load_request_bytes,
)
from shared.blackbox_v2.history import CURRENT_SNAPSHOT_REPLAY, build_historical_cases
from shared.blackbox_v2.requests import build_live_request, write_request
from shared.blackbox_v2.platform_inputs import FrozenPlatformInput
from shared.blackbox_v2.snapshot import (
    BlackboxInputBundle,
    BlackboxSnapshot,
    SNAPSHOT_FILENAMES,
    compose_blackbox_input_bundle,
    create_snapshot_from_frames,
)
from shared.calendar_service import get_calendar
from shared.input_artifacts import (
    build_blackbox_input_snapshot,
    capture_blackbox_platform_inputs_from_connection,
    open_blackbox_runtime_view,
    resolve_blackbox_input_cutoffs,
)
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
INPUT_STATE_INITIALIZED_SEAL = (
    b'{"schema_version":"blackbox-input-state-initialized-v1"}\n'
)


@dataclass(frozen=True)
class InputState:
    snapshot: BlackboxSnapshot
    request_path: Path
    request: BlackboxRequest
    bundle: BlackboxInputBundle | None = None


@dataclass(frozen=True)
class _StablePrivateFile:
    content_bytes: bytes
    sha256: str
    size_bytes: int
    mode: int


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
            Evidence("data_schema_version", state.snapshot.schema_version),
            Evidence("files", base_snapshot_files),
            Evidence("base_snapshot_files", base_snapshot_files),
            Evidence("platform_input_files", platform_files),
            Evidence("input_identity_manifest", bundle.identity_manifest),
            Evidence("input_audit_manifest", bundle.audit_manifest),
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
        with _open_runtime_input(ctx, state) as runtime_view:
            try:
                execute_blackbox_cli(
                    script_path=_script(cfg),
                    mode="predict",
                    input_path=invalid_request,
                    data_dir=runtime_view.data_dir,
                    output_path=output,
                    profile=_profile(ctx),
                    platform_input_ids=runtime_view.bundle.platform_input_ids,
                )
            except BlackboxExecutionError:
                rejected = True
        errors: list[str] = []
        if not rejected:
            errors.append("script must reject an invalid Request with a non-zero exit")
        if output.exists():
            errors.append("failed execution must not leave an Output file")
        evidence = [
            *_bundle_evidence(_input_bundle(state)),
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
        with _open_runtime_input(ctx, state) as runtime_view:
            record = run_blackbox_predict(
                metadata=metadata,
                script_path=_script(cfg),
                request=state.request,
                data_dir=runtime_view.data_dir,
                profile=_profile(ctx),
                **_runner_bundle_kwargs(runtime_view.bundle),
            )
        result_path = _gate_root(ctx) / "dry_run_prediction_record.json"
        result_path.write_text(json.dumps(asdict(record), ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        evidence = [
            *_bundle_evidence(_input_bundle(state)),
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
        bundle = _input_bundle(state)
        with _open_runtime_input(ctx, state) as runtime_view:
            original_platform_hashes = _verify_runtime_platform_files(
                runtime_view.bundle,
                runtime_view.data_dir,
            )
            runtime_kwargs = _runner_bundle_kwargs(runtime_view.bundle)
            baseline = run_blackbox_predict(
                metadata=metadata,
                script_path=_script(cfg),
                request=state.request,
                data_dir=runtime_view.data_dir,
                profile=profile,
                **runtime_kwargs,
            )
            repeated = run_blackbox_predict(
                metadata=metadata,
                script_path=_script(cfg),
                request=state.request,
                data_dir=runtime_view.data_dir,
                profile=profile,
                **runtime_kwargs,
            )

            batch = _comparison_requests(
                state.request,
                runtime_view.data_dir,
            )
            earlier = run_blackbox_predict(
                metadata=metadata,
                script_path=_script(cfg),
                request=batch[0],
                data_dir=runtime_view.data_dir,
                profile=profile,
                **runtime_kwargs,
            )
            unsplit = run_blackbox_backtest(
                metadata=metadata,
                script_path=_script(cfg),
                requests=batch,
                data_dir=runtime_view.data_dir,
                profile=profile,
                **runtime_kwargs,
            )
            split = run_blackbox_backtest(
                metadata=metadata,
                script_path=_script(cfg),
                requests=batch,
                data_dir=runtime_view.data_dir,
                profile=replace(profile, max_batch_requests=1),
                **runtime_kwargs,
            )
            reversed_records = run_blackbox_backtest(
                metadata=metadata,
                script_path=_script(cfg),
                requests=list(reversed(batch)),
                data_dir=runtime_view.data_dir,
                profile=profile,
                **runtime_kwargs,
            )

            with tempfile.TemporaryDirectory(
                prefix="blackbox-v2-future-isolation-"
            ) as tmpdir:
                probe_root = Path(tmpdir)
                mutated_data = probe_root / "data"
                shutil.copytree(runtime_view.data_dir, mutated_data)
                appended_rows = _append_future_rows(mutated_data)
                mutated_platform_hashes = (
                    _verify_runtime_platform_files(
                        bundle,
                        mutated_data,
                    )
                )
                mutated_frames = {
                    filename: pd.read_csv(
                        mutated_data / filename,
                        dtype=str,
                        keep_default_na=False,
                    )
                    for filename in SNAPSHOT_FILENAMES
                }
                mutated_snapshot = create_snapshot_from_frames(
                    mutated_frames,
                    output_root=probe_root / "snapshots",
                    expected_columns={
                        filename: list(frame.columns)
                        for filename, frame in mutated_frames.items()
                    },
                    schema_version=state.snapshot.schema_version,
                )
                mutated_bundle = compose_blackbox_input_bundle(
                    mutated_snapshot,
                    platform_input_ids=bundle.platform_input_ids,
                    platform_input_artifacts=bundle.platform_input_artifacts,
                )
                mutated_state = replace(
                    state,
                    snapshot=mutated_snapshot,
                    bundle=mutated_bundle,
                )
                with _open_runtime_input(
                    ctx,
                    mutated_state,
                ) as mutated_view:
                    future_record = run_blackbox_predict(
                        metadata=metadata,
                        script_path=_script(cfg),
                        request=state.request,
                        data_dir=mutated_view.data_dir,
                        profile=profile,
                        **_runner_bundle_kwargs(mutated_view.bundle),
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
        platform_input_hashes_unchanged = (
            original_platform_hashes == mutated_platform_hashes
        )
        if not platform_input_hashes_unchanged:
            errors.append(
                "CompareGate changed a declared platform input artifact"
            )
        evidence = [
            *_bundle_evidence(bundle),
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
            Evidence(
                "platform_input_hashes_unchanged",
                platform_input_hashes_unchanged,
            ),
        ]
        return _finish(self.name, started_at, evidence, errors)


class BlackboxBacktestGate(_BlackboxGate):
    name = "backtest"

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        if ctx.persist_backtest:
            if ctx.backtest_sample_size is not None:
                return _blocked(
                    self.name,
                    started_at,
                    [
                        "Blackbox persisted backtest does not accept --sample-size; "
                        "use --backtest-start-date to define the complete interval"
                    ],
                )
            return self._run_persist(ctx, started_at)
        sample_size = 100 if ctx.backtest_sample_size is None else int(ctx.backtest_sample_size)
        if sample_size < 1 or sample_size > 1000:
            return _finish(
                self.name,
                started_at,
                [Evidence("sample_size", sample_size), Evidence("persist", False)],
                [f"Blackbox no-persist sample_size must be between 1 and 1000: got {sample_size}"],
            )
        cfg = _config(ctx)
        metadata = _metadata(cfg)
        state = _ensure_input_state(ctx)
        bundle = _input_bundle(state)
        profile = _profile(ctx)
        alternate_batch_size = _alternate_batch_size(profile.max_batch_requests)
        max_subprocesses = (
            math.ceil(sample_size / profile.max_batch_requests)
            + math.ceil(sample_size / alternate_batch_size)
        )
        budget = BacktestExecutionBudget(
            deadline_monotonic=time.monotonic() + ctx.timeout_sec,
            max_subprocesses=max_subprocesses,
        )
        with _open_runtime_input(ctx, state) as runtime_view:
            templates = _comparison_requests(
                state.request,
                runtime_view.data_dir,
            )
            requests = [
                replace(
                    templates[index % len(templates)],
                    request_id=(
                        f"{state.request.request_id}:batch:{index:04d}"
                    ),
                )
                for index in range(sample_size)
            ]
            records = run_blackbox_backtest(
                metadata=metadata,
                script_path=_script(cfg),
                requests=requests,
                data_dir=runtime_view.data_dir,
                profile=profile,
                budget=budget,
                **_runner_bundle_kwargs(runtime_view.bundle),
            )
            alternate_records = run_blackbox_backtest(
                metadata=metadata,
                script_path=_script(cfg),
                requests=requests,
                data_dir=runtime_view.data_dir,
                profile=replace(
                    profile,
                    max_batch_requests=alternate_batch_size,
                ),
                budget=budget,
                **_runner_bundle_kwargs(runtime_view.bundle),
            )
        errors: list[str] = []
        if len(records) != sample_size:
            errors.append(
                f"{sample_size}-row no-persist backtest returned {len(records)} records"
            )
        if [record.extra["request_id"] for record in records] != [item.request_id for item in requests]:
            errors.append("no-persist backtest did not preserve one-to-one Request order")
        batch_split_invariant = _direction_map(records) == _direction_map(alternate_records)
        if not batch_split_invariant:
            errors.append("no-persist backtest results changed with platform batch partitioning")
        if [record.extra["request_id"] for record in alternate_records] != [
            item.request_id for item in requests
        ]:
            errors.append("alternate batch partition did not preserve Request order")
        evidence = [
            *_bundle_evidence(bundle),
            Evidence("requests", len(requests)),
            Evidence("records", len(records)),
            Evidence("sample_size", sample_size),
            Evidence("distinct_cutoff_sets", len(templates)),
            Evidence("primary_batch_size", profile.max_batch_requests),
            Evidence("alternate_batch_size", alternate_batch_size),
            Evidence("subprocesses_started", budget.subprocesses_started),
            Evidence("max_subprocesses", budget.max_subprocesses),
            Evidence("total_deadline_sec", ctx.timeout_sec),
            Evidence("batch_split_invariant", batch_split_invariant),
            Evidence("persist", False),
            Evidence("business_tables_written", False),
        ]
        return _finish(self.name, started_at, evidence, errors)

    def _run_persist(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        if not authorization_signing_enabled():
            return _blocked(
                self.name,
                started_at,
                ["Blackbox backtest persistence requires HMAC signing via HARNESS_AUTH_SECRET"],
            )
        if not isinstance(ctx.authorization, str):
            return _blocked(
                self.name,
                started_at,
                ["Blackbox backtest persistence requires the original signed token string"],
            )
        auth, auth_errors = verify_authorization(
            ctx.authorization,
            scheme_id=ctx.scheme_id,
            action="backtest_persist",
            predict_date=ctx.predict_date,
            backtest_start_date=ctx.backtest_start_date,
            used_store_path=used_tokens_path(ctx.project_root),
        )
        if auth is not None:
            auth_errors.extend(required_future_expiry_errors(auth.issued_at, auth.expires_at))
            if not auth.issued_by.strip():
                auth_errors.append("Blackbox backtest authorization requires non-empty issued_by")
            if auth.scheme_version != cfg.scheme_version:
                auth_errors.append(
                    "authorization scheme_version must match current canonical version: "
                    f"token={auth.scheme_version}, current={cfg.scheme_version}"
                )
        if auth is None or auth_errors:
            return _blocked(self.name, started_at, auth_errors)

        engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
        audit_path: Path | None = None
        try:
            cfg = _reload_pinned_blackbox_config(cfg, phase="persisted backtest preflight")
            passed_run = _verify_passed_all(engine, cfg)
            if auth.harness_run_id != passed_run.harness_run_id:
                return _blocked(
                    self.name,
                    started_at,
                    [
                        "authorization harness_run_id must match latest passed all-stage run: "
                        f"token={auth.harness_run_id}, latest={passed_run.harness_run_id}"
                    ],
                )
            state = _ensure_input_state(ctx)
            provenance = _data_bridge_provenance(ctx, state.snapshot)
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
                limit=None,
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
            audit_path = write_authorization_audit(auth, ctx.report_dir / "backtest_authorization")
            mark_token_used(auth, used_tokens_path(ctx.project_root))
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
                _input_bundle(state).combined_snapshot_id,
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
            Evidence("protected_table_counts_before", before),
            Evidence("protected_table_counts_after", after),
            Evidence("protected_table_deltas", deltas),
            Evidence("authorization_audit_path", str(audit_path)),
            Evidence("replay_semantics", CURRENT_SNAPSHOT_REPLAY),
        ]
        result = _finish(self.name, started_at, evidence, errors)
        return replace(result, report_path=audit_path)


class BlackboxApiReadinessGate(_BlackboxGate):
    name = "api-readiness"

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        metadata = _metadata(cfg)
        state = _ensure_input_state(ctx)
        bundle = _input_bundle(state)
        with _open_runtime_input(ctx, state) as runtime_view:
            record = run_blackbox_predict(
                metadata=metadata,
                script_path=_script(cfg),
                request=state.request,
                data_dir=runtime_view.data_dir,
                profile=_profile(ctx),
                **_runner_bundle_kwargs(runtime_view.bundle),
            )
        registry_id = f"{cfg.scheme_id}__h{cfg.horizon}__{metadata.target_tenor}"
        errors: list[str] = []
        if record.scheme_id != cfg.scheme_id:
            errors.append("PredictionRecord scheme_id does not match base scheme identity")
        if record.target_tenor != metadata.target_tenor or record.horizon != metadata.horizon:
            errors.append("PredictionRecord target identity does not match Metadata")
        if cfg.status == "paused":
            lifecycle_mode = "pre_shadow"
            scheduler_eligible = False
            api_visible = False
        elif cfg.status == "active":
            lifecycle_mode = "active_recertification"
            scheduler_eligible = True
            api_visible = True
            if cfg.version_status != "active":
                errors.append(
                    "active Blackbox V2 recertification requires version_status=active"
                )
        else:
            lifecycle_mode = "invalid"
            scheduler_eligible = False
            api_visible = False
            errors.append(
                "Blackbox V2 api-readiness requires paused onboarding or active recertification"
            )
        evidence = [
            *_bundle_evidence(bundle),
            Evidence("prediction_record", asdict(record)),
            Evidence("registry_id", registry_id),
            Evidence("registry_status", cfg.status),
            Evidence("lifecycle_mode", lifecycle_mode),
            Evidence("scheduler_eligible", scheduler_eligible),
            Evidence("api_visible", api_visible),
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
            original_identity = replace(
                cfg,
                environment_fingerprint=environment_fingerprint,
                data_snapshot_id=passed_run.data_snapshot_id,
            )
            compensating = False

            def consume() -> None:
                nonlocal audit_path
                mark_token_used(auth, used_tokens_path(ctx.project_root))
                audit_path = write_authorization_audit(auth, ctx.report_dir / "shadow_authorization")

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


def _input_bundle(state: InputState) -> BlackboxInputBundle:
    if state.bundle is not None:
        return state.bundle
    return compose_blackbox_input_bundle(state.snapshot)


@contextmanager
def _open_runtime_input(ctx: GateContext, state: InputState):
    gate_root = _gate_root(ctx)
    runtime_root = gate_root / "runtime_views"
    runtime_root.mkdir(exist_ok=True)
    _require_controlled_directory(
        runtime_root,
        controlled_parent=gate_root,
        label="Blackbox runtime views",
    )
    try:
        with open_blackbox_runtime_view(
            _input_bundle(state),
            runtime_root=runtime_root,
        ) as runtime_view:
            try:
                yield runtime_view
            except ProcessGroupTerminationError:
                runtime_view.mark_termination_uncertain()
                raise
    finally:
        _cleanup_empty_runtime_view_root(runtime_root)


def _cleanup_empty_runtime_view_root(runtime_root: Path) -> None:
    if not os.path.lexists(runtime_root):
        return
    runtime_info = runtime_root.lstat()
    if (
        stat.S_ISLNK(runtime_info.st_mode)
        or not stat.S_ISDIR(runtime_info.st_mode)
    ):
        raise ValueError(
            "Blackbox runtime views must be a real directory"
        )
    active = runtime_root / "active"
    debris = runtime_root / "debris"
    for path, label in (
        (active, "Blackbox runtime active root"),
        (debris, "Blackbox runtime debris root"),
    ):
        if os.path.lexists(path) and not _is_real_directory(path):
            raise ValueError(f"{label} must not be a symlink")
    if (
        _is_real_directory(active)
        and _is_real_directory(debris)
        and not any(active.iterdir())
        and not any(debris.iterdir())
    ):
        active.rmdir()
        debris.rmdir()
        runtime_root.rmdir()


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


def _verify_runtime_platform_files(
    bundle: BlackboxInputBundle,
    data_dir: Path,
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for artifact in bundle.platform_input_artifacts:
        path = data_dir / artifact.filename
        stable = _read_stable_private_file(
            path,
            label=f"platform input {artifact.artifact_id}",
            require_read_only=True,
        )
        if (
            stable.sha256 != artifact.sha256
            or stable.size_bytes != artifact.size_bytes
        ):
            raise ValueError(
                f"platform input {artifact.artifact_id} content mismatch"
            )
        hashes[artifact.artifact_id] = stable.sha256
    return hashes


def _ensure_input_state(ctx: GateContext, *, force: bool = False) -> InputState:
    root = _gate_root(ctx)
    state_path = root / "input_state.json"
    seal_path = root / "input_state.initialized"
    state_exists = os.path.lexists(state_path)
    seal_exists = os.path.lexists(seal_path)
    if not force and (state_exists or seal_exists):
        if not state_exists or not seal_exists:
            raise ValueError(
                "Blackbox V2 input state was initialized but its canonical "
                "state or initialization seal is missing"
            )
        _validate_input_state_seal(seal_path)
        return _read_input_state(state_path)
    if not force:
        residual_entries = sorted(path.name for path in root.iterdir())
        if residual_entries:
            raise ValueError(
                "Blackbox V2 fresh input state root contains residual "
                f"artifacts: {residual_entries}"
            )
    if state_exists:
        _require_regular_file(
            state_path,
            "Blackbox V2 input state",
        )
    if seal_exists:
        _require_regular_file(
            seal_path,
            "Blackbox V2 input state initialization seal",
        )
    runtime_snapshot_root = root / "runtime_snapshot"
    if os.path.lexists(runtime_snapshot_root):
        _remove_harness_runtime_tree(
            runtime_snapshot_root,
            controlled_parent=root,
        )
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
    runtime_platform_paths = _write_runtime_platform_inputs(
        root,
        bundle.platform_input_artifacts,
    )
    request_path = root / "request.json"
    if os.path.lexists(request_path):
        _require_regular_file(request_path, "Blackbox Request")
        if request_path.lstat().st_nlink != 1:
            raise ValueError(
                "Blackbox Request must not be a hardlink"
            )
    request_path = write_request(request, request_path)
    request_sha256 = _regular_file_sha256(
        request_path,
        label="Blackbox Request",
    )
    provenance = _data_bridge_provenance(ctx, snapshot)
    payload = {
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_root": str(snapshot.root_dir),
        "data_dir": str(snapshot.data_dir),
        "manifest_path": str(snapshot.manifest_path),
        "schema_version": snapshot.schema_version,
        "request_path": str(request_path),
        "combined_snapshot_id": bundle.combined_snapshot_id,
        "parent_snapshot_id": bundle.parent_snapshot_id,
        "platform_input_ids": list(bundle.platform_input_ids),
        "platform_input_artifacts": [
            {
                **artifact.identity_manifest,
                "runtime_content_path": str(
                    runtime_platform_paths[artifact.artifact_id]
                ),
                "audit_provenance": dict(artifact.audit_provenance),
            }
            for artifact in bundle.platform_input_artifacts
        ],
        "identity_manifest": bundle.identity_manifest,
        "audit_manifest": bundle.audit_manifest,
        "request_sha256": request_sha256,
        **provenance,
    }
    _atomic_write(state_path, json.dumps(payload, ensure_ascii=True, indent=2) + "\n")
    _atomic_write(
        seal_path,
        INPUT_STATE_INITIALIZED_SEAL.decode("ascii"),
    )
    return InputState(
        snapshot=snapshot,
        request_path=request_path,
        request=request,
        bundle=bundle,
    )


def _validate_input_state_seal(seal_path: Path) -> None:
    stable = _read_stable_private_file(
        seal_path,
        label="Blackbox V2 input state initialization seal",
    )
    if stable.content_bytes != INPUT_STATE_INITIALIZED_SEAL:
        raise ValueError(
            "Blackbox V2 initialized state seal is not canonical"
        )


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
    """删除 Harness 临时输入副本，保留 input_state.json 审计记录。"""
    root = _existing_gate_root(ctx)
    if root is None:
        return
    runtime_snapshot = root / "runtime_snapshot"
    if os.path.lexists(runtime_snapshot):
        _remove_harness_runtime_tree(
            runtime_snapshot,
            controlled_parent=root,
        )
    runtime_views = root / "runtime_views"
    if os.path.lexists(runtime_views):
        _require_controlled_directory(
            runtime_views,
            controlled_parent=root,
            label="Blackbox runtime views",
        )
    _cleanup_empty_runtime_view_root(runtime_views)
    _sanitize_final_input_state(root / "input_state.json")


def _remove_harness_runtime_tree(
    runtime_root: Path,
    *,
    controlled_parent: Path,
) -> None:
    _require_controlled_directory(
        runtime_root,
        controlled_parent=controlled_parent,
        label="Blackbox Harness runtime tree",
    )
    entries: list[Path] = []
    for current_root, directories, files in os.walk(
        runtime_root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_root)
        for name in (*directories, *files):
            entry = current / name
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(
                    "Blackbox Harness runtime tree contains a symlink"
                )
            if not (
                stat.S_ISDIR(info.st_mode)
                or stat.S_ISREG(info.st_mode)
            ):
                raise ValueError(
                    "Blackbox Harness runtime tree contains a special file"
                )
            entries.append(entry)
    os.chmod(runtime_root, 0o755, follow_symlinks=False)
    for entry in entries:
        mode = entry.lstat().st_mode
        os.chmod(
            entry,
            0o755 if stat.S_ISDIR(mode) else 0o644,
            follow_symlinks=False,
        )
    shutil.rmtree(runtime_root)


def _write_runtime_platform_inputs(
    gate_root: Path,
    artifacts: tuple[FrozenPlatformInput, ...],
) -> dict[str, Path]:
    if not artifacts:
        return {}
    runtime_snapshot = gate_root / "runtime_snapshot"
    runtime_snapshot.mkdir(exist_ok=True)
    _require_controlled_directory(
        runtime_snapshot,
        controlled_parent=gate_root,
        label="Blackbox runtime snapshot",
    )
    platform_root = runtime_snapshot / "platform_inputs"
    platform_root.mkdir()
    _require_controlled_directory(
        platform_root,
        controlled_parent=runtime_snapshot,
        label="Blackbox runtime platform inputs",
    )
    paths: dict[str, Path] = {}
    for artifact in artifacts:
        destination = platform_root / artifact.filename
        with destination.open("xb") as stream:
            stream.write(artifact.content_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        destination.chmod(0o444)
        if (
            _regular_file_sha256(
                destination,
                label=f"platform input {artifact.artifact_id}",
            )
            != artifact.sha256
        ):
            raise ValueError(
                f"platform input {artifact.artifact_id} write mismatch"
            )
        paths[artifact.artifact_id] = destination
    platform_root.chmod(0o555)
    return paths


def _read_runtime_platform_content(
    state_path: Path,
    raw_artifact: dict[str, Any],
) -> bytes:
    runtime_snapshot = state_path.parent / "runtime_snapshot"
    _require_controlled_directory(
        runtime_snapshot,
        controlled_parent=state_path.parent,
        label="Blackbox runtime snapshot",
    )
    platform_root = runtime_snapshot / "platform_inputs"
    _require_controlled_directory(
        platform_root,
        controlled_parent=runtime_snapshot,
        label="Blackbox runtime platform inputs",
    )
    runtime_path = Path(raw_artifact["runtime_content_path"])
    filename = str(raw_artifact["filename"])
    expected = (
        platform_root / filename
    )
    if runtime_path != expected:
        raise ValueError(
            "platform input runtime path is outside controlled snapshot"
        )
    stable = _read_stable_private_file(
        runtime_path,
        label=f"platform input {raw_artifact.get('artifact_id')}",
        require_read_only=True,
    )
    expected_size = raw_artifact.get("size_bytes")
    expected_sha256 = raw_artifact.get("sha256")
    if (
        stable.size_bytes != expected_size
        or stable.sha256 != expected_sha256
    ):
        raise ValueError("platform input runtime content mismatch")
    return stable.content_bytes


def _sanitize_final_input_state(state_path: Path) -> None:
    if not os.path.lexists(state_path):
        return
    stable = _read_stable_private_file(
        state_path,
        label="Blackbox V2 input state",
    )
    raw = json.loads(stable.content_bytes.decode("utf-8"))
    artifacts = raw.get("platform_input_artifacts", [])
    if not isinstance(artifacts, list):
        raise ValueError(
            "Blackbox V2 input state platform artifacts are invalid"
        )
    sanitized: list[dict[str, Any]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            raise ValueError(
                "Blackbox V2 input state platform artifact is invalid"
            )
        clean = dict(item)
        clean.pop("content_base64", None)
        clean.pop("runtime_content_path", None)
        clean["runtime_content_removed"] = True
        sanitized.append(clean)
    raw["platform_input_artifacts"] = sanitized
    raw["runtime_content_removed"] = True
    _atomic_write(
        state_path,
        json.dumps(raw, ensure_ascii=True, indent=2) + "\n",
    )


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
    stable_state = _read_stable_private_file(
        path,
        label="Blackbox V2 input state",
    )
    raw = json.loads(stable_state.content_bytes.decode("utf-8"))
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
    expected_request_path = path.parent / "request.json"
    if request_path != expected_request_path:
        raise ValueError("Blackbox V2 Request path is outside the gate root")
    request_sha256 = raw.get("request_sha256")
    stable_request = _read_stable_private_file(
        request_path,
        label="Blackbox Request",
    )
    if (
        not isinstance(request_sha256, str)
        or stable_request.sha256 != request_sha256
    ):
        raise ValueError("Blackbox V2 Request SHA-256 mismatch")
    request = load_request_bytes(
        stable_request.content_bytes,
        source="verified Blackbox Request",
    )
    raw_artifacts = raw.get("platform_input_artifacts", [])
    if not isinstance(raw_artifacts, list):
        raise ValueError("Blackbox V2 input state platform artifacts are invalid")
    artifacts: list[FrozenPlatformInput] = []
    for item in raw_artifacts:
        if not isinstance(item, dict):
            raise ValueError("Blackbox V2 input state platform artifact is invalid")
        try:
            artifacts.append(
                FrozenPlatformInput(
                    artifact_id=item["artifact_id"],
                    provider_version=item["provider_version"],
                    filename=item["filename"],
                    columns=tuple(item["columns"]),
                    content_bytes=_read_runtime_platform_content(
                        path,
                        item,
                    ),
                    sha256=item["sha256"],
                    size_bytes=item["size_bytes"],
                    row_count=item["row_count"],
                    audit_provenance=item["audit_provenance"],
                )
            )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise ValueError(
                "Blackbox V2 input state platform artifact is invalid"
            ) from exc
    platform_input_ids = tuple(raw.get("platform_input_ids", ()))
    bundle = compose_blackbox_input_bundle(
        snapshot,
        platform_input_ids=platform_input_ids,
        platform_input_artifacts=artifacts,
    )
    if (
        raw.get("combined_snapshot_id") != bundle.combined_snapshot_id
        or raw.get("parent_snapshot_id") != bundle.parent_snapshot_id
        or raw.get("identity_manifest") != bundle.identity_manifest
        or raw.get("audit_manifest") != bundle.audit_manifest
    ):
        raise ValueError("Blackbox V2 input state bundle evidence mismatch")
    return InputState(
        snapshot=snapshot,
        request_path=request_path,
        request=request,
        bundle=bundle,
    )


def _create_engine():
    from scheduler.repository import create_engine_from_env

    return create_engine_from_env()


def _existing_gate_root(ctx: GateContext) -> Path | None:
    report_dir = ctx.report_dir
    if not os.path.lexists(report_dir):
        return None
    _require_real_directory(report_dir, "Harness report_dir")
    root = report_dir / "blackbox_v2"
    if not os.path.lexists(root):
        return None
    _require_controlled_directory(
        root,
        controlled_parent=report_dir,
        label="Blackbox gate root",
    )
    return root


def _gate_root(ctx: GateContext) -> Path:
    report_dir = ctx.report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    _require_real_directory(report_dir, "Harness report_dir")
    root = report_dir / "blackbox_v2"
    root.mkdir(exist_ok=True)
    _require_controlled_directory(
        root,
        controlled_parent=report_dir,
        label="Blackbox gate root",
    )
    return root


def _require_real_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"{label} must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{label} must be a directory")


def _require_controlled_directory(
    path: Path,
    *,
    controlled_parent: Path,
    label: str,
) -> None:
    _require_real_directory(controlled_parent, f"{label} parent")
    _require_real_directory(path, label)
    try:
        actual_parent = path.parent.resolve(strict=True)
        expected_parent = controlled_parent.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} boundary is unavailable") from exc
    if actual_parent != expected_parent:
        raise ValueError(f"{label} is outside the controlled boundary")


def _is_real_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(
        info.st_mode
    )


def _require_regular_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"{label} must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be a regular file")


def _regular_file_sha256(path: Path, *, label: str) -> str:
    return _read_stable_private_file(path, label=label).sha256


def _read_stable_private_file(
    path: Path,
    *,
    label: str,
    require_read_only: bool = False,
) -> _StablePrivateFile:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError(
                f"{label} must be a private regular file"
            )
        if require_read_only and before.st_mode & 0o222:
            raise ValueError(f"{label} must be read-only")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after_fd = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    before_fingerprint = _stable_file_fingerprint(before)
    after_fd_fingerprint = _stable_file_fingerprint(after_fd)
    if before_fingerprint != after_fd_fingerprint:
        raise ValueError(f"{label} changed while reading")
    content = b"".join(chunks)
    if len(content) != before.st_size:
        raise ValueError(f"{label} size changed while reading")
    try:
        after_path = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} changed while reading") from exc
    if (
        stat.S_ISLNK(after_path.st_mode)
        or _stable_file_fingerprint(after_path)
        != after_fd_fingerprint
    ):
        raise ValueError(f"{label} changed while reading")
    return _StablePrivateFile(
        content_bytes=content,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        mode=stat.S_IMODE(before.st_mode),
    )


def _stable_file_fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


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
        "daily_output.csv": ("date", None),
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
        if filename == "daily_output.csv":
            parsed_dates = pd.to_datetime(frame[key], errors="raise")
            last_raw_date = str(frame[key].iloc[-1]).strip()
            date_format = "%Y/%m/%d" if "/" in last_raw_date else "%Y-%m-%d"
            if " " in last_raw_date:
                time_text = last_raw_date.rsplit(" ", 1)[1]
                date_format += " %H:%M:%S" if time_text.count(":") == 2 else " %H:%M"
            future_value = (parsed_dates.iloc[-1] + pd.Timedelta(days=1)).strftime(date_format)
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


def _alternate_batch_size(primary_batch_size: int) -> int:
    if primary_batch_size <= 1:
        raise ValueError("Blackbox batch invariance requires max_batch_requests greater than 1")
    return min(primary_batch_size - 1, max(1, primary_batch_size * 4 // 5))


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
    stable_state = _read_stable_private_file(
        state_path,
        label="passed all-stage Blackbox V2 input state",
    )
    state = json.loads(stable_state.content_bytes.decode("utf-8"))
    return PassedAllRun(
        harness_run_id=str(row["harness_run_id"]),
        report_uri=report_dir,
        data_snapshot_id=str(
            state.get("combined_snapshot_id", state["snapshot_id"])
        ),
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


class _CanonicalSchemeVersionChanged(RuntimeError):
    pass


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
