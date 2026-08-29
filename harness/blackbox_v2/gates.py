from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from harness.context import GateContext
from harness.operation import (
    DEFAULT_BACKTEST_START_DATE,
    operation_scope_sha256,
    verify_direct_operation,
)
from backtests.blackbox_v2 import run_blackbox_historical_backtest
from backtests.repository import persist_backtest_output_atomic
from harness.gates.base import Gate, create_default_engine, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus
from scheduler.blackbox_v2_runner import (
    DEFAULT_RUNTIME_PROFILE,
    RuntimeProfile,
    run_blackbox_backtest,
)
from scheduler.process_control import ProcessGroupTerminationError
from scheduler.discovery import SchemeConfig, load_scheme_config
from shared.blackbox_v2.contracts import BlackboxMetadata, load_metadata
from shared.blackbox_v2.intake import (
    DATA_SCHEMA_VERSION,
    RUNTIME_PROFILE,
    SCRIPT_VALIDATOR_POLICY_DIGEST,
    validate_delivery_script,
)
from shared.blackbox_v2.history import CURRENT_SNAPSHOT_REPLAY, build_historical_cases
from shared.blackbox_v2.snapshot import (
    BlackboxInputBundle,
    compose_blackbox_input_bundle,
)
from shared.input_artifacts import (
    get_ready_blackbox_snapshot,
    open_blackbox_runtime_view,
)


@dataclass(frozen=True)
class PassedBacktestRun:
    backtest_run_id: int
    benchmark_id: str
    data_snapshot_id: str
    generation_id: str
    runtime_profile: str
    environment_fingerprint: str


class _BlackboxGate(Gate):
    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        raise NotImplementedError


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
            metadata = validate_canonical_blackbox_delivery(cfg)
            operation, operation_errors = verify_direct_operation(
                ctx.operation,
                scheme_id=ctx.scheme_id,
                action="backtest_persist",
                predict_date=ctx.predict_date,
                scheme_version=cfg.scheme_version,
                backtest_start_date=ctx.backtest_start_date,
            )
            if operation is None or operation_errors:
                return _blocked(self.name, started_at, operation_errors)
            snapshot = get_ready_blackbox_snapshot(
                snapshot_date=ctx.predict_date,
                require_fresh=False,
                factor_input_mode=(
                    getattr(cfg, "factor_input_mode", None) or "legacy_v1"
                ),
            )
            generation_id = str(snapshot.generation_id or "").strip()
            if not generation_id:
                return _blocked(
                    self.name,
                    started_at,
                    ["Blackbox persisted backtest requires DataBridge generation_id evidence"],
                )
            if cfg.runtime_profile != DEFAULT_RUNTIME_PROFILE.name:
                return _blocked(
                    self.name,
                    started_at,
                    [
                        "persisted backtest runtime_profile must match frozen profile: "
                        f"current={cfg.runtime_profile}, frozen={DEFAULT_RUNTIME_PROFILE.name}"
                    ],
                )
            cases = build_historical_cases(
                metadata,
                snapshot,
                engine,
                target_date_before=ctx.predict_date,
                predict_date_from=ctx.backtest_start_date,
            )
            profile = _profile(ctx)
            execution_profile = replace(
                profile,
                backtest_timeout_sec=min(
                    profile.backtest_timeout_sec,
                    ctx.timeout_sec,
                ),
            )
            benchmark_id = f"bbv2-{cfg.scheme_id}-{uuid.uuid4().hex}"
            bundle = compose_blackbox_input_bundle(
                snapshot,
                factor_input_mode=(
                    getattr(cfg, "factor_input_mode", None) or "legacy_v1"
                ),
            )
            environment_fingerprint = _environment_fingerprint(ctx.project_root)
            with _open_runtime_input(bundle) as runtime_view:
                output = run_blackbox_historical_backtest(
                    metadata=metadata,
                    script_path=_script(cfg),
                    cases=cases,
                    snapshot=snapshot,
                    input_bundle=runtime_view.bundle,
                    runtime_data_dir=runtime_view.data_dir,
                    scheme_version=cfg.scheme_version,
                    generation_id=generation_id,
                    benchmark_id=benchmark_id,
                    run_delivery=run_blackbox_backtest,
                    profile=execution_profile,
                    backtest_start_date=ctx.backtest_start_date,
                    target_date_before=ctx.predict_date,
                    total_deadline_sec=ctx.timeout_sec,
                )
            output.summary.update(
                {
                    "code_hash": cfg.code_hash,
                    "config_hash": cfg.config_hash,
                    "manifest_hash": cfg.manifest_hash,
                    "script_validator_policy_digest": (
                        SCRIPT_VALIDATOR_POLICY_DIGEST
                    ),
                    "input_artifact_hash": bundle.combined_snapshot_id,
                    "runtime_profile": cfg.runtime_profile,
                    "environment_fingerprint": environment_fingerprint,
                    "operator": operation.issued_by,
                    "operation_scope_sha256": operation_scope_sha256(operation),
                }
            )
            if not output.monthly_metrics:
                raise ValueError(
                    "Blackbox persisted backtest requires non-empty monthly metrics: "
                    f"monthly_metrics={len(output.monthly_metrics)}"
                )
            cfg = _reload_pinned_blackbox_config(cfg, phase="persisted backtest commit")
            run_id = persist_backtest_output_atomic(engine, output, benchmark_id=benchmark_id)
        finally:
            if hasattr(engine, "dispose"):
                engine.dispose()

        evidence = [
            Evidence("persist", True),
            Evidence("run_id", run_id),
            Evidence("benchmark_id", benchmark_id),
            Evidence("scheme_version", cfg.scheme_version),
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
            Evidence("subprocesses_started", 1),
            Evidence("total_deadline_sec", ctx.timeout_sec),
            Evidence("operator", operation.issued_by),
            Evidence("environment_fingerprint", environment_fingerprint),
            Evidence("operation_scope_sha256", operation_scope_sha256(operation)),
            Evidence("replay_semantics", CURRENT_SNAPSHOT_REPLAY),
        ]
        return _finish(self.name, started_at, evidence, [])


