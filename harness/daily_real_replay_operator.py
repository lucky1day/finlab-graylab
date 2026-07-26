"""真实 21/25 隔离联跑的只读、fail-closed operator 预检。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy import text

from harness.daily_real_replay import (
    DailyRealReplayInputs,
    _real_replay_lock_root,
    open_real_replay_generations,
)
from scheduler.daily_coordinator import (
    OccurrenceFileLock,
    OccurrenceLockUnavailable,
)
from scheduler.daily_policy import (
    APPROVED_0629_LIVE_SOURCE_SCHEMES,
    DEFAULT_POLICY_PATH,
    load_daily_policy,
)
from scheduler.discovery import discover_schemes
from scheduler.repository import create_engine_from_env
from scheduler.daily_control_plane_probe import (
    probe_daily_transition_quiescence,
    probe_launchagent_service_states,
    read_installed_launchagent_modes,
)
from shared.calendar_service import FrozenCalendarService
from shared.calendar_service import get_calendar
from shared.data_contract import (
    CALENDAR_SOURCE_TABLES,
    FACTOR_SOURCE_TABLES,
    METADATA_SOURCE_TABLE,
    capture_source_commit_evidence,
    inspect_native_input_readiness,
)
from shared.input_artifacts import create_input_engine
from shared.source_runtime_database import (
    load_source_runtime_database_config,
    preflight_source_runtime_database_access,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_BRANCH = "codex/audit-bugfixes-20260613"
PREFLIGHT_SCHEMA_VERSION = "daily-real-replay-preflight-v1"
EXPECTED_PRODUCTION_MIGRATIONS = tuple(range(1, 18))
LAUNCHAGENT_LABELS = (
    "com.bond-factor-lab.backend",
    "com.bond-factor-lab.scheduler",
    "com.bond-factor-lab.v2-preflight",
)
_RUNTIME_CODE_ROOTS = (
    "harness",
    "scheduler",
    "schemes",
    "shared",
)
_IGNORED_EXECUTABLE_SUFFIXES = frozenset(
    {".py", ".pyw", ".sh", ".bash", ".zsh"}
)
_SOURCE_TABLES = (
    *FACTOR_SOURCE_TABLES,
    METADATA_SOURCE_TABLE,
    *CALENDAR_SOURCE_TABLES,
)
_QUIESCENCE_FIELDS = frozenset(
    {
        "active_occurrence_count",
        "nonterminal_item_count",
        "running_ledger_run_count",
        "cleanup_pending_count",
        "running_legacy_scheduled_live_run_count",
        "registered_process_alive_count",
        "project_process_count",
    }
)


class DailyRealReplayPreflightError(RuntimeError):
    """真实联跑预检的稳定、脱敏阻断错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _ReplayDispatchIdentity:
    """成功预检后一次性绑定到锁会话的脱敏 dispatch 身份。"""

    service_uid: int
    business_date: str
    native_manifest_path: str
    databridge_manifest_path: str
    candidate_digest: str
    generation_digest: str
    definition_digest: str
    control_plane_digest: str
    production_digest: str
    source_database_digest: str
    source_watermark_digest: str

    def __post_init__(self) -> None:
        try:
            date.fromisoformat(self.business_date)
            paths = (
                Path(self.native_manifest_path),
                Path(self.databridge_manifest_path),
            )
            digests = (
                self.candidate_digest,
                self.generation_digest,
                self.definition_digest,
                self.control_plane_digest,
                self.production_digest,
                self.source_database_digest,
                self.source_watermark_digest,
            )
            valid = (
                isinstance(self.service_uid, int)
                and not isinstance(self.service_uid, bool)
                and self.service_uid >= 0
                and all(
                    path.is_absolute()
                    and path == path.resolve(strict=False)
                    for path in paths
                )
                and all(
                    len(value) == 64
                    and set(value) <= set("0123456789abcdef")
                    for value in digests
                )
            )
        except Exception:
            valid = False
        if not valid:
            raise ValueError("real replay dispatch identity is invalid")


@dataclass(frozen=True)
class _ReplayOperatorSession:
    """同一进程持续持有的 operator/runtime 双重文件锁。"""

    owner_pid: int
    operator_lock: OccurrenceFileLock
    runtime_lock: OccurrenceFileLock
    operator_file_identity: tuple[int, int]
    runtime_file_identity: tuple[int, int]
    _dispatch_identity: _ReplayDispatchIdentity | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def assert_held(self) -> None:
        """拒绝跨进程、已释放或路径被替换的锁会话。"""
        try:
            valid = (
                self.owner_pid == os.getpid()
                and self.operator_lock.acquired
                and self.runtime_lock.acquired
                and self.operator_lock.path.name
                == "real-replay-operator.lock"
                and self.runtime_lock.path.name
                == "real-replay-runtime.lock"
                and self.operator_lock.path.parent
                == self.runtime_lock.path.parent
                and _lock_file_identity(self.operator_lock.path)
                == self.operator_file_identity
                and _lock_file_identity(self.runtime_lock.path)
                == self.runtime_file_identity
            )
        except Exception:
            valid = False
        if not valid:
            raise DailyRealReplayPreflightError(
                "PREFLIGHT_SESSION_NOT_HELD"
            )

    def __enter__(self) -> _ReplayOperatorSession:
        """借用已持有的会话；资源所有权仍属于外层 session manager。"""
        self.assert_held()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.assert_held()

    def bind_dispatch_identity(
        self,
        identity: _ReplayDispatchIdentity,
    ) -> None:
        """只允许成功预检在持锁期间绑定一次 execution capability。"""
        self.assert_held()
        if type(identity) is not _ReplayDispatchIdentity:
            raise DailyRealReplayPreflightError(
                "REPLAY_DISPATCH_IDENTITY_INVALID"
            )
        if self._dispatch_identity is not None:
            raise DailyRealReplayPreflightError(
                "REPLAY_DISPATCH_IDENTITY_ALREADY_BOUND"
            )
        object.__setattr__(self, "_dispatch_identity", identity)

    def require_dispatch_identity(self) -> _ReplayDispatchIdentity:
        """返回仍由本 session 持有的 write-once dispatch 身份。"""
        self.assert_held()
        identity = self._dispatch_identity
        if type(identity) is not _ReplayDispatchIdentity:
            raise DailyRealReplayPreflightError(
                "REPLAY_DISPATCH_IDENTITY_UNBOUND"
            )
        return identity


