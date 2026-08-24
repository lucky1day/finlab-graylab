# 年均前端与目标年度 2026 缺口修复 Implementation Plan

> **状态：已完成并完成跨主机闭环。** 前端提交 `154f4bd` 与 live scope 提交 `053d562` 已组成 exact release `053d562fc40d7ecf5596f56f1beb00e4a3b58178`。五条目标年度 2026 Prediction 已在 ECS 和 Mac3 存在；Mac3 使用本地 run `3680–3684`，未复制 ECS run ID。2026 年度尚未结束，Actual 保持 `null/待验证`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让年均汇总、趋势和详情抽屉统一显示简洁目标年度，补齐五个期限的目标年度 2026 live Prediction，并在一个 immutable release 中完成 ECS 前端与数据闭环。

**Architecture:** 保留 API、数据库和前端 view model 中的 raw `predict_date`、`feature_date`、`target_date` 以及内部 `YYYY-MM` key，只在 `annual_average` 文字渲染边界提取年份。`signal-gap-plan` 只为年均任务把 raw target pointer 与平台 live start 映射为年度后比较；月均、季均和普通任务现有分支保持不变。所有业务写入继续通过既有 insert-only `signal-gap-fill`，发布只构建和激活一个精确 release。

**Tech Stack:** 原生 JavaScript、Python 3.12、pytest、Node.js `vm` 测试 harness、FastAPI 静态前端、MySQL 8、ECS systemd、immutable source release scripts。

---

### Task 1: 用失败测试锁定年均前端契约

**Files:**
- Create: `tests/test_annual_average_frontend.py`
- Reference: `tests/test_quarterly_average_frontend.py`
- Test: `tests/test_annual_average_frontend.py`

- [ ] **Step 1: 建立 Node hook harness 与年均格式化测试**

在新测试文件中写入独立 Node `vm` harness，并添加以下测试：

```python
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
```

- [ ] **Step 2: 锁定 Dashboard raw key、原始日期与 Actual 状态**

在同一文件加入完整 Dashboard fixture；2025 行带 Actual，2026 行保持 `null`：

```python
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
```

再加入 legacy candidate 的等价断言，确保 fallback 加载路径不改写日期或内部 key：

```python
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
```

- [ ] **Step 3: 锁定年均文案、周期标签与实盘分隔**

```python
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
```

- [ ] **Step 4: 锁定渲染边界与最终 cache hash**

```python
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
```

- [ ] **Step 5: 运行测试并确认红灯原因正确**

Run:

```bash
pytest -q tests/test_annual_average_frontend.py
```

Expected: FAIL；失败原因仅为年均 hook、年均展示分支或新 cache hash 尚未实现，不得是 Node harness 或 fixture 解析错误。

### Task 2: 实现年均前端格式化与展示边界

**Files:**
- Modify: `frontend/aifin-shell.js`
- Modify: `frontend/index.html`
- Test: `tests/test_annual_average_frontend.py`
- Test: `tests/test_quarterly_average_frontend.py`
- Test: `tests/test_monthly_average_frontend_target_month.py`

- [ ] **Step 1: 增加年均格式函数、任务判断和最小测试 hook**

在现有季度 helper 附近加入：

```javascript
function formatAnnualAveragePredictDate(predictDate) {
  var value = requireDashboardIsoDate(
    predictDate,
    "annual_average predict_date"
  );
  return value.slice(5, 7) + "/" + value.slice(8, 10);
}

function formatAnnualAverageTargetYear(targetMonth) {
  var value = String(targetMonth || "").trim();
  var match = value.match(/^(\d{4})-(0[1-9]|1[0-2])$/);
  if (!match) {
    throw dashboardDataError(
      "annual_average target year key must use YYYY-MM"
    );
  }
  return match[1];
}

function isAnnualAverageTask(task) {
  return task && task.taskType === "annual_average";
}
```

在 `targetDisplayMonth` 中只校验年均内部 key，不改变返回值：

```javascript
if (taskType === "annual_average") {
  formatAnnualAverageTargetYear(targetMonth);
}
```

扩展周期标签并暴露 hook：

