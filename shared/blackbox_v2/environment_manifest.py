"""按当前支持平台选择唯一 Blackbox V2 环境 manifest。"""

from __future__ import annotations

import hashlib
import json
import platform
import re
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


def load_environment_fingerprint(
    project_root: str | Path,
    *,
    expected_runtime_profile: str,
) -> str:
    """读取当前平台的环境 manifest，并校验其不可变指纹。"""
    expected_platform = runtime_environment_platform()
    path = environment_manifest_path(project_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"invalid Blackbox V2 environment manifest {path}: {exc}"
        ) from exc
    fingerprint = (
        raw.get("environment_fingerprint") if isinstance(raw, dict) else None
    )
    if not isinstance(fingerprint, str) or not re.fullmatch(
        r"[0-9a-f]{64}", fingerprint
    ):
        raise ValueError(
            "environment manifest must contain a SHA-256 "
            "environment_fingerprint"
        )
    payload = raw.get("explicit_packages")
    if not isinstance(payload, list):
        raise ValueError("environment manifest must contain explicit_packages")
    if raw.get("runtime_profile") != expected_runtime_profile:
        raise ValueError(
            "environment manifest runtime_profile must match frozen "
            "Blackbox runtime profile"
        )
    if raw.get("platform") != expected_platform:
        raise ValueError(
            "environment manifest platform must match runtime platform"
        )
    computed = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if computed != fingerprint:
        raise ValueError(
            "environment manifest fingerprint does not match explicit_packages"
        )
    return fingerprint


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
