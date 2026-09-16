#!/usr/bin/env python3
"""从 clean Git HEAD 构建可重复的源码 release。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence


MANIFEST_SCHEMA_VERSION = "bfl-source-release-v2"
SOURCE_ROOT_PREFIX = "source/"
_GIT_OBJECT = re.compile(r"^[0-9a-f]{40,64}$")
_FORBIDDEN_RELEASE_COMPONENTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "backtest_artifacts",
        "build",
        "cache",
        "dist",
        "logs",
        "node_modules",
        "outputs",
        "reports",
        "site-packages",
        "venv",
    }
)
_FORBIDDEN_RELEASE_SUFFIXES = frozenset(
    {
        ".bak",
        ".db",
        ".dump",
        ".key",
        ".log",
        ".orig",
        ".p12",
        ".pem",
        ".pfx",
        ".pyc",
        ".pyo",
        ".sqlite",
        ".sqlite3",
        ".swp",
        ".swo",
        ".temp",
        ".tmp",
    }
)
_FORBIDDEN_PACKAGED_DATA_SUFFIXES = frozenset(
    {
        ".7z",
        ".feather",
        ".gz",
        ".joblib",
        ".npy",
        ".npz",
        ".parquet",
        ".pickle",
        ".pkl",
        ".rar",
        ".so",
        ".tar",
        ".tgz",
        ".zip",
    }
)
_ARCHIVE_SUFFIXES = frozenset({".gz", ".tar", ".tgz", ".zip"})
_INSTALL_TIME_FILENAMES = frozenset(
    {
        ".bfl-release.env",
        "manual-run.env",
        "production_schemes.json",
        "service.env",
    }
)
_EVIDENCE_ALLOWLIST_PATH = PurePosixPath(
    "deploy/source_evidence_release_allowlist_v1.json"
)
_EVIDENCE_ALLOWLIST_SCHEMA = "bfl-source-evidence-release-allowlist-v1"
_FORBIDDEN_RUNTIME_PREFIXES = (
    PurePosixPath("data/data_bridge/current"),
)
_MAX_UNAPPROVED_FILE_BYTES = 2 * 1024 * 1024
_MAX_APPROVED_ARCHIVE_BYTES = 32 * 1024 * 1024
_MAX_APPROVED_ARCHIVE_EXPANDED_BYTES = 64 * 1024 * 1024
_MAX_APPROVED_ARCHIVE_MEMBERS = 4096
_MAX_APPROVED_ARCHIVE_NESTING = 2
_PRIVATE_KEY_PATTERN = re.compile(
    rb"-----BEGIN (?:EC |OPENSSH |PGP |RSA )?PRIVATE KEY-----"
)
_HIGH_CONFIDENCE_TOKEN_PATTERNS = (
    (
        "aws_access_key",
        re.compile(rb"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    ),
    (
        "github_token",
        re.compile(rb"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{30,}"),
    ),
    (
        "slack_token",
        re.compile(rb"(?<![A-Za-z0-9-])xox[baprs]-[A-Za-z0-9-]{20,}"),
    ),
    (
        "high_entropy_password",
        re.compile(
            rb"(?im)^\s*(?:BOND_DB_PASSWORD|DATABASE_PASSWORD|DB_PASSWORD)\s*=\s*"
            rb"['\"]?([A-Za-z0-9+/=_-]{24,})"
        ),
    ),
)


class ReleaseBuildError(RuntimeError):
    """源码 release 无法安全、可重复地构建。"""


@dataclass(frozen=True, slots=True)
class BuiltSourceRelease:
    commit: str
    archive_path: Path
    manifest_path: Path
    archive_sha256: str


def build_source_release(
    project_root: str | Path,
    output_dir: str | Path,
) -> BuiltSourceRelease:
    """构建当前 clean HEAD 的 deterministic tar.gz 与 manifest。"""
    root = Path(project_root).resolve()
    output = Path(output_dir).resolve()
    if _git(root, "rev-parse", "--show-toplevel") != str(root):
        raise ReleaseBuildError("project root must be the Git worktree root")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ReleaseBuildError("source release requires a clean repository")

    commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}").lower()
    if not _GIT_OBJECT.fullmatch(commit):
        raise ReleaseBuildError("Git commit identity is invalid")

    tar_bytes = _git_bytes(
        root,
        "archive",
        "--format=tar",
        f"--prefix={SOURCE_ROOT_PREFIX}",
        commit,
    )
    validate_release_contents(tar_bytes)
    compressed_buffer = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=compressed_buffer,
        compresslevel=9,
        mtime=0,
    ) as compressed:
        compressed.write(tar_bytes)
    archive_bytes = compressed_buffer.getvalue()
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()

    archive_name = f"{commit}.source.tar.gz"
    manifest_name = f"{commit}.manifest.json"
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "commit": commit,
        "root_prefix": SOURCE_ROOT_PREFIX,
        "archive": {
            "filename": archive_name,
            "sha256": archive_sha256,
        },
    }
    manifest_bytes = (
        json.dumps(
            manifest,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")

    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / archive_name
    manifest_path = output / manifest_name
    _write_reproducible_output(archive_path, archive_bytes)
    _write_reproducible_output(manifest_path, manifest_bytes)
    return BuiltSourceRelease(
        commit=commit,
        archive_path=archive_path,
        manifest_path=manifest_path,
        archive_sha256=archive_sha256,
    )


def validate_release_contents(tar_bytes: bytes) -> None:
    """校验即将压缩的 Git archive 成员、大小与高置信凭据特征。"""
    try:
        archive = tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:")
    except tarfile.TarError as exc:
        raise ReleaseBuildError("Git archive is not a readable tar stream") from exc
    with archive:
        members: dict[PurePosixPath, bytes] = {}
        for member in archive.getmembers():
            relative = _release_member_path(member.name)
            if relative is None:
                if not member.isdir():
                    raise ReleaseBuildError(
                        "release source root must be a directory"
                    )
                continue
            if member.isdir():
                continue
            if not member.isfile():
                raise ReleaseBuildError(
                    f"release contains unsupported member type: {relative.as_posix()}"
                )
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ReleaseBuildError(
                    f"release member cannot be read: {relative.as_posix()}"
                )
            payload = extracted.read()
            if len(payload) != member.size:
                raise ReleaseBuildError(
                    f"release member size mismatch: {relative.as_posix()}"
                )
            if relative in members:
                raise ReleaseBuildError(
                    f"release contains duplicate member: {relative.as_posix()}"
                )
            members[relative] = payload

        approvals = _load_evidence_allowlist(members)
        _validate_evidence_approvals(members, approvals)
        for relative, payload in members.items():
            approval = _matching_evidence_approval(relative, approvals)
            _validate_release_path(
                relative,
                size=len(payload),
                evidence_approval=approval,
            )
            _scan_release_member(relative, payload)
            if approval is not None and approval["type"] == "archive":
                _scan_approved_archive(relative, payload)


def _release_member_path(name: str) -> PurePosixPath | None:
    prefix = SOURCE_ROOT_PREFIX.rstrip("/")
    path = PurePosixPath(name)
    if path == PurePosixPath(prefix):
        return None
    if not path.parts or path.parts[0] != prefix:
        raise ReleaseBuildError("release archive member is outside the source root")
    relative = PurePosixPath(*path.parts[1:])
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ReleaseBuildError("release archive member path is invalid")
    return relative


def _validate_release_path(
    path: PurePosixPath,
    *,
    size: int,
    evidence_approval: Mapping[str, str] | None,
) -> None:
    path_text = path.as_posix()
    lowered_parts = tuple(part.lower() for part in path.parts)
    filename = path.name.lower()
    suffix = path.suffix.lower()
    approved_evidence = evidence_approval is not None
    if any(
        path == prefix or path.is_relative_to(prefix)
        for prefix in _FORBIDDEN_RUNTIME_PREFIXES
    ):
        raise ReleaseBuildError(f"release contains runtime state: {path_text}")
    if any(part in _FORBIDDEN_RELEASE_COMPONENTS for part in lowered_parts):
        raise ReleaseBuildError(f"release contains forbidden path: {path_text}")
    if filename == ".env.example":
        if path != PurePosixPath(".env.example"):
            raise ReleaseBuildError(f"release contains forbidden env file: {path_text}")
    elif filename == ".env" or filename.startswith(".env."):
        raise ReleaseBuildError(f"release contains forbidden env file: {path_text}")
    if filename in _INSTALL_TIME_FILENAMES:
        raise ReleaseBuildError(
            f"release contains install-time configuration: {path_text}"
        )
    if filename.endswith("~") or suffix in _FORBIDDEN_RELEASE_SUFFIXES:
        raise ReleaseBuildError(f"release contains forbidden file type: {path_text}")
    if (
        evidence_approval is not None
        and evidence_approval["type"] == "tree"
        and suffix in {".7z", ".gz", ".rar", ".tar", ".tgz", ".zip"}
    ):
        raise ReleaseBuildError(
            f"source evidence archive requires exact approval: {path_text}"
        )
    if (
        suffix == ".sql"
        and path.parts[:1] != ("migrations",)
        and not approved_evidence
    ):
        raise ReleaseBuildError(
            f"release contains unapproved SQL or database export: {path_text}"
        )
    if suffix in _FORBIDDEN_PACKAGED_DATA_SUFFIXES and not approved_evidence:
        raise ReleaseBuildError(
            f"release contains unapproved binary or data package: {path_text}"
        )
    if size > _MAX_UNAPPROVED_FILE_BYTES and not approved_evidence:
        raise ReleaseBuildError(f"release contains unapproved large file: {path_text}")


def _load_evidence_allowlist(
    members: Mapping[PurePosixPath, bytes],
) -> tuple[dict[str, str], ...]:
    payload = members.get(_EVIDENCE_ALLOWLIST_PATH)
    if payload is None:
        raise ReleaseBuildError("release source evidence allowlist is missing")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError("release source evidence allowlist is invalid") from exc
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "entries",
    }:
        raise ReleaseBuildError("release source evidence allowlist schema is invalid")
    if document.get("schema_version") != _EVIDENCE_ALLOWLIST_SCHEMA:
        raise ReleaseBuildError("release source evidence allowlist version is invalid")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ReleaseBuildError("release source evidence allowlist entries are invalid")
    normalized: list[dict[str, str]] = []
    seen: set[PurePosixPath] = set()
    for raw in entries:
        if not isinstance(raw, dict) or set(raw) != {
            "path",
            "type",
            "sha256",
            "reason",
        }:
            raise ReleaseBuildError("release source evidence entry schema is invalid")
        path_text = raw.get("path")
        entry_type = raw.get("type")
        digest = raw.get("sha256")
        reason = raw.get("reason")
        if not isinstance(path_text, str):
            raise ReleaseBuildError("release source evidence path is invalid")
        path = PurePosixPath(path_text)
        if (
            not path.parts
            or path.is_absolute()
            or path.as_posix() != path_text
            or path.parts[0] != "source_evidence"
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ReleaseBuildError("release source evidence path is invalid")
        if path in seen:
            raise ReleaseBuildError("release source evidence path is duplicated")
        if entry_type not in {"tree", "archive"}:
            raise ReleaseBuildError("release source evidence type is invalid")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ReleaseBuildError("release source evidence SHA-256 is invalid")
        if not isinstance(reason, str) or not reason.strip() or reason != reason.strip():
            raise ReleaseBuildError("release source evidence reason is invalid")
        seen.add(path)
        normalized.append(
            {
                "path": path.as_posix(),
                "type": entry_type,
                "sha256": digest,
                "reason": reason,
            }
        )
    for first_index, first in enumerate(normalized):
        first_path = PurePosixPath(first["path"])
        for second in normalized[first_index + 1 :]:
            second_path = PurePosixPath(second["path"])
            if first_path.is_relative_to(second_path) or second_path.is_relative_to(first_path):
                raise ReleaseBuildError("release source evidence paths overlap")
    return tuple(normalized)


def _matching_evidence_approval(
    path: PurePosixPath,
    approvals: Sequence[Mapping[str, str]],
) -> Mapping[str, str] | None:
    for approval in approvals:
        approved_path = PurePosixPath(approval["path"])
        if approval["type"] == "tree" and path.is_relative_to(approved_path):
            return approval
        if approval["type"] == "archive" and path == approved_path:
            return approval
    return None


def _validate_evidence_approvals(
    members: Mapping[PurePosixPath, bytes],
    approvals: Sequence[Mapping[str, str]],
) -> None:
    for approval in approvals:
        root = PurePosixPath(approval["path"])
        if approval["type"] == "archive":
            payload = members.get(root)
            if payload is None:
                raise ReleaseBuildError(
                    f"approved source evidence archive is missing: {root}"
                )
            actual = hashlib.sha256(payload).hexdigest()
        else:
            descendants = sorted(
                path for path in members if path.is_relative_to(root)
            )
            if not descendants:
                raise ReleaseBuildError(
                    f"approved source evidence tree is missing: {root}"
                )
            digest = hashlib.sha256()
            for path in descendants:
                relative = path.relative_to(root).as_posix()
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(members[path])
                digest.update(b"\0")
            actual = digest.hexdigest()
        if actual != approval["sha256"]:
            raise ReleaseBuildError(
                f"approved source evidence digest differs: {root.as_posix()}"
            )


def _scan_approved_archive(path: PurePosixPath, payload: bytes) -> None:
    if len(payload) > _MAX_APPROVED_ARCHIVE_BYTES:
        raise ReleaseBuildError(
            f"approved source archive exceeds compressed limit: {path}"
        )
    state = {"members": 0, "expanded": 0}
    _scan_archive_payload(path, payload, depth=0, state=state)


def _scan_archive_payload(
    path: PurePosixPath,
    payload: bytes,
    *,
    depth: int,
    state: dict[str, int],
) -> None:
    stream = io.BytesIO(payload)
    if zipfile.is_zipfile(stream):
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                mode = (member.external_attr >> 16) & 0o170000
                if member.flag_bits & 0x1:
                    raise ReleaseBuildError(f"approved source archive is encrypted: {path}")
                if mode == 0o120000:
                    raise ReleaseBuildError(f"approved source archive contains link: {path}")
                inner = _safe_archive_member_path(member.filename, path)
                _reserve_archive_member(state, member.file_size, path / inner)
                with archive.open(member) as source:
                    child = source.read(member.file_size + 1)
                if len(child) != member.file_size:
                    raise ReleaseBuildError(
                        f"approved source archive member size mismatch: {path / inner}"
                    )
                _scan_archive_child(path / inner, child, depth=depth, state=state)
        return
    stream.seek(0)
    try:
        archive = tarfile.open(fileobj=stream, mode="r:*")
    except tarfile.TarError as exc:
        if path.suffix.lower() == ".gz":
            _scan_gzip_payload(path, payload, depth=depth, state=state)
            return
        raise ReleaseBuildError(
            f"approved source archive format is unsupported: {path}"
        ) from exc
    with archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                raise ReleaseBuildError(
                    f"approved source archive contains unsupported member: {path}"
                )
            inner = _safe_archive_member_path(member.name, path)
            _reserve_archive_member(state, member.size, path / inner)
            source = archive.extractfile(member)
            if source is None:
                raise ReleaseBuildError(
                    f"approved source archive member cannot be read: {path / inner}"
                )
            child = source.read(member.size + 1)
            if len(child) != member.size:
                raise ReleaseBuildError(
                    f"approved source archive member size mismatch: {path / inner}"
                )
            _scan_archive_child(path / inner, child, depth=depth, state=state)


def _scan_gzip_payload(
    path: PurePosixPath,
    payload: bytes,
    *,
    depth: int,
    state: dict[str, int],
) -> None:
    remaining = _MAX_APPROVED_ARCHIVE_EXPANDED_BYTES - state["expanded"]
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as source:
            child = source.read(remaining + 1)
    except (EOFError, OSError) as exc:
        raise ReleaseBuildError(
            f"approved source gzip is invalid: {path}"
        ) from exc
    inner = PurePosixPath(path.stem or "payload")
    _reserve_archive_member(state, len(child), path / inner)
    _scan_archive_child(path / inner, child, depth=depth, state=state)


def _safe_archive_member_path(name: str, archive_path: PurePosixPath) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not path.parts
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ReleaseBuildError(
            f"approved source archive contains unsafe path: {archive_path}"
        )
    return path


def _reserve_archive_member(
    state: dict[str, int],
    size: int,
    path: PurePosixPath,
) -> None:
    state["members"] += 1
    state["expanded"] += size
    if state["members"] > _MAX_APPROVED_ARCHIVE_MEMBERS:
        raise ReleaseBuildError(
            f"approved source archive exceeds member limit: {path}"
        )
    if state["expanded"] > _MAX_APPROVED_ARCHIVE_EXPANDED_BYTES:
        raise ReleaseBuildError(
            f"approved source archive exceeds expanded limit: {path}"
        )


def _scan_archive_child(
    path: PurePosixPath,
    payload: bytes,
    *,
    depth: int,
    state: dict[str, int],
) -> None:
    _scan_release_member(path, payload)
    if not _looks_like_supported_archive(path, payload):
        return
    if depth >= _MAX_APPROVED_ARCHIVE_NESTING:
        raise ReleaseBuildError(
            f"approved source archive exceeds nesting limit: {path}"
        )
    _scan_archive_payload(path, payload, depth=depth + 1, state=state)


def _looks_like_supported_archive(path: PurePosixPath, payload: bytes) -> bool:
    if path.suffix.lower() in _ARCHIVE_SUFFIXES:
        return True
    stream = io.BytesIO(payload)
    if zipfile.is_zipfile(stream):
        return True
    stream.seek(0)
    try:
        with tarfile.open(fileobj=stream, mode="r:*"):
            return True
    except tarfile.TarError:
        return False


def _scan_release_member(path: PurePosixPath, payload: bytes) -> None:
    path_text = path.as_posix()
    if _PRIVATE_KEY_PATTERN.search(payload):
        raise ReleaseBuildError(
            f"release credential scan rejected private key material: {path_text}"
        )
    for category, pattern in _HIGH_CONFIDENCE_TOKEN_PATTERNS:
        if pattern.search(payload):
            raise ReleaseBuildError(
                f"release credential scan rejected {category}: {path_text}"
            )


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "Git command failed"
        raise ReleaseBuildError(detail)
    return completed.stdout.strip()


def _git_bytes(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseBuildError(detail or "Git archive failed")
    return completed.stdout


def _write_reproducible_output(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.is_file() and path.read_bytes() == payload:
            return
        raise ReleaseBuildError(f"release output already differs: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--output-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        built = build_source_release(args.project_root, args.output_dir)
    except ReleaseBuildError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "commit": built.commit,
                "archive": str(built.archive_path),
                "manifest": str(built.manifest_path),
                "archive_sha256": built.archive_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
