"""副作用命令直接生成精确审计意图，不暴露密钥或 token 操作面。"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from harness.authorization import parse_token
from harness.cli import _build_parser, _direct_authorization, _gate_action
from scheduler.discovery import load_scheme_config


ROOT = Path(__file__).resolve().parents[1]
SCHEME = "weekly_1y_causal_v1_31_0_standalone"


def _config():
    return load_scheme_config(ROOT / "schemes" / SCHEME / "config.yaml")


def test_cli_has_no_auth_issue_or_authorize_argument() -> None:
    parser = _build_parser()
    commands = next(
        action for action in parser._actions if action.dest == "command"
    ).choices
    assert "auth" not in commands
    shadow = next(
        action
        for action in commands["gate"]._actions
        if action.dest == "gate_name"
    ).choices["shadow-register"]
    option_strings = {
        option
        for action in shadow._actions
        for option in action.option_strings
    }
    assert "--authorize" not in option_strings
    assert "--operator" in option_strings


def test_gate_action_only_marks_real_side_effects() -> None:
    assert _gate_action(Namespace(gate_name="shadow-register")) == "shadow_register"
    assert (
        _gate_action(Namespace(gate_name="lifecycle-bootstrap"))
        == "blackbox_lifecycle_bootstrap"
    )
    assert _gate_action(Namespace(gate_name="backtest", persist=True)) == "backtest_persist"
    assert _gate_action(Namespace(gate_name="backtest", persist=False)) is None
    assert _gate_action(Namespace(gate_name="dashboard")) is None


def test_direct_authorization_derives_version_and_operator(monkeypatch) -> None:
    monkeypatch.setenv("BFL_OPERATOR_ID", "solo-maintainer")
    operation = _direct_authorization(
        action="shadow_register",
        scheme_id=SCHEME,
        config=_config(),
        predict_date="2026-08-22",
        operator=None,
    )
    auth = parse_token(operation)
    assert auth.scheme_version == _config().scheme_version
    assert auth.issued_by == "solo-maintainer"
    assert auth.harness_run_id is None
    assert auth.predict_date == "2026-08-22"


def test_direct_authorization_rejects_invalid_config() -> None:
    try:
        _direct_authorization(
            action="activate",
            scheme_id="missing",
            config=None,
            predict_date=None,
            operator="solo-maintainer",
        )
    except SystemExit as exc:
        assert "valid canonical scheme config" in str(exc)
    else:
        raise AssertionError("invalid config must fail closed")
