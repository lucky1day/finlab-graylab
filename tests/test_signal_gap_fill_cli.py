from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import pytest

from harness import cli


PREDICT_DATE = "2026-08-10"


def _plan(
    *,
    action: str = "GRAY_LIVE_GAP",
    base_scheme_id: str | None = None,
    status: str = "READY",
) -> dict[str, object]:
    actions = []
    if action:
        actions.append(
            {
                "registry_scheme_id": "demo_native__h5__5Y",
                "base_scheme_id": "demo_native",
                "runtime_type": "native_adapter",
                "frequency": "daily",
                "task_type": "T+5",
                "target_tenor": "5Y",
                "horizon": 5,
                "predict_date": PREDICT_DATE,
                "feature_date": "2026-08-07",
                "target_date": "2026-08-14",
                "prediction_phase": "gray_live",
                "scheme_version": "version-1",
                "business_key": [
                    "demo_native",
                    "5Y",
                    5,
                    "2026-08-14",
                ],
                "action": action,
                "reason": "test",
                "business_key_present": action == "SKIP_PRESENT",
                "input_authority": None,
            }
        )
    return {
        "schema_version": "single-date-active-live-gap-plan-v1",
        "status": status,
        "failure_code": "INPUT_AUTHORITY_BLOCKED" if status == "BLOCKED" else None,
        "predict_date": PREDICT_DATE,
        "base_scheme_id": base_scheme_id,
        "counts": {
            "active_target": len(actions),
            "expected": int(action not in {"", "SKIP_NOT_DUE"}),
            "present": int(action == "SKIP_PRESENT"),
            "actionable": int(action == "GRAY_LIVE_GAP"),
            "blocked": int(status == "BLOCKED"),
            "SKIP_PRESENT": int(action == "SKIP_PRESENT"),
            "SKIP_NOT_DUE": int(action == "SKIP_NOT_DUE"),
            "GRAY_LIVE_GAP": int(action == "GRAY_LIVE_GAP"),
            "BLOCKED_NO_GENERATION": 0,
            "BLOCKED_DATA_CONTRACT": 0,
        },
        "actions": actions,
    }


def _run_cli(
    tmp_path: Path,
    plan: dict[str, object],
    *,
    scheme_id: str | None = None,
    fill_report: dict[str, object] | None = None,
) -> tuple[int, dict[str, object], Mock, Mock, list[Path]]:
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
    report_root = tmp_path / "reports" / "harness" / "signal-gap-fill"
    report_dirs = list(report_root.iterdir()) if report_root.exists() else []
    return (
        exit_code,
        json.loads(output.getvalue()),
        planner,
        runner,
        report_dirs,
    )


def test_parser_exposes_only_date_and_optional_exact_base_scheme() -> None:
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
    for removed in ("plan", "authorize", "algo_env", "issued_by"):
        assert not hasattr(args, removed)

    for invalid in (
        ["--scheme-id", "demo_native__h5__5Y"],
        ["--scheme-id", "demo_native,other"],
        ["--scheme-id", "demo_native", "--scheme-id", "other"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["signal-gap-fill", "--predict-date", PREDICT_DATE, *invalid]
            )






@pytest.mark.parametrize("scheme_id", [None, "demo_native"])
def test_cli_plans_once_for_all_or_one_base_then_runs_coordinator(
    tmp_path: Path,
    scheme_id: str | None,
) -> None:
    plan = _plan(base_scheme_id=scheme_id)

    exit_code, payload, planner, runner, report_dirs = _run_cli(
        tmp_path,
        plan,
        scheme_id=scheme_id,
    )

    assert exit_code == 0
    assert payload["status"] == "PASSED"
    planner.assert_called_once_with(
        PREDICT_DATE,
        scheme_id,
        databridge_config=ANY,
    )
    runner.assert_called_once_with(
        plan=plan,
        project_root=tmp_path.resolve(),
        engine_factory=cli.create_engine_from_env,
        databridge_config=ANY,
        algo_env="forecast_env",
        timeout_sec=600,
    )
    assert len(report_dirs) == 1
    assert (report_dirs[0] / "signal_gap_plan.json").is_file()
    assert not (report_dirs[0] / "frozen_plan.json").exists()
    assert not (report_dirs[0] / "post_fill_plan.json").exists()




@pytest.mark.parametrize(
    ("action", "expected_status"),
    [
        ("SKIP_NOT_DUE", "SKIP_NOT_DUE"),
        ("SKIP_PRESENT", "SKIP_PRESENT"),
        ("", "SKIP_NOT_DUE"),
    ],
)
def test_not_due_or_present_exits_without_algorithm_or_write(
    tmp_path: Path,
    action: str,
    expected_status: str,
) -> None:
    plan = _plan(action=action, base_scheme_id="demo_native")

    exit_code, payload, planner, runner, _ = _run_cli(
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

    exit_code, payload, planner, runner, report_dirs = _run_cli(
        tmp_path,
        plan,
        scheme_id="demo_native",
    )

    assert exit_code == 2
    assert payload["status"] == "BLOCKED"
    assert payload["failure_code"] == "INPUT_AUTHORITY_BLOCKED"
    planner.assert_called_once()
    runner.assert_not_called()
    assert (report_dirs[0] / "signal_gap_plan.json").is_file()




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

    exit_code, payload, planner, runner, _ = _run_cli(
        tmp_path,
        _plan(),
        fill_report=report,
    )

    assert exit_code == expected_exit
    assert payload["status"] == status
    assert payload["failure_code"] == "TEST_FAILURE"
    planner.assert_called_once()
    runner.assert_called_once()
