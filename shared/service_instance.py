from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import text

from shared.runtime_paths import DEPLOYMENT_TARGET_ENV


_GIT_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
FINGERPRINT_VERSION = "2"
SERVICE_FINGERPRINT_SECRET_ENV = "BOND_FACTOR_LAB_SERVICE_FINGERPRINT_SECRET"
FALLBACK_FINGERPRINT_SECRET_ENV = "HARNESS_AUTH_SECRET"
RELEASE_COMMIT_ENV = "BFL_RELEASE_COMMIT"


def build_service_instance_identity(
    engine,
    *,
    project_root: str | Path,
    runtime_profile: str,
    instance_nonce: str,
    fingerprint_secret: str,
) -> dict[str, str]:
    """生成由共享密钥认证、且不公开可枚举组成部分的服务实例指纹。"""
    profile = str(runtime_profile).strip()
    nonce = str(instance_nonce).strip()
    secret = str(fingerprint_secret)
    if not profile:
        raise ValueError("service runtime_profile must be non-empty")
    if not nonce:
        raise ValueError("service instance_nonce must be non-empty")
    if not secret:
        raise ValueError("service fingerprint secret must be non-empty")
    database_identity = _effective_database_identity(engine)
    code_commit = resolve_code_commit(Path(project_root).resolve())
    fingerprint_payload = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "database_identity": database_identity,
        "code_commit": code_commit,
        "runtime_profile": profile,
        "instance_nonce": nonce,
    }
    return {
        "fingerprint_version": FINGERPRINT_VERSION,
        "fingerprint": hmac.new(
            secret.encode("utf-8"),
            json.dumps(
                fingerprint_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest(),
    }


def service_fingerprint_secret() -> str | None:
    """返回服务与 formal Harness 共享的指纹密钥，专用密钥优先。"""
    for name in (SERVICE_FINGERPRINT_SECRET_ENV, FALLBACK_FINGERPRINT_SECRET_ENV):
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return None


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


def resolve_code_commit(project_root: str | Path) -> str:
    """返回 release 注入的 commit，开发环境才回退到 Git。"""
    configured = str(os.getenv(RELEASE_COMMIT_ENV) or "").strip().lower()
    if configured:
        if not _GIT_COMMIT.fullmatch(configured):
            raise RuntimeError("BFL_RELEASE_COMMIT is invalid")
        return configured
    if str(os.getenv(DEPLOYMENT_TARGET_ENV) or "").strip():
        raise RuntimeError(
            "production release requires BFL_RELEASE_COMMIT"
        )
    return _git_commit(Path(project_root).resolve())


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
