from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "frontend"
JAVASCRIPT_PATH = FRONTEND / "aifin-shell.js"

_NODE_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const [shellPath, hookName, serializedArgs] = process.argv.slice(1);
const document = {
  visibilityState: "visible",
  getElementById() { return null; },
  querySelectorAll() { return []; },
  querySelector() { return null; },
  addEventListener() {}
};
const window = {
  location: { pathname: "/", origin: "http://localhost" },
  history: { pushState() {} },
  addEventListener() {}
};
const context = vm.createContext({ document, window, console, Promise, Map, Set });

vm.runInContext(fs.readFileSync(shellPath, "utf8"), context, { filename: shellPath });
const hook = window.__factorLabTestHooks[hookName];
if (typeof hook !== "function") throw new Error(`missing hook: ${hookName}`);
process.stdout.write(JSON.stringify(hook(...JSON.parse(serializedArgs))));
"""


def _call_hook(hook_name: str, *args: object) -> object:
    result = subprocess.run(
        [
            "node",
            "-e",
            _NODE_HARNESS,
            str(JAVASCRIPT_PATH),
            hook_name,
            json.dumps(args, ensure_ascii=False),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_scheme_total_badge_precedes_range_and_metric_badges() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    count_position = html.index('id="factorOverviewSchemeCount"')
    range_position = html.index('id="factorOverviewRange"')
    metric_position = html.index('id="factorOverviewMetric"')
    assert count_position < range_position < metric_position
    assert "方案总数：--" in html


def test_scheme_total_uses_all_task_counts_only_after_successful_load() -> None:
    tasks = {
        "1Y|T+1": [{"id": "a"}, {"id": "b"}],
        "5Y|weekly_point": [{"id": "c"}],
        "10Y|monthly": [],
    }
    successful = {
        "remoteLoaded": True,
        "remoteLoading": False,
        "apiError": "",
        "dataMode": "dashboard",
    }
    assert _call_hook(
        "factorLabSchemeTotalLabelForTest", tasks, successful
    ) == "方案总数：3"
    assert _call_hook(
        "factorLabSchemeTotalLabelForTest",
        tasks,
        {**successful, "startMonth": "2099-12", "dataSource": "live"},
    ) == "方案总数：3"
    assert _call_hook(
        "factorLabSchemeTotalLabelForTest", {}, successful
    ) == "方案总数：0"

    for unavailable in (
        {**successful, "remoteLoaded": False},
        {**successful, "remoteLoading": True},
        {**successful, "apiError": "dashboard unavailable"},
        {**successful, "dataMode": "loading"},
        {**successful, "dataMode": "error"},
    ):
        assert _call_hook(
            "factorLabSchemeTotalLabelForTest", {}, unavailable
        ) == "方案总数：--"


def test_task_cells_share_the_nonzero_error_state_contract() -> None:
    javascript = JAVASCRIPT_PATH.read_text(encoding="utf-8")
    overview_body = javascript.split("function renderTaskOverview()", 1)[1].split(
        "\n  function ", 1
    )[0]
    assert (
        "factorLabSchemeCountsAvailable(currentFactorLabDataState())"
        in overview_body
    )
    assert (
        'var schemeCount = schemeCountsAvailable ? schemes.length : "--";'
        in overview_body
    )
    assert "+ schemeCount + ' 个方案</span>'" in overview_body


def test_task_labels_change_display_text_without_changing_contract_ids() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    javascript = JAVASCRIPT_PATH.read_text(encoding="utf-8")
    assert "<th>周收盘</th>" in html
    assert "<th>月中收</th>" in html
    assert "<th>月均</th>" in html
    assert "<th>季均</th>" in html
    assert "<th>年均</th>" in html
    assert (
        'label: "周收盘", taskType: "weekly_point", frequency: "weekly"'
        in javascript
    )
    assert (
        'label: "月中收", taskType: "monthly", frequency: "monthly"'
        in javascript
    )
    assert 'label: "周平均", taskType: "weekly_average"' in javascript
    assert (
        'label: "月均", taskType: "monthly_average", frequency: "monthly"'
        in javascript
    )
    assert (
        'label: "季均", taskType: "quarterly_average", frequency: "quarterly"'
        in javascript
    )
    assert (
        'label: "年均", taskType: "annual_average", frequency: "annual"'
        in javascript
    )


def test_task_matrix_keeps_required_column_order_and_scroll_floor() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    stylesheet = (FRONTEND / "aifin-shell.css").read_text(encoding="utf-8")
    task_head = html.split('class="factor-task-table"', 1)[1].split(
        "</thead>", 1
    )[0]
    labels = [
        "Y标的",
        "T+1",
        "T+5",
        "周收盘",
        "周平均",
        "月中收",
        "月均",
        "季均",
        "年均",
    ]
    assert [task_head.index(f"<th>{label}</th>") for label in labels] == sorted(
        task_head.index(f"<th>{label}</th>") for label in labels
    )
    assert re.search(
        r"\.factor-task-table\s*\{[^}]*min-width:\s*980px;",
        stylesheet,
    )
    assert re.search(
        r"\.factor-task-wrap,[^}]*overflow-x:\s*auto;",
        stylesheet,
        flags=re.DOTALL,
    )


def test_ranking_sort_has_no_visible_state_but_keeps_accessible_state() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    javascript = JAVASCRIPT_PATH.read_text(encoding="utf-8")
    stylesheet = (FRONTEND / "aifin-shell.css").read_text(encoding="utf-8")
    ranking_head = html.split('class="factor-ranking-table"', 1)[1].split(
        "</thead>", 1
    )[0]
    update_body = javascript.split("function updateFactorFilterUi()", 1)[1].split(
        "\n  function ", 1
    )[0]

    assert "data-sort-direction" not in ranking_head
    assert "ASC" not in ranking_head and "DESC" not in ranking_head
    assert "factor-ranking-sort is-active" not in ranking_head
    assert ".factor-ranking-sort::after" not in stylesheet
    assert ".factor-ranking-sort.is-active" not in stylesheet
    assert "classList.toggle(\"is-active\", active)" not in update_body
    assert (
        'button.setAttribute("aria-sort", active ? ariaDirection : "none")'
        in update_body
    )
    assert (
        'factorLabState.rankDirection === "asc" ? "ascending" : "descending"'
        in update_body
    )


def test_click_sort_order_remains_ascending_and_descending() -> None:
    schemes = [
        {
            "id": "low",
            "monthlyRows": [],
            "dailyRowsByMonth": {
                "2025-01": [
                    {
                        "predictedDirection": 1,
                        "actualDirection": -1,
                        "_source": "backtest",
                    }
                ]
            },
        },
        {
            "id": "high",
            "monthlyRows": [],
            "dailyRowsByMonth": {
                "2025-01": [
                    {
                        "predictedDirection": 1,
                        "actualDirection": 1,
                        "_source": "backtest",
                    }
                ]
            },
        },
    ]
    descending = _call_hook("sortRankingSchemes", schemes, "overall", "desc")
    ascending = _call_hook("sortRankingSchemes", schemes, "overall", "asc")
    assert [item["id"] for item in descending] == ["high", "low"]
    assert [item["id"] for item in ascending] == ["low", "high"]


def test_overview_badges_wrap_on_desktop_and_align_on_narrow_layout() -> None:
    stylesheet = (FRONTEND / "aifin-shell.css").read_text(encoding="utf-8")
    meta_rule = re.search(r"\.factor-accuracy-meta\s*\{(?P<body>[^}]*)\}", stylesheet)
    assert meta_rule is not None
    assert "flex-wrap: wrap;" in meta_rule.group("body")
    narrow = stylesheet.split("@media (max-width: 980px)", 1)[1]
    assert re.search(
        r"\.factor-accuracy-meta\s*\{[^}]*justify-content:\s*flex-start;",
        narrow,
    )
