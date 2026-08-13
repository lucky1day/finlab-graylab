"""候选方案排行的紧凑列宽和备注详情纯展示契约。"""

from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "frontend"
INDEX = FRONTEND / "index.html"
JAVASCRIPT = FRONTEND / "aifin-shell.js"
STYLESHEET = FRONTEND / "aifin-shell.css"


def _function_body(name: str) -> str:
    javascript = JAVASCRIPT.read_text(encoding="utf-8")
    marker = f"function {name}"
    return javascript.split(marker, 1)[1].split("\n  function ", 1)[0]


def _column_width(column: int) -> int:
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    match = re.search(
        rf"\.factor-ranking-table th:nth-child\({column}\)\s*\{{[^}}]*"
        r"width:\s*(\d+)px;",
        stylesheet,
        re.S,
    )
    assert match is not None, f"ranking column {column} has no fixed width"
    return int(match.group(1))


def test_ranking_keeps_current_overall_accuracy_markup() -> None:
    body = _function_body("renderSchemeRankingRow")

    assert "factor-score-cell" in body
    assert "formatPercent(metric.overall)" in body
    assert "metric.correct + '/' + metricSamples" in body
    assert "factor-score-bar" in body
    assert "barWidth.toFixed(1)" in body


def test_only_requested_ranking_columns_are_compacted() -> None:
    assert _column_width(3) == 180
    assert _column_width(4) == 82
    assert _column_width(5) == 112
    assert _column_width(6) == 112
    assert _column_width(7) == 120
    assert _column_width(8) == 84
    assert _column_width(9) == 96


def test_ranking_uses_detail_button_instead_of_truncated_remark() -> None:
    body = _function_body("renderSchemeRankingRow")

    assert 'data-factor-remark-open="' in body
    assert 'aria-controls="factorRemarkPopover"' in body
    assert 'aria-expanded="false"' in body
    assert "factor-remark-detail" in body
    assert "factor-remark-text" not in body
    assert body.count("'<td") + body.count('"<td') == 9


def test_remark_popover_is_one_accessible_non_modal_dialog() -> None:
    html = INDEX.read_text(encoding="utf-8")

    assert html.count('id="factorRemarkPopover"') == 1
    assert 'role="dialog"' in html
    assert 'aria-modal="false"' in html
    assert 'aria-hidden="true"' in html
    assert 'aria-labelledby="factorRemarkTitle"' in html
    assert 'id="factorRemarkBody"' in html
    assert 'data-factor-remark-close' in html


def test_remark_trigger_precedes_row_selection_and_stops_bubbling() -> None:
    body = _function_body("bindFactorLabEvents")

    trigger_index = body.index("[data-factor-remark-open]")
    row_index = body.index("[data-factor-scheme-id]")
    assert trigger_index < row_index
    assert "event.preventDefault();" in body
    assert "event.stopPropagation();" in body
    assert "openFactorRemark" in body


def test_remark_popover_supports_all_dismissal_and_focus_paths() -> None:
    javascript = JAVASCRIPT.read_text(encoding="utf-8")

    assert 'document.addEventListener("click"' in javascript
    assert 'event.key === "Escape"' in javascript
    assert "closeFactorRemark(true)" in javascript
    assert "factorRemarkTrigger.focus()" in javascript
    assert "textContent = remark" in javascript


def test_remark_popover_is_readable_without_full_screen_backdrop() -> None:
    stylesheet = STYLESHEET.read_text(encoding="utf-8")

    popover = re.search(
        r"\.factor-remark-popover\s*\{(?P<body>[^}]*)\}",
        stylesheet,
        re.S,
    )
    body = re.search(
        r"\.factor-remark-popover-body\s*\{(?P<body>[^}]*)\}",
        stylesheet,
        re.S,
    )
    assert popover is not None
    assert body is not None
    assert "position: fixed;" in popover.group("body")
    assert "width: min(440px, calc(100vw - 24px));" in popover.group("body")
    assert "max-height: 190px;" in body.group("body")
    assert "font-size: 14px;" in body.group("body")
    assert "line-height: 1.75;" in body.group("body")
    assert ".factor-remark-backdrop" not in stylesheet
