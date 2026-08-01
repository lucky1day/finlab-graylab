from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from harness.authorization import (
    mark_token_used,
    used_tokens_path,
    verify_authorization,
    write_authorization_audit,
)
from harness.config_loader import load_config_raw
from harness.context import GateContext
from harness.contracts.config_schema import validate_config
from harness.contracts.onboarding_policy import validate_onboarding_policy
from harness.gates.base import Gate, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus

if TYPE_CHECKING:
    from scheduler.discovery import SchemeConfig


_ROOT_STATUS_KEY = re.compile(r"^status[ \t]*:")
_ROOT_STATUS_VALUE = re.compile(
    r"^(?P<prefix>status[ \t]*:[ \t]*)(?P<value>paused|active)"
    r"(?P<suffix>[ \t]*(?:#.*)?)(?P<newline>\r?\n)?$"
)
_NATIVE_ACTIVATION_DERIVED_FIELDS = frozenset(
    {
        "status",
        "version_status",
        "code_hash",
        "config_hash",
        "manifest_hash",
        "scheme_version",
    }
)


@dataclass(frozen=True)
class NativeActivationPreflight:
    """Native 激活前冻结的文件、版本与业务身份。"""

    validation_config: "SchemeConfig"
    config_bytes: bytes
    config_text: str
    expected_active_config_bytes: bytes
    expected_active_config_text: str
    expected_active_config_hash: str
    expected_active_scheme_version: str
    code_hash: str
    manifest_hash: str | None
    business_identity: tuple[tuple[str, object], ...]

    def business_identity_for(
        self,
        cfg: "SchemeConfig",
    ) -> tuple[tuple[str, object], ...]:
        return _native_activation_business_identity(cfg)


