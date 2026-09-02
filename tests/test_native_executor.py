from __future__ import annotations

import os
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
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
                "BOND_DB_USER=bond_user",
                "BOND_DB_PASSWORD=database-secret",
                "BOND_DB_HOST=127.0.0.1",
                "BOND_DB_PORT=3306",
                "BOND_DB_NAME=bond_db",
                "BOND_DB_CHARSET=utf8mb4",
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


def test_ephemeral_native_runtime_only_sets_shared_input_environment(
    tmp_path: Path,
) -> None:
    from scheduler.executor import run_scheme_subprocess
    from shared.input_artifacts import EPHEMERAL_NATIVE_INPUT_ROOT_ENV
    from shared.liwei_0616_cache_contract import CACHE_MUTATION_POLICY_ENV

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

    assert captured[EPHEMERAL_NATIVE_INPUT_ROOT_ENV] == str(root)
    assert "LIWEI_0616_PHASE_A_CACHE_ROOT" not in captured
    assert CACHE_MUTATION_POLICY_ENV not in captured


def test_incremental_native_runtime_reuses_persistent_cache(
    tmp_path: Path,
) -> None:
    from scheduler.executor import run_scheme_subprocess
    from shared.input_artifacts import EPHEMERAL_NATIVE_INPUT_ROOT_ENV
    from shared.liwei_0616_cache_contract import (
        CACHE_MUTATION_POLICY_ENV,
        CACHE_MUTATION_POLICY_INCREMENTAL_ONLY,
    )

    captured: dict[str, str] = {}

    def fake_run(cmd, *, cwd, env, timeout):
        captured.update(env)
        return CompletedProcess(cmd, 0, "[]", "")

    root = tmp_path.resolve()
    persistent_cache = str(tmp_path / "persistent-cache")
    with (
        patch.dict(
            os.environ,
            {"LIWEI_0616_PHASE_A_CACHE_ROOT": persistent_cache},
            clear=False,
        ),
        patch(
            "scheduler.executor._run_process_group",
            side_effect=fake_run,
        ),
    ):
        run_scheme_subprocess(
            "daily_demo",
            "2026-07-24",
            ephemeral_native_runtime_root=root,
            native_cache_mutation_policy=(
                CACHE_MUTATION_POLICY_INCREMENTAL_ONLY
            ),
        )

    assert captured[EPHEMERAL_NATIVE_INPUT_ROOT_ENV] == str(root)
    assert captured["LIWEI_0616_PHASE_A_CACHE_ROOT"] == persistent_cache
    assert captured[CACHE_MUTATION_POLICY_ENV] == (
        CACHE_MUTATION_POLICY_INCREMENTAL_ONLY
    )


def test_private_native_runtime_uses_explicit_cache_root(
    tmp_path: Path,
) -> None:
    from scheduler.executor import run_scheme_subprocess
    from shared.liwei_0616_cache_contract import (
        CACHE_MUTATION_POLICY_ENV,
        CACHE_MUTATION_POLICY_PRIVATE_BUILD,
    )

    captured: dict[str, str] = {}

    def fake_run(cmd, *, cwd, env, timeout):
        captured.update(env)
        return CompletedProcess(cmd, 0, "[]", "")

    persistent_cache = tmp_path / "persistent-cache"
    persistent_cache.mkdir()
    persistent_current = persistent_cache / "current.json"
    persistent_current.write_text("persistent", encoding="utf-8")
    private_cache = tmp_path / "dry-run" / "phase-a-cache"
    with (
        patch.dict(
            os.environ,
            {"LIWEI_0616_PHASE_A_CACHE_ROOT": str(persistent_cache)},
            clear=False,
        ),
        patch(
            "scheduler.executor._run_process_group",
            side_effect=fake_run,
        ),
    ):
        run_scheme_subprocess(
            "daily_demo",
            "2026-07-24",
            ephemeral_native_runtime_root=tmp_path.resolve(),
            native_cache_mutation_policy=(
                CACHE_MUTATION_POLICY_PRIVATE_BUILD
            ),
            native_phase_a_cache_root=private_cache.resolve(),
        )

    assert captured["LIWEI_0616_PHASE_A_CACHE_ROOT"] == str(
        private_cache.resolve()
    )
    assert captured[CACHE_MUTATION_POLICY_ENV] == (
        CACHE_MUTATION_POLICY_PRIVATE_BUILD
    )
    assert persistent_current.read_text(encoding="utf-8") == "persistent"