BLACKBOX_GATES: dict[str, type[Gate]] = {
    "backtest": BlackboxBacktestGate,
}


def _config(ctx: GateContext) -> SchemeConfig:
    cfg = ctx.config or load_scheme_config(ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml")
    if cfg.runtime_type != "blackbox_v2":
        raise ValueError(f"Blackbox V2 gate requires runtime_type=blackbox_v2, got {cfg.runtime_type}")
    return cfg


def validate_canonical_blackbox_delivery(cfg: SchemeConfig) -> BlackboxMetadata:
    """复用已加载 canonical 身份，只补充 Intake 脚本安全校验。"""
    validate_delivery_script(_script(cfg))
    metadata = getattr(cfg, "blackbox_metadata", None)
    if not isinstance(metadata, BlackboxMetadata):
        if cfg.delivery_metadata is None:
            raise ValueError("Blackbox V2 metadata path missing")
        metadata = load_metadata(cfg.delivery_metadata)
    if cfg.runtime_profile != RUNTIME_PROFILE:
        raise ValueError(
            "Blackbox runtime_profile must match the platform contract: "
            f"expected={RUNTIME_PROFILE}, actual={cfg.runtime_profile}"
        )
    if cfg.data_schema_version != DATA_SCHEMA_VERSION:
        raise ValueError(
            "Blackbox data_schema_version must match the platform contract: "
            f"expected={DATA_SCHEMA_VERSION}, actual={cfg.data_schema_version}"
        )
    if cfg.input_source != "data_bridge_current":
        raise ValueError("Blackbox input_source must be data_bridge_current")
    if metadata.description is None:
        raise ValueError("description is required for a Blackbox V2 delivery")
    return metadata


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


@contextmanager
def _open_runtime_input(bundle: BlackboxInputBundle):
    with open_blackbox_runtime_view(bundle) as runtime_view:
        try:
            yield runtime_view
        except ProcessGroupTerminationError:
            runtime_view.mark_termination_uncertain()
            raise


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


def verify_passed_blackbox_backtest(engine, cfg: SchemeConfig) -> PassedBacktestRun:
    """读取与 canonical exact version 精确匹配的最近一次成功回测。"""
    from sqlalchemy import text

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, benchmark_id, summary, input_artifact_hash
                FROM t_backtest_runs
                WHERE scheme_id = :scheme_id
                  AND data_source = :data_source
                  AND status = 'success'
                  AND run_mode = 'persist'
                  AND code_hash = :code_hash
                  AND config_hash = :config_hash
                ORDER BY updated_at DESC, id DESC
                """
            ),
            {
                "scheme_id": cfg.scheme_id,
                "data_source": "blackbox_v2_current_snapshot_as_of",
                "code_hash": cfg.code_hash,
                "config_hash": cfg.config_hash,
            },
        ).mappings().all()

    for row in rows:
        summary = _decode_json_object(row["summary"], "backtest summary")
        if (
            summary.get("scheme_version") != cfg.scheme_version
            or summary.get("manifest_hash") != cfg.manifest_hash
            or summary.get("script_validator_policy_digest")
            != SCRIPT_VALIDATOR_POLICY_DIGEST
        ):
            continue
        required = {
            "data_snapshot_id": summary.get("data_snapshot_id"),
            "generation_id": summary.get("generation_id"),
            "runtime_profile": summary.get("runtime_profile"),
            "environment_fingerprint": summary.get("environment_fingerprint"),
        }
        missing = [
            key
            for key, value in required.items()
            if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            raise ValueError(
                "successful Blackbox backtest evidence is incomplete: "
                f"missing={missing}"
            )
        if row["input_artifact_hash"] != required["data_snapshot_id"]:
            raise ValueError(
                "successful Blackbox backtest input identity is inconsistent"
            )
        return PassedBacktestRun(
            backtest_run_id=int(row["id"]),
            benchmark_id=str(row["benchmark_id"]),
            data_snapshot_id=str(required["data_snapshot_id"]),
            generation_id=str(required["generation_id"]),
            runtime_profile=str(required["runtime_profile"]),
            environment_fingerprint=str(required["environment_fingerprint"]),
        )
    raise ValueError(
        f"no successful persisted Blackbox backtest for {cfg.scheme_id} "
        f"version {cfg.scheme_version}"
    )


def _decode_json_object(value: object, label: str) -> dict[str, object]:
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{label} is malformed") from exc
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} is malformed") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is malformed")
    return value


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