class ActivationGate(Gate):
    """Native 激活 gate：授权后执行 paused→active 或 active 精确版本重批准。"""

    name = "activate"
    requires_authorization = True

    def run(self, ctx: GateContext) -> GateResult:
        config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
        cfg = ctx.config
        raw_runtime_type = None
        if config_path.is_file():
            try:
                raw = load_config_raw(config_path)
                raw_runtime_type = raw.get("runtime_type") if isinstance(raw, dict) else None
            except (OSError, UnicodeError, ValueError):
                raw_runtime_type = getattr(cfg, "runtime_type", None)
        if raw_runtime_type == "blackbox_v2" or getattr(cfg, "runtime_type", None) == "blackbox_v2":
            try:
                from scheduler.discovery import load_scheme_config

                cfg = load_scheme_config(config_path)
            except Exception as exc:  # noqa: BLE001
                return guarded_result(
                    self.name,
                    lambda started_at: _blackbox_config_failure(started_at, config_path, exc),
                )
            from harness.blackbox_v2.activation import activate_blackbox

            return activate_blackbox(replace(ctx, config=cfg))
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"

        auth, auth_errors = verify_authorization(
            ctx.authorization,
            scheme_id=ctx.scheme_id,
            action="activate",
            predict_date=None,
            used_store_path=used_tokens_path(ctx.project_root),
        )
        if auth_errors:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                passed=False,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("authorization_required", True),
                ],
                errors=auth_errors,
                started_at=started_at,
                finished_at=finished_at,
            )

        errors: list[str] = []
        if not config_path.exists():
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.FAILED,
                passed=False,
                evidence=[Evidence("config_path", str(config_path))],
                errors=[f"config.yaml not found: {config_path}"],
                started_at=started_at,
                finished_at=finished_at,
            )

        raw = load_config_raw(config_path)
        config_errors = validate_config(raw, ctx.scheme_id)
        if config_errors:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.FAILED,
                passed=False,
                evidence=[Evidence("config_errors", config_errors)],
                errors=[f"config validation failed: {len(config_errors)} error(s)"],
                started_at=started_at,
                finished_at=finished_at,
            )

        policy_errors = validate_onboarding_policy(
            ctx.project_root,
            ctx.scheme_id,
            str(raw.get("runtime_type", "native_adapter")),
        )
        if policy_errors:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.FAILED,
                passed=False,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("onboarding_policy_allowed", False),
                    Evidence("onboarding_policy_errors", policy_errors),
                ],
                errors=policy_errors,
                started_at=started_at,
                finished_at=finished_at,
            )

        # 校验 gate 历史：当前精确版本必须有一次 stage=all 全通过的 harness 运行。
        validation_scheme_version = _compute_scheme_version(ctx)
        binding_errors = _native_activation_authorization_errors(
            auth,
            validation_scheme_version,
        )
        if binding_errors:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                passed=False,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("validation_scheme_version", validation_scheme_version),
                    Evidence("authorization_required", True),
                ],
                errors=binding_errors,
                started_at=started_at,
                finished_at=finished_at,
            )
        try:
            preflight = _strict_native_activation_preflight(
                ctx,
                validation_scheme_version,
            )
        except Exception as exc:  # noqa: BLE001
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.FAILED,
                passed=False,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("validation_scheme_version", validation_scheme_version),
                    Evidence("strict_discovery", False),
                ],
                errors=[f"Native validation config failed strict discovery: {exc}"],
                started_at=started_at,
                finished_at=finished_at,
            )
        gate_history_errors = _verify_gate_history(ctx, validation_scheme_version)
        if gate_history_errors:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                passed=False,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("validation_scheme_version", validation_scheme_version),
                    Evidence("gate_history_errors", gate_history_errors),
                ],
                errors=gate_history_errors,
                started_at=started_at,
                finished_at=finished_at,
            )

        # 消费是首次授权副作用；锁内重检保证并发输家不会修改配置或 Registry。
        mark_token_used(auth, used_tokens_path(ctx.project_root))
        audit_dir = ctx.report_dir / "activation_authorization"
        audit_path = write_authorization_audit(auth, audit_dir)

        previous_status = preflight.validation_config.status
        new_status = "active"
        flipped = False
        try:
            if previous_status != "active":
                flipped = _write_expected_active_config(
                    config_path,
                    preflight,
                )
            activated_scheme_version = _sync_registry_after_activation(
                ctx,
                preflight=preflight,
                approved_by=auth.issued_by.strip(),
                approved_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            rollback_error: str | None = None
            status_rolled_back = previous_status == "active"
            if previous_status != "active":
                try:
                    _set_status(config_path, previous_status)
                    status_rolled_back = True
                except Exception as rollback_exc:
                    rollback_error = str(rollback_exc)
            finished_at = utc_now()
            errors = [f"registry sync after activation failed: {exc}"]
            if rollback_error:
                errors.append(f"failed to roll back config status after sync failure: {rollback_error}")
            return GateResult(
                gate_name=self.name,
                status=GateStatus.FAILED,
                passed=False,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("validation_scheme_version", validation_scheme_version),
                    Evidence("config_path", str(config_path)),
                    Evidence("previous_status", previous_status),
                    Evidence("new_status", new_status),
                    Evidence("status_flipped", flipped),
                    Evidence("status_rolled_back", status_rolled_back),
                    Evidence("registry_synced", False),
                ],
                errors=errors,
                started_at=started_at,
                finished_at=finished_at,
            )

        finished_at = utc_now()
        return GateResult(
            gate_name=self.name,
            status=GateStatus.PASSED,
            passed=True,
            evidence=[
                Evidence("scheme_id", ctx.scheme_id),
                Evidence("validation_scheme_version", validation_scheme_version),
                Evidence("activated_scheme_version", activated_scheme_version),
                Evidence("config_path", str(config_path)),
                Evidence("previous_status", previous_status),
                Evidence("new_status", new_status),
                Evidence("status_flipped", flipped),
                Evidence("cron", _cron_of(raw)),
                Evidence("gate_history_verified", True),
                Evidence("registry_synced", True),
                Evidence("authorization_audit_path", str(audit_path)),
            ],
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
            report_path=audit_path,
        )


def _blackbox_config_failure(started_at: str, config_path: Path, exc: Exception) -> GateResult:
    return GateResult(
        gate_name="activate",
        status=GateStatus.FAILED,
        passed=False,
        evidence=[
            Evidence("config_path", str(config_path)),
            Evidence("runtime_type", "blackbox_v2"),
        ],
        errors=[f"Blackbox config failed strict loading: {exc}"],
        started_at=started_at,
        finished_at=utc_now(),
    )


def _cron_of(raw: dict) -> str | None:
    schedule = raw.get("schedule")
    if isinstance(schedule, dict):
        cron = schedule.get("cron")
        return str(cron) if cron else None
    return None


def _set_status(config_path: Path, status: str) -> bool:
    """只替换唯一根级 status 值，保留其它字节。"""
    original = config_path.read_bytes()
    updated = _replace_root_status(original, status)
    if updated == original:
        return False
    config_path.write_bytes(updated)
    return True


