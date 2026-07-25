"""日频协调器 rollout 与 machine-global epoch chain 契约。"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import event


DAILY_COORDINATOR_MODE_ENV = "BOND_DAILY_COORDINATOR_MODE"
VALID_DAILY_COORDINATOR_MODES = frozenset({"legacy", "ledger"})
ROLLOUT_SCHEMA_VERSION = "daily-coordinator-rollout-v1"
EPOCH_SCHEMA_VERSION = "daily-coordinator-epoch-v1"
EPOCH_CONTRACT_SCHEMA_VERSION = "daily-coordinator-epoch-contract-v1"
EPOCH_FILENAME_FORMAT = "epoch-%020d.json"
ZERO_RECORD_SHA256 = "0" * 64
DEFAULT_ROLLOUT_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "daily_coordinator_rollout_v1.json"
)
DEFAULT_EPOCH_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "daily_coordinator_epoch_contract_v1.json"
)
DEFAULT_EPOCH_GENESIS_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "daily_coordinator_epoch_genesis_v1.json"
)
DEFAULT_EPOCH_DIRECTORY = Path(
    "/Library/Application Support/BondFactorLab/"
    "daily-coordinator-epochs-v1"
)
DEFAULT_EPOCH_OWNER_UID = 0
DEFAULT_EPOCH_SERVICE_UID = os.getuid()
DEFAULT_EPOCH_PARENT_ANCHOR = Path("/")
EPOCH_RECORDS_DIRECTORY_NAME = "records"
DAILY_RUNTIME_RELATIVE_PATH = (
    "Library",
    "Application Support",
    "BondFactorLab",
    "daily-runtime-v1",
)

_EPOCH_FIELDS = frozenset(
    {
        "epoch",
        "mode",
        "previous_record_sha256",
        "schema_version",
        "transition_id",
    }
)
_CONTRACT_FIELDS = frozenset(
    {
        "epoch_filename",
        "first_epoch",
        "genesis_sha256",
        "modes",
        "record_schema_version",
        "schema_version",
        "zero_previous_record_sha256",
    }
)
_POLICY_EPOCH_FIELDS = frozenset(
    {
        "epoch",
        "mode",
        "record_sha256",
    }
)
_ISOLATED_REPLAY_ENGINE_ATTRIBUTE = (
    "_bfl_isolated_daily_coordinator_epoch"
)
_ISOLATED_REPLAY_DATABASE_RE = re.compile(
    r"^bfl_real_replay_[a-z0-9_]{1,45}$"
)
_ISOLATED_REPLAY_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_ISOLATED_REPLAY_IDENTITY_FIELDS = (
    "version",
    "database_name",
    "server_uuid",
    "session_time_zone",
    "storage_engine",
    "sql_mode",
    "isolation_level",
    "bind_address",
    "port",
    "socket_path",
    "datadir",
    "secure_file_priv",
    "foreign_key_checks",
    "log_bin",
    "local_infile",
)
_EPOCH_FILENAME_RE = re.compile(r"^epoch-([0-9]{20})\.json$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRANSITION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_EPOCH_RECORD_BYTES = 4096
_MAX_CONTRACT_BYTES = 8192
_process_identity_lock = threading.Lock()
_process_bound_identity: DailyCoordinatorEpochIdentity | None = None
_verified_isolation_lock = threading.Lock()
_verified_isolations: weakref.WeakKeyDictionary[
    object,
    object,
] = weakref.WeakKeyDictionary()
_pending_isolations: weakref.WeakKeyDictionary[
    object,
    object,
] = weakref.WeakKeyDictionary()


class DailyCoordinatorModeMissingError(ValueError):
    """调用者没有提供显式 rollout 模式。"""


@dataclass(frozen=True)
class DailyCoordinatorEpochIdentity:
    """当前 process 所绑定的日频 control-plane epoch 身份。"""

    epoch: int
    mode: str
    previous_record_sha256: str
    record_sha256: str
    source: str
    transition_id: str

    def policy_payload(self) -> dict[str, object]:
        """返回 occurrence/heartbeat 使用的最小不可变 fence。"""
        return {
            "epoch": self.epoch,
            "mode": self.mode,
            "record_sha256": self.record_sha256,
        }


@dataclass(frozen=True)
class IsolatedDailyCoordinatorEpochBinding:
    """只绑定到受保护临时 MySQL Engine 的 replay epoch。"""

    identity: DailyCoordinatorEpochIdentity
    engine_identity: int
    database_name: str
    server_uuid: str
    isolation_identity: int
    connection_marker_key: str
    connection_guard_identity: int


@dataclass(frozen=True)
class VerifiedIsolatedDailyDatabase:
    """由 shared 原子 verifier 创建并登记的临时 MySQL 能力。"""

    version: str
    database_name: str
    server_uuid: str
    session_time_zone: str
    storage_engine: str
    sql_mode: str
    isolation_level: str
    bind_address: str
    port: int
    socket_path: str
    datadir: str
    secure_file_priv: str
    foreign_key_checks: int
    log_bin: int
    local_infile: int
    engine_identity: int
    connection_guard: Any


def verify_and_register_isolated_daily_database(
    engine: Any,
    *,
    expected_database_name: str,
    expected_server_uuid: str,
    expected_port: int,
    expected_private_root: str | Path,
    connection_marker_key: str,
) -> VerifiedIsolatedDailyDatabase:
    """原子验证、创建 guard 并登记隔离 replay 数据库能力。"""
    expected_database = str(expected_database_name).strip()
    expected_uuid = str(expected_server_uuid).strip().lower()
    expected_root = Path(expected_private_root).resolve()
    marker_key = str(connection_marker_key).strip()
    if (
        _ISOLATED_REPLAY_DATABASE_RE.fullmatch(expected_database) is None
        or _ISOLATED_REPLAY_UUID_RE.fullmatch(expected_uuid) is None
        or getattr(getattr(engine, "dialect", None), "name", None)
        != "mysql"
        or str(
            getattr(getattr(engine, "url", None), "database", "")
        ).strip()
        != expected_database
        or not marker_key
    ):
        raise RuntimeError(
            "isolated replay database expected identity is invalid"
        )
    reservation = _reserve_isolated_database_verification(engine)
    protected: VerifiedIsolatedDailyDatabase | None = None
    listener_installed = False
    try:
        try:
            with engine.connect() as connection:
                actual = _read_isolated_replay_connection_identity(
                    connection
                )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "isolated replay database identity could not be inspected"
            ) from exc
        _validate_isolated_replay_identity(actual)
        expected_fields = {
            "database_name": expected_database,
            "server_uuid": expected_uuid,
            "port": int(expected_port),
            "socket_path": str(expected_root / "mysql.sock"),
            "datadir": str(expected_root / "data"),
            "secure_file_priv": str(expected_root / "secure"),
        }
        drift = sorted(
            field
            for field, expected in expected_fields.items()
            if actual[field] != expected
        )
        if drift:
            raise RuntimeError(
                "isolated replay database differs from expected private "
                "MySQL: " + ",".join(drift)
            )

        def connection_guard(connection: Any) -> None:
            if protected is None:
                raise RuntimeError(
                    "isolated replay database capability is unavailable"
                )
            _assert_isolated_replay_connection_identity(
                connection,
                database_name=protected.database_name,
                server_uuid=protected.server_uuid,
                isolation=protected,
            )
            connection.info[marker_key] = protected.server_uuid

        protected = VerifiedIsolatedDailyDatabase(
            **actual,
            engine_identity=id(engine),
            connection_guard=connection_guard,
        )
        event.listen(engine, "engine_connect", connection_guard)
        listener_installed = True
        if getattr(engine, "_bfl_real_replay_isolation", None) is not None:
            raise RuntimeError(
                "isolated replay database Engine metadata changed"
            )
        setattr(engine, "_bfl_real_replay_isolation", protected)
        _promote_isolated_database_verification(
            engine,
            reservation=reservation,
            protected=protected,
        )
        return protected
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            "isolated replay database capability registration failed"
        ) from exc
    finally:
        if not _is_real_replay_isolation_capability(
            engine,
            protected,
        ):
            if (
                protected is not None
                and getattr(
                    engine,
                    "_bfl_real_replay_isolation",
                    None,
                )
                is protected
            ):
                delattr(engine, "_bfl_real_replay_isolation")
            if listener_installed and protected is not None:
                try:
                    event.remove(
                        engine,
                        "engine_connect",
                        protected.connection_guard,
                    )
                except Exception:
                    pass
            _release_isolated_database_verification(
                engine,
                reservation=reservation,
            )


def recheck_verified_isolated_daily_database(
    engine: Any,
    isolation: VerifiedIsolatedDailyDatabase,
) -> None:
    """重新核验 verifier 登记、listener 和当前完整数据库身份。"""
    if not isinstance(isolation, VerifiedIsolatedDailyDatabase):
        raise RuntimeError(
            "isolated replay database verified capability is required"
        )
    guard = isolation.connection_guard
    if (
        getattr(engine, "_bfl_real_replay_isolation", None) is not isolation
        or isolation.engine_identity != id(engine)
        or not _is_real_replay_isolation_capability(engine, isolation)
        or not event.contains(engine, "engine_connect", guard)
    ):
        raise RuntimeError(
            "isolated replay database registered capability drift"
        )
    with engine.connect() as connection:
        guard(connection)


def bind_isolated_daily_coordinator_epoch(
    engine: Any,
    *,
    frozen: Mapping[str, object],
    database_name: str,
    server_uuid: str,
    isolation: object,
    connection_marker_key: str,
) -> DailyCoordinatorEpochIdentity:
    """为已验证的 ``bfl_real_replay_*`` Engine 绑定非生产 epoch。"""
    normalized_database = str(database_name).strip()
    normalized_uuid = str(server_uuid).strip().lower()
    normalized_marker = str(connection_marker_key).strip()
    guard = getattr(isolation, "connection_guard", None)
    if (
        _ISOLATED_REPLAY_DATABASE_RE.fullmatch(normalized_database)
        is None
    ):
        raise RuntimeError(
            "isolated replay epoch requires bfl_real_replay_* database"
        )
    if getattr(getattr(engine, "dialect", None), "name", None) != "mysql":
        raise RuntimeError("isolated replay epoch requires MySQL Engine")
    if not normalized_marker:
        raise RuntimeError(
            "isolated replay epoch connection marker is missing"
        )
    if (
        str(getattr(getattr(engine, "url", None), "database", "")).strip()
        != normalized_database
    ):
        raise RuntimeError(
            "isolated replay epoch Engine database identity drift"
        )
    if getattr(engine, "_bfl_real_replay_isolation", None) is not isolation:
        raise RuntimeError(
            "isolated replay epoch requires verified database isolation"
        )
    if not _is_real_replay_isolation_capability(engine, isolation):
        raise RuntimeError(
            "isolated replay epoch requires registered database isolation"
        )
    if (
        getattr(isolation, "engine_identity", None) != id(engine)
        or getattr(isolation, "database_name", None) != normalized_database
        or getattr(isolation, "server_uuid", None) != normalized_uuid
        or guard is None
    ):
        raise RuntimeError(
            "isolated replay epoch database isolation identity drift"
        )
    if not event.contains(engine, "engine_connect", guard):
        raise RuntimeError(
            "isolated replay epoch database guard listener is unavailable"
        )
    try:
        with engine.connect() as connection:
            _assert_isolated_replay_connection_identity(
                connection,
                database_name=normalized_database,
                server_uuid=normalized_uuid,
                isolation=isolation,
            )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            "isolated replay epoch actual database identity is unavailable"
        ) from exc
    identity = _isolated_epoch_identity(frozen, server_uuid=normalized_uuid)
    binding = IsolatedDailyCoordinatorEpochBinding(
        identity=identity,
        engine_identity=id(engine),
        database_name=normalized_database,
        server_uuid=normalized_uuid,
        isolation_identity=id(isolation),
        connection_marker_key=normalized_marker,
        connection_guard_identity=id(guard),
    )
    existing = getattr(
        engine,
        _ISOLATED_REPLAY_ENGINE_ATTRIBUTE,
        None,
    )
    if existing is not None and existing != binding:
        raise RuntimeError("isolated replay epoch binding changed")
    setattr(engine, _ISOLATED_REPLAY_ENGINE_ATTRIBUTE, binding)
    return identity


def require_daily_coordinator_mode(
    environ: Mapping[str, str] | None = None,
) -> str:
    """只解析显式环境值；生产 control-plane 入口必须改用 bootstrap。"""
    source = os.environ if environ is None else environ
    raw = source.get(DAILY_COORDINATOR_MODE_ENV)
    if raw is None or not raw.strip():
        raise DailyCoordinatorModeMissingError(
            f"{DAILY_COORDINATOR_MODE_ENV} must be explicitly set to "
            "legacy or ledger"
        )
    mode = raw.strip()
    if mode not in VALID_DAILY_COORDINATOR_MODES:
        raise ValueError(
            f"{DAILY_COORDINATOR_MODE_ENV} must be legacy or ledger, "
            f"got {mode!r}"
        )
    return mode


def read_deployment_daily_coordinator_mode(
    rollout_path: Path | None = None,
) -> str:
    """读取只适用于从未创建 epoch 目录的 repository bootstrap。"""
    path = DEFAULT_ROLLOUT_PATH if rollout_path is None else rollout_path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "daily coordinator rollout file is missing or invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "daily coordinator rollout file must contain a JSON object"
        )
    if payload.get("schema_version") != ROLLOUT_SCHEMA_VERSION:
        raise RuntimeError(
            "daily coordinator rollout schema_version is invalid"
        )
    mode = payload.get("mode")
    if mode not in VALID_DAILY_COORDINATOR_MODES:
        raise RuntimeError(
            "daily coordinator rollout mode must be legacy or ledger"
        )
    if set(payload) != {"schema_version", "mode"}:
        raise RuntimeError(
            "daily coordinator rollout file contains unknown fields"
        )
    return str(mode)


def resolve_daily_runtime_root(
    *,
    service_uid: int | None = None,
) -> Path:
    """返回与 checkout 无关、按服务账号唯一的 machine-global runtime 根。"""
    uid = _validated_uid(service_uid, "daily runtime service UID")
    try:
        account = pwd.getpwuid(uid)
    except (KeyError, OSError) as exc:
        raise RuntimeError(
            f"daily runtime service UID has no account: {uid}"
        ) from exc
    home = Path(str(account.pw_dir))
    if not home.is_absolute():
        raise RuntimeError("daily runtime service home must be absolute")
    root = home.joinpath(*DAILY_RUNTIME_RELATIVE_PATH).resolve(
        strict=False
    )
    if not root.is_absolute():
        raise RuntimeError("daily runtime root must be absolute")
    return root


def daily_control_plane_artifacts(
    *,
    service_uid: int | None = None,
    runtime_root: str | Path | None = None,
    epoch_directory: str | Path | None = None,
    epoch_contract_path: str | Path | None = None,
    epoch_genesis_path: str | Path | None = None,
) -> dict[str, str]:
    """构造不随 active epoch 切换而变化的 capacity artifact 集。"""
    uid = _validated_uid(service_uid, "daily runtime service UID")
    root = (
        resolve_daily_runtime_root(service_uid=uid)
        if runtime_root is None
        else _normalized_absolute_path(
            Path(runtime_root),
            "daily runtime root",
        )
    )
    epoch_dir = _normalized_absolute_path(
        (
            DEFAULT_EPOCH_DIRECTORY
            if epoch_directory is None
            else Path(epoch_directory)
        ),
        "daily coordinator epoch directory",
    )
    contract_path = (
        DEFAULT_EPOCH_CONTRACT_PATH
        if epoch_contract_path is None
        else Path(epoch_contract_path)
    )
    genesis_path = (
        DEFAULT_EPOCH_GENESIS_PATH
        if epoch_genesis_path is None
        else Path(epoch_genesis_path)
    )
    contract_raw, genesis_raw, _genesis_payload = (
        _read_epoch_contract_and_genesis(
            contract_path=contract_path,
            genesis_path=genesis_path,
        )
    )
    contents = {
        "cutover/epoch-contract.json": contract_raw,
        "cutover/epoch-directory.txt":
            os.fspath(epoch_dir).encode("utf-8"),
        "cutover/epoch-genesis.json": genesis_raw,
        "runtime/root.txt": os.fspath(root).encode("utf-8"),
        "service/uid.txt": str(uid).encode("ascii"),
    }
    return {
        identity: hashlib.sha256(content).hexdigest()
        for identity, content in contents.items()
    }


def read_daily_coordinator_epoch_chain(
    *,
    epoch_directory: str | Path | None = None,
    epoch_contract_path: str | Path | None = None,
    epoch_genesis_path: str | Path | None = None,
    expected_owner_uid: int | None = None,
    service_uid: int | None = None,
    parent_anchor: str | Path | None = None,
) -> DailyCoordinatorEpochIdentity | None:
    """安全读取完整 append-only chain；目录不存在才表示初始 legacy。"""
    directory = _normalized_absolute_path(
        (
            DEFAULT_EPOCH_DIRECTORY
            if epoch_directory is None
            else Path(epoch_directory)
        ),
        "daily coordinator epoch directory",
    )
    owner_uid = _validated_uid(
        (
            DEFAULT_EPOCH_OWNER_UID
            if expected_owner_uid is None
            else expected_owner_uid
        ),
        "daily coordinator epoch owner UID",
    )
    effective_service_uid = _validated_uid(
        (
            DEFAULT_EPOCH_SERVICE_UID
            if service_uid is None
            else service_uid
        ),
        "daily coordinator service UID",
    )
    anchor = _normalized_absolute_path(
        (
            DEFAULT_EPOCH_PARENT_ANCHOR
            if parent_anchor is None
            else Path(parent_anchor)
        ),
        "daily coordinator epoch parent anchor",
    )
    contract_path = (
        DEFAULT_EPOCH_CONTRACT_PATH
        if epoch_contract_path is None
        else Path(epoch_contract_path)
    )
    genesis_path = (
        DEFAULT_EPOCH_GENESIS_PATH
        if epoch_genesis_path is None
        else Path(epoch_genesis_path)
    )
    _contract_raw, genesis_raw, genesis_payload = (
        _read_epoch_contract_and_genesis(
            contract_path=contract_path,
            genesis_path=genesis_path,
        )
    )

    try:
        os.lstat(directory)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(
            "daily coordinator epoch directory cannot be inspected: "
            f"{directory}"
        ) from exc
    _validate_epoch_parent_chain(
        directory.parent,
        anchor=anchor,
        expected_owner_uid=owner_uid,
        service_uid=effective_service_uid,
    )
    records_directory = directory / EPOCH_RECORDS_DIRECTORY_NAME
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        root_descriptor = os.open(os.fspath(directory), flags)
    except OSError as exc:
        raise RuntimeError(
            "daily coordinator epoch directory is unreadable or unsafe: "
            f"{directory}"
        ) from exc
    try:
        root_details = os.fstat(root_descriptor)
        _validate_epoch_directory_security(
            root_details,
            path=directory,
            expected_owner_uid=owner_uid,
            service_uid=effective_service_uid,
        )
        try:
            descriptor = os.open(
                EPOCH_RECORDS_DIRECTORY_NAME,
                flags,
                dir_fd=root_descriptor,
            )
        except OSError as exc:
            raise RuntimeError(
                "daily coordinator epoch records directory is missing or "
                f"unsafe: {records_directory}"
            ) from exc
        try:
            before = os.fstat(descriptor)
            _validate_epoch_directory_security(
                before,
                path=records_directory,
                expected_owner_uid=owner_uid,
                service_uid=effective_service_uid,
            )
            names = os.listdir(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(root_descriptor)
    try:
        parsed_names: list[tuple[int, str]] = []
        for name in names:
            match = _EPOCH_FILENAME_RE.fullmatch(name)
            if match is None:
                raise RuntimeError(
                    "daily coordinator epoch directory contains an "
                    f"unexpected entry: {name!r}"
                )
            epoch = int(match.group(1))
            if epoch <= 0:
                raise RuntimeError(
                    "daily coordinator epoch filenames must start at 1"
                )
            parsed_names.append((epoch, name))
        if not parsed_names:
            raise RuntimeError(
                "daily coordinator epoch directory is empty; repository "
                "rollout fallback is forbidden once the directory exists"
            )
        parsed_names.sort()
        expected_epochs = list(range(1, parsed_names[-1][0] + 1))
        actual_epochs = [epoch for epoch, _name in parsed_names]
        if actual_epochs != expected_epochs:
            raise RuntimeError(
                "daily coordinator epoch chain must be contiguous from 1"
            )

        previous_raw: bytes | None = None
        previous_sha256 = ZERO_RECORD_SHA256
        transition_ids: set[str] = set()
        current_payload: Mapping[str, object] | None = None
        current_raw = b""
        for filename_epoch, name in parsed_names:
            raw = _read_epoch_file(
                descriptor,
                name=name,
                expected_owner_uid=owner_uid,
                service_uid=effective_service_uid,
            )
            payload = _parse_epoch_record(raw)
            payload_epoch = int(payload["epoch"])
            if payload_epoch != filename_epoch:
                raise RuntimeError(
                    "daily coordinator epoch filename does not match "
                    "payload epoch"
                )
            if filename_epoch == 1:
                if raw != genesis_raw or payload != genesis_payload:
                    raise RuntimeError(
                        "daily coordinator epoch genesis differs from the "
                        "fixed canonical genesis"
                    )
            if payload["previous_record_sha256"] != previous_sha256:
                raise RuntimeError(
                    "daily coordinator epoch previous_record_sha256 "
                    "breaks the chain"
                )
            transition_id = str(payload["transition_id"])
            if transition_id in transition_ids:
                raise RuntimeError(
                    "daily coordinator epoch transition_id was replayed"
                )
            transition_ids.add(transition_id)
            previous_raw = raw
            previous_sha256 = hashlib.sha256(raw).hexdigest()
            current_payload = payload
            current_raw = raw

        after = os.fstat(descriptor)
        if _directory_stat_identity(before) != _directory_stat_identity(after):
            raise RuntimeError(
                "daily coordinator epoch directory changed while being read"
            )
    finally:
        os.close(descriptor)

    assert previous_raw is not None
    assert current_payload is not None
    return DailyCoordinatorEpochIdentity(
        epoch=int(current_payload["epoch"]),
        mode=str(current_payload["mode"]),
        previous_record_sha256=str(
            current_payload["previous_record_sha256"]
        ),
        record_sha256=hashlib.sha256(current_raw).hexdigest(),
        source="epoch_chain",
        transition_id=str(current_payload["transition_id"]),
    )


def require_current_daily_coordinator_identity(
    *,
    bind_process: bool = True,
) -> DailyCoordinatorEpochIdentity:
    """校验 env/current chain，并可把进程永久绑定到首次所见 epoch。"""
    try:
        explicit_mode: str | None = require_daily_coordinator_mode()
    except DailyCoordinatorModeMissingError:
        explicit_mode = None

    identity = read_daily_coordinator_epoch_chain()
    if identity is None:
        rollout_mode = read_deployment_daily_coordinator_mode()
        if rollout_mode != "legacy":
            raise RuntimeError(
                "repository rollout may only bootstrap legacy before the "
                "machine-global epoch directory exists"
            )
        if explicit_mode is None:
            os.environ[DAILY_COORDINATOR_MODE_ENV] = "legacy"
        elif explicit_mode != "legacy":
            raise RuntimeError(
                "ledger process mode requires the machine-global epoch chain"
            )
        rollout_payload = {
            "mode": "legacy",
            "schema_version": ROLLOUT_SCHEMA_VERSION,
        }
        rollout_digest = hashlib.sha256(
            _canonical_json_bytes(rollout_payload)
        ).hexdigest()
        identity = DailyCoordinatorEpochIdentity(
            epoch=0,
            mode="legacy",
            previous_record_sha256=ZERO_RECORD_SHA256,
            record_sha256=rollout_digest,
            source="repository_bootstrap",
            transition_id="repository-bootstrap-legacy",
        )
    else:
        if (
            explicit_mode is not None
            and explicit_mode != identity.mode
        ):
            raise RuntimeError(
                "daily coordinator process mode does not match current "
                f"epoch mode {identity.mode}"
            )

    if bind_process:
        _bind_process_identity(identity)
    return identity


def bootstrap_deployment_daily_coordinator_mode() -> str:
    """返回 process-bound mode；每次调用都重读并校验完整 epoch chain。"""
    return require_current_daily_coordinator_identity().mode


def assert_daily_coordinator_epoch_matches_policy(
    policy_json: Mapping[str, object],
    *,
    engine: Any | None = None,
) -> DailyCoordinatorEpochIdentity:
    """要求 current/process epoch 与 occurrence 冻结 fence 完全一致。"""
    if not isinstance(policy_json, Mapping):
        raise RuntimeError(
            "daily occurrence policy_json is unavailable"
        )
    frozen = policy_json.get("daily_coordinator_epoch")
    return assert_daily_coordinator_epoch_payload_matches_current(
        frozen,
        label="daily occurrence coordinator epoch",
        engine=engine,
    )


def assert_daily_coordinator_epoch_payload_matches_current(
    frozen: object,
    *,
    label: str = "daily coordinator epoch",
    engine: Any | None = None,
) -> DailyCoordinatorEpochIdentity:
    """校验任意 occurrence/heartbeat capability 与当前身份完全相等。"""
    if (
        not isinstance(frozen, Mapping)
        or set(frozen) != _POLICY_EPOCH_FIELDS
    ):
        raise RuntimeError(
            f"{label} is missing or has unexpected fields"
        )
    epoch = frozen.get("epoch")
    mode = frozen.get("mode")
    digest = frozen.get("record_sha256")
    if (
        not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch <= 0
        or mode not in VALID_DAILY_COORDINATOR_MODES
        or not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
    ):
        raise RuntimeError(
            f"{label} is invalid"
        )
    isolated = _isolated_epoch_binding(engine)
    if isolated is not None:
        current = isolated.identity
    else:
        current = require_current_daily_coordinator_identity()
    if current.policy_payload() != dict(frozen):
        raise RuntimeError(
            f"{label} differs from current machine-global epoch"
        )
    return current


def _isolated_epoch_identity(
    frozen: Mapping[str, object],
    *,
    server_uuid: str,
) -> DailyCoordinatorEpochIdentity:
    if set(frozen) != _POLICY_EPOCH_FIELDS:
        raise RuntimeError(
            "isolated replay epoch has unexpected fields"
        )
    epoch = frozen.get("epoch")
    mode = frozen.get("mode")
    digest = frozen.get("record_sha256")
    if (
        not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch <= 0
        or mode != "ledger"
        or not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
    ):
        raise RuntimeError("isolated replay epoch payload is invalid")
    return DailyCoordinatorEpochIdentity(
        epoch=epoch,
        mode=mode,
        previous_record_sha256=ZERO_RECORD_SHA256,
        record_sha256=digest,
        source="isolated_real_replay",
        transition_id=f"isolated-real-replay:{server_uuid}",
    )


def _isolated_epoch_binding(
    bind: Any | None,
) -> IsolatedDailyCoordinatorEpochBinding | None:
    if bind is None:
        return None
    engine = getattr(bind, "engine", bind)
    is_connection = engine is not bind
    binding = getattr(
        engine,
        _ISOLATED_REPLAY_ENGINE_ATTRIBUTE,
        None,
    )
    if binding is None:
        return None
    if not isinstance(binding, IsolatedDailyCoordinatorEpochBinding):
        raise RuntimeError("isolated replay epoch binding type drift")
    isolation = getattr(engine, "_bfl_real_replay_isolation", None)
    guard = getattr(isolation, "connection_guard", None)
    if (
        binding.engine_identity != id(engine)
        or id(isolation) != binding.isolation_identity
        or not _is_real_replay_isolation_capability(engine, isolation)
        or getattr(isolation, "engine_identity", None) != id(engine)
        or getattr(isolation, "database_name", None)
        != binding.database_name
        or getattr(isolation, "server_uuid", None)
        != binding.server_uuid
        or guard is None
        or id(guard) != binding.connection_guard_identity
        or getattr(getattr(engine, "dialect", None), "name", None)
        != "mysql"
        or str(
            getattr(getattr(engine, "url", None), "database", "")
        ).strip()
        != binding.database_name
    ):
        raise RuntimeError("isolated replay epoch binding identity drift")
    if not event.contains(engine, "engine_connect", guard):
        raise RuntimeError(
            "isolated replay epoch database guard listener is unavailable"
        )
    if is_connection:
        try:
            guard(bind)
        except Exception as exc:
            raise RuntimeError(
                "isolated replay epoch connection guard validation failed"
            ) from exc
    if (
        is_connection
        and getattr(bind, "info", {}).get(
            binding.connection_marker_key
        )
        != binding.server_uuid
    ):
        raise RuntimeError(
            "isolated replay epoch connection guard marker is missing"
        )
    if is_connection:
        _assert_isolated_replay_connection_identity(
            bind,
            database_name=binding.database_name,
            server_uuid=binding.server_uuid,
            isolation=isolation,
        )
    return binding


def _is_real_replay_isolation_capability(
    engine: object,
    value: object,
) -> bool:
    with _verified_isolation_lock:
        registered = _verified_isolations.get(engine)
        return registered is not None and registered is value


def _reserve_isolated_database_verification(engine: object) -> object:
    """用短锁预留单个 Engine；锁内不执行 DB 或 event 外部代码。"""
    reservation = object()
    with _verified_isolation_lock:
        try:
            unavailable = (
                engine in _verified_isolations
                or engine in _pending_isolations
            )
        except TypeError as exc:
            raise RuntimeError(
                "isolated replay database Engine cannot hold capability"
            ) from exc
        if (
            unavailable
            or getattr(engine, "_bfl_real_replay_isolation", None)
            is not None
        ):
            raise RuntimeError(
                "isolated replay database Engine is already registered"
            )
        _pending_isolations[engine] = reservation
    return reservation


def _promote_isolated_database_verification(
    engine: object,
    *,
    reservation: object,
    protected: VerifiedIsolatedDailyDatabase,
) -> None:
    """把 exact pending reservation 原子升级为 verified capability。"""
    with _verified_isolation_lock:
        if (
            _pending_isolations.get(engine) is not reservation
            or engine in _verified_isolations
            or getattr(engine, "_bfl_real_replay_isolation", None)
            is not protected
        ):
            raise RuntimeError(
                "isolated replay database verification reservation drift"
            )
        _verified_isolations[engine] = protected
        del _pending_isolations[engine]


def _release_isolated_database_verification(
    engine: object,
    *,
    reservation: object,
) -> None:
    """失败时只释放调用方拥有的 exact pending reservation。"""
    with _verified_isolation_lock:
        try:
            if _pending_isolations.get(engine) is reservation:
                del _pending_isolations[engine]
        except TypeError:
            return


def _assert_isolated_replay_connection_identity(
    connection: Any,
    *,
    database_name: str,
    server_uuid: str,
    isolation: object,
) -> None:
    """从当前 DBAPI 连接读取完整身份，拒绝 URL、guard 或属性伪装。"""
    normalized = _read_isolated_replay_connection_identity(connection)
    _validate_isolated_replay_identity(normalized)
    if (
        normalized["database_name"] != database_name
        or normalized["server_uuid"] != server_uuid
    ):
        raise RuntimeError(
            "isolated replay epoch actual database identity drift"
        )
    drift = [
        field
        for field in _ISOLATED_REPLAY_IDENTITY_FIELDS
        if normalized[field] != getattr(isolation, field, None)
    ]
    if drift:
        raise RuntimeError(
            "isolated replay epoch registered database identity drift: "
            + ",".join(sorted(drift))
        )


def _read_isolated_replay_connection_identity(
    connection: Any,
) -> dict[str, object]:
    try:
        driver_connection = connection.connection.driver_connection
        cursor = driver_connection.cursor()
        try:
            cursor.execute(
                """
                SELECT VERSION() AS version,
                       DATABASE() AS database_name,
                       @@server_uuid AS server_uuid,
                       @@session.time_zone AS session_time_zone,
                       @@default_storage_engine AS storage_engine,
                       @@session.sql_mode AS sql_mode,
                       @@session.transaction_isolation AS isolation_level,
                       @@bind_address AS bind_address,
                       @@port AS port,
                       @@socket AS socket_path,
                       @@datadir AS datadir,
                       @@secure_file_priv AS secure_file_priv,
                       @@foreign_key_checks AS foreign_key_checks,
                       @@global.log_bin AS log_bin,
                       @@global.local_infile AS local_infile
                """
            )
            raw = cursor.fetchone()
            description = tuple(cursor.description or ())
        finally:
            cursor.close()
    except Exception as exc:
        raise RuntimeError(
            "isolated replay epoch actual database identity is unavailable"
        ) from exc
    if raw is None or len(raw) != len(description):
        raise RuntimeError(
            "isolated replay epoch actual database identity is invalid"
        )
    row = {
        str(column[0]): value
        for column, value in zip(description, raw, strict=True)
    }
    if set(row) != set(_ISOLATED_REPLAY_IDENTITY_FIELDS):
        raise RuntimeError(
            "isolated replay epoch actual database identity columns drift"
        )
    normalized = {
        "version": str(row["version"] or "").strip(),
        "database_name": str(row["database_name"] or "").strip(),
        "server_uuid": str(row["server_uuid"] or "").strip().lower(),
        "session_time_zone": str(
            row["session_time_zone"] or ""
        ).strip(),
        "storage_engine": str(row["storage_engine"] or "").strip(),
        "sql_mode": str(row["sql_mode"] or "").strip(),
        "isolation_level": str(
            row["isolation_level"] or ""
        ).strip(),
        "bind_address": str(row["bind_address"] or "").strip(),
        "port": int(row["port"]),
        "socket_path": str(
            Path(str(row["socket_path"] or "")).resolve()
        ),
        "datadir": str(Path(str(row["datadir"] or "")).resolve()),
        "secure_file_priv": str(
            Path(str(row["secure_file_priv"] or "")).resolve()
        ),
        "foreign_key_checks": int(row["foreign_key_checks"]),
        "log_bin": int(row["log_bin"]),
        "local_infile": int(row["local_infile"]),
    }
    return normalized


def _validate_isolated_replay_identity(
    normalized: Mapping[str, object],
) -> None:
    sql_modes = {
        value.strip().upper()
        for value in str(normalized["sql_mode"]).split(",")
        if value.strip()
    }
    if (
        not str(normalized["version"]).startswith("8.0.45")
        or _ISOLATED_REPLAY_DATABASE_RE.fullmatch(
            str(normalized["database_name"])
        )
        is None
        or _ISOLATED_REPLAY_UUID_RE.fullmatch(
            str(normalized["server_uuid"])
        )
        is None
        or normalized["session_time_zone"] != "+00:00"
        or str(normalized["storage_engine"]).lower() != "innodb"
        or not sql_modes.intersection(
            {"STRICT_TRANS_TABLES", "STRICT_ALL_TABLES"}
        )
        or str(normalized["isolation_level"]).upper()
        != "REPEATABLE-READ"
        or normalized["bind_address"] != "127.0.0.1"
        or int(normalized["port"]) <= 0
        or int(normalized["port"]) > 65535
        or int(normalized["port"]) == 3306
        or not normalized["socket_path"]
        or not normalized["datadir"]
        or not normalized["secure_file_priv"]
        or int(normalized["foreign_key_checks"]) != 1
        or int(normalized["log_bin"]) != 0
        or int(normalized["local_infile"]) != 0
    ):
        raise RuntimeError(
            "isolated replay epoch actual database identity drift"
        )


def build_daily_coordinator_epoch_record(
    *,
    current: DailyCoordinatorEpochIdentity,
    epoch: int,
    mode: str,
    transition_id: str,
) -> bytes:
    """为 root operator 构造下一条 canonical record；本函数不写机器状态。"""
    if not isinstance(current, DailyCoordinatorEpochIdentity):
        raise TypeError("current daily coordinator epoch is invalid")
    if (
        not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch != current.epoch + 1
    ):
        raise ValueError(
            "daily coordinator transition epoch must be exactly current "
            "epoch + 1"
        )
    if mode not in VALID_DAILY_COORDINATOR_MODES:
        raise ValueError(
            "daily coordinator transition mode must be legacy or ledger"
        )
    if (
        not isinstance(transition_id, str)
        or _TRANSITION_ID_RE.fullmatch(transition_id) is None
        or transition_id == current.transition_id
    ):
        raise ValueError(
            "daily coordinator transition_id is invalid or replayed"
        )
    return _canonical_json_bytes(
        {
            "epoch": epoch,
            "mode": mode,
            "previous_record_sha256": current.record_sha256,
            "schema_version": EPOCH_SCHEMA_VERSION,
            "transition_id": transition_id,
        }
    )


def _bind_process_identity(
    identity: DailyCoordinatorEpochIdentity,
) -> None:
    global _process_bound_identity
    with _process_identity_lock:
        if _process_bound_identity is None:
            _process_bound_identity = identity
            return
        if _process_bound_identity != identity:
            raise RuntimeError(
                "process-bound daily coordinator epoch drift detected: "
                f"bound={_process_bound_identity.epoch}/"
                f"{_process_bound_identity.mode}/"
                f"{_process_bound_identity.record_sha256}; "
                f"current={identity.epoch}/{identity.mode}/"
                f"{identity.record_sha256}"
            )


def _reset_daily_coordinator_process_identity_for_tests() -> None:
    """测试隔离专用；生产代码不得清除 process epoch fence。"""
    global _process_bound_identity
    with _process_identity_lock:
        _process_bound_identity = None


def _read_epoch_contract_and_genesis(
    *,
    contract_path: Path,
    genesis_path: Path,
) -> tuple[bytes, bytes, Mapping[str, object]]:
    contract_raw = _read_bounded_file(
        contract_path,
        label="daily coordinator epoch contract",
        limit=_MAX_CONTRACT_BYTES,
    )
    contract = _parse_canonical_mapping(
        contract_raw,
        label="daily coordinator epoch contract",
        expected_fields=_CONTRACT_FIELDS,
    )
    expected_contract = {
        "epoch_filename": EPOCH_FILENAME_FORMAT,
        "first_epoch": 1,
        "modes": ["legacy", "ledger"],
        "record_schema_version": EPOCH_SCHEMA_VERSION,
        "schema_version": EPOCH_CONTRACT_SCHEMA_VERSION,
        "zero_previous_record_sha256": ZERO_RECORD_SHA256,
    }
    for field, expected in expected_contract.items():
        if contract.get(field) != expected:
            raise RuntimeError(
                f"daily coordinator epoch contract {field} is invalid"
            )
    genesis_sha256 = contract.get("genesis_sha256")
    if (
        not isinstance(genesis_sha256, str)
        or _SHA256_RE.fullmatch(genesis_sha256) is None
    ):
        raise RuntimeError(
            "daily coordinator epoch contract genesis_sha256 is invalid"
        )
    genesis_raw = _read_bounded_file(
        genesis_path,
        label="daily coordinator epoch genesis",
        limit=_MAX_EPOCH_RECORD_BYTES,
    )
    if hashlib.sha256(genesis_raw).hexdigest() != genesis_sha256:
        raise RuntimeError(
            "daily coordinator epoch genesis digest differs from contract"
        )
    genesis = _parse_epoch_record(genesis_raw)
    if (
        genesis.get("epoch") != 1
        or genesis.get("mode") != "ledger"
        or genesis.get("previous_record_sha256")
        != ZERO_RECORD_SHA256
    ):
        raise RuntimeError(
            "daily coordinator epoch genesis semantics are invalid"
        )
    return contract_raw, genesis_raw, genesis


def _read_epoch_file(
    directory_descriptor: int,
    *,
    name: str,
    expected_owner_uid: int,
    service_uid: int,
) -> bytes:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(
            name,
            flags,
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise RuntimeError(
            f"daily coordinator epoch record is unreadable or unsafe: {name}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        _validate_epoch_file_security(
            before,
            name=name,
            expected_owner_uid=expected_owner_uid,
            service_uid=service_uid,
        )
        raw = _read_descriptor_bytes(
            descriptor,
            label=f"daily coordinator epoch record {name}",
            limit=_MAX_EPOCH_RECORD_BYTES,
        )
        after = os.fstat(descriptor)
        if _file_stat_identity(before) != _file_stat_identity(after):
            raise RuntimeError(
                f"daily coordinator epoch record changed while read: {name}"
            )
        return raw
    finally:
        os.close(descriptor)


def _parse_epoch_record(raw: bytes) -> Mapping[str, object]:
    payload = _parse_canonical_mapping(
        raw,
        label="daily coordinator epoch record",
        expected_fields=_EPOCH_FIELDS,
    )
    epoch = payload.get("epoch")
    if (
        not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch <= 0
    ):
        raise RuntimeError(
            "daily coordinator epoch must be a positive integer"
        )
    if payload.get("schema_version") != EPOCH_SCHEMA_VERSION:
        raise RuntimeError(
            "daily coordinator epoch schema_version is invalid"
        )
    if payload.get("mode") not in VALID_DAILY_COORDINATOR_MODES:
        raise RuntimeError(
            "daily coordinator epoch mode must be legacy or ledger"
        )
    previous = payload.get("previous_record_sha256")
    if (
        not isinstance(previous, str)
        or _SHA256_RE.fullmatch(previous) is None
    ):
        raise RuntimeError(
            "daily coordinator epoch previous_record_sha256 is invalid"
        )
    transition_id = payload.get("transition_id")
    if (
        not isinstance(transition_id, str)
        or _TRANSITION_ID_RE.fullmatch(transition_id) is None
    ):
        raise RuntimeError(
            "daily coordinator epoch transition_id is invalid"
        )
    return payload


def _parse_canonical_mapping(
    raw: bytes,
    *,
    label: str,
    expected_fields: frozenset[str],
) -> Mapping[str, object]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise RuntimeError(f"{label} fields are invalid")
    if raw != _canonical_json_bytes(payload):
        raise RuntimeError(f"{label} must be canonical JSON")
    return payload


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "daily coordinator epoch value is not canonical JSON data"
        ) from exc


def _read_bounded_file(
    path: Path,
    *,
    label: str,
    limit: int,
) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"{label} is missing or unreadable") from exc
    if len(raw) > limit:
        raise RuntimeError(f"{label} is too large")
    return raw


def _read_descriptor_bytes(
    descriptor: int,
    *,
    label: str,
    limit: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise RuntimeError(f"{label} is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _normalized_absolute_path(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise RuntimeError(f"{label} path must be absolute")
    normalized = path.resolve(strict=False)
    if normalized != path:
        raise RuntimeError(
            f"{label} path must be normalized and symlink-free"
        )
    return normalized


def _validated_uid(value: int | None, label: str) -> int:
    uid = os.getuid() if value is None else value
    if (
        not isinstance(uid, int)
        or isinstance(uid, bool)
        or uid < 0
    ):
        raise RuntimeError(f"{label} is invalid")
    return uid


def _validate_epoch_directory_security(
    details: os.stat_result,
    *,
    path: Path,
    expected_owner_uid: int,
    service_uid: int,
) -> None:
    if not stat.S_ISDIR(details.st_mode):
        raise RuntimeError(
            f"daily coordinator epoch path is not a directory: {path}"
        )
    if details.st_uid != expected_owner_uid:
        raise RuntimeError(
            "daily coordinator epoch directory has unexpected owner: "
            f"{path}"
        )
    if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError(
            f"daily coordinator epoch directory is writable by non-root: {path}"
        )
    _require_service_access(
        details.st_mode,
        owner_uid=details.st_uid,
        service_uid=service_uid,
        owner_bit=stat.S_IXUSR,
        other_bit=stat.S_IXOTH,
        label=f"daily coordinator epoch directory {path}",
    )


def _validate_epoch_file_security(
    details: os.stat_result,
    *,
    name: str,
    expected_owner_uid: int,
    service_uid: int,
) -> None:
    if not stat.S_ISREG(details.st_mode):
        raise RuntimeError(
            f"daily coordinator epoch record is not regular: {name}"
        )
    if details.st_uid != expected_owner_uid:
        raise RuntimeError(
            f"daily coordinator epoch record has unexpected owner: {name}"
        )
    if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError(
            f"daily coordinator epoch record is writable by non-root: {name}"
        )
    _require_service_access(
        details.st_mode,
        owner_uid=details.st_uid,
        service_uid=service_uid,
        owner_bit=stat.S_IRUSR,
        other_bit=stat.S_IROTH,
        label=f"daily coordinator epoch record {name}",
    )


def _validate_epoch_parent_chain(
    start: Path,
    *,
    anchor: Path,
    expected_owner_uid: int,
    service_uid: int,
) -> None:
    if start != anchor and anchor not in start.parents:
        raise RuntimeError(
            "daily coordinator epoch parent anchor does not contain the "
            "epoch directory"
        )
    current = start
    while True:
        try:
            details = os.stat(current, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(
                f"daily coordinator epoch parent is unreadable: {current}"
            ) from exc
        if not stat.S_ISDIR(details.st_mode):
            raise RuntimeError(
                f"daily coordinator epoch parent is not a directory: {current}"
            )
        if details.st_uid != expected_owner_uid:
            raise RuntimeError(
                "daily coordinator epoch parent has unexpected owner: "
                f"{current}"
            )
        if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError(
                "daily coordinator epoch parent is writable by non-root: "
                f"{current}"
            )
        _require_service_access(
            details.st_mode,
            owner_uid=details.st_uid,
            service_uid=service_uid,
            owner_bit=stat.S_IXUSR,
            other_bit=stat.S_IXOTH,
            label=f"daily coordinator epoch parent {current}",
        )
        if current == anchor:
            break
        current = current.parent


def _require_service_access(
    mode: int,
    *,
    owner_uid: int,
    service_uid: int,
    owner_bit: int,
    other_bit: int,
    label: str,
) -> None:
    required = owner_bit if service_uid == owner_uid else other_bit
    if not mode & required:
        raise RuntimeError(
            f"{label} is not readable/traversable by service UID "
            f"{service_uid}"
        )


def _file_stat_identity(
    details: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _directory_stat_identity(
    details: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return _file_stat_identity(details)
