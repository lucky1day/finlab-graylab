"""只消费冻结 occurrence ledger 的日频 item 执行入口。"""

from __future__ import annotations

import errno
import json
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from sqlalchemy.exc import (
    DBAPIError,
    DataError,
    IntegrityError,
    OperationalError,
    ProgrammingError,
)

from scheduler.completion_verifier import (
    FilesystemScheduledCompletionVerifier,
    ScheduledVerificationError,
)
from scheduler.daily_policy import (
    APPROVED_0629_LIVE_SOURCE_SCHEMES,
)
from scheduler.discovery import SchemeConfig, load_scheme_config
from scheduler.daily_ledger import (
    FAILURE_ABANDONED_FENCE_PENDING_CLEANUP,
    FAILURE_ALGORITHM,
    FAILURE_CONTRACT,
    FAILURE_DATA,
    FAILURE_RECOVERY_CUTOFF_EXPIRED,
    FAILURE_RESULT,
    FAILURE_STALE_ATTEMPT,
    FAILURE_TIMEOUT,
    FAILURE_TRANSIENT_INFRA,
)
from scheduler.executor import (
    DEFAULT_ALGO_ENV,
    _effective_timeout_sec,
    run_configured_scheme,
)
from scheduler.process_control import (
    ProcessGroupTerminationError,
    ProcessStartGuard,
    require_process_start_guard,
)
from scheduler.repository import (
    ScheduleExecutionEnvelope,
    complete_scheduled_attempt,
    fence_current_schedule_attempt,
    mark_schedule_attempt_retry_wait,
    mark_schedule_attempt_terminal_failure,
    read_schedule_execution_envelope,
    register_schedule_attempt_process,
    start_schedule_attempt,
)
from shared.databridge_input_generation import open_databridge_generation
from shared.daily_coordinator_mode import (
    assert_daily_coordinator_epoch_matches_policy,
)
from shared.input_artifacts import BLACKBOX_SCHEMA_PATH
from shared.input_artifacts import LIVE_SOURCE_INPUT_MODE
from shared.models import PredictionRecord
from shared.native_input_generation import open_native_generation
from shared.liwei_0616_cache_contract import (
    validate_prediction_cache_audit,
    validate_direct_cache_runtime_context,
    validate_trusted_cache_use_qualification,
)
from shared.liwei_0616_phase_a_cache import (
    verify_phase_a_cache_audit_files,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
V2_HARD_TIMEOUT_SEC = 120
_MAX_ERROR_LENGTH = 4000
_TRANSIENT_MYSQL_OPERATIONAL_CODES = frozenset(
    {
        1158,  # network read error
        1159,  # network read timeout
        1160,  # network write error
        1161,  # network write timeout
        1205,  # lock wait timeout
        1213,  # deadlock
        2002,  # local server connection failed
        2003,  # remote server connection failed
        2006,  # server has gone away
        2013,  # lost connection during query
        2055,  # lost connection at handshake/query
    }
)
_TRANSIENT_OS_ERRNOS = frozenset(
    {
        errno.EAGAIN,
        errno.EINTR,
        errno.ETIMEDOUT,
    }
)
_TRANSIENT_CONNECTION_ERRNOS = frozenset(
    value
    for value in (
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "ECONNREFUSED", None),
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "EHOSTDOWN", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENETDOWN", None),
        getattr(errno, "ENETRESET", None),
        getattr(errno, "ENETUNREACH", None),
    )
    if value is not None
)


class ScheduledContractError(RuntimeError):
    """冻结的方案身份或运行契约已经漂移。"""


class ScheduledDataError(RuntimeError):
    """冻结 generation 无法通过重开与完整性校验。"""


class ScheduledResultError(RuntimeError):
    """算法返回结果不符合冻结 target 契约。"""


class ScheduledRecoveryCutoffError(RuntimeError):
    """attempt claim 后的真实子进程启动越过 08:30 fence。"""


class ScheduledEpochDriftError(ScheduledContractError):
    """current machine epoch 与冻结 occurrence capability 不一致。"""


class ScheduledProcessFenceError(RuntimeError):
    """隔离 replay 进程边界无法确认，禁止 claim/启动/提交。"""


@dataclass(frozen=True)
class ScheduledItemExecutionResult:
    """一个 ledger item attempt 的结构化执行结果。"""

    item_id: int
    scheme_id: str
    status: str
    run_id: int | None
    attempt_no: int | None
    records_written: int = 0
    failure_code: str | None = None
    error_message: str | None = None


