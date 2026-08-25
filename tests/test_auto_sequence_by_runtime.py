"""`all` 自动段按 runtime_type 分开：Blackbox 精简，Native 保持不变。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from harness.context import GateContext
from harness.registry import (
    AUTO_SEQUENCE,
    BLACKBOX_AUTO_SEQUENCE,
    gates_for_stage,
    sequence_for_stage,
)


def test_native_sequence_is_unchanged() -> None:
    """Native 的 CompareGate 是 benchmark 对比，与 Blackbox 无关，其序列不得改动。"""
    assert AUTO_SEQUENCE == ["static", "input", "unit", "dry-run", "compare", "backtest"]
    assert sequence_for_stage("all", runtime_type="native_adapter") == AUTO_SEQUENCE


def test_blackbox_sequence_drops_the_duplicated_gates() -> None:
    """dry-run 已并入 Compare；交付自身性质不由平台重复抽样认证。"""
    assert BLACKBOX_AUTO_SEQUENCE == ["static", "input", "unit", "compare"]
    assert sequence_for_stage("all", runtime_type="blackbox_v2") == BLACKBOX_AUTO_SEQUENCE
    assert "dry-run" not in BLACKBOX_AUTO_SEQUENCE
    assert "backtest" not in BLACKBOX_AUTO_SEQUENCE


def test_partial_onboard_stages_are_rejected_for_both_runtimes() -> None:
    """定位问题使用独立 gate；onboard 只保留完整流程。"""
    for runtime_type in ("blackbox_v2", "native_adapter"):
        for stage in ("static", "input", "unit", "dry-run", "compare", "backtest"):
            with pytest.raises(ValueError, match="unsupported onboard stage"):
                sequence_for_stage(stage, runtime_type=runtime_type)


def test_default_runtime_type_is_native_for_backward_compatibility() -> None:
    assert sequence_for_stage("all") == AUTO_SEQUENCE


def test_native_maintenance_has_one_exact_sequence(tmp_path) -> None:
    expected = [
        "static",
        "native-maintenance-admission",
        "input",
        "unit",
        "dry-run",
    ]
    ctx = GateContext(
        scheme_id="native_daily",
        predict_date="2026-08-25",
        project_root=tmp_path,
        report_dir=tmp_path / "reports",
        config=SimpleNamespace(runtime_type="native_adapter"),
    )

    assert sequence_for_stage("native-maintenance") == expected
    assert [
        gate.name
        for gate in gates_for_stage("native-maintenance", ctx=ctx)
    ] == expected
    with pytest.raises(ValueError, match="unsupported onboard stage"):
        sequence_for_stage("native-maintenance-admission")


def test_blackbox_rejects_native_maintenance(tmp_path) -> None:
    ctx = GateContext(
        scheme_id="blackbox_daily",
        predict_date="2026-08-25",
        project_root=tmp_path,
        report_dir=tmp_path / "reports",
        config=SimpleNamespace(runtime_type="blackbox_v2"),
    )

    with pytest.raises(ValueError, match="Blackbox"):
        gates_for_stage("native-maintenance", ctx=ctx)


def test_activation_gate_set_matches_native_all_sequence() -> None:
    from harness.gates.activate_gate import REQUIRED_ACTIVATE_GATES

    assert REQUIRED_ACTIVATE_GATES == frozenset(AUTO_SEQUENCE)


def test_blackbox_has_no_standalone_dry_run_or_no_persist_backtest(tmp_path) -> None:
    """Compare 已覆盖冒烟；Blackbox backtest 入口只保留真正的持久化动作。"""
    from harness.blackbox_v2.gates import BlackboxBacktestGate
    from harness.cli import _build_parser
    from harness.registry import gate_for_name

    config = type("Config", (), {"runtime_type": "blackbox_v2"})()
    ctx = GateContext(
        scheme_id="trial",
        predict_date="2026-08-25",
        project_root=tmp_path,
        report_dir=tmp_path / "reports",
        config=config,
    )
    with pytest.raises(ValueError, match="unsupported Blackbox V2 gate"):
        gate_for_name("dry-run", ctx=ctx)

    result = BlackboxBacktestGate().run(ctx)
    assert not result.passed
    assert result.errors == ["Blackbox backtest requires --persist"]

    from harness.result import GateResult, GateStatus

    expected = GateResult(
        gate_name="backtest",
        status=GateStatus.PASSED,
        passed=True,
        evidence=[],
        errors=[],
        started_at="2026-08-25T00:00:00+00:00",
        finished_at="2026-08-25T00:00:01+00:00",
    )
    gate = BlackboxBacktestGate()
    persist_ctx = replace(ctx, persist_backtest=True)
    with patch.object(gate, "_run_persist", return_value=expected) as persist:
        assert gate.run(persist_ctx) is expected
    persist.assert_called_once()
    assert persist.call_args.args[0] is persist_ctx

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["gate", "backtest", "--scheme-id", "trial", "--sample-size", "4"]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "gate",
                "compare",
                "--scheme-id",
                "trial",
                "--prediction-phase",
                "gray_live",
            ]
        )


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
                passed=True,
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
    assert ctx.prediction_phase is None
    assert ctx.operation.action == "backtest_persist"
    assert ctx.operation.predict_date == "2026-08-25"
    assert ctx.operation.backtest_start_date == "2025-01-01"


def test_live_cli_requires_prediction_phase() -> None:
    from harness.cli import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["gate", "live", "--scheme-id", "trial"])
    args = parser.parse_args(
        [
            "gate",
            "live",
            "--scheme-id",
            "trial",
            "--prediction-phase",
            "gray_live",
        ]
    )
    assert args.prediction_phase == "gray_live"


@pytest.mark.parametrize(
    "gate_name",
    ["static", "input", "unit", "dry-run", "compare", "dashboard"],
)
def test_read_only_gate_cli_rejects_operator(gate_name: str) -> None:
    from harness.cli import _build_parser

    with pytest.raises(SystemExit):
        _build_parser().parse_args(
            ["gate", gate_name, "--scheme-id", "trial", "--operator", "owner"]
        )


@pytest.mark.parametrize(
    ("gate_name", "extra"),
    [
        ("backtest", []),
        ("shadow-register", []),
        ("live", ["--prediction-phase", "gray_live"]),
        ("lifecycle-reconcile", []),
    ],
)
def test_side_effect_gate_cli_accepts_operator(
    gate_name: str,
    extra: list[str],
) -> None:
    from harness.cli import _build_parser

    args = _build_parser().parse_args(
        ["gate", gate_name, "--scheme-id", "trial", "--operator", "owner", *extra]
    )
    assert args.operator == "owner"
