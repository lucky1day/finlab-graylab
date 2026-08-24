from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
JAVASCRIPT_PATH = PROJECT_ROOT / "frontend" / "aifin-shell.js"

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
try {
  const hook = window.__factorLabTestHooks[hookName];
  if (typeof hook !== "function") throw new Error(`missing hook: ${hookName}`);
  const value = hook(...JSON.parse(serializedArgs));
  process.stdout.write(JSON.stringify({ ok: true, value }));
} catch (error) {
  process.stdout.write(JSON.stringify({ ok: false, message: String(error.message || error) }));
}
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
    payload = json.loads(result.stdout)
    assert payload["ok"], payload["message"]
    return payload["value"]


def _call_hook_error(hook_name: str, *args: object) -> str:
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
    payload = json.loads(result.stdout)
    assert not payload["ok"], payload
    return payload["message"]


@pytest.mark.parametrize(
    ("target_date", "expected"),
    [
        ("2026-01-16", "2026-02"),
        ("2026-04-16", "2026-05"),
        ("2026-12-16", "2027-01"),
    ],
)
def test_monthly_average_target_month_uses_next_natural_month(
    target_date: str, expected: str
) -> None:
    assert _call_hook("monthlyAverageTargetMonth", target_date) == expected


@pytest.mark.parametrize("target_date", ["", "2026-02-30", "2026-1-16", None])
def test_monthly_average_target_month_rejects_invalid_dates(
    target_date: str | None,
) -> None:
    assert "ISO date" in _call_hook_error(
        "monthlyAverageTargetMonth", target_date
    )


def test_monthly_average_detail_formats_predict_date_and_target_month() -> None:
    assert _call_hook(
        "formatMonthlyAveragePredictDate", "2026-06-15"
    ) == "06/15"
    assert _call_hook(
        "formatMonthlyAverageTargetMonth", "2025-02"
    ) == "2025/02"


@pytest.mark.parametrize("value", ["", "2026-6-15", "2026-02-30", None])
def test_monthly_average_predict_date_format_rejects_invalid_values(
    value: str | None,
) -> None:
    assert "ISO date" in _call_hook_error(
        "formatMonthlyAveragePredictDate", value
    )


@pytest.mark.parametrize("value", ["", "2025-2", "2025-13", None])
def test_monthly_average_target_month_format_rejects_invalid_values(
    value: str | None,
) -> None:
    assert "target month" in _call_hook_error(
        "formatMonthlyAverageTargetMonth", value
    )


def test_non_monthly_task_keeps_raw_target_month() -> None:
    assert _call_hook(
        "targetDisplayMonth", "2026-01-16", "quarterly_average"
    ) == "2026-01"


def _decoded_dashboard() -> dict[str, object]:
    row = {
        "predictDate": "2026-01-15",
        "featureDate": "2026-01-15",
        "targetDate": "2026-01-16",
        "predictionPhase": "gray_live",
        "predictedDirection": 1,
        "actualDirection": -1,
        "source": "live",
    }
    return {
        "snapshotId": "dashboard-v1-20260824T000000Z-a1b2c3d4e5f6",
        "generatedAt": "2026-08-24T00:00:00Z",
        "displayUntil": "2026-08-24",
        "stale": False,
        "snapshotAgeMs": 0,
        "targetLabels": {"1Y": "1年期"},
        "schemes": [
            {
                "schemeId": "monthly-demo__h1__1Y",
                "baseSchemeId": "monthly-demo",
                "targetTenor": "1Y",
                "taskType": "monthly_average",
                "name": "月均示例",
                "owner": "tester",
                "description": "fixture",
                "status": "active",
                "signalStatus": "present",
                "signalFailureCategory": None,
                "deployedAt": "2026-01-01T00:00:00+08:00",
                "liveRows": [row],
                "backtest": None,
            }
        ],
    }


def test_dashboard_monthly_average_groups_by_display_target_month() -> None:
    view_model = _call_hook("buildFactorLabViewModel", _decoded_dashboard())
    scheme = view_model["tasks"]["1Y|monthly_average"][0]
    assert [row["month"] for row in scheme["monthlyRows"]] == ["2026-02"]
    detail = scheme["dailyRowsByMonth"]["2026-02"][0]
    assert detail["targetDate"] == "2026-01-16"
    assert detail["targetMonth"] == "2026-02"
    assert detail["predictedDirection"] == 1
    assert detail["actualDirection"] == -1
    assert detail["correct"] is False


