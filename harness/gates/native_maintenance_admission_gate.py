from __future__ import annotations

from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Mapping

from sqlalchemy import text

from harness.context import GateContext
from harness.contracts.onboarding_policy import validate_onboarding_policy
from harness.gates.base import Gate, utc_now
from harness.result import Evidence, GateResult, GateStatus
from shared.scheme_config_schema import ALLOWED_STATUS

if TYPE_CHECKING:
    from scheduler.discovery import SchemeConfig


NATIVE_MAINTENANCE_STAGE = "native-maintenance"
NATIVE_MAINTENANCE_SEQUENCE = (
    "static",
    "native-maintenance-admission",
    "dry-run",
)
_VALIDATION_PROFILE = "native_post_admission_revision_v1"
NATIVE_BUSINESS_IDENTITY_EVIDENCE_KEY = "native_business_identity"
_NATIVE_BUSINESS_IDENTITY_FIELDS = frozenset(
    {
        "scheme_id",
        "runtime_type",
        "horizon",
        "task_type",
        "frequency",
        "tenors",
        "registry_scheme_ids",
    }
)


@dataclass(frozen=True)
class NativeMaintenanceAdmission:
    """Native 维护版进入自动 Gate 前所需的既往准入证据。"""

    prior_admitted_scheme_version: str
    prior_harness_run_id: str
    registry_scheme_ids: tuple[str, ...]
    current_candidate_runtime_type: str
    current_candidate_status: str
    registry_lifecycle: str


@dataclass(frozen=True)
class PriorNativeStaticIdentityState:
    """既往 StaticGate 身份字段的严格读取状态。"""

    identity: dict[str, object] | None
    error: str | None


class NativeMaintenanceAdmissionGate(Gate):
    """只读核验 Native 修订版是否源于已准入的既往版本。"""

    name = "native-maintenance-admission"

    def run(self, ctx: GateContext) -> GateResult:
        started_at = utc_now()
        admission, errors = verify_native_maintenance_admission(ctx)
        finished_at = utc_now()
        if admission is None:
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("validation_profile", _VALIDATION_PROFILE),
                    Evidence("admission_verified", False),
                ],
                errors=errors or [
                    "native maintenance admission verification did not return evidence"
                ],
                started_at=started_at,
                finished_at=finished_at,
            )
        evidence = [
            Evidence("scheme_id", ctx.scheme_id),
            Evidence("validation_profile", _VALIDATION_PROFILE),
            Evidence(
                "prior_admitted_scheme_version",
                admission.prior_admitted_scheme_version,
            ),
            Evidence("prior_harness_run_id", admission.prior_harness_run_id),
            Evidence("registry_scheme_ids", list(admission.registry_scheme_ids)),
            Evidence(
                "current_candidate_runtime_type",
                admission.current_candidate_runtime_type,
            ),
            Evidence(
                "current_candidate_status",
                admission.current_candidate_status,
            ),
            Evidence("registry_lifecycle", admission.registry_lifecycle),
        ]
        return GateResult(
            gate_name=self.name,
            status=GateStatus.PASSED,
            evidence=evidence,
            errors=[],
            started_at=started_at,
            finished_at=finished_at,
        )


