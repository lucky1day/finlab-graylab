from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from harness.operation import (
    operation_scope_sha256,
    verify_direct_operation,
)
from harness.context import GateContext
from shared.scheme_config_schema import ALLOWED_STATUS, validate_config
from harness.contracts.onboarding_policy import validate_onboarding_policy
from harness.gates.base import Gate, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus
from shared.scheme_config_loader import load_yaml_mapping

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
    expected_active_config_bytes: bytes
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


@dataclass(frozen=True)
class NativeActivationValidation:
    """激活前冻结的当前精确版本验证路径证据。"""

    validation_profile: str
    validation_harness_run_id: str
    validation_stage: str
    prior_admitted_scheme_version: str | None
    registry_scheme_ids: tuple[str, ...]
    benchmark_validation: str


@dataclass(frozen=True)
class NativePersistedBacktest:
    """Native 激活前所需的最新持久化回测事实。"""

    run_id: int
    data_source: str
    prediction_count: int


@dataclass(frozen=True)
class NativeActivationLifecycle:
    """Native 激活入口读取到的精确版本与 Registry lifecycle。"""

    activation_noop: bool
    version_status: str
    registry_status: str
    registry_scheme_ids: tuple[str, ...]


@dataclass(frozen=True)
class NativeHarnessGateHistory:
    """Native 激活读取到的 passed run 及其 Gate 行。"""

    run_id: str
    gate_rows: tuple[tuple[str, str], ...]


class ActivationGate(Gate):
    """Native 激活 gate：按直接操作执行 paused→active 或 active 精确版本重批准。"""

    name = "activate"
    requires_operation = True

    def run(self, ctx: GateContext) -> GateResult:
        config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
        cfg = ctx.config
        raw_runtime_type = None
        if config_path.is_file():
            try:
                raw = load_yaml_mapping(config_path)
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

        errors: list[str] = []
        if not config_path.exists():
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.FAILED,
                evidence=[Evidence("config_path", str(config_path))],
                errors=[f"config.yaml not found: {config_path}"],
                started_at=started_at,
                finished_at=finished_at,
            )

        raw = load_yaml_mapping(config_path)
        config_errors = validate_config(raw, ctx.scheme_id)
        if config_errors:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.FAILED,
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
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("onboarding_policy_allowed", False),
                    Evidence("onboarding_policy_errors", policy_errors),
                ],
                errors=policy_errors,
                started_at=started_at,
                finished_at=finished_at,
            )

        # 当前精确版本须有完整首次入库，或满足已准入 Native 修订的维护路径。
        validation_scheme_version = _compute_scheme_version(ctx)
        operation, operation_errors = verify_direct_operation(
            ctx.operation,
            scheme_id=ctx.scheme_id,
            action="activate",
            scheme_version=validation_scheme_version,
        )
        if operation is None or operation_errors:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("validation_scheme_version", validation_scheme_version),
                    Evidence("operation_required", True),
                ],
                errors=operation_errors,
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
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("validation_scheme_version", validation_scheme_version),
                    Evidence("strict_discovery", False),
                ],
                errors=[f"Native validation config failed strict discovery: {exc}"],
                started_at=started_at,
                finished_at=finished_at,
            )
        validation_ctx = replace(ctx, config=preflight.validation_config)
        lifecycle, lifecycle_errors = _native_activation_lifecycle_preflight(
            validation_ctx,
        )
        if lifecycle_errors:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("validation_scheme_version", validation_scheme_version),
                    Evidence("activation_noop", False),
                ],
                errors=lifecycle_errors,
                started_at=started_at,
                finished_at=finished_at,
            )
        assert lifecycle is not None
        if lifecycle.activation_noop:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.PASSED,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("validation_scheme_version", validation_scheme_version),
                    Evidence("activated_scheme_version", validation_scheme_version),
                    Evidence("config_path", str(config_path)),
                    Evidence("previous_status", "active"),
                    Evidence("new_status", "active"),
                    Evidence("status_flipped", False),
                    Evidence("activation_noop", True),
                    Evidence("version_status", lifecycle.version_status),
                    Evidence("registry_status", lifecycle.registry_status),
                    Evidence(
                        "registry_scheme_ids",
                        list(lifecycle.registry_scheme_ids),
                    ),
                    Evidence("gate_history_verified", False),
                    Evidence("persisted_backtest_verified", False),
                    Evidence("registry_synced", False),
                    Evidence("operation_applied", False),
                ],
                errors=[],
                started_at=started_at,
                finished_at=finished_at,
            )
        validation, gate_history_errors = _resolve_native_activation_validation(
            validation_ctx,
            validation_scheme_version,
        )
        if gate_history_errors:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("validation_scheme_version", validation_scheme_version),
                    Evidence("gate_history_errors", gate_history_errors),
                    *_validation_evidence(validation),
                ],
                errors=gate_history_errors,
                started_at=started_at,
                finished_at=finished_at,
            )
        if validation is None:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("validation_scheme_version", validation_scheme_version),
                    *_validation_evidence(None),
                ],
                errors=["activation validation did not return evidence"],
                started_at=started_at,
                finished_at=finished_at,
            )

        backtest, backtest_errors = _native_persisted_backtest_preflight(
            validation_ctx,
        )
        if backtest_errors:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("validation_scheme_version", validation_scheme_version),
                    *_validation_evidence(validation),
                    *_persisted_backtest_evidence(backtest),
                ],
                errors=backtest_errors,
                started_at=started_at,
                finished_at=finished_at,
            )
        assert backtest is not None

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
                approved_by=operation.issued_by.strip(),
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
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("validation_scheme_version", validation_scheme_version),
                    Evidence("config_path", str(config_path)),
                    Evidence("previous_status", previous_status),
                    Evidence("new_status", new_status),
                    Evidence("status_flipped", flipped),
                    Evidence("status_rolled_back", status_rolled_back),
                    Evidence("registry_synced", False),
                    *_validation_evidence(validation),
                ],
                errors=errors,
                started_at=started_at,
                finished_at=finished_at,
            )

        finished_at = utc_now()
        return GateResult(
            gate_name=self.name,
            status=GateStatus.PASSED,
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
                *_validation_evidence(validation),
                *_persisted_backtest_evidence(backtest),
                Evidence("operator", operation.issued_by),
                Evidence("operation_scope_sha256", operation_scope_sha256(operation)),
            ],
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
        )