def _frozen_cache_use_qualification(
    envelope: ScheduleExecutionEnvelope,
) -> dict[str, object] | None:
    """从 occurrence 冻结策略精确选择当前 consumer qualification。"""
    policy = getattr(envelope.occurrence, "policy_json", None)
    if policy is None and not isinstance(
        envelope,
        ScheduleExecutionEnvelope,
    ):
        # 旧单元测试的最小 mock 不承载 occurrence policy；生产仓储只会
        # 返回强类型 ScheduleExecutionEnvelope。
        return None
    if not isinstance(policy, dict):
        raise ScheduledContractError(
            "frozen occurrence policy is unavailable"
        )
    if "direct_cache_authorities" in policy:
        return None
    raw_schemes = policy.get("schemes")
    if not isinstance(raw_schemes, list):
        raise ScheduledContractError(
            "frozen policy schemes are unavailable"
        )
    matches = [
        row
        for row in raw_schemes
        if (
            isinstance(row, dict)
            and row.get("scheme_id") == envelope.item.base_scheme_id
        )
    ]
    if len(matches) != 1:
        raise ScheduledContractError(
            "frozen policy consumer identity is ambiguous"
        )
    expected_spec = matches[0].get("cache_spec_fingerprint")
    raw_qualifications = policy.get("cache_use_qualifications")
    if expected_spec is None:
        if (
            isinstance(raw_qualifications, dict)
            and envelope.item.base_scheme_id in raw_qualifications
        ):
            raise ScheduledContractError(
                "non-cache consumer has frozen cache qualification"
            )
        return None
    if not isinstance(raw_qualifications, dict):
        raise ScheduledContractError(
            "cache-qualified consumer is missing frozen qualification map"
        )
    raw = raw_qualifications.get(envelope.item.base_scheme_id)
    if not isinstance(raw, dict):
        raise ScheduledContractError(
            "cache-qualified consumer is missing frozen qualification"
        )
    candidate = policy.get("capacity_candidate_fingerprint")
    if not isinstance(candidate, str):
        raise ScheduledContractError(
            "cache qualification is missing frozen capacity candidate"
        )
    try:
        trusted = validate_trusted_cache_use_qualification(
            raw,
            expected_base_scheme_id=envelope.item.base_scheme_id,
            expected_candidate_fingerprint=candidate,
        )
    except ValueError as exc:
        raise ScheduledContractError(
            "frozen cache qualification is invalid"
        ) from exc
    qualification = trusted["qualification"]
    expected = {
        "scheme_version": envelope.item.scheme_version,
        "cache_group": envelope.item.cache_group,
        "spec_fingerprint": expected_spec,
    }
    mismatches = [
        field
        for field, expected_value in expected.items()
        if qualification.get(field) != expected_value
    ]
    if mismatches:
        raise ScheduledContractError(
            "frozen cache qualification consumer drift: "
            + ",".join(sorted(mismatches))
        )
    return trusted


def _frozen_direct_cache_runtime_context(
    envelope: ScheduleExecutionEnvelope,
) -> dict[str, object] | None:
    """为当前 item 选择 occurrence 冻结的 exact direct cache authority。"""
    policy = getattr(envelope.occurrence, "policy_json", None)
    if not isinstance(policy, dict):
        raise ScheduledContractError(
            "frozen occurrence policy is unavailable"
        )
    raw_authorities = policy.get("direct_cache_authorities")
    if raw_authorities is None:
        return None
    if not isinstance(raw_authorities, dict):
        raise ScheduledContractError(
            "frozen direct cache authorities are invalid"
        )
    consumers = raw_authorities.get("consumers")
    if not isinstance(consumers, dict):
        raise ScheduledContractError(
            "frozen direct cache consumers are invalid"
        )
    scheme_id = envelope.item.base_scheme_id
    consumer = consumers.get(scheme_id)
    frozen_scheme = _frozen_scheme_policy_row(envelope)
    if consumer is None:
        if frozen_scheme.get("cache_spec_fingerprint") is not None:
            raise ScheduledContractError(
                "cache-qualified consumer is missing direct authority"
            )
        return None
    context = {
        "schema_version":
            "liwei-0616-direct-cache-runtime-context-v1",
        "storage_root": raw_authorities.get("storage_root"),
        "contract": raw_authorities.get("contract"),
        "consumer": consumer,
    }
    try:
        validated = validate_direct_cache_runtime_context(context)
    except ValueError as exc:
        raise ScheduledContractError(
            "frozen direct cache runtime context is invalid"
        ) from exc
    expected = {
        "base_scheme_id": scheme_id,
        "scheme_version": envelope.item.scheme_version,
        "code_sha256": envelope.item.code_sha256,
        "config_sha256": envelope.item.config_sha256,
        "cache_group": envelope.item.cache_group,
    }
    drift = [
        field
        for field, expected_value in expected.items()
        if validated["consumer"].get(field) != expected_value
    ]
    if drift:
        raise ScheduledContractError(
            "frozen direct cache consumer drift: "
            + ",".join(sorted(drift))
        )
    policy_expected = {
        "cache_spec_fingerprint":
            validated["consumer"]["spec_fingerprint"],
        "cache_adapter_sha256":
            validated["consumer"]["cache_adapter_sha256"],
        "cache_core_sha256":
            validated["consumer"]["cache_core_sha256"],
    }
    policy_drift = [
        field
        for field, expected_value in policy_expected.items()
        if frozen_scheme.get(field) != expected_value
    ]
    if policy_drift:
        raise ScheduledContractError(
            "frozen direct cache policy digest drift: "
            + ",".join(sorted(policy_drift))
        )
    return validated


