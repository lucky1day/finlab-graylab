from __future__ import annotations

import inspect
import json
from dataclasses import replace
from pathlib import Path

import pytest

from harness import signal_gap_plan
from harness.gates.signal_gap_fill_gate import (
    _load_frozen_plan,
    signal_gap_fill_authorization_claims,
)


def _native_snapshot(*, present: bool = False) -> signal_gap_plan.SignalGapSnapshot:
    target = signal_gap_plan.RegistryTarget(
        registry_scheme_id="demo_native__h5__5Y",
        base_scheme_id="demo_native",
        runtime_type="native_adapter",
        frequency="daily",
        task_type="T+5",
        target_tenor="5Y",
        horizon=5,
        scheme_version="version-1",
        live_target_start_date="2026-06-01",
        live_boundary_source="platform_live_boundary_v1",
        code_sha256="a" * 64,
        config_sha256="b" * 64,
    )
    case = signal_gap_plan.ExpectedSignalCase(
        registry_scheme_id=target.registry_scheme_id,
        base_scheme_id=target.base_scheme_id,
        runtime_type=target.runtime_type,
        frequency=target.frequency,
        task_type=target.task_type,
        target_tenor=target.target_tenor,
        horizon=target.horizon,
        predict_date="2026-08-08",
        feature_date="2026-08-07",
        target_date="2026-08-14",
        segment="live",
    )
    observed = (
        signal_gap_plan.ObservedSignal(
            base_scheme_id=case.base_scheme_id,
            target_tenor=case.target_tenor,
            horizon=case.horizon,
            target_date=case.target_date,
            predict_date=case.predict_date,
            feature_date=case.feature_date,
            phase="gray_live",
            scheme_version=target.scheme_version,
            run_status="success",
        ),
    ) if present else ()
    return signal_gap_plan.SignalGapSnapshot(
        registry_targets=(target,),
        expected_cases=(case,),
        canonical_signals=(),
        live_signals=observed,
        input_watermarks={},
        source_identity_sha256="c" * 64,
        discovery_identity_sha256="d" * 64,
        active_version_identity_sha256="e" * 64,
    )


def _build_native_plan(*, present: bool = False) -> dict[str, object]:
    return signal_gap_plan.build_signal_gap_plan(
        _native_snapshot(present=present),
        start_date="2026-08-08",
        as_of_date="2026-08-08",
    )


def test_native_gap_is_actionable_without_input_generation() -> None:
    plan = _build_native_plan()

    assert plan["schema_version"] == "active-signal-gap-plan-v7"
    assert plan["status"] == "READY"
    assert plan["counts"]["GRAY_LIVE_GAP"] == 1
    assert plan["counts"]["BLOCKED_NO_GENERATION"] == 0
    assert plan["actions"][0]["input_authority"] is None
    assert "input_mode" not in plan["actions"][0]
    assert "source_package_sha256" not in plan["actions"][0]


def test_present_native_signal_remains_skip_present() -> None:
    plan = _build_native_plan(present=True)

    assert plan["actions"][0]["action"] == "SKIP_PRESENT"
    assert plan["actions"][0]["input_authority"] is None


def test_blackbox_gap_still_requires_databridge_generation() -> None:
    native = _native_snapshot()
    blackbox_target = replace(
        native.registry_targets[0],
        registry_scheme_id="demo_blackbox__h5__5Y",
        base_scheme_id="demo_blackbox",
        runtime_type="blackbox_v2",
    )
    blackbox_case = replace(
        native.expected_cases[0],
        registry_scheme_id=blackbox_target.registry_scheme_id,
        base_scheme_id=blackbox_target.base_scheme_id,
        runtime_type="blackbox_v2",
    )
    plan = signal_gap_plan.build_signal_gap_plan(
        replace(
            native,
            registry_targets=(blackbox_target,),
            expected_cases=(blackbox_case,),
        ),
        start_date="2026-08-08",
        as_of_date="2026-08-08",
    )

    assert plan["status"] == "BLOCKED"
    assert plan["actions"][0]["action"] == "BLOCKED_NO_GENERATION"


def test_native_fill_claim_has_null_source_authority() -> None:
    claims = signal_gap_fill_authorization_claims(_build_native_plan())

    assert len(claims) == 1
    assert claims[0].source_authority is None


def test_snapshot_reader_does_not_query_input_generations() -> None:
    source = inspect.getsource(signal_gap_plan.read_signal_gap_snapshot)

    assert "_read_input_generations" not in source
    assert "t_input_generations" not in source


@pytest.mark.parametrize(
    "legacy_schema",
    [
        "active-signal-gap-plan-v4",
        "active-signal-gap-plan-v5",
        "active-signal-gap-plan-v6",
    ],
)
def test_fill_rejects_legacy_plan_schemas(
    tmp_path: Path,
    legacy_schema: str,
) -> None:
    plan = _build_native_plan()
    plan["schema_version"] = legacy_schema
    plan["plan_sha256"] = signal_gap_plan.canonical_plan_sha256(plan)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(ValueError, match="schema"):
        _load_frozen_plan(path)