def _blackbox_config_failure(started_at: str, config_path: Path, exc: Exception) -> GateResult:
    return GateResult(
        gate_name="activate",
        status=GateStatus.FAILED,
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
    if status not in ALLOWED_STATUS:
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
    if target.status not in ALLOWED_STATUS:
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
        config_bytes.decode("utf-8")
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
        expected_active_config_bytes=expected_active_bytes,
        expected_active_config_hash=expected_active_config_hash,
        expected_active_scheme_version=expected_active_scheme_version,
        code_hash=actual_code_hash,
        manifest_hash=actual_manifest_hash,
        business_identity=_native_activation_business_identity(target),
    )


def _native_activation_lifecycle_preflight(
    ctx: GateContext,
) -> tuple[NativeActivationLifecycle | None, list[str]]:
    """一次只读核验 exact Native version 与 expected Registry lifecycle。"""
    from sqlalchemy import text

    from scheduler.repository import registry_scheme_id

    cfg = ctx.config
    if cfg is None:
        return None, ["Native activation lifecycle preflight requires config"]
    try:
        expected_tenors = tuple(str(tenor) for tenor in cfg.tenors)
        if not expected_tenors or len(expected_tenors) != len(set(expected_tenors)):
            raise ValueError("Native target tenors must be non-empty and unique")
        expected_registry_ids = tuple(
            registry_scheme_id(cfg.scheme_id, int(cfg.horizon), tenor)
            for tenor in expected_tenors
        )
    except Exception as exc:  # noqa: BLE001 - malformed identity blocks activation.
        return None, [f"Native activation lifecycle identity is invalid: {exc}"]

    registry_placeholders = ", ".join(
        f":registry_scheme_id_{index}"
        for index, _registry_id in enumerate(expected_registry_ids)
    )
    registry_params: dict[str, object] = {"base_scheme_id": cfg.scheme_id}
    registry_params.update(
        {
            f"registry_scheme_id_{index}": registry_id
            for index, registry_id in enumerate(expected_registry_ids)
        }
    )

    engine, owns_engine = _validation_history_engine(ctx)
    if engine is None:
        return None, ["Native activation lifecycle preflight database unavailable"]
    try:
        with engine.begin() as conn:
            version_row = (
                conn.execute(
                    text(
                        """
                        SELECT scheme_id, scheme_version, runtime_type, status
                        FROM t_scheme_versions
                        WHERE scheme_id = :scheme_id
                          AND scheme_version = :scheme_version
                        LIMIT 1
                        """
                    ),
                    {
                        "scheme_id": cfg.scheme_id,
                        "scheme_version": cfg.scheme_version,
                    },
                )
                .mappings()
                .one_or_none()
            )
            registry_rows = (
                conn.execute(
                    text(
                        "SELECT scheme_id, base_scheme_id, horizon, task_type, "
                        "runtime_type, frequency, target_tenor, status "
                        "FROM t_scheme_registry "
                        f"WHERE scheme_id IN ({registry_placeholders}) "
                        "OR base_scheme_id = :base_scheme_id"
                    ),
                    registry_params,
                )
                .mappings()
                .all()
            )
    except Exception as exc:  # noqa: BLE001 - control-plane read must fail closed.
        return None, [f"Native activation lifecycle preflight read failed: {exc}"]
    finally:
        if owns_engine and hasattr(engine, "dispose"):
            engine.dispose()

    errors: list[str] = []
    if version_row is None:
        errors.append(
            "Native activation lifecycle exact version is missing: "
            f"{cfg.scheme_id}/{cfg.scheme_version}"
        )
        return None, errors

    expected_version_values = {
        "scheme_id": cfg.scheme_id,
        "scheme_version": cfg.scheme_version,
        "runtime_type": "native_adapter",
    }
    version_mismatches = [
        f"{field}: expected={expected!r}, actual={version_row.get(field)!r}"
        for field, expected in expected_version_values.items()
        if version_row.get(field) != expected
    ]
    if version_mismatches:
        errors.append(
            "Native activation lifecycle exact version identity mismatch: "
            + "; ".join(version_mismatches)
        )

    version_status = str(version_row.get("status") or "")
    registry_statuses = {str(row.get("status") or "") for row in registry_rows}
    registry_status = (
        next(iter(registry_statuses)) if len(registry_statuses) == 1 else ""
    )
    if version_status == "active" and registry_status == "active":
        activation_noop = True
        if cfg.status != "active":
            errors.append(
                "Native activation lifecycle config status is "
                f"{cfg.status}, expected active for active production state"
            )
    elif version_status == "draft" and registry_status == "paused":
        activation_noop = False
    else:
        activation_noop = False
        errors.append(
            "Native activation lifecycle is inconsistent: "
            f"config_status={cfg.status}, version_status={version_status!r}, "
            f"registry_statuses={sorted(registry_statuses)}"
        )

    if registry_status in ALLOWED_STATUS:
        registry_errors = _native_activation_registry_errors(
            cfg,
            expected_tenors,
            expected_registry_ids,
            registry_rows,
            expected_status=registry_status,
        )
        errors.extend(registry_errors)
    else:
        errors.append(
            "Native activation lifecycle Registry statuses must be uniformly "
            f"active or paused: {sorted(registry_statuses)}"
        )

    if errors:
        return None, errors
    return (
        NativeActivationLifecycle(
            activation_noop=activation_noop,
            version_status=version_status,
            registry_status=registry_status,
            registry_scheme_ids=expected_registry_ids,
        ),
        [],
    )