def _frozen_scheme_policy_row(
    envelope: ScheduleExecutionEnvelope,
) -> dict[str, object]:
    """读取 occurrence 中当前 item 的唯一冻结策略行。"""
    policy = getattr(envelope.occurrence, "policy_json", None)
    if not isinstance(policy, dict):
        raise ScheduledContractError(
            "frozen occurrence policy is unavailable"
        )
    raw_schemes = policy.get("schemes")
    if not isinstance(raw_schemes, list):
        raise ScheduledContractError(
            "frozen policy schemes are unavailable"
        )
    matches = [
        row
        for row in raw_schemes
        if (
            isinstance(row, dict)
            and row.get("scheme_id") == envelope.item.base_scheme_id
        )
    ]
    if len(matches) != 1:
        raise ScheduledContractError(
            "frozen policy consumer identity is ambiguous"
        )
    return matches[0]


def _frozen_input_compatibility(
    envelope: ScheduleExecutionEnvelope,
) -> str:
    """从 occurrence 冻结策略读取并复核 item 的输入模式。"""
    input_compatibility = _frozen_scheme_policy_row(envelope).get(
        "input_compatibility"
    )
    if envelope.item.runtime_type == "blackbox_v2":
        if input_compatibility != "databridge_v1":
            raise ScheduledContractError(
                "Blackbox V2 frozen input compatibility must be "
                "databridge_v1"
            )
        return "databridge_v1"
    if envelope.item.runtime_type != "native_adapter":
        raise ScheduledContractError(
            "unsupported frozen runtime_type for input compatibility"
        )
    if input_compatibility == "generation_v1":
        return "generation_v1"
    if (
        input_compatibility == LIVE_SOURCE_INPUT_MODE
        and envelope.item.base_scheme_id
        in APPROVED_0629_LIVE_SOURCE_SCHEMES
    ):
        return LIVE_SOURCE_INPUT_MODE
    raise ScheduledContractError(
        "live source compatibility is not approved for frozen item: "
        f"{envelope.item.base_scheme_id}"
    )


def _frozen_live_source_package_sha256(
    envelope: ScheduleExecutionEnvelope,
    *,
    input_compatibility: str,
) -> str | None:
    value = _frozen_scheme_policy_row(envelope).get(
        "source_package_sha256"
    )
    if input_compatibility != LIVE_SOURCE_INPUT_MODE:
        if value is not None:
            raise ScheduledContractError(
                "source_package_sha256 is only valid for live source "
                "compatibility"
            )
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ScheduledContractError(
            "frozen live source package sha256 is invalid"
        )
    return value


def _native_generation_audit_binding(
    envelope: ScheduleExecutionEnvelope,
) -> dict[str, object]:
    generation = envelope.generation
    return {
        "generation_id": generation.generation_id,
        "manifest_sha256": generation.manifest_sha256,
        "dataset_content_id": generation.dataset_content_id,
        "business_date": generation.business_date,
        "feature_date": generation.feature_date,
        "schema_version": generation.schema_version,
        "exporter_version": generation.exporter_version,
    }


def _verify_cache_qualified_records(
    records: Iterable[PredictionRecord],
    *,
    envelope: ScheduleExecutionEnvelope,
    expected_qualification: dict[str, object],
) -> None:
    """在任何 prediction 可见前复核 cache audit 与磁盘 acceptance。"""
    expected_native = _native_generation_audit_binding(envelope)
    seen = False
    for record in records:
        seen = True
        try:
            validate_prediction_cache_audit(
                record.extra,
                expected_qualification=expected_qualification,
                expected_native_generation=expected_native,
            )
            verify_phase_a_cache_audit_files(
                record.extra or {},
                expected_qualification=expected_qualification,
            )
        except ValueError as exc:
            raise ScheduledResultError(
                "cache-qualified prediction audit verification failed"
            ) from exc
    if not seen:
        raise ScheduledResultError(
            "cache-qualified consumer returned no predictions"
        )


