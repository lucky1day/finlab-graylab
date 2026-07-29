from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import pickle
import platform
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import fcntl

import numpy as np
import pandas as pd

from shared.artifact_paths import BACKTEST_ARTIFACT_ROOT, safe_path_part
from shared.input_artifacts import (
    NATIVE_BUSINESS_DATE_ENV,
    NATIVE_FEATURE_DATE_ENV,
    NATIVE_GENERATION_ID_ENV,
    NATIVE_MANIFEST_PATH_ENV,
    NATIVE_MANIFEST_SHA256_ENV,
)
from shared.liwei_0616_cache_contract import (
    APPROVED_PHASE_A_CACHE_PUBLISHERS,
    CACHE_USE_QUALIFICATION_ENV,
    GENERATION_ACCEPTANCE_SCHEMA_VERSION,
    PHASE_A_CACHE_ABI_VERSION,
    trusted_qualification_audit_binding,
    validate_generation_acceptance_record,
    validate_trusted_cache_use_qualification,
)
from shared.liwei_0616_cache_projection import (
    PROJECTION_SCHEMA_VERSION,
    AuxiliaryDependencyProjection,
)
from shared.native_input_generation import (
    NATIVE_GENERATION_EXPORTER_VERSION,
    NATIVE_GENERATION_SCHEMA_VERSION,
)


CACHE_SCHEMA_VERSION = 2
LEGACY_CACHE_SCHEMA_VERSION = 1
GENERATION_MANIFEST_SCHEMA_VERSION = 3
CURRENT_POINTER_SCHEMA_VERSION = 1
LEGACY_INPUT_GENERATION_STATE_SCHEMA_VERSION = 2
INPUT_GENERATION_STATE_SCHEMA_VERSION = 3
DEFAULT_CACHE_ROOT = BACKTEST_ARTIFACT_ROOT / "runtime_cache" / "liwei_0616"
CACHE_GENERATION_RETENTION = 3
MAX_CACHE_FAMILY_BYTES = 512 * 1024 * 1024
GLOBAL_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
COMPARE_GATE_EVIDENCE_VERSION = "phase-a-compare-gate-evidence-v2"
FULL_COMPARE_QUALIFICATION_VERSION = (
    "liwei-0616-full-output-compare-qualification-v1"
)
COMPARE_QUALIFICATION_BINDING_VERSION = (
    "liwei-0616-compare-qualification-binding-v1"
)
FULL_COMPARE_FIELDS = (
    "direction",
    "vote_score",
    "baseline_score",
    "baseline_sign",
    "probability",
    "confidence",
    "internal_fields",
    "phase_a_cache",
)
DAILY_COORDINATOR_MODE_ENV = "BOND_DAILY_COORDINATOR_MODE"


class CacheCapacityError(RuntimeError):
    """cache generation 会突破 family 或全局磁盘安全边界。"""


def verify_phase_a_cache_audit_files(
    extra: Mapping[str, object],
    *,
    expected_qualification: Mapping[str, object],
) -> Mapping[str, object]:
    """重开 cache generation/parent 并复核逐 consumer acceptance 谱系。"""
    audit = extra.get("phase_a_cache")
    if not isinstance(audit, Mapping):
        raise ValueError("PredictionRecord.extra.phase_a_cache is missing")
    generation_id = str(audit.get("generation_id") or "")
    manifest_sha256 = _require_cache_sha256(
        audit.get("generation_manifest_sha256"),
        "generation_manifest_sha256",
    )
    generation_path = Path(str(audit.get("generation_path") or ""))
    if not generation_path.is_absolute():
        raise ValueError("cache generation_path must be absolute")
    trusted = validate_trusted_cache_use_qualification(
        expected_qualification
    )
    qualification = trusted["qualification"]
    expected_family_root = generation_path.parent.parent
    if (
        generation_path.name != generation_id
        or generation_path.parent.name != "generations"
        or expected_family_root.name
        != safe_path_part(str(qualification["tenor"]).lower())
        or expected_family_root.parent.name
        != safe_path_part(str(qualification["cache_family"]))
        or audit.get("family_root") != str(expected_family_root)
        or audit.get("current_pointer")
        != str(expected_family_root / "current.json")
    ):
        raise ValueError(
            "cache audit generation path is not canonical"
        )
    generation = _load_generation_directory(
        generation_path,
        expected_generation_id=generation_id,
        expected_manifest_sha256=manifest_sha256,
        secure=True,
    )
    if audit.get("capacity_eligible") is not True:
        raise ValueError("cache audit is not capacity eligible")
    if audit.get(
        "cache_use_qualification"
    ) != trusted_qualification_audit_binding(trusted):
        raise ValueError(
            "cache audit qualification identity mismatch"
        )
    _verify_generation_acceptance_lineage(
        generation,
        trusted_qualification=trusted,
    )
    reported = audit.get("generation_acceptance")
    actual = generation.manifest.get(
        "generation_acceptance_evidence"
    )
    if (
        not isinstance(reported, Mapping)
        or _canonical_json(reported) != _canonical_json(actual)
    ):
        raise ValueError(
            "reported generation acceptance differs from cache manifest"
        )
    return dict(actual)


@dataclass(frozen=True)
class PhaseACacheSpec:
    """liwei_0616 一组可共享 baseline cache 的固定身份。"""

    cache_family: str
    tenor: str
    publisher_consumer_id: str
    baselines: tuple[str, ...]
    baseline_configs: Mapping[str, Mapping[str, Any]]
    source_ic_screen_start: str
    horizon: int
    purge_gap: int
    daily_dependency_lookback_rows: int | None = None
    daily_dependency_proof: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.publisher_consumer_id, str)
            or not self.publisher_consumer_id.strip()
        ):
            raise ValueError(
                "publisher_consumer_id must be a non-empty string"
            )


@dataclass(frozen=True)
class _LoadedGeneration:
    generation_id: str
    path: Path
    manifest: dict[str, Any]
    manifest_sha256: str
    caches: dict[str, dict[str, Any]]


def runtime_compare_gate_callbacks(
    *,
    train_phase_a: Callable[
        [str, tuple[tuple[str, str], ...]],
        Mapping[str, Any],
    ],
    run_full_output: (
        Callable[
            [Mapping[str, Mapping[str, Any]] | None],
            Any,
        ]
        | None
    ),
) -> tuple[
    Callable[
        [str, tuple[tuple[str, str], ...]],
        Mapping[str, Any],
    ]
    | None,
    Callable[
        [Mapping[str, Mapping[str, Any]]],
        tuple[Any, Any],
    ]
    | None,
]:
    """日批不在 cache 层重复训练或重复 full output。

    生产资格来自双签名 capacity corpus；本次 ``train_phase_a`` 的单次
    产物是 generation acceptance 的 authoritative affected scope。
    """
    del train_phase_a, run_full_output
    return None, None