def _native_activation_registry_errors(
    cfg: "SchemeConfig",
    expected_tenors: tuple[str, ...],
    expected_registry_ids: tuple[str, ...],
    registry_rows: list[Mapping[str, object]],
    *,
    expected_status: str,
) -> list[str]:
    """核验 Native composite Registry 的生产业务身份与统一状态。"""
    actual_ids = [str(row.get("scheme_id")) for row in registry_rows]
    errors: list[str] = []
    duplicate_ids = sorted(
        {registry_id for registry_id in actual_ids if actual_ids.count(registry_id) > 1}
    )
    if duplicate_ids:
        errors.append(
            "Native activation lifecycle Registry has duplicate rows: "
            f"{duplicate_ids}"
        )
    if set(actual_ids) != set(expected_registry_ids):
        errors.append(
            "Native activation lifecycle Registry identity mismatch: "
            f"expected={sorted(expected_registry_ids)}, actual={sorted(actual_ids)}"
        )
        return errors

    rows_by_id = {str(row.get("scheme_id")): row for row in registry_rows}
    for target_tenor, registry_id in zip(
        expected_tenors,
        expected_registry_ids,
    ):
        row = rows_by_id[registry_id]
        expected_values = {
            "base_scheme_id": cfg.scheme_id,
            "runtime_type": "native_adapter",
            "task_type": cfg.task_type,
            "target_tenor": target_tenor,
            "frequency": cfg.frequency,
            "status": expected_status,
        }
        for field, expected in expected_values.items():
            if row.get(field) != expected:
                errors.append(
                    "Native activation lifecycle Registry field mismatch: "
                    f"registry_id={registry_id}, field={field}, "
                    f"expected={expected!r}, actual={row.get(field)!r}"
                )
        try:
            actual_horizon = int(row.get("horizon"))
        except (TypeError, ValueError):
            actual_horizon = None
        if actual_horizon != int(cfg.horizon):
            errors.append(
                "Native activation lifecycle Registry field mismatch: "
                f"registry_id={registry_id}, field=horizon, "
                f"expected={int(cfg.horizon)!r}, actual={row.get('horizon')!r}"
            )
    return errors


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
    "static", "input", "dry-run", "compare", "backtest",
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