def _verify_input_compatibility_records(
    records: Iterable[PredictionRecord],
    *,
    envelope: ScheduleExecutionEnvelope,
    input_compatibility: str,
) -> None:
    """原子提交前验证 live-source mode、水位和 generation fence。"""
    for record in records:
        extra = dict(record.extra or {})
        claimed_mode = extra.get("input_mode")
        if input_compatibility != LIVE_SOURCE_INPUT_MODE:
            if claimed_mode == LIVE_SOURCE_INPUT_MODE:
                raise ScheduledResultError(
                    "generation input item cannot claim live source mode"
                )
            continue
        expected = {
            "input_mode": LIVE_SOURCE_INPUT_MODE,
            "data_watermark": envelope.occurrence.feature_date,
            "data_watermark_basis":
                "source_output_prediction_date",
            "live_source_fence_generation_id":
                envelope.generation.generation_id,
            "source_package_hash":
                _frozen_live_source_package_sha256(
                    envelope,
                    input_compatibility=input_compatibility,
                ),
        }
        drift = sorted(
            field
            for field, expected_value in expected.items()
            if extra.get(field) != expected_value
        )
        if drift:
            raise ScheduledResultError(
                "live source compatibility audit drift: "
                + ", ".join(drift)
            )


def execute_scheduled_item(
    engine,
    *,
    item_id: int,
    algo_env: str = DEFAULT_ALGO_ENV,
    trigger_origin: str = "apscheduler",
    default_timeout_sec: int = 600,
    project_root: str | Path = PROJECT_ROOT,
    databridge_schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
    trusted_verifier=None,
    process_start_guard: ProcessStartGuard | None = None,
    _process_fence: Callable[[], None] | None = None,
) -> ScheduledItemExecutionResult:
    """执行一个已绑定 generation 的 item，且只通过原子 ledger API 提交。

    本入口故意不读取 live Registry，也不调用旧的 generic
    ``execute_scheme`` 写入路径。Registry target、代码版本、输入 generation
    与日期均来自 occurrence 创建时冻结的执行信封。
    """
    process_start_guard = require_process_start_guard(
        process_start_guard
    )
    if _process_fence is not None and process_start_guard is None:
        raise TypeError(
            "process fence requires canonical ProcessStartGuard"
        )
    envelope = read_schedule_execution_envelope(
        engine,
        item_id=int(item_id),
    )
    try:
        _assert_execution_epoch(engine, envelope)
        if _process_fence is not None:
            if process_start_guard is None:
                raise AssertionError(
                    "validated process fence lost its start guard"
                )
            with process_start_guard:
                _invoke_process_fence(_process_fence)
        attempt = start_schedule_attempt(
            engine,
            item_id=int(item_id),
            trigger_origin=trigger_origin,
        )
    except (ScheduledProcessFenceError, ProcessGroupTerminationError) as exc:
        return ScheduledItemExecutionResult(
            item_id=int(item_id),
            scheme_id=envelope.item.base_scheme_id,
            status="recovery_blocked",
            run_id=None,
            attempt_no=None,
            failure_code=None,
            error_message=_bounded_error(exc),
        )
    except (RuntimeError, ValueError) as exc:
        return ScheduledItemExecutionResult(
            item_id=int(item_id),
            scheme_id=envelope.item.base_scheme_id,
            status="claim_rejected",
            run_id=None,
            attempt_no=None,
            failure_code=FAILURE_STALE_ATTEMPT,
            error_message=_bounded_error(exc),
        )
    try:
        _assert_execution_epoch(engine, envelope)
        config = _load_frozen_config(
            envelope,
            project_root=project_root,
        )
        input_compatibility = _frozen_input_compatibility(envelope)
        live_source_package_sha256 = (
            _frozen_live_source_package_sha256(
                envelope,
                input_compatibility=input_compatibility,
            )
        )
        native_generation, databridge_generation, calendar_generation = (
            _open_frozen_generations(
                envelope,
                databridge_schema_path=databridge_schema_path,
            )
        )
        timeout_sec = _scheduled_timeout(
            config,
            default_timeout_sec=default_timeout_sec,
        )
        cache_use_qualification = _frozen_cache_use_qualification(
            envelope,
        )
        direct_cache_runtime_context = (
            _frozen_direct_cache_runtime_context(envelope)
        )

        def process_started(pid: int, pgid: int) -> None:
            try:
                _assert_execution_epoch(engine, envelope)
                register_schedule_attempt_process(
                    engine,
                    run_id=attempt.run_id,
                    execution_token=attempt.execution_token,
                    process_id=pid,
                    process_group_id=pgid,
                )
                _assert_execution_epoch(engine, envelope)
            except RuntimeError as exc:
                if "recovery cutoff" in str(exc).casefold():
                    raise ScheduledRecoveryCutoffError(
                        str(exc)
                    ) from exc
                raise

        try:
            records = run_configured_scheme(
                config,
                envelope.occurrence.predict_date,
                engine=engine,
                algo_env=algo_env,
                timeout_sec=timeout_sec,
                native_generation=native_generation,
                live_source_compatibility=(
                    input_compatibility == LIVE_SOURCE_INPUT_MODE
                ),
                live_source_package_sha256=(
                    live_source_package_sha256
                ),
                cache_use_qualification=cache_use_qualification,
                direct_cache_runtime_context=(
                    direct_cache_runtime_context
                ),
                databridge_generation=databridge_generation,
                calendar_generation=calendar_generation,
                execution_token=attempt.execution_token,
                process_started=process_started,
                process_fence=lambda: _assert_scheduled_process_fence(
                    engine,
                    envelope=envelope,
                    process_fence=_process_fence,
                ),
                process_start_guard=process_start_guard,
            )
        except ScheduledEpochDriftError:
            raise
        except ScheduledProcessFenceError:
            raise
        except Exception as exc:
            raise _classify_algorithm_exception(exc) from exc
        _verify_input_compatibility_records(
            records,
            envelope=envelope,
            input_compatibility=input_compatibility,
        )
        if cache_use_qualification is not None:
            _verify_cache_qualified_records(
                records,
                envelope=envelope,
                expected_qualification=cache_use_qualification,
            )
        normalized = _normalize_scheduled_records(
            records,
            envelope=envelope,
            run_id=attempt.run_id,
        )
        verifier = trusted_verifier or FilesystemScheduledCompletionVerifier(
            project_root=Path(project_root).resolve(),
            databridge_schema_path=Path(
                databridge_schema_path
            ).resolve(),
        )
        try:
            _assert_execution_epoch(engine, envelope)
            written = complete_scheduled_attempt(
                engine,
                run_id=attempt.run_id,
                records=normalized,
                scheme_version=envelope.item.scheme_version,
                trusted_verifier=verifier,
            )
        except ScheduledVerificationError as exc:
            message = str(exc)
            if "generation" in message.casefold():
                raise ScheduledDataError(message) from exc
            raise ScheduledContractError(message) from exc
        except Exception as exc:
            if _is_epoch_drift_error(str(exc)):
                raise ScheduledEpochDriftError(str(exc)) from exc
            if isinstance(exc, DBAPIError):
                raise
            message = str(exc)
            if _is_stale_fence_error(message):
                raise
            if any(
                token in message.casefold()
                for token in (
                    "generation",
                    "manifest",
                    "feature_date",
                    "evidence",
                )
            ):
                raise ScheduledDataError(message) from exc
            raise ScheduledResultError(message) from exc
        return ScheduledItemExecutionResult(
            item_id=int(item_id),
            scheme_id=envelope.item.base_scheme_id,
            status="success",
            run_id=attempt.run_id,
            attempt_no=attempt.attempt_no,
            records_written=written,
        )
    except Exception as exc:
        if isinstance(exc, ScheduledEpochDriftError):
            return ScheduledItemExecutionResult(
                item_id=int(item_id),
                scheme_id=envelope.item.base_scheme_id,
                status="stale_rejected",
                run_id=attempt.run_id,
                attempt_no=attempt.attempt_no,
                failure_code=FAILURE_STALE_ATTEMPT,
                error_message=_bounded_error(exc),
            )
        if isinstance(exc, ProcessGroupTerminationError):
            return _fence_unconfirmed_process_group(
                engine,
                envelope=envelope,
                attempt=attempt,
                error=exc,
            )
        if isinstance(exc, ScheduledProcessFenceError):
            return ScheduledItemExecutionResult(
                item_id=int(item_id),
                scheme_id=envelope.item.base_scheme_id,
                status="recovery_blocked",
                run_id=attempt.run_id,
                attempt_no=attempt.attempt_no,
                failure_code=None,
                error_message=_bounded_error(exc),
            )
        failure_code = _failure_code(exc)
        message = _bounded_error(exc)
        if _is_stale_fence_error(message):
            return ScheduledItemExecutionResult(
                item_id=int(item_id),
                scheme_id=envelope.item.base_scheme_id,
                status="stale_rejected",
                run_id=attempt.run_id,
                attempt_no=attempt.attempt_no,
                failure_code=FAILURE_STALE_ATTEMPT,
                error_message=message,
            )
        try:
            if failure_code == FAILURE_TRANSIENT_INFRA:
                state = mark_schedule_attempt_retry_wait(
                    engine,
                    run_id=attempt.run_id,
                    failure_code=failure_code,
                    error_message=message,
                )
                status = (
                    "retry_wait"
                    if state == "RETRY_WAIT"
                    else "failed"
                )
            else:
                mark_schedule_attempt_terminal_failure(
                    engine,
                    run_id=attempt.run_id,
                    failure_code=failure_code,
                    error_message=message,
                )
                status = "failed"
        except Exception as audit_exc:
            if _is_stale_fence_error(str(audit_exc)):
                return ScheduledItemExecutionResult(
                    item_id=int(item_id),
                    scheme_id=envelope.item.base_scheme_id,
                    status="stale_rejected",
                    run_id=attempt.run_id,
                    attempt_no=attempt.attempt_no,
                    failure_code=FAILURE_STALE_ATTEMPT,
                    error_message=message,
                )
            raise RuntimeError(
                "scheduled attempt failure could not be persisted: "
                f"run_id={attempt.run_id}; original={message}; "
                f"audit={audit_exc}"
            ) from audit_exc
        return ScheduledItemExecutionResult(
            item_id=int(item_id),
            scheme_id=envelope.item.base_scheme_id,
            status=status,
            run_id=attempt.run_id,
            attempt_no=attempt.attempt_no,
            failure_code=failure_code,
            error_message=message,
        )