@pytest.mark.parametrize(
    "policy",
    ["incremental_only", "scheduled_bounded_reconcile"],
)
def test_scheduled_policy_reaches_phase_a_build_decision(
    tmp_path: Path,
    policy: str,
) -> None:
    from shared import liwei_0616_phase_a_cache as cache_module
    from shared.liwei_0616_cache_contract import (
        CACHE_MUTATION_POLICY_ENV,
    )

    spec = cache_module.PhaseACacheSpec(
        cache_family="test_incremental_policy",
        tenor="10Y",
        publisher_consumer_id="publisher",
        baselines=("baseline",),
        baseline_configs={"baseline": {}},
        source_ic_screen_start="2024-01-01",
        horizon=5,
        purge_gap=5,
    )
    captured: dict[str, object] = {}

    def fake_prepare(**kwargs):
        captured.update(kwargs)
        return {}, {}

    with (
        patch.dict(
            os.environ,
            {
                CACHE_MUTATION_POLICY_ENV: policy
            },
            clear=False,
        ),
        patch.object(
            cache_module,
            "_prepare_under_family_lock",
            side_effect=fake_prepare,
        ),
    ):
        cache_module.prepare_phase_a_caches(
            spec=spec,
            daily_df=Mock(),
            weekly_df=Mock(),
            monthly_df=Mock(),
            test_ranges=(("2026-08-26", "2026-08-26"),),
            train_missing=Mock(),
            cache_consumer_id="publisher",
            cache_root=tmp_path,
        )

    assert captured["mutation_policy"] == policy


def test_incremental_cache_policy_only_accepts_hit_or_one_tail_date() -> None:
    from shared.liwei_0616_cache_contract import (
        CACHE_MUTATION_POLICY_INCREMENTAL_ONLY,
    )
    from shared.liwei_0616_phase_a_cache import (
        _LoadedGeneration,
        _validate_scheduled_cache_build,
    )

    current = _LoadedGeneration(
        generation_id="generation-1",
        path=Path("/cache/generation-1"),
        manifest={},
        manifest_sha256="a" * 64,
        caches={
            "baseline-a": {"test_dates": ["2026-08-25"]},
            "baseline-b": {"test_dates": ["2026-08-25"]},
        },
    )
    common = {
        "mutation_policy": CACHE_MUTATION_POLICY_INCREMENTAL_ONLY,
        "build_reason": "tail_append",
        "current": current,
    }
    _validate_scheduled_cache_build(
        **common,
        build_mode="append",
        planned_missing_dates={"baseline-a": [], "baseline-b": []},
    )
    _validate_scheduled_cache_build(
        **common,
        build_mode="append",
        planned_missing_dates={
            "baseline-a": ["2026-08-26"],
            "baseline-b": ["2026-08-26"],
        },
    )
    with pytest.raises(RuntimeError, match="build_mode=suffix"):
        _validate_scheduled_cache_build(
            **common,
            build_mode="suffix",
            planned_missing_dates={"baseline-a": ["2026-08-25"]},
        )
    with pytest.raises(RuntimeError, match="at most one"):
        _validate_scheduled_cache_build(
            **common,
            build_mode="append",
            planned_missing_dates={
                "baseline-a": ["2026-08-26"],
                "baseline-b": ["2026-08-27"],
            },
        )
    with pytest.raises(RuntimeError, match="historical"):
        _validate_scheduled_cache_build(
            **common,
            build_mode="append",
            planned_missing_dates={"baseline-a": ["2026-08-25"]},
        )


