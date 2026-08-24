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
    [
        ("2025-01", "2025/Q1"),
        ("2026-04", "2026/Q2"),
        ("2026-07", "2026/Q3"),
        ("2026-10", "2026/Q4"),
    ],
)
def test_quarterly_average_target_quarter_format(
    target_month: str, expected: str
) -> None:
    assert _call_hook(
        "formatQuarterlyAverageTargetQuarter", target_month
    ) == expected


def test_quarterly_average_predict_date_format() -> None:
    assert _call_hook(
        "formatQuarterlyAveragePredictDate", "2026-03-31"
    ) == "03/31"


@pytest.mark.parametrize("value", ["", "2026-02", "2026-05", "2026-13", None])
def test_quarterly_average_target_quarter_rejects_invalid_values(
    value: str | None,
) -> None:
    assert "quarter target month" in _call_hook_error(
        "formatQuarterlyAverageTargetQuarter", value
    )


@pytest.mark.parametrize("value", ["", "2026-3-31", "2026-02-30", None])
def test_quarterly_average_predict_date_rejects_invalid_values(
    value: str | None,
) -> None:
    assert "ISO date" in _call_hook_error(
        "formatQuarterlyAveragePredictDate", value
    )


def test_quarterly_target_display_month_rejects_non_quarter_start() -> None:
    assert "quarter target month" in _call_hook_error(
        "targetDisplayMonth", "2026-05-01", "quarterly_average"
    )


def _decoded_dashboard() -> dict[str, object]:
    row = {
        "predictDate": "2026-03-31",
        "featureDate": "2026-03-31",
        "targetDate": "2026-04-01",
        "predictionPhase": "gray_live",
        "predictedDirection": 1,
        "actualDirection": -1,
        "source": "live",
    }
    return {
        "snapshotId": "dashboard-v1-20260824T010000Z-a1b2c3d4e5f6",
        "generatedAt": "2026-08-24T01:00:00Z",
        "displayUntil": "2026-08-24",
        "stale": False,
        "snapshotAgeMs": 0,
        "targetLabels": {"1Y": "1年期"},
        "schemes": [
            {
                "schemeId": "quarterly-demo__h1__1Y",
                "baseSchemeId": "quarterly-demo",
                "targetTenor": "1Y",
                "taskType": "quarterly_average",
                "name": "季均示例",
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


def test_dashboard_quarterly_average_keeps_raw_key_and_dates() -> None:
    view_model = _call_hook("buildFactorLabViewModel", _decoded_dashboard())
    scheme = view_model["tasks"]["1Y|quarterly_average"][0]
    assert [row["month"] for row in scheme["monthlyRows"]] == ["2026-04"]
    detail = scheme["dailyRowsByMonth"]["2026-04"][0]
    assert detail["predictDate"] == "2026-03-31"
    assert detail["targetDate"] == "2026-04-01"
    assert detail["targetMonth"] == "2026-04"
    assert detail["predictedDirection"] == 1
    assert detail["actualDirection"] == -1
    assert detail["correct"] is False


def test_dashboard_quarterly_average_rejects_non_quarter_start_target() -> None:
    decoded = _decoded_dashboard()
    decoded["schemes"][0]["liveRows"][0]["targetDate"] = "2026-05-01"
    assert "quarter target month" in _call_hook_error(
        "buildFactorLabViewModel", decoded
    )


def test_legacy_quarterly_average_keeps_raw_key_and_dates() -> None:
    registry_id = "quarterly-demo__h1__1Y"
    responses = {
        "/api/schemes": {
            "target_labels": {"1Y": "1年期"},
            "schemes": [
                {
                    "scheme_id": registry_id,
                    "base_scheme_id": "quarterly-demo",
                    "target_tenor": "1Y",
                    "task_type": "quarterly_average",
                    "frequency": "quarterly",
                    "horizon": 1,
                    "name": "季均示例",
                    "status": "active",
                    "deployed_at": "2026-01-01T00:00:00+08:00",
                }
            ],
        },
        f"/api/metrics/{registry_id}": {
            "daily_rows": [
                {
                    "predict_date": "2026-03-31",
                    "feature_date": "2026-03-31",
                    "target_date": "2026-04-01",
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
    scheme = view_model["tasks"]["1Y|quarterly_average"][0]
    assert [row["month"] for row in scheme["monthlyRows"]] == ["2026-04"]
    detail = scheme["dailyRowsByMonth"]["2026-04"][0]
    assert detail["predictDate"] == "2026-03-31"
    assert detail["targetDate"] == "2026-04-01"
    assert detail["targetMonth"] == "2026-04"


def test_quarterly_average_presentation_uses_target_quarter_wording() -> None:
    presentation = _call_hook(
        "factorDetailPresentationForTest",
        {"taskType": "quarterly_average", "frequency": "quarterly"},
        "2026-04",
    )
    assert presentation == {
        "title": "2026/Q2 季度平均预测明细",
        "dateHeader": "目标季度",
        "note": "",
        "emptyText": "当前季度暂无预测明细",
        "buttonLabel": "打开季度平均预测明细",
    }


def test_factor_period_label_changes_only_quarterly_average() -> None:
    assert _call_hook(
        "formatFactorPeriodLabelForTest",
        {"taskType": "quarterly_average"},
        "2026-04",
    ) == "2026/Q2"
    assert _call_hook(
        "formatFactorPeriodLabelForTest",
        {"taskType": "monthly_average"},
        "2026-04",
    ) == "2026-04"
    assert _call_hook(
        "formatFactorPeriodLabelForTest",
        {"taskType": "T+1"},
        "2026-04",
    ) == "2026-04"


def test_quarterly_average_live_divider_uses_target_quarter() -> None:
    scheme = {
        "deploymentDate": "2026/01/01",
        "dailyRowsByMonth": {
            "2026-07": [
                {
                    "_source": "live",
                    "predictDate": "2026-06-30",
                    "targetDate": "2026-07-01",
                    "targetMonth": "2026-07",
                    "predictionPhase": "gray_live",
                }
            ]
        },
    }
    task = {"taskType": "quarterly_average", "frequency": "quarterly"}
    assert _call_hook(
        "liveDividerTextForTest", scheme, task
    ) == "实盘预测目标区间：2026/Q3开始"


def test_quarterly_render_boundaries_use_target_quarter_formatters() -> None:
    javascript = JAVASCRIPT_PATH.read_text(encoding="utf-8")
    assert "formatFactorPeriodLabel(task, row.month)" in javascript
    assert "formatQuarterlyAveragePredictDate(row.predictDate)" in javascript
    assert (
        "formatQuarterlyAverageTargetQuarter(row.targetMonth || month)"
        in javascript
    )
    assert 'emptyText: "当前季度暂无预测明细"' in javascript
    assert 'buttonLabel: "打开季度平均预测明细"' in javascript


def test_index_cache_key_matches_quarterly_javascript() -> None:
    index = INDEX_PATH.read_text(encoding="utf-8")
    match = re.search(r'aifin-shell\.js\?v=([0-9a-f]{64})', index)
    assert match is not None
    expected = hashlib.sha256(JAVASCRIPT_PATH.read_bytes()).hexdigest()
    assert match.group(1) == expected
