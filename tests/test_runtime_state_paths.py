from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from shared.runtime_paths import resolve_runtime_state_path


def test_explicit_override_precedes_unified_root(tmp_path: Path) -> None:
    override = tmp_path / "bridge"
    resolved = resolve_runtime_state_path(
        relative_path="data-bridge/data",
        development_default=tmp_path / "development",
        override_env="DATABRIDGE_DATA_ROOT",
        environ={
            "BFL_RUNTIME_ROOT": str(tmp_path / "runtime"),
            "DATABRIDGE_DATA_ROOT": str(override),
        },
    )

    assert resolved == override.resolve()


def test_unified_root_resolves_safe_relative_path(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    resolved = resolve_runtime_state_path(
        relative_path="artifacts/runtime-inputs",
        development_default=tmp_path / "development",
        environ={"BFL_RUNTIME_ROOT": str(runtime_root)},
    )

    assert resolved == (
        runtime_root / "artifacts" / "runtime-inputs"
    ).resolve()


def test_development_default_is_preserved_without_deployment_target(
    tmp_path: Path,
) -> None:
    development_default = tmp_path / "development"

    resolved = resolve_runtime_state_path(
        relative_path="artifacts",
        development_default=development_default,
        environ={},
    )

    assert resolved == development_default.resolve()


def test_production_target_requires_unified_or_explicit_root(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="BFL_RUNTIME_ROOT"):
        resolve_runtime_state_path(
            relative_path="artifacts",
            development_default=tmp_path / "development",
            environ={"BFL_DEPLOYMENT_TARGET": "aliyun-gray"},
        )


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"BFL_RUNTIME_ROOT": "relative/root"}, "must be absolute"),
        ({"DATABRIDGE_DATA_ROOT": "relative/data"}, "must be absolute"),
    ],
)
def test_configured_roots_must_be_absolute(
    tmp_path: Path,
    environment: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_runtime_state_path(
            relative_path="data-bridge/data",
            development_default=tmp_path / "development",
            override_env="DATABRIDGE_DATA_ROOT",
            environ=environment,
        )


@pytest.mark.parametrize(
    "relative_path",
    ["", "/absolute", "../escape", "nested/../../escape"],
)
def test_runtime_relative_path_cannot_escape_root(
    tmp_path: Path,
    relative_path: str,
) -> None:
    with pytest.raises(ValueError, match="relative path"):
        resolve_runtime_state_path(
            relative_path=relative_path,
            development_default=tmp_path / "development",
            environ={"BFL_RUNTIME_ROOT": str(tmp_path / "runtime")},
        )


def test_runtime_consumers_share_the_unified_root(tmp_path: Path) -> None:
    from shared.daily_0629_source_runner import (
        _source_cache_path as daily_source_cache_path,
    )
    from shared.monthly_source_runner import (
        _source_cache_path as monthly_source_cache_path,
    )

    with patch.dict(
        os.environ,
        {"BFL_RUNTIME_ROOT": str(tmp_path)},
        clear=True,
    ):
        daily_path = daily_source_cache_path(
            "daily",
            "package-hash",
            "runner.module",
            "2026-08-18",
            "database-id",
            input_token="input-token",
        )
        monthly_path = monthly_source_cache_path(
            "package-hash",
            "runner.module",
            "2026-08-18",
            "database-id",
            input_token="input-token",
        )

    assert daily_path is not None
    assert daily_path.is_relative_to(tmp_path / "cache" / "daily-0629")
    assert monthly_path is not None
    assert monthly_path.is_relative_to(tmp_path / "cache" / "monthly")


def test_artifact_paths_use_unified_root_in_fresh_process(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["BFL_RUNTIME_ROOT"] = str(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from shared.artifact_paths import BACKTEST_ARTIFACT_ROOT; "
            "from shared.liwei_0616_phase_a_cache import DEFAULT_CACHE_ROOT; "
            "print(BACKTEST_ARTIFACT_ROOT); print(DEFAULT_CACHE_ROOT)",
        ],
        cwd=project_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.splitlines() == [
        str(tmp_path / "artifacts"),
        str(tmp_path / "cache" / "liwei-0616"),
    ]