def verify_native_maintenance_admission(
    ctx: GateContext,
) -> tuple[NativeMaintenanceAdmission | None, list[str]]:
    """核验当前 Native active 修订版的既往准入和 Registry 身份。"""
    cfg = ctx.config
    config_errors = _config_and_policy_errors(ctx)
    if config_errors:
        return None, config_errors
    assert cfg is not None

    try:
        from scheduler.repository import _expected_registry_identity

        expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
        current_business_identity = native_business_identity_snapshot(
            scheme_id=cfg.scheme_id,
            runtime_type=cfg.runtime_type,
            horizon=cfg.horizon,
            task_type=cfg.task_type,
            frequency=cfg.frequency,
            tenors=cfg.tenors,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed for malformed config identity.
        return None, [f"native maintenance registry identity is invalid: {exc}"]

    engine = None
    owns_engine = ctx.engine_factory is None
    try:
        if ctx.engine_factory is not None:
            engine = ctx.engine_factory()
        else:
            from scheduler.repository import create_engine_from_env

            engine = create_engine_from_env()
        if engine is None:
            return None, ["cannot connect to database for native maintenance admission"]

        with engine.begin() as conn:
            current_version_row = _read_current_native_candidate_conn(
                conn,
                scheme_id=str(cfg.scheme_id),
                scheme_version=str(cfg.scheme_version),
            )
            prior_row = _read_prior_native_admission_conn(
                conn,
                scheme_id=str(cfg.scheme_id),
                current_scheme_version=str(cfg.scheme_version),
            )
            prior_compare_error = (
                _prior_compare_evidence_error_conn(
                    conn,
                    harness_run_id=str(prior_row["harness_run_id"]),
                )
                if prior_row is not None
                else None
            )
            prior_static_identity = (
                _read_prior_native_static_identity_state_conn(
                    conn,
                    harness_run_id=str(prior_row["harness_run_id"]),
                )
                if prior_row is not None
                else PriorNativeStaticIdentityState(None, None)
            )
            prior_business_identity = prior_static_identity.identity
            prior_identity_error = prior_static_identity.error
            registry_rows = _read_active_registry_rows_conn(
                conn,
                cfg,
                expected_registry_ids,
            )
    except Exception as exc:  # noqa: BLE001 - unavailable control-plane data blocks admission.
        return None, [f"native maintenance admission read failed: {exc}"]
    finally:
        if owns_engine and engine is not None and hasattr(engine, "dispose"):
            engine.dispose()

    errors: list[str] = []
    current_version_status, current_version_error = _current_native_candidate_error(
        current_version_row,
        scheme_id=str(cfg.scheme_id),
        scheme_version=str(cfg.scheme_version),
    )
    if current_version_error is not None:
        errors.append(current_version_error)
    if prior_row is None:
        errors.append(
            "no prior active Native version has a passed all-stage harness run "
            "with a passed compare gate"
        )
    elif prior_compare_error is not None:
        errors.append(prior_compare_error)
    elif prior_identity_error is not None:
        errors.append(prior_identity_error)
    elif prior_business_identity != current_business_identity:
        errors.append(
            "prior admitted Native static gate identity snapshot does not match "
            "current Native business identity"
        )
    registry_lifecycle = _uniform_registry_lifecycle(registry_rows)
    registry_error = _registry_identity_error(
        cfg,
        expected_tenors,
        expected_registry_ids,
        registry_rows,
    )
    if registry_error is not None:
        errors.append(f"native maintenance registry identity failed: {registry_error}")
    else:
        frequency_error = _registry_frequency_error(
            cfg,
            expected_registry_ids,
            registry_rows,
        )
        if frequency_error is not None:
            errors.append(
                "native maintenance registry identity failed: "
                f"{frequency_error}"
            )
        elif (
            registry_lifecycle == "active"
            and current_version_status == "draft"
        ):
            errors.append(
                "current exact Native version draft requires a uniformly paused "
                "Registry before activation"
            )
    if errors:
        return None, errors

    assert prior_row is not None
    assert current_version_row is not None
    assert current_version_status is not None
    assert registry_lifecycle in ALLOWED_STATUS
    return (
        NativeMaintenanceAdmission(
            prior_admitted_scheme_version=str(prior_row["scheme_version"]),
            prior_harness_run_id=str(prior_row["harness_run_id"]),
            registry_scheme_ids=tuple(expected_registry_ids),
            current_candidate_runtime_type=str(current_version_row["runtime_type"]),
            current_candidate_status=current_version_status,
            registry_lifecycle=registry_lifecycle,
        ),
        [],
    )


def _config_and_policy_errors(ctx: GateContext) -> list[str]:
    """在任何数据库连接前拒绝非 Native active 或政策外身份。"""
    cfg = ctx.config
    if cfg is None:
        return ["native maintenance admission requires a loaded scheme config"]

    errors: list[str] = []
    config_scheme_id = getattr(cfg, "scheme_id", None)
    if not isinstance(config_scheme_id, str) or not config_scheme_id.strip():
        errors.append(
            "native maintenance admission requires a non-empty config scheme_id"
        )
    elif config_scheme_id != ctx.scheme_id:
        errors.append(
            "native maintenance admission config scheme_id mismatch: "
            f"config={config_scheme_id}, context={ctx.scheme_id}"
        )
    runtime_type = getattr(cfg, "runtime_type", None)
    if runtime_type != "native_adapter":
        errors.append(
            "native maintenance admission requires runtime_type=native_adapter, "
            f"got {runtime_type}"
        )
    if getattr(cfg, "status", None) != "active":
        errors.append(
            "native maintenance admission requires status=active, "
            f"got {getattr(cfg, 'status', None)}"
        )
    scheme_version = getattr(cfg, "scheme_version", None)
    if not isinstance(scheme_version, str) or not scheme_version.strip():
        errors.append(
            "native maintenance admission requires a non-empty config scheme_version"
        )

    if errors:
        return errors

    policy_errors = validate_onboarding_policy(
        ctx.project_root,
        config_scheme_id,
        str(runtime_type),
    )
    errors.extend(policy_errors)
    return errors


def _read_prior_native_admission_conn(
    conn,
    *,
    scheme_id: str,
    current_scheme_version: str,
) -> Mapping[str, object] | None:
    """读取最新且不同于当前版本的已准入 Native 版本。"""
    return (
        conn.execute(
            text(
                """
                SELECT v.scheme_version, r.harness_run_id
                FROM t_scheme_versions AS v
                INNER JOIN t_harness_runs AS r
                    ON r.scheme_id = v.scheme_id
                    AND r.scheme_version = v.scheme_version
                INNER JOIN t_harness_gate_results AS gr
                    ON gr.harness_run_id = r.harness_run_id
                WHERE v.scheme_id = :scheme_id
                  AND v.runtime_type = 'native_adapter'
                  AND v.status = 'active'
                  AND v.scheme_version <> :current_scheme_version
                  AND r.stage = 'all'
                  AND r.status = 'passed'
                  AND gr.gate_name = 'compare'
                  AND gr.status = 'passed'
                ORDER BY r.finished_at DESC, r.harness_run_id DESC
                LIMIT 1
                """
            ),
            {
                "scheme_id": scheme_id,
                "current_scheme_version": current_scheme_version,
            },
        )
        .mappings()
        .one_or_none()
    )


def _read_current_native_candidate_conn(
    conn,
    *,
    scheme_id: str,
    scheme_version: str,
) -> Mapping[str, object] | None:
    """读取当前精确候选版本的 lifecycle，避免维护证据脱离 DB 身份。"""
    return (
        conn.execute(
            text(
                """
                SELECT runtime_type, status
                FROM t_scheme_versions
                WHERE scheme_id = :scheme_id
                  AND scheme_version = :scheme_version
                LIMIT 1
                """
            ),
            {
                "scheme_id": scheme_id,
                "scheme_version": scheme_version,
            },
        )
        .mappings()
        .one_or_none()
    )


def _current_native_candidate_error(
    row: Mapping[str, object] | None,
    *,
    scheme_id: str,
    scheme_version: str,
) -> tuple[str | None, str | None]:
    """只接受当前精确 Native draft/active lifecycle。"""
    if row is None:
        return (
            None,
            "current exact Native version is missing from t_scheme_versions: "
            f"{scheme_id}/{scheme_version}",
        )
    if row.get("runtime_type") != "native_adapter":
        return (
            None,
            "current exact Native version runtime_type is "
            f"{row.get('runtime_type')}, expected native_adapter",
        )
    status = row.get("status")
    if status not in {"draft", "active"}:
        return (
            None,
            "current exact Native version status is "
            f"{status}, expected draft or active",
        )
    return str(status), None


def _read_prior_native_static_identity_state_conn(
    conn,
    *,
    harness_run_id: str,
) -> PriorNativeStaticIdentityState:
    """严格读取 prior StaticGate 持久化的 Native 业务身份。"""
    rows = (
        conn.execute(
            text(
                """
                SELECT status, summary_json
                FROM t_harness_gate_results
                WHERE harness_run_id = :harness_run_id
                  AND gate_name = 'static'
                """
            ),
            {"harness_run_id": harness_run_id},
        )
        .mappings()
        .all()
    )
    if len(rows) != 1:
        return PriorNativeStaticIdentityState(
            None,
            "prior admitted Native static gate identity snapshot is missing",
        )
    row = rows[0]
    if row.get("status") != "passed":
        return PriorNativeStaticIdentityState(
            None,
            "prior admitted Native static gate identity snapshot is missing",
        )

    summary = _decode_summary_json(row.get("summary_json"))
    if summary is None:
        return PriorNativeStaticIdentityState(
            None,
            "prior admitted Native static gate identity snapshot is malformed",
        )
    evidence = summary.get("evidence")
    if not isinstance(evidence, list):
        return PriorNativeStaticIdentityState(
            None,
            "prior admitted Native static gate identity snapshot is malformed",
        )
    snapshots = [
        item.get("value")
        for item in evidence
        if isinstance(item, Mapping)
        and item.get("key") == NATIVE_BUSINESS_IDENTITY_EVIDENCE_KEY
    ]
    if not snapshots:
        return PriorNativeStaticIdentityState(
            None,
            "prior admitted Native static gate identity snapshot is missing",
        )
    if len(snapshots) != 1:
        return PriorNativeStaticIdentityState(
            None,
            "prior admitted Native static gate identity snapshot is missing",
        )
    snapshot = _canonical_prior_business_identity_snapshot(snapshots[0])
    if snapshot is None:
        return PriorNativeStaticIdentityState(
            None,
            "prior admitted Native static gate identity snapshot is malformed",
        )
    return PriorNativeStaticIdentityState(snapshot, None)


def _prior_compare_evidence_error_conn(
    conn,
    *,
    harness_run_id: str,
) -> str | None:
    """要求被选 prior all run 的 CompareGate 证据唯一且通过。"""
    rows = (
        conn.execute(
            text(
                """
                SELECT status
                FROM t_harness_gate_results
                WHERE harness_run_id = :harness_run_id
                  AND gate_name = 'compare'
                """
            ),
            {"harness_run_id": harness_run_id},
        )
        .mappings()
        .all()
    )
    if len(rows) != 1 or rows[0].get("status") != "passed":
        return (
            "prior admitted Native CompareGate evidence must be uniquely "
            "passed"
        )
    return None


def _decode_summary_json(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _canonical_prior_business_identity_snapshot(
    value: object,
) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or set(value) != _NATIVE_BUSINESS_IDENTITY_FIELDS:
        return None
    try:
        canonical = native_business_identity_snapshot(
            scheme_id=value["scheme_id"],
            runtime_type=value["runtime_type"],
            horizon=value["horizon"],
            task_type=value["task_type"],
            frequency=value["frequency"],
            tenors=value["tenors"],
        )
    except (TypeError, ValueError):
        return None
    return canonical if value == canonical else None


def native_business_identity_snapshot(
    *,
    scheme_id: object,
    runtime_type: object,
    horizon: object,
    task_type: object,
    frequency: object,
    tenors: object,
) -> dict[str, object]:
    """构造不含版本或展示字段的确定性 Native 业务身份快照。"""
    if not isinstance(scheme_id, str) or not scheme_id:
        raise ValueError("business identity scheme_id must be a non-empty string")
    if runtime_type != "native_adapter":
        raise ValueError("business identity runtime_type must be native_adapter")
    if type(horizon) is not int or horizon <= 0:
        raise ValueError("business identity horizon must be a positive integer")
    if not isinstance(task_type, str) or not task_type:
        raise ValueError("business identity task_type must be a non-empty string")
    if not isinstance(frequency, str) or not frequency:
        raise ValueError("business identity frequency must be a non-empty string")
    if not isinstance(tenors, (list, tuple)) or not tenors:
        raise ValueError("business identity tenors must be a non-empty list or tuple")
    if not all(isinstance(tenor, str) and tenor for tenor in tenors):
        raise ValueError("business identity tenors must contain non-empty strings")

    from scheduler.repository import registry_scheme_id

    sorted_tenors = sorted(tenors)
    return {
        "scheme_id": scheme_id,
        "runtime_type": "native_adapter",
        "horizon": horizon,
        "task_type": task_type,
        "frequency": frequency,
        "tenors": sorted_tenors,
        "registry_scheme_ids": sorted(
            registry_scheme_id(scheme_id, horizon, tenor)
            for tenor in sorted_tenors
        ),
    }


def _read_active_registry_rows_conn(
    conn,
    cfg: "SchemeConfig",
    expected_registry_ids: tuple[str, ...],
) -> list[Mapping[str, object]]:
    """复用 repository 的只读 Registry 身份查询。"""
    from scheduler.repository import _read_scheme_registry_rows_conn

    return _read_scheme_registry_rows_conn(
        conn,
        cfg,
        expected_registry_ids,
        for_update=False,
    )


def _registry_identity_error(
    cfg: "SchemeConfig",
    expected_tenors: tuple[str, ...],
    expected_registry_ids: tuple[str, ...],
    registry_rows: list[Mapping[str, object]],
) -> str | None:
    """复用 Registry 身份核验，允许候选版本激活前保持统一 paused。"""
    from scheduler.repository import _registry_identity_error as repository_error

    registry_statuses = {str(row.get("status")) for row in registry_rows}
    expected_status = "paused" if registry_statuses == {"paused"} else "active"
    return repository_error(
        cfg,
        expected_tenors,
        expected_registry_ids,
        registry_rows,
        expected_status=expected_status,
        expected_runtime_type="native_adapter",
    )


def _uniform_registry_lifecycle(
    registry_rows: list[Mapping[str, object]],
) -> str | None:
    """返回统一且可用于 Native 激活前验证的 Registry lifecycle。"""
    statuses = {str(row.get("status")) for row in registry_rows}
    if len(statuses) != 1:
        return None
    status = next(iter(statuses))
    return status if status in ALLOWED_STATUS else None


def _registry_frequency_error(
    cfg: "SchemeConfig",
    expected_registry_ids: tuple[str, ...],
    registry_rows: list[Mapping[str, object]],
) -> str | None:
    """补充 repository 身份函数未覆盖的 frequency 字段核验。"""
    rows_by_id = {str(row.get("scheme_id")): row for row in registry_rows}
    for expected_id in expected_registry_ids:
        actual_frequency = rows_by_id[expected_id].get("frequency")
        if actual_frequency != cfg.frequency:
            return (
                f"registry {expected_id} frequency is {actual_frequency}, "
                f"expected {cfg.frequency}"
            )
    return None
