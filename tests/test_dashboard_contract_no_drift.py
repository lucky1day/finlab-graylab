"""Dashboard 后端权威字段与无构建前端的必要副本不得漂移。"""

from __future__ import annotations

import ast
from datetime import date, datetime
import json
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from backend import factor_lab_dashboard_semantics as semantics

SHELL_JS = PROJECT_ROOT / "frontend" / "aifin-shell.js"
PUBLIC_CHECK = PROJECT_ROOT / "scripts" / "check_public_access.sh"
DASHBOARD_BUILDER = PROJECT_ROOT / "backend" / "factor_lab_dashboard.py"


def _js_literal(name: str) -> set[str]:
    """取前端某个顶层字符串数组字面量。"""
    match = re.search(
        r"\bvar\s+" + re.escape(name) + r"\s*=\s*(\[[^\]]*\])", SHELL_JS.read_text(encoding="utf-8")
    )
    if match is None:
        raise AssertionError(f"{SHELL_JS.name} 未定义 {name}")
    return set(json.loads(match.group(1)))


def _public_check_literal(name: str) -> set[str]:
    match = re.search(
        r"^" + re.escape(name) + r"\s*=\s*(\{.*?^\})",
        PUBLIC_CHECK.read_text(encoding="utf-8"),
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"{PUBLIC_CHECK.name} 未定义 {name}")
    return set(ast.literal_eval(match.group(1)))


def test_frontend_dashboard_contract_matches_backend() -> None:
    contracts = (
        ("DASHBOARD_SCHEME_FIELDS", semantics.SCHEME_FIELDS),
        ("DASHBOARD_TOP_FIELDS", semantics.TOP_LEVEL_FIELDS),
        ("DASHBOARD_DETAIL_TOP_FIELDS", semantics.DETAIL_TOP_LEVEL_FIELDS),
        ("DASHBOARD_DETAIL_ROW_FIELDS", semantics.DETAIL_ROW_FIELDS),
        ("DASHBOARD_MONTHLY_ROW_FIELDS", semantics.MONTHLY_ROW_FIELDS),
        ("DASHBOARD_BACKTEST_FIELDS", semantics.BACKTEST_FIELDS),
        ("DASHBOARD_PHASE_RANGE_FIELDS", semantics.PHASE_RANGE_FIELDS),
        ("DASHBOARD_TASK_TYPES", semantics.VALID_TASK_TYPES),
    )
    for javascript_name, backend_values in contracts:
        assert _js_literal(javascript_name) == set(backend_values), javascript_name


def test_public_check_dashboard_contract_matches_backend() -> None:
    contracts = (
        ("DASHBOARD_TOP_FIELDS", semantics.TOP_LEVEL_FIELDS),
        ("DASHBOARD_SCHEME_FIELDS", semantics.SCHEME_FIELDS),
        ("DASHBOARD_BACKTEST_FIELDS", semantics.BACKTEST_FIELDS),
    )
    for shell_name, backend_values in contracts:
        assert _public_check_literal(shell_name) == set(backend_values), shell_name


def test_dashboard_v4_removes_signal_state_from_all_public_contracts() -> None:
    removed_fields = {"signal_status", "signal_failure_category"}
    assert removed_fields.isdisjoint(semantics.SCHEME_FIELDS)
    assert removed_fields.isdisjoint(_js_literal("DASHBOARD_SCHEME_FIELDS"))
    assert removed_fields.isdisjoint(_public_check_literal("DASHBOARD_SCHEME_FIELDS"))

    shell_text = SHELL_JS.read_text(encoding="utf-8")
    public_check_text = PUBLIC_CHECK.read_text(encoding="utf-8")
    assert "factor-lab-dashboard-v4" in shell_text
    assert "factor-lab-dashboard-v3" not in shell_text
    assert "factor-lab-dashboard-v4" in public_check_text
    assert "factor-lab-dashboard-v3" not in public_check_text


def test_dashboard_builder_queries_business_results_once_without_run_or_calendar() -> None:
    source = DASHBOARD_BUILDER.read_text(encoding="utf-8")
    lowered = source.casefold()

    assert lowered.count("from t_scheme_predictions") == 1
    for forbidden_table in (
        "t_scheme_runs",
        "api_wind_date",
        "t_trade_calendar",
    ):
        assert forbidden_table not in lowered


def test_compact_row_validation_preserves_order_and_uniqueness_contract() -> None:
    rows = [
        ["backtest", "2026-08-01", "2026-07-31", "2026-08-03", None, 1, -1],
        ["backtest", "2026-08-02", "2026-08-01", "2026-08-04", None, -1, 1],
    ]

    semantics._validate_compact_rows(rows, source="backtest", context="rows")

    with pytest.raises(semantics.DashboardDataError, match="not canonically sorted"):
        semantics._validate_compact_rows(
            list(reversed(rows)),
            source="backtest",
            context="rows",
        )
    with pytest.raises(semantics.DashboardDataError, match="duplicate"):
        semantics._validate_compact_rows(
            [rows[0], list(rows[0])],
            source="backtest",
            context="rows",
        )
    invalid_date = list(rows[0])
    invalid_date[1] = "2026-02-30"
    with pytest.raises(semantics.DashboardDataError, match="predict_date"):
        semantics._validate_compact_rows(
            [invalid_date],
            source="backtest",
            context="rows",
        )
    for invalid_value in (date(2026, 8, 1), datetime(2026, 8, 1), None):
        invalid_type = list(rows[0])
        invalid_type[1] = invalid_value
        with pytest.raises(semantics.DashboardDataError, match="predict_date"):
            semantics._validate_compact_rows(
                [invalid_type],
                source="backtest",
                context="rows",
            )
