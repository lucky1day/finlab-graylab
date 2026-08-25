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
    marker = f"function {name}("
    return javascript.split(marker, 1)[1].split("\n  function ", 1)[0]


def _ranking_header_cells() -> list[str]:
    html = INDEX.read_text(encoding="utf-8")
    table = html.split('class="factor-ranking-table"', 1)[1]
    thead = table.split("</thead>", 1)[0]
    return re.findall(r"<th[^>]*>(.*?)</th>", thead, re.S)


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


def test_ranking_keeps_current_metric_markup_without_retired_badges() -> None:
    javascript = JAVASCRIPT.read_text(encoding="utf-8")
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    body = _function_body("renderSchemeRankingRow")

    for snippet in (
        "factor-score-cell",
        "formatPercent(metric.overall)",
        "metric.correct + '/' + metricSamples",
        "factor-score-bar",
        "barWidth.toFixed(1)",
        'class="factor-sample-count"',
    ):
        assert snippet in body
    for retired_rule in (
        "lowSampleThresholdForTask",
        "isLowSampleMetric",
        "factor-sample-badge",
        "样本不足",
        "factor-signal-missing",
        "信号缺失",
    ):
        assert retired_rule not in javascript
        assert retired_rule not in stylesheet


def test_only_requested_ranking_columns_are_compacted() -> None:
    assert [_column_width(column) for column in range(3, 10)] == [
        180,
        82,
        112,
        112,
        120,
        84,
        96,
    ]


def test_ranking_table_header_rows_and_empty_state_stay_aligned() -> None:
    header_cells = _ranking_header_cells()
    assert any("来源" in cell for cell in header_cells), header_cells

    row_body = _function_body("renderSchemeRankingRow")
    assert row_body.count("'<td") + row_body.count('"<td') == len(header_cells)

    empty_body = _function_body("renderSchemeRanking")
    match = re.search(
        r'colspan="(\d+)" class="factor-empty-cell"',
        empty_body,
    )
    assert match is not None
    assert int(match.group(1)) == len(header_cells)


def test_ranking_uses_detail_button_instead_of_truncated_remark() -> None:
    body = _function_body("renderSchemeRankingRow")

    assert 'data-factor-remark-open="' in body
    assert 'aria-controls="factorRemarkPopover"' in body
    assert 'aria-expanded="false"' in body
    assert "factor-remark-detail" in body
    assert "factor-remark-text" not in body


def test_remark_popover_is_one_accessible_non_modal_dialog() -> None:
    html = INDEX.read_text(encoding="utf-8")

    assert html.count('id="factorRemarkPopover"') == 1
    assert 'role="dialog"' in html
    assert 'aria-modal="false"' in html
    assert 'aria-hidden="true"' in html
    assert 'aria-labelledby="factorRemarkTitle"' in html
    assert 'id="factorRemarkBody"' in html
    assert 'data-factor-remark-close' in html


def test_remark_popover_is_outside_animated_view_container() -> None:
    html = INDEX.read_text(encoding="utf-8")

    shell_end = html.index("    </div>\n    <aside")
    popover_index = html.index('id="factorRemarkPopover"')
    assert popover_index > shell_end


def test_remark_trigger_precedes_row_selection_and_stops_bubbling() -> None:
    body = _function_body("bindFactorLabEvents")

    trigger_index = body.index("[data-factor-remark-open]")
    row_index = body.index("[data-factor-scheme-id]")
    assert trigger_index < row_index
    assert "event.preventDefault();" in body
    assert "event.stopPropagation();" in body
    assert "openFactorRemark" in body


def test_remark_popover_supports_dismissal_and_focus_paths() -> None:
    javascript = JAVASCRIPT.read_text(encoding="utf-8")

    for snippet in (
        'document.addEventListener("click"',
        'event.key === "Escape"',
        "closeFactorRemark(true)",
        'event.target.closest("[data-factor-remark-close]")',
        "window.requestAnimationFrame",
        "trigger.focus({ preventScroll: true })",
        "textContent = remark",
    ):
        assert snippet in javascript


def test_remark_popover_position_is_clamped_to_viewport() -> None:
    body = _function_body("placeFactorRemarkPopover")

    assert "Math.max(margin, top)" in body
    assert "window.innerHeight - height - margin" in body


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