def _load_frozen_config(
    envelope: ScheduleExecutionEnvelope,
    *,
    project_root: str | Path,
) -> SchemeConfig:
    root = Path(project_root).resolve()
    schemes_root = (root / "schemes").resolve()
    scheme_id = envelope.item.base_scheme_id
    config_path = (
        schemes_root / scheme_id / "config.yaml"
    ).resolve(strict=False)
    if schemes_root not in config_path.parents or config_path.name != "config.yaml":
        raise ScheduledContractError("scheme config escaped schemes root")
    try:
        config = load_scheme_config(config_path)
    except Exception as exc:
        raise ScheduledContractError(
            f"frozen scheme config cannot be loaded: {exc}"
        ) from exc
    expected = {
        "scheme_id": scheme_id,
        "runtime_type": envelope.item.runtime_type,
        "scheme_version": envelope.item.scheme_version,
        "code_hash": envelope.item.code_sha256,
        "config_hash": envelope.item.config_sha256,
        "frequency": "daily",
    }
    actual = {
        "scheme_id": config.scheme_id,
        "runtime_type": config.runtime_type,
        "scheme_version": config.scheme_version,
        "code_hash": config.code_hash,
        "config_hash": config.config_hash,
        "frequency": config.frequency,
    }
    drift = sorted(
        field
        for field, expected_value in expected.items()
        if actual[field] != expected_value
    )
    if drift:
        raise ScheduledContractError(
            "frozen scheme identity drift: " + ", ".join(drift)
        )
    return config