def _replace_root_status(config_bytes: bytes, status: str) -> bytes:
    """纯函数：只替换唯一根级 status 值。"""
    if status not in {"paused", "active"}:
        raise ValueError(f"unsupported Native lifecycle status: {status}")
    try:
        text = config_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("config.yaml must be valid UTF-8") from exc
    lines = text.splitlines(keepends=True)
    root_status_indexes = [
        index for index, line in enumerate(lines) if _ROOT_STATUS_KEY.match(line)
    ]
    if len(root_status_indexes) != 1:
        raise ValueError(
            "config.yaml must contain exactly one root-level status field"
        )
    index = root_status_indexes[0]
    match = _ROOT_STATUS_VALUE.fullmatch(lines[index])
    if match is None:
        raise ValueError("root-level status must be exactly paused or active")
    lines[index] = (
        match.group("prefix")
        + status
        + match.group("suffix")
        + (match.group("newline") or "")
    )
    return "".join(lines).encode("utf-8")


def _write_expected_active_config(
    config_path: Path,
    preflight: NativeActivationPreflight,
) -> bool:
    """仅当 config 仍等于 preflight 快照时写入预期 active 字节。"""
    current = config_path.read_bytes()
    if current != preflight.config_bytes:
        raise RuntimeError("config.yaml drifted after Native activation preflight")
    expected = preflight.expected_active_config_bytes
    if current == expected:
        return False
    config_path.write_bytes(expected)
    return True


def _compute_scheme_version(ctx: GateContext) -> str:
    """读取 config.yaml 计算当前方案版本号。"""
    config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
    scheme_dir = config_path.parent
    from shared.versioning import compute_code_hash, compute_config_hash, compute_scheme_version
    code_hash = compute_code_hash(scheme_dir)
    config_hash = compute_config_hash(config_path)
    return compute_scheme_version(code_hash, config_hash)


def _native_activation_authorization_errors(
    auth,
    validation_scheme_version: str,
) -> list[str]:
    """校验 Native 激活授权绑定验证版本并携带可审计签发者。"""
    if auth is None:
        return ["authorization token is required"]
    errors: list[str] = []
    if getattr(auth, "scheme_version", None) != validation_scheme_version:
        errors.append(
            "activate authorization scheme_version mismatch: "
            f"token={getattr(auth, 'scheme_version', None)}, "
            f"validation={validation_scheme_version}"
        )
    issued_by = getattr(auth, "issued_by", None)
    if not isinstance(issued_by, str) or not issued_by.strip():
        errors.append("activate authorization issued_by must be a non-empty string")
    issued_at = getattr(auth, "issued_at", None)
    try:
        parsed_issued_at = datetime.fromisoformat(issued_at)
    except (TypeError, ValueError):
        errors.append("activate authorization issued_at must be an ISO datetime")
    else:
        if parsed_issued_at.tzinfo is None:
            errors.append("activate authorization issued_at must include timezone")
    return errors


