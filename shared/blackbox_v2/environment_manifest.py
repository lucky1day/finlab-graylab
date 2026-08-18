"""按当前支持平台选择唯一 Blackbox V2 环境 manifest。"""

from __future__ import annotations

import platform
from pathlib import Path


_SUPPORTED_MANIFESTS = {
    ("linux", "x86_64"): (
        "linux-64",
        "environment_manifest.json",
    ),
    ("darwin", "arm64"): (
        "osx-arm64",
        "environment_manifest.osx-arm64.json",
    ),
}


def runtime_environment_platform(
    *,
    system_name: str | None = None,
    machine_name: str | None = None,
) -> str:
    """返回当前运行时唯一受支持的 conda 平台名。"""
    return _runtime_selection(system_name, machine_name)[0]


def environment_manifest_path(
    project_root: str | Path,
    *,
    system_name: str | None = None,
    machine_name: str | None = None,
) -> Path:
    """返回当前支持平台在仓库中的 frozen manifest 路径。"""
    selected = _runtime_selection(system_name, machine_name)
    return Path(project_root) / "deploy" / "blackbox_v2" / selected[1]


def _runtime_selection(
    system_name: str | None,
    machine_name: str | None,
) -> tuple[str, str]:
    key = (
        str(system_name or platform.system()).strip().casefold(),
        str(machine_name or platform.machine()).strip().casefold(),
    )
    selected = _SUPPORTED_MANIFESTS.get(key)
    if selected is None:
        raise RuntimeError(
            "unsupported Blackbox V2 runtime platform: "
            f"system={key[0]!r}, machine={key[1]!r}"
        )
    return selected
