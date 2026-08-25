"""生产前端资产不得内置或回退到模拟金融数据。"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHELL_JS = PROJECT_ROOT / "frontend" / "aifin-shell.js"
FORBIDDEN_EMBEDDED_DATA_IDENTIFIERS = (
    "factorDailyBaseRows",
    "factorWeeklyBaseRows",
    "factorDailyRows",
    "factorWeeklyRows",
)


def _source() -> str:
    return SHELL_JS.read_text(encoding="utf-8")


def test_no_mock_financial_data_remains() -> None:
    source = _source()
    present = [
        name
        for name in FORBIDDEN_EMBEDDED_DATA_IDENTIFIERS
        if re.search(rf"\b{name}\b", source)
    ]
    assert present == [], f"生产资产仍含内置模拟数据标识符: {present}"
    assert '"mock"' not in source
    assert not re.search(r'month:\s*"20\d\d-\d\d",\s*samples:', source)


def test_missing_detail_renders_empty_state() -> None:
    """缺失明细必须落到既有空态分支，而不是替代数据源。"""
    source = _source()
    anchor = source.index("var rows = scheme && scheme.dailyRowsByMonth")
    body = source[anchor : anchor + 500]
    assert ": [];" in body, body[:300]
    assert "当前月份暂无每日明细" in body


def test_dashboard_is_the_only_frontend_data_source() -> None:
    """浏览器不得恢复旧多接口聚合回退路径。"""
    source = _source()
    assert re.findall(r'fetchJson\(\s*"([^"]+)"', source) == [
        "/api/factor-lab/dashboard"
    ]