@dataclass(frozen=True)
class ReplayCandidateIdentity:
    """候选 Git 闭包及 policy bytes 的不可变身份。"""

    branch: str
    git_head: str
    tree_sha256: str
    policy_sha256: str


@dataclass(frozen=True)
class ReplayExpectedRegistryRow:
    """从严格 discovery/policy 推导出的 Registry target 身份。"""

    registry_scheme_id: str
    base_scheme_id: str
    runtime_type: str
    task_type: str
    target_tenor: str
    horizon: int


@dataclass(frozen=True)
class ReplayExpectedVersionRow:
    """一个日频 execution 的精确代码与配置身份。"""

    scheme_id: str
    scheme_version: str
    runtime_type: str
    code_sha256: str
    config_sha256: str
    manifest_sha256: str | None = None
    algorithm_version: str | None = None
    contract_version: str | None = None
    runtime_profile: str | None = None
    environment_fingerprint: str | None = None
    data_snapshot_id: str | None = None


@dataclass(frozen=True)
class ReplayDefinitionSnapshot:
    """当前仓库严格 discovery 与版本化 policy 的归一化快照。"""

    policy_version: str
    policy_sha256: str
    expected_item_count: int
    expected_target_count: int
    native_item_count: int
    v2_item_count: int
    input_mode_counts: Mapping[str, int]
    registry_rows: tuple[ReplayExpectedRegistryRow, ...]
    version_rows: tuple[ReplayExpectedVersionRow, ...]


@dataclass(frozen=True)
class ProductionDailySnapshot:
    """生产库单一只读一致性事务内取得的最小候选快照。"""

    database_name: str
    server_identity_sha256: str
    migration_rows: tuple[Mapping[str, object], ...]
    registry_rows: tuple[Mapping[str, object], ...]
    version_rows: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class ReplayQuiescenceSnapshot:
    """会与真实算法回放争用控制面或 Mac 资源的进程快照。"""

    loaded_launchagent_labels: tuple[str, ...]
    counters: Mapping[str, int]
    digest: str


@dataclass(frozen=True)
class ReplaySourceInputEvidence:
    """三个 live_source_0629 共用的就绪水位观察证据。"""

    feature_date: str
    source_commit_token: str


@dataclass(frozen=True)
class ReplayControlPlaneSnapshot:
    """隔离联跑期间必须保持的 legacy/BLOCKED 发布边界。"""

    installed_modes: Mapping[str, str]
    rollout_mode: str
    admission_status: str
    digest: str


@dataclass(frozen=True)
class DailyRealReplayPreflightReport:
    """瞬时、仅供人工判断的报告；不是 execute capability 或准入证据。"""

    schema_version: str
    status: str
    qualification: str
    checked_at: str
    branch: str
    git_head: str
    candidate_tree_sha256: str
    policy_version: str
    policy_sha256: str
    business_date: str
    feature_date: str
    native_generation_id: str
    native_manifest_sha256: str
    databridge_generation_id: str
    databridge_manifest_sha256: str
    expected_item_count: int
    expected_target_count: int
    native_item_count: int
    v2_item_count: int
    input_mode_counts: Mapping[str, int]
    production_server_identity_sha256: str
    production_registry_digest: str
    source_database_identity_sha256: str
    source_table_count: int
    source_watermark_start_sha256: str
    source_watermark_end_sha256: str
    quiescence_digest: str
    control_plane_digest: str
    preflight_digest: str


def run_real_replay_preflight(
    *,
    native_manifest: str | Path,
    databridge_manifest: str | Path,
) -> DailyRealReplayPreflightReport:
    """在全局 session lock 内完成只读检查，并在返回前二次验明身份。"""
    service_uid = _require_operator_service_uid()
    try:
        session_context = _preflight_session()
    except Exception:
        raise DailyRealReplayPreflightError(
            "PREFLIGHT_SESSION_UNAVAILABLE"
        ) from None
    with session_context as session:
        return _run_real_replay_preflight_locked(
            session,
            service_uid=service_uid,
            native_manifest=native_manifest,
            databridge_manifest=databridge_manifest,
        )


