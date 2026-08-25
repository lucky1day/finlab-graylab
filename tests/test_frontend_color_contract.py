from __future__ import annotations

from pathlib import Path
import re

import pytest

from tests.frontend_test_support import call_frontend_hook as _call_hook


PROJECT_ROOT = Path(__file__).resolve().parents[1]
JAVASCRIPT_PATH = PROJECT_ROOT / "frontend" / "aifin-shell.js"
STYLESHEET_PATH = PROJECT_ROOT / "frontend" / "aifin-shell.css"

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "metric-low"),
        (59.9, "metric-low"),
        (60.0, "metric-high"),
        (60.1, "metric-high"),
        (60.6, "metric-high"),
        (44.5, "metric-low"),
        (60.9, "metric-high"),
        (58.3, "metric-low"),
        (52.6, "metric-low"),
        (33.3, "metric-low"),
        (100.0, "metric-high"),
        (None, "metric-empty"),
        ("", "metric-empty"),
        ("--", "metric-empty"),
        ("invalid", "metric-empty"),
    ],
)
def test_percentage_color_uses_sixty_percent_two_band_rule(
    value: object, expected: str
) -> None:
    assert _call_hook("getMetricClassForTest", value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("涨", "direction-up"),
        ("跌", "direction-down"),
        ("平", "direction-neutral"),
        (None, "direction-neutral"),
        ("", "direction-neutral"),
        ("--", "direction-neutral"),
        ("未知", "direction-neutral"),
    ],
)
def test_daily_direction_color_uses_a_share_semantics(
    value: object, expected: str
) -> None:
    assert _call_hook("getDirectionClassForTest", value) == expected


@pytest.mark.parametrize(
    ("value", "css_class", "symbol"),
    [
        (True, "is-correct", "✓"),
        (False, "is-wrong", "×"),
        (None, "is-neutral", "?"),
        ("?", "is-neutral", "?"),
        ("--", "is-neutral", "?"),
        ("unknown", "is-neutral", "?"),
    ],
)
def test_daily_result_keeps_correctness_color_semantics(
    value: object, css_class: str, symbol: str
) -> None:
    html = _call_hook("renderDailyResultForTest", {"correct": value})
    assert isinstance(html, str)
    assert css_class in html
    assert symbol in html


def test_ranking_examples_use_shared_percentage_classes() -> None:
    html = _call_hook(
        "renderSchemeRankingRowForTest",
        {
            "id": "scheme-a",
            "name": "方案A",
            "deploymentDate": "2026/08/21",
            "owner": "平台",
        },
        0,
        {
            "overall": 67.3,
            "correct": 2,
            "metricSamples": 3,
            "samples": 3,
            "upPrecision": 59.2,
            "downPrecision": 73.7,
        },
    )
    assert isinstance(html, str)
    assert 'class="metric-high"' in html
    assert 'class="metric-low"' in html
    assert html.count('class="metric-high"') == 2
    assert "metric-warn" not in html


def _rule_body(stylesheet: str, selectors: tuple[str, ...]) -> str:
    selector_pattern = r"\s*,\s*".join(re.escape(item) for item in selectors)
    match = re.search(selector_pattern + r"\s*\{(?P<body>[^}]*)\}", stylesheet)
    assert match is not None, selectors
    return match.group("body")


def test_target_components_reuse_scoped_red_green_gray_classes() -> None:
    javascript = JAVASCRIPT_PATH.read_text(encoding="utf-8")
    stylesheet = STYLESHEET_PATH.read_text(encoding="utf-8")

    assert '<span class="factor-task-top' in javascript
    assert "getMetricClass(metricValue)" in javascript
    assert javascript.count("getMetricClass(metric.overall)") >= 1
    for metric_name in (
        "row.overall",
        "row.upPrecision",
        "row.upRecall",
        "row.downPrecision",
        "row.downRecall",
    ):
        assert f"getMetricClass({metric_name})" in javascript

    target_selectors = {
        "metric-high": "var(--negative)",
        "metric-low": "var(--accent)",
        "metric-empty": "var(--text-tertiary)",
    }
    for class_name, color in target_selectors.items():
        body = _rule_body(
            stylesheet,
            (
                f".factor-task-top.{class_name}",
                f".factor-ranking-table .{class_name}",
                f".factor-month-table .{class_name}",
            ),
        )
        assert f"color: {color};" in body

    assert "metric-warn" not in javascript
    assert "metric-warn" not in stylesheet
    for rule in re.finditer(
        r"(?P<selectors>[^{}]+)\{(?P<body>[^}]*)\}", stylesheet
    ):
        if "font-weight" in rule.group("body"):
            assert "metric-empty" not in rule.group("selectors")


def test_direction_result_and_trend_colors_remain_independent() -> None:
    javascript = JAVASCRIPT_PATH.read_text(encoding="utf-8")
    stylesheet = STYLESHEET_PATH.read_text(encoding="utf-8")

    assert "color: var(--negative);" in _rule_body(
        stylesheet, (".factor-daily-table .direction-up",)
    )
    assert "color: var(--accent);" in _rule_body(
        stylesheet, (".factor-daily-table .direction-down",)
    )
    assert "color: var(--text-tertiary);" in _rule_body(
        stylesheet, (".factor-daily-table .direction-neutral",)
    )
    assert "background: var(--accent);" in _rule_body(
        stylesheet, (".factor-result-dot.is-correct",)
    )
    assert "background: var(--negative);" in _rule_body(
        stylesheet, (".factor-result-dot.is-wrong",)
    )

    for color in ("#15623f", "#2f7ba1", "#b98728", "#d62828", "#6f5aa8"):
        assert color in javascript