```javascript
function formatFactorPeriodLabel(task, month) {
  if (isQuarterlyAverageTask(task)) {
    return formatQuarterlyAverageTargetQuarter(month);
  }
  if (isAnnualAverageTask(task)) {
    return formatAnnualAverageTargetYear(month);
  }
  return month;
}
```

```javascript
formatAnnualAveragePredictDate: formatAnnualAveragePredictDate,
formatAnnualAverageTargetYear: formatAnnualAverageTargetYear,
```

- [ ] **Step 2: 接入实盘分隔、汇总、趋势与详情文案**

`liveDividerText` 的月均和季均分支之后增加：

```javascript
else if (targetStart && isAnnualAverageTask(task)) {
  targetStart = formatAnnualAverageTargetYear(
    targetDisplayMonth(targetStart, task.taskType)
  );
}
```

汇总第一列、趋势横轴和 tooltip 已统一调用 `formatFactorPeriodLabel`，因此不改内部 `row.month`。在 `factorDetailPresentation` 的季均分支之后增加：

```javascript
if (isAnnualAverageTask(task)) {
  return {
    title: formatAnnualAverageTargetYear(month) + " 年度平均预测明细",
    dateHeader: "目标年度",
    note: "",
    emptyText: "当前年度暂无预测明细",
    buttonLabel: "打开年度平均预测明细"
  };
}
```

- [ ] **Step 3: 接入年均抽屉预测日和目标年度**

在 `renderFactorDailyRows` 中增加：

```javascript
var isAnnualAverage = isAnnualAverageTask(task);
```

空状态改为：

```javascript
var emptyText = isMonthlyAverage || isQuarterlyAverage || isAnnualAverage
  ? presentation.emptyText
  : "当前月份暂无每日明细";
```

在季均日期分支之后增加：

```javascript
else if (isAnnualAverage) {
  html += '<td class="mono">' + escapeHtml(
    formatAnnualAveragePredictDate(row.predictDate)
  ) + '</td>';
  html += '<td class="mono">' + escapeHtml(
    formatAnnualAverageTargetYear(row.targetMonth || month)
  ) + '</td>';
}
```

- [ ] **Step 4: 更新静态资源 cache key**

Run:

```bash
BFL_ANNUAL_JS_SHA256=$(shasum -a 256 frontend/aifin-shell.js | awk '{print $1}')
export BFL_ANNUAL_JS_SHA256
perl -0pi -e '
  s{aifin-shell\.js\?v=[0-9a-f]{64}}{"aifin-shell.js?v=" . $ENV{BFL_ANNUAL_JS_SHA256}}e
' frontend/index.html
```

Expected: `frontend/index.html` 中查询串被机械更新为最终 JavaScript 内容的完整
64 位 SHA-256；不改动其他 HTML。

- [ ] **Step 5: 运行前端聚焦测试**

Run:

```bash
pytest -q \
  tests/test_annual_average_frontend.py \
  tests/test_quarterly_average_frontend.py \
  tests/test_monthly_average_frontend_target_month.py \
  tests/test_frontend_overview_contract.py \
  tests/test_frontend_asset_versions.py
```

Expected: 全部 PASS；2025/2026 内部 key、季度 `YYYY/Qn`、月均格式与普通任务均无回归。

- [ ] **Step 6: 审查并提交前端修改**

```bash
git status --short
git branch --list 'codex/*'
git diff --check
git diff -- frontend/aifin-shell.js frontend/index.html tests/test_annual_average_frontend.py
git add frontend/aifin-shell.js frontend/index.html tests/test_annual_average_frontend.py
git commit -m "fix: show annual average target years"
```

Expected: 提交只包含三份年均前端文件；两个既有月均未跟踪计划文件不被暂存。

### Task 3: 用 TDD 修复年均 live-scope 身份判断

**Files:**
- Modify: `tests/test_signal_gap_plan.py`
- Modify: `harness/signal_gap_plan.py`
- Test: `tests/test_signal_gap_plan.py`

- [ ] **Step 1: 增加年均 case helper 与两条年度边界测试**

