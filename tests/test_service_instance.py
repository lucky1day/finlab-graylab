from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from shared.service_instance import resolve_code_commit


def test_release_commit_environment_is_authoritative(tmp_path: Path) -> None:
    release_commit = "a" * 40
    with patch.dict(
        os.environ,
        {"BFL_RELEASE_COMMIT": release_commit},
        clear=True,
    ):
        resolved = resolve_code_commit(tmp_path)

    assert resolved == release_commit


def test_invalid_release_commit_is_rejected(tmp_path: Path) -> None:
    with patch.dict(
        os.environ,
        {"BFL_RELEASE_COMMIT": "not-a-commit"},
        clear=True,
    ):
        with pytest.raises(RuntimeError, match="BFL_RELEASE_COMMIT"):
            resolve_code_commit(tmp_path)


def test_production_release_rejects_missing_commit(tmp_path: Path) -> None:
    with patch.dict(
        os.environ,
        {"BFL_DEPLOYMENT_TARGET": "aliyun-gray"},
        clear=True,
    ):
        with pytest.raises(RuntimeError, match="BFL_RELEASE_COMMIT"):
            resolve_code_commit(tmp_path)


def test_development_without_release_environment_has_no_git_fallback(
    tmp_path: Path,
) -> None:
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="BFL_RELEASE_COMMIT"):
            resolve_code_commit(tmp_path)
