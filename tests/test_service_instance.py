from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from shared.service_instance import resolve_code_commit


def test_release_commit_environment_precedes_git(tmp_path: Path) -> None:
    release_commit = "a" * 40
    with (
        patch.dict(
            os.environ,
            {"BFL_RELEASE_COMMIT": release_commit},
            clear=True,
        ),
        patch("shared.service_instance.subprocess.run") as git_run,
    ):
        resolved = resolve_code_commit(tmp_path)

    assert resolved == release_commit
    git_run.assert_not_called()


def test_invalid_release_commit_is_rejected(tmp_path: Path) -> None:
    with patch.dict(
        os.environ,
        {"BFL_RELEASE_COMMIT": "not-a-commit"},
        clear=True,
    ):
        with pytest.raises(RuntimeError, match="BFL_RELEASE_COMMIT"):
            resolve_code_commit(tmp_path)


def test_production_release_rejects_missing_commit(tmp_path: Path) -> None:
    with (
        patch.dict(
            os.environ,
            {"BFL_DEPLOYMENT_TARGET": "aliyun-gray"},
            clear=True,
        ),
        patch("shared.service_instance.subprocess.run") as git_run,
    ):
        with pytest.raises(RuntimeError, match="BFL_RELEASE_COMMIT"):
            resolve_code_commit(tmp_path)

    git_run.assert_not_called()


def test_development_without_release_environment_uses_git(
    tmp_path: Path,
) -> None:
    git_commit = "b" * 40
    completed = subprocess.CompletedProcess(
        args=["git"],
        returncode=0,
        stdout=f"{git_commit}\n",
        stderr="",
    )
    with (
        patch.dict(os.environ, {}, clear=True),
        patch(
            "shared.service_instance.subprocess.run",
            return_value=completed,
        ),
    ):
        resolved = resolve_code_commit(tmp_path)

    assert resolved == git_commit
