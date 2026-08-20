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
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


MANIFEST_SCHEMA_VERSION = "bfl-source-release-v2"
SOURCE_ROOT_PREFIX = "source/"
_GIT_OBJECT = re.compile(r"^[0-9a-f]{40,64}$")


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
