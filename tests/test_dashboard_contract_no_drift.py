"""Dashboard 后端权威字段与无构建前端的必要副本不得漂移。"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from backend import factor_lab_dashboard_semantics as semantics

SHELL_JS = PROJECT_ROOT / "frontend" / "aifin-shell.js"
PUBLIC_CHECK = PROJECT_ROOT / "scripts" / "check_public_access.sh"


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
        ("DASHBOARD_ROW_FIELDS", semantics.ROW_FIELDS),
        ("DASHBOARD_BACKTEST_FIELDS", semantics.BACKTEST_FIELDS),
        ("DASHBOARD_TASK_TYPES", semantics.VALID_TASK_TYPES),
        ("DASHBOARD_SIGNAL_STATUSES", semantics.VALID_SIGNAL_STATUSES),
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
