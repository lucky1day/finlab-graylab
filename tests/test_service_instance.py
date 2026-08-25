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


@pytest.mark.parametrize(
    "environment",
    [{"BFL_RELEASE_COMMIT": "not-a-commit"}, {}],
    ids=("invalid", "missing"),
)
def test_invalid_or_missing_release_commit_is_rejected(
    environment: dict[str, str],
) -> None:
    with patch.dict(os.environ, environment, clear=True):
        with pytest.raises(RuntimeError, match="BFL_RELEASE_COMMIT"):
            resolve_code_commit()
