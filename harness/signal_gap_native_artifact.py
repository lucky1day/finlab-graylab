from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Mapping

from harness.authorization import (
    _atomic_write_json,
    authorization_token_hash,
    mark_token_used,
    used_tokens_path,
    verify_signal_gap_native_cache_prewarm_authorization,
    verify_signal_gap_native_artifact_register_authorization,
    write_authorization_audit,
)
from scheduler.discovery import load_scheme_config
from scheduler.executor import (
    NATIVE_EXECUTION_MODE_SIGNAL_GAP_CACHE_PREWARM,
    run_configured_scheme,
)
from scheduler.generation_registry import (
    register_gray_gap_native_artifact,
)
from scheduler.repository import (
    create_engine_from_env,
    read_sealed_input_generation,
)
from shared.native_input_generation import (
    SIGNAL_GAP_NATIVE_EXPORTER_VERSION,
    create_signal_gap_native_artifact,
    open_signal_gap_native_artifact,
    resolve_signal_gap_native_storage_root,
)
from shared.liwei_0616_cache_contract import (
    SIGNAL_GAP_CACHE_PREWARM_PUBLISHER_SCHEME_ID,
)
from shared.liwei_0616_signal_gap_prewarm import (
    create_signal_gap_cache_prewarm_permit,
)


