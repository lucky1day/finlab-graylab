from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from harness.authorization import (
    mark_token_used,
    used_tokens_path,
    verify_signal_gap_native_artifact_register_authorization,
    write_authorization_audit,
)
from scheduler.generation_registry import (
    register_gray_gap_native_artifact,
)
from scheduler.repository import create_engine_from_env
from shared.native_input_generation import (
    SIGNAL_GAP_NATIVE_EXPORTER_VERSION,
    create_signal_gap_native_artifact,
    open_native_generation,
)


_PURPOSE = "signal_gap_gray_live_current_snapshot"
_VINTAGE_DISCLAIMER = (
    "current_snapshot_as_of_not_historical_vintage"
)


def native_artifact_registration_authority(context: Any) -> dict[str, str]:
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
        "manifest_sha256": context.manifest_sha256,
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
    normalized_output_root = Path(output_root)
    if not normalized_output_root.is_absolute():
        raise ValueError(
            "signal-gap Native artifact output_root must be absolute"
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
                native_artifact_registration_authority(context),
            "manifest_uri": str(context.manifest_path.resolve()),
        }
    finally:
        if hasattr(engine, "dispose"):
            engine.dispose()


def register_signal_gap_native_artifact(
    *,
    manifest: Path,
    historical_predict_date: str,
    authorize: str,
    project_root: Path,
    engine_factory: Callable[[], Any] = create_engine_from_env,
) -> dict[str, Any]:
    """消费专项 HMAC 后登记 SEALED authority，不创建 ledger 对象。"""
    manifest_path = Path(manifest)
    if not manifest_path.is_absolute():
        raise ValueError(
            "signal-gap Native artifact manifest must be absolute"
        )
    context = open_native_generation(manifest_path)
    authority = native_artifact_registration_authority(context)
    replay_store = used_tokens_path(Path(project_root).resolve())
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
        Path(project_root).resolve()
        / "reports"
        / "harness"
        / "signal-gap-native-artifact"
        / context.generation_id
    )
    mark_token_used(auth, replay_store)
    write_authorization_audit(auth, audit_dir)
    engine = engine_factory()
    try:
        generation_id = register_gray_gap_native_artifact(
            engine,
            context,
            historical_predict_date=historical_predict_date,
        )
    finally:
        if hasattr(engine, "dispose"):
            engine.dispose()
    return {
        "status": "REGISTERED_SEALED",
        "generation_id": generation_id,
        "source_authority": authority,
    }