def _run_real_replay_preflight_locked(
    session: _ReplayOperatorSession,
    *,
    service_uid: int,
    native_manifest: str | Path,
    databridge_manifest: str | Path,
) -> DailyRealReplayPreflightReport:
    """只在调用方持续持有同一双锁时执行完整预检。"""
    with session as locked_session:
        locked_session.assert_held()
        candidate = _stable_candidate_identity()
        inputs = _stable_generation_inputs(
            native_manifest=native_manifest,
            databridge_manifest=databridge_manifest,
        )
        definitions = _stable_definition_snapshot(inputs)
        if candidate.policy_sha256 != definitions.policy_sha256:
            raise DailyRealReplayPreflightError(
                "CANDIDATE_POLICY_IDENTITY_DRIFT"
            )
        control_plane = _read_control_plane_boundary(
            service_uid
        )

        source_config, source_preflight, source_start = (
            _read_source_database_preflight(inputs)
        )

        try:
            production = _read_production_daily_snapshot()
        except DailyRealReplayPreflightError:
            raise
        except Exception:
            raise DailyRealReplayPreflightError(
                "PRODUCTION_SNAPSHOT_UNAVAILABLE"
            ) from None
        registry_digest = validate_production_daily_snapshot(
            production,
            definitions=definitions,
        )

        try:
            quiescence = _probe_replay_quiescence(
                inputs.business_date,
                service_uid=service_uid,
            )
        except DailyRealReplayPreflightError:
            raise
        except Exception:
            raise DailyRealReplayPreflightError(
                "QUIESCENCE_PROBE_FAILED"
            ) from None
        validate_replay_quiescence(quiescence)

        current_candidate = _stable_candidate_identity()
        current_inputs = _stable_generation_inputs(
            native_manifest=native_manifest,
            databridge_manifest=databridge_manifest,
        )
        current_definitions = _stable_definition_snapshot(current_inputs)
        current_control_plane = _read_control_plane_boundary(
            service_uid
        )
        (
            current_source_config,
            current_source_preflight,
            source_end,
        ) = (
            _read_source_database_preflight(current_inputs)
        )
        try:
            current_production = _read_production_daily_snapshot()
            current_registry_digest = (
                validate_production_daily_snapshot(
                    current_production,
                    definitions=current_definitions,
                )
            )
            current_quiescence = _probe_replay_quiescence(
                current_inputs.business_date,
                service_uid=service_uid,
            )
            validate_replay_quiescence(current_quiescence)
        except DailyRealReplayPreflightError:
            raise
        except Exception:
            raise DailyRealReplayPreflightError(
                "PREFLIGHT_IDENTITY_DRIFT"
            ) from None
        if (
            current_candidate != candidate
            or _generation_identity(current_inputs)
            != _generation_identity(inputs)
            or current_definitions != definitions
            or current_control_plane != control_plane
            or _source_database_identity(
                current_source_config,
                current_source_preflight,
            )
            != _source_database_identity(
                source_config,
                source_preflight,
            )
            or current_production != production
            or current_registry_digest != registry_digest
            or current_quiescence != quiescence
        ):
            raise DailyRealReplayPreflightError(
                "PREFLIGHT_IDENTITY_DRIFT"
            )

        locked_session.bind_dispatch_identity(
            _build_replay_dispatch_identity(
                service_uid=service_uid,
                native_manifest=native_manifest,
                databridge_manifest=databridge_manifest,
                candidate=candidate,
                inputs=inputs,
                definitions=definitions,
                control_plane=control_plane,
                production=production,
                production_registry_digest=registry_digest,
                source_config=source_config,
                source_preflight=source_preflight,
                source_start=source_start,
                source_end=source_end,
            )
        )
        locked_session.assert_held()
        checked_at = datetime.now(timezone.utc).isoformat()
        report_payload: dict[str, object] = {
            "schema_version": PREFLIGHT_SCHEMA_VERSION,
            "status": "CHECK_PASSED",
            "qualification": "EXCLUDED",
            "checked_at": checked_at,
            "branch": candidate.branch,
            "git_head": candidate.git_head,
            "candidate_tree_sha256": candidate.tree_sha256,
            "policy_version": definitions.policy_version,
            "policy_sha256": definitions.policy_sha256,
            "business_date": inputs.business_date,
            "feature_date": inputs.feature_date,
            "native_generation_id":
                inputs.native_generation.generation_id,
            "native_manifest_sha256":
                inputs.native_generation.manifest_sha256,
            "databridge_generation_id":
                inputs.databridge_generation.generation_id,
            "databridge_manifest_sha256":
                inputs.databridge_generation.manifest_sha256,
            "expected_item_count": definitions.expected_item_count,
            "expected_target_count": definitions.expected_target_count,
            "native_item_count": definitions.native_item_count,
            "v2_item_count": definitions.v2_item_count,
            "input_mode_counts": dict(definitions.input_mode_counts),
            "production_server_identity_sha256":
                production.server_identity_sha256,
            "production_registry_digest": registry_digest,
            "source_database_identity_sha256":
                source_config.cache_identity,
            "source_table_count": len(source_preflight.tables),
            "source_watermark_start_sha256":
                source_start.source_commit_token,
            "source_watermark_end_sha256":
                source_end.source_commit_token,
            "quiescence_digest": quiescence.digest,
            "control_plane_digest": control_plane.digest,
        }
        preflight_digest = _canonical_sha256(report_payload)
        return DailyRealReplayPreflightReport(
            **report_payload,
            preflight_digest=preflight_digest,
        )