```python
def _annual_average_case(
    *, target_date: str = "2026-02-14"
) -> signal_gap_plan.ExpectedSignalCase:
    return signal_gap_plan.ExpectedSignalCase(
        registry_scheme_id="annual_avg__h1__5Y",
        base_scheme_id="annual_avg",
        runtime_type="blackbox_v2",
        frequency="annual",
        task_type="annual_average",
        target_tenor="5Y",
        horizon=1,
        predict_date="2026-02-13",
        feature_date="2026-02-13",
        target_date=target_date,
    )


def test_annual_average_live_scope_uses_target_year_identity() -> None:
    assert signal_gap_plan._case_is_in_platform_live_scope(
        _annual_average_case(target_date="2026-02-14")
    )


def test_annual_average_live_scope_rejects_prior_target_year() -> None:
    assert not signal_gap_plan._case_is_in_platform_live_scope(
        _annual_average_case(target_date="2025-01-28")
    )
```

- [ ] **Step 2: 运行新测试并确认红灯**

Run:

```bash
PYTHONPATH=. conda run -n bond_factor_lab_service \
  pytest -q tests/test_signal_gap_plan.py
```

Expected: `2026-02-14` 年均测试 FAIL；2025、月均、季均和普通任务边界测试 PASS。

- [ ] **Step 3: 增加最小年度比较分支**

在季度分支之后、普通 raw-date 分支之前加入：

```python
if case.task_type == "annual_average":
    pointer = date.fromisoformat(case.target_date)
    live_start = date.fromisoformat(PLATFORM_LIVE_TARGET_START_DATE)
    return pointer.year >= live_start.year
```

不得提取通用 period abstraction，也不改 `_is_frequency_due`、春节桶、prediction context 或 repository。

- [ ] **Step 4: 运行 gap 聚焦测试**

Run:

```bash
PYTHONPATH=. conda run -n bond_factor_lab_service pytest -q \
  tests/test_signal_gap_plan.py \
  tests/test_signal_gap_fill.py \
  tests/test_signal_gap_fill_cli.py \
  tests/test_period_average_buckets.py \
  tests/test_period_average_requests.py
```

Expected: 全部 PASS；年度 2026 进入 live scope，年度 2025 仍排除。

- [ ] **Step 5: 审查并提交 gap-scope 修改**

```bash
git status --short
git branch --list 'codex/*'
git diff --check
git diff -- harness/signal_gap_plan.py tests/test_signal_gap_plan.py
git add harness/signal_gap_plan.py tests/test_signal_gap_plan.py
git commit -m "fix: include target year in live gap scope"
```

Expected: 提交只有 gap-scope 实现和测试，不包含算法、Actuals、Registry 或运维配置。

### Task 4: 完成本地整体验证

**Files:**
- No source changes expected

- [ ] **Step 1: 运行前端语法与静态资源验证**

Run:

```bash
node --check frontend/aifin-shell.js
PYTHONPATH=. conda run -n bond_factor_lab_service pytest -q \
  tests/test_annual_average_frontend.py \
  tests/test_quarterly_average_frontend.py \
  tests/test_monthly_average_frontend_target_month.py \
  tests/test_frontend_asset_versions.py
```

Expected: JavaScript syntax OK，所有前端测试 PASS。

- [ ] **Step 2: 运行全量测试**

Run:

```bash
PYTHONPATH=. conda run -n bond_factor_lab_service pytest -q
```

Expected: 全部 PASS；不得只用聚焦测试代替。

- [ ] **Step 3: 核对精确提交和工作树**

```bash
git status --short
git log -8 --oneline --decorate
git diff --check
git diff 86e9f32cda9d1cb573a14d202ed376c995e249f0...HEAD --stat
```

Expected: 年均 source changes 全部已提交；工作树只保留两个用户既有的未跟踪月均计划；`master` 未移动。

### Task 5: 从精确提交构建唯一确定性 release

**Files:**
- No source changes expected

- [ ] **Step 1: 构建两次 archive**

