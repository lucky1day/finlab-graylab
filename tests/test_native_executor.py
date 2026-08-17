from __future__ import annotations

import inspect
import os
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import patch

import pytest


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
    assert captured["BFL_DATABASE_ENV_FILE"] == parent[
        "BFL_DATABASE_ENV_FILE"
    ]
    assert "BOND_DB_PASSWORD" not in captured
    assert "BOND_NATIVE_GENERATION_ID" not in captured


def test_v2_policy_timeout_is_a_hard_upper_bound() -> None:
    from scheduler.executor import _effective_timeout_sec

    cfg = SimpleNamespace(
        scheme_id="daily_v2",
        runtime_type="blackbox_v2",
        schedule=SimpleNamespace(timeout_sec=3600),
    )
    assert _effective_timeout_sec(cfg, 120) == 120
