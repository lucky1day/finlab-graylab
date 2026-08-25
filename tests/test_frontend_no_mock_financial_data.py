"""生产前端资产不得内置模拟金融数据。

真实明细缺失时，日/周表格原本回落到内置的 2025 年模拟 rows。空状态表达「未知」，
模拟结果表达「一个伪业务事实」——对金融页面而言二者不是等价降级：读者会把一组
看起来合理的预测、实际方向和结果当成真实业绩。

模拟数据与生产 runtime 共用同一个静态 JS 资产，没有不可逾越的测试边界，因此只能
从资产中彻底移除。
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHELL_JS = PROJECT_ROOT / "frontend" / "aifin-shell.js"

MOCK_IDENTIFIERS = (
    "factorDailyBaseRows",
    "factorWeeklyBaseRows",
    "factorDailyRows",
    "factorWeeklyRows",
)


def _source() -> str:
    return SHELL_JS.read_text(encoding="utf-8")


def test_no_mock_financial_data_remains() -> None:
    source = _source()
    present = [name for name in MOCK_IDENTIFIERS if re.search(rf"\b{name}\b", source)]
    assert present == [], f"生产资产仍含模拟数据标识符: {present}"
    assert '"mock"' not in source
    assert not re.search(r'month:\s*"20\d\d-\d\d",\s*samples:', source)


def test_missing_detail_renders_empty_state() -> None:
    """缺失明细必须落到既有空态分支，而不是任何替代数据源。

    定位渲染函数里那次带 dataSource 过滤的取值——另一处同名下标出现在
    「有月度指标却无明细行」的校验器里，不是本测试的目标。
    """
    source = _source()
    anchor = source.index("var rows = scheme && scheme.dailyRowsByMonth")
    body = source[anchor : anchor + 500]
    assert ": [];" in body, body[:300]
    assert "当前月份暂无每日明细" in body
