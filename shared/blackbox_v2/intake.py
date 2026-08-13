from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from shared.blackbox_v2.contracts import BlackboxMetadata, load_metadata
from shared.blackbox_v2.platform_input_registry import (
    normalize_platform_input_ids,
)
from shared.scheme_owner_registry import (
    OWNER_FILE_RELATIVE_PATH,
    load_scheme_owner_document,
    owner_registry_scheme_id,
    serialize_scheme_owner_document,
)


SCHEDULES = {
    "daily": "3 7 * * 1-5",
    "weekly": "30 11 * * 6",
    "monthly": "0 18 15 * *",
}
def intake_delivery(
    delivery_dir: str | Path,
    *,
    schemes_root: str | Path,
    runtime_profile: str = "blackbox-v2-v1",
    data_schema_version: str = "data-bridge-v1",
    platform_inputs: Sequence[str] | None = None,
) -> Path:
    """Atomically preserve a two-file delivery and generate its platform-owned config."""
    normalized_platform_inputs = (
        ()
        if platform_inputs is None
        else normalize_platform_input_ids(platform_inputs)
    )
    source = Path(delivery_dir).resolve()
    if not source.is_dir():
        raise ValueError(f"delivery directory does not exist: {source}")
    entries = sorted(source.iterdir())
    if len(entries) != 2 or any(not item.is_file() or item.is_symlink() for item in entries):
        raise ValueError("Blackbox V2 delivery must contain exactly two regular files")
    scripts = [item for item in entries if item.suffix == ".py"]
    metadata_files = [item for item in entries if item.suffix == ".json"]
    if len(scripts) != 1 or len(metadata_files) != 1:
        raise ValueError("Blackbox V2 delivery must contain one .py and one .json")
    metadata = load_metadata(metadata_files[0])
    if metadata.owner is None:
        raise ValueError("owner is required for a new Blackbox V2 Intake")
    if metadata.description is None:
        raise ValueError("description is required for a new Blackbox V2 Intake")
    expected_names = {f"{metadata.scheme_id}.py", f"{metadata.scheme_id}.json"}
    if {item.name for item in entries} != expected_names:
        raise ValueError("delivery filenames must match metadata scheme_id")

    destination_root = Path(schemes_root).resolve()
    project_root = destination_root.parent
    lock_descriptor = _acquire_intake_lock(project_root)
    staged_registry: Path | None = None
    try:
        destination = destination_root / metadata.scheme_id
        if destination.exists():
            raise FileExistsError(f"scheme already exists: {metadata.scheme_id}")

        registry_path = project_root / OWNER_FILE_RELATIVE_PATH
        registry_id = owner_registry_scheme_id(
            metadata.scheme_id,
            metadata.horizon,
            metadata.target_tenor,
        )
        original_registry_bytes, updated_registry_bytes = _prepare_owner_update(
            registry_path,
            registry_id=registry_id,
            owner=metadata.owner,
        )
        registry_mode = registry_path.stat().st_mode & 0o777
        destination_root.mkdir(parents=True, exist_ok=True)

        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{metadata.scheme_id}.",
                dir=destination_root,
            )
        )
        registry_committed = False
        try:
            staged_delivery = staging / "delivery"
            staged_delivery.mkdir()
            shutil.copyfile(scripts[0], staged_delivery / scripts[0].name)
            shutil.copyfile(
                metadata_files[0],
                staged_delivery / metadata_files[0].name,
            )
            (staging / "config.yaml").write_text(
                _config_text(
                    metadata,
                    runtime_profile=runtime_profile,
                    data_schema_version=data_schema_version,
                    platform_inputs=normalized_platform_inputs,
                ),
                encoding="utf-8",
            )
            for path in staged_delivery.iterdir():
                path.chmod(0o444)
            if updated_registry_bytes is not None:
                staged_registry = _stage_registry(
                    registry_path,
                    updated_registry_bytes,
                    mode=registry_mode,
                )
                _require_registry_unchanged(
                    registry_path,
                    original_registry_bytes,
                )
                os.replace(staged_registry, registry_path)
                registry_committed = True
                staged_registry = None
            try:
                if updated_registry_bytes is None:
                    _require_registry_unchanged(
                        registry_path,
                        original_registry_bytes,
                    )
                os.replace(staging, destination)
            except Exception as exc:
                if registry_committed:
                    _restore_registry(
                        registry_path,
                        original_registry_bytes,
                        exc,
                    )
                raise
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            if staged_registry is not None:
                staged_registry.unlink(missing_ok=True)
            raise
        return destination
    finally:
        _release_intake_lock(lock_descriptor)