def _resolve_native_activation_validation(
    ctx: GateContext,
    scheme_version: str,
) -> tuple[NativeActivationValidation | None, list[str]]:
    """解析当前 Native 精确版本可用于激活的完整或维护验证路径。"""
    full_validation, full_errors, full_database_available = (
        _passed_full_all_validation(ctx, scheme_version)
    )
    if full_validation is not None:
        return full_validation, []
    if not full_database_available:
        return None, full_errors

    maintenance_run_id, maintenance_errors, maintenance_database_available = (
        _passed_native_maintenance_validation(ctx, scheme_version)
    )
    if not maintenance_database_available:
        return None, _combine_validation_errors(full_errors, maintenance_errors)
    if maintenance_run_id is None:
        return None, _combine_validation_errors(full_errors, maintenance_errors)

    try:
        from harness.gates.native_maintenance_admission_gate import (
            verify_native_maintenance_admission,
        )

        admission, admission_errors = verify_native_maintenance_admission(ctx)
    except Exception as exc:  # noqa: BLE001 - activation must fail closed.
        return None, _combine_validation_errors(
            full_errors,
            [f"native maintenance admission verification failed: {exc}"],
        )
    if admission is None:
        return None, _combine_validation_errors(full_errors, admission_errors)

    return (
        NativeActivationValidation(
            validation_profile="native_post_admission_revision_v1",
            validation_harness_run_id=maintenance_run_id,
            validation_stage="native-maintenance",
            prior_admitted_scheme_version=admission.prior_admitted_scheme_version,
            registry_scheme_ids=admission.registry_scheme_ids,
            benchmark_validation="not_run_post_admission",
        ),
        [],
    )


def _passed_full_all_validation(
    ctx: GateContext,
    scheme_version: str,
) -> tuple[NativeActivationValidation | None, list[str], bool]:
    """保留首次入库 ``all`` 路径的精确历史查询和 gate 语义。"""
    config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
    raw_config = load_yaml_mapping(config_path) if config_path.exists() else {}
    backtest_config = (
        raw_config.get("backtest")
        if isinstance(raw_config.get("backtest"), dict)
        else {}
    )
    benchmark_required = bool(backtest_config.get("benchmark_required"))
    history, history_errors, database_available = _read_passed_gate_history(
        ctx,
        scheme_version,
        stage="all",
    )
    if history is None:
        return None, history_errors, database_available
    gate_rows = history.gate_rows
    duplicates, missing, extra = _gate_name_differences(
        gate_rows,
        REQUIRED_ACTIVATE_GATES,
    )
    if benchmark_required and ("compare", "skipped") in gate_rows:
        return (
            None,
            [
                f"CompareGate status is skipped for benchmark_required scheme {ctx.scheme_id}; "
                "run compare with original/current benchmarks until status=passed"
            ],
            True,
        )
    invalid_statuses = sorted(
        f"{gate_name}={status}"
        for gate_name, status in gate_rows
        if status != "passed"
        and not (
            gate_name == "compare"
            and status == "skipped"
            and not benchmark_required
        )
    )
    if (
        len(gate_rows) != len(REQUIRED_ACTIVATE_GATES)
        or duplicates
        or missing
        or extra
        or invalid_statuses
    ):
        return (
            None,
            [
                "all-stage harness run must have the exact current gate multiset "
                "with benchmark-policy statuses: "
                f"harness_run_id={history.run_id}, row_count={len(gate_rows)}, "
                f"duplicates={duplicates}, missing={missing}, extra={extra}, "
                f"invalid_statuses={invalid_statuses}"
            ],
            True,
        )
    return (
        NativeActivationValidation(
            validation_profile="full_initial_onboarding_v1",
            validation_harness_run_id=history.run_id,
            validation_stage="all",
            prior_admitted_scheme_version=None,
            registry_scheme_ids=(),
            benchmark_validation="passed_initial_admission",
        ),
        [],
        True,
    )


