"""将本机不可变输入 generation 登记到日批 ledger。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import Engine

from scheduler.repository import (
    read_sealed_input_generation,
    register_sealed_archived_native_generation,
    register_sealed_gray_gap_native_generation,
    register_seal_and_bind_schedule_occurrence_generation,
)
from shared.databridge_input_generation import (
    DataBridgeGenerationContext,
    open_databridge_generation,
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


def register_native_generation(
    engine: Engine,
    context: NativeGenerationContext,
    *,
    occurrence_id: int | None = None,
) -> str:
    """重新 rehash Native manifest 后创建并封存 ledger generation。"""
    if not isinstance(context, NativeGenerationContext):
        raise TypeError("context must be a NativeGenerationContext")
    normalized_occurrence_id = _required_occurrence_id(occurrence_id)
    verified = open_native_generation(
        context.manifest_path,
        expected_generation_id=context.generation_id,
        expected_manifest_sha256=context.manifest_sha256,
        expected_business_date=context.business_date,
        expected_feature_date=context.feature_date,
    )
    if (
        verified.exporter_version
        == SIGNAL_GAP_NATIVE_EXPORTER_VERSION
    ):
        raise ValueError(
            "daily ledger Native registration rejects the signal-gap "
            "special exporter_version"
        )
    return _register(
        engine,
        verified,
        occurrence_id=normalized_occurrence_id,
    )


def register_databridge_generation(
    engine: Engine,
    context: DataBridgeGenerationContext,
    *,
    schema_path: str | Path,
    occurrence_id: int | None = None,
) -> str:
    """重新 rehash DataBridge manifest 后创建并封存 ledger generation。"""
    if not isinstance(context, DataBridgeGenerationContext):
        raise TypeError("context must be a DataBridgeGenerationContext")
    normalized_occurrence_id = _required_occurrence_id(occurrence_id)
    verified = open_databridge_generation(
        context.manifest_path,
        expected_generation_id=context.generation_id,
        expected_manifest_sha256=context.manifest_sha256,
        expected_business_date=context.business_date,
        expected_feature_date=context.feature_date,
        schema_path=schema_path,
    )
    parent_fence = read_sealed_input_generation(
        engine,
        generation_id=verified.native_generation_id,
        expected_generation_type="native_source",
    )
    parent_manifest = Path(parent_fence.manifest_uri)
    if not parent_manifest.is_absolute():
        raise RuntimeError(
            "Native parent DB fence manifest_uri must be absolute"
        )
    verified_parent = open_native_generation(
        parent_manifest,
        expected_generation_id=parent_fence.generation_id,
        expected_manifest_sha256=parent_fence.manifest_sha256,
        expected_business_date=parent_fence.business_date,
        expected_feature_date=parent_fence.feature_date,
    )
    if (
        verified.native_generation_id != verified_parent.generation_id
        or verified.native_manifest_sha256
        != verified_parent.manifest_sha256
        or verified.business_date != verified_parent.business_date
        or verified.feature_date != verified_parent.feature_date
    ):
        raise RuntimeError(
            "DataBridge generation Native parent differs from "
            "rehashed DB fence"
        )
    return _register(
        engine,
        verified,
        occurrence_id=normalized_occurrence_id,
    )


def _register(
    engine: Engine,
    context: NativeGenerationContext | DataBridgeGenerationContext,
    *,
    occurrence_id: int,
) -> str:
    manifest_path = context.manifest_path
    if not manifest_path.is_absolute():
        raise ValueError("generation manifest_uri must be an absolute path")
    native_relation: dict[str, str] = {}
    if isinstance(context, DataBridgeGenerationContext):
        native_relation = {
            "native_generation_id": context.native_generation_id,
            "native_manifest_sha256":
                context.native_manifest_sha256,
        }
    registration = {
        "generation_id": context.generation_id,
        "generation_type": context.generation_type,
        "business_date": context.business_date,
        "feature_date": context.feature_date,
        "readiness_basis": context.readiness_basis,
        "source_commit_token": context.source_commit_token,
        "dataset_content_id": context.dataset_content_id,
        "schema_version": context.schema_version,
        "exporter_version": context.exporter_version,
        "manifest_uri": str(manifest_path),
        "manifest_sha256": context.manifest_sha256,
        **native_relation,
    }
    generation_id, _bound_count = (
        register_seal_and_bind_schedule_occurrence_generation(
            engine,
            occurrence_id=occurrence_id,
            expected_feature_date=context.feature_date,
            **registration,
        )
    )
    return generation_id


def _required_occurrence_id(value: object) -> int:
    if value is None:
        raise ValueError(
            "occurrence_id is required; standalone generation sealing "
            "is disabled"
        )
    if isinstance(value, bool):
        raise ValueError("occurrence_id must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "occurrence_id must be a positive integer"
        ) from exc
    if normalized <= 0:
        raise ValueError("occurrence_id must be a positive integer")
    return normalized