def validate_production_daily_snapshot(
    snapshot: ProductionDailySnapshot,
    *,
    definitions: ReplayDefinitionSnapshot,
) -> str:
    """验证生产 Registry/version 与候选精确一致，返回脱敏摘要。"""
    if snapshot.database_name != "bond_db":
        raise DailyRealReplayPreflightError(
            "PRODUCTION_DATABASE_IDENTITY_DRIFT"
        )
    if (
        not _is_hex_digest(
            snapshot.server_identity_sha256,
            length=64,
        )
    ):
        raise DailyRealReplayPreflightError(
            "PRODUCTION_DATABASE_IDENTITY_DRIFT"
        )
    expected_migrations = _expected_production_migrations()
    try:
        actual_migrations = tuple(
            (
                int(row["version"]),
                str(row["filename"]),
                str(row["checksum_sha256"]),
                str(row["state"]),
            )
            for row in snapshot.migration_rows
        )
    except (KeyError, TypeError, ValueError):
        raise DailyRealReplayPreflightError(
            "PRODUCTION_MIGRATION_DRIFT"
        ) from None
    if (
        tuple(row[0] for row in actual_migrations)
        != EXPECTED_PRODUCTION_MIGRATIONS
        or actual_migrations != expected_migrations
    ):
        raise DailyRealReplayPreflightError(
            "PRODUCTION_MIGRATION_DRIFT"
        )

    expected_registry = tuple(
        sorted(
            (
                row.registry_scheme_id,
                row.base_scheme_id,
                row.runtime_type,
                "active",
                "daily",
                row.task_type,
                row.target_tenor,
                row.horizon,
            )
            for row in definitions.registry_rows
        )
    )
    try:
        actual_registry = tuple(
            sorted(
                (
                    str(row["scheme_id"]),
                    str(row["base_scheme_id"]),
                    str(row["runtime_type"]),
                    str(row["status"]),
                    str(row["frequency"]),
                    str(row["task_type"]),
                    str(row["target_tenor"]),
                    int(row["horizon"]),
                )
                for row in snapshot.registry_rows
            )
        )
    except (KeyError, TypeError, ValueError):
        raise DailyRealReplayPreflightError(
            "PRODUCTION_REGISTRY_DRIFT"
        ) from None
    if actual_registry != expected_registry:
        raise DailyRealReplayPreflightError(
            "PRODUCTION_REGISTRY_DRIFT"
        )
    if {
        row[1] for row in actual_registry
    } != {
        row.scheme_id for row in definitions.version_rows
    }:
        raise DailyRealReplayPreflightError(
            "PRODUCTION_REGISTRY_DRIFT"
        )

    expected_versions = tuple(
        sorted(
            (
                row.scheme_id,
                row.scheme_version,
                row.runtime_type,
                row.code_sha256,
                row.config_sha256,
                row.manifest_sha256,
                row.algorithm_version,
                row.contract_version,
                row.runtime_profile,
                row.environment_fingerprint,
                row.data_snapshot_id,
                "active",
            )
            for row in definitions.version_rows
        )
    )
    expected_scheme_ids = {row[0] for row in expected_versions}
    try:
        actual_versions = tuple(
            sorted(
                (
                    str(row["scheme_id"]),
                    str(row["scheme_version"]),
                    str(row["runtime_type"]),
                    str(row["code_sha256"]),
                    str(row["config_sha256"]),
                    _optional_text(row["manifest_sha256"]),
                    _optional_text(row["algorithm_version"]),
                    _optional_text(row["contract_version"]),
                    _optional_text(row["runtime_profile"]),
                    _optional_text(
                        row["environment_fingerprint"]
                    ),
                    _optional_text(row["data_snapshot_id"]),
                    str(row["status"]),
                )
                for row in snapshot.version_rows
                if str(row["scheme_id"]) in expected_scheme_ids
            )
        )
    except (KeyError, TypeError, ValueError):
        raise DailyRealReplayPreflightError(
            "PRODUCTION_VERSION_DRIFT"
        ) from None
    if actual_versions != expected_versions:
        raise DailyRealReplayPreflightError(
            "PRODUCTION_VERSION_DRIFT"
        )
    return _canonical_sha256(
        {
            "registry": actual_registry,
            "versions": actual_versions,
        }
    )


def validate_replay_quiescence(
    snapshot: ReplayQuiescenceSnapshot,
) -> None:
    """真实算法联跑只能在 BFL 控制面和算法进程完全静默时开始。"""
    if (
        set(snapshot.counters) != _QUIESCENCE_FIELDS
        or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for value in snapshot.counters.values()
        )
    ):
        raise DailyRealReplayPreflightError(
            "QUIESCENCE_EVIDENCE_INVALID"
        )
    if snapshot.loaded_launchagent_labels or any(
        snapshot.counters.values()
    ):
        raise DailyRealReplayPreflightError(
            "PLATFORM_NOT_QUIESCENT"
        )


def validate_source_database_preflight(
    config: object,
    *,
    preflight: object,
) -> None:
    """二次闭合 source 配置身份及九张只读源表，拒绝同数量替换。"""
    try:
        valid = (
            str(getattr(config, "database"))
            == str(getattr(preflight, "database"))
            and str(getattr(config, "user"))
            == str(getattr(preflight, "authenticated_user"))
            and tuple(getattr(preflight, "tables"))
            == _SOURCE_TABLES
        )
    except Exception:
        valid = False
    if not valid:
        raise DailyRealReplayPreflightError(
            "SOURCE_DATABASE_IDENTITY_DRIFT"
        )


def _read_control_plane_boundary(
    service_uid: int,
) -> ReplayControlPlaneSnapshot:
    """只读验证已安装 plist、仓库 rollout 与 admission 的固定边界。"""
    try:
        installed_modes = read_installed_launchagent_modes(
            service_uid
        )
        rollout = json.loads(
            (
                PROJECT_ROOT
                / "deploy"
                / "daily_coordinator_rollout_v1.json"
            ).read_text(encoding="utf-8")
        )
        admission = json.loads(
            (
                PROJECT_ROOT
                / "deploy"
                / "daily_capacity_admission_v2.json"
            ).read_text(encoding="utf-8")
        )
    except Exception:
        raise DailyRealReplayPreflightError(
            "CONTROL_PLANE_BOUNDARY_UNAVAILABLE"
        ) from None
    if (
        set(installed_modes) != set(LAUNCHAGENT_LABELS)
        or set(installed_modes.values()) != {"legacy"}
        or not isinstance(rollout, dict)
        or rollout
        != {
            "schema_version": "daily-coordinator-rollout-v1",
            "mode": "legacy",
        }
        or not isinstance(admission, dict)
        or admission.get("schema_version")
        != "daily-capacity-admission-v2"
        or admission.get("status") != "BLOCKED"
    ):
        raise DailyRealReplayPreflightError(
            "CONTROL_PLANE_BOUNDARY_DRIFT"
        )
    payload = {
        "installed_modes": dict(sorted(installed_modes.items())),
        "rollout_mode": "legacy",
        "admission_status": "BLOCKED",
    }
    return ReplayControlPlaneSnapshot(
        installed_modes=MappingProxyType(
            dict(sorted(installed_modes.items()))
        ),
        rollout_mode="legacy",
        admission_status="BLOCKED",
        digest=_canonical_sha256(payload),
    )