def test_scheduled_cache_policy_accepts_only_bounded_proven_suffix() -> None:
    from shared.liwei_0616_cache_contract import (
        CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE,
    )
    from shared.liwei_0616_phase_a_cache import (
        _LoadedGeneration,
        _validate_scheduled_cache_build,
    )

    current = _LoadedGeneration(
        generation_id="generation-1",
        path=Path("/cache/generation-1"),
        manifest={},
        manifest_sha256="a" * 64,
        caches={"baseline-a": {"test_dates": ["2026-07-25"]}},
    )
    common = {
        "mutation_policy": (
            CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE
        ),
        "current": current,
    }
    for reason in (
        "proven_daily_input_revision",
        "combined_daily_effective_revision",
        "effective_auxiliary_revision",
    ):
        _validate_scheduled_cache_build(
            **common,
            build_mode="suffix",
            build_reason=reason,
            planned_missing_dates={
                "baseline-a": [
                    f"2026-08-{day:02d}" for day in range(1, 25)
                ]
            },
        )

    with pytest.raises(RuntimeError, match="rejects suffix reason"):
        _validate_scheduled_cache_build(
            **common,
            build_mode="suffix",
            build_reason="weekly_input_revision_unmappable",
            planned_missing_dates={"baseline-a": ["2026-08-01"]},
        )
    with pytest.raises(RuntimeError, match="exceeds 32 dates"):
        _validate_scheduled_cache_build(
            **common,
            build_mode="suffix",
            build_reason="proven_daily_input_revision",
            planned_missing_dates={
                "baseline-a": [f"2026-08-{day:02d}" for day in range(1, 34)]
            },
        )
    with pytest.raises(RuntimeError, match="build_mode=full"):
        _validate_scheduled_cache_build(
            **common,
            build_mode="full",
            build_reason="spec_changed",
            planned_missing_dates={"baseline-a": ["2026-08-01"]},
        )


def test_suffix_rebuild_includes_new_requested_dates_before_cutoff() -> None:
    from shared.liwei_0616_phase_a_cache import (
        _planned_cache_missing_dates,
    )

    missing = _planned_cache_missing_dates(
        build_mode="suffix",
        requested_dates=[
            "2025-09-01",
            "2025-09-02",
            "2026-09-01",
        ],
        cached_dates={"2026-08-22", "2026-08-25"},
        parent_dates={"2026-08-22", "2026-08-25"},
        suffix_start_date="2026-08-24",
    )

    assert missing == [
        "2025-09-01",
        "2025-09-02",
        "2026-08-25",
        "2026-09-01",
    ]


def test_scheduled_policy_accepts_bounded_requested_coverage_expansion() -> None:
    from shared.liwei_0616_cache_contract import (
        CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE,
    )
    from shared.liwei_0616_phase_a_cache import (
        _LoadedGeneration,
        _validate_scheduled_cache_build,
    )

    current = _LoadedGeneration(
        generation_id="generation-1",
        path=Path("/cache/generation-1"),
        manifest={},
        manifest_sha256="a" * 64,
        caches={"baseline-a": {"test_dates": ["2026-08-31"]}},
    )
    coverage_dates = [
        "2025-09-01",
        "2025-09-02",
        "2025-09-03",
    ]

    _validate_scheduled_cache_build(
        mutation_policy=(
            CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE
        ),
        build_mode="append",
        build_reason="cache_complete",
        current=current,
        planned_missing_dates={"baseline-a": coverage_dates},
        requested_expansion_dates={"baseline-a": coverage_dates},
    )


