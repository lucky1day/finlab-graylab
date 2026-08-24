from __future__ import annotations

import os
import re
from pathlib import Path


from shared.runtime_paths import DEPLOYMENT_TARGET_ENV


_GIT_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
RELEASE_COMMIT_ENV = "BFL_RELEASE_COMMIT"


def resolve_code_commit(project_root: str | Path) -> str:
    """返回 immutable launcher 注入的 release commit。"""
    del project_root
    configured = str(os.getenv(RELEASE_COMMIT_ENV) or "").strip().lower()
    if configured:
        if not _GIT_COMMIT.fullmatch(configured):
            raise RuntimeError("BFL_RELEASE_COMMIT is invalid")
        return configured
    target = str(os.getenv(DEPLOYMENT_TARGET_ENV) or "").strip()
    scope = "production release" if target else "runtime"
    raise RuntimeError(f"{scope} requires BFL_RELEASE_COMMIT")