@contextmanager
def _preflight_session() -> Iterator[_ReplayOperatorSession]:
    """check-only 与未来 execute 共用的机器级非阻塞 operator fence。"""
    operator_lock = None
    runtime_lock = None
    try:
        lock_root = _real_replay_lock_root()
        operator_lock = OccurrenceFileLock(
            lock_root / "real-replay-operator.lock"
        )
        operator_lock.acquire()
        runtime_lock = OccurrenceFileLock(
            lock_root / "real-replay-runtime.lock"
        )
        runtime_lock.acquire()
        session = _ReplayOperatorSession(
            owner_pid=os.getpid(),
            operator_lock=operator_lock,
            runtime_lock=runtime_lock,
            operator_file_identity=_lock_file_identity(
                operator_lock.path
            ),
            runtime_file_identity=_lock_file_identity(
                runtime_lock.path
            ),
        )
    except OccurrenceLockUnavailable:
        if runtime_lock is not None:
            runtime_lock.release()
        if operator_lock is not None:
            operator_lock.release()
        raise DailyRealReplayPreflightError(
            "PREFLIGHT_SESSION_ALREADY_HELD"
        ) from None
    except DailyRealReplayPreflightError:
        if runtime_lock is not None:
            runtime_lock.release()
        if operator_lock is not None:
            operator_lock.release()
        raise
    except Exception:
        if runtime_lock is not None:
            runtime_lock.release()
        if operator_lock is not None:
            operator_lock.release()
        raise DailyRealReplayPreflightError(
            "PREFLIGHT_SESSION_UNAVAILABLE"
        ) from None
    try:
        yield session
    finally:
        if runtime_lock is not None:
            runtime_lock.release()
        if operator_lock is not None:
            operator_lock.release()


def _lock_file_identity(path: Path) -> tuple[int, int]:
    """返回 owner-only 普通 fence 文件的稳定设备/inode 身份。"""
    details = path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise DailyRealReplayPreflightError(
            "PREFLIGHT_SESSION_NOT_HELD"
        )
    return details.st_dev, details.st_ino


def _stable_candidate_identity() -> ReplayCandidateIdentity:
    try:
        return _freeze_candidate_identity()
    except DailyRealReplayPreflightError:
        raise
    except Exception:
        raise DailyRealReplayPreflightError(
            "CANDIDATE_IDENTITY_UNAVAILABLE"
        ) from None


def _stable_generation_inputs(
    *,
    native_manifest: str | Path,
    databridge_manifest: str | Path,
) -> DailyRealReplayInputs:
    try:
        return open_real_replay_generations(
            native_manifest=native_manifest,
            databridge_manifest=databridge_manifest,
        )
    except DailyRealReplayPreflightError:
        raise
    except Exception:
        raise DailyRealReplayPreflightError(
            "GENERATION_CONTEXT_INVALID"
        ) from None


def _stable_definition_snapshot(
    inputs: DailyRealReplayInputs,
) -> ReplayDefinitionSnapshot:
    try:
        return _load_definition_snapshot(inputs)
    except DailyRealReplayPreflightError:
        raise
    except Exception:
        raise DailyRealReplayPreflightError(
            "DEPLOYED_DEFINITION_INVALID"
        ) from None


def _read_source_database_preflight(
    inputs: DailyRealReplayInputs,
) -> tuple[object, object, ReplaySourceInputEvidence]:
    try:
        config = load_source_runtime_database_config()
        preflight = preflight_source_runtime_database_access(config)
        validate_source_database_preflight(
            config,
            preflight=preflight,
        )
        evidence = _read_source_input_evidence(
            config,
            inputs=inputs,
        )
        return config, preflight, evidence
    except DailyRealReplayPreflightError:
        raise
    except Exception:
        raise DailyRealReplayPreflightError(
            "SOURCE_DATABASE_PREFLIGHT_FAILED"
        ) from None


def _read_source_input_evidence(
    config: object,
    *,
    inputs: DailyRealReplayInputs,
) -> ReplaySourceInputEvidence:
    """验证 live source 的 T-1 就绪与冻结日历闭合，并采集水位。"""
    engine = None
    try:
        engine = create_input_engine(database_config=config)
        calendar = get_calendar(engine)
        if (
            not calendar.is_trading_day(inputs.business_date)
            or calendar.previous_trading_day(inputs.business_date)
            != inputs.feature_date
        ):
            raise DailyRealReplayPreflightError(
                "SOURCE_FROZEN_CALENDAR_DRIFT"
            )
        readiness = inspect_native_input_readiness(
            engine,
            feature_date=inputs.feature_date,
        )
        if not readiness.ready:
            raise DailyRealReplayPreflightError(
                "SOURCE_INPUT_NOT_READY"
            )
        evidence = capture_source_commit_evidence(
            engine,
            feature_date=inputs.feature_date,
        )
        if (
            evidence.feature_date != inputs.feature_date
            or not _is_hex_digest(
                evidence.source_commit_token,
                length=64,
            )
        ):
            raise DailyRealReplayPreflightError(
                "SOURCE_WATERMARK_INVALID"
            )
        return ReplaySourceInputEvidence(
            feature_date=evidence.feature_date,
            source_commit_token=evidence.source_commit_token,
        )
    except DailyRealReplayPreflightError:
        raise
    except Exception:
        raise DailyRealReplayPreflightError(
            "SOURCE_INPUT_EVIDENCE_FAILED"
        ) from None
    finally:
        if engine is not None:
            engine.dispose()


