"""14 个冻结 Native 日频方案的 no-persist 真实执行认证。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import secrets
import stat
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from scheduler.daily_policy import (
    DEFAULT_POLICY_PATH,
    load_daily_policy,
)
from shared.calendar_service import get_calendar
from shared.native_input_generation import open_native_generation


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
CERTIFICATION_SCHEMA_VERSION = "native-daily-certification-v1"
EXPECTED_SCHEME_COUNT = 14
EXPECTED_TARGET_COUNT = 18
_LIWEI_PREFIX = "liwei_0616_"
_RUNTIME_EXTRA_FIELDS = frozenset(
    {
        "input_artifact_path",
        "daily_input_artifact_path",
        "weekly_input_artifact_path",
        "monthly_input_artifact_path",
        "phase_a_cache",
        "phase_a_cache_status",
        "phase_a_cache_watermark",
        "phase_a_cache_missing_dates",
        "phase_a_cache_version",
        "phase_a_cache_fingerprint",
        "phase_a_cache_root",
    }
)
_INTERNAL_FIELDS_BY_SCHEME = {
    "t1_daily": (
        "base_pred",
        "base_decision",
        "vote_sum",
        "decision",
        "threshold",
    ),
    "t5_daily": (
        "model_pred",
        "vote_sum",
        "signal_sum",
        "threshold",
        "decision",
        "vote_signals",
    ),
    "daily_5y_2_v28": ("vote_score", "ens_prob"),
    "daily_7y_1_v28": ("vote_score", "ens_prob"),
}
_LIWEI_INTERNAL_FIELDS = (
    "vote_score",
    "baseline_signs",
    "baseline_scores",
    "vote_baselines",
    "fallback_baseline",
)


@dataclass(frozen=True)
class NativeCertificationItem:
    """一个政策冻结的 generation Native 认证项。"""

    scheme_id: str
    horizon: int
    target_tenors: tuple[str, ...]
    input_compatibility: str
    cache_group: str
    cache_prerequisite: bool
    timeout_sec: int
    required_internal_fields: tuple[str, ...]

    @property
    def is_liwei(self) -> bool:
        """是否属于必须执行 cold/warm CompareGate 的 Liwei 方案。"""
        return self.scheme_id.startswith(_LIWEI_PREFIX)


@dataclass(frozen=True)
class NativeCertificationCandidate:
    """长运行期间必须保持不变的代码、策略和输出身份。"""

    project_root: Path
    commit: str
    policy_path: Path
    policy_sha256: str
    output_root: Path
    output_device: int
    output_inode: int


class NativeDailyCertificationError(RuntimeError):
    """真实 Native 认证的稳定、无敏感信息错误。"""

    def __init__(
        self,
        code: str,
        *,
        scheme_id: str | None = None,
    ) -> None:
        self.code = code
        self.scheme_id = scheme_id
        suffix = f": {scheme_id}" if scheme_id else ""
        super().__init__(f"{code}{suffix}")


Worker = Callable[..., Mapping[str, object]]


def load_generation_native_matrix(
    policy_path: str | Path = DEFAULT_POLICY_PATH,
) -> tuple[NativeCertificationItem, ...]:
    """从真实 policy 动态冻结 14 item / 18 target 认证矩阵。"""
    policy = load_daily_policy(policy_path)
    items: list[NativeCertificationItem] = []
    for scheme in policy.schemes.values():
        if (
            scheme.runtime_type != "native_adapter"
            or scheme.input_compatibility != "generation_v1"
        ):
            continue
        required_fields = (
            _LIWEI_INTERNAL_FIELDS
            if scheme.scheme_id.startswith(_LIWEI_PREFIX)
            else _INTERNAL_FIELDS_BY_SCHEME.get(scheme.scheme_id)
        )
        if not required_fields:
            raise NativeDailyCertificationError(
                "NATIVE_CERT_INTERNAL_SCOPE_MISSING",
                scheme_id=scheme.scheme_id,
            )
        items.append(
            NativeCertificationItem(
                scheme_id=scheme.scheme_id,
                horizon=scheme.horizon,
                target_tenors=tuple(scheme.target_tenors),
                input_compatibility=scheme.input_compatibility,
                cache_group=scheme.cache_group,
                cache_prerequisite=scheme.cache_prerequisite,
                timeout_sec=scheme.admitted_hard_runtime_sec,
                required_internal_fields=tuple(required_fields),
            )
        )
    if (
        len(items) != EXPECTED_SCHEME_COUNT
        or sum(len(item.target_tenors) for item in items)
        != EXPECTED_TARGET_COUNT
    ):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_POLICY_MATRIX_DRIFT"
        )
    return tuple(items)


def certify_generation_native_daily(
    *,
    manifest_path: str | Path,
    output_root: str | Path,
    worker: Worker | None = None,
    policy_path: str | Path = DEFAULT_POLICY_PATH,
    generation_opener: Callable[..., object] = open_native_generation,
    candidate: NativeCertificationCandidate | None = None,
) -> dict[str, object]:
    """串行执行真实算法认证，只写隔离证据目录，不写业务数据库。"""
    root = _require_empty_private_output_root(output_root)
    manifest = Path(manifest_path)
    if not manifest.is_absolute():
        raise NativeDailyCertificationError(
            "NATIVE_CERT_MANIFEST_PATH_UNSAFE"
        )
    generation = generation_opener(manifest)
    expected_generation = _generation_binding(generation)
    real_worker = worker is None
    generation_calendar = (
        _require_daily_generation_calendar(generation)
        if real_worker
        else None
    )
    resolved_policy = Path(policy_path).resolve(strict=True)
    matrix = _certification_order(
        load_generation_native_matrix(resolved_policy)
    )
    policy_sha256 = hashlib.sha256(
        resolved_policy.read_bytes()
    ).hexdigest()
    scheme_reports: list[dict[str, object]] = []
    completed_targets = 0
    cache_lineages: dict[str, tuple[str, str, str]] = {}
    shared_cache_groups = _shared_cache_groups(matrix)
    if real_worker:
        if candidate is None:
            candidate = _freeze_candidate_identity(
                project_root=_PROJECT_ROOT,
                output_root=root,
                policy_path=resolved_policy,
            )
        _verify_candidate_identity(candidate)
        if (
            candidate.policy_path != resolved_policy
            or candidate.policy_sha256 != policy_sha256
            or candidate.output_root != root
        ):
            raise NativeDailyCertificationError(
                "NATIVE_CERT_CANDIDATE_DRIFT"
            )
    if real_worker:
        from harness.native_daily_certification_worker import (
            run_real_native_worker,
        )

        run_worker = run_real_native_worker
    else:
        run_worker = worker

    for item in matrix:
        expected_target_date = (
            _expected_target_date(
                generation_calendar,
                feature_date=expected_generation["feature_date"],
                horizon=item.horizon,
                scheme_id=item.scheme_id,
            )
            if generation_calendar is not None
            else None
        )
        modes = (
            ("cold", "warm_build", "warm_hit")
            if item.is_liwei
            else ("first", "second")
        )
        projections: list[object] = []
        run_digests: list[str] = []
        for mode in modes:
            if candidate is not None:
                _verify_candidate_identity(candidate)
            _reopen_expected_generation(
                generation_opener,
                manifest,
                expected_generation,
                scheme_id=item.scheme_id,
            )
            result = run_worker(
                item,
                mode,
                generation=generation,
                output_root=root,
                policy_path=resolved_policy,
                policy_sha256=policy_sha256,
                project_root=_PROJECT_ROOT,
            )
            records = _validate_worker_result(
                result,
                item=item,
                mode=mode,
                expected_generation=expected_generation,
                business_date=str(generation.business_date),
                feature_date=str(generation.feature_date),
                expected_target_date=expected_target_date,
                output_root=root,
                require_os_sandbox=real_worker,
            )
            if item.is_liwei and mode != "cold":
                lineage = _cache_lineage(
                    records,
                    item=item,
                    output_root=root,
                )
                previous_lineage = cache_lineages.get(
                    item.cache_group
                )
                if (
                    previous_lineage is not None
                    and lineage != previous_lineage
                ):
                    raise NativeDailyCertificationError(
                        "NATIVE_CERT_CACHE_LINEAGE_INVALID",
                        scheme_id=item.scheme_id,
                    )
                if (
                    mode == "warm_build"
                    and item.cache_group in shared_cache_groups
                    and not item.cache_prerequisite
                    and result.get("cache_status") != "hit"
                ):
                    raise NativeDailyCertificationError(
                        "NATIVE_CERT_CACHE_REUSE_MISSING",
                        scheme_id=item.scheme_id,
                    )
                cache_lineages[item.cache_group] = lineage
            projection = _algorithm_projection(records)
            projections.append(projection)
            run_digests.append(
                hashlib.sha256(
                    _canonical_json_bytes(projection)
                ).hexdigest()
            )
            _reopen_expected_generation(
                generation_opener,
                manifest,
                expected_generation,
                scheme_id=item.scheme_id,
            )
            if candidate is not None:
                _verify_candidate_identity(candidate)

        if any(
            projection != projections[0]
            for projection in projections[1:]
        ):
            raise NativeDailyCertificationError(
                "NATIVE_CERT_COMPARE_MISMATCH",
                scheme_id=item.scheme_id,
            )
        scheme_reports.append(
            {
                "scheme_id": item.scheme_id,
                "target_tenors": list(item.target_tenors),
                "horizon": item.horizon,
                "cache_group": item.cache_group,
                "run_modes": list(modes),
                "algorithm_output_sha256": run_digests[0],
                "deterministic": True,
                "cold_warm_compare": (
                    "passed" if item.is_liwei else "not_applicable"
                ),
                "cache_lineage": (
                    {
                        "generation_id":
                            cache_lineages[item.cache_group][0],
                        "generation_manifest_sha256":
                            cache_lineages[item.cache_group][1],
                        "family_root":
                            cache_lineages[item.cache_group][2],
                    }
                    if item.is_liwei
                    else None
                ),
            }
        )
        completed_targets += len(item.target_tenors)

    report: dict[str, object] = {
        "schema_version": CERTIFICATION_SCHEMA_VERSION,
        "status": "passed",
        "persistence": "none",
        "network_policy": (
            "macos_sandbox_deny"
            if real_worker
            else "test_double"
        ),
        "worker_isolation": (
            "macos_sandbox_readonly_inputs"
            if real_worker
            else "test_double"
        ),
        "evidence_scope": (
            "real_algorithm"
            if real_worker
            else "test_double"
        ),
        "candidate_commit": (
            candidate.commit
            if candidate is not None
            else _current_git_commit(_PROJECT_ROOT)
        ),
        "policy_sha256": policy_sha256,
        "generation": expected_generation,
        "expected_scheme_count": EXPECTED_SCHEME_COUNT,
        "expected_target_count": EXPECTED_TARGET_COUNT,
        "completed_scheme_count": len(scheme_reports),
        "completed_target_count": completed_targets,
        "schemes": scheme_reports,
    }
    if candidate is not None:
        _verify_candidate_identity(candidate)
    _write_private_json(
        root / "certification.json",
        report,
        expected_root=(
            (
                candidate.output_device,
                candidate.output_inode,
            )
            if candidate is not None
            else None
        ),
    )
    if candidate is not None:
        _verify_candidate_identity(candidate)
    return report


def _validate_worker_result(
    raw: Mapping[str, object],
    *,
    item: NativeCertificationItem,
    mode: str,
    expected_generation: Mapping[str, str],
    business_date: str,
    feature_date: str,
    expected_target_date: str | None = None,
    output_root: Path,
    require_os_sandbox: bool = False,
) -> list[dict[str, object]]:
    if not isinstance(raw, Mapping):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_WORKER_RESULT_INVALID",
            scheme_id=item.scheme_id,
        )
    if raw.get("scheme_id") != item.scheme_id or raw.get("mode") != mode:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_WORKER_RESULT_INVALID",
            scheme_id=item.scheme_id,
        )
    if raw.get("persistence") != "none":
        raise NativeDailyCertificationError(
            "NATIVE_CERT_PERSISTENCE_UNSAFE",
            scheme_id=item.scheme_id,
        )
    if raw.get("network_attempts") != 0:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_NETWORK_ATTEMPT",
            scheme_id=item.scheme_id,
        )
    if (
        require_os_sandbox
        and (
            raw.get("sandbox_profile")
            != "native-certification-macos-v1"
            or raw.get("multiprocessing_start_method") != "spawn"
        )
    ):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_OS_SANDBOX_MISSING",
            scheme_id=item.scheme_id,
        )
    if require_os_sandbox and expected_target_date is None:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_GENERATION_CALENDAR_INVALID",
            scheme_id=item.scheme_id,
        )
    if (
        raw.get("generation_id")
        != expected_generation["generation_id"]
        or raw.get("manifest_sha256")
        != expected_generation["manifest_sha256"]
    ):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_GENERATION_DRIFT",
            scheme_id=item.scheme_id,
        )
    if item.is_liwei and mode == "warm_hit":
        if raw.get("cache_status") != "hit":
            raise NativeDailyCertificationError(
                "NATIVE_CERT_CACHE_HIT_MISSING",
                scheme_id=item.scheme_id,
            )
    records = raw.get("records")
    if not isinstance(records, list):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_RECORDS_INVALID",
            scheme_id=item.scheme_id,
        )
    target_counter: Counter[tuple[str, int]] = Counter()
    normalized: list[dict[str, object]] = []
    for raw_record in records:
        if not isinstance(raw_record, dict):
            raise NativeDailyCertificationError(
                "NATIVE_CERT_RECORDS_INVALID",
                scheme_id=item.scheme_id,
            )
        record = dict(raw_record)
        if (
            record.get("scheme_id") != item.scheme_id
            or record.get("predict_date") != business_date
            or record.get("feature_date") != feature_date
        ):
            raise NativeDailyCertificationError(
                "NATIVE_CERT_RECORDS_INVALID",
                scheme_id=item.scheme_id,
            )
        tenor = record.get("target_tenor")
        horizon = record.get("horizon")
        if not isinstance(tenor, str) or horizon != item.horizon:
            raise NativeDailyCertificationError(
                "NATIVE_CERT_RECORDS_INVALID",
                scheme_id=item.scheme_id,
            )
        direction = record.get("predicted_direction")
        confidence = record.get("confidence")
        model_version = record.get("model_version")
        target_date = record.get("target_date")
        try:
            parsed_target_date = date.fromisoformat(
                str(target_date)
            )
        except ValueError:
            parsed_target_date = None
        if (
            isinstance(direction, bool)
            or not isinstance(direction, int)
            or direction not in {-1, 0, 1}
            or not isinstance(model_version, str)
            or not model_version.strip()
            or not isinstance(target_date, str)
            or parsed_target_date is None
            or parsed_target_date.isoformat() != target_date
            or (
                expected_target_date is not None
                and target_date != expected_target_date
            )
        ):
            raise NativeDailyCertificationError(
                "NATIVE_CERT_RECORDS_INVALID",
                scheme_id=item.scheme_id,
            )
        extra = record.get("extra")
        if not isinstance(extra, dict):
            raise NativeDailyCertificationError(
                "NATIVE_CERT_INTERNAL_SCOPE_MISSING",
                scheme_id=item.scheme_id,
            )
        if any(
            field not in extra
            for field in item.required_internal_fields
        ):
            raise NativeDailyCertificationError(
                "NATIVE_CERT_INTERNAL_SCOPE_MISSING",
                scheme_id=item.scheme_id,
            )
        t5_cold_fallback = (
            item.scheme_id == "t5_daily"
            and extra.get("decision") == "cold_fallback"
        )
        for field in item.required_internal_fields:
            value = extra[field]
            if value is None:
                if field == "threshold" and t5_cold_fallback:
                    continue
                raise NativeDailyCertificationError(
                    "NATIVE_CERT_INTERNAL_SCOPE_MISSING",
                    scheme_id=item.scheme_id,
                )
            if (
                (isinstance(value, Mapping) and not value)
                or (isinstance(value, list) and not value)
                or (
                    isinstance(value, str)
                    and not value.strip()
                )
            ):
                raise NativeDailyCertificationError(
                    "NATIVE_CERT_INTERNAL_SCOPE_MISSING",
                    scheme_id=item.scheme_id,
                )
        if (
            confidence is None
            and not t5_cold_fallback
        ) or (
            confidence is not None
            and not _is_probability(confidence)
        ) or (
            t5_cold_fallback
            and (
                confidence is not None
                or extra.get("threshold") is not None
            )
        ):
            raise NativeDailyCertificationError(
                "NATIVE_CERT_RECORDS_INVALID",
                scheme_id=item.scheme_id,
            )
        for field in ("vote_sum", "signal_sum"):
            if field in extra and not _is_integer(extra[field]):
                raise NativeDailyCertificationError(
                    "NATIVE_CERT_INTERNAL_SCOPE_MISSING",
                    scheme_id=item.scheme_id,
                )
        for field in ("base_pred", "model_pred"):
            if field in extra and not _is_direction(extra[field]):
                raise NativeDailyCertificationError(
                    "NATIVE_CERT_INTERNAL_SCOPE_MISSING",
                    scheme_id=item.scheme_id,
                )
        for field in ("threshold", "ens_prob"):
            if (
                field in extra
                and extra[field] is not None
                and not _is_probability(extra[field])
            ):
                raise NativeDailyCertificationError(
                    "NATIVE_CERT_INTERNAL_SCOPE_MISSING",
                    scheme_id=item.scheme_id,
                )
        for field in ("vote_score",):
            if field in extra and not _is_finite_number(
                extra[field]
            ):
                raise NativeDailyCertificationError(
                    "NATIVE_CERT_INTERNAL_SCOPE_MISSING",
                    scheme_id=item.scheme_id,
                )
        vote_signals = extra.get("vote_signals")
        if (
            vote_signals is not None
            and not _is_direction_mapping(vote_signals)
        ):
            raise NativeDailyCertificationError(
                "NATIVE_CERT_INTERNAL_SCOPE_MISSING",
                scheme_id=item.scheme_id,
            )
        if item.is_liwei:
            signs = extra.get("baseline_signs")
            scores = extra.get("baseline_scores")
            baselines = extra.get("vote_baselines")
            fallback = extra.get("fallback_baseline")
            baseline_scope = (
                set(baselines) | {fallback}
                if (
                    isinstance(baselines, list)
                    and isinstance(fallback, str)
                )
                else set()
            )
            if (
                not isinstance(signs, Mapping)
                or not isinstance(scores, Mapping)
                or not isinstance(baselines, list)
                or not baselines
                or any(
                    not isinstance(value, str)
                    or not value.strip()
                    or value.strip() != value
                    for value in baselines
                )
                or not isinstance(fallback, str)
                or not fallback.strip()
                or fallback.strip() != fallback
                or len(set(baselines)) != len(baselines)
                or set(signs) != set(scores)
                or set(signs) != baseline_scope
                or not signs
                or not _is_direction_mapping(signs)
                or not all(
                    _is_finite_number(value)
                    for value in scores.values()
                )
            ):
                raise NativeDailyCertificationError(
                    "NATIVE_CERT_INTERNAL_SCOPE_MISSING",
                    scheme_id=item.scheme_id,
                )
        if item.is_liwei and mode != "cold":
            _require_warm_cache_generation(
                extra,
                expected_generation=expected_generation,
                scheme_id=item.scheme_id,
            )
        target_counter[(tenor, int(horizon))] += 1
        normalized.append(record)
    expected_targets = Counter(
        (tenor, item.horizon)
        for tenor in item.target_tenors
    )
    if target_counter != expected_targets:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_TARGET_MATRIX_MISMATCH",
            scheme_id=item.scheme_id,
        )
    return normalized


def _is_finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _is_integer(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int)


def _is_direction(value: object) -> bool:
    return _is_integer(value) and value in {-1, 0, 1}


def _is_probability(value: object) -> bool:
    return _is_finite_number(value) and 0 <= float(value) <= 1


def _is_direction_mapping(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and bool(value)
        and all(
            isinstance(key, str)
            and bool(key.strip())
            and key.strip() == key
            and _is_direction(item)
            for key, item in value.items()
        )
    )


def _certification_order(
    matrix: Sequence[NativeCertificationItem],
) -> tuple[NativeCertificationItem, ...]:
    """保持 policy 顺序，同时令共享 cache prewarmer 先于 consumer。"""
    prerequisites = {
        item.cache_group: item
        for item in matrix
        if item.is_liwei and item.cache_prerequisite
    }
    ordered: list[NativeCertificationItem] = []
    emitted: set[str] = set()
    for item in matrix:
        prerequisite = prerequisites.get(item.cache_group)
        if (
            prerequisite is not None
            and prerequisite.scheme_id not in emitted
        ):
            ordered.append(prerequisite)
            emitted.add(prerequisite.scheme_id)
        if item.scheme_id not in emitted:
            ordered.append(item)
            emitted.add(item.scheme_id)
    return tuple(ordered)


def _shared_cache_groups(
    matrix: Sequence[NativeCertificationItem],
) -> frozenset[str]:
    counts = Counter(
        item.cache_group
        for item in matrix
        if item.is_liwei
    )
    return frozenset(
        cache_group
        for cache_group, count in counts.items()
        if count > 1
    )


def _cache_lineage(
    records: Sequence[Mapping[str, object]],
    *,
    item: NativeCertificationItem,
    output_root: Path,
) -> tuple[str, str, str]:
    try:
        expected_family, expected_tenor = item.cache_group.rsplit(
            ":",
            1,
        )
    except ValueError:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_CACHE_LINEAGE_INVALID",
            scheme_id=item.scheme_id,
        ) from None
    expected_root = (
        output_root
        / "cache"
        / expected_family
        / expected_tenor.lower()
    )
    lineages: set[tuple[str, str, str]] = set()
    for record in records:
        extra = record.get("extra")
        cache = (
            extra.get("phase_a_cache")
            if isinstance(extra, Mapping)
            else None
        )
        if not isinstance(cache, Mapping):
            raise NativeDailyCertificationError(
                "NATIVE_CERT_CACHE_LINEAGE_INVALID",
                scheme_id=item.scheme_id,
            )
        family_root = Path(str(cache.get("family_root") or ""))
        generation_id = cache.get("generation_id")
        generation_manifest = cache.get(
            "generation_manifest_sha256"
        )
        if (
            cache.get("cache_family") != expected_family
            or cache.get("tenor") != expected_tenor
            or not family_root.is_absolute()
            or family_root.resolve(strict=False) != expected_root
            or not isinstance(generation_id, str)
            or not generation_id
            or not isinstance(generation_manifest, str)
            or len(generation_manifest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in generation_manifest
            )
        ):
            raise NativeDailyCertificationError(
                "NATIVE_CERT_CACHE_LINEAGE_INVALID",
                scheme_id=item.scheme_id,
            )
        lineages.add(
            (
                generation_id,
                generation_manifest,
                str(family_root),
            )
        )
    if len(lineages) != 1:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_CACHE_LINEAGE_INVALID",
            scheme_id=item.scheme_id,
        )
    return lineages.pop()


def _require_warm_cache_generation(
    extra: Mapping[str, object],
    *,
    expected_generation: Mapping[str, str],
    scheme_id: str,
) -> None:
    cache = extra.get("phase_a_cache")
    acceptance = (
        cache.get("generation_acceptance")
        if isinstance(cache, Mapping)
        else None
    )
    native_generation = (
        acceptance.get("native_generation")
        if isinstance(acceptance, Mapping)
        else None
    )
    if (
        not isinstance(native_generation, Mapping)
        or dict(native_generation) != dict(expected_generation)
    ):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_GENERATION_DRIFT",
            scheme_id=scheme_id,
        )


def _algorithm_projection(
    records: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    projected: list[dict[str, object]] = []
    for raw_record in records:
        record = dict(raw_record)
        extra = dict(record.get("extra") or {})
        for field in _RUNTIME_EXTRA_FIELDS:
            extra.pop(field, None)
        record["extra"] = extra
        _require_json_value(record)
        projected.append(record)
    return sorted(
        projected,
        key=lambda record: (
            str(record["target_tenor"]),
            int(record["horizon"]),
        ),
    )


def _generation_binding(generation: object) -> dict[str, str]:
    fields = (
        "generation_id",
        "manifest_sha256",
        "dataset_content_id",
        "business_date",
        "feature_date",
        "schema_version",
        "exporter_version",
    )
    binding: dict[str, str] = {}
    for field in fields:
        value = getattr(generation, field, None)
        if not isinstance(value, str) or not value:
            raise NativeDailyCertificationError(
                "NATIVE_CERT_GENERATION_INVALID"
            )
        binding[field] = value
    return binding


def _require_daily_generation_calendar(generation: object) -> object:
    """证明 business_date 是 feature_date 后的首个真实交易日。"""
    try:
        calendar = get_calendar(engine=generation)
        business_date = getattr(generation, "business_date")
        feature_date = getattr(generation, "feature_date")
        if (
            not calendar.is_trading_day(business_date)
            or not calendar.is_trading_day(feature_date)
        ):
            raise ValueError("generation dates are not trading days")
        previous = calendar.previous_trading_day(
            business_date
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_GENERATION_CALENDAR_INVALID"
        ) from None
    if previous != feature_date:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_GENERATION_CALENDAR_INVALID"
        )
    return calendar


def _expected_target_date(
    calendar: object,
    *,
    feature_date: str,
    horizon: int,
    scheme_id: str,
) -> str:
    """从同一冻结日历计算并校验 canonical target date。"""
    try:
        target_date = calendar.nth_trading_day_after(
            feature_date,
            horizon,
        )
        if (
            not isinstance(target_date, str)
            or date.fromisoformat(target_date).isoformat()
            != target_date
            or not calendar.is_trading_day(target_date)
        ):
            raise ValueError("invalid target date")
    except (AttributeError, TypeError, ValueError):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_GENERATION_CALENDAR_INVALID",
            scheme_id=scheme_id,
        ) from None
    return target_date


def _reopen_expected_generation(
    opener: Callable[..., object],
    manifest_path: Path,
    expected: Mapping[str, str],
    *,
    scheme_id: str,
) -> None:
    reopened = opener(
        manifest_path,
        expected_generation_id=expected["generation_id"],
        expected_manifest_sha256=expected["manifest_sha256"],
        expected_business_date=expected["business_date"],
        expected_feature_date=expected["feature_date"],
    )
    if _generation_binding(reopened) != dict(expected):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_GENERATION_DRIFT",
            scheme_id=scheme_id,
        )


def _require_empty_private_output_root(
    raw_path: str | Path,
) -> Path:
    path = Path(raw_path)
    if not path.is_absolute() or path.resolve(strict=False) != path:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_OUTPUT_ROOT_UNSAFE"
        )
    try:
        details = path.lstat()
    except OSError:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_OUTPUT_ROOT_UNSAFE"
        ) from None
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o700
        or any(path.iterdir())
    ):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_OUTPUT_ROOT_UNSAFE"
        )
    return path


def _write_private_json(
    path: Path,
    payload: Mapping[str, object],
    *,
    expected_root: tuple[int, int] | None = None,
) -> None:
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    parent_flags |= getattr(os, "O_NOFOLLOW", 0)
    parent_descriptor = os.open(path.parent, parent_flags)
    temporary_name = (
        f".certification-{secrets.token_hex(16)}.json"
    )
    try:
        parent_details = os.fstat(parent_descriptor)
        if (
            expected_root is not None
            and (
                parent_details.st_dev,
                parent_details.st_ino,
            )
            != expected_root
        ):
            raise NativeDailyCertificationError(
                "NATIVE_CERT_OUTPUT_ROOT_DRIFT"
            )
        descriptor = os.open(
            temporary_name,
            (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
            ),
            0o600,
            dir_fd=parent_descriptor,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
    except BaseException:
        try:
            os.unlink(
                temporary_name,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(parent_descriptor)


def _current_git_commit(
    project_root: Path = _PROJECT_ROOT,
) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_GIT_IDENTITY_INVALID"
        ) from None
    commit = completed.stdout.strip()
    if (
        len(commit) != 40
        or any(character not in "0123456789abcdef" for character in commit)
    ):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_GIT_IDENTITY_INVALID"
        )
    return commit


def _require_isolated_clean_worktree(
    project_root: Path,
    *,
    output_root: Path,
) -> None:
    if not (project_root / ".git").is_file():
        raise NativeDailyCertificationError(
            "NATIVE_CERT_ISOLATED_WORKTREE_REQUIRED"
        )
    try:
        output_root.relative_to(project_root)
    except ValueError:
        pass
    else:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_OUTPUT_ROOT_UNSAFE"
        )
    try:
        symbolic = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        status = subprocess.run(
            [
                "git",
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            cwd=project_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_ISOLATED_WORKTREE_REQUIRED"
        ) from None
    if symbolic.returncode == 0 or status.stdout.strip():
        raise NativeDailyCertificationError(
            "NATIVE_CERT_ISOLATED_WORKTREE_REQUIRED"
        )


def _freeze_candidate_identity(
    *,
    project_root: Path,
    output_root: Path,
    policy_path: Path,
) -> NativeCertificationCandidate:
    root = project_root.resolve(strict=True)
    if (
        Path.cwd().resolve(strict=True) != root
        or root != _PROJECT_ROOT
    ):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_PROJECT_ROOT_MISMATCH"
        )
    output = output_root.resolve(strict=True)
    policy = policy_path.resolve(strict=True)
    _require_isolated_clean_worktree(
        root,
        output_root=output,
    )
    _require_tracked_policy(
        project_root=root,
        policy_path=policy,
    )
    details = output.lstat()
    candidate = NativeCertificationCandidate(
        project_root=root,
        commit=_current_git_commit(root),
        policy_path=policy,
        policy_sha256=hashlib.sha256(
            policy.read_bytes()
        ).hexdigest(),
        output_root=output,
        output_device=details.st_dev,
        output_inode=details.st_ino,
    )
    _verify_candidate_identity(candidate)
    return candidate


def _verify_candidate_identity(
    candidate: NativeCertificationCandidate,
) -> None:
    if (
        Path.cwd().resolve(strict=True) != candidate.project_root
        or candidate.project_root != _PROJECT_ROOT
        or _current_git_commit(candidate.project_root)
        != candidate.commit
    ):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_CANDIDATE_DRIFT"
        )
    _require_isolated_clean_worktree(
        candidate.project_root,
        output_root=candidate.output_root,
    )
    _require_tracked_policy(
        project_root=candidate.project_root,
        policy_path=candidate.policy_path,
    )
    try:
        policy_sha256 = hashlib.sha256(
            candidate.policy_path.read_bytes()
        ).hexdigest()
        details = candidate.output_root.lstat()
    except OSError:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_CANDIDATE_DRIFT"
        ) from None
    if (
        policy_sha256 != candidate.policy_sha256
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o700
        or (
            details.st_dev,
            details.st_ino,
        )
        != (
            candidate.output_device,
            candidate.output_inode,
        )
    ):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_CANDIDATE_DRIFT"
        )


def _require_tracked_policy(
    *,
    project_root: Path,
    policy_path: Path,
) -> None:
    try:
        relative = policy_path.relative_to(project_root)
    except ValueError:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_POLICY_UNTRACKED"
        ) from None
    try:
        details = policy_path.lstat()
        completed = subprocess.run(
            [
                "git",
                "ls-files",
                "--error-unmatch",
                "--",
                str(relative),
            ],
            cwd=project_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_POLICY_UNTRACKED"
        ) from None
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or completed.stdout.strip() != str(relative)
    ):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_POLICY_UNTRACKED"
        )


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _require_json_value(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_RECORDS_INVALID"
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise NativeDailyCertificationError(
                    "NATIVE_CERT_RECORDS_INVALID"
                )
            _require_json_value(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _require_json_value(item)
        return
    if value is not None and not isinstance(
        value,
        (str, int, float, bool),
    ):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_RECORDS_INVALID"
        )


def _parse_arguments(
    argv: Sequence[str] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Certify all generation_v1 Native daily consumers without "
            "database persistence."
        )
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-root")
    parser.add_argument(
        "--policy",
        default=str(DEFAULT_POLICY_PATH),
    )
    parser.add_argument("--no-persist", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_arguments(argv)
    if (
        not args.no_persist
        or not args.output_root
    ):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_NO_PERSIST_REQUIRED"
        )
    root = _require_empty_private_output_root(
        Path(args.output_root)
    )
    candidate = _freeze_candidate_identity(
        project_root=_PROJECT_ROOT,
        output_root=root,
        policy_path=Path(args.policy),
    )
    report = certify_generation_native_daily(
        manifest_path=args.manifest,
        output_root=root,
        policy_path=candidate.policy_path,
        candidate=candidate,
    )
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
