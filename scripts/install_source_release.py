#!/usr/bin/env python3
"""校验、预安装并可选原子激活 Bond Factor Lab 源码 release。"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping, Sequence

if __package__:
    from scripts.build_source_release import (
        MANIFEST_SCHEMA_VERSION,
        SOURCE_ROOT_PREFIX,
    )
else:
    _project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_project_root))
    from scripts.build_source_release import (  # type: ignore[no-redef]
        MANIFEST_SCHEMA_VERSION,
        SOURCE_ROOT_PREFIX,
    )
    del sys.path[0]


_GIT_OBJECT = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_RUNTIME_ROOT = re.compile(r"^/[A-Za-z0-9/._-]+$")
_MANIFEST_FIELDS = frozenset(
    {"schema_version", "commit", "root_prefix", "archive"}
)
_ARCHIVE_FIELDS = frozenset({"filename", "sha256"})
_INSTALL_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "commit",
        "archive_sha256",
        "source_tree_sha256",
        "runtime_root",
    }
)
_INSTALL_RECORD_SCHEMA = "bfl-source-release-install-v1"
_GENERATED_RELEASE_FILES = frozenset(
    {".bfl-release.env", ".bfl-release-install.json"}
)
_REQUIRED_SOURCE_PATHS = (
    "AGENTS.md",
    "shared/service_instance.py",
    "backend/main.py",
    "scheduler/executor.py",
    "scripts/run_launchd_release.py",
)


class ReleaseInstallError(RuntimeError):
    """源码 release 校验、预安装或激活失败。"""


@dataclass(frozen=True, slots=True)
class InstalledSourceRelease:
    commit: str
    release_root: Path
    archive_sha256: str
    activated: bool
    previous_commit: str | None


def _release_environment(*, commit: str, runtime_root: Path) -> str:
    native_cache_root = runtime_root / "cache" / "native" / commit
    return (
        f"BFL_RELEASE_COMMIT={commit}\n"
        f"BFL_RUNTIME_ROOT={json.dumps(str(runtime_root))}\n"
        f"NUMBA_CACHE_DIR={json.dumps(str(native_cache_root / 'numba'))}\n"
        f"MPLCONFIGDIR={json.dumps(str(native_cache_root / 'matplotlib'))}\n"
    )


def install_source_release(
    *,
    manifest_path: str | Path,
    archive_path: str | Path,
    deploy_root: str | Path,
    runtime_root: str | Path,
    activate: bool,
    expected_current: str | None,
    expected_archive_sha256: str,
) -> InstalledSourceRelease:
    """预安装或激活已由独立 SHA256 批准的源码 archive。"""
    approved_sha256 = str(expected_archive_sha256).strip().lower()
    if not _SHA256.fullmatch(approved_sha256):
        raise ReleaseInstallError("expected archive SHA-256 is invalid")
    manifest_file = Path(manifest_path).resolve()
    archive_file = Path(archive_path).resolve()
    deploy = _require_absolute_path(deploy_root, "deploy root")
    runtime = _require_absolute_path(runtime_root, "runtime root")
    _validate_runtime_root(runtime)
    normalized_expected = _normalize_expected_current(expected_current)
    manifest = _read_manifest(manifest_file)
    commit = str(manifest["commit"])
    archive = manifest["archive"]
    assert isinstance(archive, Mapping)
    if archive_file.name != archive["filename"]:
        raise ReleaseInstallError("archive filename does not match manifest")
    archive_sha256 = _sha256_file(archive_file)
    if (
        archive_sha256 != approved_sha256
        or archive_sha256 != archive["sha256"]
    ):
        raise ReleaseInstallError(
            "archive SHA-256 does not match approved hash and manifest"
        )
    archive_tree_sha256 = _archive_source_tree_sha256(
        archive_file,
        expected_commit=commit,
    )

    _ensure_secure_directory(deploy, "deploy root", create=True)
    releases_root = deploy / "releases"
    _ensure_secure_directory(releases_root, "releases root", create=True)
    release_root = releases_root / commit

    with _install_lock(deploy):
        observed_current = _current_commit(deploy, releases_root)
        if activate:
            if observed_current != normalized_expected:
                raise ReleaseInstallError(
                    "expected current release does not match installed current"
                )
            if observed_current == commit:
                raise ReleaseInstallError("requested release is already current")
            if not release_root.is_dir() or release_root.is_symlink():
                raise ReleaseInstallError(
                    "activation requires a preinstalled release"
                )
            _ensure_secure_directory(
                runtime,
                "runtime root",
                create=False,
            )
            _ensure_secure_directory(
                runtime / "logs",
                "runtime logs root",
                create=False,
            )
            _validate_existing_release(
                release_root=release_root,
                commit=commit,
                archive_sha256=archive_sha256,
                archive_tree_sha256=archive_tree_sha256,
                runtime_root=runtime,
            )
            _activate_release(
                deploy_root=deploy,
                releases_root=releases_root,
                previous_commit=observed_current,
                next_commit=commit,
                archive_sha256=archive_sha256,
            )
        else:
            _ensure_secure_directory(
                runtime,
                "runtime root",
                create=True,
            )
            _ensure_secure_directory(
                runtime / "logs",
                "runtime logs root",
                create=True,
            )
            if release_root.exists() or release_root.is_symlink():
                _validate_existing_release(
                    release_root=release_root,
                    commit=commit,
                    archive_sha256=archive_sha256,
                    archive_tree_sha256=archive_tree_sha256,
                    runtime_root=runtime,
                )
            else:
                _install_archive(
                    archive_file=archive_file,
                    commit=commit,
                    archive_sha256=archive_sha256,
                    archive_tree_sha256=archive_tree_sha256,
                    releases_root=releases_root,
                    runtime_root=runtime,
                )

    return InstalledSourceRelease(
        commit=commit,
        release_root=release_root,
        archive_sha256=archive_sha256,
        activated=activate,
        previous_commit=observed_current if activate else None,
    )


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseInstallError("release manifest is unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != _MANIFEST_FIELDS:
        raise ReleaseInstallError("release manifest fields are invalid")
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ReleaseInstallError("release manifest schema is invalid")
    if payload.get("root_prefix") != SOURCE_ROOT_PREFIX:
        raise ReleaseInstallError("release root prefix is invalid")
    commit = payload.get("commit")
    if not isinstance(commit, str) or not _GIT_OBJECT.fullmatch(commit):
        raise ReleaseInstallError("release manifest commit is invalid")
    archive = payload.get("archive")
    if not isinstance(archive, dict) or set(archive) != _ARCHIVE_FIELDS:
        raise ReleaseInstallError("release archive manifest is invalid")
    filename = archive.get("filename")
    sha256 = archive.get("sha256")
    if (
        not isinstance(filename, str)
        or Path(filename).name != filename
        or not filename.endswith(".source.tar.gz")
    ):
        raise ReleaseInstallError("release archive filename is invalid")
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise ReleaseInstallError("release archive SHA-256 is invalid")
    return payload


def _install_archive(
    *,
    archive_file: Path,
    commit: str,
    archive_sha256: str,
    archive_tree_sha256: str,
    releases_root: Path,
    runtime_root: Path,
) -> None:
    release_root = releases_root / commit
    staging = Path(
        tempfile.mkdtemp(prefix=f".staging-{commit[:12]}-", dir=releases_root)
    )
    try:
        try:
            with tarfile.open(archive_file, mode="r:gz") as archive:
                members = archive.getmembers()
                _validate_archive_members(members)
                archive.extractall(staging, members=members, filter="data")
        except (OSError, tarfile.TarError) as exc:
            raise ReleaseInstallError("release archive cannot be extracted") from exc
        source_root = staging / SOURCE_ROOT_PREFIX.rstrip("/")
        _validate_required_source(source_root)
        source_tree_sha256 = _source_tree_sha256(source_root)
        if source_tree_sha256 != archive_tree_sha256:
            raise ReleaseInstallError(
                "extracted source integrity differs from approved archive"
            )
        release_environment = _release_environment(
            commit=commit,
            runtime_root=runtime_root,
        )
        (source_root / ".bfl-release.env").write_text(
            release_environment,
            encoding="utf-8",
        )
        install_record = {
            "schema_version": _INSTALL_RECORD_SCHEMA,
            "commit": commit,
            "archive_sha256": archive_sha256,
            "source_tree_sha256": source_tree_sha256,
            "runtime_root": str(runtime_root),
        }
        (source_root / ".bfl-release-install.json").write_text(
            _canonical_json(install_record) + "\n",
            encoding="utf-8",
        )
        os.replace(source_root, release_root)
        _make_tree_read_only(release_root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _validate_existing_release(
    *,
    release_root: Path,
    commit: str,
    archive_sha256: str,
    archive_tree_sha256: str,
    runtime_root: Path,
) -> None:
    _validate_required_source(release_root)
    record_path = release_root / ".bfl-release-install.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseInstallError(
            "existing release integrity record is unreadable"
        ) from exc
    if not isinstance(record, dict) or set(record) != _INSTALL_RECORD_FIELDS:
        raise ReleaseInstallError("existing release integrity record is invalid")
    expected_record = {
        "schema_version": _INSTALL_RECORD_SCHEMA,
        "commit": commit,
        "archive_sha256": archive_sha256,
        "source_tree_sha256": archive_tree_sha256,
        "runtime_root": str(runtime_root),
    }
    if record != expected_record:
        raise ReleaseInstallError("existing release integrity record differs")
    actual_tree_sha256 = _source_tree_sha256(release_root)
    if actual_tree_sha256 != archive_tree_sha256:
        raise ReleaseInstallError("existing release source integrity differs")
    expected_environment = _release_environment(
        commit=commit,
        runtime_root=runtime_root,
    )
    try:
        actual_environment = (
            release_root / ".bfl-release.env"
        ).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReleaseInstallError(
            "existing release environment is unreadable"
        ) from exc
    if actual_environment != expected_environment:
        raise ReleaseInstallError("existing release environment differs")
    _validate_read_only_tree(release_root)


def _validate_required_source(source_root: Path) -> None:
    if not source_root.is_dir() or source_root.is_symlink():
        raise ReleaseInstallError("release archive source root is missing")
    for relative in _REQUIRED_SOURCE_PATHS:
        path = source_root / relative
        if not path.is_file() or path.is_symlink():
            raise ReleaseInstallError(
                f"release archive is missing required source: {relative}"
            )


def _source_tree_sha256(root: Path) -> str:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative in _GENERATED_RELEASE_FILES:
            continue
        if path.is_symlink():
            raise ReleaseInstallError("release source integrity rejects symlinks")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ReleaseInstallError("release source integrity rejects special files")
        mode = stat.S_IMODE(path.stat().st_mode)
        entries.append(
            {
                "path": relative,
                "executable": bool(mode & 0o111),
                "sha256": _sha256_file(path),
            }
        )
    return hashlib.sha256(_canonical_json(entries).encode("utf-8")).hexdigest()


def _archive_source_tree_sha256(
    archive_path: Path,
    *,
    expected_commit: str,
) -> str:
    entries: list[dict[str, object]] = []
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            _validate_archive_members(members)
            if archive.pax_headers.get("comment") != expected_commit:
                raise ReleaseInstallError(
                    "archive commit identity does not match manifest"
                )
            for member in members:
                if not member.isfile():
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise ReleaseInstallError(
                        "release archive file is unreadable"
                    )
                relative = PurePosixPath(member.name).relative_to(
                    SOURCE_ROOT_PREFIX.rstrip("/")
                )
                entries.append(
                    {
                        "path": relative.as_posix(),
                        "executable": bool(member.mode & 0o111),
                        "sha256": hashlib.sha256(source.read()).hexdigest(),
                    }
                )
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseInstallError("release archive cannot be inspected") from exc
    return hashlib.sha256(_canonical_json(entries).encode("utf-8")).hexdigest()


def _validate_archive_members(members: Sequence[tarfile.TarInfo]) -> None:
    if not members:
        raise ReleaseInstallError("release archive is empty")
    root_seen = False
    for member in members:
        name = PurePosixPath(member.name)
        if (
            name.is_absolute()
            or not name.parts
            or name.parts[0] != SOURCE_ROOT_PREFIX.rstrip("/")
            or any(part in {"", ".."} for part in name.parts)
            or member.issym()
            or member.islnk()
            or not (member.isdir() or member.isfile())
        ):
            raise ReleaseInstallError(f"unsafe archive member: {member.name}")
        if name.parts == (SOURCE_ROOT_PREFIX.rstrip("/"),):
            if not member.isdir():
                raise ReleaseInstallError("unsafe archive source root")
            root_seen = True
    if not root_seen:
        raise ReleaseInstallError("release archive source root is missing")


def _make_tree_read_only(root: Path) -> None:
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            path = current / filename
            mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(0o555 if mode & 0o111 else 0o444)
        for name in directory_names:
            (current / name).chmod(0o555)
    root.chmod(0o555)


def _validate_read_only_tree(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            raise ReleaseInstallError("existing release integrity rejects symlinks")
        if stat.S_IMODE(path.stat().st_mode) & 0o222:
            raise ReleaseInstallError("existing release is writable")


def _activate_release(
    *,
    deploy_root: Path,
    releases_root: Path,
    previous_commit: str | None,
    next_commit: str,
    archive_sha256: str,
) -> None:
    current_temporary = _stage_symlink(
        deploy_root / "current",
        Path("releases") / next_commit,
    )
    previous_temporary: Path | None = None
    try:
        if previous_commit is not None:
            previous_release = releases_root / previous_commit
            if not previous_release.is_dir():
                raise ReleaseInstallError("current release directory is missing")
            previous_temporary = _stage_symlink(
                deploy_root / "previous",
                Path("releases") / previous_commit,
            )
        elif (deploy_root / "previous").exists() or (
            deploy_root / "previous"
        ).is_symlink():
            raise ReleaseInstallError("previous link exists without current")
        _write_activation_intent(
            deploy_root=deploy_root,
            previous_commit=previous_commit,
            next_commit=next_commit,
            archive_sha256=archive_sha256,
        )
        if previous_temporary is not None:
            os.replace(previous_temporary, deploy_root / "previous")
        os.replace(current_temporary, deploy_root / "current")
    except BaseException:
        current_temporary.unlink(missing_ok=True)
        if previous_temporary is not None:
            previous_temporary.unlink(missing_ok=True)
        raise


def _stage_symlink(path: Path, target: Path) -> Path:
    if path.exists() and not path.is_symlink():
        raise ReleaseInstallError(f"release pointer is not a symlink: {path.name}")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    temporary.symlink_to(target)
    return temporary


def _write_activation_intent(
    *,
    deploy_root: Path,
    previous_commit: str | None,
    next_commit: str,
    archive_sha256: str,
) -> None:
    payload = {
        "schema_version": "bfl-release-revision-v1",
        "phase": "activation_intent",
        "previous_commit": previous_commit,
        "next_commit": next_commit,
        "archive_sha256": archive_sha256,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    path = deploy_root / "revisions.log"
    with path.open("a", encoding="utf-8") as stream:
        path.chmod(0o600)
        stream.write(_canonical_json(payload) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _current_commit(deploy_root: Path, releases_root: Path) -> str | None:
    current = deploy_root / "current"
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise ReleaseInstallError("current must be a symbolic link")
    resolved = current.resolve(strict=False)
    if resolved.parent != releases_root.resolve() or not _GIT_OBJECT.fullmatch(
        resolved.name
    ):
        raise ReleaseInstallError("current release link is outside releases")
    if not resolved.is_dir():
        raise ReleaseInstallError("current release directory is missing")
    return resolved.name


@contextmanager
def _install_lock(deploy_root: Path) -> Iterator[None]:
    lock_path = deploy_root / ".release-install.lock"
    with lock_path.open("a+") as stream:
        lock_path.chmod(0o600)
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def _ensure_secure_directory(
    path: Path,
    label: str,
    *,
    create: bool,
) -> None:
    if path.is_symlink():
        raise ReleaseInstallError(f"{label} must not be a symlink")
    if not path.exists():
        if not create:
            raise ReleaseInstallError(f"{label} does not exist")
        path.mkdir(parents=True, mode=0o700)
    if not path.is_dir():
        raise ReleaseInstallError(f"{label} must be a directory")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid():
        raise ReleaseInstallError(f"{label} owner is invalid")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise ReleaseInstallError(f"{label} permissions are insecure")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReleaseInstallError("release file is unreadable") from exc
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalize_expected_current(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not _GIT_OBJECT.fullmatch(normalized):
        raise ReleaseInstallError("expected current commit is invalid")
    return normalized


def _require_absolute_path(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ReleaseInstallError(f"{label} must be absolute")
    if path.is_symlink():
        raise ReleaseInstallError(f"{label} must not be a symlink")
    return path.resolve(strict=False)


def _validate_runtime_root(runtime_root: Path) -> None:
    if not _SAFE_RUNTIME_ROOT.fullmatch(str(runtime_root)):
        raise ReleaseInstallError(
            "runtime root contains unsafe/unsupported characters"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--expected-archive-sha256", required=True)
    parser.add_argument("--deploy-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument(
        "--expected-current",
        help="激活前必须匹配的 commit；首次激活使用 none",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.activate and args.expected_current is None:
        parser.error("--activate requires --expected-current")
    expected_current = (
        None if args.expected_current == "none" else args.expected_current
    )
    try:
        installed = install_source_release(
            manifest_path=args.manifest,
            archive_path=args.archive,
            expected_archive_sha256=args.expected_archive_sha256,
            deploy_root=args.deploy_root,
            runtime_root=args.runtime_root,
            activate=args.activate,
            expected_current=expected_current,
        )
    except ReleaseInstallError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "commit": installed.commit,
                "release_root": str(installed.release_root),
                "archive_sha256": installed.archive_sha256,
                "activated": installed.activated,
                "previous_commit": installed.previous_commit,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