def _passed_native_maintenance_validation(
    ctx: GateContext,
    scheme_version: str,
) -> tuple[str | None, list[str], bool]:
    """读取维护阶段的当前版本 Gate，不读取当前 compare/backtest 结果。"""
    from harness.gates.native_maintenance_admission_gate import (
        NATIVE_MAINTENANCE_SEQUENCE,
    )

    required = frozenset(NATIVE_MAINTENANCE_SEQUENCE)
    history, history_errors, database_available = _read_passed_gate_history(
        ctx,
        scheme_version,
        stage="native-maintenance",
    )
    if history is None:
        return None, history_errors, database_available
    gate_rows = history.gate_rows
    duplicates, missing, extra = _gate_name_differences(gate_rows, required)
    non_passed = sorted(
        f"{gate_name}={status}"
        for gate_name, status in gate_rows
        if status != "passed"
    )
    if (
        len(gate_rows) != len(required)
        or duplicates
        or missing
        or extra
        or non_passed
    ):
        return (
            None,
            [
                "native-maintenance harness run must have the exact current "
                "passed gate rows: "
                f"harness_run_id={history.run_id}, row_count={len(gate_rows)}, "
                f"duplicates={duplicates}, missing={missing}, extra={extra}, "
                f"non_passed={non_passed}"
            ],
            True,
        )
    return history.run_id, [], True


def _read_passed_gate_history(
    ctx: GateContext,
    scheme_version: str,
    *,
    stage: str,
) -> tuple[NativeHarnessGateHistory | None, list[str], bool]:
    """读取一个 Native stage 的 latest passed run 与 Gate 行。"""
    from sqlalchemy import text

    engine, owns_engine = _validation_history_engine(ctx)
    if engine is None:
        return None, ["cannot connect to database to verify gate history"], False
    try:
        with engine.begin() as conn:
            run_id = conn.execute(
                text(
                    """
                    SELECT harness_run_id
                    FROM t_harness_runs
                    WHERE scheme_id = :scheme_id
                      AND scheme_version = :scheme_version
                      AND stage = :stage
                      AND status = 'passed'
                    ORDER BY finished_at DESC, harness_run_id DESC
                    LIMIT 1
                    """
                ),
                {
                    "scheme_id": ctx.scheme_id,
                    "scheme_version": scheme_version,
                    "stage": stage,
                },
            ).scalar_one_or_none()
            if run_id is None:
                return (
                    None,
                    [
                        f"no passed {stage!r} harness run found for "
                        f"{ctx.scheme_id} version {scheme_version}"
                    ],
                    True,
                )
            gate_rows = tuple(
                (str(row[0]), str(row[1]))
                for row in conn.execute(
                    text(
                        "SELECT gate_name, status "
                        "FROM t_harness_gate_results "
                        "WHERE harness_run_id = :harness_run_id"
                    ),
                    {"harness_run_id": run_id},
                )
            )
    except Exception as exc:  # noqa: BLE001 - database errors block activation.
        return None, [f"cannot read database to verify gate history: {exc}"], False
    finally:
        if owns_engine and hasattr(engine, "dispose"):
            engine.dispose()

    return (
        NativeHarnessGateHistory(
            run_id=str(run_id),
            gate_rows=gate_rows,
        ),
        [],
        True,
    )


def _gate_name_differences(
    gate_rows: tuple[tuple[str, str], ...],
    required: frozenset[str],
) -> tuple[list[str], list[str], list[str]]:
    gate_names = [gate_name for gate_name, _status in gate_rows]
    duplicates = sorted(
        {gate_name for gate_name in gate_names if gate_names.count(gate_name) > 1}
    )
    return (
        duplicates,
        sorted(required - set(gate_names)),
        sorted(set(gate_names) - required),
    )