def prepare_phase_a_caches(
    *,
    spec: PhaseACacheSpec,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    test_ranges: tuple[tuple[str, str], ...],
    train_missing: Callable[[str, tuple[tuple[str, str], ...]], Mapping[str, Any]],
    compare_cold: (
        Callable[
            [str, tuple[tuple[str, str], ...]],
            Mapping[str, Any],
        ]
        | None
    ) = None,
    qualify_compare_gate: (
        Callable[
            [Mapping[str, Any]],
            Mapping[str, Any],
        ]
        | None
    ) = None,
    compare_full_output: (
        Callable[
            [Mapping[str, Mapping[str, Any]]],
            tuple[Any, Any],
        ]
        | None
    ) = None,
    require_compare_gate: bool | None = None,
    cache_use_qualification: Mapping[str, object] | None = None,
    cache_consumer_id: str | None = None,
    native_generation: Mapping[str, object] | None = None,
    auxiliary_dependency_projection: (
        AuxiliaryDependencyProjection | None
    ) = None,
    cache_root: str | Path | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """读取或生成不可变 Phase A cache generation。

    同一 ``cache_family + tenor`` 只有一个 prewarmer。只有完整 generation
    写入、校验且通过容量门禁后，才会原子替换 ``current.json``。历史修订
    仅在 spec 同时声明可审计的 daily 依赖窗口与证明时重算保守 suffix；
    有效辅助投影可证明的 append/suffix 只训练受影响日期；投影缺失或
    证明漂移仍 fail-closed full rebuild。自哈希只用于完整性，
    ``compare_cold`` 只用于非生产诊断。ledger/SLA 路径必须传入 capacity
    corpus 双签名生成的逐 consumer qualification，且禁止在日批内通过
    runtime callback 自行生成资格。
    """
    _validate_cache_publisher_identity(spec)
    _validate_daily_dependency_proof(spec)
    if (
        not isinstance(cache_consumer_id, str)
        or not cache_consumer_id.strip()
    ):
        raise ValueError("cache_consumer_id must be a non-empty string")
    cache_consumer_id = cache_consumer_id.strip()
    if compare_cold is not None and compare_cold is train_missing:
        raise ValueError(
            "compare_cold must be independent from train_missing"
        )
    if qualify_compare_gate is not None and compare_cold is None:
        raise ValueError(
            "qualify_compare_gate requires an independent "
            "compare_cold callback"
        )
    if compare_full_output is not None and compare_cold is None:
        raise ValueError(
            "compare_full_output requires an independent "
            "compare_cold callback"
        )
    if (
        qualify_compare_gate is not None
        and compare_full_output is not None
    ):
        raise ValueError(
            "choose either offline qualify_compare_gate evidence "
            "or runtime compare_full_output"
        )
    qualification_required = _compare_gate_required(
        require_compare_gate
    )
    trusted_qualification = _resolve_cache_use_qualification(
        cache_use_qualification
    )
    native_generation_binding = _resolve_native_generation_binding(
        native_generation
    )
    if qualification_required:
        if trusted_qualification is None:
            raise RuntimeError(
                "signed per-consumer cache use qualification is required "
                "for ledger/SLA cache use"
            )
        if native_generation_binding is None:
            raise RuntimeError(
                "Native generation provenance is required for ledger/SLA "
                "cache use"
            )
        if (
            compare_cold is not None
            or qualify_compare_gate is not None
            or compare_full_output is not None
        ):
            raise RuntimeError(
                "ordinary ledger execution cannot self-sign cache "
                "qualification with runtime compare callbacks"
            )
        _validate_qualification_for_cache_use(
            trusted_qualification,
            cache_consumer_id=cache_consumer_id,
            spec=spec,
            native_generation=native_generation_binding,
        )
    root = _cache_root(cache_root)
    family_root = _family_cache_root(root, spec)
    is_publisher = (
        cache_consumer_id == spec.publisher_consumer_id
    )
    if not is_publisher:
        return _prepare_under_family_lock(
            spec=spec,
            cache_consumer_id=cache_consumer_id,
            is_publisher=False,
            root=root,
            family_root=family_root,
            daily_df=daily_df,
            weekly_df=weekly_df,
            monthly_df=monthly_df,
            auxiliary_dependency_projection=(
                auxiliary_dependency_projection
            ),
            test_ranges=test_ranges,
            train_missing=train_missing,
            compare_cold=compare_cold,
            qualify_compare_gate=qualify_compare_gate,
            compare_full_output=compare_full_output,
            qualification_required=qualification_required,
            trusted_qualification=trusted_qualification,
            native_generation_binding=native_generation_binding,
        )
    family_root.mkdir(parents=True, exist_ok=True)
    if qualification_required:
        _require_secure_directory(root, "cache root")
        _require_secure_directory(family_root, "cache family")
    with _exclusive_lock(family_root / ".prewarmer.lock"):
        if qualification_required:
            _require_secure_directory(family_root, "cache family")
        _cleanup_staging_directories(
            family_root,
            secure=qualification_required,
        )
        return _prepare_under_family_lock(
            spec=spec,
            cache_consumer_id=cache_consumer_id,
            is_publisher=is_publisher,
            root=root,
            family_root=family_root,
            daily_df=daily_df,
            weekly_df=weekly_df,
            monthly_df=monthly_df,
            auxiliary_dependency_projection=(
                auxiliary_dependency_projection
            ),
            test_ranges=test_ranges,
            train_missing=train_missing,
            compare_cold=compare_cold,
            qualify_compare_gate=qualify_compare_gate,
            compare_full_output=compare_full_output,
            qualification_required=qualification_required,
            trusted_qualification=trusted_qualification,
            native_generation_binding=native_generation_binding,
        )


def _prepare_under_family_lock(
    *,
    spec: PhaseACacheSpec,
    cache_consumer_id: str,
    is_publisher: bool,
    root: Path,
    family_root: Path,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    auxiliary_dependency_projection: (
        AuxiliaryDependencyProjection | None
    ),
    test_ranges: tuple[tuple[str, str], ...],
    train_missing: Callable[
        [str, tuple[tuple[str, str], ...]],
        Mapping[str, Any],
    ],
    compare_cold: (
        Callable[
            [str, tuple[tuple[str, str], ...]],
            Mapping[str, Any],
        ]
        | None
    ),
    qualify_compare_gate: (
        Callable[
            [Mapping[str, Any]],
            Mapping[str, Any],
        ]
        | None
    ),
    compare_full_output: (
        Callable[
            [Mapping[str, Mapping[str, Any]]],
            tuple[Any, Any],
        ]
        | None
    ),
    qualification_required: bool,
    trusted_qualification: Mapping[str, object] | None,
    native_generation_binding: Mapping[str, object] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    requested_by_baseline: dict[str, list[str]] = {}
    for baseline in spec.baselines:
        config = spec.baseline_configs.get(baseline)
        if config is None:
            raise KeyError(f"missing baseline config: {baseline}")
        requested_dates = _requested_dates(daily_df, config, test_ranges)
        if not requested_dates:
            raise ValueError(f"baseline {baseline} has no requested test dates")
        requested_by_baseline[baseline] = requested_dates

    spec_fingerprint = _spec_fingerprint(spec)
    input_state = _input_generation_state(
        daily_df=daily_df,
        weekly_df=weekly_df,
        monthly_df=monthly_df,
        auxiliary_dependency_projection=(
            auxiliary_dependency_projection
        ),
        native_generation_binding=native_generation_binding,
    )
    current, current_error = _load_current_generation(
        family_root,
        secure=qualification_required or not is_publisher,
    )
    input_change = _input_change_analysis(
        (
            current.manifest.get("input_state")
            if current is not None
            else None
        ),
        input_state,
    )
    if not is_publisher:
        return _validated_consumer_hit(
            spec=spec,
            cache_consumer_id=cache_consumer_id,
            current=current,
            current_error=current_error,
            requested_by_baseline=requested_by_baseline,
            input_state=input_state,
            input_change=input_change,
            family_root=family_root,
            qualification_required=qualification_required,
            trusted_qualification=trusted_qualification,
            native_generation_binding=native_generation_binding,
        )
    legacy_caches = None
    if (
        current is None
        and current_error == "no_current_generation"
    ):
        legacy_caches = _load_legacy_v1_caches(
            root=root,
            family_root=family_root,
            spec=spec,
            daily_df=daily_df,
            weekly_df=weekly_df,
            monthly_df=monthly_df,
        )
    if (
        current is not None
        and current.manifest.get("spec_fingerprint")
        == spec_fingerprint
        and set(current.caches) == set(spec.baselines)
    ):
        current_watermark = max(
            max(cache["test_dates"])
            for cache in current.caches.values()
        )
        if (
            _max_daily_date(daily_df) < current_watermark
            and _is_safe_truncated_input(
                current.manifest.get("input_state"),
                input_state,
            )
        ):
            if qualification_required:
                _validate_generation_acceptance_for_use(
                    current,
                    native_generation_binding=native_generation_binding,
                )
            truncated_audits: dict[str, dict[str, Any]] = {}
            for baseline in spec.baselines:
                requested = set(requested_by_baseline[baseline])
                available = set(
                    current.caches[baseline]["test_dates"]
                )
                if not requested.issubset(available):
                    raise RuntimeError(
                        "truncated input cannot extend a newer immutable "
                        f"cache generation for baseline {baseline}"
                    )
                truncated_audits[baseline] = {
                    "status": "hit",
                    "watermark": max(
                        current.caches[baseline]["test_dates"]
                    ),
                    "missing_dates": [],
                    "fingerprint": _baseline_fingerprint(
                        spec,
                        baseline,
                    ),
                    "preserved_newer_watermark": True,
                }
            return current.caches, _generation_audit(
                spec=spec,
                generation=current,
                baseline_audits=truncated_audits,
                status="hit",
                build_mode="hit",
                build_reason="truncated_request_preserved",
                family_root=family_root,
                published=True,
                input_state=input_state,
                input_change=input_change,
                trusted_qualification=trusted_qualification,
            )
    build_mode = "full"
    build_reason = current_error or "no_current_generation"
    cached: dict[str, dict[str, Any]] = {}
    suffix_start_date: str | None = None
    if legacy_caches is not None:
        build_mode = "migration"
        build_reason = "legacy_v1_migration"
        cached = legacy_caches
    elif current is not None:
        manifest_spec = str(current.manifest.get("spec_fingerprint") or "")
        if manifest_spec != spec_fingerprint:
            build_reason = "spec_changed"
        elif set(current.caches) != set(spec.baselines):
            build_reason = "baseline_set_changed"
        elif input_change["projection_status"] != "absent":
            build_mode, build_reason, suffix_start_date = (
                _projection_build_decision(
                    spec=spec,
                    input_change=input_change,
                )
            )
            if build_mode != "full":
                cached = current.caches
        else:
            build_mode, build_reason, suffix_start_date = (
                _legacy_build_decision(
                    spec=spec,
                    input_change=input_change,
                )
            )
            if build_mode != "full":
                cached = current.caches

    caches: dict[str, dict[str, Any]] = {}
    baseline_audits: dict[str, dict[str, Any]] = {}
    acceptance_scopes: dict[str, dict[str, Any]] = {}
    needs_build = (
        build_mode in {"full", "suffix", "rebind"}
        or legacy_caches is not None
        or (
            current is not None
            and (
                current.manifest.get("input_state", {}).get(
                    "content_id"
                )
                != input_state["content_id"]
            )
        )
    )
    for baseline in spec.baselines:
        requested_dates = requested_by_baseline[baseline]
        previous = cached.get(baseline)
        parent_cache = (
            current.caches.get(baseline)
            if current is not None
            else None
        )
        cached_dates = (
            set(previous["test_dates"])
            if previous is not None
            else set()
        )
        parent_dates = (
            set(parent_cache["test_dates"])
            if parent_cache is not None
            else set()
        )
        desired_dates = sorted(
            parent_dates | set(requested_dates)
        )
        if build_mode == "full":
            missing_dates = desired_dates
            preserved = None
        elif build_mode == "suffix":
            if previous is None or suffix_start_date is None:
                raise RuntimeError(
                    "suffix rebuild requires a previous cache and cutoff"
                )
            missing_dates = [
                day
                for day in desired_dates
                if day >= suffix_start_date
            ]
            preserved = _phase_a_cache_before(
                previous,
                suffix_start_date,
            )
        else:
            missing_dates = [
                day
                for day in desired_dates
                if day not in cached_dates
            ]
            preserved = previous
        if missing_dates:
            needs_build = True
            missing_ranges = tuple((day, day) for day in missing_dates)
            trained = _validate_phase_a_cache(
                train_missing(baseline, missing_ranges)
            )
            _require_exact_trained_dates(
                baseline,
                trained,
                missing_dates,
            )
            merged = _merge_phase_a_caches(
                preserved,
                trained,
            )
            authoritative = trained
        else:
            if previous is None:
                raise RuntimeError(
                    f"baseline {baseline} cache is empty after preparation"
                )
            merged = previous
            authoritative = None
        if not parent_dates.issubset(set(merged["test_dates"])):
            raise RuntimeError(
                "cache generation coverage cannot shrink below its parent"
            )
        caches[baseline] = merged
        acceptance_scopes[baseline] = _generation_acceptance_scope(
            parent_cache=parent_cache,
            candidate_cache=merged,
            authoritative_cache=authoritative,
            affected_dates=missing_dates,
            preserve_parent=build_mode != "full",
        )
        baseline_status = (
            "cold_build"
            if build_mode == "full"
            else "suffix_rebuilt"
            if build_mode == "suffix" and missing_dates
            else "extended"
            if missing_dates
            else "hit"
        )
        baseline_audits[baseline] = {
            "status": baseline_status,
            "watermark": max(merged["test_dates"]),
            "missing_dates": missing_dates,
            "fingerprint": _baseline_fingerprint(spec, baseline),
            "preserved_newer_watermark": False,
        }

    if not needs_build and current is not None:
        if (
            not qualification_required
            and
            compare_cold is not None
            and (
                (
                    (
                        qualify_compare_gate is not None
                        or compare_full_output is not None
                    )
                    and not _generation_is_compare_gate_qualified(
                        current
                    )
                )
                or (
                    qualify_compare_gate is None
                    and compare_full_output is None
                    and _generation_compare_gate_status(current)
                    == "unqualified"
                )
            )
        ):
            needs_build = True
            build_mode = "qualification"
            build_reason = "compare_gate_qualification"

    if not needs_build and current is not None:
        if qualification_required:
            _validate_generation_acceptance_for_use(
                current,
                native_generation_binding=native_generation_binding,
            )
        return caches, _generation_audit(
            spec=spec,
            generation=current,
            baseline_audits=baseline_audits,
            status="hit",
            build_mode="hit",
            build_reason="cache_complete",
            family_root=family_root,
            published=True,
            input_state=input_state,
            input_change=input_change,
            trusted_qualification=trusted_qualification,
        )

    compare_gate_evidence = _build_compare_gate_evidence(
        caches,
        compare_cold=compare_cold,
        qualify_compare_gate=qualify_compare_gate,
        compare_full_output=compare_full_output,
        cache_family=spec.cache_family,
        tenor=spec.tenor,
        spec_fingerprint=spec_fingerprint,
        input_content_id=str(input_state["content_id"]),
    )
    generation = _create_generation(
        spec=spec,
        family_root=family_root,
        spec_fingerprint=spec_fingerprint,
        input_state=input_state,
        caches=caches,
        parent_generation=current,
        build_mode=build_mode,
        compare_gate_evidence=compare_gate_evidence,
        input_change=input_change,
        acceptance_scopes=acceptance_scopes,
        native_generation_binding=native_generation_binding,
        secure=qualification_required,
    )
    if qualification_required:
        _validate_generation_acceptance_for_use(
            generation,
            native_generation_binding=native_generation_binding,
        )
    try:
        overall_status = (
            "cold_build"
            if build_mode == "full"
            else "hit"
            if build_mode == "qualification"
            else "extended"
        )
        audit = _generation_audit(
            spec=spec,
            generation=generation,
            baseline_audits=baseline_audits,
            status=overall_status,
            build_mode=build_mode,
            build_reason=build_reason,
            family_root=family_root,
            published=True,
            input_state=input_state,
            input_change=input_change,
            trusted_qualification=trusted_qualification,
        )
        protected_generation_ids = {generation.generation_id}
        if current is not None:
            protected_generation_ids.add(current.generation_id)
        _prune_generations(
            family_root,
            protected_generation_ids=protected_generation_ids,
            secure=qualification_required,
        )
        # current.json 的原子 replace 是唯一 publication commit
        # point。所有可能失败的容量清理与返回值构造都必须在它之前完成。
        _switch_current_generation(
            family_root,
            generation,
            secure=qualification_required,
        )
    except BaseException as error:
        try:
            _discard_unpublished_generation(
                family_root,
                generation.generation_id,
                secure=qualification_required,
            )
        except BaseException as cleanup_error:
            error.add_note(
                "failed to discard unpublished cache generation "
                f"{generation.generation_id}: {cleanup_error}"
            )
        raise
    return generation.caches, audit


def _validated_consumer_hit(
    *,
    spec: PhaseACacheSpec,
    cache_consumer_id: str,
    current: _LoadedGeneration | None,
    current_error: str | None,
    requested_by_baseline: Mapping[str, list[str]],
    input_state: Mapping[str, Any],
    input_change: Mapping[str, Any],
    family_root: Path,
    qualification_required: bool,
    trusted_qualification: Mapping[str, object] | None,
    native_generation_binding: Mapping[str, object] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """非发布者只能读取同输入、完整覆盖且谱系可信的 current。"""
    if current is None:
        raise RuntimeError(
            "CACHE_PUBLISHER_REQUIRED: "
            f"{cache_consumer_id} cannot prepare cache "
            f"({current_error or 'no_current_generation'})"
        )
    if (
        current.manifest.get("spec_fingerprint")
        != _spec_fingerprint(spec)
        or set(current.caches) != set(spec.baselines)
        or not _consumer_input_states_equivalent(
            current.manifest.get("input_state"),
            input_state,
        )
    ):
        raise RuntimeError(
            "CACHE_PUBLISHER_REQUIRED: "
            f"{cache_consumer_id} requires publisher refresh"
        )
    baseline_audits: dict[str, dict[str, Any]] = {}
    for baseline in spec.baselines:
        requested = set(requested_by_baseline[baseline])
        available = set(current.caches[baseline]["test_dates"])
        if not requested.issubset(available):
            raise RuntimeError(
                "CACHE_PUBLISHER_REQUIRED: "
                f"{cache_consumer_id} requires publisher coverage"
            )
        baseline_audits[baseline] = {
            "status": "hit",
            "watermark": max(current.caches[baseline]["test_dates"]),
            "missing_dates": [],
            "fingerprint": _baseline_fingerprint(spec, baseline),
            "preserved_newer_watermark": False,
        }
    lineage_qualification = (
        trusted_qualification
        if trusted_qualification is not None
        else _lineage_qualification_for_spec(spec)
    )
    try:
        _verify_generation_acceptance_lineage(
            current,
            trusted_qualification=lineage_qualification,
        )
        if qualification_required:
            _validate_generation_acceptance_for_use(
                current,
                native_generation_binding=native_generation_binding,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            "CACHE_PUBLISHER_REQUIRED: "
            f"{cache_consumer_id} cannot validate publisher lineage"
        ) from exc
    return current.caches, _generation_audit(
        spec=spec,
        generation=current,
        baseline_audits=baseline_audits,
        status="hit",
        build_mode="hit",
        build_reason="consumer_validated_hit",
        family_root=family_root,
        published=True,
        input_state=input_state,
        input_change=dict(input_change),
        trusted_qualification=trusted_qualification,
    )


def _consumer_input_states_equivalent(
    publisher_state: Any,
    consumer_state: Any,
) -> bool:
    """比较共享 family 的有效输入，忽略各 adapter 自身 proof 文件身份。"""
    try:
        return consumer_input_state_equivalence_sha256(
            publisher_state
        ) == consumer_input_state_equivalence_sha256(
            consumer_state
        )
    except (TypeError, ValueError):
        return False


def consumer_input_state_equivalence_sha256(state: Any) -> str:
    """返回共享 consumer 输入等价口径的 canonical SHA-256。

    摘要前完整验证 input state 结构与 self content-id。schema 3 只忽略
    adapter 自身 proof 文件身份及其派生摘要，其余输入全部参与摘要。
    """
    validated = _validate_input_generation_state_record(state)
    comparable = _consumer_input_state_comparable(validated)
    return hashlib.sha256(
        _canonical_json(comparable).encode("utf-8")
    ).hexdigest()


def _consumer_input_state_comparable(
    state: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        state["schema_version"]
        != INPUT_GENERATION_STATE_SCHEMA_VERSION
    ):
        return dict(state)

    effective = dict(state["effective_auxiliary"])
    proof = dict(effective["proof"])
    proof.pop("proof_files")
    effective["proof"] = proof
    effective.pop("proof_identity_sha256")
    effective.pop("content_sha256")
    return {
        "schema_version": state["schema_version"],
        "frames": state["frames"],
        "effective_auxiliary": effective,
        "native_generation": state["native_generation"],
    }


def _lineage_qualification_for_spec(
    spec: PhaseACacheSpec,
) -> dict[str, object]:
    return {
        "qualification": {
            "cache_abi_version": PHASE_A_CACHE_ABI_VERSION,
            "cache_family": spec.cache_family,
            "tenor": spec.tenor,
            "spec_fingerprint": _spec_fingerprint(spec),
            "daily_dependency_lookback_rows": (
                spec.daily_dependency_lookback_rows
            ),
            "daily_dependency_proof": spec.daily_dependency_proof,
        }
    }


def _cache_root(cache_root: str | Path | None) -> Path:
    configured = cache_root or os.getenv("LIWEI_0616_PHASE_A_CACHE_ROOT") or DEFAULT_CACHE_ROOT
    return Path(configured)


def _resolve_cache_use_qualification(
    explicit: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if explicit is not None:
        return validate_trusted_cache_use_qualification(explicit)
    encoded = os.getenv(CACHE_USE_QUALIFICATION_ENV)
    if encoded is None:
        return None
    if not encoded.strip():
        raise ValueError(
            f"{CACHE_USE_QUALIFICATION_ENV} cannot be empty"
        )
    try:
        payload = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{CACHE_USE_QUALIFICATION_ENV} must be valid JSON"
        ) from exc
    if not isinstance(payload, Mapping):
        raise ValueError(
            f"{CACHE_USE_QUALIFICATION_ENV} must contain an object"
        )
    return validate_trusted_cache_use_qualification(payload)


def _resolve_native_generation_binding(
    explicit: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if explicit is not None:
        return _validate_native_generation_binding(explicit)
    configured = {
        "generation_id": os.getenv(NATIVE_GENERATION_ID_ENV),
        "manifest_sha256": os.getenv(NATIVE_MANIFEST_SHA256_ENV),
        "business_date": os.getenv(NATIVE_BUSINESS_DATE_ENV),
        "feature_date": os.getenv(NATIVE_FEATURE_DATE_ENV),
        "manifest_path": os.getenv(NATIVE_MANIFEST_PATH_ENV),
    }
    present = {
        field for field, value in configured.items() if value is not None
    }
    if not present:
        return None
    missing = sorted(set(configured) - present)
    if missing:
        raise ValueError(
            "partial Native generation cache binding: "
            + ", ".join(missing)
        )
    manifest_path = Path(str(configured["manifest_path"]))
    if not manifest_path.is_absolute():
        raise ValueError(
            "Native generation cache manifest path must be absolute"
        )
    manifest_bytes = manifest_path.read_bytes()
    actual_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_sha != configured["manifest_sha256"]:
        raise ValueError(
            "Native generation cache manifest SHA-256 mismatch"
        )
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Native generation cache manifest is invalid"
        ) from exc
    if not isinstance(manifest, Mapping):
        raise ValueError(
            "Native generation cache manifest must be an object"
        )
    binding = {
        "generation_id": manifest.get("generation_id"),
        "manifest_sha256": actual_sha,
        "dataset_content_id": manifest.get("dataset_content_id"),
        "business_date": manifest.get("business_date"),
        "feature_date": manifest.get("feature_date"),
        "schema_version": manifest.get("schema_version"),
        "exporter_version": manifest.get("exporter_version"),
    }
    validated = _validate_native_generation_binding(binding)
    for field in ("generation_id", "business_date", "feature_date"):
        if validated[field] != configured[field]:
            raise ValueError(
                f"Native generation cache {field} mismatch"
            )
    return validated


def _validate_native_generation_binding(
    raw: Mapping[str, object],
) -> dict[str, object]:
    required = {
        "generation_id",
        "manifest_sha256",
        "dataset_content_id",
        "business_date",
        "feature_date",
        "schema_version",
        "exporter_version",
    }
    if set(raw) != required:
        raise ValueError(
            "Native generation cache binding fields mismatch"
        )
    normalized = dict(raw)
    for field in (
        "generation_id",
        "business_date",
        "feature_date",
        "schema_version",
        "exporter_version",
    ):
        value = normalized.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"Native generation cache {field} is required"
            )
    for field in ("manifest_sha256", "dataset_content_id"):
        _require_cache_sha256(normalized.get(field), field)
    if normalized["schema_version"] != NATIVE_GENERATION_SCHEMA_VERSION:
        raise ValueError(
            "Native generation cache schema_version mismatch"
        )
    if (
        normalized["exporter_version"]
        != NATIVE_GENERATION_EXPORTER_VERSION
    ):
        raise ValueError(
            "Native generation cache exporter_version mismatch"
        )
    return normalized


def _validate_qualification_for_cache_use(
    trusted: Mapping[str, object],
    *,
    cache_consumer_id: str,
    spec: PhaseACacheSpec,
    native_generation: Mapping[str, object],
) -> None:
    validated_trusted = validate_trusted_cache_use_qualification(
        trusted,
        expected_base_scheme_id=cache_consumer_id,
    )
    qualification = validated_trusted["qualification"]
    expected = {
        "base_scheme_id": cache_consumer_id,
        "cache_group": f"{spec.cache_family}:{spec.tenor}",
        "cache_family": spec.cache_family,
        "tenor": spec.tenor,
        "spec_fingerprint": _spec_fingerprint(spec),
        "cache_abi_version": PHASE_A_CACHE_ABI_VERSION,
        "native_generation_schema_version":
            native_generation["schema_version"],
        "native_exporter_version":
            native_generation["exporter_version"],
        "daily_dependency_lookback_rows":
            spec.daily_dependency_lookback_rows,
        "daily_dependency_proof": spec.daily_dependency_proof,
    }
    drift = sorted(
        field
        for field, expected_value in expected.items()
        if qualification.get(field) != expected_value
    )
    if drift:
        raise RuntimeError(
            "cache use qualification identity drift: "
            + ", ".join(drift)
        )


def _require_cache_sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be lowercase SHA-256")
    return value


def _family_cache_root(root: Path, spec: PhaseACacheSpec) -> Path:
    return (
        root
        / safe_path_part(spec.cache_family)
        / safe_path_part(spec.tenor.lower())
    )


def _load_legacy_v1_caches(
    *,
    root: Path,
    family_root: Path,
    spec: PhaseACacheSpec,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
) -> dict[str, dict[str, Any]] | None:
    """只读校验并收编 v1 热缓存；任何不确定性都回退 cold build。"""
    candidate_directories = (
        root / safe_path_part(spec.tenor.lower()),
        family_root,
    )
    for directory in candidate_directories:
        paths = {
            baseline: (
                directory / f"{safe_path_part(baseline)}.pkl"
            )
            for baseline in spec.baselines
        }
        if not all(path.is_file() for path in paths.values()):
            continue
        caches: dict[str, dict[str, Any]] = {}
        try:
            for baseline, path in paths.items():
                with path.open("rb") as handle:
                    envelope = pickle.load(handle)
                if (
                    not isinstance(envelope, Mapping)
                    or envelope.get("schema_version")
                    != LEGACY_CACHE_SCHEMA_VERSION
                    or envelope.get("cache_family")
                    != spec.cache_family
                    or str(envelope.get("tenor") or "").lower()
                    != spec.tenor.lower()
                    or envelope.get("baseline") != baseline
                    or envelope.get("baseline_fingerprint")
                    != _baseline_fingerprint(spec, baseline)
                ):
                    raise ValueError(
                        "legacy v1 cache identity mismatch"
                    )
                cache = _validate_phase_a_cache(
                    envelope.get("phase_a_cache")
                )
                if (
                    str(envelope.get("watermark") or "")
                    != max(cache["test_dates"])
                ):
                    raise ValueError(
                        "legacy v1 cache watermark mismatch"
                    )
                if not _legacy_input_prefix_matches(
                    envelope.get("input_prefix"),
                    daily_df=daily_df,
                    weekly_df=weekly_df,
                    monthly_df=monthly_df,
                ):
                    raise ValueError(
                        "legacy v1 cache input prefix mismatch"
                    )
                caches[baseline] = cache
        except (
            OSError,
            EOFError,
            pickle.PickleError,
            AttributeError,
            ImportError,
            TypeError,
            ValueError,
        ):
            continue
        return caches
    return None


def _spec_fingerprint(spec: PhaseACacheSpec) -> str:
    payload = {
        "abi": PHASE_A_CACHE_ABI_VERSION,
        "cache_family": spec.cache_family,
        "tenor": spec.tenor,
        "baselines": list(spec.baselines),
        "baseline_fingerprints": {
            baseline: _baseline_fingerprint(spec, baseline)
            for baseline in spec.baselines
        },
        "source_ic_screen_start": spec.source_ic_screen_start,
        "horizon": spec.horizon,
        "purge_gap": spec.purge_gap,
        "daily_dependency_lookback_rows": (
            spec.daily_dependency_lookback_rows
        ),
        "daily_dependency_proof": spec.daily_dependency_proof,
    }
    return hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()


def _input_generation_state(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    auxiliary_dependency_projection: (
        AuxiliaryDependencyProjection | None
    ),
    native_generation_binding: Mapping[str, object] | None,
) -> dict[str, Any]:
    frames = {
        "daily": _frame_generation_state(daily_df, "date"),
        "weekly": _frame_generation_state(weekly_df, "week_id"),
        "monthly": _frame_generation_state(monthly_df, "month_id"),
    }
    native_generation = (
        dict(native_generation_binding)
        if native_generation_binding is not None
        else None
    )
    if auxiliary_dependency_projection is None:
        legacy_basis = {
            "frames": frames,
            "native_generation": native_generation,
        }
        return {
            "schema_version":
                LEGACY_INPUT_GENERATION_STATE_SCHEMA_VERSION,
            "frames": frames,
            "native_generation": native_generation,
            "content_id": hashlib.sha256(
                _canonical_json(legacy_basis).encode("utf-8")
            ).hexdigest(),
        }
    effective_auxiliary = _effective_auxiliary_generation_state(
        auxiliary_dependency_projection
    )
    basis = {
        "frames": frames,
        "effective_auxiliary": effective_auxiliary,
        "native_generation": native_generation,
    }
    return {
        "schema_version": INPUT_GENERATION_STATE_SCHEMA_VERSION,
        "frames": frames,
        "effective_auxiliary": effective_auxiliary,
        "native_generation": basis["native_generation"],
        "content_id": hashlib.sha256(
            _canonical_json(basis).encode("utf-8")
        ).hexdigest(),
    }


def _validate_input_generation_state_record(raw: Any) -> dict[str, Any]:
    """校验 manifest 内输入状态的完整结构及 self content id。"""
    if not isinstance(raw, Mapping):
        raise ValueError("cache input generation state fields mismatch")
    schema_version = raw.get("schema_version")
    if schema_version not in {
        LEGACY_INPUT_GENERATION_STATE_SCHEMA_VERSION,
        INPUT_GENERATION_STATE_SCHEMA_VERSION,
    }:
        raise ValueError("cache input generation state schema mismatch")
    expected_fields = {
        "schema_version",
        "frames",
        "native_generation",
        "content_id",
    }
    if schema_version == INPUT_GENERATION_STATE_SCHEMA_VERSION:
        expected_fields.add("effective_auxiliary")
    if set(raw) != expected_fields:
        raise ValueError("cache input generation state fields mismatch")
    frames = raw.get("frames")
    if not isinstance(frames, Mapping) or set(frames) != {
        "daily",
        "weekly",
        "monthly",
    }:
        raise ValueError("cache input generation frame set mismatch")
    expected_keys = {
        "daily": "date",
        "weekly": "week_id",
        "monthly": "month_id",
    }
    normalized_frames: dict[str, dict[str, Any]] = {}
    for name, expected_key in expected_keys.items():
        normalized_frames[name] = _validate_frame_generation_state(
            frames.get(name),
            expected_key=expected_key,
            label=f"cache input generation {name}",
        )
    native = raw.get("native_generation")
    normalized_native = (
        _validate_native_generation_binding(native)
        if isinstance(native, Mapping)
        else None
    )
    if native is not None and normalized_native is None:
        raise ValueError("cache input Native generation is invalid")
    content_id = _require_cache_sha256(
        raw.get("content_id"),
        "input_state.content_id",
    )
    basis: dict[str, object] = {
        "frames": normalized_frames,
        "native_generation": normalized_native,
    }
    normalized_effective: dict[str, Any] | None = None
    if schema_version == INPUT_GENERATION_STATE_SCHEMA_VERSION:
        normalized_effective = _validate_effective_auxiliary_state(
            raw.get("effective_auxiliary")
        )
        basis["effective_auxiliary"] = normalized_effective
    expected_content_id = hashlib.sha256(
        _canonical_json(basis).encode("utf-8")
    ).hexdigest()
    if content_id != expected_content_id:
        raise ValueError("cache input generation content digest mismatch")
    validated = {
        "schema_version": schema_version,
        "frames": normalized_frames,
        "native_generation": normalized_native,
        "content_id": content_id,
    }
    if normalized_effective is not None:
        validated["effective_auxiliary"] = normalized_effective
    return validated


def _validate_frame_generation_state(
    raw: Any,
    *,
    expected_key: str,
    label: str,
) -> dict[str, Any]:
    fields = {
        "key",
        "bound",
        "row_count",
        "columns",
        "dtypes",
        "key_fingerprints",
        "fingerprint",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise ValueError(f"{label} frame fields mismatch")
    if raw.get("key") != expected_key:
        raise ValueError(f"{label} key mismatch")
    row_count = raw.get("row_count")
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 0
    ):
        raise ValueError(f"{label} row_count is invalid")
    columns = raw.get("columns")
    dtypes = raw.get("dtypes")
    if (
        not isinstance(columns, list)
        or any(not isinstance(item, str) for item in columns)
        or columns != sorted(set(columns))
        or not isinstance(dtypes, Mapping)
        or set(dtypes) != set(columns)
        or any(
            not isinstance(value, str) or not value
            for value in dtypes.values()
        )
    ):
        raise ValueError(f"{label} schema is invalid")
    key_fingerprints = raw.get("key_fingerprints")
    if not isinstance(key_fingerprints, list):
        raise ValueError(f"{label} key fingerprints are invalid")
    fingerprint_rows = 0
    for entry in key_fingerprints:
        if not isinstance(entry, Mapping) or set(entry) != {
            "key",
            "row_count",
            "fingerprint",
        }:
            raise ValueError(f"{label} key entry is invalid")
        entry_rows = entry.get("row_count")
        if (
            isinstance(entry_rows, bool)
            or not isinstance(entry_rows, int)
            or entry_rows < 0
        ):
            raise ValueError(f"{label} key row_count is invalid")
        _require_cache_sha256(
            entry.get("fingerprint"),
            f"{label}.key_fingerprint",
        )
        fingerprint_rows += entry_rows
    if fingerprint_rows != row_count:
        raise ValueError(f"{label} row_count mismatch")
    _require_cache_sha256(
        raw.get("fingerprint"),
        f"{label}.fingerprint",
    )
    return dict(raw)


def _effective_auxiliary_generation_state(
    projection: AuxiliaryDependencyProjection,
) -> dict[str, Any]:
    if not isinstance(projection, AuxiliaryDependencyProjection):
        raise TypeError(
            "auxiliary_dependency_projection must be an "
            "AuxiliaryDependencyProjection"
        )
    frame = projection.frame
    proof = projection.proof
    frame_state = _frame_generation_state(frame, "date")
    normalized_proof = _validate_effective_auxiliary_proof(
        proof,
        frame_state=frame_state,
    )
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "frame": frame_state,
        "proof": normalized_proof,
        "proof_identity_sha256": _effective_auxiliary_proof_identity(
            normalized_proof
        ),
        "content_sha256": _require_cache_sha256(
            projection.content_sha256,
            "effective_auxiliary.content_sha256",
        ),
    }


def _validate_effective_auxiliary_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "schema_version",
        "frame",
        "proof",
        "proof_identity_sha256",
        "content_sha256",
    }:
        raise ValueError("effective auxiliary input state fields mismatch")
    if raw.get("schema_version") != PROJECTION_SCHEMA_VERSION:
        raise ValueError("effective auxiliary input schema mismatch")
    frame = raw.get("frame")
    if not isinstance(frame, Mapping):
        raise ValueError("effective auxiliary frame state is invalid")
    normalized_frame = _validate_frame_generation_state(
        frame,
        expected_key="date",
        label="effective auxiliary",
    )
    normalized_proof = _validate_effective_auxiliary_proof(
        raw.get("proof"),
        frame_state=normalized_frame,
    )
    proof_identity = _require_cache_sha256(
        raw.get("proof_identity_sha256"),
        "effective_auxiliary.proof_identity_sha256",
    )
    if proof_identity != _effective_auxiliary_proof_identity(
        normalized_proof
    ):
        raise ValueError("effective auxiliary proof identity mismatch")
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "frame": normalized_frame,
        "proof": normalized_proof,
        "proof_identity_sha256": proof_identity,
        "content_sha256": _require_cache_sha256(
            raw.get("content_sha256"),
            "effective_auxiliary.content_sha256",
        ),
    }


