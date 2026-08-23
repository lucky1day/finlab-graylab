from __future__ import annotations

import inspect
import os
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.run_launchd_release import prepare_exec_environment


_RELEASE_COMMIT = "a" * 40


def _trusted_release(tmp_path: Path) -> tuple[Path, Path]:
    release = tmp_path / "deploy" / "releases" / _RELEASE_COMMIT
    release.mkdir(parents=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    config = runtime / "config"
    config.mkdir(mode=0o700)
    service_environment = config / "service.env"
    service_environment.write_text(
        "\n".join(
            (
                "BOND_ADMIN_TOKEN=local-admin-token",
                "BOND_DB_USER=bond_user",
                "BOND_DB_PASSWORD=database-secret",
                "BOND_DB_HOST=127.0.0.1",
                "BOND_DB_PORT=3306",
                "BOND_DB_NAME=bond_db",
                "BOND_DB_CHARSET=utf8mb4",
                "BOND_FACTOR_LAB_INSTANCE_NONCE=mac3-instance",
                "DATABRIDGE_API_BASE_URL=https://example.invalid",
                "DATABRIDGE_API_USERNAME=bridge_user",
                "DATABRIDGE_API_PASSWORD=bridge-secret",
                "",
            )
        ),
        encoding="utf-8",
    )
    service_environment.chmod(0o600)
    native_cache = runtime / "cache" / "native" / _RELEASE_COMMIT
    release_environment = release / ".bfl-release.env"
    release_environment.write_text(
        "\n".join(
            (
                f"BFL_RELEASE_COMMIT={_RELEASE_COMMIT}",
                f"BFL_RUNTIME_ROOT={runtime}",
                f"NUMBA_CACHE_DIR={native_cache / 'numba'}",
                f"MPLCONFIGDIR={native_cache / 'matplotlib'}",
                "",
            )
        ),
        encoding="utf-8",
    )
    release_environment.chmod(0o444)
    release.chmod(0o555)
    return release, runtime


def test_ephemeral_native_runtime_sets_private_environment(
    tmp_path: Path,
) -> None:
    from scheduler.executor import run_scheme_subprocess
    from shared.input_artifacts import EPHEMERAL_NATIVE_INPUT_ROOT_ENV
    from shared.liwei_0616_cache_contract import (
        CACHE_MUTATION_POLICY_ENV,
        CACHE_MUTATION_POLICY_PRIVATE_BUILD,
    )

    captured: dict[str, str] = {}

    def fake_run(cmd, *, cwd, env, timeout):
        captured.update(env)
        return CompletedProcess(cmd, 0, "[]", "")

    root = tmp_path.resolve()
    with patch(
        "scheduler.executor._run_process_group",
        side_effect=fake_run,
    ):
        run_scheme_subprocess(
            "daily_demo",
            "2026-07-24",
            ephemeral_native_runtime_root=root,
        )

    assert captured[EPHEMERAL_NATIVE_INPUT_ROOT_ENV] == str(root / "inputs")
    assert captured["LIWEI_0616_PHASE_A_CACHE_ROOT"] == str(
        root / "phase-a-cache"
    )
    assert captured[CACHE_MUTATION_POLICY_ENV] == (
        CACHE_MUTATION_POLICY_PRIVATE_BUILD
    )


def test_native_execution_rejects_invalid_runtime_controls() -> None:
    from scheduler.executor import run_configured_scheme

    native = SimpleNamespace(
        runtime_type="native_adapter",
        scheme_id="daily_demo",
    )
    blackbox = SimpleNamespace(
        runtime_type="blackbox_v2",
        input_source="data_bridge_current",
        scheme_id="blackbox_demo",
    )
    common = {
        "engine": object(),
        "algo_env": "forecast_env",
        "timeout_sec": 600,
    }
    with pytest.raises(ValueError, match="absolute"):
        run_configured_scheme(
            native,
            "2026-07-24",
            ephemeral_native_runtime_root="relative/root",
            **common,
        )
    with pytest.raises(ValueError, match="Blackbox"):
        run_configured_scheme(
            native,
            "2026-07-24",
            execution_token="blackbox-only",
            **common,
        )
    with pytest.raises(ValueError, match="native_adapter"):
        run_configured_scheme(
            blackbox,
            "2026-07-24",
            ephemeral_native_runtime_root=Path("/private/native-gap"),
            **common,
        )


def test_retired_generation_keyword_arguments_are_absent() -> None:
    from scheduler import executor

    retired = {
        "native_generation",
        "live_source_compatibility",
        "live_source_package_sha256",
        "databridge_generation",
        "calendar_generation",
    }
    for function in (
        executor.run_scheme_subprocess,
        executor.run_configured_scheme,
        executor.run_blackbox_scheme_subprocess,
    ):
        assert retired.isdisjoint(inspect.signature(function).parameters)


def test_native_subprocess_environment_is_allowlisted() -> None:
    from scheduler.executor import run_scheme_subprocess

    captured: dict[str, str] = {}

    def fake_run(cmd, *, cwd, env, timeout):
        captured.update(env)
        return CompletedProcess(cmd, 0, "[]", "")

    parent = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/Users/tester",
        "TMPDIR": "/tmp/tester/",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "OMP_NUM_THREADS": "2",
        "LIWEI_0616_PHASE_A_CACHE_ROOT": "/tmp/cache",
        "BFL_RUNTIME_ROOT": "/var/lib/bond-factor-lab/runtime",
        "NUMBA_CACHE_DIR": (
            "/var/lib/bond-factor-lab/runtime/cache/native/abc/numba"
        ),
        "MPLCONFIGDIR": (
            "/var/lib/bond-factor-lab/runtime/cache/native/abc/matplotlib"
        ),
        "BFL_DATABASE_ENV_FILE": "/etc/bond-factor-lab/bond-factor-lab.env",
        "BOND_DB_PASSWORD": "secret",
        "BOND_NATIVE_GENERATION_ID": "retired-inherited-value",
    }
    with (
        patch.dict(os.environ, parent, clear=True),
        patch(
            "scheduler.executor._run_process_group",
            side_effect=fake_run,
        ),
    ):
        run_scheme_subprocess("daily_demo", "2026-07-24")

    assert captured["PATH"] == parent["PATH"]
    assert captured["LIWEI_0616_PHASE_A_CACHE_ROOT"] == "/tmp/cache"
    assert captured["BFL_RUNTIME_ROOT"] == (
        "/var/lib/bond-factor-lab/runtime"
    )
    assert captured["NUMBA_CACHE_DIR"] == parent["NUMBA_CACHE_DIR"]
    assert captured["MPLCONFIGDIR"] == parent["MPLCONFIGDIR"]
    assert captured["BFL_DATABASE_ENV_FILE"] == parent[
        "BFL_DATABASE_ENV_FILE"
    ]
    assert "BOND_DB_PASSWORD" not in captured
    assert "BOND_NATIVE_GENERATION_ID" not in captured