def test_scheduled_policy_rejects_unrequested_historical_expansion() -> None:
    from shared.liwei_0616_cache_contract import (
        CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE,
    )
    from shared.liwei_0616_phase_a_cache import (
        _LoadedGeneration,
        _validate_scheduled_cache_build,
    )

    current = _LoadedGeneration(
        generation_id="generation-1",
        path=Path("/cache/generation-1"),
        manifest={},
        manifest_sha256="a" * 64,
        caches={"baseline-a": {"test_dates": ["2026-08-31"]}},
    )

    with pytest.raises(RuntimeError, match="requested coverage"):
        _validate_scheduled_cache_build(
            mutation_policy=(
                CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE
            ),
            build_mode="append",
            build_reason="cache_complete",
            current=current,
            planned_missing_dates={
                "baseline-a": ["2025-09-01", "2025-09-02"]
            },
            requested_expansion_dates={
                "baseline-a": ["2025-09-01"]
            },
        )


def test_scheduled_policy_rejects_cross_baseline_coverage_substitution() -> None:
    from shared.liwei_0616_cache_contract import (
        CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE,
    )
    from shared.liwei_0616_phase_a_cache import (
        _LoadedGeneration,
        _validate_scheduled_cache_build,
    )

    current = _LoadedGeneration(
        generation_id="generation-1",
        path=Path("/cache/generation-1"),
        manifest={},
        manifest_sha256="a" * 64,
        caches={
            "baseline-a": {"test_dates": ["2026-08-31"]},
            "baseline-b": {"test_dates": ["2026-08-31"]},
        },
    )

    with pytest.raises(RuntimeError, match="requested coverage"):
        _validate_scheduled_cache_build(
            mutation_policy=(
                CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE
            ),
            build_mode="append",
            build_reason="cache_complete",
            current=current,
            planned_missing_dates={
                "baseline-a": ["2025-09-01"],
                "baseline-b": [],
            },
            requested_expansion_dates={
                "baseline-a": [],
                "baseline-b": ["2025-09-01"],
            },
        )


def test_scheduled_policy_limits_requested_coverage_expansion() -> None:
    from shared.liwei_0616_cache_contract import (
        CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE,
    )
    from shared.liwei_0616_phase_a_cache import (
        _LoadedGeneration,
        _validate_scheduled_cache_build,
    )

    current = _LoadedGeneration(
        generation_id="generation-1",
        path=Path("/cache/generation-1"),
        manifest={},
        manifest_sha256="a" * 64,
        caches={"baseline-a": {"test_dates": ["2026-08-31"]}},
    )
    coverage_dates = [f"2025-09-{day:02d}" for day in range(1, 34)]

    with pytest.raises(RuntimeError, match="exceeds 32 dates"):
        _validate_scheduled_cache_build(
            mutation_policy=(
                CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE
            ),
            build_mode="append",
            build_reason="cache_complete",
            current=current,
            planned_missing_dates={"baseline-a": coverage_dates},
            requested_expansion_dates={"baseline-a": coverage_dates},
        )


def test_candidate_cache_must_cover_every_requested_date() -> None:
    from shared.liwei_0616_phase_a_cache import (
        _require_requested_cache_coverage,
    )

    with pytest.raises(RuntimeError, match="requested dates"):
        _require_requested_cache_coverage(
            baseline="baseline-a",
            requested_dates=["2025-09-01", "2025-09-02"],
            cache={"test_dates": ["2025-09-01"]},
        )


