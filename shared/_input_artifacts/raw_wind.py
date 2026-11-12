"""原始源表旁路产物的就绪标记、准备、失效与读取。

旁路产物（`shared.data_bridge.raw_wind`）与 ready 快照共享 `generation_id`：producer 在 DataBridge 发布并封存
ready 快照之后导出，写入 `ready.json`（generation_id/refresh_date/artifact_id）；使用方只在标记与快照 generation
一致时取用，否则 fail-closed。每次发布开始时先 `invalidate_raw_wind_sidecar`，避免新旧代次错配。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping

from shared._input_artifacts.contract import BLACKBOX_RAW_WIND_ROOT, BLACKBOX_SCHEMA_PATH
from shared._input_artifacts.generation import _generation_snapshot_identity
from shared.blackbox_v2.contracts import INPUT_PROFILE_RAW_WIND_V1
from shared.blackbox_v2.snapshot import BlackboxSnapshot
from shared.data_bridge.raw_wind import (
    RAW_WIND_FILENAMES,
    RawWindArtifact,
    RawWindError,
    export_raw_wind,
    read_raw_wind_manifest,
    verify_raw_wind_artifact,
)

_READY_NAME = "ready.json"
_KEEP_ARTIFACTS = 2


class RawWindUnavailableError(ValueError):
    """声明需要原始源表旁路产物的交付，在本代次没有可用产物。"""


def prepare_raw_wind_sidecar(
    *,
    state: Mapping[str, Any],
    engine: Any,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
    root: str | Path = BLACKBOX_RAW_WIND_ROOT,
) -> RawWindArtifact:
    """由 DataBridge producer 在发布与 ready 快照封存之后，以一致性快照导出旁路产物并标记就绪。"""
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    identity = _generation_snapshot_identity(state, schema_path=Path(schema_path))
    with engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        connection.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        try:
            artifact = export_raw_wind(
                connection,
                base,
                generation_id=str(identity["generation_id"]),
                refresh_date=str(identity["refresh_date"]),
            )
        finally:
            connection.rollback()
    verify_raw_wind_artifact(artifact.path)
    _write_ready(base, artifact)
    _prune(base, keep=_KEEP_ARTIFACTS, current=artifact.artifact_id)
    return artifact


def invalidate_raw_wind_sidecar(*, root: str | Path = BLACKBOX_RAW_WIND_ROOT) -> None:
    """发布开始时撤销就绪标记；产物目录保留给仍在运行的上一代使用方，由准备阶段按保留策略清理。"""
    marker = Path(root) / _READY_NAME
    try:
        marker.chmod(0o644)
    except FileNotFoundError:
        return
    marker.unlink(missing_ok=True)


def get_ready_raw_wind_artifact(
    *,
    snapshot: BlackboxSnapshot,
    root: str | Path = BLACKBOX_RAW_WIND_ROOT,
) -> RawWindArtifact:
    """取与 ready 快照同一 generation 的旁路产物；轻量核对身份与文件大小，完整哈希在复制到运行视图时校验。"""
    base = Path(root)
    try:
        marker = json.loads((base / _READY_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RawWindUnavailableError("raw wind sidecar is not ready") from exc
    if (
        not isinstance(marker, dict)
        or snapshot.generation_id is None
        or marker.get("generation_id") != snapshot.generation_id
        or marker.get("refresh_date") != snapshot.refresh_date
    ):
        raise RawWindUnavailableError("raw wind sidecar does not match the ready snapshot generation")
    artifact_id = str(marker.get("artifact_id") or "")
    path = base / artifact_id
    try:
        manifest = read_raw_wind_manifest(path)
        if {entry.name for entry in path.iterdir()} != {*RAW_WIND_FILENAMES, "raw_wind_manifest.json"}:
            raise RawWindError("raw wind artifact file set is invalid")
        for info in manifest["tables"].values():
            if (path / info["file"]).lstat().st_size != info["bytes"]:
                raise RawWindError(f"raw wind file size drifted: {info['file']}")
    except (OSError, RawWindError) as exc:
        raise RawWindUnavailableError(f"raw wind sidecar is invalid: {exc}") from exc
    if manifest.get("generation_id") != snapshot.generation_id:
        raise RawWindUnavailableError("raw wind artifact does not belong to the ready snapshot generation")
    return RawWindArtifact(artifact_id=artifact_id, path=path, manifest=manifest)


def raw_wind_for_profile(
    snapshot: BlackboxSnapshot,
    input_profile: str,
    *,
    root: str | Path = BLACKBOX_RAW_WIND_ROOT,
) -> RawWindArtifact | None:
    """按交付声明的 profile 取旁路产物：standard 返回 None，raw_wind_v1 必须就绪否则抛 RawWindUnavailableError。"""
    if input_profile != INPUT_PROFILE_RAW_WIND_V1:
        return None
    return get_ready_raw_wind_artifact(snapshot=snapshot, root=root)


def _write_ready(root: Path, artifact: RawWindArtifact) -> None:
    payload = json.dumps(
        {
            "schema_version": "raw-wind-ready-v1",
            "generation_id": artifact.manifest["generation_id"],
            "refresh_date": artifact.manifest["refresh_date"],
            "artifact_id": artifact.artifact_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ready-", dir=root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o444)
        os.replace(temporary, root / _READY_NAME)
    finally:
        temporary.unlink(missing_ok=True)


def _prune(root: Path, *, keep: int, current: str) -> None:
    """只保留最新的 keep 个产物目录（含当前）；删除前把只读目录改回可写。"""
    candidates = sorted(
        (entry for entry in root.iterdir() if entry.is_dir() and entry.name.startswith("raw-wind-")),
        key=lambda entry: entry.stat().st_mtime,
        reverse=True,
    )
    survivors = {current, *(entry.name for entry in candidates[:keep])}
    for entry in candidates:
        if entry.name in survivors:
            continue
        for item in [entry, *entry.rglob("*")]:
            try:
                item.chmod(0o755 if item.is_dir() else 0o644)
            except OSError:
                pass
        shutil.rmtree(entry, ignore_errors=True)
