"""已退役运行时入口的最小负向边界。"""

from __future__ import annotations

import inspect

import pytest


def test_executor_has_no_secondary_generation_controls():
    from scheduler import executor

    retired = {
        "native_generation",
        "live_source_compatibility",
        "live_source_package_sha256",
        "databridge_generation",
        "calendar_generation",
    }
    for execute in (
        executor.run_configured_scheme,
        executor.run_scheme_subprocess,
        executor.run_blackbox_scheme_subprocess,
    ):
        assert retired.isdisjoint(inspect.signature(execute).parameters)


def test_native_rejects_blackbox_execution_token_before_subprocess(monkeypatch):
    from scheduler import executor

    monkeypatch.setattr(
        executor,
        "run_scheme_subprocess",
        lambda *_args, **_kwargs: pytest.fail("subprocess must not start"),
    )
    config = type(
        "NativeConfig",
        (),
        {"runtime_type": "native_adapter", "scheme_id": "native_demo"},
    )()

    with pytest.raises(ValueError, match="Blackbox"):
        executor.run_configured_scheme(
            config,
            "2026-08-08",
            engine=object(),
            algo_env="forecast_env",
            timeout_sec=60,
            execution_token="opaque-attempt",
        )


def test_gap_plan_has_no_native_input_identity_matrix():
    from harness.signal_gap_plan import RegistryTarget

    fields = RegistryTarget.__dataclass_fields__
    assert "input_mode" not in fields
    assert "source_package_sha256" not in fields


def test_repository_has_no_input_generation_runtime_api():
    from scheduler import repository

    for name in (
        "InputGenerationEnvelope",
        "read_sealed_input_generation",
        "_create_or_match_input_generation_conn",
        "_input_generation_envelope",
    ):
        assert not hasattr(repository, name)
