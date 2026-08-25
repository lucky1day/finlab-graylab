"""方案生命周期状态的主机级存放。

`status` / `version_status` 回答「这个方案此刻在**这台主机**上是否活跃」，是主机级运行状态，
生命周期是这台主机；而 `config.yaml` 位于 immutable release 内、参与 `source_tree_sha256`，
其生命周期是一次发布。两者生命周期不同，不共用同一个存储。

平台其实已经画出这条界线——`shared/blackbox_v2/versioning.canonical_platform_config` 刻意把
`status` 与 `version_status` 排除在版本计算之外，因此激活不改变 `scheme_version`。本模块只是把
这两个已被排除在身份之外的字段搬到与其生命周期匹配的位置。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from shared.runtime_paths import resolve_runtime_state_path
from shared.scheme_config_schema import ALLOWED_STATUS, ALLOWED_VERSION_STATUS


LIFECYCLE_RELATIVE_ROOT = "lifecycle"
SCHEMA_VERSION = "scheme-lifecycle-state-v1"
_REQUIRED_FIELDS = (
    "schema_version",
    "scheme_id",
    "scheme_version",
    "status",
    "version_status",
)
@dataclass(frozen=True)
class LifecycleStateRecord:
    """某方案在本机的生效生命周期状态。"""

    scheme_id: str
    scheme_version: str
    status: str
    version_status: str


def lifecycle_state_path(project_root: Path | str, scheme_id: str) -> Path:
    """返回该方案在本机的生命周期状态文件路径。"""
    safe_id = str(scheme_id).strip()
    if not safe_id or "/" in safe_id or safe_id.startswith("."):
        raise ValueError(f"invalid scheme_id for lifecycle state: {scheme_id!r}")
    root = resolve_runtime_state_path(
        relative_path=LIFECYCLE_RELATIVE_ROOT,
        development_default=(
            Path(project_root)
            / "backtest_artifacts"
            / LIFECYCLE_RELATIVE_ROOT
        ),
    )
    # 只规范化受控根目录，不 resolve 最终文件名；否则一个已存在的 symlink
    # 会把 no-clobber 检查和写入目标悄悄改到链接指向的位置。
    return root / f"{safe_id}.json"


def read_lifecycle_state(
    project_root: Path | str,
    scheme_id: str,
    scheme_version: str,
) -> LifecycleStateRecord | None:
    """读取生效状态；不存在或与当前精确版本不匹配一律返回 None。

    返回 None 表示调用方应回落到 `config.yaml` 中的初始声明。任何不可读、字段缺失、
    取值非法或 `scheme_version` 不匹配都返回 None——代码一变，旧状态自动失效，方案必须重新激活。
    """
    try:
        path = lifecycle_state_path(project_root, scheme_id)
    except (ValueError, RuntimeError):
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if any(
        not isinstance(payload.get(field), str) or not payload[field].strip()
        for field in _REQUIRED_FIELDS
    ):
        return None
    if payload["schema_version"] != SCHEMA_VERSION:
        return None
    if payload["scheme_id"] != str(scheme_id):
        return None
    if payload["scheme_version"] != str(scheme_version):
        return None
    if payload["status"] not in ALLOWED_STATUS:
        return None
    if payload["version_status"] not in ALLOWED_VERSION_STATUS:
        return None
    return LifecycleStateRecord(
        scheme_id=payload["scheme_id"],
        scheme_version=payload["scheme_version"],
        status=payload["status"],
        version_status=payload["version_status"],
    )


def write_lifecycle_state(
    project_root: Path | str,
    *,
    scheme_id: str,
    scheme_version: str,
    status: str,
    version_status: str,
    harness_run_id: str | None = None,
) -> Path:
    """原子写入本机生命周期状态。取值非法一律拒绝。"""
    path = lifecycle_state_path(project_root, scheme_id)
    payload = _lifecycle_payload(
        scheme_id=scheme_id,
        scheme_version=scheme_version,
        status=status,
        version_status=version_status,
        harness_run_id=harness_run_id,
    )
    _atomic_write_json(path, payload)
    return path


def _lifecycle_payload(
    *,
    scheme_id: str,
    scheme_version: str,
    status: str,
    version_status: str,
    harness_run_id: str | None,
) -> dict[str, str]:
    if status not in ALLOWED_STATUS:
        raise ValueError(f"unsupported lifecycle status: {status!r}")
    if version_status not in ALLOWED_VERSION_STATUS:
        raise ValueError(f"unsupported lifecycle version_status: {version_status!r}")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "scheme_id": str(scheme_id),
        "scheme_version": str(scheme_version),
        "status": status,
        "version_status": version_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if harness_run_id:
        payload["harness_run_id"] = str(harness_run_id)
    return payload


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
