from __future__ import annotations

import json

import pytest

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




def test_direct_operation_requires_canonical_dates() -> None:
    with pytest.raises(ValueError, match="canonical YYYY-MM-DD"):
        build_direct_operation(
            "trial_10y",
            "live_write",
            "2026-8-25",
            scheme_version="version-1",
            issued_by="operator",
        )
