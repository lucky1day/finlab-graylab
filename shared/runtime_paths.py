"""与具体调度控制面无关的服务运行期路径。"""

from __future__ import annotations

import os
import pwd
from pathlib import Path
from typing import Mapping


RUNTIME_ARTIFACT_RELATIVE_PATH = (
    "Library",
    "Application Support",
    "BondFactorLab",
    "daily-runtime-v1",
)
RUNTIME_ROOT_ENV = "BFL_RUNTIME_ROOT"
DEPLOYMENT_TARGET_ENV = "BFL_DEPLOYMENT_TARGET"


def resolve_runtime_state_path(
    *,
    relative_path: str | Path,
    development_default: str | Path,
    override_env: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """解析跨源码 release 保留的运行状态路径。"""
    environment = os.environ if environ is None else environ
    if override_env is not None:
        configured_override = str(
            environment.get(override_env) or ""
        ).strip()
        if configured_override:
            return _absolute_runtime_path(
                configured_override,
                override_env,
            )

    configured_root = str(
        environment.get(RUNTIME_ROOT_ENV) or ""
    ).strip()
    if configured_root:
        root = _absolute_runtime_path(
            configured_root,
            RUNTIME_ROOT_ENV,
        )
        relative = _safe_relative_runtime_path(relative_path)
        return (root / relative).resolve(strict=False)

    if str(environment.get(DEPLOYMENT_TARGET_ENV) or "").strip():
        raise RuntimeError(
            "production runtime state requires BFL_RUNTIME_ROOT "
            "or an explicit state path"
        )
    return _absolute_runtime_path(
        development_default,
        "development runtime default",
    )


def _absolute_runtime_path(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return path.resolve(strict=False)


def _safe_relative_runtime_path(value: str | Path) -> Path:
    raw = str(value).strip()
    path = Path(raw)
    if (
        not raw
        or path.is_absolute()
        or not path.parts
        or any(part == ".." for part in path.parts)
    ):
        raise ValueError("runtime state relative path is invalid")
    return path


def resolve_runtime_artifact_root(
    *,
    service_uid: int | None = None,
) -> Path:
    """返回按服务账号唯一的 machine-global 运行期根。"""
    uid = os.getuid() if service_uid is None else service_uid
    if (
        not isinstance(uid, int)
        or isinstance(uid, bool)
        or uid < 0
    ):
        raise ValueError("runtime artifact service UID is invalid")
    try:
        account = pwd.getpwuid(uid)
    except (KeyError, OSError) as exc:
        raise RuntimeError(
            f"runtime artifact service UID has no account: {uid}"
        ) from exc
    home = Path(str(account.pw_dir))
    if not home.is_absolute():
        raise RuntimeError("runtime artifact service home must be absolute")
    root = home.joinpath(*RUNTIME_ARTIFACT_RELATIVE_PATH).resolve(
        strict=False
    )
    if not root.is_absolute():
        raise RuntimeError("runtime artifact root must be absolute")
    return root