def _validate_effective_auxiliary_proof(
    raw: Any,
    *,
    frame_state: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "date_to_week_mode",
        "date_to_week_sha256",
        "date_to_week_entries",
        "proof_files",
        "columns",
        "dtypes",
        "daily_grid_sha256",
        "feature_cutoff",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise ValueError("effective auxiliary proof fields mismatch")
    if raw.get("schema_version") != PROJECTION_SCHEMA_VERSION:
        raise ValueError("effective auxiliary proof schema mismatch")
    if raw.get("date_to_week_mode") not in {"explicit", "fallback"}:
        raise ValueError("effective auxiliary date_to_week mode is invalid")
    date_to_week_entries = _validate_date_to_week_entries(
        raw.get("date_to_week_entries")
    )
    date_to_week_sha = _require_cache_sha256(
        raw.get("date_to_week_sha256"),
        "effective_auxiliary.date_to_week_sha256",
    )
    expected_mapping_sha = hashlib.sha256(
        _canonical_json(
            [
                [entry["date"], entry["week_id"]]
                for entry in date_to_week_entries
            ]
        ).encode("utf-8")
    ).hexdigest()
    if date_to_week_sha != expected_mapping_sha:
        raise ValueError("effective auxiliary date_to_week digest mismatch")
    daily_grid_sha = _require_cache_sha256(
        raw.get("daily_grid_sha256"),
        "effective_auxiliary.daily_grid_sha256",
    )
    proof_files = raw.get("proof_files")
    if not isinstance(proof_files, list) or not proof_files:
        raise ValueError("effective auxiliary proof files are invalid")
    normalized_files: list[dict[str, str]] = []
    for record in proof_files:
        if (
            not isinstance(record, Mapping)
            or set(record) != {"name", "sha256"}
            or not isinstance(record.get("name"), str)
            or not str(record["name"]).strip()
        ):
            raise ValueError("effective auxiliary proof file is invalid")
        normalized_files.append(
            {
                "name": str(record["name"]),
                "sha256": _require_cache_sha256(
                    record.get("sha256"),
                    "effective_auxiliary.proof_file_sha256",
                ),
            }
        )
    columns = raw.get("columns")
    dtypes = raw.get("dtypes")
    frame_columns = list(frame_state.get("columns") or [])
    frame_dtypes = frame_state.get("dtypes")
    expected_dtypes = (
        [frame_dtypes[column] for column in columns]
        if isinstance(columns, list)
        and isinstance(frame_dtypes, Mapping)
        and all(column in frame_dtypes for column in columns)
        else None
    )
    if (
        not isinstance(columns, list)
        or any(not isinstance(column, str) for column in columns)
        or set(columns) != set(frame_columns)
        or not isinstance(dtypes, list)
        or dtypes != expected_dtypes
    ):
        raise ValueError("effective auxiliary proof frame schema mismatch")
    feature_cutoff = raw.get("feature_cutoff")
    if (
        not isinstance(feature_cutoff, str)
        or feature_cutoff != frame_state.get("bound")
    ):
        raise ValueError("effective auxiliary feature cutoff mismatch")
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "date_to_week_mode": raw["date_to_week_mode"],
        "date_to_week_sha256": date_to_week_sha,
        "date_to_week_entries": date_to_week_entries,
        "proof_files": normalized_files,
        "columns": list(columns),
        "dtypes": list(dtypes),
        "daily_grid_sha256": daily_grid_sha,
        "feature_cutoff": feature_cutoff,
    }


def _effective_auxiliary_proof_identity(
    proof: Mapping[str, Any],
) -> str:
    stable_proof = {
        key: proof[key]
        for key in (
            "schema_version",
            "date_to_week_mode",
            "proof_files",
            "columns",
            "dtypes",
        )
    }
    return hashlib.sha256(
        _canonical_json(stable_proof).encode("utf-8")
    ).hexdigest()


