"""登记历史补齐仍需要的独立 Native input generation。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import Engine

from scheduler.repository import (
    register_sealed_archived_native_generation,
    register_sealed_gray_gap_native_generation,
)
from shared.native_input_generation import (
    NATIVE_GENERATION_EXPORTER_VERSION,
    SIGNAL_GAP_NATIVE_EXPORTER_VERSION,
    NativeGenerationContext,
    open_native_generation,
    open_signal_gap_native_artifact,
)


def register_archived_native_artifact(
    engine: Engine,
    context: NativeGenerationContext,
    *,
    historical_predict_date: str,
    lock_timeout_sec: float = 5.0,
) -> str:
    """重验当日历史 generation 后独立登记，不绑定 occurrence。"""
    if not isinstance(context, NativeGenerationContext):
        raise TypeError("context must be a NativeGenerationContext")
    verified = open_native_generation(
        context.manifest_path,
        expected_generation_id=context.generation_id,
        expected_manifest_sha256=context.manifest_sha256,
        expected_business_date=context.business_date,
        expected_feature_date=context.feature_date,
    )
    if verified.exporter_version != NATIVE_GENERATION_EXPORTER_VERSION:
        raise ValueError(
            "archived Native artifact exporter_version is invalid"
        )
    return register_sealed_archived_native_generation(
        engine,
        generation_id=verified.generation_id,
        generation_type=verified.generation_type,
        business_date=verified.business_date,
        feature_date=verified.feature_date,
        readiness_basis=verified.readiness_basis,
        source_commit_token=verified.source_commit_token,
        dataset_content_id=verified.dataset_content_id,
        schema_version=verified.schema_version,
        exporter_version=verified.exporter_version,
        manifest_uri=str(verified.manifest_path),
        manifest_sha256=verified.manifest_sha256,
        historical_predict_date=historical_predict_date,
        lock_timeout_sec=lock_timeout_sec,
    )


def register_gray_gap_native_artifact(
    engine: Engine,
    context: NativeGenerationContext,
    *,
    historical_predict_date: str,
    storage_root: str | Path,
    lock_timeout_sec: float = 5.0,
) -> str:
    """重验 current-snapshot artifact 后独立登记，不绑定 occurrence。"""
    if not isinstance(context, NativeGenerationContext):
        raise TypeError("context must be a NativeGenerationContext")
    verified, _root_identity = open_signal_gap_native_artifact(
        context.manifest_path,
        storage_root=storage_root,
        expected_generation_id=context.generation_id,
        expected_manifest_sha256=context.manifest_sha256,
        expected_business_date=context.business_date,
        expected_feature_date=context.feature_date,
    )
    if verified.exporter_version != SIGNAL_GAP_NATIVE_EXPORTER_VERSION:
        raise ValueError(
            "gray-gap Native artifact exporter_version is invalid"
        )
    return register_sealed_gray_gap_native_generation(
        engine,
        generation_id=verified.generation_id,
        generation_type=verified.generation_type,
        business_date=verified.business_date,
        feature_date=verified.feature_date,
        readiness_basis=verified.readiness_basis,
        source_commit_token=verified.source_commit_token,
        dataset_content_id=verified.dataset_content_id,
        schema_version=verified.schema_version,
        exporter_version=verified.exporter_version,
        manifest_uri=str(verified.manifest_path),
        manifest_sha256=verified.manifest_sha256,
        historical_predict_date=historical_predict_date,
        lock_timeout_sec=lock_timeout_sec,
    )