```bash
BFL_ANNUAL_RELEASE_ID=$(git rev-parse HEAD)
BFL_ANNUAL_BUILD_ROOT=$(mktemp -d /tmp/bfl-annual-release.XXXXXX)
git worktree add --detach "$BFL_ANNUAL_BUILD_ROOT/worktree" "$BFL_ANNUAL_RELEASE_ID"
python -B "$BFL_ANNUAL_BUILD_ROOT/worktree/scripts/build_source_release.py" \
  --project-root "$BFL_ANNUAL_BUILD_ROOT/worktree" \
  --output-dir "$BFL_ANNUAL_BUILD_ROOT/build-1"
python -B "$BFL_ANNUAL_BUILD_ROOT/worktree/scripts/build_source_release.py" \
  --project-root "$BFL_ANNUAL_BUILD_ROOT/worktree" \
  --output-dir "$BFL_ANNUAL_BUILD_ROOT/build-2"
shasum -a 256 "$BFL_ANNUAL_BUILD_ROOT"/build-{1,2}/*.source.tar.gz
shasum -a 256 "$BFL_ANNUAL_BUILD_ROOT"/build-{1,2}/*.manifest.json
```

Expected: 两次 archive SHA-256 相同，两次 manifest SHA-256 相同；archive 不含 `.git`、`outputs/` 或未跟踪文件。

- [ ] **Step 2: 记录 release identity**

记录：

```text
release ID = 精确 HEAD SHA
archive SHA-256 = 两次一致的 archive 摘要
manifest SHA-256 = 两次一致的 manifest 摘要
source-tree SHA-256 = manifest 中的精确树摘要
```

### Task 6: ECS candidate 只读验收与单次激活

**Files:**
- Runtime candidate only: `/var/tmp/bfl-release-$BFL_ANNUAL_RELEASE_ID`

- [ ] **Step 1: 只读核对现场 authority**

通过 `root@47.103.45.193` 与 `/Users/macstudio0/.ssh/finlab-key.pem` 核对：

```text
current = 86e9f32cda9d1cb573a14d202ed376c995e249f0
previous = 4f7cbd8d4297f214ec2b55e048e1d64e075c84ce
backend = active
五个 timer = enabled/active/waiting
数据库 = ECS 本地 authority
五个年均 Registry 与 exact versions = active
五个 2026-02-14 Prediction 业务键 = 不存在
五个 2025-01-28 Actual = 存在
五个 2026-02-14 Actual = 不存在
```

若 `current`、数据库身份、Registry/exact version 或缺失键状态漂移，则停止激活并重新评估。

- [ ] **Step 2: 上传并预安装 candidate**

上传 archive、manifest 与候选提交中的安装器，在不传 `--activate` 的情况下执行：

```bash
/opt/miniconda3/envs/bond_factor_lab_service/bin/python -B \
  /var/tmp/bfl-release-$BFL_ANNUAL_RELEASE_ID/install_source_release.py \
  --manifest /var/tmp/bfl-release-$BFL_ANNUAL_RELEASE_ID/$BFL_ANNUAL_RELEASE_ID.manifest.json \
  --archive /var/tmp/bfl-release-$BFL_ANNUAL_RELEASE_ID/$BFL_ANNUAL_RELEASE_ID.source.tar.gz \
  --expected-archive-sha256 "$BFL_ANNUAL_ARCHIVE_SHA256" \
  --deploy-root /opt/bond-factor-lab \
  --runtime-root /var/lib/bond-factor-lab/state
```

Expected: candidate source-tree hash 与 manifest 一致；`current` 未变化。

- [ ] **Step 3: 运行 candidate 测试与五个只读 targeted plan**

候选测试至少覆盖：

```bash
pytest -q \
  tests/test_annual_average_frontend.py \
  tests/test_signal_gap_plan.py \
  tests/test_signal_gap_fill.py \
  tests/test_frontend_asset_versions.py
```

加载 ECS service env，并以 candidate release 为 `PYTHONPATH/project-root`，逐方案执行：

```bash
for BFL_ANNUAL_SCHEME_ID in \
  m0_annual_avg_sf_1y_v1 \
  m0_annual_avg_sf_3y_v1 \
  m0_annual_avg_sf_5y_v1 \
  m0_annual_avg_sf_7y_v1 \
  m0_annual_avg_sf_10y_v1
do
  /opt/miniconda3/envs/bond_factor_lab_service/bin/python -B -m harness \
    signal-gap-plan \
    --predict-date 2026-02-13 \
    --scheme-id "$BFL_ANNUAL_SCHEME_ID"
done
```

