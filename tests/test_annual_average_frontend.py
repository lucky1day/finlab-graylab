from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
JAVASCRIPT_PATH = PROJECT_ROOT / "frontend" / "aifin-shell.js"
INDEX_PATH = PROJECT_ROOT / "frontend" / "index.html"

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


def _hook_payload(hook_name: str, *args: object) -> dict[str, object]:
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


def _call_hook(hook_name: str, *args: object) -> object:
    payload = _hook_payload(hook_name, *args)
    assert payload["ok"], payload["message"]
    return payload["value"]


def _call_hook_error(hook_name: str, *args: object) -> str:
    payload = _hook_payload(hook_name, *args)
    assert not payload["ok"], payload
    return str(payload["message"])


@pytest.mark.parametrize(
    ("target_month", "expected"),
    [("2025-01", "2025"), ("2026-02", "2026")],
)
def test_annual_average_target_year_format(
    target_month: str, expected: str
) -> None:
    assert _call_hook("formatAnnualAverageTargetYear", target_month) == expected


def test_annual_average_predict_date_format() -> None:
    assert _call_hook(
        "formatAnnualAveragePredictDate", "2026-02-13"
    ) == "02/13"


@pytest.mark.parametrize("value", ["", "2026", "2026-2", "2026-13", None])
def test_annual_average_target_year_rejects_invalid_values(
    value: str | None,
) -> None:
    assert "target year" in _call_hook_error(
        "formatAnnualAverageTargetYear", value
    )


@pytest.mark.parametrize("value", ["", "2026-2-13", "2026-02-30", None])
def test_annual_average_predict_date_rejects_invalid_values(
    value: str | None,
) -> None:
    assert "ISO date" in _call_hook_error(
        "formatAnnualAveragePredictDate", value
    )


def _decoded_dashboard() -> dict[str, object]:
    return {
        "snapshotId": "dashboard-v1-20260824T040000Z-a1b2c3d4e5f6",
        "generatedAt": "2026-08-24T04:00:00Z",
        "displayUntil": "2026-08-24",
        "stale": False,
        "snapshotAgeMs": 0,
        "targetLabels": {"1Y": "1年期"},
        "schemes": [
            {
                "schemeId": "annual-demo__h1__1Y",
                "baseSchemeId": "annual-demo",
                "targetTenor": "1Y",
                "taskType": "annual_average",
                "name": "年均示例",
                "owner": "tester",
                "description": "fixture",
                "status": "active",
                "signalStatus": "present",
                "signalFailureCategory": None,
                "deployedAt": "2026-01-01T00:00:00+08:00",
                "liveRows": [
                    {
                        "predictDate": "2026-02-13",
                        "featureDate": "2026-02-13",
                        "targetDate": "2026-02-14",
                        "predictionPhase": "gray_live",
                        "predictedDirection": 1,
                        "actualDirection": None,
                        "source": "live",
                    }
                ],
                "backtest": {
                    "benchmarkLabel": "canonical",
                    "dataSource": "wind",
                    "dataSourceLabel": "Wind",
                    "latestRunDate": "2026-08-24",
                    "rows": [
                        {
                            "predictDate": "2025-01-27",
                            "featureDate": "2025-01-27",
                            "targetDate": "2025-01-28",
                            "predictionPhase": "backtest",
                            "predictedDirection": -1,
                            "actualDirection": -1,
                            "source": "backtest",
                        }
                    ],
                },
            }
        ],
    }


def test_dashboard_annual_average_keeps_raw_keys_dates_and_actuals() -> None:
    view_model = _call_hook("buildFactorLabViewModel", _decoded_dashboard())
    scheme = view_model["tasks"]["1Y|annual_average"][0]
    assert [row["month"] for row in scheme["monthlyRows"]] == [
        "2025-01",
        "2026-02",
    ]
    backtest = scheme["dailyRowsByMonth"]["2025-01"][0]
    live = scheme["dailyRowsByMonth"]["2026-02"][0]
    assert backtest["predictDate"] == "2025-01-27"
    assert backtest["targetDate"] == "2025-01-28"
    assert backtest["targetMonth"] == "2025-01"
    assert backtest["actualDirection"] == -1
    assert backtest["correct"] is True
    assert live["predictDate"] == "2026-02-13"
    assert live["targetDate"] == "2026-02-14"
    assert live["targetMonth"] == "2026-02"
    assert live["actualDirection"] is None
    assert live["correct"] is None