def test_dashboard_monthly_average_rejects_cross_source_display_month_conflict() -> None:
    decoded = _decoded_dashboard()
    scheme = decoded["schemes"][0]
    scheme["backtest"] = {
        "benchmarkLabel": "fixture",
        "dataSourceLabel": "fixture",
        "latestRunDate": "2026-01-15",
        "rows": [
            {
                "predictDate": "2026-01-15",
                "featureDate": "2026-01-15",
                "targetDate": "2026-01-15",
                "predictionPhase": None,
                "predictedDirection": -1,
                "actualDirection": -1,
                "source": "backtest",
            }
        ],
    }
    assert "conflicting backtest/live target month 2026-02" in _call_hook_error(
        "buildFactorLabViewModel", decoded
    )


def test_legacy_monthly_average_groups_by_same_display_target_month() -> None:
    registry_id = "monthly-demo__h1__1Y"
    responses = {
        "/api/schemes": {
            "target_labels": {"1Y": "1年期"},
            "schemes": [
                {
                    "scheme_id": registry_id,
                    "base_scheme_id": "monthly-demo",
                    "target_tenor": "1Y",
                    "task_type": "monthly_average",
                    "frequency": "monthly",
                    "horizon": 1,
                    "name": "月均示例",
                    "status": "active",
                    "deployed_at": "2026-01-01T00:00:00+08:00",
                }
            ],
        },
        f"/api/metrics/{registry_id}": {
            "daily_rows": [
                {
                    "predict_date": "2026-01-15",
                    "feature_date": "2026-01-15",
                    "target_date": "2026-01-16",
                    "prediction_phase": "gray_live",
                    "predicted_direction": 1,
                    "actual_direction": -1,
                    "is_correct": False,
                }
            ],
            "monthly_metrics": [],
            "phase_ranges": [],
        },
    }
    view_model = _call_hook("buildLegacyFactorLabViewModelForTest", responses)
    scheme = view_model["tasks"]["1Y|monthly_average"][0]
    assert [row["month"] for row in scheme["monthlyRows"]] == ["2026-02"]
    detail = scheme["dailyRowsByMonth"]["2026-02"][0]
    assert detail["targetDate"] == "2026-01-16"
    assert detail["targetMonth"] == "2026-02"


def test_monthly_average_presentation_uses_target_month_wording() -> None:
    presentation = _call_hook(
        "factorDetailPresentationForTest",
        {"taskType": "monthly_average", "frequency": "monthly"},
        "2026-02",
    )
    assert presentation == {
        "title": "2026-02 月度平均预测明细",
        "dateHeader": "目标月",
        "note": "",
        "emptyText": "当前月份暂无预测明细",
        "buttonLabel": "打开月度平均预测明细",
    }


def test_monthly_average_live_divider_uses_display_target_month() -> None:
    scheme = {
        "deploymentDate": "2026/01/01",
        "dailyRowsByMonth": {
            "2026-02": [
                {
                    "_source": "live",
                    "predictDate": "2026-01-15",
                    "targetDate": "2026-01-16",
                    "targetMonth": "2026-02",
                    "predictionPhase": "gray_live",
                }
            ]
        },
    }
    task = {"taskType": "monthly_average", "frequency": "monthly"}
    assert _call_hook(
        "liveDividerTextForTest", scheme, task
    ) == "实盘预测目标区间：2026-02开始"


def test_static_monthly_average_copy_has_no_daily_detail_label() -> None:
    html = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    javascript = JAVASCRIPT_PATH.read_text(encoding="utf-8")
    monthly_table_head = html.split('class="factor-month-table"', 1)[1].split(
        "</thead>", 1
    )[0]
    assert "<th>预测明细</th>" in monthly_table_head
    assert "<th>每日明细</th>" not in monthly_table_head
    assert "查看' + escapeHtml(row.month) + '每日明细" not in javascript
    assert "formatMonthlyAveragePredictDate(row.predictDate)" in javascript
    assert (
        "formatMonthlyAverageTargetMonth(row.targetMonth || month)"
        in javascript
    )
