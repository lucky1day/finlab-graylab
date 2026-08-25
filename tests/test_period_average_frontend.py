from __future__ import annotations

from pathlib import Path

import pytest

from tests.frontend_test_support import (
    call_frontend_hook as _call_hook,
    call_frontend_hook_error as _call_hook_error,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
JAVASCRIPT_PATH = PROJECT_ROOT / "frontend" / "aifin-shell.js"

PERIOD_CASES = [
    {
        "task_type": "monthly_average",
        "frequency": "monthly",
        "name": "月均示例",
        "predict_date": "2026-01-15",
        "target_date": "2026-01-16",
        "period_key": "2026-02",
        "presentation": {
            "title": "2026-02 月度平均预测明细",
            "dateHeader": "目标月",
            "note": "",
            "emptyText": "当前月份暂无预测明细",
            "buttonLabel": "打开月度平均预测明细",
        },
        "divider": "实盘预测目标区间：2026-02开始",
    },
    {
        "task_type": "quarterly_average",
        "frequency": "quarterly",
        "name": "季均示例",
        "predict_date": "2026-03-31",
        "target_date": "2026-04-01",
        "period_key": "2026-04",
        "presentation": {
            "title": "2026/Q2 季度平均预测明细",
            "dateHeader": "目标季度",
            "note": "",
            "emptyText": "当前季度暂无预测明细",
            "buttonLabel": "打开季度平均预测明细",
        },
        "divider": "实盘预测目标区间：2026/Q2开始",
    },
    {
        "task_type": "annual_average",
        "frequency": "annual",
        "name": "年均示例",
        "predict_date": "2026-02-13",
        "target_date": "2026-02-14",
        "period_key": "2026-02",
        "presentation": {
            "title": "2026 年度平均预测明细",
            "dateHeader": "目标年度",
            "note": "",
            "emptyText": "当前年度暂无预测明细",
            "buttonLabel": "打开年度平均预测明细",
        },
        "divider": "实盘预测目标区间：2026开始",
    },
]


def _dashboard(case: dict[str, object]) -> dict[str, object]:
    task_type = str(case["task_type"])
    return {
        "snapshotId": "dashboard-v1-20260824T000000Z-a1b2c3d4e5f6",
        "generatedAt": "2026-08-24T00:00:00Z",
        "displayUntil": "2026-08-24",
        "stale": False,
        "snapshotAgeMs": 0,
        "targetLabels": {"1Y": "1年期"},
        "schemes": [
            {
                "schemeId": f"{task_type}-demo__h1__1Y",
                "baseSchemeId": f"{task_type}-demo",
                "targetTenor": "1Y",
                "taskType": task_type,
                "name": case["name"],
                "owner": "tester",
                "description": "fixture",
                "status": "active",
                "signalStatus": "present",
                "signalFailureCategory": None,
                "deployedAt": "2026-01-01T00:00:00+08:00",
                "liveRows": [
                    {
                        "predictDate": case["predict_date"],
                        "featureDate": case["predict_date"],
                        "targetDate": case["target_date"],
                        "predictionPhase": "gray_live",
                        "predictedDirection": 1,
                        "actualDirection": -1,
                        "source": "live",
                    }
                ],
                "backtest": None,
            }
        ],
    }


def _legacy_responses(case: dict[str, object]) -> dict[str, object]:
    task_type = str(case["task_type"])
    registry_id = f"{task_type}-demo__h1__1Y"
    return {
        "/api/schemes": {
            "target_labels": {"1Y": "1年期"},
            "schemes": [
                {
                    "scheme_id": registry_id,
                    "base_scheme_id": f"{task_type}-demo",
                    "target_tenor": "1Y",
                    "task_type": task_type,
                    "frequency": case["frequency"],
                    "horizon": 1,
                    "name": case["name"],
                    "status": "active",
                    "deployed_at": "2026-01-01T00:00:00+08:00",
                }
            ],
        },
        f"/api/metrics/{registry_id}": {
            "daily_rows": [
                {
                    "predict_date": case["predict_date"],
                    "feature_date": case["predict_date"],
                    "target_date": case["target_date"],
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


@pytest.mark.parametrize(
    ("hook", "value", "expected"),
    [
        ("monthlyAverageTargetMonth", "2026-01-16", "2026-02"),
        ("monthlyAverageTargetMonth", "2026-12-16", "2027-01"),
        ("formatMonthlyAveragePredictDate", "2026-06-15", "06/15"),
        ("formatMonthlyAverageTargetMonth", "2025-02", "2025/02"),
        ("formatQuarterlyAverageTargetQuarter", "2025-01", "2025/Q1"),
        ("formatQuarterlyAverageTargetQuarter", "2026-10", "2026/Q4"),
        ("formatQuarterlyAveragePredictDate", "2026-03-31", "03/31"),
        ("formatAnnualAverageTargetYear", "2025-01", "2025"),
        ("formatAnnualAverageTargetYear", "2026-02", "2026"),
        ("formatAnnualAveragePredictDate", "2026-02-13", "02/13"),
    ],
)
def test_period_average_formatters(
    hook: str,
    value: str,
    expected: str,
) -> None:
    assert _call_hook(hook, value) == expected


@pytest.mark.parametrize(
    ("hook", "invalid_values", "message"),
    [
        ("monthlyAverageTargetMonth", ["", "2026-02-30", "2026-1-16", None], "ISO date"),
        ("formatMonthlyAveragePredictDate", ["", "2026-6-15", "2026-02-30", None], "ISO date"),
        ("formatMonthlyAverageTargetMonth", ["", "2025-2", "2025-13", None], "target month"),
        ("formatQuarterlyAverageTargetQuarter", ["", "2026-02", "2026-05", "2026-13", None], "quarter target month"),
        ("formatQuarterlyAveragePredictDate", ["", "2026-3-31", "2026-02-30", None], "ISO date"),
        ("formatAnnualAverageTargetYear", ["", "2026", "2026-2", "2026-13", None], "target year"),
        ("formatAnnualAveragePredictDate", ["", "2026-2-13", "2026-02-30", None], "ISO date"),
    ],
)
def test_period_average_formatters_reject_invalid_values(
    hook: str,
    invalid_values: list[str | None],
    message: str,
) -> None:
    for value in invalid_values:
        assert message in _call_hook_error(hook, value)


@pytest.mark.parametrize("case", PERIOD_CASES, ids=lambda case: case["task_type"])
def test_dashboard_period_average_rows(case: dict[str, object]) -> None:
    view_model = _call_hook("buildFactorLabViewModel", _dashboard(case))
    task_key = f"1Y|{case['task_type']}"
    scheme = view_model["tasks"][task_key][0]
    period_key = str(case["period_key"])
    assert [row["month"] for row in scheme["monthlyRows"]] == [period_key]
    detail = scheme["dailyRowsByMonth"][period_key][0]
    assert detail["predictDate"] == case["predict_date"]
    assert detail["targetDate"] == case["target_date"]
    assert detail["targetMonth"] == period_key
    assert detail["predictedDirection"] == 1
    assert detail["actualDirection"] == -1
    assert detail["correct"] is False


@pytest.mark.parametrize("case", PERIOD_CASES, ids=lambda case: case["task_type"])
def test_legacy_period_average_rows(case: dict[str, object]) -> None:
    view_model = _call_hook(
        "buildLegacyFactorLabViewModelForTest",
        _legacy_responses(case),
    )
    task_key = f"1Y|{case['task_type']}"
    scheme = view_model["tasks"][task_key][0]
    period_key = str(case["period_key"])
    assert [row["month"] for row in scheme["monthlyRows"]] == [period_key]
    detail = scheme["dailyRowsByMonth"][period_key][0]
    assert detail["predictDate"] == case["predict_date"]
    assert detail["targetDate"] == case["target_date"]
    assert detail["targetMonth"] == period_key


@pytest.mark.parametrize("case", PERIOD_CASES, ids=lambda case: case["task_type"])
def test_period_average_presentation_and_live_divider(
    case: dict[str, object],
) -> None:
    task = {
        "taskType": case["task_type"],
        "frequency": case["frequency"],
    }
    assert _call_hook(
        "factorDetailPresentationForTest",
        task,
        case["period_key"],
    ) == case["presentation"]
    scheme = {
        "deploymentDate": "2026/01/01",
        "dailyRowsByMonth": {
            case["period_key"]: [
                {
                    "_source": "live",
                    "predictDate": case["predict_date"],
                    "targetDate": case["target_date"],
                    "targetMonth": case["period_key"],
                    "predictionPhase": "gray_live",
                }
            ]
        },
    }
    assert _call_hook("liveDividerTextForTest", scheme, task) == case["divider"]


@pytest.mark.parametrize(
    ("task_type", "period_key", "expected"),
    [
        ("monthly_average", "2026-04", "2026-04"),
        ("quarterly_average", "2026-04", "2026/Q2"),
        ("annual_average", "2026-02", "2026"),
        ("T+1", "2026-06", "2026-06"),
    ],
)
def test_factor_period_labels(
    task_type: str,
    period_key: str,
    expected: str,
) -> None:
    assert _call_hook(
        "formatFactorPeriodLabelForTest",
        {"taskType": task_type},
        period_key,
    ) == expected


def test_monthly_average_rejects_cross_source_display_month_conflict() -> None:
    decoded = _dashboard(PERIOD_CASES[0])
    decoded["schemes"][0]["backtest"] = {
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


def test_quarterly_average_rejects_non_quarter_start_target() -> None:
    assert "quarter target month" in _call_hook_error(
        "targetDisplayMonth", "2026-05-01", "quarterly_average"
    )
    decoded = _dashboard(PERIOD_CASES[1])
    decoded["schemes"][0]["liveRows"][0]["targetDate"] = "2026-05-01"
    assert "quarter target month" in _call_hook_error(
        "buildFactorLabViewModel", decoded
    )


def test_period_average_render_boundaries() -> None:
    html = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    javascript = JAVASCRIPT_PATH.read_text(encoding="utf-8")
    monthly_table_head = html.split('class="factor-month-table"', 1)[1].split(
        "</thead>", 1
    )[0]
    assert "<th>预测明细</th>" in monthly_table_head
    assert "<th>每日明细</th>" not in monthly_table_head
    for snippet in (
        "formatFactorPeriodLabel(task, row.month)",
        "formatMonthlyAveragePredictDate(row.predictDate)",
        "formatMonthlyAverageTargetMonth(row.targetMonth || month)",
        "formatQuarterlyAveragePredictDate(row.predictDate)",
        "formatQuarterlyAverageTargetQuarter(row.targetMonth || month)",
        "formatAnnualAveragePredictDate(row.predictDate)",
        "formatAnnualAverageTargetYear(row.targetMonth || month)",
    ):
        assert snippet in javascript