def test_legacy_annual_average_keeps_raw_key_dates_and_pending_actual() -> None:
    registry_id = "annual-demo__h1__1Y"
    responses = {
        "/api/schemes": {
            "target_labels": {"1Y": "1年期"},
            "schemes": [
                {
                    "scheme_id": registry_id,
                    "base_scheme_id": "annual-demo",
                    "target_tenor": "1Y",
                    "task_type": "annual_average",
                    "frequency": "annual",
                    "horizon": 1,
                    "name": "年均示例",
                    "status": "active",
                    "deployed_at": "2026-01-01T00:00:00+08:00",
                }
            ],
        },
        f"/api/metrics/{registry_id}": {
            "daily_rows": [
                {
                    "predict_date": "2026-02-13",
                    "feature_date": "2026-02-13",
                    "target_date": "2026-02-14",
                    "prediction_phase": "gray_live",
                    "predicted_direction": 1,
                    "actual_direction": None,
                    "is_correct": None,
                }
            ],
            "monthly_metrics": [],
            "phase_ranges": [],
        },
    }
    view_model = _call_hook("buildLegacyFactorLabViewModelForTest", responses)
    scheme = view_model["tasks"]["1Y|annual_average"][0]
    assert [row["month"] for row in scheme["monthlyRows"]] == ["2026-02"]
    detail = scheme["dailyRowsByMonth"]["2026-02"][0]
    assert detail["predictDate"] == "2026-02-13"
    assert detail["targetDate"] == "2026-02-14"
    assert detail["targetMonth"] == "2026-02"
    assert detail["actualDirection"] is None
    assert detail["correct"] is None


def test_annual_average_presentation_uses_target_year_wording() -> None:
    presentation = _call_hook(
        "factorDetailPresentationForTest",
        {"taskType": "annual_average", "frequency": "annual"},
        "2026-02",
    )
    assert presentation == {
        "title": "2026 年度平均预测明细",
        "dateHeader": "目标年度",
        "note": "",
        "emptyText": "当前年度暂无预测明细",
        "buttonLabel": "打开年度平均预测明细",
    }


def test_factor_period_label_changes_annual_without_regressing_other_tasks() -> None:
    assert _call_hook(
        "formatFactorPeriodLabelForTest",
        {"taskType": "annual_average"},
        "2026-02",
    ) == "2026"
    assert _call_hook(
        "formatFactorPeriodLabelForTest",
        {"taskType": "quarterly_average"},
        "2026-04",
    ) == "2026/Q2"
    assert _call_hook(
        "formatFactorPeriodLabelForTest",
        {"taskType": "monthly_average"},
        "2026-06",
    ) == "2026-06"
    assert _call_hook(
        "formatFactorPeriodLabelForTest",
        {"taskType": "T+1"},
        "2026-06",
    ) == "2026-06"


def test_annual_average_live_divider_uses_target_year() -> None:
    scheme = {
        "deploymentDate": "2026/01/01",
        "dailyRowsByMonth": {
            "2026-02": [
                {
                    "_source": "live",
                    "predictDate": "2026-02-13",
                    "targetDate": "2026-02-14",
                    "targetMonth": "2026-02",
                    "predictionPhase": "gray_live",
                }
            ]
        },
    }
    task = {"taskType": "annual_average", "frequency": "annual"}
    assert _call_hook(
        "liveDividerTextForTest", scheme, task
    ) == "实盘预测目标区间：2026开始"


def test_annual_render_boundaries_use_target_year_formatters() -> None:
    javascript = JAVASCRIPT_PATH.read_text(encoding="utf-8")
    assert "formatFactorPeriodLabel(task, row.month)" in javascript
    assert "formatAnnualAveragePredictDate(row.predictDate)" in javascript
    assert "formatAnnualAverageTargetYear(row.targetMonth || month)" in javascript
    assert 'emptyText: "当前年度暂无预测明细"' in javascript
    assert 'buttonLabel: "打开年度平均预测明细"' in javascript


def test_index_cache_key_matches_annual_javascript() -> None:
    index = INDEX_PATH.read_text(encoding="utf-8")
    match = re.search(r'aifin-shell\.js\?v=([0-9a-f]{64})', index)
    assert match is not None
    expected = hashlib.sha256(JAVASCRIPT_PATH.read_bytes()).hexdigest()
    assert match.group(1) == expected
