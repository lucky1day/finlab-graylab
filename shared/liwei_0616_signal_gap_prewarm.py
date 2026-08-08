"""封存 Native signal-gap Phase-A cache 的一次性子进程 permit。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from shared.input_artifacts import NATIVE_MANIFEST_PATH_ENV
from shared.liwei_0616_cache_contract import (
    SIGNAL_GAP_CACHE_PREWARM_PUBLISHER_SCHEME_ID,
)
from shared.native_input_generation import (
    SIGNAL_GAP_NATIVE_EXPORTER_VERSION,
    resolve_signal_gap_native_storage_root,
)


PREWARM_PERMIT_ENV = "BOND_LIWEI_0616_SIGNAL_GAP_PREWARM_PERMIT"
PREWARM_CAPABILITY_ENV = "BOND_LIWEI_0616_SIGNAL_GAP_PREWARM_CAPABILITY"
PREWARM_PERMIT_SCHEMA_VERSION = "liwei_0616-signal-gap-prewarm-permit-v1"
_PERMIT_PREFIX = ".prewarm-permit-"
_PERMIT_SUFFIX = ".json"
_PERMIT_PATTERN = re.compile(
    r"^\.prewarm-permit-([A-Za-z0-9_-]{16,128})\.json$"
)
_PERMIT_FIELDS = frozenset(
    {
        "schema_version",
        "nonce",
        "publisher_scheme_id",
        "artifact_root",
        "cache_root",
        "native_generation",
        "expires_at",
        "authorization_token_sha256",
        "capability_sha256",
    }
)
_NATIVE_BINDING_FIELDS = frozenset(
    {
        "generation_id",
        "manifest_sha256",
        "dataset_content_id",
        "business_date",
        "feature_date",
        "schema_version",
        "exporter_version",
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class SignalGapCachePrewarmPermit:
    """父 harness 写入、算法子进程只消费的一次性 permit。"""

    path: Path
    capability: str
    expires_at: str


def create_signal_gap_cache_prewarm_permit(
    *,
    artifact_root: Path,
    cache_root: Path,
    publisher_scheme_id: str,
    native_generation: Mapping[str, object],
    expires_at: str,
    authorization_token_sha256: str,
) -> SignalGapCachePrewarmPermit:
    """在 HMAC 已验证后签发一次 private、artifact-bound 子进程 permit。"""
    normalized_artifact_root = _resolve_artifact_root(artifact_root)
    normalized_native = _normalize_native_generation(native_generation)
    expected_cache_root = _expected_cache_root(
        normalized_artifact_root,
        generation_id=str(normalized_native["generation_id"]),
    )
    normalized_cache_root = _require_exact_cache_root(
        cache_root,
        expected_cache_root,
    )
    _require_publisher(publisher_scheme_id)
    normalized_expires_at = _require_future_expiry(expires_at)
    normalized_token_hash = _require_sha256(
        authorization_token_sha256,
        "authorization_token_sha256",
    )
    _ensure_private_directory(
        normalized_cache_root.parent,
        "signal-gap cache namespace",
    )
    _ensure_private_directory(
        normalized_cache_root,
        "signal-gap cache root",
    )
    nonce = secrets.token_urlsafe(24)
    capability = secrets.token_urlsafe(32)
    path = normalized_cache_root / f"{_PERMIT_PREFIX}{nonce}{_PERMIT_SUFFIX}"
    payload = {
        "schema_version": PREWARM_PERMIT_SCHEMA_VERSION,
        "nonce": nonce,
        "publisher_scheme_id": publisher_scheme_id,
        "artifact_root": str(normalized_artifact_root),
        "cache_root": str(normalized_cache_root),
        "native_generation": normalized_native,
        "expires_at": normalized_expires_at,
        "authorization_token_sha256": normalized_token_hash,
        "capability_sha256": _sha256_text(capability),
    }
    _write_private_new_json(path, payload)
    return SignalGapCachePrewarmPermit(
        path=path,
        capability=capability,
        expires_at=normalized_expires_at,
    )


def consume_signal_gap_cache_prewarm_permit(
    *,
    cache_root: str | Path,
    cache_consumer_id: str,
    native_generation: Mapping[str, object] | None,
) -> dict[str, object]:
    """在真正发布 cache 前校验并原子消费预热 permit。"""
    _require_publisher(cache_consumer_id)
    normalized_native = _normalize_native_generation(native_generation)
    artifact_root = _artifact_root_from_native_environment()
    expected_cache_root = _expected_cache_root(
        artifact_root,
        generation_id=str(normalized_native["generation_id"]),
    )
    normalized_cache_root = _require_exact_cache_root(
        cache_root,
        expected_cache_root,
    )
    _require_private_directory(
        normalized_cache_root.parent,
        "signal-gap cache namespace",
    )
    _require_private_directory(
        normalized_cache_root,
        "signal-gap cache root",
    )
    permit_path, nonce = _permit_path_from_environment(
        normalized_cache_root,
    )
    capability = os.environ.get(PREWARM_CAPABILITY_ENV)
    if not isinstance(capability, str) or not capability:
        raise RuntimeError("signal-gap cache prewarm capability is missing")
    payload = _read_private_permit(permit_path)
    expected = {
        "schema_version": PREWARM_PERMIT_SCHEMA_VERSION,
        "nonce": nonce,
        "publisher_scheme_id": SIGNAL_GAP_CACHE_PREWARM_PUBLISHER_SCHEME_ID,
        "artifact_root": str(artifact_root),
        "cache_root": str(normalized_cache_root),
        "native_generation": normalized_native,
    }
    if any(payload.get(field) != value for field, value in expected.items()):
        raise RuntimeError("signal-gap cache prewarm permit binding mismatch")
    _require_sha256(
        payload.get("authorization_token_sha256"),
        "authorization_token_sha256",
    )
    if payload.get("capability_sha256") != _sha256_text(capability):
        raise RuntimeError("signal-gap cache prewarm capability mismatch")
    _require_future_expiry(payload.get("expires_at"))
    _consume_permit(permit_path)
    return {
        "permit_path": str(permit_path),
        "authorization_token_sha256": payload[
            "authorization_token_sha256"
        ],
        "expires_at": payload["expires_at"],
    }


def _artifact_root_from_native_environment() -> Path:
    configured = os.environ.get(NATIVE_MANIFEST_PATH_ENV)
    if not isinstance(configured, str) or not configured:
        raise RuntimeError("signal-gap cache prewarm manifest is missing")
    manifest_path = Path(configured)
    if (
        not manifest_path.is_absolute()
        or manifest_path.name != "manifest.json"
    ):
        raise RuntimeError("signal-gap cache prewarm manifest is invalid")
    return _resolve_artifact_root(manifest_path.parent.parent)


def _resolve_artifact_root(value: Path) -> Path:
    root, _identity = resolve_signal_gap_native_storage_root(value)
    return root


def _expected_cache_root(
    artifact_root: Path,
    *,
    generation_id: str,
) -> Path:
    if not re.fullmatch(r"native-[0-9a-f]{24}", generation_id):
        raise ValueError("signal-gap cache prewarm generation_id is invalid")
    return artifact_root / ".phase-a-cache" / generation_id


def _require_exact_cache_root(
    value: str | Path,
    expected: Path,
) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute() or str(candidate) != str(expected):
        raise RuntimeError("signal-gap cache prewarm cache_root is invalid")
    return candidate


def _require_publisher(value: str) -> None:
    if value != SIGNAL_GAP_CACHE_PREWARM_PUBLISHER_SCHEME_ID:
        raise RuntimeError("signal-gap cache prewarm publisher is invalid")


def _normalize_native_generation(
    value: Mapping[str, object] | None,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _NATIVE_BINDING_FIELDS:
        raise RuntimeError("signal-gap cache prewarm Native binding is invalid")
    normalized = {field: value[field] for field in sorted(_NATIVE_BINDING_FIELDS)}
    for field in (
        "generation_id",
        "business_date",
        "feature_date",
        "schema_version",
        "exporter_version",
    ):
        if not isinstance(normalized[field], str) or not normalized[field]:
            raise RuntimeError(
                f"signal-gap cache prewarm Native {field} is invalid"
            )
    for field in ("manifest_sha256", "dataset_content_id"):
        normalized[field] = _require_sha256(normalized[field], field)
    if (
        not re.fullmatch(
            r"native-[0-9a-f]{24}",
            str(normalized["generation_id"]),
        )
        or normalized["exporter_version"]
        != SIGNAL_GAP_NATIVE_EXPORTER_VERSION
    ):
        raise RuntimeError("signal-gap cache prewarm Native binding is invalid")
    return normalized


def _permit_path_from_environment(cache_root: Path) -> tuple[Path, str]:
    configured = os.environ.get(PREWARM_PERMIT_ENV)
    if not isinstance(configured, str) or not configured:
        raise RuntimeError("signal-gap cache prewarm permit is missing")
    path = Path(configured)
    if not path.is_absolute() or path.parent != cache_root:
        raise RuntimeError("signal-gap cache prewarm permit path is invalid")
    match = _PERMIT_PATTERN.fullmatch(path.name)
    if match is None:
        raise RuntimeError("signal-gap cache prewarm permit path is invalid")
    if path.with_name(path.name + ".consumed").exists():
        raise RuntimeError("signal-gap cache prewarm permit was already consumed")
    return path, match.group(1)


def _read_private_permit(path: Path) -> dict[str, object]:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError("signal-gap cache prewarm permit is unavailable") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) != 0o600
    ):
        raise RuntimeError("signal-gap cache prewarm permit is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise RuntimeError("signal-gap cache prewarm permit changed")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    try:
        after = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError("signal-gap cache prewarm permit changed") from exc
    if (
        after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise RuntimeError("signal-gap cache prewarm permit changed")
    try:
        payload = json.loads(
            b"".join(chunks).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("signal-gap cache prewarm permit is invalid") from exc
    if not isinstance(payload, dict) or frozenset(payload) != _PERMIT_FIELDS:
        raise RuntimeError("signal-gap cache prewarm permit schema is invalid")
    return payload


def _consume_permit(path: Path) -> None:
    consumed = path.with_name(path.name + ".consumed")
    try:
        os.link(path, consumed, follow_symlinks=False)
    except (FileExistsError, FileNotFoundError) as exc:
        raise RuntimeError("signal-gap cache prewarm permit was already consumed") from exc
    try:
        os.unlink(path)
    except FileNotFoundError as exc:
        raise RuntimeError("signal-gap cache prewarm permit changed") from exc
    _fsync_directory(path.parent)


def _ensure_private_directory(path: Path, label: str) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    _require_private_directory(path, label)


def _require_private_directory(path: Path, label: str) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RuntimeError(f"{label} must be a private real directory")


def _write_private_new_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError(
                    "signal-gap cache prewarm permit write failed"
                )
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RuntimeError("signal-gap cache prewarm permit is unsafe")
    _fsync_directory(path.parent)


def _require_future_expiry(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("signal-gap cache prewarm permit expiry is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(
            "signal-gap cache prewarm permit expiry is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("signal-gap cache prewarm permit expiry is invalid")
    if datetime.now(timezone.utc) > parsed.astimezone(timezone.utc):
        raise RuntimeError("signal-gap cache prewarm permit expired")
    return value


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise RuntimeError(f"signal-gap cache prewarm {field} is invalid")
    return value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