def _strict_native_activation_preflight(
    ctx: GateContext,
    validation_scheme_version: str,
) -> NativeActivationPreflight:
    """授权消费前冻结并校验 Native 验证版本的完整文件身份。"""
    from scheduler.discovery import discover_schemes
    from shared.versioning import (
        compute_code_hash,
        compute_manifest_hash,
        compute_scheme_version,
    )

    config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
    configs = discover_schemes(
        schemes_root=ctx.project_root / "schemes",
        strict=True,
    )
    target = next((cfg for cfg in configs if cfg.scheme_id == ctx.scheme_id), None)
    if target is None:
        raise ValueError(f"scheme not discovered before activation: {ctx.scheme_id}")
    if getattr(target, "runtime_type", None) != "native_adapter":
        raise ValueError(
            "validation config runtime_type is not native_adapter: "
            f"{getattr(target, 'runtime_type', None)}"
        )
    if target.status not in {"paused", "active"}:
        raise ValueError(
            "validation config status must be paused or active: "
            f"{target.status}"
        )
    if target.scheme_version != validation_scheme_version:
        raise ValueError(
            "validation config scheme_version drifted: "
            f"discovered={target.scheme_version}, expected={validation_scheme_version}"
        )
    config_bytes = config_path.read_bytes()
    try:
        config_text = config_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("config.yaml must be valid UTF-8") from exc
    scheme_dir = config_path.parent
    actual_code_hash = compute_code_hash(scheme_dir)
    actual_manifest_hash = compute_manifest_hash(scheme_dir)
    actual_config_hash = hashlib.sha256(config_bytes).hexdigest()
    mismatches = []
    for field_name, discovered, actual in (
        ("code_hash", target.code_hash, actual_code_hash),
        ("config_hash", target.config_hash, actual_config_hash),
        ("manifest_hash", target.manifest_hash, actual_manifest_hash),
    ):
        if discovered != actual:
            mismatches.append(
                f"{field_name}: discovered={discovered!r}, actual={actual!r}"
            )
    if mismatches:
        raise RuntimeError(
            "Native validation files drifted during strict discovery: "
            + "; ".join(mismatches)
        )
    if _replace_root_status(config_bytes, target.status) != config_bytes:
        raise RuntimeError("root-level status does not match strict discovery")

    expected_active_bytes = _replace_root_status(config_bytes, "active")
    expected_active_text = expected_active_bytes.decode("utf-8")
    expected_active_config_hash = hashlib.sha256(
        expected_active_bytes
    ).hexdigest()
    expected_active_scheme_version = compute_scheme_version(
        actual_code_hash,
        expected_active_config_hash,
    )
    return NativeActivationPreflight(
        validation_config=target,
        config_bytes=config_bytes,
        config_text=config_text,
        expected_active_config_bytes=expected_active_bytes,
        expected_active_config_text=expected_active_text,
        expected_active_config_hash=expected_active_config_hash,
        expected_active_scheme_version=expected_active_scheme_version,
        code_hash=actual_code_hash,
        manifest_hash=actual_manifest_hash,
        business_identity=_native_activation_business_identity(target),
    )


def _native_activation_business_identity(
    cfg: "SchemeConfig",
) -> tuple[tuple[str, object], ...]:
    """冻结除生命周期和派生哈希外的完整 SchemeConfig 业务身份。"""
    return tuple(
        (field.name, _freeze_identity_value(getattr(cfg, field.name)))
        for field in fields(cfg)
        if field.name not in _NATIVE_ACTIVATION_DERIVED_FIELDS
    )


def _freeze_identity_value(value: object) -> object:
    if is_dataclass(value):
        return tuple(
            (field.name, _freeze_identity_value(getattr(value, field.name)))
            for field in fields(value)
        )
    if isinstance(value, dict):
        return tuple(
            sorted(
                (str(key), _freeze_identity_value(item))
                for key, item in value.items()
            )
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_identity_value(item) for item in value)
    return value


REQUIRED_ACTIVATE_GATES = frozenset({
    "static", "input", "unit", "dry-run", "compare", "backtest", "api-readiness",
})


def _sync_registry_after_activation(
    ctx: GateContext,
    *,
    preflight: NativeActivationPreflight,
    approved_by: str,
    approved_at: datetime,
) -> str:
    """严格重载目标 config，并可信激活其翻转后的精确版本。"""
    from scheduler.repository import apply_native_activation_state

    target = _validate_native_activated_config(ctx, preflight)
    engine = ctx.engine_factory() if ctx.engine_factory is not None else _db_engine()
    owns_engine = ctx.engine_factory is None
    if engine is None:
        raise RuntimeError("cannot connect to database for registry sync")
    try:
        activated_version = apply_native_activation_state(
            engine,
            target,
            approved_by=approved_by,
            approved_at=approved_at,
        )
    finally:
        if owns_engine and engine is not None and hasattr(engine, "dispose"):
            engine.dispose()
    return activated_version