def _assert_execution_epoch(
    engine,
    envelope: ScheduleExecutionEnvelope,
) -> None:
    """在 claim/process/commit 边界验证冻结 exact epoch capability。"""
    try:
        assert_daily_coordinator_epoch_matches_policy(
            envelope.occurrence.policy_json,
            engine=engine,
        )
    except Exception as exc:
        raise ScheduledEpochDriftError(
            "daily occurrence coordinator epoch differs from current "
            "machine-global epoch"
        ) from exc


def _invoke_process_fence(
    process_fence: Callable[[], None],
) -> None:
    """把外部 replay fence 的任意失败稳定映射为恢复阻断。"""
    try:
        process_fence()
    except ScheduledProcessFenceError:
        raise
    except Exception as exc:
        raise ScheduledProcessFenceError(
            "isolated replay process boundary could not be confirmed"
        ) from exc


def _assert_scheduled_process_fence(
    engine,
    *,
    envelope: ScheduleExecutionEnvelope,
    process_fence: Callable[[], None] | None,
) -> None:
    """在 canonical guard 内组合 epoch 与隔离进程边界。"""
    _assert_execution_epoch(engine, envelope)
    if process_fence is not None:
        _invoke_process_fence(process_fence)


def _open_frozen_generations(
    envelope: ScheduleExecutionEnvelope,
    *,
    databridge_schema_path: str | Path,
):
    generation = envelope.generation
    if generation.state != "SEALED":
        raise ScheduledDataError(
            f"bound generation is not SEALED: {generation.generation_id}"
        )
    if generation.business_date != envelope.occurrence.predict_date:
        raise ScheduledDataError(
            "bound generation business_date differs from occurrence"
        )
    if envelope.item.runtime_type == "native_adapter":
        if generation.generation_type != "native_source":
            raise ScheduledDataError(
                "Native item requires native_source generation"
            )
        try:
            native = _open_native(generation)
        except Exception as exc:
            raise ScheduledDataError(
                f"Native generation verification failed: {exc}"
            ) from exc
        return native, None, None
    if envelope.item.runtime_type != "blackbox_v2":
        raise ScheduledContractError(
            f"unsupported frozen runtime_type={envelope.item.runtime_type}"
        )
    if generation.generation_type != "databridge_v1":
        raise ScheduledDataError(
            "Blackbox V2 item requires databridge_v1 generation"
        )
    calendar_generation = getattr(
        envelope,
        "calendar_generation",
        None,
    )
    if calendar_generation is None:
        raise ScheduledDataError(
            "DataBridge generation has no frozen Native calendar generation"
        )
    if (
        calendar_generation.state != "SEALED"
        or calendar_generation.generation_type != "native_source"
    ):
        raise ScheduledDataError(
            "linked Native calendar generation is not SEALED native_source"
        )
    if (
        calendar_generation.business_date != generation.business_date
        or calendar_generation.feature_date != generation.feature_date
    ):
        raise ScheduledDataError(
            "DataBridge and Native calendar generation dates differ"
        )
    linked_id = getattr(generation, "native_generation_id", None)
    linked_sha = getattr(generation, "native_manifest_sha256", None)
    if linked_id is not None and linked_id != calendar_generation.generation_id:
        raise ScheduledDataError(
            "DataBridge linked Native generation id drifted"
        )
    if (
        linked_sha is not None
        and linked_sha != calendar_generation.manifest_sha256
    ):
        raise ScheduledDataError(
            "DataBridge linked Native manifest hash drifted"
        )
    try:
        calendar = _open_native(calendar_generation)
        databridge = open_databridge_generation(
            Path(generation.manifest_uri),
            expected_generation_id=generation.generation_id,
            expected_manifest_sha256=generation.manifest_sha256,
            expected_business_date=generation.business_date,
            expected_feature_date=generation.feature_date,
            schema_path=Path(databridge_schema_path),
        )
    except Exception as exc:
        raise ScheduledDataError(
            f"DataBridge generation verification failed: {exc}"
        ) from exc
    return None, databridge, calendar


