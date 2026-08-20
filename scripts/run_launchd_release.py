#!/usr/bin/env python3
"""从 Mac3 immutable release 加载身份后执行 launchd 应用入口。"""

from __future__ import annotations

import os
import re
import shlex
import stat
import sys
from pathlib import Path
from typing import Mapping, Sequence


_GIT_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_SAFE_RUNTIME_ROOT = re.compile(r"^/[A-Za-z0-9/._-]+$")
_RELEASE_ENVIRONMENT_KEYS = frozenset(
    {
        "BFL_RELEASE_COMMIT",
        "BFL_RUNTIME_ROOT",
        "NUMBA_CACHE_DIR",
        "MPLCONFIGDIR",
    }
)
_CONTROL_ENVIRONMENT = {
    "BOND_FACTOR_LAB_CONTROL_PLANE": "launchd_one_shot",
    "PYTHONDONTWRITEBYTECODE": "1",
}
_FORBIDDEN_AMBIENT_PYTHON_ENVIRONMENT = frozenset({"PYTHONHOME", "PYTHONPATH"})


class LaunchdReleaseError(RuntimeError):
    """Mac3 launchd release 身份或启动命令不可信。"""


def load_release_environment(release_root: str | Path) -> dict[str, str]:
    """读取并核验安装器生成的精确 release 环境。"""
    root = _validated_release_root(release_root)
    environment_path = root / ".bfl-release.env"
    try:
        details = environment_path.lstat()
    except OSError as exc:
        raise LaunchdReleaseError(
            "release environment must be a trusted read-only regular file"
        ) from exc
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) & 0o222
    ):
        raise LaunchdReleaseError(
            "release environment must be a trusted read-only regular file"
        )

    values: dict[str, str] = {}
    try:
        lines = environment_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LaunchdReleaseError("release environment is unreadable") from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if "=" not in line:
            raise LaunchdReleaseError("release environment has invalid syntax")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in _RELEASE_ENVIRONMENT_KEYS or key in values:
            raise LaunchdReleaseError("release environment keys are invalid")
        try:
            tokens = shlex.split(raw_value, comments=False, posix=True)
        except ValueError as exc:
            raise LaunchdReleaseError(
                "release environment has invalid syntax"
            ) from exc
        if len(tokens) != 1 or not tokens[0]:
            raise LaunchdReleaseError("release environment values are invalid")
        values[key] = tokens[0]
    if set(values) != _RELEASE_ENVIRONMENT_KEYS:
        raise LaunchdReleaseError("release environment keys are invalid")

    commit = values["BFL_RELEASE_COMMIT"].lower()
    if commit != root.name:
        raise LaunchdReleaseError("release commit does not match release root")
    runtime_root = values["BFL_RUNTIME_ROOT"]
    if not _SAFE_RUNTIME_ROOT.fullmatch(runtime_root):
        raise LaunchdReleaseError("BFL_RUNTIME_ROOT is invalid")
    runtime = Path(runtime_root).resolve(strict=False)
    if str(runtime) != runtime_root:
        raise LaunchdReleaseError("BFL_RUNTIME_ROOT is not canonical")
    try:
        runtime_details = runtime.stat()
    except OSError as exc:
        raise LaunchdReleaseError("runtime root is insecure") from exc
    if (
        not stat.S_ISDIR(runtime_details.st_mode)
        or runtime_details.st_uid != os.geteuid()
        or stat.S_IMODE(runtime_details.st_mode) & 0o022
    ):
        raise LaunchdReleaseError("runtime root is insecure")
    native_cache = runtime / "cache" / "native" / commit
    expected_caches = {
        "NUMBA_CACHE_DIR": str(native_cache / "numba"),
        "MPLCONFIGDIR": str(native_cache / "matplotlib"),
    }
    for key, expected in expected_caches.items():
        if values[key] != expected:
            raise LaunchdReleaseError(f"{key} does not match release identity")
    return values


def prepare_exec_environment(
    release_root: str | Path,
    environ: Mapping[str, str],
) -> dict[str, str]:
    """合并可信 release 身份，并拒绝 launchd 外层覆盖。"""
    forbidden = sorted(
        _FORBIDDEN_AMBIENT_PYTHON_ENVIRONMENT.intersection(environ)
    )
    if forbidden:
        raise LaunchdReleaseError(
            "ambient Python environment is forbidden: " + ", ".join(forbidden)
        )
    trusted = {
        **load_release_environment(release_root),
        **_CONTROL_ENVIRONMENT,
    }
    for key, value in trusted.items():
        if key in environ and str(environ[key]) != value:
            raise LaunchdReleaseError(
                f"ambient environment conflicts with trusted release key: {key}"
            )
    merged = {str(key): str(value) for key, value in environ.items()}
    merged.update(trusted)
    return merged


def main(argv: Sequence[str] | None = None) -> int:
    """核验当前 release 后，以原进程身份执行绝对路径命令。"""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] != "--" or len(arguments) < 2:
        print(
            "launchd release configuration error: command must follow --",
            file=sys.stderr,
        )
        return 2
    command = arguments[1:]
    executable = Path(command[0])
    if not executable.is_absolute() or not executable.is_file():
        print(
            "launchd release configuration error: command must be an absolute file",
            file=sys.stderr,
        )
        return 2
    try:
        environment = prepare_exec_environment(Path.cwd(), os.environ)
    except LaunchdReleaseError as exc:
        print(f"launchd release configuration error: {exc}", file=sys.stderr)
        return 2
    os.execvpe(str(executable), command, environment)
    return 0


def _validated_release_root(release_root: str | Path) -> Path:
    try:
        root = Path(release_root).resolve(strict=True)
        details = root.stat()
    except OSError as exc:
        raise LaunchdReleaseError("release root is unavailable") from exc
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) & 0o222
        or root.parent.name != "releases"
        or not _GIT_COMMIT.fullmatch(root.name)
        or (root / ".git").exists()
    ):
        raise LaunchdReleaseError("release root identity is invalid")
    return root


if __name__ == "__main__":
    raise SystemExit(main())