def _acquire_intake_lock(project_root: Path) -> int:
    """串行化同一 project root 内的 scheme 与 owner registry 提交。"""
    root_digest = hashlib.sha256(
        str(project_root.resolve()).encode("utf-8")
    ).hexdigest()
    lock_path = Path(tempfile.gettempdir()) / (
        f"bond-factor-lab-blackbox-intake-{root_digest}.lock"
    )
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _release_intake_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _prepare_owner_update(
    registry_path: Path,
    *,
    registry_id: str,
    owner: str,
) -> tuple[bytes | None, bytes | None]:
    """预检 owner 冲突，并返回原内容与需要提交的新内容。"""
    if not registry_path.is_file() or registry_path.is_symlink():
        raise ValueError(
            f"scheme owner registry must be an existing regular file: {registry_path}"
        )
    original = registry_path.read_bytes()
    payload, owners = load_scheme_owner_document(registry_path)
    existing = owners.get(registry_id)
    if existing is not None:
        if existing != owner:
            raise ValueError(
                "scheme owner conflict: "
                f"registry_id={registry_id} existing={existing!r} requested={owner!r}"
            )
        return original, None
    updated: dict[str, Any] = dict(payload)
    updated_owners = dict(owners)
    updated_owners[registry_id] = owner
    updated["owners"] = updated_owners
    return original, serialize_scheme_owner_document(updated)


def _stage_registry(registry_path: Path, content: bytes, *, mode: int) -> Path:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{registry_path.name}.",
        dir=registry_path.parent,
    )
    staged = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(mode)
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def _require_registry_unchanged(
    registry_path: Path,
    original: bytes | None,
) -> None:
    """在原子替换前拒绝覆盖未遵守 Intake lock 的并发改动。"""
    if original is None or registry_path.read_bytes() != original:
        raise RuntimeError(
            "scheme owner registry changed after Intake preflight"
        )


def _restore_registry(
    registry_path: Path,
    original: bytes | None,
    commit_error: Exception,
) -> None:
    """scheme 提交失败时把本次 owner 变更恢复为原始状态。"""
    try:
        if original is None:
            raise RuntimeError("owner registry original bytes are unavailable")
        mode = registry_path.stat().st_mode & 0o777
        rollback = _stage_registry(registry_path, original, mode=mode)
        try:
            os.replace(rollback, registry_path)
        finally:
            rollback.unlink(missing_ok=True)
    except Exception as rollback_error:
        raise RuntimeError(
            "Blackbox V2 Intake failed and owner registry rollback failed"
        ) from ExceptionGroup(
            "intake and rollback failures",
            [commit_error, rollback_error],
        )


def _config_text(
    metadata: BlackboxMetadata,
    *,
    runtime_profile: str,
    data_schema_version: str,
    platform_inputs: tuple[str, ...] = (),
) -> str:
    cron = SCHEDULES[metadata.frequency]
    platform_input_text = ""
    if platform_inputs:
        platform_input_text = "platform_inputs:\n" + "".join(
            f"  - {artifact_id}\n" for artifact_id in platform_inputs
        )
    return (
        f"scheme_id: {metadata.scheme_id}\n"
        "runtime_type: blackbox_v2\n"
        "input_source: data_bridge_current\n"
        f"runtime_profile: {runtime_profile}\n"
        f"data_schema_version: {data_schema_version}\n"
        f"{platform_input_text}"
        "status: paused\n"
        "version_status: draft\n"
        "schedule:\n"
        f"  cron: '{cron}'\n"
        "  timezone: Asia/Shanghai\n"
        "delivery:\n"
        f"  script: delivery/{metadata.scheme_id}.py\n"
        f"  metadata: delivery/{metadata.scheme_id}.json\n"
    )