def _open_native(generation):
    return open_native_generation(
        Path(generation.manifest_uri),
        expected_generation_id=generation.generation_id,
        expected_manifest_sha256=generation.manifest_sha256,
        expected_business_date=generation.business_date,
        expected_feature_date=generation.feature_date,
    )


def _scheduled_timeout(
    config: SchemeConfig,
    *,
    default_timeout_sec: int,
) -> int:
    if default_timeout_sec <= 0:
        raise ScheduledContractError(
            "default_timeout_sec must be positive"
        )
    if config.runtime_type == "blackbox_v2":
        return min(
            V2_HARD_TIMEOUT_SEC,
            _effective_timeout_sec(config, V2_HARD_TIMEOUT_SEC),
        )
    return _effective_timeout_sec(config, default_timeout_sec)


def _normalize_scheduled_records(
    records: Iterable[PredictionRecord],
    *,
    envelope: ScheduleExecutionEnvelope,
    run_id: int,
) -> list[PredictionRecord]:
    if not isinstance(records, (list, tuple)):
        raise ScheduledResultError(
            "algorithm result must be a materialized list or tuple"
        )
    normalized: list[PredictionRecord] = []
    for record in records:
        if not isinstance(record, PredictionRecord):
            raise ScheduledResultError(
                "algorithm result contains a non-PredictionRecord value"
            )
        extra = dict(record.extra or {})
        feature_date = record.feature_date or extra.get("feature_date")
        if feature_date is None:
            raise ScheduledResultError(
                "scheduled prediction requires feature_date"
            )
        extra.update(
            {
                "feature_date": str(feature_date),
                "prediction_phase": "scheduled_live",
                "input_generation_id":
                    envelope.generation.generation_id,
                "input_generation_manifest_sha256":
                    envelope.generation.manifest_sha256,
                "schedule_occurrence_id":
                    envelope.occurrence.occurrence_id,
                "schedule_item_id": envelope.item.item_id,
            }
        )
        normalized.append(
            replace(
                record,
                feature_date=str(feature_date),
                prediction_phase="scheduled_live",
                run_id=int(run_id),
                scheme_version=envelope.item.scheme_version,
                extra=extra,
            )
        )
    return normalized