def test_suffix_requested_expansion_survives_consumer_lineage_reload(
    tmp_path: Path,
) -> None:
    from shared import liwei_0616_phase_a_cache as cache_module
    from shared.liwei_0616_cache_contract import (
        CACHE_MUTATION_POLICY_ENV,
        CACHE_MUTATION_POLICY_PRIVATE_BUILD,
        CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE,
    )

    spec = cache_module.PhaseACacheSpec(
        cache_family="test_month_boundary_lineage",
        tenor="7Y",
        publisher_consumer_id="publisher",
        baselines=("baseline",),
        baseline_configs={"baseline": {"close": "close"}},
        source_ic_screen_start="2024-01-01",
        horizon=1,
        purge_gap=1,
        daily_dependency_lookback_rows=1,
        daily_dependency_proof=(
            cache_module.DAILY_REVISION_SUFFIX_PROOF_V1
        ),
    )
    original_daily = pd.DataFrame(
        {
            "date": [
                "2025-09-01",
                "2026-08-25",
                "2026-08-28",
                "2026-08-31",
            ],
            "close": [1.0, 2.0, 3.0, 4.0],
        }
    )
    revised_daily = original_daily.copy()
    revised_daily.loc[
        revised_daily["date"] == "2026-08-31",
        "close",
    ] = 4.5
    weekly = pd.DataFrame({"week_id": [202635], "value": [1.0]})
    monthly = pd.DataFrame({"month_id": ["2026-08"], "value": [1.0]})

    def train(_baseline, ranges):
        dates = [start for start, end in ranges if start == end]
        return {
            "test_dates": dates,
            "results": [
                {
                    "config": {"model": "fixed"},
                    "preds": np.ones(len(dates), dtype=np.int32),
                    "probs": np.full(len(dates), 0.75),
                }
            ],
        }

    common = {
        "spec": spec,
        "weekly_df": weekly,
        "monthly_df": monthly,
        "train_missing": train,
        "cache_root": tmp_path.resolve(),
    }
    with patch.dict(
        os.environ,
        {CACHE_MUTATION_POLICY_ENV: CACHE_MUTATION_POLICY_PRIVATE_BUILD},
        clear=False,
    ):
        cache_module.prepare_phase_a_caches(
            **common,
            daily_df=original_daily,
            test_ranges=(
                ("2026-08-25", "2026-08-25"),
                ("2026-08-31", "2026-08-31"),
            ),
            cache_consumer_id="publisher",
        )

    expanded_ranges = (
        ("2025-09-01", "2025-09-01"),
        ("2026-08-25", "2026-08-25"),
        ("2026-08-31", "2026-08-31"),
    )
    with patch.dict(
        os.environ,
        {
            CACHE_MUTATION_POLICY_ENV: (
                CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE
            )
        },
        clear=False,
    ):
        published, publish_audit = cache_module.prepare_phase_a_caches(
            **common,
            daily_df=revised_daily,
            test_ranges=expanded_ranges,
            cache_consumer_id="publisher",
        )
        reloaded, consumer_audit = cache_module.prepare_phase_a_caches(
            **common,
            daily_df=revised_daily,
            test_ranges=expanded_ranges,
            cache_consumer_id="consumer",
        )

    assert publish_audit["build_mode"] == "suffix"
    assert published["baseline"]["test_dates"] == [
        "2025-09-01",
        "2026-08-25",
        "2026-08-31",
    ]
    assert reloaded["baseline"]["test_dates"] == (
        published["baseline"]["test_dates"]
    )
    assert consumer_audit["build_reason"] == "consumer_validated_hit"


def test_called_process_error_uses_bounded_stderr_reason() -> None:
    import subprocess

    from scheduler.executor import _execution_error_message

    exc = subprocess.CalledProcessError(
        1,
        ["secret-command", "--token", "secret"],
        output="ignored stdout\n",
        stderr=(
            "traceback\n"
            "incremental_only requires build_mode=suffix, "
            "reason=proven_daily_input_revision\x00\n"
        ),
    )

    message = _execution_error_message(exc)

    assert message == (
        "cache policy blocked: policy=incremental_only, "
        "build_mode=suffix, reason=proven_daily_input_revision"
    )
    assert "secret-command" not in message


@pytest.mark.parametrize("stream", ["stderr", "stdout"])
def test_called_process_error_does_not_persist_unrecognized_output(
    stream: str,
) -> None:
    import subprocess

    from scheduler.executor import _execution_error_message

    payload = (
        "DATABASE_URL=mysql://writer:s3cr3t@db/bond "
        "password=hunter2 token=secret-token"
    )
    kwargs = {"output": "", "stderr": ""}
    kwargs["stderr" if stream == "stderr" else "output"] = payload
    exc = subprocess.CalledProcessError(7, ["algorithm"], **kwargs)

    message = _execution_error_message(exc)

    assert message == "algorithm process exited with status 7"
    assert "s3cr3t" not in message
    assert "hunter2" not in message
    assert "secret-token" not in message