def _validation_history_engine(ctx: GateContext) -> tuple[object | None, bool]:
    """创建验证查询 engine，并由调用方负责释放。"""
    if ctx.engine_factory is not None:
        return ctx.engine_factory(), True
    return _db_engine(), True


def _native_persisted_backtest_preflight(
    ctx: GateContext,
) -> tuple[NativePersistedBacktest | None, list[str]]:
    """只读核验 base scheme 在运行时默认口径下的最新成功回测。"""
    from sqlalchemy import text

    from backend.factor_lab_dashboard_semantics import (
        BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE,
    )

    runtime_type = str(getattr(ctx.config, "runtime_type", ""))
    try:
        data_source = BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE[runtime_type]
    except KeyError:
        return (
            None,
            [
                "Native activation runtime_type has no default persisted backtest "
                f"data_source: {runtime_type!r}"
            ],
        )

    engine, owns_engine = _validation_history_engine(ctx)
    if engine is None:
        return None, ["cannot read persisted backtest: database unavailable"]
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT r.id AS run_id, COUNT(p.id) AS prediction_count
                    FROM t_backtest_runs AS r
                    LEFT JOIN t_backtest_predictions AS p
                      ON p.run_id = r.id
                    WHERE r.scheme_id = :scheme_id
                      AND r.data_source = :data_source
                      AND r.status = 'success'
                    GROUP BY r.id, r.updated_at
                    ORDER BY r.updated_at DESC, r.id DESC
                    LIMIT 1
                    """
                ),
                {
                    "scheme_id": ctx.scheme_id,
                    "data_source": data_source,
                },
            ).one_or_none()
    except Exception as exc:  # noqa: BLE001 - DB read failure blocks activation.
        return None, [f"cannot read persisted backtest for Native activation: {exc}"]
    finally:
        if owns_engine and hasattr(engine, "dispose"):
            engine.dispose()

    if row is None:
        return (
            None,
            [
                "latest successful persisted backtest run missing for "
                f"scheme_id={ctx.scheme_id} data_source={data_source}"
            ],
        )
    run_id = int(row[0])
    prediction_count = int(row[1] or 0)
    backtest = NativePersistedBacktest(
        run_id=run_id,
        data_source=data_source,
        prediction_count=prediction_count,
    )
    if prediction_count <= 0:
        return (
            backtest,
            [
                "latest successful persisted backtest has no prediction rows: "
                f"run_id={run_id} scheme_id={ctx.scheme_id} "
                f"data_source={data_source}"
            ],
        )
    return backtest, []


def _combine_validation_errors(*error_sets: list[str]) -> list[str]:
    """保留首次入库失败原因，同时补充维护路径的拒绝证据。"""
    combined: list[str] = []
    for errors in error_sets:
        for error in errors:
            if error not in combined:
                combined.append(error)
    return combined


def _persisted_backtest_evidence(
    backtest: NativePersistedBacktest | None,
) -> list[Evidence]:
    """将 Native persisted backtest preflight 编码为激活证据。"""
    return [
        Evidence(
            "latest_backtest_run_id",
            backtest.run_id if backtest is not None else None,
        ),
        Evidence(
            "latest_backtest_data_source",
            backtest.data_source if backtest is not None else None,
        ),
        Evidence(
            "latest_backtest_prediction_count",
            backtest.prediction_count if backtest is not None else 0,
        ),
    ]


def _validation_evidence(
    validation: NativeActivationValidation | None,
) -> list[Evidence]:
    """将验证路径统一编码为成功与历史拒绝均可审计的 Evidence。"""
    return [
        Evidence(
            "validation_profile",
            validation.validation_profile if validation is not None else None,
        ),
        Evidence(
            "validation_harness_run_id",
            validation.validation_harness_run_id if validation is not None else None,
        ),
        Evidence(
            "validation_stage",
            validation.validation_stage if validation is not None else None,
        ),
        Evidence(
            "prior_admitted_scheme_version",
            (
                validation.prior_admitted_scheme_version
                if validation is not None
                else None
            ),
        ),
        Evidence(
            "admission_registry_scheme_ids",
            list(validation.registry_scheme_ids) if validation is not None else [],
        ),
        Evidence(
            "benchmark_validation",
            validation.benchmark_validation if validation is not None else None,
        ),
    ]


def _db_engine():
    """创建数据库连接。"""
    try:
        from scheduler.repository import create_engine_from_env
        return create_engine_from_env()
    except Exception:
        return None