def test_launchd_environment_passes_only_trusted_database_file_to_native(
    tmp_path: Path,
) -> None:
    from scheduler.executor import run_scheme_subprocess

    captured: dict[str, str] = {}

    def fake_run(cmd, *, cwd, env, timeout):
        captured.update(env)
        return CompletedProcess(cmd, 0, "[]", "")

    release, runtime = _trusted_release(tmp_path)
    try:
        parent = prepare_exec_environment(release, {})
        with (
            patch.dict(os.environ, parent, clear=True),
            patch(
                "scheduler.executor._run_process_group",
                side_effect=fake_run,
            ),
        ):
            run_scheme_subprocess("daily_demo", "2026-07-24")
    finally:
        release.chmod(0o755)

    assert captured["BFL_DATABASE_ENV_FILE"] == str(
        runtime / "config" / "service.env"
    )
    assert "BOND_DB_USER" not in captured
    assert "BOND_DB_PASSWORD" not in captured
    assert "BOND_DB_HOST" not in captured


def test_v2_policy_timeout_is_a_hard_upper_bound() -> None:
    from scheduler.executor import _effective_timeout_sec

    cfg = SimpleNamespace(
        scheme_id="daily_v2",
        runtime_type="blackbox_v2",
        schedule=SimpleNamespace(timeout_sec=3600),
    )
    assert _effective_timeout_sec(cfg, 120) == 120
