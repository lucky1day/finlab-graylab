from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from harness.authorization import (
    _atomic_write_json,
    authorization_token_hash,
    mark_token_used,
    used_tokens_path,
    verify_signal_gap_native_artifact_register_authorization,
    write_authorization_audit,
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


_PURPOSE = "signal_gap_gray_live_current_snapshot"
_VINTAGE_DISCLAIMER = (
    "current_snapshot_as_of_not_historical_vintage"
)
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
        }
        observed = {
            field: getattr(row, field)
            for field in expected
        }
        return observed == expected and row.sealed_at is not None
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
