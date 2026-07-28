#!/usr/bin/env python
"""Root-only 日频 coordinator epoch 切换工具。

最终 record 不直接边写边发布：先在同一文件系统的受管 ``staging/`` 中
完成写入、fsync 和内容复核，再以 hard-link no-clobber 原子发布到
``records/``，最后 fsync records 目录。
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.daily_coordinator_mode import (
    DEFAULT_EPOCH_CONTRACT_PATH,
    DEFAULT_EPOCH_DIRECTORY,
    DEFAULT_EPOCH_GENESIS_PATH,
    DEFAULT_EPOCH_OWNER_UID,
    EPOCH_FILENAME_FORMAT,
    EPOCH_RECORDS_DIRECTORY_NAME,
    DailyCoordinatorEpochIdentity,
    _parse_epoch_record,
    _read_epoch_contract_and_genesis,
    _normalized_absolute_path,
    _validate_epoch_directory_security,
    _validate_epoch_parent_chain,
    _validated_uid,
    build_daily_coordinator_epoch_record,
    read_daily_coordinator_epoch_chain,
)
from scheduler.daily_control_plane_probe import (
    LAUNCHAGENT_LABELS,
    QUIESCENCE_FIELDS,
    _is_daily_platform_process,
    _read_database_quiescence,
    _validate_quiescence_report,
    probe_daily_transition_quiescence,
    probe_launchagent_service_states,
    read_installed_launchagent_modes,
)
from scheduler.daily_policy import POLICY_V2_PATH


_STAGING_DIRECTORY_NAME = "staging"
_MAX_RECORD_BYTES = 4096
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class EpochPublicationResult:
    """一次 operator publish 的稳定结果。"""

    status: str
    identity: DailyCoordinatorEpochIdentity
    record_path: Path


def publish_daily_coordinator_epoch(
    *,
    expected_current_epoch: int,
    mode: str,
    transition_id: str,
    service_uid: int,
    business_date: date,
    epoch_directory: str | Path = DEFAULT_EPOCH_DIRECTORY,
    epoch_contract_path: str | Path = DEFAULT_EPOCH_CONTRACT_PATH,
    epoch_genesis_path: str | Path = DEFAULT_EPOCH_GENESIS_PATH,
    expected_owner_uid: int = DEFAULT_EPOCH_OWNER_UID,
    parent_anchor: str | Path = Path("/"),
    effective_uid: int | None = None,
    service_state_probe: Callable[[int], Mapping[str, bool]] | None = None,
    installed_mode_probe: Callable[[int], Mapping[str, str]] | None = None,
    quiescence_probe: (
        Callable[[int, date], Mapping[str, int]] | None
    ) = None,
    capacity_admission_probe: (
        Callable[[int], Mapping[str, object]] | None
    ) = None,
    phase_hook: Callable[[str], None] | None = None,
) -> EpochPublicationResult:
    """在停机维护窗口追加恰好一个更高 epoch。

    ``expected_current_epoch`` 是 operator 的 anti-replay fence。调用在最终
    publish 后丢失返回时，使用相同参数重试只会返回
    ``already_published``，绝不会再追加一个 epoch。
    """
    actual_effective_uid = (
        os.geteuid() if effective_uid is None else effective_uid
    )
    if actual_effective_uid != 0:
        raise PermissionError(
            "daily coordinator epoch transition must run as root"
        )
    if (
        not isinstance(expected_current_epoch, int)
        or isinstance(expected_current_epoch, bool)
        or expected_current_epoch < 0
    ):
        raise ValueError("expected_current_epoch must be >= 0")
    owner_uid = _validated_uid(
        expected_owner_uid,
        "daily coordinator epoch owner UID",
    )
    launchagent_uid = _validated_uid(
        service_uid,
        "daily coordinator service UID",
    )
    if not isinstance(business_date, date):
        raise TypeError("business_date must be a date")

    root = _normalized_absolute_path(
        Path(epoch_directory),
        "daily coordinator epoch directory",
    )
    contract_path = Path(epoch_contract_path)
    genesis_path = Path(epoch_genesis_path)
    anchor = _normalized_absolute_path(
        Path(parent_anchor),
        "daily coordinator epoch parent anchor",
    )
    next_epoch = expected_current_epoch + 1
    final_path = (
        root
        / EPOCH_RECORDS_DIRECTORY_NAME
        / (EPOCH_FILENAME_FORMAT % next_epoch)
    )

    current = _read_current_for_operator(
        expected_current_epoch=expected_current_epoch,
        final_path=final_path,
        epoch_directory=root,
        epoch_contract_path=contract_path,
        epoch_genesis_path=genesis_path,
        expected_owner_uid=owner_uid,
        service_uid=launchagent_uid,
        parent_anchor=anchor,
    )
    if current is not None and current.epoch == next_epoch:
        if (
            current.mode == mode
            and current.transition_id == transition_id
        ):
            return EpochPublicationResult(
                status="already_published",
                identity=current,
                record_path=final_path,
            )
        raise RuntimeError(
            "daily coordinator epoch was already published with a "
            "different mode or transition_id"
        )
    actual_epoch = 0 if current is None else current.epoch
    if actual_epoch != expected_current_epoch:
        raise RuntimeError(
            "daily coordinator current epoch differs from operator fence: "
            f"expected={expected_current_epoch}, actual={actual_epoch}"
        )

    states = (
        probe_launchagent_service_states(launchagent_uid)
        if service_state_probe is None
        else dict(service_state_probe(launchagent_uid))
    )
    _require_exact_probe_labels(states, "service state")
    loaded = sorted(label for label, is_loaded in states.items() if is_loaded)
    if loaded:
        raise RuntimeError(
            "daily coordinator services are still loaded; bootout all "
            f"LaunchAgents before transition: {loaded}"
        )

    installed_modes = (
        read_installed_launchagent_modes(launchagent_uid)
        if installed_mode_probe is None
        else dict(installed_mode_probe(launchagent_uid))
    )
    _require_exact_probe_labels(installed_modes, "installed plist")
    drifted = {
        label: installed_modes[label]
        for label in LAUNCHAGENT_LABELS
        if installed_modes[label] != mode
    }
    if drifted:
        raise RuntimeError(
            "all installed plist modes must match the target epoch mode: "
            f"target={mode}, drifted={drifted}"
        )

    quiescence = (
        probe_daily_transition_quiescence(
            launchagent_uid,
            business_date,
        )
        if quiescence_probe is None
        else dict(quiescence_probe(launchagent_uid, business_date))
    )
    _validate_quiescence_report(quiescence)
    busy = {
        field: value
        for field, value in quiescence.items()
        if value != 0
    }
    if busy:
        raise RuntimeError(
            "daily coordinator transition is not quiescent: "
            f"{busy}"
        )

    if mode == "ledger":
        admission = (
            require_trusted_current_capacity_admission(launchagent_uid)
            if capacity_admission_probe is None
            else capacity_admission_probe(launchagent_uid)
        )
        _validate_capacity_admission(admission)

    _contract_raw, genesis_raw, genesis_payload = (
        _read_epoch_contract_and_genesis(
            contract_path=contract_path,
            genesis_path=genesis_path,
        )
    )
    if current is None:
        if (
            next_epoch != 1
            or mode != genesis_payload["mode"]
            or transition_id != genesis_payload["transition_id"]
        ):
            raise RuntimeError(
                "the first epoch must publish the fixed canonical genesis"
            )
        record_raw = genesis_raw
    else:
        record_raw = build_daily_coordinator_epoch_record(
            current=current,
            epoch=next_epoch,
            mode=mode,
            transition_id=transition_id,
        )

    _atomic_publish_record(
        record_raw,
        epoch=next_epoch,
        epoch_directory=root,
        expected_owner_uid=owner_uid,
        service_uid=launchagent_uid,
        parent_anchor=anchor,
        phase_hook=phase_hook,
    )
    identity = read_daily_coordinator_epoch_chain(
        epoch_directory=root,
        epoch_contract_path=contract_path,
        epoch_genesis_path=genesis_path,
        expected_owner_uid=owner_uid,
        service_uid=launchagent_uid,
        parent_anchor=anchor,
    )
    if (
        identity is None
        or identity.epoch != next_epoch
        or identity.mode != mode
        or identity.transition_id != transition_id
    ):
        raise RuntimeError(
            "published daily coordinator epoch did not pass full chain "
            "verification"
        )
    return EpochPublicationResult(
        status="published",
        identity=identity,
        record_path=final_path,
    )


def _read_current_for_operator(
    *,
    expected_current_epoch: int,
    final_path: Path,
    epoch_directory: Path,
    epoch_contract_path: Path,
    epoch_genesis_path: Path,
    expected_owner_uid: int,
    service_uid: int,
    parent_anchor: Path,
) -> DailyCoordinatorEpochIdentity | None:
    try:
        return read_daily_coordinator_epoch_chain(
            epoch_directory=epoch_directory,
            epoch_contract_path=epoch_contract_path,
            epoch_genesis_path=epoch_genesis_path,
            expected_owner_uid=expected_owner_uid,
            service_uid=service_uid,
            parent_anchor=parent_anchor,
        )
    except RuntimeError as exc:
        if final_path.exists():
            raise RuntimeError(
                "existing final epoch record is invalid and will not be "
                "overwritten"
            ) from exc
        records = epoch_directory / EPOCH_RECORDS_DIRECTORY_NAME
        if (
            expected_current_epoch == 0
            and records.is_dir()
            and not any(records.iterdir())
        ):
            # 允许 root operator 修复首次 publish 前遗留的空布局/partial
            # staging；普通 reader 仍必须 fail-closed。
            return None
        raise


def _atomic_publish_record(
    record_raw: bytes,
    *,
    epoch: int,
    epoch_directory: Path,
    expected_owner_uid: int,
    service_uid: int,
    parent_anchor: Path,
    phase_hook: Callable[[str], None] | None,
) -> None:
    payload = _parse_epoch_record(record_raw)
    if payload["epoch"] != epoch:
        raise RuntimeError(
            "daily coordinator record epoch differs from final filename"
        )
    if len(record_raw) > _MAX_RECORD_BYTES:
        raise RuntimeError("daily coordinator epoch record is too large")
    root, records, staging = _ensure_epoch_layout(
        epoch_directory,
        expected_owner_uid=expected_owner_uid,
        service_uid=service_uid,
        parent_anchor=parent_anchor,
    )
    if phase_hook is not None:
        phase_hook("after_layout_fsync")
    final_name = EPOCH_FILENAME_FORMAT % epoch
    temporary_name = (
        f".{final_name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    staging_fd = os.open(os.fspath(staging), directory_flags)
    records_fd = os.open(os.fspath(records), directory_flags)
    temporary_created = False
    published = False
    try:
        file_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            temporary_name,
            file_flags,
            0o600,
            dir_fd=staging_fd,
        )
        temporary_created = True
        try:
            _write_all(descriptor, record_raw)
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            verified = _read_all(descriptor, _MAX_RECORD_BYTES)
            if verified != record_raw:
                raise RuntimeError(
                    "staged daily coordinator epoch failed byte verification"
                )
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != expected_owner_uid
                or stat.S_IMODE(details.st_mode) != 0o644
            ):
                raise RuntimeError(
                    "staged daily coordinator epoch owner/mode is invalid"
                )
        finally:
            os.close(descriptor)
        if phase_hook is not None:
            phase_hook("after_staging_fsync")
        try:
            os.link(
                temporary_name,
                final_name,
                src_dir_fd=staging_fd,
                dst_dir_fd=records_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise RuntimeError(
                "existing final epoch record refuses no-clobber publish"
            ) from exc
        published = True
        os.fsync(records_fd)
        if phase_hook is not None:
            phase_hook("after_publish")
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=staging_fd)
                os.fsync(staging_fd)
            except FileNotFoundError:
                pass
        os.close(records_fd)
        os.close(staging_fd)
    if not published:
        raise RuntimeError("daily coordinator epoch was not published")


def _ensure_epoch_layout(
    epoch_directory: Path,
    *,
    expected_owner_uid: int,
    service_uid: int,
    parent_anchor: Path,
) -> tuple[Path, Path, Path]:
    _validate_epoch_parent_chain(
        epoch_directory.parent,
        anchor=parent_anchor,
        expected_owner_uid=expected_owner_uid,
        service_uid=service_uid,
    )
    _mkdir_if_missing(epoch_directory, 0o755)
    records = epoch_directory / EPOCH_RECORDS_DIRECTORY_NAME
    staging = epoch_directory / _STAGING_DIRECTORY_NAME
    _mkdir_if_missing(records, 0o755)
    _mkdir_if_missing(staging, 0o700)
    _validate_epoch_directory_security(
        os.stat(epoch_directory, follow_symlinks=False),
        path=epoch_directory,
        expected_owner_uid=expected_owner_uid,
        service_uid=service_uid,
    )
    _validate_epoch_directory_security(
        os.stat(records, follow_symlinks=False),
        path=records,
        expected_owner_uid=expected_owner_uid,
        service_uid=service_uid,
    )
    staging_details = os.stat(staging, follow_symlinks=False)
    if (
        not stat.S_ISDIR(staging_details.st_mode)
        or staging_details.st_uid != expected_owner_uid
        or stat.S_IMODE(staging_details.st_mode) != 0o700
    ):
        raise RuntimeError(
            "daily coordinator epoch staging directory must be root-owned "
            "0700"
        )
    return epoch_directory, records, staging


def _mkdir_if_missing(path: Path, mode: int) -> None:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    parent_descriptor = os.open(os.fspath(path.parent), flags)
    created = False
    try:
        try:
            os.mkdir(path.name, mode, dir_fd=parent_descriptor)
            created = True
        except FileExistsError:
            pass
        child_descriptor = os.open(
            path.name,
            flags,
            dir_fd=parent_descriptor,
        )
        try:
            if created:
                os.fchmod(child_descriptor, mode)
            os.fsync(child_descriptor)
            details = os.fstat(child_descriptor)
        finally:
            os.close(child_descriptor)
        if created:
            os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    if not stat.S_ISDIR(details.st_mode):
        raise RuntimeError(
            f"daily coordinator epoch layout path is unsafe: {path}"
        )
    if stat.S_IMODE(details.st_mode) != mode:
        raise RuntimeError(
            f"daily coordinator epoch layout mode is invalid: {path}"
        )


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("daily coordinator staging write made no progress")
        offset += written


def _read_all(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise RuntimeError("daily coordinator staged record is too large")
        chunks.append(chunk)


def _require_exact_probe_labels(
    rows: Mapping[str, object],
    label: str,
) -> None:
    if set(rows) != set(LAUNCHAGENT_LABELS):
        raise RuntimeError(
            f"daily coordinator {label} probe did not cover all services"
        )


def require_trusted_current_capacity_admission(
    service_uid: int,
) -> Mapping[str, object]:
    """以真实 LaunchAgent UID 重算并验证签名 capacity admission。"""
    if os.geteuid() == service_uid:
        return _require_capacity_admission_in_current_process()
    if os.geteuid() != 0:
        raise PermissionError(
            "capacity admission UID switch requires root"
        )
    try:
        account = pwd.getpwuid(service_uid)
    except (KeyError, OSError) as exc:
        raise RuntimeError(
            f"service UID has no local account: {service_uid}"
        ) from exc
    child_code = (
        "import json;"
        "from scheduler.capacity_runtime_admission import "
        "require_current_capacity_admission;"
        "from scheduler.daily_policy import POLICY_V2_PATH;"
        "from scheduler.repository import create_engine_from_env;"
        "engine=create_engine_from_env();"
        "result=require_current_capacity_admission("
        "engine,policy_path=POLICY_V2_PATH);"
        "engine.dispose();"
        "print(json.dumps({'status':result.get('status'),"
        "'candidate_fingerprint':result.get('candidate_fingerprint')},"
        "sort_keys=True))"
    )

    def drop_to_service_uid() -> None:
        os.initgroups(account.pw_name, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(service_uid)

    completed = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        preexec_fn=drop_to_service_uid,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "current capacity admission verification failed under the "
            f"LaunchAgent UID: {completed.stderr[-2000:]}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "capacity admission verifier returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "capacity admission verifier returned an invalid object"
        )
    return payload


def _require_capacity_admission_in_current_process() -> Mapping[str, object]:
    from scheduler.capacity_runtime_admission import (
        require_current_capacity_admission,
    )
    from scheduler.repository import create_engine_from_env

    engine = create_engine_from_env()
    try:
        return require_current_capacity_admission(
            engine,
            policy_path=POLICY_V2_PATH,
        )
    finally:
        engine.dispose()


def _validate_capacity_admission(
    admission: Mapping[str, object],
) -> None:
    if not isinstance(admission, Mapping):
        raise RuntimeError(
            "daily coordinator target ledger capacity admission is invalid"
        )
    fingerprint = admission.get("candidate_fingerprint")
    if (
        admission.get("status") != "ADMITTED"
        or not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(
            character not in "0123456789abcdef"
            for character in fingerprint
        )
    ):
        raise RuntimeError(
            "daily coordinator target ledger requires current trusted "
            "capacity admission"
        )


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "business date must be YYYY-MM-DD"
        ) from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-current-epoch", type=int, required=True)
    parser.add_argument(
        "--mode",
        choices=("legacy", "ledger"),
        required=True,
    )
    parser.add_argument("--transition-id", required=True)
    parser.add_argument("--service-uid", type=int, required=True)
    parser.add_argument(
        "--business-date",
        type=_parse_date,
        default=datetime.now(_SHANGHAI).date(),
    )
    args = parser.parse_args(argv)
    try:
        result = publish_daily_coordinator_epoch(
            expected_current_epoch=args.expected_current_epoch,
            mode=args.mode,
            transition_id=args.transition_id,
            service_uid=args.service_uid,
            business_date=args.business_date,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "rejected",
                    "error": str(exc),
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": result.status,
                "epoch": result.identity.epoch,
                "mode": result.identity.mode,
                "record_sha256": result.identity.record_sha256,
                "record_path": os.fspath(result.record_path),
                "transition_id": result.identity.transition_id,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