def _freeze_candidate_identity() -> ReplayCandidateIdentity:
    """拒绝脏工作区和被忽略的运行代码，再按实际 bytes 计算闭包摘要。"""
    root = _git_output(("rev-parse", "--show-toplevel")).decode().strip()
    if Path(root).resolve(strict=True) != PROJECT_ROOT:
        raise DailyRealReplayPreflightError(
            "CANDIDATE_PROJECT_ROOT_DRIFT"
        )
    branch = _git_output(
        ("branch", "--show-current")
    ).decode().strip()
    if branch != EXPECTED_BRANCH:
        raise DailyRealReplayPreflightError(
            "CANDIDATE_BRANCH_DRIFT"
        )
    head = _git_output(("rev-parse", "HEAD")).decode().strip()
    if not _is_hex_digest(head, length=40):
        raise DailyRealReplayPreflightError(
            "CANDIDATE_HEAD_INVALID"
        )
    dirty = _git_output(
        (
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
    )
    if dirty:
        raise DailyRealReplayPreflightError(
            "CANDIDATE_WORKTREE_DIRTY"
        )
    ignored = _git_output(
        (
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            "--",
            *_RUNTIME_CODE_ROOTS,
        )
    )
    ignored_paths = (
        value
        for value in ignored.split(b"\0")
        if value
    )
    if any(
        Path(
            value.decode("utf-8", errors="surrogateescape")
        ).suffix.casefold()
        in _IGNORED_EXECUTABLE_SUFFIXES
        for value in ignored_paths
    ):
        raise DailyRealReplayPreflightError(
            "CANDIDATE_IGNORED_RUNTIME_CODE"
        )

    tracked = tuple(
        value
        for value in _git_output(("ls-files", "-z")).split(b"\0")
        if value
    )
    digest = hashlib.sha256()
    for raw_relative in tracked:
        relative = raw_relative.decode(
            "utf-8",
            errors="surrogateescape",
        )
        path = PROJECT_ROOT / relative
        try:
            details = path.lstat()
            data = path.read_bytes()
        except OSError:
            raise DailyRealReplayPreflightError(
                "CANDIDATE_TRACKED_FILE_UNREADABLE"
            ) from None
        if not stat.S_ISREG(details.st_mode):
            raise DailyRealReplayPreflightError(
                "CANDIDATE_TRACKED_FILE_UNSAFE"
            )
        digest.update(raw_relative)
        digest.update(b"\0")
        digest.update(
            f"{stat.S_IMODE(details.st_mode):04o}".encode("ascii")
        )
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    try:
        policy_sha256 = hashlib.sha256(
            DEFAULT_POLICY_PATH.read_bytes()
        ).hexdigest()
    except OSError:
        raise DailyRealReplayPreflightError(
            "CANDIDATE_POLICY_UNREADABLE"
        ) from None
    return ReplayCandidateIdentity(
        branch=branch,
        git_head=head,
        tree_sha256=digest.hexdigest(),
        policy_sha256=policy_sha256,
    )


def _load_definition_snapshot(
    inputs: DailyRealReplayInputs,
) -> ReplayDefinitionSnapshot:
    discovered = tuple(discover_schemes(strict=True))
    policy = load_daily_policy(discovered=discovered)
    configs = {
        config.scheme_id: config
        for config in discovered
        if config.status == "active"
        and config.frequency == "daily"
    }
    if (
        set(configs) != set(policy.schemes)
        or len(configs) != 21
        or policy.expected_item_count != 21
        or policy.expected_target_count != 25
    ):
        raise DailyRealReplayPreflightError(
            "DEPLOYED_CARDINALITY_DRIFT"
        )
    calendar = FrozenCalendarService(inputs.native_generation)
    if (
        not calendar.is_trading_day(inputs.business_date)
        or calendar.previous_trading_day(inputs.business_date)
        != inputs.feature_date
    ):
        raise DailyRealReplayPreflightError(
            "GENERATION_TRADING_DATE_INVALID"
        )

    input_mode_counts: dict[str, int] = {}
    registry_rows: list[ReplayExpectedRegistryRow] = []
    version_rows: list[ReplayExpectedVersionRow] = []
    native_item_count = 0
    v2_item_count = 0
    for scheme_id in sorted(policy.schemes):
        item = policy.schemes[scheme_id]
        config = configs[scheme_id]
        input_mode_counts[item.input_compatibility] = (
            input_mode_counts.get(item.input_compatibility, 0) + 1
        )
        if item.runtime_type == "native_adapter":
            native_item_count += 1
        elif item.runtime_type == "blackbox_v2":
            v2_item_count += 1
        else:
            raise DailyRealReplayPreflightError(
                "DEPLOYED_RUNTIME_TYPE_DRIFT"
            )
        if (
            config.runtime_type != item.runtime_type
            or config.task_type != item.task_type
            or config.horizon != item.horizon
            or tuple(config.tenors) != item.target_tenors
        ):
            raise DailyRealReplayPreflightError(
                "DEPLOYED_SCHEME_POLICY_DRIFT"
            )
        for target_tenor in item.target_tenors:
            registry_rows.append(
                ReplayExpectedRegistryRow(
                    registry_scheme_id=(
                        f"{scheme_id}__h{item.horizon}__{target_tenor}"
                    ),
                    base_scheme_id=scheme_id,
                    runtime_type=item.runtime_type,
                    task_type=item.task_type,
                    target_tenor=target_tenor,
                    horizon=item.horizon,
                )
            )
        version_rows.append(
            ReplayExpectedVersionRow(
                scheme_id=scheme_id,
                scheme_version=config.scheme_version,
                runtime_type=config.runtime_type,
                code_sha256=config.code_hash,
                config_sha256=config.config_hash,
                manifest_sha256=config.manifest_hash,
                algorithm_version=config.algorithm_version,
                contract_version=config.contract_version,
                runtime_profile=config.runtime_profile,
                environment_fingerprint=(
                    config.environment_fingerprint
                ),
                data_snapshot_id=config.data_snapshot_id,
            )
        )
    if (
        native_item_count != 17
        or v2_item_count != 4
        or len(registry_rows) != 25
        or input_mode_counts
        != {
            "generation_v1": 14,
            "live_source_0629": 3,
            "databridge_v1": 4,
        }
        or {
            scheme_id
            for scheme_id, item in policy.schemes.items()
            if item.input_compatibility == "live_source_0629"
        }
        != set(APPROVED_0629_LIVE_SOURCE_SCHEMES)
    ):
        raise DailyRealReplayPreflightError(
            "DEPLOYED_INPUT_MATRIX_DRIFT"
        )
    policy_sha256 = hashlib.sha256(
        DEFAULT_POLICY_PATH.read_bytes()
    ).hexdigest()
    return ReplayDefinitionSnapshot(
        policy_version=policy.version,
        policy_sha256=policy_sha256,
        expected_item_count=policy.expected_item_count,
        expected_target_count=policy.expected_target_count,
        native_item_count=native_item_count,
        v2_item_count=v2_item_count,
        input_mode_counts=MappingProxyType(
            dict(sorted(input_mode_counts.items()))
        ),
        registry_rows=tuple(
            sorted(
                registry_rows,
                key=lambda row: row.registry_scheme_id,
            )
        ),
        version_rows=tuple(
            sorted(version_rows, key=lambda row: row.scheme_id)
        ),
    )


def _read_production_daily_snapshot() -> ProductionDailySnapshot:
    """只在单个 RR consistent snapshot/read-only 事务读取生产控制面。"""
    engine = create_engine_from_env()
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
            )
            connection.exec_driver_sql(
                "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
            )
            try:
                server_row = connection.execute(
                    text(
                        """
                        SELECT DATABASE() AS database_name,
                               @@server_uuid AS server_uuid,
                               @@port AS server_port
                        """
                    )
                ).mappings().one()
                database_name = str(
                    server_row["database_name"] or ""
                )
                server_identity_sha256 = _canonical_sha256(
                    {
                        "database_name": database_name,
                        "server_uuid": str(
                            server_row["server_uuid"] or ""
                        ),
                        "server_port": int(
                            server_row["server_port"]
                        ),
                    }
                )
                migration_rows = tuple(
                    connection.execute(
                        text(
                            """
                            SELECT version, filename,
                                   sha256 AS checksum_sha256,
                                   state
                            FROM t_schema_migrations
                            ORDER BY version
                            """
                        )
                    ).mappings()
                )
                registry_rows = tuple(
                    dict(row)
                    for row in connection.execute(
                        text(
                            """
                            SELECT scheme_id, base_scheme_id,
                                   runtime_type, status, frequency,
                                   task_type, target_tenor, horizon
                            FROM t_scheme_registry
                            WHERE status = 'active'
                              AND frequency = 'daily'
                            ORDER BY base_scheme_id, scheme_id
                            """
                        )
                    ).mappings()
                )
                version_rows = tuple(
                    dict(row)
                    for row in connection.execute(
                        text(
                            """
                            SELECT scheme_id, scheme_version,
                                   runtime_type,
                                   code_hash AS code_sha256,
                                   config_hash AS config_sha256,
                                   manifest_hash AS manifest_sha256,
                                   algorithm_version,
                                   contract_version,
                                   runtime_profile,
                                   environment_fingerprint,
                                   data_snapshot_id,
                                   status
                            FROM t_scheme_versions
                            WHERE status = 'active'
                            ORDER BY scheme_id, scheme_version
                            """
                        )
                    ).mappings()
                )
            finally:
                connection.rollback()
    finally:
        engine.dispose()
    return ProductionDailySnapshot(
        database_name=database_name,
        server_identity_sha256=server_identity_sha256,
        migration_rows=tuple(dict(row) for row in migration_rows),
        registry_rows=registry_rows,
        version_rows=version_rows,
    )