Expected: 每个计划恰好 `expected=1`、`actionable=1`、`blocked=0`，action 为 `GRAY_LIVE_GAP`；五个写前业务键仍不存在。

- [ ] **Step 4: 激活同一 candidate 并只重启 backend**

```bash
/opt/miniconda3/envs/bond_factor_lab_service/bin/python -B \
  /var/tmp/bfl-release-$BFL_ANNUAL_RELEASE_ID/install_source_release.py \
  --manifest /var/tmp/bfl-release-$BFL_ANNUAL_RELEASE_ID/$BFL_ANNUAL_RELEASE_ID.manifest.json \
  --archive /var/tmp/bfl-release-$BFL_ANNUAL_RELEASE_ID/$BFL_ANNUAL_RELEASE_ID.source.tar.gz \
  --expected-archive-sha256 "$BFL_ANNUAL_ARCHIVE_SHA256" \
  --deploy-root /opt/bond-factor-lab \
  --runtime-root /var/lib/bond-factor-lab/state \
  --activate \
  --expected-current 86e9f32cda9d1cb573a14d202ed376c995e249f0
systemctl restart bond-factor-lab-backend.service
```

Expected: `current` 指向新 release，`previous` 指向 `86e9f32...`；backend active；root、health、Dashboard、HTML、JS、CSS 均 HTTP 200；五个 timer 状态不变。

### Task 7: 逐条补齐目标年度 2026 并完成前端闭环

**Files:**
- Runtime reports only

- [ ] **Step 1: 按期限执行 insert-only gap fill**

在 current release 环境中按 `1Y → 3Y → 5Y → 7Y → 10Y` 执行，每次成功后立即读回再继续：

```bash
for BFL_ANNUAL_SCHEME_ID in \
  m0_annual_avg_sf_1y_v1 \
  m0_annual_avg_sf_3y_v1 \
  m0_annual_avg_sf_5y_v1 \
  m0_annual_avg_sf_7y_v1 \
  m0_annual_avg_sf_10y_v1
do
  /opt/miniconda3/envs/bond_factor_lab_service/bin/python -B -m harness \
    signal-gap-fill \
    --predict-date 2026-02-13 \
    --scheme-id "$BFL_ANNUAL_SCHEME_ID" \
    --project-root /opt/bond-factor-lab/current || break
done
```

Expected: 每次 `status=PASSED`、`records_written=1`；任一业务键存在或任一命令失败即停止，不更新、不删除、不覆盖。

- [ ] **Step 2: 数据库和 Dashboard 验收**

五个新增 Prediction 必须满足：

```text
prediction_phase = gray_live
predict_date = feature_date = 2026-02-13
target_date = 2026-02-14
horizon = 1
scheme_version = 对应 active exact version
run.status = success
run.records_written = 1
Actual = null
```

同时确认目标年度 2025 五条 backtest 仍可 join 已有 Actual `-1`，Dashboard 不新增、不修改 Actual。

- [ ] **Step 3: 前端显示验收**

在现有 tunnel `127.0.0.1:18110 -> ECS 127.0.0.1:8100` 上打开 `http://127.0.0.1:18110/`，确认五个期限均满足：

```text
汇总和趋势包含 2025、2026
2025 已验证
2026 待验证
2026 抽屉标题 = 2026 年度平均预测明细
预测日 = 02/13
目标年度 = 2026
```

- [ ] **Step 4: 最终现场核对与汇报**

再次核对 ECS `current/previous`、backend、五个 timer、HTTP 状态、Dashboard 年均行数与 pending 数。最终报告列出设计/计划/前端/gap 提交、release ID、archive/manifest/source-tree SHA-256、五次 gap-fill 结果、2025/2026 Actual 状态，并明确未修改算法、Actuals、Registry、systemd timer、Nginx、DNS、Mac3、`master` 或远程分支。
