from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from harness.cli import _build_parser, _run_activate, _run_gate
from harness.operation import (
    build_direct_operation,
    operation_scope_sha256,
    verify_direct_operation,
    write_operation_audit,
)


def test_direct_operation_binds_latest_exact_harness_run(tmp_path) -> None:
    operation = build_direct_operation(
        "trial_10y",
        "blackbox_activate",
        scheme_version="version-1",
        issued_by="operator",
    )

    bound, errors = verify_direct_operation(
        operation,
        scheme_id="trial_10y",
        action="blackbox_activate",
        scheme_version="version-1",
        harness_run_id="hr-passed",
    )

    assert errors == []
    assert bound is not None
    assert bound.harness_run_id == "hr-passed"
    path = write_operation_audit(bound, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["operation_mode"] == "direct_operator_command_v2"
    assert payload["operation_scope_sha256"] == operation_scope_sha256(bound)
    assert "token" not in payload


def test_direct_operation_rejects_scope_mismatch() -> None:
    operation = build_direct_operation(
        "trial_10y",
        "live_write",
        "2026-08-25",
        scheme_version="version-1",
        issued_by="operator",
    )

    bound, errors = verify_direct_operation(
        operation,
        scheme_id="other_10y",
        action="live_write",
        predict_date="2026-08-25",
        scheme_version="version-1",
    )

    assert bound is None
    assert any("scheme_id mismatch" in error for error in errors)


def test_direct_operation_requires_canonical_dates() -> None:
    with pytest.raises(ValueError, match="canonical YYYY-MM-DD"):
        build_direct_operation(
            "trial_10y",
            "live_write",
            "2026-8-25",
            scheme_version="version-1",
            issued_by="operator",
        )


def test_activate_cli_does_not_expose_an_ignored_predict_date() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "activate",
                "--scheme-id",
                "trial_10y",
                "--predict-date",
                "2026-08-25",
            ]
        )


def test_gate_cli_passes_the_requested_predict_date(tmp_path) -> None:
    args = _build_parser().parse_args(
        [
            "gate",
            "input",
            "--scheme-id",
            "trial_10y",
            "--predict-date",
            "2026-08-25",
            "--project-root",
            str(tmp_path),
        ]
    )
    cfg = SimpleNamespace(
        runtime_type="native_adapter",
        scheme_version="version-1",
    )
    gate = SimpleNamespace(run=lambda ctx: ctx)
    with (
        patch("harness.cli._load_config_for_dispatch", return_value=cfg),
        patch("harness.cli.gate_for_name", return_value=gate),
    ):
        ctx = _run_gate(args)
    assert ctx.predict_date == "2026-08-25"


def test_activate_cli_uses_internal_non_date_context(tmp_path) -> None:
    args = _build_parser().parse_args(
        [
            "activate",
            "--scheme-id",
            "trial_10y",
            "--project-root",
            str(tmp_path),
        ]
    )
    cfg = SimpleNamespace(
        runtime_type="blackbox_v2",
        scheme_version="version-1",
    )
    with (
        patch("harness.cli._load_config_for_dispatch", return_value=cfg),
        patch("harness.cli.ActivationGate.run", side_effect=lambda ctx: ctx),
    ):
        ctx = _run_activate(args)
    assert ctx.predict_date == "activate"
    assert ctx.operation.predict_date is None