def _probe_replay_quiescence(
    business_date: str,
    *,
    service_uid: int,
) -> ReplayQuiescenceSnapshot:
    try:
        normalized_date = date.fromisoformat(business_date)
        service_states = probe_launchagent_service_states(
            service_uid
        )
        if set(service_states) != set(LAUNCHAGENT_LABELS):
            raise ValueError("launchagent labels drifted")
        counters = probe_daily_transition_quiescence(
            service_uid,
            normalized_date,
        )
    except DailyRealReplayPreflightError:
        raise
    except Exception:
        raise DailyRealReplayPreflightError(
            "QUIESCENCE_PROBE_FAILED"
        ) from None
    loaded = tuple(
        sorted(
            label
            for label, is_loaded in service_states.items()
            if is_loaded
        )
    )
    payload = {
        "loaded_launchagent_labels": loaded,
        "counters": dict(sorted(counters.items())),
    }
    snapshot = ReplayQuiescenceSnapshot(
        loaded_launchagent_labels=loaded,
        counters=MappingProxyType(dict(sorted(counters.items()))),
        digest=_canonical_sha256(payload),
    )
    validate_replay_quiescence(snapshot)
    return snapshot


def _require_operator_service_uid() -> int:
    """拒绝 sudo/root 或非项目服务账号，避免对错误 gui domain 做静默检查。"""
    real_uid = os.getuid()
    effective_uid = os.geteuid()
    try:
        project_uid = PROJECT_ROOT.stat().st_uid
    except OSError:
        raise DailyRealReplayPreflightError(
            "OPERATOR_SERVICE_UID_UNAVAILABLE"
        ) from None
    if (
        real_uid != effective_uid
        or effective_uid != project_uid
    ):
        raise DailyRealReplayPreflightError(
            "OPERATOR_SERVICE_UID_MISMATCH"
        )
    return effective_uid


