"""`auth issue --scheme-version` 的解析与即时校验。

该值只有一个正确答案：Gate 使用 token 时会从同一份 config 重算 `scheme_version`
并逐字比对，填错必然被拦。因此默认从 config 解析；显式传入且不符时在签发这一刻失败，
而不是等到用 token 时才发现。
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from harness.authorization import parse_token
from harness.cli import main
from scheduler.discovery import load_scheme_config

SCHEME = "weekly_1y_causal_v1_31_0_standalone"


@pytest.fixture(autouse=True)
def _secret():
    with patch.dict(os.environ, {"HARNESS_AUTH_SECRET": "s" * 32}, clear=False):
        yield


def _declared_version() -> str:
    root = Path(__file__).resolve().parents[1]
    return load_scheme_config(root / "schemes" / SCHEME / "config.yaml").scheme_version


def _issue(capsys, *extra: str) -> str:
    assert (
        main(
            [
                "auth",
                "issue",
                "--scheme-id",
                SCHEME,
                "--action",
                "shadow_register",
                "--predict-date",
                "2026-08-22",
                "--harness-run-id",
                "hr_test",
                "--issued-by",
                "operator",
                *extra,
            ]
        )
        == 0
    )
    return capsys.readouterr().out.strip()


def test_scheme_version_is_resolved_from_config_when_omitted(capsys) -> None:
    token = _issue(capsys)
    assert parse_token(token).scheme_version == _declared_version()


def test_explicit_matching_value_still_works(capsys) -> None:
    """既有 runbook 里带着该参数的命令必须继续可用。"""
    declared = _declared_version()
    token = _issue(capsys, "--scheme-version", declared)
    assert parse_token(token).scheme_version == declared


def test_explicit_mismatch_fails_at_issue_time(capsys) -> None:
    with pytest.raises(SystemExit):
        _issue(capsys, "--scheme-version", "deadbeefcafe")
    assert "does not match the declared config" in capsys.readouterr().err


def test_blank_explicit_value_is_still_rejected(capsys) -> None:
    with pytest.raises(SystemExit):
        _issue(capsys, "--scheme-version", "   ")
    assert "non-empty --scheme-version" in capsys.readouterr().err


def test_unresolvable_scheme_reports_why(capsys) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "auth",
                "issue",
                "--scheme-id",
                "no_such_scheme_anywhere",
                "--action",
                "activate",
                "--issued-by",
                "operator",
            ]
        )
    assert "could not resolve --scheme-version" in capsys.readouterr().err


def test_issuing_stays_offline(capsys) -> None:
    """签发器不得因为解析版本而开始连数据库。"""
    import scheduler.repository as repository

    with patch.object(
        repository, "create_engine_from_env", side_effect=AssertionError("DB touched")
    ):
        token = _issue(capsys)
    assert parse_token(token).scheme_version == _declared_version()