def _validate_date_to_week_entries(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("effective auxiliary date_to_week entries are invalid")
    normalized: list[dict[str, Any]] = []
    previous_date: str | None = None
    for entry in raw:
        if not isinstance(entry, Mapping) or set(entry) != {
            "date",
            "week_id",
        }:
            raise ValueError(
                "effective auxiliary date_to_week entry is invalid"
            )
        date_value = entry.get("date")
        week_id = entry.get("week_id")
        if (
            not isinstance(date_value, str)
            or isinstance(week_id, bool)
            or not isinstance(week_id, int)
        ):
            raise ValueError(
                "effective auxiliary date_to_week entry is invalid"
            )
        try:
            normalized_date = pd.Timestamp(date_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "effective auxiliary date_to_week entry is invalid"
            ) from exc
        if (
            pd.isna(normalized_date)
            or normalized_date.tzinfo is not None
            or normalized_date.strftime("%Y-%m-%d") != date_value
            or (
                previous_date is not None
                and date_value <= previous_date
            )
        ):
            raise ValueError(
                "effective auxiliary date_to_week entries are not canonical"
            )
        normalized.append({"date": date_value, "week_id": week_id})
        previous_date = date_value
    return normalized


def _frame_generation_state(
    frame: pd.DataFrame,
    key: str,
) -> dict[str, Any]:
    if key not in frame.columns:
        raise ValueError(f"input frame missing generation key {key}")
    normalized = _normalized_key(frame[key], key)
    if normalized.isna().any():
        raise ValueError(f"input frame contains invalid generation key {key}")
    values = normalized.tolist()
    bound = max(values) if values else None
    normalized_bound = (
        _normalized_bound(bound, key)
        if bound is not None
        else None
    )
    work = frame.copy()
    work[key] = normalized
    columns = sorted(str(column) for column in work.columns)
    if set(columns) != set(work.columns):
        raise ValueError("input frame columns must be strings")
    dtypes = {
        column: str(work[column].dtype)
        for column in columns
    }
    key_fingerprints: list[dict[str, Any]] = []
    for key_value, group in work.groupby(
        key,
        dropna=False,
        sort=True,
    ):
        normalized_value = _normalized_bound(key_value, key)
        row_hashes = sorted(
            int(value)
            for value in pd.util.hash_pandas_object(
                group[columns],
                index=False,
            ).astype("uint64")
        )
        key_fingerprints.append(
            {
                "key": normalized_value,
                "row_count": len(group),
                "fingerprint": hashlib.sha256(
                    _canonical_json(
                        {
                            "key": normalized_value,
                            "columns": columns,
                            "dtypes": dtypes,
                            "row_hashes": row_hashes,
                        }
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
    return {
        "key": key,
        "bound": normalized_bound,
        "row_count": len(frame),
        "columns": columns,
        "dtypes": dtypes,
        "key_fingerprints": key_fingerprints,
        "fingerprint": _frame_prefix_fingerprint(
            frame,
            key,
            normalized_bound,
        ),
    }


def _validate_daily_dependency_proof(
    spec: PhaseACacheSpec,
) -> None:
    rows = spec.daily_dependency_lookback_rows
    proof = str(spec.daily_dependency_proof or "").strip()
    if rows is None and not proof:
        return
    if rows is None or not proof:
        raise ValueError(
            "daily suffix invalidation requires both "
            "daily_dependency_lookback_rows and "
            "daily_dependency_proof"
        )
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
        raise ValueError(
            "daily_dependency_lookback_rows must be a "
            "non-negative integer"
        )


def _validate_cache_publisher_identity(spec: PhaseACacheSpec) -> None:
    expected = APPROVED_PHASE_A_CACHE_PUBLISHERS.get(
        spec.cache_family
    )
    if expected is None:
        return
    expected_tenor, expected_publisher = expected
    if (
        spec.tenor.upper() != expected_tenor
        or spec.publisher_consumer_id != expected_publisher
    ):
        raise RuntimeError("CACHE_PUBLISHER_IDENTITY_DRIFT")


def _compare_gate_required(explicit: bool | None) -> bool:
    if explicit is not None and not isinstance(explicit, bool):
        raise TypeError("require_compare_gate must be a boolean or None")
    mode = str(
        os.getenv(DAILY_COORDINATOR_MODE_ENV) or ""
    ).strip()
    if mode and mode not in {"legacy", "ledger"}:
        raise ValueError(
            f"{DAILY_COORDINATOR_MODE_ENV} must be legacy or ledger"
        )
    return mode == "ledger" or explicit is True


def _input_change_analysis(
    previous: Any,
    current: Mapping[str, Any],
) -> dict[str, Any]:
    previous_frames = (
        previous.get("frames")
        if isinstance(previous, Mapping)
        else None
    )
    current_frames = current.get("frames")
    if not isinstance(current_frames, Mapping):
        raise ValueError("current input generation frames are missing")
    frame_changes: dict[str, dict[str, Any]] = {}
    for name in ("daily", "weekly", "monthly"):
        prior = (
            previous_frames.get(name)
            if isinstance(previous_frames, Mapping)
            else None
        )
        latest = current_frames.get(name)
        frame_changes[name] = _frame_change_analysis(
            prior,
            latest,
        )
    raw_change_type = _aggregate_input_change_type(
        str(item["change_type"])
        for item in frame_changes.values()
    )
    daily_union_keys: list[str | int] = []
    if isinstance(previous_frames, Mapping):
        prior_daily = previous_frames.get("daily")
        current_daily = current_frames.get("daily")
        if isinstance(prior_daily, Mapping) and isinstance(
            current_daily,
            Mapping,
        ):
            prior_entries = _frame_key_entries(prior_daily)
            current_entries = _frame_key_entries(current_daily)
            if prior_entries is not None and current_entries is not None:
                daily_union_keys = sorted(
                    set(prior_entries) | set(current_entries),
                    key=_frame_key_sort_value,
                )
    previous_native = (
        previous.get("native_generation")
        if isinstance(previous, Mapping)
        else None
    )
    current_native = current.get("native_generation")
    previous_effective = (
        previous.get("effective_auxiliary")
        if isinstance(previous, Mapping)
        else None
    )
    current_effective = current.get("effective_auxiliary")
    projection_status = "absent"
    mapping_change = {
        "change_type": "unavailable",
        "earliest_changed_key": None,
    }
    effective_change = {
        "change_type": "unavailable",
        "earliest_changed_key": None,
        "schema_changed": False,
    }
    if isinstance(previous_effective, Mapping) and isinstance(
        current_effective,
        Mapping,
    ):
        prior_identity = previous_effective.get(
            "proof_identity_sha256"
        )
        current_identity = current_effective.get(
            "proof_identity_sha256"
        )
        if (
            previous_effective.get("schema_version")
            != current_effective.get("schema_version")
        ):
            projection_status = "schema_changed"
        elif prior_identity != current_identity:
            projection_status = "proof_changed"
        else:
            mapping_change = _date_to_week_change_analysis(
                previous_effective,
                current_effective,
            )
            if mapping_change["change_type"] in {
                "revision",
                "unknown",
            }:
                projection_status = "mapping_changed"
            else:
                projection_status = "valid"
                effective_change = _frame_change_analysis(
                    previous_effective.get("frame"),
                    current_effective.get("frame"),
                )
    elif previous_effective is None and current_effective is None:
        projection_status = "absent"
    elif previous_effective is None:
        projection_status = "missing_from_parent"
    else:
        projection_status = "missing_current"
    if projection_status == "valid":
        change_type = _aggregate_input_change_type(
            (
                str(frame_changes["daily"]["change_type"]),
                str(effective_change["change_type"]),
            )
        )
    elif projection_status == "absent":
        change_type = raw_change_type
    else:
        change_type = "unknown"
    return {
        "change_type": change_type,
        "raw_change_type": raw_change_type,
        "frames": frame_changes,
        "effective_auxiliary": effective_change,
        "date_to_week": mapping_change,
        "projection_status": projection_status,
        "suffix_start_date": None,
        "native_generation_changed": (
            previous_native != current_native
        ),
        "_daily_union_keys": daily_union_keys,
    }


def _aggregate_input_change_type(change_types: Any) -> str:
    normalized = {str(item) for item in change_types}
    if "unknown" in normalized:
        return "unknown"
    if "revision" in normalized:
        return "revision"
    if "append" in normalized:
        return "append"
    if normalized == {"initial"}:
        return "initial"
    if normalized <= {"unchanged", "initial"} and "initial" in normalized:
        return "initial"
    return "unchanged"


def _date_to_week_change_analysis(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        previous_entries = {
            str(item["date"]): int(item["week_id"])
            for item in previous["proof"]["date_to_week_entries"]
        }
        current_entries = {
            str(item["date"]): int(item["week_id"])
            for item in current["proof"]["date_to_week_entries"]
        }
    except (KeyError, TypeError, ValueError):
        return {
            "change_type": "unknown",
            "earliest_changed_key": None,
        }
    changed = {
        day
        for day in set(previous_entries) | set(current_entries)
        if previous_entries.get(day) != current_entries.get(day)
    }
    if not changed:
        return {
            "change_type": "unchanged",
            "earliest_changed_key": None,
        }
    added = set(current_entries) - set(previous_entries)
    removed = set(previous_entries) - set(current_entries)
    only_tail_append = (
        bool(added)
        and not removed
        and changed == added
        and (
            not previous_entries
            or all(day > max(previous_entries) for day in added)
        )
    )
    return {
        "change_type": "append" if only_tail_append else "revision",
        "earliest_changed_key": min(changed),
    }


def _frame_change_analysis(
    previous: Any,
    current: Any,
) -> dict[str, Any]:
    if not isinstance(current, Mapping):
        raise ValueError("current frame generation state is invalid")
    current_entries = _frame_key_entries(current)
    if previous is None:
        return {
            "change_type": "initial",
            "earliest_changed_key": (
                _minimum_frame_key(current_entries)
            ),
            "schema_changed": False,
        }
    if not isinstance(previous, Mapping):
        return {
            "change_type": "unknown",
            "earliest_changed_key": None,
            "schema_changed": True,
        }
    previous_entries = _frame_key_entries(previous)
    if previous_entries is None or current_entries is None:
        return {
            "change_type": "unknown",
            "earliest_changed_key": None,
            "schema_changed": True,
        }
    schema_changed = (
        previous.get("key") != current.get("key")
        or previous.get("columns") != current.get("columns")
        or previous.get("dtypes") != current.get("dtypes")
    )
    changed_keys = {
        key_value
        for key_value in set(previous_entries) | set(current_entries)
        if previous_entries.get(key_value)
        != current_entries.get(key_value)
    }
    if not changed_keys and not schema_changed:
        return {
            "change_type": "unchanged",
            "earliest_changed_key": None,
            "schema_changed": False,
        }
    earliest = _minimum_frame_key(changed_keys)
    if schema_changed:
        return {
            "change_type": "revision",
            "earliest_changed_key": earliest,
            "schema_changed": True,
        }
    previous_keys = set(previous_entries)
    current_keys = set(current_entries)
    added = current_keys - previous_keys
    removed = previous_keys - current_keys
    old_bound = previous.get("bound")
    only_new_suffix = (
        bool(added)
        and not removed
        and changed_keys == added
        and (
            not previous_keys
            or (
                old_bound is not None
                and all(
                    _frame_key_sort_value(value)
                    > _frame_key_sort_value(old_bound)
                    for value in added
                )
            )
        )
    )
    return {
        "change_type": (
            "append" if only_new_suffix else "revision"
        ),
        "earliest_changed_key": earliest,
        "schema_changed": False,
    }


def _frame_key_entries(
    state: Mapping[str, Any],
) -> dict[str | int, tuple[int, str]] | None:
    raw_entries = state.get("key_fingerprints")
    if not isinstance(raw_entries, list):
        return None
    entries: dict[str | int, tuple[int, str]] = {}
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            return None
        key_value = raw.get("key")
        if isinstance(key_value, bool) or not isinstance(
            key_value,
            (str, int),
        ):
            return None
        if key_value in entries:
            return None
        row_count = raw.get("row_count")
        fingerprint = raw.get("fingerprint")
        if (
            isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count < 1
            or not isinstance(fingerprint, str)
            or len(fingerprint) != 64
        ):
            return None
        entries[key_value] = (row_count, fingerprint)
    return entries


def _minimum_frame_key(
    values: Mapping[str | int, Any] | set[str | int] | None,
) -> str | int | None:
    if not values:
        return None
    return min(values, key=_frame_key_sort_value)


def _frame_key_sort_value(value: Any) -> tuple[int, Any]:
    if isinstance(value, bool):
        return (2, str(value))
    if isinstance(value, int):
        return (0, value)
    return (1, str(value))


def _projection_build_decision(
    *,
    spec: PhaseACacheSpec,
    input_change: dict[str, Any],
) -> tuple[str, str, str | None]:
    status = input_change["projection_status"]
    if status == "missing_from_parent":
        return (
            "full",
            "effective_auxiliary_projection_missing_from_parent",
            None,
        )
    if status == "missing_current":
        return (
            "full",
            "effective_auxiliary_projection_missing_current",
            None,
        )
    if status == "proof_changed":
        return "full", "effective_auxiliary_proof_changed", None
    if status == "mapping_changed":
        return "full", "date_to_week_history_changed", None
    if status != "valid":
        return "full", "effective_auxiliary_projection_unknown", None
    daily = input_change["frames"]["daily"]
    effective = input_change["effective_auxiliary"]
    effective_cutoff: str | None = None
    if effective["change_type"] == "revision":
        effective_cutoff = effective["earliest_changed_key"]
        if (
            effective["schema_changed"]
            or not isinstance(effective_cutoff, str)
        ):
            return "full", "effective_auxiliary_revision_unknown", None
    if daily["change_type"] in {"revision", "unknown"}:
        daily_decision = _revision_build_decision(
            spec=spec,
            input_change=input_change,
            ignore_raw_auxiliary=True,
        )
        if daily_decision[0] != "suffix":
            return daily_decision
        if effective_cutoff is None:
            return daily_decision
        combined_cutoff = min(daily_decision[2], effective_cutoff)
        input_change["suffix_start_date"] = combined_cutoff
        return (
            "suffix",
            "combined_daily_effective_revision",
            combined_cutoff,
        )
    if effective_cutoff is not None:
        input_change["suffix_start_date"] = effective_cutoff
        return (
            "suffix",
            "effective_auxiliary_revision",
            effective_cutoff,
        )
    if effective["change_type"] not in {"unchanged", "append"}:
        return "full", "effective_auxiliary_projection_unknown", None
    if input_change["native_generation_changed"]:
        return "rebind", "native_generation_rebound", None
    return (
        "append",
        (
            "effective_auxiliary_append"
            if (
                effective["change_type"] == "append"
                or input_change["date_to_week"]["change_type"]
                == "append"
            )
            else "cache_complete"
        ),
        None,
    )


def _legacy_build_decision(
    *,
    spec: PhaseACacheSpec,
    input_change: dict[str, Any],
) -> tuple[str, str, str | None]:
    for name in ("weekly", "monthly"):
        change_type = input_change["frames"][name]["change_type"]
        if change_type in {"revision", "unknown"}:
            return _revision_build_decision(
                spec=spec,
                input_change=input_change,
            )
        if change_type == "append":
            return "full", f"{name}_input_append_unmappable", None
    if input_change["native_generation_changed"] and (
        input_change["change_type"] in {"unchanged", "append"}
    ):
        return "rebind", "native_generation_rebound", None
    if input_change["change_type"] in {"unchanged", "append"}:
        return (
            "append",
            (
                "cache_complete"
                if input_change["change_type"] == "unchanged"
                else "append_only"
            ),
            None,
        )
    return _revision_build_decision(
        spec=spec,
        input_change=input_change,
    )


def _revision_build_decision(
    *,
    spec: PhaseACacheSpec,
    input_change: dict[str, Any],
    ignore_raw_auxiliary: bool = False,
) -> tuple[str, str, str | None]:
    frames = input_change["frames"]
    has_daily_proof = (
        spec.daily_dependency_lookback_rows is not None
        and bool(str(spec.daily_dependency_proof or "").strip())
    )
    if not ignore_raw_auxiliary:
        for name in ("weekly", "monthly"):
            change = frames[name]
            if change["change_type"] in {"revision", "unknown"}:
                return (
                    "full",
                    (
                        f"{name}_input_revision_unmappable"
                        if has_daily_proof
                        else "input_revision"
                    ),
                    None,
                )
    daily = frames["daily"]
    if (
        daily["change_type"] != "revision"
        or daily["schema_changed"]
        or not has_daily_proof
    ):
        return "full", "input_revision", None
    earliest = daily["earliest_changed_key"]
    if not isinstance(earliest, str):
        return "full", "input_revision", None
    union_keys = input_change.get("_daily_union_keys")
    if not isinstance(union_keys, list) or earliest not in union_keys:
        return "full", "input_revision", None
    position = union_keys.index(earliest)
    cutoff_position = max(
        0,
        position - spec.daily_dependency_lookback_rows,
    )
    cutoff = union_keys[cutoff_position]
    if not isinstance(cutoff, str):
        return "full", "input_revision", None
    input_change["suffix_start_date"] = cutoff
    return "suffix", "proven_daily_input_revision", cutoff


def _is_safe_truncated_input(
    previous: Any,
    current: Mapping[str, Any],
) -> bool:
    if not isinstance(previous, Mapping):
        return False
    previous_frames = previous.get("frames")
    current_frames = current.get("frames")
    if not isinstance(previous_frames, Mapping) or not isinstance(
        current_frames,
        Mapping,
    ):
        return False
    if (
        previous.get("effective_auxiliary") is not None
        or current.get("effective_auxiliary") is not None
    ):
        return False
    for name in ("daily", "weekly", "monthly"):
        old = previous_frames.get(name)
        new = current_frames.get(name)
        if not isinstance(old, Mapping) or not isinstance(new, Mapping):
            return False
        old_entries = _frame_key_entries(old)
        new_entries = _frame_key_entries(new)
        if old_entries is None or new_entries is None:
            return False
        if (
            old.get("key") != new.get("key")
            or old.get("columns") != new.get("columns")
            or old.get("dtypes") != new.get("dtypes")
        ):
            return False
        if any(
            old_entries.get(key_value) != value
            for key_value, value in new_entries.items()
        ):
            return False
        if not set(new_entries).issubset(old_entries):
            return False
    return True


def _require_exact_trained_dates(
    baseline: str,
    cache: Mapping[str, Any],
    expected_dates: list[str],
) -> None:
    actual = [str(day) for day in cache["test_dates"]]
    if set(actual) != set(expected_dates) or len(actual) != len(expected_dates):
        raise ValueError(
            f"baseline {baseline} returned unexpected Phase A dates; "
            f"expected={expected_dates}, actual={actual}"
        )


def _create_generation(
    *,
    spec: PhaseACacheSpec,
    family_root: Path,
    spec_fingerprint: str,
    input_state: Mapping[str, Any],
    caches: Mapping[str, Mapping[str, Any]],
    parent_generation: _LoadedGeneration | None,
    build_mode: str,
    compare_gate_evidence: Mapping[str, Any],
    input_change: Mapping[str, Any],
    acceptance_scopes: Mapping[str, Mapping[str, Any]],
    native_generation_binding: Mapping[str, object] | None,
    secure: bool,
) -> _LoadedGeneration:
    family_root.mkdir(parents=True, exist_ok=True)
    family_identity = (
        _require_secure_directory(family_root, "cache family")
        if secure
        else None
    )
    staging = Path(
        tempfile.mkdtemp(
            dir=family_root,
            prefix=".building-",
        )
    )
    finalized = False
    try:
        baseline_directory = staging / "baselines"
        baseline_directory.mkdir()
        entries: dict[str, dict[str, Any]] = {}
        evidence_by_baseline: dict[str, dict[str, Any]] = {}
        for baseline in spec.baselines:
            cache = _validate_phase_a_cache(caches[baseline])
            evidence = _phase_a_cache_evidence(cache)
            relative_path = (
                Path("baselines")
                / f"{safe_path_part(baseline)}.pkl"
            )
            now = _utc_now()
            envelope = {
                "schema_version": CACHE_SCHEMA_VERSION,
                "cache_family": spec.cache_family,
                "tenor": spec.tenor,
                "baseline": baseline,
                "baseline_fingerprint": _baseline_fingerprint(
                    spec,
                    baseline,
                ),
                "watermark": max(cache["test_dates"]),
                "phase_a_cache": cache,
                "created_at": now,
                "updated_at": now,
            }
            cache_path = staging / relative_path
            _atomic_pickle_dump(cache_path, envelope)
            entries[baseline] = {
                "relative_path": str(relative_path),
                "file_sha256": _file_sha256(cache_path),
                "baseline_fingerprint": _baseline_fingerprint(
                    spec,
                    baseline,
                ),
                "cache_content_sha256": evidence[
                    "cache_content_sha256"
                ],
                "watermark": max(cache["test_dates"]),
                "evidence": evidence,
            }
            evidence_by_baseline[baseline] = evidence

        generation_basis = {
            "abi_version": PHASE_A_CACHE_ABI_VERSION,
            "cache_family": spec.cache_family,
            "tenor": spec.tenor,
            "spec_fingerprint": spec_fingerprint,
            "input_content_id": input_state["content_id"],
            "baseline_content": {
                baseline: {
                    "baseline_fingerprint": entry[
                        "baseline_fingerprint"
                    ],
                    "cache_content_sha256": entry[
                        "cache_content_sha256"
                    ],
                }
                for baseline, entry in sorted(entries.items())
            },
        }
        content_id = hashlib.sha256(
            _canonical_json(generation_basis).encode("utf-8")
        ).hexdigest()
        generation_acceptance = _build_generation_acceptance_evidence(
            native_generation_binding=native_generation_binding,
            input_content_id=str(input_state["content_id"]),
            parent_generation=parent_generation,
            candidate_content_id=content_id,
            build_mode=build_mode,
            input_change=input_change,
            scopes=acceptance_scopes,
        )
        generation_id = (
            f"generation-{content_id[:24]}-{uuid.uuid4().hex[:8]}"
        )
        _validate_compare_gate_evidence(
            compare_gate_evidence,
            evidence_by_baseline,
            qualification_binding=_compare_qualification_binding(
                cache_family=spec.cache_family,
                tenor=spec.tenor,
                abi_version=PHASE_A_CACHE_ABI_VERSION,
                spec_fingerprint=spec_fingerprint,
                input_content_id=str(input_state["content_id"]),
                cached_integrity=evidence_by_baseline,
            ),
        )
        manifest = {
            "schema_version": GENERATION_MANIFEST_SCHEMA_VERSION,
            "generation_id": generation_id,
            "generation_content_id": content_id,
            "abi_version": PHASE_A_CACHE_ABI_VERSION,
            "cache_family": spec.cache_family,
            "tenor": spec.tenor,
            "spec_fingerprint": spec_fingerprint,
            "input_state": dict(input_state),
            "parent_generation_id": (
                parent_generation.generation_id
                if parent_generation is not None
                else None
            ),
            "build_mode": build_mode,
            "input_change": _public_input_change(input_change),
            "created_at": _utc_now(),
            "baselines": entries,
            "compare_gate_evidence": dict(
                compare_gate_evidence
            ),
            "generation_acceptance_evidence":
                generation_acceptance,
        }
        _atomic_json_dump(staging / "manifest.json", manifest)
        validated = _validate_staged_generation(
            staging,
            secure=secure,
        )
        staged_bytes = _directory_size_bytes(staging)
        if staged_bytes > MAX_CACHE_FAMILY_BYTES:
            raise CacheCapacityError(
                "cache generation exceeds 512 MiB family limit"
            )
        if _available_disk_bytes(family_root) < GLOBAL_MIN_FREE_BYTES:
            raise CacheCapacityError(
                "cache generation would violate 2 GiB free-space reserve"
            )

        generation_root = family_root / "generations"
        generation_root.mkdir(exist_ok=True)
        generation_root_identity = None
        if secure:
            generation_root_identity = _require_secure_directory(
                generation_root,
                "cache generations root",
            )
            _require_unchanged_directory(
                family_root,
                family_identity,
                "cache family",
            )
        target = generation_root / generation_id
        if secure:
            try:
                os.lstat(target)
            except FileNotFoundError:
                pass
            else:
                raise ValueError(
                    "cache generation target already exists"
                )
            _require_unchanged_directory(
                generation_root,
                generation_root_identity,
                "cache generations root",
            )
        os.replace(staging, target)
        if secure:
            _require_unchanged_directory(
                generation_root,
                generation_root_identity,
                "cache generations root",
            )
            _require_secure_directory(
                target,
                "cache generation",
            )
        _fsync_directory(generation_root)
        finalized = True
        return _LoadedGeneration(
            generation_id=generation_id,
            path=target,
            manifest=validated.manifest,
            manifest_sha256=validated.manifest_sha256,
            caches=validated.caches,
        )
    finally:
        if not finalized and staging.exists():
            shutil.rmtree(staging)


def _load_current_generation(
    family_root: Path,
    *,
    secure: bool = False,
) -> tuple[_LoadedGeneration | None, str | None]:
    pointer_path = family_root / "current.json"
    if secure:
        try:
            os.lstat(pointer_path)
        except FileNotFoundError:
            return None, "no_current_generation"
        except OSError:
            return None, "current_generation_invalid"
    elif not pointer_path.is_file():
        return None, "no_current_generation"
    try:
        pointer_bytes = (
            _secure_read_regular_file(
                pointer_path,
                "cache current pointer",
                max_bytes=1024 * 1024,
            )
            if secure
            else pointer_path.read_bytes()
        )
        pointer = json.loads(pointer_bytes.decode("utf-8"))
        if (
            not isinstance(pointer, dict)
            or pointer.get("schema_version")
            != CURRENT_POINTER_SCHEMA_VERSION
        ):
            raise ValueError("invalid current pointer schema")
        generation_id = str(pointer["generation_id"])
        manifest_sha256 = str(pointer["manifest_sha256"])
        generation = _load_generation_directory(
            family_root / "generations" / generation_id,
            expected_generation_id=generation_id,
            expected_manifest_sha256=manifest_sha256,
            secure=secure,
        )
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        pickle.PickleError,
    ):
        return None, "current_generation_invalid"
    return generation, None


def _validate_staged_generation(
    staging: Path,
    *,
    secure: bool = False,
) -> _LoadedGeneration:
    return _load_generation_directory(staging, secure=secure)


def _load_generation_directory(
    generation_path: Path,
    *,
    expected_generation_id: str | None = None,
    expected_manifest_sha256: str | None = None,
    secure: bool = False,
) -> _LoadedGeneration:
    if secure:
        _require_secure_generation_ancestry(generation_path)
    generation_identity = (
        _require_secure_directory(
            generation_path,
            "cache generation",
        )
        if secure
        else None
    )
    manifest_path = generation_path / "manifest.json"
    manifest_bytes = (
        _secure_read_regular_file(
            manifest_path,
            "cache generation manifest",
            max_bytes=8 * 1024 * 1024,
        )
        if secure
        else manifest_path.read_bytes()
    )
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if (
        expected_manifest_sha256 is not None
        and manifest_sha256 != expected_manifest_sha256
    ):
        raise ValueError("cache generation manifest digest mismatch")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version")
        != GENERATION_MANIFEST_SCHEMA_VERSION
    ):
        raise ValueError("invalid cache generation manifest")
    generation_id = str(manifest.get("generation_id") or "")
    if not generation_id:
        raise ValueError("cache generation_id is missing")
    if (
        expected_generation_id is not None
        and generation_id != expected_generation_id
    ):
        raise ValueError("cache generation_id mismatch")
    entries = manifest.get("baselines")
    if not isinstance(entries, Mapping) or not entries:
        raise ValueError("cache generation baselines are missing")
    input_state = _validate_input_generation_state_record(
        manifest.get("input_state")
    )
    baseline_directory_identity = (
        _require_secure_directory(
            generation_path / "baselines",
            "cache baselines directory",
        )
        if secure
        else None
    )

    caches: dict[str, dict[str, Any]] = {}
    evidence_by_baseline: dict[str, dict[str, Any]] = {}
    for baseline, raw_entry in entries.items():
        if not isinstance(baseline, str) or not isinstance(
            raw_entry,
            Mapping,
        ):
            raise ValueError("invalid cache generation baseline entry")
        expected_relative = (
            Path("baselines")
            / f"{safe_path_part(baseline)}.pkl"
        )
        if raw_entry.get("relative_path") != str(expected_relative):
            raise ValueError("cache baseline path is not canonical")
        cache_path = generation_path / expected_relative
        cache_bytes = (
            _secure_read_regular_file(
                cache_path,
                f"cache baseline {baseline}",
                max_bytes=MAX_CACHE_FAMILY_BYTES,
            )
            if secure
            else cache_path.read_bytes()
        )
        if (
            hashlib.sha256(cache_bytes).hexdigest()
            != raw_entry.get("file_sha256")
        ):
            raise ValueError("cache baseline file digest mismatch")
        envelope = pickle.loads(cache_bytes)
        if (
            not isinstance(envelope, dict)
            or envelope.get("schema_version") != CACHE_SCHEMA_VERSION
        ):
            raise ValueError("invalid cache baseline envelope")
        cache = _validate_phase_a_cache(envelope.get("phase_a_cache"))
        evidence = _phase_a_cache_evidence(cache)
        if (
            evidence["cache_content_sha256"]
            != raw_entry.get("cache_content_sha256")
        ):
            raise ValueError("cache baseline content digest mismatch")
        if evidence != raw_entry.get("evidence"):
            raise ValueError("cache integrity evidence mismatch")
        caches[baseline] = cache
        evidence_by_baseline[baseline] = evidence
    generation_basis = {
        "abi_version": manifest.get("abi_version"),
        "cache_family": manifest.get("cache_family"),
        "tenor": manifest.get("tenor"),
        "spec_fingerprint": manifest.get("spec_fingerprint"),
        "input_content_id": input_state["content_id"],
        "baseline_content": {
            baseline: {
                "baseline_fingerprint": entry[
                    "baseline_fingerprint"
                ],
                "cache_content_sha256": entry[
                    "cache_content_sha256"
                ],
            }
            for baseline, entry in sorted(entries.items())
        },
    }
    actual_content_id = hashlib.sha256(
        _canonical_json(generation_basis).encode("utf-8")
    ).hexdigest()
    if manifest.get("generation_content_id") != actual_content_id:
        raise ValueError("cache generation content digest mismatch")
    _validate_compare_gate_evidence(
        manifest.get("compare_gate_evidence"),
        evidence_by_baseline,
        qualification_binding=_compare_qualification_binding(
            cache_family=str(manifest.get("cache_family") or ""),
            tenor=str(manifest.get("tenor") or ""),
            abi_version=str(manifest.get("abi_version") or ""),
            spec_fingerprint=str(
                manifest.get("spec_fingerprint") or ""
            ),
            input_content_id=str(
                (
                    manifest.get("input_state")
                    if isinstance(
                        manifest.get("input_state"),
                        Mapping,
                    )
                    else {}
                ).get("content_id")
                or ""
            ),
            cached_integrity=evidence_by_baseline,
        ),
    )
    _validate_generation_acceptance_evidence(
        manifest.get("generation_acceptance_evidence"),
        caches=caches,
        candidate_content_id=str(
            manifest.get("generation_content_id") or ""
        ),
        input_state=input_state,
    )
    if secure:
        _require_unchanged_directory(
            generation_path / "baselines",
            baseline_directory_identity,
            "cache baselines directory",
        )
        _require_unchanged_directory(
            generation_path,
            generation_identity,
            "cache generation",
        )
    return _LoadedGeneration(
        generation_id=generation_id,
        path=generation_path,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        caches=caches,
    )


def _phase_a_cache_evidence(
    cache: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = _validate_phase_a_cache(cache)
    payloads = {
        "test_dates": normalized["test_dates"],
        "results[].config": [
            item["config"] for item in normalized["results"]
        ],
        "results[].preds": [
            item["preds"].tolist() for item in normalized["results"]
        ],
        "results[].probs": [
            item["probs"].tolist() for item in normalized["results"]
        ],
    }
    field_sha256 = {
        field: hashlib.sha256(
            _canonical_json(value).encode("utf-8")
        ).hexdigest()
        for field, value in payloads.items()
    }
    return {
        "cache_content_sha256": hashlib.sha256(
            _canonical_json(payloads).encode("utf-8")
        ).hexdigest(),
        "field_sha256": field_sha256,
        "test_date_count": len(normalized["test_dates"]),
        "result_config_count": len(normalized["results"]),
    }


def _generation_acceptance_scope(
    *,
    parent_cache: Mapping[str, Any] | None,
    candidate_cache: Mapping[str, Any],
    authoritative_cache: Mapping[str, Any] | None,
    affected_dates: list[str],
    preserve_parent: bool = True,
) -> dict[str, Any]:
    """用本次唯一 train 产物验证 merge 后 affected scope。"""
    normalized_dates = [str(day) for day in affected_dates]
    if len(normalized_dates) != len(set(normalized_dates)):
        raise ValueError(
            "generation acceptance affected dates must be unique"
        )
    candidate_scope_sha = _phase_a_scope_sha256(
        candidate_cache,
        normalized_dates,
    )
    if authoritative_cache is None:
        if normalized_dates:
            raise ValueError(
                "generation acceptance authoritative scope is missing"
            )
        authoritative_scope_sha = None
    else:
        authoritative_scope_sha = _phase_a_scope_sha256(
            authoritative_cache,
            normalized_dates,
        )
        if authoritative_scope_sha != candidate_scope_sha:
            raise ValueError(
                "generation acceptance merge changed authoritative scope"
            )
    parent = (
        _validate_phase_a_cache(parent_cache)
        if parent_cache is not None
        else None
    )
    preserved_dates = (
        [
            day
            for day in parent["test_dates"]
            if day not in set(normalized_dates)
        ]
        if parent is not None and preserve_parent
        else []
    )
    parent_preserved_sha = (
        _phase_a_scope_sha256(parent, preserved_dates)
        if preserved_dates
        else None
    )
    candidate_preserved_sha = (
        _phase_a_scope_sha256(candidate_cache, preserved_dates)
        if preserved_dates
        else None
    )
    if parent_preserved_sha != candidate_preserved_sha:
        raise ValueError(
            "generation acceptance changed preserved prefix"
        )
    return {
        "affected_dates": normalized_dates,
        "authoritative_scope_sha256": authoritative_scope_sha,
        "candidate_scope_sha256": candidate_scope_sha,
        "preserved_dates": preserved_dates,
        "parent_preserved_sha256": parent_preserved_sha,
        "candidate_preserved_sha256": candidate_preserved_sha,
        "scope_equal": True,
        "preserved_equal": True,
    }


def _phase_a_scope_sha256(
    cache: Mapping[str, Any],
    dates: list[str],
) -> str:
    normalized = _validate_phase_a_cache(cache)
    indexes_by_date = {
        day: index
        for index, day in enumerate(normalized["test_dates"])
    }
    missing = sorted(set(dates) - set(indexes_by_date))
    if missing:
        raise ValueError(
            "generation acceptance scope is absent from cache: "
            + ", ".join(missing)
        )
    indexes = [indexes_by_date[day] for day in dates]
    payload = {
        "test_dates": dates,
        "results": [
            {
                "config": item["config"],
                "preds": [
                    int(item["preds"][index]) for index in indexes
                ],
                "probs": [
                    float(item["probs"][index]) for index in indexes
                ],
            }
            for item in normalized["results"]
        ],
    }
    return hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()


def _build_generation_acceptance_evidence(
    *,
    native_generation_binding: Mapping[str, object] | None,
    input_content_id: str,
    parent_generation: _LoadedGeneration | None,
    candidate_content_id: str,
    build_mode: str,
    input_change: Mapping[str, object],
    scopes: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    parent = (
        {
            "generation_id": parent_generation.generation_id,
            "manifest_sha256": parent_generation.manifest_sha256,
            "generation_content_id": parent_generation.manifest[
                "generation_content_id"
            ],
        }
        if parent_generation is not None
        else None
    )
    payload: dict[str, object] = {
        "schema_version": GENERATION_ACCEPTANCE_SCHEMA_VERSION,
        "status": (
            "ACCEPTED"
            if native_generation_binding is not None
            else "NON_PRODUCTION"
        ),
        "native_generation": (
            dict(native_generation_binding)
            if native_generation_binding is not None
            else None
        ),
        "input_content_id": input_content_id,
        "parent": parent,
        "candidate_content_id": candidate_content_id,
        "build_mode": build_mode,
        "input_change": _public_input_change(input_change),
        "baselines": {
            str(name): dict(value)
            for name, value in sorted(scopes.items())
        },
    }
    evidence_sha = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()
    return {**payload, "evidence_sha256": evidence_sha}


def _validate_generation_acceptance_evidence(
    raw: Any,
    *,
    caches: Mapping[str, Mapping[str, Any]],
    candidate_content_id: str,
    input_state: Mapping[str, object],
) -> None:
    if not isinstance(raw, Mapping):
        raise ValueError(
            "cache generation acceptance evidence is missing"
        )
    validated_record = validate_generation_acceptance_record(raw)
    raw = validated_record
    fields = {
        "schema_version",
        "status",
        "native_generation",
        "input_content_id",
        "parent",
        "candidate_content_id",
        "build_mode",
        "input_change",
        "baselines",
        "evidence_sha256",
    }
    if set(raw) != fields:
        raise ValueError(
            "cache generation acceptance evidence fields mismatch"
        )
    if (
        raw.get("schema_version")
        != GENERATION_ACCEPTANCE_SCHEMA_VERSION
    ):
        raise ValueError(
            "cache generation acceptance schema mismatch"
        )
    payload = {
        key: value
        for key, value in raw.items()
        if key != "evidence_sha256"
    }
    expected_sha = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()
    if raw.get("evidence_sha256") != expected_sha:
        raise ValueError(
            "cache generation acceptance evidence digest mismatch"
        )
    if raw.get("candidate_content_id") != candidate_content_id:
        raise ValueError(
            "cache generation acceptance candidate hash mismatch"
        )
    if raw.get("input_content_id") != input_state.get("content_id"):
        raise ValueError(
            "cache generation acceptance input hash mismatch"
        )
    native = raw.get("native_generation")
    if native is None:
        if raw.get("status") != "NON_PRODUCTION":
            raise ValueError(
                "unbound cache generation cannot be production accepted"
            )
    else:
        validated_native = _validate_native_generation_binding(
            native
        )
        if (
            raw.get("status") != "ACCEPTED"
            or validated_native != input_state.get("native_generation")
        ):
            raise ValueError(
                "cache generation acceptance Native binding mismatch"
            )
    baselines = raw.get("baselines")
    if (
        not isinstance(baselines, Mapping)
        or set(baselines) != set(caches)
    ):
        raise ValueError(
            "cache generation acceptance baseline set mismatch"
        )
    scope_fields = {
        "affected_dates",
        "authoritative_scope_sha256",
        "candidate_scope_sha256",
        "preserved_dates",
        "parent_preserved_sha256",
        "candidate_preserved_sha256",
        "scope_equal",
        "preserved_equal",
    }
    for baseline, cache in caches.items():
        scope = baselines.get(baseline)
        if not isinstance(scope, Mapping) or set(scope) != scope_fields:
            raise ValueError(
                "cache generation acceptance scope fields mismatch"
            )
        affected = scope.get("affected_dates")
        preserved = scope.get("preserved_dates")
        if (
            not isinstance(affected, list)
            or not isinstance(preserved, list)
            or any(not isinstance(day, str) for day in (*affected, *preserved))
        ):
            raise ValueError(
                "cache generation acceptance dates are invalid"
            )
        candidate_scope_sha = _phase_a_scope_sha256(
            cache,
            affected,
        )
        if scope.get("candidate_scope_sha256") != candidate_scope_sha:
            raise ValueError(
                "serialized cache changed accepted affected scope"
            )
        authoritative_sha = scope.get(
            "authoritative_scope_sha256"
        )
        if affected:
            if authoritative_sha != candidate_scope_sha:
                raise ValueError(
                    "cache authoritative affected scope mismatch"
                )
        elif authoritative_sha is not None:
            raise ValueError(
                "empty cache scope cannot claim authoritative training"
            )
        candidate_preserved_sha = (
            _phase_a_scope_sha256(cache, preserved)
            if preserved
            else None
        )
        if (
            scope.get("candidate_preserved_sha256")
            != candidate_preserved_sha
            or scope.get("parent_preserved_sha256")
            != candidate_preserved_sha
            or scope.get("scope_equal") is not True
            or scope.get("preserved_equal") is not True
        ):
            raise ValueError(
                "cache generation preserved prefix mismatch"
            )


def _validate_generation_acceptance_for_use(
    generation: _LoadedGeneration,
    *,
    native_generation_binding: Mapping[str, object] | None,
) -> None:
    if native_generation_binding is None:
        raise RuntimeError(
            "Native generation binding is required for cache use"
        )
    evidence = generation.manifest.get(
        "generation_acceptance_evidence"
    )
    if (
        not isinstance(evidence, Mapping)
        or evidence.get("status") != "ACCEPTED"
        or evidence.get("native_generation")
        != dict(native_generation_binding)
    ):
        raise RuntimeError(
            "cache generation acceptance does not match current Native "
            "generation"
        )


def _verify_generation_acceptance_lineage(
    generation: _LoadedGeneration,
    *,
    trusted_qualification: Mapping[str, object],
) -> None:
    """从真实 parent 文件重算 diff、build mode 与全部 scope。"""
    qualification = trusted_qualification["qualification"]
    manifest = generation.manifest
    if (
        manifest.get("abi_version") != qualification["cache_abi_version"]
        or manifest.get("cache_family") != qualification["cache_family"]
        or manifest.get("tenor") != qualification["tenor"]
        or manifest.get("spec_fingerprint")
        != qualification["spec_fingerprint"]
    ):
        raise ValueError(
            "cache generation does not match qualified cache identity"
        )
    acceptance = validate_generation_acceptance_record(
        manifest.get("generation_acceptance_evidence"),
    )
    parent_record = acceptance.get("parent")
    parent_generation_id = manifest.get("parent_generation_id")
    parent: _LoadedGeneration | None
    if parent_record is None:
        if parent_generation_id is not None:
            raise ValueError(
                "cache generation parent linkage is inconsistent"
            )
        parent = None
    else:
        if (
            not isinstance(parent_generation_id, str)
            or parent_generation_id != parent_record["generation_id"]
            or safe_path_part(parent_generation_id)
            != parent_generation_id
        ):
            raise ValueError(
                "cache generation parent identity mismatch"
            )
        parent = _load_generation_directory(
            generation.path.parent / parent_generation_id,
            expected_generation_id=parent_generation_id,
            expected_manifest_sha256=parent_record["manifest_sha256"],
            secure=True,
        )
        if (
            parent.manifest.get("generation_content_id")
            != parent_record["generation_content_id"]
        ):
            raise ValueError(
                "cache generation parent content digest mismatch"
            )

    previous_input = (
        parent.manifest.get("input_state")
        if parent is not None
        else None
    )
    current_input = generation.manifest.get("input_state")
    if not isinstance(current_input, Mapping):
        raise ValueError("cache generation input state is missing")
    recomputed_change = _input_change_analysis(
        previous_input,
        current_input,
    )
    expected_mode = _lineage_build_mode(
        parent=parent,
        generation=generation,
        input_change=recomputed_change,
        qualification=qualification,
    )
    public_change = _public_input_change(recomputed_change)
    if (
        acceptance.get("input_change") != public_change
        or manifest.get("input_change") != public_change
    ):
        raise ValueError(
            "cache generation input diff does not match lineage"
        )
    if (
        acceptance.get("build_mode") != expected_mode
        or manifest.get("build_mode") != expected_mode
    ):
        raise ValueError(
            "cache generation build mode does not match input diff"
        )

    scopes = acceptance["baselines"]
    if set(scopes) != set(generation.caches):
        raise ValueError(
            "cache generation acceptance baseline set mismatch"
        )
    for baseline, candidate_cache in generation.caches.items():
        candidate_dates = list(candidate_cache["test_dates"])
        parent_cache = (
            parent.caches.get(baseline)
            if parent is not None
            else None
        )
        parent_dates = (
            list(parent_cache["test_dates"])
            if parent_cache is not None
            else []
        )
        if expected_mode == "full":
            affected_dates = candidate_dates
            preserved_dates: list[str] = []
        elif expected_mode == "suffix":
            suffix_start = public_change.get("suffix_start_date")
            if not isinstance(suffix_start, str):
                raise ValueError(
                    "cache generation suffix input diff is incomplete"
                )
            affected_dates = [
                day for day in candidate_dates if day >= suffix_start
            ]
            preserved_dates = [
                day for day in parent_dates if day < suffix_start
            ]
        else:
            affected_dates = [
                day for day in candidate_dates if day not in set(parent_dates)
            ]
            preserved_dates = [
                day for day in parent_dates if day not in set(affected_dates)
            ]
        scope = scopes[baseline]
        if (
            scope.get("affected_dates") != affected_dates
            or scope.get("preserved_dates") != preserved_dates
        ):
            raise ValueError(
                f"cache generation {baseline} recompute scope mismatch"
            )
        expected_parent_sha = (
            _phase_a_scope_sha256(parent_cache, preserved_dates)
            if parent_cache is not None and preserved_dates
            else None
        )
        if scope.get("parent_preserved_sha256") != expected_parent_sha:
            raise ValueError(
                f"cache generation {baseline} parent scope mismatch"
            )


def _lineage_build_mode(
    *,
    parent: _LoadedGeneration | None,
    generation: _LoadedGeneration,
    input_change: dict[str, Any],
    qualification: Mapping[str, object],
) -> str:
    """仅凭冻结 qualification 与前后 manifest 重放 build-mode 决策。"""
    if parent is None:
        return "full"
    manifest = generation.manifest
    if (
        parent.manifest.get("spec_fingerprint")
        != manifest.get("spec_fingerprint")
        or set(parent.caches) != set(generation.caches)
    ):
        return "full"
    frames = input_change["frames"]
    projection_status = input_change.get("projection_status")
    if projection_status != "absent":
        if projection_status != "valid":
            return "full"
        daily = frames["daily"]
        effective = input_change["effective_auxiliary"]
        effective_cutoff: str | None = None
        if effective["change_type"] == "revision":
            effective_cutoff = effective["earliest_changed_key"]
            if (
                effective["schema_changed"]
                or not isinstance(effective_cutoff, str)
            ):
                return "full"
        elif effective["change_type"] not in {"unchanged", "append"}:
            return "full"
        if daily["change_type"] in {"revision", "unknown"}:
            rows = qualification.get("daily_dependency_lookback_rows")
            proof = qualification.get("daily_dependency_proof")
            earliest = daily["earliest_changed_key"]
            union_keys = input_change.get("_daily_union_keys")
            if (
                daily["change_type"] != "revision"
                or daily["schema_changed"]
                or isinstance(rows, bool)
                or not isinstance(rows, int)
                or rows < 0
                or not isinstance(proof, str)
                or not proof.strip()
                or not isinstance(earliest, str)
                or not isinstance(union_keys, list)
                or earliest not in union_keys
            ):
                return "full"
            position = union_keys.index(earliest)
            daily_cutoff = union_keys[max(0, position - rows)]
            if not isinstance(daily_cutoff, str):
                return "full"
            input_change["suffix_start_date"] = min(
                daily_cutoff,
                effective_cutoff or daily_cutoff,
            )
            return "suffix"
        if effective_cutoff is not None:
            input_change["suffix_start_date"] = effective_cutoff
            return "suffix"
        if input_change["native_generation_changed"]:
            return "rebind"
        return "append"
    else:
        if any(
            frames[name]["change_type"] in {"revision", "unknown"}
            for name in ("weekly", "monthly")
        ):
            return "full"
        if any(
            frames[name]["change_type"] == "append"
            for name in ("weekly", "monthly")
        ):
            return "full"
    if input_change["native_generation_changed"] and (
        input_change["change_type"] in {"unchanged", "append"}
    ):
        return "rebind"
    if input_change["change_type"] in {"unchanged", "append"}:
        return "append"
    daily = frames["daily"]
    rows = qualification.get("daily_dependency_lookback_rows")
    proof = qualification.get("daily_dependency_proof")
    if (
        daily["change_type"] != "revision"
        or daily["schema_changed"]
        or isinstance(rows, bool)
        or not isinstance(rows, int)
        or rows < 0
        or not isinstance(proof, str)
        or not proof.strip()
    ):
        return "full"
    earliest = daily["earliest_changed_key"]
    union_keys = input_change.get("_daily_union_keys")
    if (
        not isinstance(earliest, str)
        or not isinstance(union_keys, list)
        or earliest not in union_keys
    ):
        return "full"
    position = union_keys.index(earliest)
    cutoff = union_keys[max(0, position - rows)]
    if not isinstance(cutoff, str):
        return "full"
    input_change["suffix_start_date"] = cutoff
    return "suffix"


def _build_compare_gate_evidence(
    caches: Mapping[str, Mapping[str, Any]],
    *,
    compare_cold: (
        Callable[
            [str, tuple[tuple[str, str], ...]],
            Mapping[str, Any],
        ]
        | None
    ),
    qualify_compare_gate: (
        Callable[
            [Mapping[str, Any]],
            Mapping[str, Any],
        ]
        | None
    ),
    compare_full_output: (
        Callable[
            [Mapping[str, Mapping[str, Any]]],
            tuple[Any, Any],
        ]
        | None
    ),
    cache_family: str,
    tenor: str,
    spec_fingerprint: str,
    input_content_id: str,
) -> dict[str, Any]:
    baseline_evidence: dict[str, dict[str, Any]] = {}
    for baseline, raw_cache in caches.items():
        cache = _validate_phase_a_cache(raw_cache)
        cached_integrity = _phase_a_cache_evidence(cache)
        if compare_cold is None:
            baseline_evidence[baseline] = {
                "cached_integrity": cached_integrity,
                "cold_integrity": None,
                "fields_equal": None,
            }
            continue
        dates = [str(day) for day in cache["test_dates"]]
        cold = _validate_phase_a_cache(
            compare_cold(
                baseline,
                tuple((day, day) for day in dates),
            )
        )
        _require_exact_trained_dates(baseline, cold, dates)
        cold_integrity = _phase_a_cache_evidence(cold)
        if not _phase_a_caches_equal(cache, cold):
            raise ValueError(
                f"baseline {baseline} cold CompareGate mismatch"
            )
        baseline_evidence[baseline] = {
            "cached_integrity": cached_integrity,
            "cold_integrity": cold_integrity,
            "fields_equal": True,
        }
    cached_integrity_by_baseline = {
        baseline: _phase_a_cache_evidence(cache)
        for baseline, cache in caches.items()
    }
    qualification_binding = _compare_qualification_binding(
        cache_family=cache_family,
        tenor=tenor,
        abi_version=PHASE_A_CACHE_ABI_VERSION,
        spec_fingerprint=spec_fingerprint,
        input_content_id=input_content_id,
        cached_integrity=cached_integrity_by_baseline,
    )
    full_output_qualification = None
    if compare_full_output is not None:
        compared_outputs = compare_full_output(caches)
        if (
            not isinstance(compared_outputs, tuple)
            or len(compared_outputs) != 2
        ):
            raise TypeError(
                "compare_full_output must return "
                "(cached_output, cold_output)"
            )
        full_output_qualification = (
            _runtime_full_output_qualification(
                qualification_binding,
                cached_output=compared_outputs[0],
                cold_output=compared_outputs[1],
            )
        )
    elif qualify_compare_gate is not None:
        full_output_qualification = (
            _validate_full_output_qualification(
                qualify_compare_gate(
                    qualification_binding
                ),
                cached_integrity_by_baseline,
                qualification_binding=qualification_binding,
            )
        )
    if full_output_qualification is not None:
        qualification_status = "qualified"
        qualification_method = (
            "independent_cold_callback+full_output_evidence"
        )
        capacity_eligible = True
    elif compare_cold is not None:
        qualification_status = "phase_a_compared"
        qualification_method = "independent_cold_callback"
        capacity_eligible = False
    else:
        qualification_status = "unqualified"
        qualification_method = None
        capacity_eligible = False
    evidence = {
        "schema_version": COMPARE_GATE_EVIDENCE_VERSION,
        "comparison_fields": [
            "test_dates",
            "results[].config",
            "results[].preds",
            "results[].probs",
        ],
        "cold_compare_required": True,
        "qualification_status": qualification_status,
        "qualification_method": qualification_method,
        "capacity_eligible": capacity_eligible,
        "full_output_qualification": (
            full_output_qualification
        ),
        "baselines": baseline_evidence,
    }
    _validate_compare_gate_evidence(
        evidence,
        cached_integrity_by_baseline,
        qualification_binding=qualification_binding,
    )
    return evidence


def _validate_compare_gate_evidence(
    raw: Any,
    cached_integrity: Mapping[str, Mapping[str, Any]],
    *,
    qualification_binding: Mapping[str, Any],
) -> None:
    if not isinstance(raw, Mapping):
        raise ValueError("cache CompareGate qualification is missing")
    if set(raw) != {
        "schema_version",
        "comparison_fields",
        "cold_compare_required",
        "qualification_status",
        "qualification_method",
        "capacity_eligible",
        "full_output_qualification",
        "baselines",
    }:
        raise ValueError(
            "cache CompareGate qualification fields are invalid"
        )
    if raw.get("schema_version") != COMPARE_GATE_EVIDENCE_VERSION:
        raise ValueError("cache CompareGate qualification schema mismatch")
    if raw.get("comparison_fields") != [
        "test_dates",
        "results[].config",
        "results[].preds",
        "results[].probs",
    ]:
        raise ValueError("cache CompareGate comparison fields mismatch")
    if raw.get("cold_compare_required") is not True:
        raise ValueError("cache cold CompareGate must remain required")
    status = raw.get("qualification_status")
    method = raw.get("qualification_method")
    eligible = raw.get("capacity_eligible")
    full_output_qualification = raw.get(
        "full_output_qualification"
    )
    if status == "qualified":
        if (
            method
            != "independent_cold_callback+full_output_evidence"
            or eligible is not True
        ):
            raise ValueError(
                "qualified cache CompareGate evidence is invalid"
            )
        _validate_full_output_qualification(
            full_output_qualification,
            cached_integrity,
            qualification_binding=qualification_binding,
        )
    elif status == "phase_a_compared":
        if (
            method != "independent_cold_callback"
            or eligible is not False
            or full_output_qualification is not None
        ):
            raise ValueError(
                "phase-a-only CompareGate evidence is invalid"
            )
    elif status == "unqualified":
        if method is not None or eligible is not False:
            raise ValueError(
                "unqualified cache CompareGate evidence is invalid"
            )
        if full_output_qualification is not None:
            raise ValueError(
                "unqualified cache must not carry full output evidence"
            )
    else:
        raise ValueError(
            "cache CompareGate qualification status is invalid"
        )
    baselines = raw.get("baselines")
    if not isinstance(baselines, Mapping) or set(baselines) != set(
        cached_integrity
    ):
        raise ValueError("cache CompareGate baseline set mismatch")
    for baseline, expected_integrity in cached_integrity.items():
        item = baselines.get(baseline)
        if not isinstance(item, Mapping):
            raise ValueError(
                "cache CompareGate baseline evidence is invalid"
            )
        if item.get("cached_integrity") != expected_integrity:
            raise ValueError(
                "cache CompareGate cached integrity mismatch"
            )
        if status in {"qualified", "phase_a_compared"}:
            if (
                item.get("cold_integrity") != expected_integrity
                or item.get("fields_equal") is not True
            ):
                raise ValueError(
                    "cache CompareGate cold qualification mismatch"
                )
        elif (
            item.get("cold_integrity") is not None
            or item.get("fields_equal") is not None
        ):
            raise ValueError(
                "unqualified cache must not carry cold evidence"
            )


def _generation_is_compare_gate_qualified(
    generation: _LoadedGeneration,
) -> bool:
    evidence = generation.manifest.get("compare_gate_evidence")
    return (
        isinstance(evidence, Mapping)
        and evidence.get("qualification_status") == "qualified"
        and evidence.get("capacity_eligible") is True
    )


def _generation_compare_gate_status(
    generation: _LoadedGeneration,
) -> str:
    evidence = generation.manifest.get("compare_gate_evidence")
    if not isinstance(evidence, Mapping):
        return "unqualified"
    return str(evidence.get("qualification_status") or "unqualified")


def _runtime_full_output_qualification(
    qualification_binding: Mapping[str, Any],
    *,
    cached_output: Any,
    cold_output: Any,
) -> dict[str, Any]:
    """对生产 cached/cold 全输出做逐字段精确比较并生成绑定证据。"""
    _validate_runtime_full_output_scope(cached_output)
    _validate_runtime_full_output_scope(cold_output)
    cached_payload = _normalize_full_output(cached_output)
    cold_payload = _normalize_full_output(cold_output)
    cached_json = _canonical_json(cached_payload)
    cold_json = _canonical_json(cold_payload)
    if cached_json != cold_json:
        raise ValueError(
            "full output CompareGate mismatch between cached and cold run"
        )
    binding_sha256 = hashlib.sha256(
        _canonical_json(qualification_binding).encode("utf-8")
    ).hexdigest()
    output_sha256 = hashlib.sha256(
        cached_json.encode("utf-8")
    ).hexdigest()
    evidence_payload = {
        "qualification_binding_sha256": binding_sha256,
        "comparison_fields": list(FULL_COMPARE_FIELDS),
        "full_output_sha256": output_sha256,
        "comparison": "exact",
    }
    evidence_sha256 = hashlib.sha256(
        _canonical_json(evidence_payload).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": FULL_COMPARE_QUALIFICATION_VERSION,
        "status": "passed",
        "qualification_id": (
            f"runtime-full-output-{evidence_sha256[:24]}"
        ),
        "verifier_version": (
            "liwei-0616-runtime-full-output-exact-v1"
        ),
        "evidence_uri": (
            f"embedded://phase-a-compare-gate/{evidence_sha256}"
        ),
        "evidence_sha256": evidence_sha256,
        "qualification_binding_sha256": binding_sha256,
        "comparison_fields": list(FULL_COMPARE_FIELDS),
        "cache_content_sha256": dict(
            qualification_binding["cache_content_sha256"]
        ),
    }


def _validate_runtime_full_output_scope(value: Any) -> None:
    """确认 runtime CompareGate 覆盖方向、投票与全部 baseline 内部值。"""
    if (
        not isinstance(value, pd.DataFrame)
        or value.empty
        or value.columns.has_duplicates
    ):
        raise ValueError(
            "full output CompareGate scope is incomplete"
        )
    columns = {str(column) for column in value.columns}
    required_groups = (
        frozenset({"direction", "prediction"}),
        frozenset({"vote_score"}),
        frozenset({"baseline_scores"}),
        frozenset({"baseline_signs"}),
        frozenset({"probability", "confidence"}),
    )
    if any(not columns.intersection(group) for group in required_groups):
        raise ValueError(
            "full output CompareGate scope is incomplete"
        )
    for field in ("baseline_scores", "baseline_signs"):
        if any(
            not isinstance(item, Mapping) or not item
            for item in value[field].tolist()
        ):
            raise ValueError(
                "full output CompareGate scope is incomplete"
            )


def _normalize_full_output(value: Any) -> Any:
    """把全输出转换为保留顺序、dtype、缺失值语义的稳定载荷。"""
    if isinstance(value, pd.DataFrame):
        return {
            "__type__": "dataframe",
            "columns": [
                _normalize_full_output(column)
                for column in value.columns.tolist()
            ],
            "dtypes": [str(dtype) for dtype in value.dtypes.tolist()],
            "index": [
                _normalize_full_output(item)
                for item in value.index.tolist()
            ],
            "rows": [
                [
                    _normalize_full_output(item)
                    for item in row
                ]
                for row in value.itertuples(
                    index=False,
                    name=None,
                )
            ],
        }
    if isinstance(value, pd.Series):
        return {
            "__type__": "series",
            "name": _normalize_full_output(value.name),
            "dtype": str(value.dtype),
            "index": [
                _normalize_full_output(item)
                for item in value.index.tolist()
            ],
            "values": [
                _normalize_full_output(item)
                for item in value.tolist()
            ],
        }
    if isinstance(value, np.ndarray):
        return {
            "__type__": "ndarray",
            "dtype": str(value.dtype),
            "shape": list(value.shape),
            "values": _normalize_full_output(value.tolist()),
        }
    if isinstance(value, np.generic):
        return _normalize_full_output(value.item())
    if value is pd.NA:
        return {"__type__": "pd.NA"}
    if value is pd.NaT:
        return {"__type__": "pd.NaT"}
    if isinstance(value, float):
        if np.isnan(value):
            return {"__type__": "float", "value": "nan"}
        if np.isposinf(value):
            return {"__type__": "float", "value": "+inf"}
        if np.isneginf(value):
            return {"__type__": "float", "value": "-inf"}
        return value
    if isinstance(value, pd.Timestamp):
        return {
            "__type__": "timestamp",
            "value": value.isoformat(),
        }
    if isinstance(value, datetime):
        return {
            "__type__": "datetime",
            "value": value.isoformat(),
        }
    if isinstance(value, date):
        return {
            "__type__": "date",
            "value": value.isoformat(),
        }
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if normalized_key in normalized:
                raise TypeError(
                    "full output mapping contains colliding keys"
                )
            normalized[normalized_key] = _normalize_full_output(item)
        return normalized
    if isinstance(value, tuple):
        return {
            "__type__": "tuple",
            "values": [
                _normalize_full_output(item) for item in value
            ],
        }
    if isinstance(value, list):
        return [
            _normalize_full_output(item) for item in value
        ]
    if isinstance(value, Path):
        return {"__type__": "path", "value": str(value)}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise TypeError(
        "unsupported full output CompareGate value: "
        f"{type(value).__name__}"
    )


def _validate_full_output_qualification(
    raw: Any,
    cached_integrity: Mapping[str, Mapping[str, Any]],
    *,
    qualification_binding: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(
            "full output CompareGate qualification is missing"
        )
    required_keys = {
        "schema_version",
        "status",
        "qualification_id",
        "verifier_version",
        "evidence_uri",
        "evidence_sha256",
        "qualification_binding_sha256",
        "comparison_fields",
        "cache_content_sha256",
    }
    if set(raw) != required_keys:
        raise ValueError(
            "full output CompareGate qualification fields are invalid"
        )
    if raw.get("schema_version") != FULL_COMPARE_QUALIFICATION_VERSION:
        raise ValueError(
            "full output CompareGate qualification schema mismatch"
        )
    if raw.get("status") != "passed":
        raise ValueError(
            "full output CompareGate qualification did not pass"
        )
    for field in (
        "qualification_id",
        "verifier_version",
        "evidence_uri",
    ):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"full output CompareGate {field} is required"
            )
    evidence_sha256 = raw.get("evidence_sha256")
    if (
        not isinstance(evidence_sha256, str)
        or len(evidence_sha256) != 64
        or any(
            char not in "0123456789abcdef"
            for char in evidence_sha256.lower()
        )
    ):
        raise ValueError(
            "full output CompareGate evidence_sha256 is invalid"
        )
    if raw.get("comparison_fields") != list(
        FULL_COMPARE_FIELDS
    ):
        raise ValueError(
            "full output CompareGate comparison scope is incomplete"
        )
    expected_content = {
        baseline: evidence["cache_content_sha256"]
        for baseline, evidence in cached_integrity.items()
    }
    if raw.get("cache_content_sha256") != expected_content:
        raise ValueError(
            "full output CompareGate evidence is not bound to cache"
        )
    expected_binding_sha256 = hashlib.sha256(
        _canonical_json(qualification_binding).encode("utf-8")
    ).hexdigest()
    if (
        raw.get("qualification_binding_sha256")
        != expected_binding_sha256
    ):
        raise ValueError(
            "full output CompareGate evidence is not bound to this "
            "input generation"
        )
    return {
        "schema_version": FULL_COMPARE_QUALIFICATION_VERSION,
        "status": "passed",
        "qualification_id": str(raw["qualification_id"]),
        "verifier_version": str(raw["verifier_version"]),
        "evidence_uri": str(raw["evidence_uri"]),
        "evidence_sha256": evidence_sha256.lower(),
        "qualification_binding_sha256": (
            expected_binding_sha256
        ),
        "comparison_fields": list(FULL_COMPARE_FIELDS),
        "cache_content_sha256": expected_content,
    }


def _compare_qualification_binding(
    *,
    cache_family: str,
    tenor: str,
    abi_version: str,
    spec_fingerprint: str,
    input_content_id: str,
    cached_integrity: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    required_text = {
        "cache_family": cache_family,
        "tenor": tenor,
        "abi_version": abi_version,
        "spec_fingerprint": spec_fingerprint,
        "input_content_id": input_content_id,
    }
    if any(not str(value).strip() for value in required_text.values()):
        raise ValueError(
            "CompareGate qualification binding is incomplete"
        )
    return {
        "schema_version": COMPARE_QUALIFICATION_BINDING_VERSION,
        **required_text,
        "cache_content_sha256": {
            baseline: evidence["cache_content_sha256"]
            for baseline, evidence in cached_integrity.items()
        },
    }


def _phase_a_caches_equal(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    first = _validate_phase_a_cache(left)
    second = _validate_phase_a_cache(right)
    if first["test_dates"] != second["test_dates"]:
        return False
    if len(first["results"]) != len(second["results"]):
        return False
    for first_result, second_result in zip(
        first["results"],
        second["results"],
        strict=True,
    ):
        if _canonical_json(first_result["config"]) != _canonical_json(
            second_result["config"]
        ):
            return False
        if not np.array_equal(
            first_result["preds"],
            second_result["preds"],
        ):
            return False
        if not np.array_equal(
            first_result["probs"],
            second_result["probs"],
            equal_nan=True,
        ):
            return False
    return True


def _public_input_change(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        str(key): item
        for key, item in value.items()
        if not str(key).startswith("_")
    }


def validate_phase_a_cache_input_change_audit(
    value: Any,
) -> dict[str, Any]:
    """闭世界校验 Phase A audit 的公开 input-change 结构。"""
    fields = {
        "change_type",
        "raw_change_type",
        "frames",
        "effective_auxiliary",
        "date_to_week",
        "projection_status",
        "suffix_start_date",
        "native_generation_changed",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("cache input-change audit fields mismatch")
    aggregate_types = {
        "initial",
        "unchanged",
        "append",
        "revision",
        "unknown",
    }
    for field in ("change_type", "raw_change_type"):
        if value.get(field) not in aggregate_types:
            raise ValueError(
                f"cache input-change audit {field} is invalid"
            )
    frames = value.get("frames")
    if not isinstance(frames, Mapping) or set(frames) != {
        "daily",
        "weekly",
        "monthly",
    }:
        raise ValueError("cache input-change audit frame set mismatch")
    normalized_frames = {
        name: _validate_input_change_frame_audit(
            frames[name],
            label=f"frames.{name}",
            allow_unavailable=False,
            require_schema_changed=True,
        )
        for name in ("daily", "weekly", "monthly")
    }
    effective = _validate_input_change_frame_audit(
        value.get("effective_auxiliary"),
        label="effective_auxiliary",
        allow_unavailable=True,
        require_schema_changed=True,
    )
    date_to_week = _validate_input_change_frame_audit(
        value.get("date_to_week"),
        label="date_to_week",
        allow_unavailable=True,
        require_schema_changed=False,
    )
    projection_status = value.get("projection_status")
    if projection_status not in {
        "absent",
        "schema_changed",
        "proof_changed",
        "mapping_changed",
        "valid",
        "missing_from_parent",
        "missing_current",
    }:
        raise ValueError(
            "cache input-change audit projection_status is invalid"
        )
    suffix_start = value.get("suffix_start_date")
    if suffix_start is not None and (
        not isinstance(suffix_start, str) or not suffix_start
    ):
        raise ValueError(
            "cache input-change audit suffix_start_date is invalid"
        )
    native_changed = value.get("native_generation_changed")
    if type(native_changed) is not bool:
        raise ValueError(
            "cache input-change audit native flag is invalid"
        )
    return {
        "change_type": value["change_type"],
        "raw_change_type": value["raw_change_type"],
        "frames": normalized_frames,
        "effective_auxiliary": effective,
        "date_to_week": date_to_week,
        "projection_status": projection_status,
        "suffix_start_date": suffix_start,
        "native_generation_changed": native_changed,
    }


def _validate_input_change_frame_audit(
    value: Any,
    *,
    label: str,
    allow_unavailable: bool,
    require_schema_changed: bool,
) -> dict[str, Any]:
    fields = {"change_type", "earliest_changed_key"}
    if require_schema_changed:
        fields.add("schema_changed")
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(
            f"cache input-change audit {label} fields mismatch"
        )
    change_types = {
        "initial",
        "unchanged",
        "append",
        "revision",
        "unknown",
    }
    if allow_unavailable:
        change_types.add("unavailable")
    change_type = value.get("change_type")
    if change_type not in change_types:
        raise ValueError(
            f"cache input-change audit {label} type is invalid"
        )
    earliest = value.get("earliest_changed_key")
    if (
        earliest is not None
        and (
            isinstance(earliest, bool)
            or not isinstance(earliest, (str, int))
        )
    ):
        raise ValueError(
            f"cache input-change audit {label} earliest key is invalid"
        )
    normalized = {
        "change_type": change_type,
        "earliest_changed_key": earliest,
    }
    if require_schema_changed:
        schema_changed = value.get("schema_changed")
        if type(schema_changed) is not bool:
            raise ValueError(
                f"cache input-change audit {label} schema flag is invalid"
            )
        normalized["schema_changed"] = schema_changed
    return normalized


def _switch_current_generation(
    family_root: Path,
    generation: _LoadedGeneration,
    *,
    secure: bool = False,
) -> None:
    family_identity = (
        _require_secure_directory(family_root, "cache family")
        if secure
        else None
    )
    pointer_path = family_root / "current.json"
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    if secure:
        try:
            os.lstat(pointer_path)
        except FileNotFoundError:
            pass
        else:
            _secure_read_regular_file(
                pointer_path,
                "cache current pointer",
                max_bytes=1024 * 1024,
            )
    encoded = (
        _canonical_json(
            {
                "schema_version": CURRENT_POINTER_SCHEMA_VERSION,
                "generation_id": generation.generation_id,
                "manifest_sha256": generation.manifest_sha256,
                "switched_at": _utc_now(),
            }
        )
        + "\n"
    ).encode("utf-8")
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=pointer_path.parent,
            prefix=f".{pointer_path.name}.",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        json.loads(temp_path.read_text(encoding="utf-8"))
        if secure:
            _secure_read_regular_file(
                temp_path,
                "cache current pointer candidate",
                max_bytes=1024 * 1024,
            )
            _require_unchanged_directory(
                family_root,
                family_identity,
                "cache family",
            )
        os.replace(temp_path, pointer_path)
        temp_path = None
        if secure:
            _secure_read_regular_file(
                pointer_path,
                "cache current pointer",
                max_bytes=1024 * 1024,
            )
            _require_unchanged_directory(
                family_root,
                family_identity,
                "cache family",
            )
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    # replace 成功后 publication 已提交，后续 durability sync 失败不能
    # 再向调用方报告“失败”并诱发重复发布。文件自身已在 replace 前
    # fsync。
    try:
        _fsync_directory(pointer_path.parent)
    except OSError:
        return


def _generation_audit(
    *,
    spec: PhaseACacheSpec,
    generation: _LoadedGeneration,
    baseline_audits: Mapping[str, Mapping[str, Any]],
    status: str,
    build_mode: str,
    build_reason: str,
    family_root: Path,
    published: bool,
    input_state: Mapping[str, Any],
    input_change: Mapping[str, Any],
    trusted_qualification: Mapping[str, object] | None,
) -> dict[str, Any]:
    all_missing = sorted(
        {
            str(day)
            for item in baseline_audits.values()
            for day in item["missing_dates"]
        }
    )
    evidence = dict(generation.manifest["compare_gate_evidence"])
    evidence.update(
        {
            "generation_id": generation.generation_id,
            "generation_manifest_sha256": (
                generation.manifest_sha256
            ),
        }
    )
    acceptance = generation.manifest[
        "generation_acceptance_evidence"
    ]
    trusted_binding = (
        trusted_qualification_audit_binding(
            trusted_qualification
        )
        if trusted_qualification is not None
        else None
    )
    acceptance_audit = validate_generation_acceptance_record(
        acceptance
    )
    return {
        "status": status,
        "build_mode": build_mode,
        "build_reason": build_reason,
        "watermark": max(
            str(item["watermark"])
            for item in baseline_audits.values()
        ),
        "missing_dates": all_missing,
        "version": PHASE_A_CACHE_ABI_VERSION,
        "fingerprint": _audit_fingerprint(baseline_audits),
        "baselines": dict(baseline_audits),
        "cache_family": spec.cache_family,
        "tenor": spec.tenor,
        "generation_id": generation.generation_id,
        "generation_path": str(generation.path),
        "generation_manifest_sha256": generation.manifest_sha256,
        "input_content_id": generation.manifest["input_state"][
            "content_id"
        ],
        "consumer_input_equivalence_sha256":
            consumer_input_state_equivalence_sha256(input_state),
        "compare_gate_evidence": evidence,
        "input_change": _public_input_change(input_change),
        "family_root": str(family_root),
        "current_pointer": str(family_root / "current.json"),
        "published": published,
        "capacity_eligible": (
            trusted_binding is not None
            and acceptance["status"] == "ACCEPTED"
        ),
        "cache_use_qualification": trusted_binding,
        "generation_acceptance": acceptance_audit,
        "retention_limit": CACHE_GENERATION_RETENTION,
        "family_limit_bytes": MAX_CACHE_FAMILY_BYTES,
        "global_free_reserve_bytes": GLOBAL_MIN_FREE_BYTES,
    }


def _cleanup_staging_directories(
    family_root: Path,
    *,
    secure: bool = False,
) -> None:
    family_identity = (
        _require_secure_directory(family_root, "cache family")
        if secure
        else None
    )
    for path in family_root.glob(".building-*"):
        if secure:
            _require_secure_directory(path, "cache staging directory")
            _require_unchanged_directory(
                family_root,
                family_identity,
                "cache family",
            )
        if path.is_dir():
            shutil.rmtree(path)


def _discard_unpublished_generation(
    family_root: Path,
    generation_id: str,
    *,
    secure: bool = False,
) -> None:
    """删除未提交 candidate；绝不删除 current 指针引用的 generation。"""
    pointer_path = family_root / "current.json"
    pointer_exists = pointer_path.exists()
    if secure:
        try:
            os.lstat(pointer_path)
        except FileNotFoundError:
            pointer_exists = False
        else:
            pointer_exists = True
    if pointer_exists:
        try:
            pointer_bytes = (
                _secure_read_regular_file(
                    pointer_path,
                    "cache current pointer",
                    max_bytes=1024 * 1024,
                )
                if secure
                else pointer_path.read_bytes()
            )
            pointer = json.loads(pointer_bytes.decode("utf-8"))
            current_generation_id = str(
                pointer.get("generation_id") or ""
            )
        except (OSError, AttributeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "cannot prove cache candidate is unpublished"
            ) from error
        if current_generation_id == generation_id:
            raise RuntimeError(
                "refusing to delete the published current generation"
            )

    generation_root = family_root / "generations"
    candidate = generation_root / generation_id
    if candidate.exists():
        if secure:
            _require_secure_directory(
                generation_root,
                "cache generations root",
            )
            _require_secure_directory(
                candidate,
                "cache unpublished generation",
            )
        shutil.rmtree(candidate)
        _fsync_directory(generation_root)


def _prune_generations(
    family_root: Path,
    *,
    protected_generation_ids: set[str],
    secure: bool = False,
) -> None:
    """提交前清理历史 generation，同时保护 old current 与 candidate。"""
    generation_root = family_root / "generations"
    if not generation_root.is_dir():
        return
    if secure:
        _require_secure_directory(
            generation_root,
            "cache generations root",
        )
    protected = {
        str(generation_id)
        for generation_id in protected_generation_ids
        if str(generation_id)
    }
    if not protected:
        raise ValueError(
            "cache generation pruning requires protected generations"
        )
    generations = sorted(
        (
            path
            for path in generation_root.iterdir()
            if path.is_dir()
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )
    total_bytes = sum(_directory_size_bytes(path) for path in generations)
    while (
        len(generations) > CACHE_GENERATION_RETENTION
        or total_bytes > MAX_CACHE_FAMILY_BYTES
    ):
        removable = next(
            (
                path
                for path in generations
                if path.name not in protected
            ),
            None,
        )
        if removable is None:
            raise CacheCapacityError(
                "protected current and candidate cache generations "
                "cannot satisfy retention or family limit"
            )
        removed_bytes = _directory_size_bytes(removable)
        if secure:
            _require_secure_directory(
                removable,
                "cache removable generation",
            )
        shutil.rmtree(removable)
        generations.remove(removable)
        total_bytes -= removed_bytes
    _fsync_directory(generation_root)


def _directory_size_bytes(path: Path) -> int:
    return sum(
        item.stat().st_size
        for item in path.rglob("*")
        if item.is_file()
    )


def _available_disk_bytes(path: Path) -> int:
    return int(shutil.disk_usage(path).free)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_secure_directory(
    path: Path,
    label: str,
) -> tuple[int, int]:
    """要求 production cache 目录为当前 uid 持有的真实私有目录。"""
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
    ):
        raise ValueError(f"{label} must be a real directory")
    if metadata.st_uid != os.geteuid():
        raise ValueError(f"{label} owner mismatch")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise ValueError(f"{label} must not be group/world writable")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or not stat.S_ISDIR(opened.st_mode)
        ):
            raise ValueError(f"{label} changed while opening")
    finally:
        os.close(descriptor)
    return metadata.st_dev, metadata.st_ino


def _require_secure_generation_ancestry(path: Path) -> None:
    """校验 generation 到本 cache root 的所有可写控制目录。"""
    _require_secure_directory(path, "cache generation")
    if path.parent.name == "generations":
        generations_root = path.parent
        family_root = generations_root.parent
    else:
        # 原子发布前的 .building-* 位于 family root 之下。
        generations_root = None
        family_root = path.parent
    if generations_root is not None:
        _require_secure_directory(
            generations_root,
            "cache generations root",
        )
    _require_secure_directory(family_root, "cache family")
    _require_secure_directory(
        family_root.parent,
        "cache family namespace",
    )
    _require_secure_directory(
        family_root.parent.parent,
        "cache root",
    )


def _require_unchanged_directory(
    path: Path,
    identity: tuple[int, int] | None,
    label: str,
) -> None:
    if identity is None:
        return
    current = _require_secure_directory(path, label)
    if current != identity:
        raise ValueError(f"{label} inode changed during cache operation")


def _secure_read_regular_file(
    path: Path,
    label: str,
    *,
    max_bytes: int,
) -> bytes:
    """用 no-follow fd 读取并验证 uid/mode/大小/inode 未漂移。"""
    if max_bytes <= 0:
        raise ValueError("secure cache file max_bytes must be positive")
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
    ):
        raise ValueError(f"{label} must be a regular non-symlink file")
    if before.st_uid != os.geteuid():
        raise ValueError(f"{label} owner mismatch")
    if stat.S_IMODE(before.st_mode) & 0o022:
        raise ValueError(f"{label} must not be group/world writable")
    if before.st_size < 0 or before.st_size > max_bytes:
        raise ValueError(f"{label} exceeds size limit")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_uid != before.st_uid
            or opened.st_size != before.st_size
        ):
            raise ValueError(f"{label} changed while opening")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"{label} was truncated while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"{label} grew while reading")
        after_fd = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"{label} changed after reading") from exc
    identity = (before.st_dev, before.st_ino, before.st_size)
    if (
        (after_fd.st_dev, after_fd.st_ino, after_fd.st_size) != identity
        or (
            after_path.st_dev,
            after_path.st_ino,
            after_path.st_size,
        )
        != identity
        or not stat.S_ISREG(after_path.st_mode)
        or after_fd.st_uid != os.geteuid()
        or after_path.st_uid != os.geteuid()
        or stat.S_IMODE(after_fd.st_mode) & 0o022
        or stat.S_IMODE(after_path.st_mode) & 0o022
    ):
        raise ValueError(f"{label} changed while reading")
    return b"".join(chunks)


def _atomic_json_dump(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        _canonical_json(dict(payload)) + "\n"
    ).encode("utf-8")
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        json.loads(temp_path.read_text(encoding="utf-8"))
        os.replace(temp_path, path)
        temp_path = None
        _fsync_directory(path.parent)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _requested_dates(
    daily_df: pd.DataFrame,
    baseline_config: Mapping[str, Any],
    test_ranges: tuple[tuple[str, str], ...],
) -> list[str]:
    close_column = str(baseline_config.get("close") or "")
    if "date" not in daily_df.columns or close_column not in daily_df.columns:
        raise ValueError(f"daily input missing date or close column {close_column}")
    dates = pd.to_datetime(daily_df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    valid = dates.notna() & daily_df[close_column].notna()
    in_range = pd.Series(False, index=daily_df.index)
    for start, end in test_ranges:
        in_range |= dates.between(str(start), str(end), inclusive="both")
    return sorted(set(dates[valid & in_range].astype(str).tolist()))


def _max_daily_date(daily_df: pd.DataFrame) -> str:
    if "date" not in daily_df.columns:
        return ""
    dates = pd.to_datetime(daily_df["date"], errors="coerce").dropna()
    if dates.empty:
        return ""
    return dates.max().strftime("%Y-%m-%d")


def _baseline_fingerprint(spec: PhaseACacheSpec, baseline: str) -> str:
    payload = {
        "abi": PHASE_A_CACHE_ABI_VERSION,
        "cache_family": spec.cache_family,
        "tenor": spec.tenor,
        "baseline": baseline,
        "config": spec.baseline_configs[baseline],
        "source_ic_screen_start": spec.source_ic_screen_start,
        "horizon": spec.horizon,
        "purge_gap": spec.purge_gap,
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "lightgbm": _package_version("lightgbm"),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _audit_fingerprint(audits: Mapping[str, Mapping[str, Any]]) -> str:
    payload = {name: item["fingerprint"] for name, item in sorted(audits.items())}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def _validate_phase_a_cache(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("phase_a_cache must be a mapping")
    dates = [str(day) for day in value.get("test_dates", [])]
    if not dates or len(dates) != len(set(dates)):
        raise ValueError("phase_a_cache test_dates must be non-empty and unique")
    results = value.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("phase_a_cache results must be a non-empty list")
    normalized_results: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, Mapping) or not isinstance(item.get("config"), Mapping):
            raise ValueError("phase_a_cache result missing config")
        preds = np.asarray(item.get("preds"), dtype=np.int32)
        probs = np.asarray(item.get("probs"), dtype=np.float64)
        if preds.shape != (len(dates),) or probs.shape != (len(dates),):
            raise ValueError("phase_a_cache result arrays do not match test_dates")
        normalized_results.append(
            {
                "config": dict(item["config"]),
                "preds": preds.copy(),
                "probs": probs.copy(),
            }
        )
    return {"test_dates": dates, "results": normalized_results}


def _merge_phase_a_caches(
    cached: Mapping[str, Any] | None,
    new: Mapping[str, Any],
) -> dict[str, Any]:
    if cached is None:
        return _validate_phase_a_cache(new)
    old = _validate_phase_a_cache(cached)
    fresh = _validate_phase_a_cache(new)
    old_by_config = {_canonical_json(item["config"]): item for item in old["results"]}
    new_by_config = {_canonical_json(item["config"]): item for item in fresh["results"]}
    if set(old_by_config) != set(new_by_config):
        raise ValueError("phase_a_cache config sets do not match")
    merged_dates = sorted(set(old["test_dates"]) | set(fresh["test_dates"]))
    merged_results: list[dict[str, Any]] = []
    for old_item in old["results"]:
        key = _canonical_json(old_item["config"])
        new_item = new_by_config[key]
        pred_by_date = dict(zip(old["test_dates"], old_item["preds"], strict=True))
        prob_by_date = dict(zip(old["test_dates"], old_item["probs"], strict=True))
        for index, day in enumerate(fresh["test_dates"]):
            if day in pred_by_date:
                if int(pred_by_date[day]) != int(new_item["preds"][index]) or not np.isclose(
                    float(prob_by_date[day]), float(new_item["probs"][index]), rtol=0.0, atol=0.0
                ):
                    raise ValueError(f"conflicting Phase A cache row for {day}")
                continue
            pred_by_date[day] = new_item["preds"][index]
            prob_by_date[day] = new_item["probs"][index]
        merged_results.append(
            {
                "config": dict(old_item["config"]),
                "preds": np.asarray([pred_by_date[day] for day in merged_dates], dtype=np.int32),
                "probs": np.asarray([prob_by_date[day] for day in merged_dates], dtype=np.float64),
            }
        )
    return {"test_dates": merged_dates, "results": merged_results}


def _phase_a_cache_before(
    cache: Mapping[str, Any],
    cutoff_date: str,
) -> dict[str, Any] | None:
    normalized = _validate_phase_a_cache(cache)
    indexes = [
        index
        for index, day in enumerate(normalized["test_dates"])
        if day < cutoff_date
    ]
    if not indexes:
        return None
    return {
        "test_dates": [
            normalized["test_dates"][index]
            for index in indexes
        ],
        "results": [
            {
                "config": dict(item["config"]),
                "preds": item["preds"][indexes].copy(),
                "probs": item["probs"][indexes].copy(),
            }
            for item in normalized["results"]
        ],
    }


def _legacy_input_prefix_matches(
    value: Any,
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
) -> bool:
    """按 v1 原始 prefix 规则验证旧缓存绑定，禁止猜测式迁移。"""
    if not isinstance(value, Mapping):
        return False
    bounds = value.get("bounds")
    fingerprints = value.get("fingerprints")
    if not isinstance(bounds, Mapping) or not isinstance(
        fingerprints,
        Mapping,
    ):
        return False
    current = {
        "daily": _frame_prefix_fingerprint(
            daily_df,
            "date",
            bounds.get("daily"),
        ),
        "weekly": _frame_prefix_fingerprint(
            weekly_df,
            "week_id",
            bounds.get("weekly"),
        ),
        "monthly": _frame_prefix_fingerprint(
            monthly_df,
            "month_id",
            bounds.get("monthly"),
        ),
    }
    return current == {
        name: str(fingerprints.get(name))
        for name in current
    }


def _frame_prefix_fingerprint(df: pd.DataFrame, key: str, bound: Any) -> str:
    if key not in df.columns:
        raise ValueError(f"input frame missing prefix key {key}")
    work = df.copy()
    work[key] = _normalized_key(work[key], key)
    if bound is None:
        work = work.iloc[0:0]
    else:
        normalized_bound = _normalized_bound(bound, key)
        work = work[work[key].notna() & (work[key] <= normalized_bound)]
    work = work.sort_values(key).reset_index(drop=True)
    columns = sorted(str(column) for column in work.columns)
    work = work[columns]
    payload = {
        "key": key,
        "bound": _normalized_bound(bound, key) if bound is not None else None,
        "columns": columns,
        "dtypes": [str(work[column].dtype) for column in columns],
        "row_hashes": pd.util.hash_pandas_object(work, index=False).astype("uint64").tolist(),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _normalized_key(values: pd.Series, key: str) -> pd.Series:
    if key == "date":
        return pd.to_datetime(values, errors="coerce").dt.strftime("%Y-%m-%d")
    if key == "week_id":
        return pd.to_numeric(values, errors="coerce").astype("Int64")
    return values.astype("string").str.strip().str.replace(r"\.0$", "", regex=True)


def _normalized_bound(value: Any, key: str) -> str | int:
    if key == "week_id":
        return int(value)
    if key == "date":
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    return str(value).removesuffix(".0")


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_pickle_dump(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temp_path = Path(handle.name)
            pickle.dump(dict(payload), handle, protocol=pickle.HIGHEST_PROTOCOL)
            handle.flush()
            os.fsync(handle.fileno())
        with temp_path.open("rb") as handle:
            verified = pickle.load(handle)
        if not isinstance(verified, dict) or verified.get("schema_version") != CACHE_SCHEMA_VERSION:
            raise ValueError("atomic cache verification failed")
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
