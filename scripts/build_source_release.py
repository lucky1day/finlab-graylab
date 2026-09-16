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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence


MANIFEST_SCHEMA_VERSION = "bfl-source-release-v2"
SOURCE_ROOT_PREFIX = "source/"
_GIT_OBJECT = re.compile(r"^[0-9a-f]{40,64}$")
_FORBIDDEN_RELEASE_COMPONENTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "backtest_artifacts",
        "cache",
        "dist",
        "logs",
        "outputs",
        "reports",
    }
)
_FORBIDDEN_RELEASE_SUFFIXES = frozenset(
    {
        ".bak",
        ".db",
        ".dump",
        ".key",
        ".p12",
        ".pem",
        ".pfx",
        ".sqlite",
        ".sqlite3",
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
_INSTALL_TIME_FILENAMES = frozenset(
    {
        ".bfl-release.env",
        "manual-run.env",
        "production_schemes.json",
        "service.env",
    }
)
_APPROVED_EVIDENCE_PREFIX = PurePosixPath("source_evidence")
_FORBIDDEN_RUNTIME_PREFIXES = (
    PurePosixPath("data/data_bridge/current"),
)
_MAX_UNAPPROVED_FILE_BYTES = 2 * 1024 * 1024
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
            _validate_release_path(relative, size=member.size)
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
            _scan_release_member(relative, payload)


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


def _validate_release_path(path: PurePosixPath, *, size: int) -> None:
    path_text = path.as_posix()
    lowered_parts = tuple(part.lower() for part in path.parts)
    filename = path.name.lower()
    suffix = path.suffix.lower()
    in_source_evidence = path == _APPROVED_EVIDENCE_PREFIX or (
        path.parts[:1] == _APPROVED_EVIDENCE_PREFIX.parts
    )
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
    if suffix in _FORBIDDEN_RELEASE_SUFFIXES:
        raise ReleaseBuildError(f"release contains forbidden file type: {path_text}")
    if (
        suffix == ".sql"
        and path.parts[:1] != ("migrations",)
        and not in_source_evidence
    ):
        raise ReleaseBuildError(
            f"release contains unapproved SQL or database export: {path_text}"
        )
    if suffix in _FORBIDDEN_PACKAGED_DATA_SUFFIXES and not in_source_evidence:
        raise ReleaseBuildError(
            f"release contains unapproved binary or data package: {path_text}"
        )
    if size > _MAX_UNAPPROVED_FILE_BYTES and not in_source_evidence:
        raise ReleaseBuildError(f"release contains unapproved large file: {path_text}")


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
