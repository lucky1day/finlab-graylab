from __future__ import annotations

import hashlib
import json
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import text


_GIT_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")


def build_service_instance_identity(
    engine,
    *,
    project_root: str | Path,
    runtime_profile: str,
    instance_nonce: str,
) -> dict[str, str]:
    """生成不泄露数据库名、凭据或 nonce 原文的服务实例指纹。"""
    profile = str(runtime_profile).strip()
    nonce = str(instance_nonce).strip()
    if not profile:
        raise ValueError("service runtime_profile must be non-empty")
    if not nonce:
        raise ValueError("service instance_nonce must be non-empty")
    database_identity = _effective_database_identity(engine)
    code_commit = _git_commit(Path(project_root).resolve())
    database_hash = _sha256(database_identity)
    nonce_hash = _sha256(nonce)
    fingerprint_payload = {
        "fingerprint_version": "1",
        "database_identity_sha256": database_hash,
        "code_commit": code_commit,
        "runtime_profile": profile,
        "instance_nonce_sha256": nonce_hash,
    }
    return {
        **fingerprint_payload,
        "fingerprint": _sha256(
            json.dumps(
                fingerprint_payload,
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
    }


def _effective_database_identity(engine) -> str:
    with engine.connect() as conn:
        effective_schema = str(
            conn.execute(text("SELECT DATABASE()")).scalar_one() or ""
        ).strip()
    if not effective_schema:
        raise RuntimeError("effective database Schema is empty")
    url = getattr(engine, "url", None)
    if url is None:
        raise RuntimeError("database engine URL is unavailable")
    backend = (
        str(url.get_backend_name())
        if hasattr(url, "get_backend_name")
        else str(getattr(url, "drivername", ""))
    )
    payload: dict[str, Any] = {
        "backend": backend,
        "host": str(getattr(url, "host", "") or ""),
        "port": int(getattr(url, "port", 0) or 0),
        "configured_database": str(getattr(url, "database", "") or ""),
        "effective_database": effective_schema,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


@lru_cache(maxsize=8)
def _git_commit(project_root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    commit = completed.stdout.strip().lower()
    if completed.returncode != 0 or not _GIT_COMMIT.fullmatch(commit):
        raise RuntimeError("service code commit is unavailable")
    return commit


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