def _generation_identity(
    inputs: DailyRealReplayInputs,
) -> tuple[str, ...]:
    return (
        str(inputs.business_date),
        str(inputs.feature_date),
        str(inputs.native_generation.generation_id),
        str(inputs.native_generation.manifest_sha256),
        str(inputs.databridge_generation.generation_id),
        str(inputs.databridge_generation.manifest_sha256),
    )


def _build_replay_dispatch_identity(
    *,
    service_uid: int,
    native_manifest: str | Path,
    databridge_manifest: str | Path,
    candidate: ReplayCandidateIdentity,
    inputs: DailyRealReplayInputs,
    definitions: ReplayDefinitionSnapshot,
    control_plane: ReplayControlPlaneSnapshot,
    production: ProductionDailySnapshot,
    production_registry_digest: str,
    source_config: object,
    source_preflight: object,
    source_start: ReplaySourceInputEvidence,
    source_end: ReplaySourceInputEvidence,
) -> _ReplayDispatchIdentity:
    """把成功二次预检的稳定输入压缩为 write-once capability。"""
    definition_payload = {
        "policy_version": definitions.policy_version,
        "policy_sha256": definitions.policy_sha256,
        "expected_item_count": definitions.expected_item_count,
        "expected_target_count": definitions.expected_target_count,
        "native_item_count": definitions.native_item_count,
        "v2_item_count": definitions.v2_item_count,
        "input_mode_counts": dict(definitions.input_mode_counts),
        "registry_rows": [
            vars(row) for row in definitions.registry_rows
        ],
        "version_rows": [
            vars(row) for row in definitions.version_rows
        ],
    }
    production_payload = {
        "database_name": production.database_name,
        "server_identity_sha256":
            production.server_identity_sha256,
        "migration_rows": [
            dict(row) for row in production.migration_rows
        ],
        "registry_digest": production_registry_digest,
    }
    return _ReplayDispatchIdentity(
        service_uid=service_uid,
        business_date=inputs.business_date,
        native_manifest_path=str(
            Path(native_manifest).resolve(strict=False)
        ),
        databridge_manifest_path=str(
            Path(databridge_manifest).resolve(strict=False)
        ),
        candidate_digest=_canonical_sha256(
            {
                "branch": candidate.branch,
                "git_head": candidate.git_head,
                "tree_sha256": candidate.tree_sha256,
                "policy_sha256": candidate.policy_sha256,
            }
        ),
        generation_digest=_canonical_sha256(
            {"identity": list(_generation_identity(inputs))}
        ),
        definition_digest=_canonical_sha256(
            definition_payload
        ),
        control_plane_digest=control_plane.digest,
        production_digest=_canonical_sha256(
            production_payload
        ),
        source_database_digest=_canonical_sha256(
            {
                "identity": list(
                    _source_database_identity(
                        source_config,
                        source_preflight,
                    )
                )
            }
        ),
        source_watermark_digest=_canonical_sha256(
            {
                "start": {
                    "feature_date": source_start.feature_date,
                    "source_commit_token":
                        source_start.source_commit_token,
                },
                "end": {
                    "feature_date": source_end.feature_date,
                    "source_commit_token":
                        source_end.source_commit_token,
                },
            }
        ),
    )


def _source_database_identity(
    config: object,
    preflight: object,
) -> tuple[object, ...]:
    return (
        str(getattr(config, "cache_identity")),
        str(getattr(preflight, "database")),
        str(getattr(preflight, "authenticated_user")),
        tuple(getattr(preflight, "tables")),
    )


def _expected_production_migrations(
) -> tuple[tuple[int, str, str, str], ...]:
    rows: list[tuple[int, str, str, str]] = []
    for version in EXPECTED_PRODUCTION_MIGRATIONS:
        matches = tuple(
            sorted(
                (PROJECT_ROOT / "migrations").glob(
                    f"{version:03d}_*.sql"
                )
            )
        )
        if len(matches) != 1:
            raise DailyRealReplayPreflightError(
                "CANDIDATE_MIGRATION_MANIFEST_INVALID"
            )
        path = matches[0]
        rows.append(
            (
                version,
                path.name,
                hashlib.sha256(path.read_bytes()).hexdigest(),
                "APPLIED",
            )
        )
    return tuple(rows)


def _git_output(arguments: Sequence[str]) -> bytes:
    try:
        completed = subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                "core.excludesFile=/dev/null",
                "-C",
                str(PROJECT_ROOT),
                *arguments,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        raise DailyRealReplayPreflightError(
            "CANDIDATE_GIT_UNAVAILABLE"
        ) from None
    return completed.stdout


def _is_hex_digest(value: str, *, length: int) -> bool:
    return (
        len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
