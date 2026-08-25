from __future__ import annotations

import os
import re


_GIT_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
RELEASE_COMMIT_ENV = "BFL_RELEASE_COMMIT"


def resolve_code_commit() -> str:
    """返回 immutable launcher 注入的 release commit。"""
    configured = str(os.getenv(RELEASE_COMMIT_ENV) or "").strip().lower()
    if configured:
        if not _GIT_COMMIT.fullmatch(configured):
            raise RuntimeError("BFL_RELEASE_COMMIT is invalid")
        return configured
    raise RuntimeError("BFL_RELEASE_COMMIT is required")
