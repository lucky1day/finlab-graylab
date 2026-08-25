from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from shared.service_instance import resolve_code_commit


def test_release_commit_environment_is_authoritative() -> None:
    release_commit = "a" * 40
    with patch.dict(
        os.environ,
        {"BFL_RELEASE_COMMIT": release_commit},
        clear=True,
    ):
        resolved = resolve_code_commit()

    assert resolved == release_commit


def test_invalid_release_commit_is_rejected() -> None:
    with patch.dict(
        os.environ,
        {"BFL_RELEASE_COMMIT": "not-a-commit"},
        clear=True,
    ):
        with pytest.raises(RuntimeError, match="BFL_RELEASE_COMMIT"):
            resolve_code_commit()


def test_missing_release_commit_has_no_git_fallback() -> None:
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="BFL_RELEASE_COMMIT"):
            resolve_code_commit()