def _validate_native_activated_config(
    ctx: GateContext,
    preflight: NativeActivationPreflight,
) -> "SchemeConfig":
    """在创建 Engine 前校验 active config 与 preflight 预期完全一致。"""
    from scheduler.discovery import discover_schemes
    from shared.versioning import compute_code_hash, compute_manifest_hash

    config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
    if config_path.read_bytes() != preflight.expected_active_config_bytes:
        raise RuntimeError("activated config bytes do not match preflight expectation")
    configs = discover_schemes(
        schemes_root=ctx.project_root / "schemes",
        strict=True,
    )
    target = next((cfg for cfg in configs if cfg.scheme_id == ctx.scheme_id), None)
    if target is None:
        raise ValueError(f"scheme not discovered after activation: {ctx.scheme_id}")
    if config_path.read_bytes() != preflight.expected_active_config_bytes:
        raise RuntimeError("activated config bytes drifted during strict discovery")
    actual_code_hash = compute_code_hash(config_path.parent)
    actual_manifest_hash = compute_manifest_hash(config_path.parent)
    mismatches = []
    expected_values = {
        "status": "active",
        "version_status": "active",
        "code_hash": preflight.code_hash,
        "manifest_hash": preflight.manifest_hash,
        "config_hash": preflight.expected_active_config_hash,
        "scheme_version": preflight.expected_active_scheme_version,
        "business_identity": preflight.business_identity,
    }
    actual_values = {
        "status": target.status,
        "version_status": target.version_status,
        "code_hash": target.code_hash,
        "manifest_hash": target.manifest_hash,
        "config_hash": target.config_hash,
        "scheme_version": target.scheme_version,
        "business_identity": preflight.business_identity_for(target),
    }
    for field_name, expected in expected_values.items():
        if actual_values[field_name] != expected:
            mismatches.append(
                f"{field_name}: expected={expected!r}, "
                f"actual={actual_values[field_name]!r}"
            )
    if actual_code_hash != preflight.code_hash:
        mismatches.append(
            f"code_hash on disk: expected={preflight.code_hash!r}, "
            f"actual={actual_code_hash!r}"
        )
    if actual_manifest_hash != preflight.manifest_hash:
        mismatches.append(
            f"manifest_hash on disk: expected={preflight.manifest_hash!r}, "
            f"actual={actual_manifest_hash!r}"
        )
    if mismatches:
        raise RuntimeError(
            "activated Native identity does not match preflight: "
            + "; ".join(mismatches)
        )
    return target


def _verify_gate_history(ctx: GateContext, scheme_version: str) -> list[str]:
    """查询 t_harness_gate_results 确认当前版本已通过全部必要 gate。

    返回错误列表，空列表 = 校验通过。
    """
    from sqlalchemy import text

    config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
    raw_config = load_config_raw(config_path) if config_path.exists() else {}
    backtest_config = raw_config.get("backtest") if isinstance(raw_config.get("backtest"), dict) else {}
    benchmark_required = bool(backtest_config.get("benchmark_required"))
    engine = _db_engine()
    if engine is None:
        return ["cannot connect to database to verify gate history"]
    try:
        with engine.begin() as conn:
            # 查找最近一次 stage=all 且 status=passed 的 harness 运行
            run = conn.execute(
                text(
                    """
                    SELECT harness_run_id, finished_at
                    FROM t_harness_runs
                    WHERE scheme_id = :scheme_id
                      AND scheme_version = :scheme_version
                      AND stage = 'all'
                      AND status = 'passed'
                    ORDER BY finished_at DESC, harness_run_id DESC
                    LIMIT 1
                    """
                ),
                {"scheme_id": ctx.scheme_id, "scheme_version": scheme_version},
            ).one_or_none()
            if run is None:
                return [
                    f"no passed 'all' stage harness run found for {ctx.scheme_id} "
                    f"version {scheme_version}; run 'python -m harness onboard "
                    f"--scheme-id {ctx.scheme_id} --stage all' first"
                ]

            # 查找该运行中各 gate 的状态
            rows = conn.execute(
                text(
                    """
                    SELECT gate_name, status
                    FROM t_harness_gate_results
                    WHERE harness_run_id = :harness_run_id
                    """
                ),
                {"harness_run_id": run[0]},
            ).fetchall()

            gate_statuses = {str(row[0]): str(row[1]) for row in rows}
            if benchmark_required and gate_statuses.get("compare") == "skipped":
                return [
                    f"CompareGate status is skipped for benchmark_required scheme {ctx.scheme_id}; "
                    "run compare with original/current benchmarks until status=passed"
                ]
            passed_gates = {
                gate_name
                for gate_name, status in gate_statuses.items()
                if status == "passed" or (status == "skipped" and not benchmark_required)
            }
            missing = REQUIRED_ACTIVATE_GATES - passed_gates
            if missing:
                return [
                    f"required gates not all passed for harness run {run[0]}: "
                    f"missing/not passed: {sorted(missing)}"
                ]
            return []
    finally:
        if engine is not None and hasattr(engine, "dispose"):
            engine.dispose()


def _db_engine():
    """创建数据库连接。"""
    try:
        from scheduler.repository import create_engine_from_env
        return create_engine_from_env()
    except Exception:
        return None
