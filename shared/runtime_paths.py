"""与具体调度控制面无关的服务运行期路径。"""

from __future__ import annotations

import os
import pwd
from pathlib import Path


RUNTIME_ARTIFACT_RELATIVE_PATH = (
    "Library",
    "Application Support",
    "BondFactorLab",
    "daily-runtime-v1",
)


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
