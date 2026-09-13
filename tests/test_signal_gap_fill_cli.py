from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from harness import cli


PREDICT_DATE = "2026-08-10"


def _plan(
    *,
    action: str = "GRAY_LIVE_GAP",
    base_scheme_id: str | None = None,
    status: str = "READY",
) -> dict[str, object]:
    return {
        "status": status,
        "failure_code": "INPUT_AUTHORITY_BLOCKED" if status == "BLOCKED" else None,
        "base_scheme_id": base_scheme_id,
        "counts": {
            "expected": int(action not in {"", "SKIP_NOT_DUE"}),
            "actionable": int(action == "GRAY_LIVE_GAP"),
            "blocked": int(status == "BLOCKED"),
        },
    }


def _run_cli(
    tmp_path: Path,
    plan: dict[str, object],
    *,
    scheme_id: str | None = None,
    fill_report: dict[str, object] | None = None,
) -> tuple[int, dict[str, object], Mock, Mock]:
    output = io.StringIO()
    planner = Mock(return_value=plan)
    runner = Mock(
        return_value=fill_report
        or {
            "schema_version": "single-date-signal-gap-fill-v2",
            "status": "PASSED",
            "failure_code": None,
            "completed": [{"base_scheme_id": "demo_native"}],
            "remaining": [],
        }
    )
    databridge_config = SimpleNamespace(name="config")
    argv = [
        "signal-gap-fill",
        "--predict-date",
        PREDICT_DATE,
        "--project-root",
        str(tmp_path),
    ]
    if scheme_id is not None:
        argv.extend(["--scheme-id", scheme_id])
    with (
        patch.object(
            cli.DataBridgeRefreshConfig,
            "from_env",
            return_value=databridge_config,
        ),
        patch.object(cli, "_plan_signal_gap_date", planner),
        patch.object(cli, "run_signal_gap_fill", runner, create=True),
        redirect_stdout(output),
    ):
        exit_code = cli.main(argv)
    return (
        exit_code,
        json.loads(output.getvalue()),
        planner,
        runner,
    )


def test_parser_accepts_date_and_optional_exact_base_scheme() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(
        [
            "signal-gap-fill",
            "--predict-date",
            PREDICT_DATE,
            "--scheme-id",
            "demo_native",
        ]
    )

    assert args.predict_date == PREDICT_DATE
    assert args.scheme_id == "demo_native"

    for invalid in (
        ["--scheme-id", "demo_native__h5__5Y"],
        ["--scheme-id", "demo_native,other"],
        ["--scheme-id", "demo_native", "--scheme-id", "other"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["signal-gap-fill", "--predict-date", PREDICT_DATE, *invalid]
            )


def test_parser_accepts_exact_target_range_and_rejects_mixed_scope() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(
        [
            "signal-gap-fill",
            "--target-date-from",
            "2026-06-01",
            "--target-date-before",
            "2026-09-04",
            "--scheme-id",
            "demo_blackbox",
        ]
    )

    assert args.predict_date is None
    assert args.target_date_from == "2026-06-01"
    assert args.target_date_before == "2026-09-04"
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "signal-gap-fill",
                "--predict-date",
                PREDICT_DATE,
                "--target-date-from",
                "2026-06-01",
            ]
        )


def test_target_range_requires_before_and_exact_scheme(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="target range requires"):
        cli.main(
            [
                "signal-gap-fill",
                "--target-date-from",
                "2026-06-01",
                "--project-root",
                str(tmp_path),
            ]
        )


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [
        ("SKIP_NOT_DUE", "SKIP_NOT_DUE"),
        ("SKIP_PRESENT", "SKIP_PRESENT"),
    ],
)
def test_not_due_or_present_exits_without_algorithm_or_write(
    tmp_path: Path,
    action: str,
    expected_status: str,
) -> None:
    plan = _plan(action=action, base_scheme_id="demo_native")

    exit_code, payload, planner, runner = _run_cli(
        tmp_path,
        plan,
        scheme_id="demo_native",
    )

    assert exit_code == 0
    assert payload["status"] == expected_status
    planner.assert_called_once()
    runner.assert_not_called()


def test_blocked_plan_exits_without_coordinator(tmp_path: Path) -> None:
    plan = _plan(status="BLOCKED", base_scheme_id="demo_native")

    exit_code, payload, planner, runner = _run_cli(
        tmp_path,
        plan,
        scheme_id="demo_native",
    )

    assert exit_code == 2
    assert payload["status"] == "BLOCKED"
    assert payload["failure_code"] == "INPUT_AUTHORITY_BLOCKED"
    planner.assert_called_once()
    runner.assert_not_called()


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [("FAILED", 1), ("BLOCKED", 2)],
)
def test_coordinator_status_maps_directly_without_retry(
    tmp_path: Path,
    status: str,
    expected_exit: int,
) -> None:
    report = {
        "schema_version": "single-date-signal-gap-fill-v2",
        "status": status,
        "failure_code": "TEST_FAILURE",
        "completed": [],
        "remaining": [{"base_scheme_id": "demo_native"}],
    }

    exit_code, payload, planner, runner = _run_cli(
        tmp_path,
        _plan(),
        fill_report=report,
    )

    assert exit_code == expected_exit
    assert payload["status"] == status
    assert payload["failure_code"] == "TEST_FAILURE"
    planner.assert_called_once()
    runner.assert_called_once()