def _classify_algorithm_exception(exc: Exception) -> Exception:
    if isinstance(exc, ProcessGroupTerminationError):
        return exc
    if isinstance(exc, ScheduledRecoveryCutoffError):
        return exc
    if isinstance(exc, DBAPIError):
        return exc
    if isinstance(exc, subprocess.TimeoutExpired):
        return TimeoutError(str(exc))
    if isinstance(exc, TimeoutError):
        return exc
    if isinstance(exc, json.JSONDecodeError):
        return ScheduledResultError(str(exc))
    if isinstance(exc, subprocess.CalledProcessError):
        return RuntimeError(str(exc))
    if isinstance(exc, OSError):
        return exc
    message = str(exc)
    lowered = message.casefold()
    if "timeout" in lowered or "deadline" in lowered:
        return TimeoutError(message)
    if any(
        token in lowered
        for token in (
            "output",
            "predictionrecord",
            "result",
            "returned",
            "schema",
        )
    ):
        return ScheduledResultError(message)
    if isinstance(exc, ValueError):
        return ScheduledResultError(message)
    return RuntimeError(message)


def _fence_unconfirmed_process_group(
    engine,
    *,
    envelope: ScheduleExecutionEnvelope,
    attempt,
    error: ProcessGroupTerminationError,
) -> ScheduledItemExecutionResult:
    """先持久化 completion fence；fence 失败时绝不转入重试。"""
    message = _bounded_error(error)
    try:
        fence_current_schedule_attempt(
            engine,
            item_id=int(attempt.item_id),
        )
    except Exception as fence_error:
        return ScheduledItemExecutionResult(
            item_id=int(attempt.item_id),
            scheme_id=envelope.item.base_scheme_id,
            status="recovery_blocked",
            run_id=attempt.run_id,
            attempt_no=attempt.attempt_no,
            failure_code=None,
            error_message=_bounded_text(
                "process cleanup fence could not be persisted; "
                "attempt remains RUNNING for coordinator recovery: "
                f"original={message}; fence={fence_error}"
            ),
        )
    return ScheduledItemExecutionResult(
        item_id=int(attempt.item_id),
        scheme_id=envelope.item.base_scheme_id,
        status="fenced_pending_cleanup",
        run_id=attempt.run_id,
        attempt_no=attempt.attempt_no,
        failure_code=FAILURE_ABANDONED_FENCE_PENDING_CLEANUP,
        error_message=message,
    )


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, (IntegrityError, DataError)):
        return FAILURE_RESULT
    if isinstance(exc, ProgrammingError):
        return FAILURE_CONTRACT
    if isinstance(exc, OperationalError):
        if _is_transient_mysql_operational_error(exc):
            return FAILURE_TRANSIENT_INFRA
        return FAILURE_CONTRACT
    if isinstance(exc, DBAPIError):
        return FAILURE_CONTRACT
    if isinstance(exc, ScheduledDataError):
        return FAILURE_DATA
    if isinstance(exc, ScheduledContractError):
        return FAILURE_CONTRACT
    if isinstance(exc, ScheduledResultError):
        return FAILURE_RESULT
    if isinstance(exc, ScheduledRecoveryCutoffError):
        return FAILURE_RECOVERY_CUTOFF_EXPIRED
    if isinstance(exc, subprocess.TimeoutExpired):
        return FAILURE_TIMEOUT
    if isinstance(exc, TimeoutError):
        if _is_transient_os_error(exc):
            return FAILURE_TRANSIENT_INFRA
        return FAILURE_TIMEOUT
    if isinstance(exc, OSError):
        if _is_transient_os_error(exc):
            return FAILURE_TRANSIENT_INFRA
        return FAILURE_CONTRACT
    return FAILURE_ALGORITHM


def _is_transient_mysql_operational_error(
    exc: OperationalError,
) -> bool:
    return (
        _dbapi_driver_code(exc)
        in _TRANSIENT_MYSQL_OPERATIONAL_CODES
    )


def _dbapi_driver_code(exc: DBAPIError) -> int | None:
    original = getattr(exc, "orig", None)
    candidates = [
        getattr(original, "errno", None),
    ]
    original_args = getattr(original, "args", ())
    if original_args:
        candidates.append(original_args[0])
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isascii() and value.isdigit():
            return int(value)
    return None


def _is_transient_os_error(exc: OSError) -> bool:
    error_number = getattr(exc, "errno", None)
    if error_number in _TRANSIENT_OS_ERRNOS:
        return True
    return (
        isinstance(exc, ConnectionError)
        and error_number in _TRANSIENT_CONNECTION_ERRNOS
    )


def _is_stale_fence_error(message: str) -> bool:
    lowered = message.casefold()
    return "stale scheduled run" in lowered or "stale attempt" in lowered


def _is_epoch_drift_error(message: str) -> bool:
    lowered = message.casefold()
    return (
        "coordinator epoch" in lowered
        or "machine-global epoch" in lowered
    )


def _bounded_error(exc: Exception) -> str:
    return _bounded_text(f"{type(exc).__name__}: {exc}".strip())


def _bounded_text(value: str) -> str:
    if len(value) <= _MAX_ERROR_LENGTH:
        return value
    return value[: _MAX_ERROR_LENGTH - 3] + "..."