_PURPOSE = "signal_gap_gray_live_current_snapshot"
_VINTAGE_DISCLAIMER = (
    "current_snapshot_as_of_not_historical_vintage"
)
_PREWARM_AUTHORITY_TYPE = "native_current_snapshot_cache_prewarm"
_PREWARM_PURPOSE = "signal_gap_native_cache_prewarm"
_NATIVE_GENERATION_ID_PATTERN = re.compile(r"^native-[0-9a-f]{24}$")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SignalGapNativeArtifactRegistrationError(RuntimeError):
    """携带 token 消费状态与不可覆盖审计位置的登记失败。"""

    def __init__(
        self,
        message: str,
        *,
        failure_code: str,
        token_consumed: bool,
        audit_path: Path | None,
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code
        self.token_consumed = token_consumed
        self.audit_path = audit_path


class SignalGapNativeCachePrewarmError(RuntimeError):
    """封存 Native cache 预热失败时保留明确的消费与审计语义。"""

    def __init__(
        self,
        message: str,
        *,
        failure_code: str,
        token_consumed: bool,
        audit_path: Path | None,
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code
        self.token_consumed = token_consumed
        self.audit_path = audit_path


def native_artifact_registration_authority(
    context: Any,
    *,
    storage_root_identity: str,
) -> dict[str, str]:
    """构造 HMAC 与登记共同绑定的完整 artifact provenance。"""
    if (
        context.generation_type != "native_source"
        or context.readiness_basis != "CLOCK_CONTRACT"
    ):
        raise ValueError(
            "signal-gap Native artifact source/readiness is invalid"
        )
    if context.exporter_version != SIGNAL_GAP_NATIVE_EXPORTER_VERSION:
        raise ValueError(
            "signal-gap Native artifact exporter_version is invalid"
        )
    return {
        "authority_type": "native_current_snapshot_artifact",
        "purpose": _PURPOSE,
        "artifact_id": context.generation_id,
        "manifest_uri": str(context.manifest_path),
        "manifest_sha256": context.manifest_sha256,
        "storage_root_identity": storage_root_identity,
        "dataset_content_id": context.dataset_content_id,
        "source_commit_token": context.source_commit_token,
        "capture_business_date": context.business_date,
        "feature_date": context.feature_date,
        "exporter_version": context.exporter_version,
        "vintage_disclaimer": _VINTAGE_DISCLAIMER,
    }


def native_cache_prewarm_authority(
    context: Any,
    *,
    storage_root_identity: str,
    publisher_scheme_id: str,
    historical_predict_date: str,
) -> dict[str, Any]:
    """构造封存 artifact 与唯一 publisher 共同绑定的预热 authority。"""
    if publisher_scheme_id != SIGNAL_GAP_CACHE_PREWARM_PUBLISHER_SCHEME_ID:
        raise ValueError("signal-gap cache prewarm publisher is invalid")
    if (
        not isinstance(historical_predict_date, str)
        or not historical_predict_date.strip()
    ):
        raise ValueError(
            "signal-gap cache prewarm historical predict_date is invalid"
        )
    return {
        "authority_type": _PREWARM_AUTHORITY_TYPE,
        "purpose": _PREWARM_PURPOSE,
        "publisher_scheme_id": publisher_scheme_id,
        "historical_predict_date": historical_predict_date,
        "artifact": native_artifact_registration_authority(
            context,
            storage_root_identity=storage_root_identity,
        ),
    }


def signal_gap_native_cache_root(
    *,
    storage_root: Path,
    generation_id: str,
) -> Path:
    """从专项 artifact root 与 generation 唯一派生 cache root。"""
    if not _NATIVE_GENERATION_ID_PATTERN.fullmatch(str(generation_id)):
        raise ValueError("signal-gap Native cache generation_id is invalid")
    root, _ = resolve_signal_gap_native_storage_root(storage_root)
    return root / ".phase-a-cache" / str(generation_id)


def describe_signal_gap_native_cache_prewarm(
    *,
    manifest: Path,
    historical_predict_date: str,
    publisher_scheme_id: str,
    storage_root: Path,
    engine_factory: Callable[[], Any] = create_engine_from_env,
) -> dict[str, Any]:
    """只读生成预热 token 所需的完整 authority 与派生 cache root。"""
    context, storage_root_identity = open_signal_gap_native_artifact(
        manifest,
        storage_root=storage_root,
    )
    try:
        _require_exact_sealed_native_generation(
            context,
            engine_factory=engine_factory,
        )
    except Exception as exc:  # noqa: BLE001
        raise SignalGapNativeCachePrewarmError(
            "signal-gap Native cache prewarm requires an exact "
            "REGISTERED_SEALED artifact",
            failure_code="ARTIFACT_NOT_REGISTERED_SEALED",
            token_consumed=False,
            audit_path=None,
        ) from exc
    _require_approved_prewarm_publisher(publisher_scheme_id)
    authority = native_cache_prewarm_authority(
        context,
        storage_root_identity=storage_root_identity,
        publisher_scheme_id=publisher_scheme_id,
        historical_predict_date=historical_predict_date,
    )
    return {
        "status": "PREWARM_AUTHORITY_READY",
        "source_authority": authority,
        "cache_root": str(
            signal_gap_native_cache_root(
                storage_root=storage_root,
                generation_id=context.generation_id,
            )
        ),
    }


def prepare_signal_gap_native_artifact(
    *,
    capture_business_date: str,
    feature_date: str,
    output_root: Path,
    engine_factory: Callable[[], Any] = create_engine_from_env,
) -> dict[str, Any]:
    """从当前只读一致性快照准备一个未登记 artifact。"""
    normalized_output_root, storage_root_identity = (
        resolve_signal_gap_native_storage_root(output_root)
    )
    engine = engine_factory()
    try:
        context = create_signal_gap_native_artifact(
            engine,
            capture_business_date=capture_business_date,
            feature_date=feature_date,
            output_root=normalized_output_root,
        )
        return {
            "status": "PREPARED_NOT_REGISTERED",
            "source_authority":
                native_artifact_registration_authority(
                    context,
                    storage_root_identity=storage_root_identity,
                ),
            "manifest_uri": str(context.manifest_path),
        }
    finally:
        if hasattr(engine, "dispose"):
            engine.dispose()


def register_signal_gap_native_artifact(
    *,
    manifest: Path,
    historical_predict_date: str,
    authorize: str,
    storage_root: Path,
    engine_factory: Callable[[], Any] = create_engine_from_env,
) -> dict[str, Any]:
    """消费专项 HMAC 后登记 SEALED authority，不创建 ledger 对象。"""
    context, storage_root_identity = open_signal_gap_native_artifact(
        manifest,
        storage_root=storage_root,
    )
    authority = native_artifact_registration_authority(
        context,
        storage_root_identity=storage_root_identity,
    )
    replay_store = used_tokens_path(PROJECT_ROOT)
    auth, errors = (
        verify_signal_gap_native_artifact_register_authorization(
            authorize,
            historical_predict_date=historical_predict_date,
            source_authority=authority,
            used_store_path=replay_store,
        )
    )
    if errors or auth is None:
        raise RuntimeError(
            "signal-gap Native artifact authorization failed: "
            + "; ".join(errors)
        )
    audit_dir = (
        PROJECT_ROOT
        / "reports"
        / "harness"
        / "signal-gap-native-artifact"
        / context.generation_id
        / authorization_token_hash(auth)
    )
    audit_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        audit_dir.mkdir()
    except FileExistsError as exc:
        raise RuntimeError(
            "signal-gap Native authorization audit attempt already exists"
        ) from exc
    write_authorization_audit(auth, audit_dir)
    outcome_path = audit_dir / "outcome.json"
    outcome = {
        "status": "REGISTERING",
        "authorization_token_sha256": authorization_token_hash(auth),
        "authorization_consumed": False,
        "database_outcome": "NOT_STARTED",
        "generation_id": context.generation_id,
        "historical_predict_date": historical_predict_date,
        "source_authority": authority,
        "error_type": None,
        "error_message": None,
    }
    _atomic_write_json(outcome_path, outcome)
    try:
        mark_token_used(auth, replay_store)
    except Exception as exc:
        failed = {
            **outcome,
            "status": "AUTHORIZATION_FAILED",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
        _atomic_write_json(outcome_path, failed)
        raise SignalGapNativeArtifactRegistrationError(
            "signal-gap Native authorization consumption failed; "
            f"audit={outcome_path}",
            failure_code="AUTHORIZATION_CONSUMPTION_FAILED",
            token_consumed=False,
            audit_path=outcome_path,
        ) from exc
    registering = {
        **outcome,
        "authorization_consumed": True,
    }
    _write_consumed_outcome(
        outcome_path,
        registering,
        failure_code="POST_CONSUMPTION_AUDIT_FAILED",
    )
    engine = None
    try:
        engine = engine_factory()
        generation_id = register_gray_gap_native_artifact(
            engine,
            context,
            historical_predict_date=historical_predict_date,
            storage_root=storage_root,
        )
    except Exception as exc:
        sealed_after_error = _readback_exact_sealed_generation(
            engine,
            context,
        )
        if sealed_after_error:
            sealed_warning = {
                **registering,
                "status": "SEALED_AFTER_ERROR",
                "database_outcome": "SEALED",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }
            _persist_consumed_outcome_and_dispose(
                outcome_path,
                sealed_warning,
                engine=engine,
                failure_code="DATABASE_SEALED_AUDIT_FAILED",
            )
            return {
                "status": "REGISTERED_SEALED_AFTER_ERROR",
                "generation_id": context.generation_id,
                "source_authority": authority,
                "authorization_audit_path": str(outcome_path),
                "registration_warning":
                    f"{type(exc).__name__}: {exc}",
            }
        failed = {
            **registering,
            "status": "DB_FAILED",
            "database_outcome": "FAILED",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
        _persist_consumed_outcome_and_dispose(
            outcome_path,
            failed,
            engine=engine,
            failure_code="DATABASE_FAILURE_AUDIT_FAILED",
        )
        raise SignalGapNativeArtifactRegistrationError(
            "signal-gap Native database registration failed after token "
            f"consumption; audit={outcome_path}; "
            f"cause={type(exc).__name__}: {exc}",
            failure_code="DATABASE_REGISTRATION_FAILED",
            token_consumed=True,
            audit_path=outcome_path,
        ) from exc
    registered = {
        **registering,
        "status": "REGISTERED",
        "database_outcome": "SEALED",
    }
    _persist_consumed_outcome_and_dispose(
        outcome_path,
        registered,
        engine=engine,
        failure_code="DATABASE_SEALED_AUDIT_FAILED",
    )
    return {
        "status": "REGISTERED_SEALED",
        "generation_id": generation_id,
        "source_authority": authority,
        "authorization_audit_path": str(outcome_path),
    }


def prewarm_signal_gap_native_cache(
    *,
    manifest: Path,
    historical_predict_date: str,
    publisher_scheme_id: str,
    authorize: str,
    storage_root: Path,
    algo_env: str = "forecast_env",
    timeout_sec: int = 600,
    engine_factory: Callable[[], Any] = create_engine_from_env,
) -> dict[str, Any]:
    """只用已封存 Native 输入为批准 publisher 发布专用 Phase-A cache。"""
    context, storage_root_identity = open_signal_gap_native_artifact(
        manifest,
        storage_root=storage_root,
    )
    try:
        _require_exact_sealed_native_generation(
            context,
            engine_factory=engine_factory,
        )
    except Exception as exc:  # noqa: BLE001
        raise SignalGapNativeCachePrewarmError(
            "signal-gap Native cache prewarm requires an exact "
            "REGISTERED_SEALED artifact",
            failure_code="ARTIFACT_NOT_REGISTERED_SEALED",
            token_consumed=False,
            audit_path=None,
        ) from exc
    try:
        _require_approved_prewarm_publisher(publisher_scheme_id)
    except ValueError as exc:
        raise SignalGapNativeCachePrewarmError(
            "signal-gap Native cache prewarm publisher is not approved",
            failure_code="PUBLISHER_NOT_APPROVED",
            token_consumed=False,
            audit_path=None,
        ) from exc
    authority = native_cache_prewarm_authority(
        context,
        storage_root_identity=storage_root_identity,
        publisher_scheme_id=publisher_scheme_id,
        historical_predict_date=historical_predict_date,
    )
    replay_store = used_tokens_path(PROJECT_ROOT)
    auth, errors = verify_signal_gap_native_cache_prewarm_authorization(
        authorize,
        historical_predict_date=historical_predict_date,
        source_authority=authority,
        used_store_path=replay_store,
    )
    if errors or auth is None:
        raise SignalGapNativeCachePrewarmError(
            "signal-gap Native cache prewarm authorization failed: "
            + "; ".join(errors),
            failure_code="AUTHORIZATION_INVALID",
            token_consumed=False,
            audit_path=None,
        )
    try:
        cfg = load_scheme_config(
            PROJECT_ROOT
            / "schemes"
            / publisher_scheme_id
            / "config.yaml"
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise SignalGapNativeCachePrewarmError(
            "signal-gap Native cache prewarm publisher discovery failed",
            failure_code="PUBLISHER_DISCOVERY_FAILED",
            token_consumed=False,
            audit_path=None,
        ) from exc
    if (
        cfg.scheme_id != publisher_scheme_id
        or cfg.runtime_type != "native_adapter"
    ):
        raise SignalGapNativeCachePrewarmError(
            "signal-gap Native cache prewarm publisher identity drift",
            failure_code="PUBLISHER_IDENTITY_DRIFT",
            token_consumed=False,
            audit_path=None,
        )
    cache_root = signal_gap_native_cache_root(
        storage_root=storage_root,
        generation_id=context.generation_id,
    )
    audit_dir = (
        PROJECT_ROOT
        / "reports"
        / "harness"
        / "signal-gap-native-cache-prewarm"
        / context.generation_id
        / authorization_token_hash(auth)
    )
    audit_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        audit_dir.mkdir()
    except FileExistsError as exc:
        raise SignalGapNativeCachePrewarmError(
            "signal-gap Native cache prewarm audit attempt already exists",
            failure_code="AUDIT_ATTEMPT_EXISTS",
            token_consumed=False,
            audit_path=audit_dir,
        ) from exc
    write_authorization_audit(auth, audit_dir)
    outcome_path = audit_dir / "outcome.json"
    outcome = {
        "status": "PREWARMING",
        "authorization_token_sha256": authorization_token_hash(auth),
        "authorization_consumed": False,
        "generation_id": context.generation_id,
        "historical_predict_date": historical_predict_date,
        "publisher_scheme_id": publisher_scheme_id,
        "cache_root": str(cache_root),
        "source_authority": authority,
        "failure_code": None,
        "error_type": None,
        "error_message": None,
    }
    _atomic_write_json(outcome_path, outcome)
    try:
        mark_token_used(auth, replay_store)
    except Exception as exc:  # noqa: BLE001
        _atomic_write_json(
            outcome_path,
            {
                **outcome,
                "status": "AUTHORIZATION_FAILED",
                "failure_code": "AUTHORIZATION_CONSUMPTION_FAILED",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        raise SignalGapNativeCachePrewarmError(
            "signal-gap Native cache prewarm authorization consumption "
            f"failed; audit={outcome_path}",
            failure_code="AUTHORIZATION_CONSUMPTION_FAILED",
            token_consumed=False,
            audit_path=outcome_path,
        ) from exc
    consumed = {**outcome, "authorization_consumed": True}
    _atomic_write_json(outcome_path, consumed)
    try:
        if auth.expires_at is None:
            raise RuntimeError("prewarm authorization expiry is missing")
        permit = create_signal_gap_cache_prewarm_permit(
            artifact_root=Path(context.manifest_path).parent.parent,
            cache_root=cache_root,
            publisher_scheme_id=publisher_scheme_id,
            native_generation=_native_cache_binding(context),
            expires_at=auth.expires_at,
            authorization_token_sha256=authorization_token_hash(auth),
        )
    except Exception as exc:  # noqa: BLE001
        _atomic_write_json(
            outcome_path,
            {
                **consumed,
                "status": "PREWARM_PERMIT_FAILED",
                "failure_code": "PREWARM_PERMIT_ISSUE_FAILED",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        raise SignalGapNativeCachePrewarmError(
            "signal-gap Native cache prewarm permit issuance failed; "
            f"audit={outcome_path}; cause={type(exc).__name__}: {exc}",
            failure_code="PREWARM_PERMIT_ISSUE_FAILED",
            token_consumed=True,
            audit_path=outcome_path,
        ) from exc
    prewarm_started = {
        **consumed,
        "permit_path": str(permit.path),
        "permit_expires_at": permit.expires_at,
    }
    _atomic_write_json(outcome_path, prewarm_started)
    try:
        records = run_configured_scheme(
            cfg,
            historical_predict_date,
            engine=None,
            algo_env=algo_env,
            timeout_sec=timeout_sec,
            native_generation=context,
            native_execution_mode=(
                NATIVE_EXECUTION_MODE_SIGNAL_GAP_CACHE_PREWARM
            ),
            expected_native_feature_date=context.feature_date,
            phase_a_cache_root=cache_root,
            phase_a_cache_prewarm_permit=permit.path,
            phase_a_cache_prewarm_capability=permit.capability,
        )
        cache_audit = _validate_prewarm_records(
            records,
            publisher_scheme_id=publisher_scheme_id,
            context=context,
        )
    except Exception as exc:  # noqa: BLE001
        _atomic_write_json(
            outcome_path,
            {
                **prewarm_started,
                "status": "PUBLISH_FAILED",
                "failure_code": "CACHE_PUBLISH_FAILED",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        raise SignalGapNativeCachePrewarmError(
            "signal-gap Native cache prewarm publication failed; "
            f"audit={outcome_path}; cause={type(exc).__name__}: {exc}",
            failure_code="CACHE_PUBLISH_FAILED",
            token_consumed=True,
            audit_path=outcome_path,
        ) from exc
    _atomic_write_json(
        outcome_path,
        {
            **prewarm_started,
            "status": "PREWARMED_PUBLISHED",
            "cache": cache_audit,
        },
    )
    return {
        "status": "PREWARMED_PUBLISHED",
        "generation_id": context.generation_id,
        "cache_root": str(cache_root),
        "cache": cache_audit,
        "authorization_audit_path": str(outcome_path),
    }


def _validate_prewarm_records(
    records: Any,
    *,
    publisher_scheme_id: str,
    context: Any,
) -> dict[str, Any]:
    """只接受 publisher 返回的一个、已发布且绑定同 artifact 的 cache 审计。"""
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError(
            "signal-gap Native cache prewarm publisher must return one record"
        )
    record = records[0]
    if getattr(record, "scheme_id", None) != publisher_scheme_id:
        raise ValueError(
            "signal-gap Native cache prewarm publisher record identity drift"
        )
    extra = getattr(record, "extra", None)
    if not isinstance(extra, Mapping):
        raise ValueError(
            "signal-gap Native cache prewarm publisher cache audit is missing"
        )
    cache_audit = extra.get("phase_a_cache")
    if not isinstance(cache_audit, Mapping):
        raise ValueError(
            "signal-gap Native cache prewarm publisher cache audit is missing"
        )
    acceptance = cache_audit.get("generation_acceptance")
    expected_binding = _native_cache_binding(context)
    if (
        cache_audit.get("published") is not True
        or not isinstance(acceptance, Mapping)
        or acceptance.get("status") != "ACCEPTED"
        or acceptance.get("native_generation") != expected_binding
    ):
        raise ValueError(
            "signal-gap Native cache prewarm publisher did not publish "
            "the sealed cache generation"
        )
    return dict(cache_audit)


def _require_approved_prewarm_publisher(
    publisher_scheme_id: str,
) -> None:
    if publisher_scheme_id != SIGNAL_GAP_CACHE_PREWARM_PUBLISHER_SCHEME_ID:
        raise ValueError("signal-gap Native cache prewarm publisher is invalid")


def _native_cache_binding(context: Any) -> dict[str, str]:
    """从封存 Native context 提取 cache permit 与审计共用的精确绑定。"""
    return {
        "generation_id": context.generation_id,
        "manifest_sha256": context.manifest_sha256,
        "dataset_content_id": context.dataset_content_id,
        "business_date": context.business_date,
        "feature_date": context.feature_date,
        "schema_version": context.schema_version,
        "exporter_version": context.exporter_version,
    }


def _require_exact_sealed_native_generation(
    context: Any,
    *,
    engine_factory: Callable[[], Any],
) -> None:
    """只读重验登记的 SEALED fence 与封存 artifact 完全一致。"""
    engine = engine_factory()
    try:
        row = read_sealed_input_generation(
            engine,
            generation_id=context.generation_id,
            expected_generation_type="native_source",
        )
        if not _sealed_generation_matches_context(row, context):
            raise RuntimeError(
                "sealed Native generation differs from artifact"
            )
    finally:
        if hasattr(engine, "dispose"):
            engine.dispose()


def _sealed_generation_matches_context(
    row: Any,
    context: Any,
) -> bool:
    expected = {
        "generation_id": context.generation_id,
        "generation_type": context.generation_type,
        "business_date": context.business_date,
        "feature_date": context.feature_date,
        "readiness_basis": context.readiness_basis,
        "source_commit_token": context.source_commit_token,
        "dataset_content_id": context.dataset_content_id,
        "schema_version": context.schema_version,
        "exporter_version": context.exporter_version,
        "manifest_uri": str(context.manifest_path),
        "manifest_sha256": context.manifest_sha256,
        "native_generation_id": None,
        "native_manifest_sha256": None,
        "state": "SEALED",
        "invalidated_at": None,
        "invalid_reason": None,
    }
    observed = {
        field: getattr(row, field, None)
        for field in expected
    }
    return observed == expected and getattr(row, "sealed_at", None) is not None


def _readback_exact_sealed_generation(
    engine: Any,
    context: Any,
) -> bool:
    """登记异常后只以完整只读 SEALED identity 判定是否已提交。"""
    if engine is None:
        return False
    try:
        row = read_sealed_input_generation(
            engine,
            generation_id=context.generation_id,
            expected_generation_type="native_source",
        )
        return _sealed_generation_matches_context(row, context)
    except Exception:
        return False


def _write_consumed_outcome(
    outcome_path: Path,
    outcome: dict[str, Any],
    *,
    failure_code: str,
) -> None:
    """授权消费后审计失败也必须保留 consumed=true 的错误语义。"""
    try:
        _atomic_write_json(outcome_path, outcome)
    except Exception as exc:
        raise SignalGapNativeArtifactRegistrationError(
            "signal-gap Native audit write failed after token "
            f"consumption; audit={outcome_path}; "
            f"database_outcome={outcome['database_outcome']}; "
            f"cause={type(exc).__name__}: {exc}",
            failure_code=failure_code,
            token_consumed=True,
            audit_path=outcome_path,
        ) from exc


def _dispose_after_consumption(
    engine: Any,
    *,
    outcome_path: Path,
    outcome: dict[str, Any],
) -> None:
    if engine is None or not hasattr(engine, "dispose"):
        return
    try:
        engine.dispose()
    except Exception as exc:
        raise SignalGapNativeArtifactRegistrationError(
            "signal-gap Native engine disposal failed after token "
            f"consumption; audit={outcome_path}; "
            f"database_outcome={outcome['database_outcome']}; "
            f"cause={type(exc).__name__}: {exc}",
            failure_code=(
                "DATABASE_SEALED_POSTPROCESS_FAILED"
                if outcome["database_outcome"] == "SEALED"
                else "DATABASE_FAILURE_POSTPROCESS_FAILED"
            ),
            token_consumed=True,
            audit_path=outcome_path,
        ) from exc


def _persist_consumed_outcome_and_dispose(
    outcome_path: Path,
    outcome: dict[str, Any],
    *,
    engine: Any,
    failure_code: str,
) -> None:
    """审计与 dispose 都尝试；双失败时保留更具体的审计异常。"""
    primary_error: SignalGapNativeArtifactRegistrationError | None = None
    try:
        _write_consumed_outcome(
            outcome_path,
            outcome,
            failure_code=failure_code,
        )
    except SignalGapNativeArtifactRegistrationError as exc:
        primary_error = exc
    try:
        _dispose_after_consumption(
            engine,
            outcome_path=outcome_path,
            outcome=outcome,
        )
    except SignalGapNativeArtifactRegistrationError as exc:
        if primary_error is None:
            primary_error = exc
        elif hasattr(primary_error, "add_note"):
            primary_error.add_note(
                "engine disposal also failed: " + str(exc)
            )
    if primary_error is not None:
        raise primary_error
