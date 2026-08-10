from __future__ import annotations

import inspect

import pytest


def test_executor_public_contract_has_no_secondary_generation_controls() -> None:
    from scheduler import executor

    configured = inspect.signature(executor.run_configured_scheme).parameters
    native = inspect.signature(executor.run_scheme_subprocess).parameters
    blackbox = inspect.signature(
        executor.run_blackbox_scheme_subprocess
    ).parameters

    retired = {
        "native_generation",
        "live_source_compatibility",
        "live_source_package_sha256",
        "databridge_generation",
        "calendar_generation",
    }
    assert retired.isdisjoint(configured)
    assert retired.isdisjoint(native)
    assert retired.isdisjoint(blackbox)


def test_native_rejects_blackbox_execution_token_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scheduler import executor

    monkeypatch.setattr(
        executor,
        "run_scheme_subprocess",
        lambda *_args, **_kwargs: pytest.fail("subprocess must not start"),
    )
    cfg = type(
        "NativeConfig",
        (),
        {"runtime_type": "native_adapter", "scheme_id": "native_demo"},
    )()

    with pytest.raises(ValueError, match="Blackbox"):
        executor.run_configured_scheme(
            cfg,
            "2026-08-08",
            engine=object(),
            algo_env="forecast_env",
            timeout_sec=60,
            execution_token="opaque-attempt",
        )


def test_single_date_signal_gap_plan_has_no_native_input_identity_matrix() -> None:
    from harness import signal_gap_plan

    fields = signal_gap_plan.RegistryTarget.__dataclass_fields__

    assert signal_gap_plan.PLAN_SCHEMA_VERSION == (
        "single-date-active-live-gap-plan-v1"
    )
    assert "input_mode" not in fields
    assert "source_package_sha256" not in fields


def test_repository_has_no_input_generation_runtime_api() -> None:
    from scheduler import repository

    for name in (
        "InputGenerationEnvelope",
        "read_sealed_input_generation",
        "_create_or_match_input_generation_conn",
        "_input_generation_envelope",
    ):
        assert not hasattr(repository, name)