def test_called_process_error_rejects_unknown_cache_reason() -> None:
    import subprocess

    from scheduler.executor import _execution_error_message

    exc = subprocess.CalledProcessError(
        9,
        ["algorithm"],
        stderr=(
            "scheduled_bounded_reconcile requires build_mode=suffix, "
            "reason=secret_token"
        ),
    )

    assert _execution_error_message(exc) == (
        "algorithm process exited with status 9"
    )


def test_scheduled_cache_policies_disable_runtime_comparison() -> None:
    from shared.liwei_0616_cache_contract import (
        CACHE_MUTATION_POLICY_ENV,
        CACHE_MUTATION_POLICY_INCREMENTAL_ONLY,
        CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE,
    )
    from shared.liwei_0616_phase_a_cache import (
        runtime_compare_gate_callbacks,
    )

    for policy in (
        CACHE_MUTATION_POLICY_INCREMENTAL_ONLY,
        CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE,
    ):
        train = Mock()
        full_output = Mock()
        with patch.dict(
            os.environ,
            {CACHE_MUTATION_POLICY_ENV: policy},
            clear=False,
        ):
            callbacks = runtime_compare_gate_callbacks(
                train_phase_a=train,
                run_full_output=full_output,
            )

        assert callbacks == (None, None)
        train.assert_not_called()
        full_output.assert_not_called()


def test_native_input_audit_root_is_passed_only_when_explicit(
    tmp_path: Path,
) -> None:
    from scheduler.executor import run_scheme_subprocess
    from shared.input_artifacts import NATIVE_INPUT_AUDIT_ROOT_ENV

    captured: dict[str, str] = {}

    def fake_run(cmd, *, cwd, env, timeout):
        captured.update(env)
        return CompletedProcess(cmd, 0, "[]", "")

    audit_root = (tmp_path / "audit").resolve()
    audit_root.mkdir(mode=0o700)
    audit_root.chmod(0o700)
    with patch(
        "scheduler.executor._run_process_group",
        side_effect=fake_run,
    ):
        run_scheme_subprocess(
            "daily_demo",
            "2026-07-24",
            native_input_audit_root=audit_root,
        )

    assert captured[NATIVE_INPUT_AUDIT_ROOT_ENV] == str(audit_root)


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
    with pytest.raises(ValueError, match="native_adapter"):
        run_configured_scheme(
            blackbox,
            "2026-07-24",
            ephemeral_native_runtime_root=Path("/private/native-gap"),
            **common,
        )
    with pytest.raises(ValueError, match="cache mutation policy"):
        run_configured_scheme(
            native,
            "2026-07-24",
            native_cache_mutation_policy="full_rebuild",
            **common,
        )
    with pytest.raises(ValueError, match="native_adapter"):
        run_configured_scheme(
            blackbox,
            "2026-07-24",
            native_cache_mutation_policy="incremental_only",
            **common,
        )


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
        "UNTRUSTED_PARENT_VALUE": "must-not-be-inherited",
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
    assert "UNTRUSTED_PARENT_VALUE" not in captured


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


def test_native_process_group_is_terminated_on_interruption() -> None:
    from scheduler.executor import _run_process_group

    process = SimpleNamespace(
        pid=4321,
        communicate=Mock(side_effect=KeyboardInterrupt()),
        returncode=None,
    )
    termination = SimpleNamespace(confirmed_gone=True)
    with (
        patch("scheduler.executor.subprocess.Popen", return_value=process),
        patch(
            "scheduler.executor.capture_new_session_process_group",
            return_value=4321,
        ),
        patch(
            "scheduler.executor._terminate_process_group",
            return_value=termination,
        ) as terminate,
        pytest.raises(KeyboardInterrupt),
    ):
        _run_process_group(
            ["python", "scheme.py"],
            cwd=Path("/tmp"),
            env={},
            timeout=600,
        )

    terminate.assert_called_once_with(process, process_group_id=4321)
