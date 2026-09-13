"""Blackbox 激活入口只接受磁盘 canonical 与精确操作范围。"""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from harness.context import GateContext
from harness.gates.activate_gate import ActivationGate
from harness.operation import build_direct_operation
from harness.result import GateResult, GateStatus
from scheduler.discovery import load_scheme_config
from tests.test_blackbox_v2_discovery import _write_blackbox_scheme


@pytest.fixture
def activation_context(tmp_path):
    scheme = _write_blackbox_scheme(tmp_path / "schemes")
    cfg = load_scheme_config(scheme / "config.yaml")
    operation = build_direct_operation(
        cfg.scheme_id, "blackbox_activate", scheme_version=cfg.scheme_version,
        issued_by="test-operator",
    )
    return GateContext(
        scheme_id=cfg.scheme_id, predict_date="activate", project_root=tmp_path,
        config=cfg, operation=operation,
    )


def test_activation_dispatches_fresh_canonical_and_preserves_operation(activation_context):
    expected = GateResult("activate", GateStatus.PASSED, [], [], "start", "finish")
    with patch("harness.blackbox_v2.activation.activate_blackbox", return_value=expected) as run:
        result = ActivationGate().run(activation_context)
    assert result is expected
    actual = run.call_args.args[0]
    assert actual.config == activation_context.config
    assert actual.config is not activation_context.config
    assert actual.operation == activation_context.operation


@pytest.mark.parametrize("field,value", [
    ("scheme_id", "other"), ("scheme_version", "stale-version"),
    ("action", "backtest_persist"),
])
def test_activation_rejects_wrong_operation_before_side_effects(activation_context, field, value):
    ctx = replace(activation_context, operation=replace(activation_context.operation, **{field: value}))
    with patch("harness.blackbox_v2.activation.activate_blackbox") as run:
        result = ActivationGate().run(ctx)
    assert result.status == GateStatus.BLOCKED
    assert any("mismatch" in error for error in result.errors)
    run.assert_not_called()


def test_activation_rejects_changed_canonical(activation_context):
    path = activation_context.config.path / "config.yaml"
    path.write_text(path.read_text().replace("timeout_sec: 3600", "timeout_sec: 3500"))
    with patch("harness.blackbox_v2.activation.activate_blackbox") as run:
        result = ActivationGate().run(activation_context)
    assert result.status == GateStatus.BLOCKED
    run.assert_not_called()


def test_activation_rejects_invalid_canonical(activation_context):
    (activation_context.config.path / "config.yaml").write_text("- invalid\n")
    with patch("harness.blackbox_v2.activation.activate_blackbox") as run:
        result = ActivationGate().run(activation_context)
    assert result.status == GateStatus.FAILED
    run.assert_not_called()


def test_activation_rejects_native_without_database():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    native = next(
        path for path in (root / "schemes").glob("*/config.yaml")
        if load_scheme_config(path).runtime_type == "native_adapter"
    )
    cfg = load_scheme_config(native)
    ctx = GateContext(cfg.scheme_id, "activate", root, config=cfg)
    with patch("harness.blackbox_v2.activation.activate_blackbox") as run:
        result = ActivationGate().run(ctx)
    assert result.status == GateStatus.BLOCKED
    assert any("Native activation is retired" in error for error in result.errors)
    run.assert_not_called()


def test_activate_cli_binds_exact_canonical(activation_context):
    from harness.cli import _build_parser, _run_activate

    args = _build_parser().parse_args([
        "activate", "--scheme-id", activation_context.scheme_id,
        "--project-root", str(activation_context.project_root),
        "--operator", "cli-operator",
    ])
    expected = GateResult("activate", GateStatus.PASSED, [], [], "start", "finish")
    with patch("harness.blackbox_v2.activation.activate_blackbox", return_value=expected) as run:
        assert _run_activate(args) is expected
    ctx = run.call_args.args[0]
    assert ctx.operation.scheme_id == activation_context.scheme_id
    assert ctx.operation.scheme_version == activation_context.config.scheme_version
    assert ctx.operation.action == "blackbox_activate"
    assert ctx.operation.issued_by == "cli-operator"


def test_activate_cli_rejects_invalid_config_before_dispatch(activation_context):
    from harness.cli import _build_parser, _run_activate

    (activation_context.config.path / "config.yaml").write_text("- invalid\n")
    args = _build_parser().parse_args([
        "activate", "--scheme-id", activation_context.scheme_id,
        "--project-root", str(activation_context.project_root),
    ])
    with patch("harness.blackbox_v2.activation.activate_blackbox") as run:
        with pytest.raises(SystemExit, match="strict discovery"):
            _run_activate(args)
    run.assert_not_called()
