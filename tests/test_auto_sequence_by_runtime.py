"""`all` 自动段按 runtime_type 分开，并避免重复构建 Native 输入。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from harness.context import GateContext
from harness.registry import (
    gate_for_name,
    gates_for_stage,
    sequence_for_stage,
)


def test_auto_sequence_is_exact_for_each_runtime() -> None:
    expected = {
        "native_adapter": [
            "static",
            "dry-run",
            "compare",
            "backtest",
        ],
        "blackbox_v2": ["static", "compare"],
    }
    for runtime_type, sequence in expected.items():
        assert sequence_for_stage("all", runtime_type=runtime_type) == sequence


def test_partial_onboard_stages_are_rejected_for_both_runtimes() -> None:
    """定位问题使用独立 gate；onboard 只保留完整流程。"""
    for runtime_type in ("blackbox_v2", "native_adapter"):
        for stage in ("static", "input", "unit", "dry-run", "compare", "backtest"):
            with pytest.raises(ValueError, match="unsupported onboard stage"):
                sequence_for_stage(stage, runtime_type=runtime_type)


def test_native_maintenance_has_one_exact_sequence(tmp_path) -> None:
    expected = [
        "static",
        "native-maintenance-admission",
        "dry-run",
    ]
    ctx = GateContext(
        scheme_id="native_daily",
        predict_date="2026-08-25",
        project_root=tmp_path,
        config=SimpleNamespace(runtime_type="native_adapter"),
    )

    assert sequence_for_stage("native-maintenance") == expected
    assert [
        gate.name
        for gate in gates_for_stage("native-maintenance", ctx=ctx)
    ] == expected
    with pytest.raises(ValueError, match="unsupported onboard stage"):
        sequence_for_stage("native-maintenance-admission")


def test_native_has_no_standalone_input_gate(tmp_path) -> None:
    ctx = GateContext(
        scheme_id="native_daily",
        predict_date="2026-08-25",
        project_root=tmp_path,
        config=SimpleNamespace(runtime_type="native_adapter"),
    )

    with pytest.raises(ValueError, match="unsupported gate: input"):
        gate_for_name("input", ctx=ctx)


def test_blackbox_rejects_native_maintenance(tmp_path) -> None:
    ctx = GateContext(
        scheme_id="blackbox_daily",
        predict_date="2026-08-25",
        project_root=tmp_path,
        config=SimpleNamespace(runtime_type="blackbox_v2"),
    )

    with pytest.raises(ValueError, match="Blackbox"):
        gates_for_stage("native-maintenance", ctx=ctx)


def test_blackbox_persist_cli_builds_exact_operation_scope(tmp_path) -> None:
    from harness.cli import _build_parser, _run_gate
    from harness.result import GateResult, GateStatus

    captured = {}

    class CaptureGate:
        def run(self, ctx):
            captured["ctx"] = ctx
            return GateResult(
                gate_name="backtest",
                status=GateStatus.PASSED,
                evidence=[],
                errors=[],
                started_at="2026-08-25T00:00:00+00:00",
                finished_at="2026-08-25T00:00:01+00:00",
            )

    args = _build_parser().parse_args(
        [
            "gate",
            "backtest",
            "--scheme-id",
            "trial",
            "--predict-date",
            "2026-08-25",
            "--persist",
            "--backtest-start-date",
            "2025-01-01",
            "--project-root",
            str(tmp_path),
        ]
    )
    config = SimpleNamespace(
        runtime_type="blackbox_v2",
        scheme_version="version-test",
    )
    with (
        patch("harness.cli._load_config_for_dispatch", return_value=config),
        patch("harness.cli.gate_for_name", return_value=CaptureGate()),
        patch("harness.blackbox_v2.gates.cleanup_runtime_input"),
    ):
        result = _run_gate(args)

    assert result.passed
    ctx = captured["ctx"]
    assert ctx.persist_backtest is True
    assert ctx.operation.action == "backtest_persist"
    assert ctx.operation.predict_date == "2026-08-25"
    assert ctx.operation.backtest_start_date == "2025-01-01"


def test_cli_json_keeps_derived_passed_field() -> None:
    from harness.cli import _jsonable
    from harness.result import GateResult, GateStatus, OnboardReport

    result = GateResult(
        gate_name="input",
        status=GateStatus.SKIPPED,
        evidence=[],
        errors=[],
        started_at="2026-08-25T00:00:00+00:00",
        finished_at="2026-08-25T00:00:01+00:00",
    )
    report = OnboardReport(
        scheme_id="trial",
        predict_date="2026-08-25",
        stage_requested="all",
        results=[result],
    )

    assert _jsonable(result)["passed"] is True
    assert _jsonable(report)["results"][0]["passed"] is True
    assert _jsonable(report)["overall_passed"] is True
    assert _jsonable(report)["control_plane_persisted"] is True
