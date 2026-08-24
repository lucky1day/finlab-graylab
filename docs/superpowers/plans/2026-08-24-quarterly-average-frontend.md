# 季均前端目标季度展示 Implementation Plan

> **状态：已完成。** 前端提交 `04d222c` 已随 exact release `053d562fc40d7ecf5596f56f1beb00e4a3b58178` 在 ECS 与 Mac3 生效；汇总、趋势和抽屉统一使用 `YYYY/Qn`，预测日使用 `MM/DD`。正式回归测试继续保留。

> **For Codex:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** 在不修改 API、数据库原始日期字段和内部月份索引的前提下，让季度均值任务的汇总表、趋势图和详情抽屉统一显示目标季度，并将预测日显示为 `MM/DD`。

**Architecture:** 继续以原始 `target_date` 派生的 `YYYY-MM` 作为排序、筛选和详情索引键，只在季度均值的文字渲染边界转换为 `YYYY/Qn`。季度首月校验放在 `targetDisplayMonth` 路径中，使 Dashboard 与 legacy candidate 在提交前对非 `01/04/07/10` 月份 fail-closed；月均和其他任务沿用现有分支。

**Tech Stack:** 原生 JavaScript、Python pytest、Node.js `vm` 测试 harness、静态 FastAPI 前端。

---

### Task 1: 用失败测试锁定季度格式与候选数据契约

**Files:**
- Create: `tests/test_quarterly_average_frontend.py`
- Reference: `tests/test_monthly_average_frontend_target_month.py`
- Test: `tests/test_quarterly_average_frontend.py`

**Step 1: 写季度格式化和 fail-closed 测试**

复用月均测试中的 Node `vm` harness，并添加以下测试：

```python
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
```

**Step 2: 写 Dashboard candidate 的原始字段保留测试**

构造一个 `taskType="quarterly_average"`、`targetDate="2026-04-01"` 的 Dashboard payload，并验证：

```python
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
```

同时添加 legacy fixture 的等价断言，确保两种加载路径使用同一个内部 key，且不改写原始 `targetDate`。

**Step 3: 写展示文案与渲染边界测试**

```python
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
```

再读取 `frontend/aifin-shell.js`，断言汇总首列、趋势横轴、趋势 tooltip、抽屉日期均调用季度展示函数；读取 `frontend/index.html`，断言脚本查询串等于最终 JavaScript 文件的 SHA-256。

**Step 4: 运行测试并确认失败原因正确**

Run:

```bash
pytest -q tests/test_quarterly_average_frontend.py
```

Expected: FAIL，原因应为季度 hook、季度展示分支或新 cache hash 尚不存在；不得是 fixture 或 Node harness 自身错误。

### Task 2: 实现季度专属格式化与 candidate 校验

**Files:**
- Modify: `frontend/aifin-shell.js`
- Test: `tests/test_quarterly_average_frontend.py`
- Test: `tests/test_monthly_average_frontend_target_month.py`

**Step 1: 增加季度格式函数与任务判断**

在现有月均函数附近添加：

```javascript
function formatQuarterlyAveragePredictDate(predictDate) {
  var value = requireDashboardIsoDate(
    predictDate,
    "quarterly_average predict_date"
  );
  return value.slice(5, 7) + "/" + value.slice(8, 10);
}

function formatQuarterlyAverageTargetQuarter(targetMonth) {
  var value = String(targetMonth || "").trim();
  var match = value.match(/^(\d{4})-(01|04|07|10)$/);
  if (!match) {
    throw dashboardDataError(
      "quarterly_average quarter target month must use a quarter-start YYYY-MM"
    );
  }
  return match[1] + "/Q" + String((Number(match[2]) - 1) / 3 + 1);
}

function isQuarterlyAverageTask(task) {
  return task && task.taskType === "quarterly_average";
}

function formatFactorPeriodLabel(task, month) {
  return isQuarterlyAverageTask(task)
    ? formatQuarterlyAverageTargetQuarter(month)
    : month;
}
```

**Step 2: 在 target key 构造路径执行季度校验**

更新 `targetDisplayMonth`，内部 key 仍返回原始月份：

```javascript
function targetDisplayMonth(targetDate, taskType) {
  var value = requireDashboardIsoDate(targetDate, "target_date");
  if (taskType === "monthly_average") {
    return monthlyAverageTargetMonth(value);
  }
  var targetMonth = value.slice(0, 7);
  if (taskType === "quarterly_average") {
    formatQuarterlyAverageTargetQuarter(targetMonth);
  }
  return targetMonth;
}
```

这使 Dashboard 和 legacy view model 在分组阶段就拒绝非季度首月 key，同时保持 `2026-04` 作为内部键。

**Step 3: 暴露最小测试 hook**

在 `window.__factorLabTestHooks` 增加：

```javascript
formatQuarterlyAveragePredictDate: formatQuarterlyAveragePredictDate,
formatQuarterlyAverageTargetQuarter: formatQuarterlyAverageTargetQuarter,
formatFactorPeriodLabelForTest: formatFactorPeriodLabel,
```

**Step 4: 运行格式与 candidate 测试**

Run:

```bash
pytest -q \
  tests/test_quarterly_average_frontend.py \
  tests/test_monthly_average_frontend_target_month.py
```

Expected: 格式化、candidate 和既有月均测试 PASS；渲染边界测试仍可因 Task 3 尚未实现而 FAIL。

### Task 3: 接入汇总、趋势、分隔文案与详情抽屉

**Files:**
- Modify: `frontend/aifin-shell.js`
- Test: `tests/test_quarterly_average_frontend.py`
- Test: `tests/test_frontend_overview_contract.py`

**Step 1: 季均详情文案使用格式化后的目标季度**

在 `factorDetailPresentation` 的月均分支之后加入：

```javascript
if (isQuarterlyAverageTask(task)) {
  return {
    title: formatQuarterlyAverageTargetQuarter(month) + " 季度平均预测明细",
    dateHeader: "目标季度",
    note: "",
    emptyText: "当前季度暂无预测明细",
    buttonLabel: "打开季度平均预测明细"
  };
}
```

**Step 2: 汇总表和趋势图只在文字边界转换季度标签**

汇总表第一列改为：

```javascript
html += '<td>' + escapeHtml(formatFactorPeriodLabel(task, row.month)) + '</td>';
```

趋势图取得当前 task，并将横轴和 tooltip 的月份标签统一通过 `formatFactorPeriodLabel`：

```javascript
var task = getTaskByKey(factorLabState.selectedTaskKey);
var periodLabel = formatFactorPeriodLabel(task, row.month);
```

点位数组保存格式化后的标签，准确率、分隔点和内部排序均不变。

**Step 3: 季均抽屉日期显示 `MM/DD` 与 `YYYY/Qn`**

在 `renderFactorDailyRows` 中增加 `isQuarterlyAverage`，并与月均并列处理：

```javascript
if (isMonthlyAverage) {
  html += '<td class="mono">' + escapeHtml(
    formatMonthlyAveragePredictDate(row.predictDate)
  ) + '</td>';
  html += '<td class="mono">' + escapeHtml(
    formatMonthlyAverageTargetMonth(row.targetMonth || month)
  ) + '</td>';
} else if (isQuarterlyAverage) {
  html += '<td class="mono">' + escapeHtml(
    formatQuarterlyAveragePredictDate(row.predictDate)
  ) + '</td>';
  html += '<td class="mono">' + escapeHtml(
    formatQuarterlyAverageTargetQuarter(row.targetMonth || month)
  ) + '</td>';
} else {
  html += dateCellHtml(row.predictDate, "--");
  html += dateCellHtml(row.targetDate, displayDay);
}
```

空状态在月均或季均时均采用 `presentation.emptyText`。

**Step 4: 实盘分隔文案也使用目标季度**

`liveDividerText` 保留原始候选排序，仅在最终文字中转换：

```javascript
if (targetStart && isMonthlyAverageTask(task)) {
  targetStart = targetDisplayMonth(targetStart, task.taskType);
} else if (targetStart && isQuarterlyAverageTask(task)) {
  targetStart = formatQuarterlyAverageTargetQuarter(
    targetDisplayMonth(targetStart, task.taskType)
  );
}
```

目标季度 `2026-07-01` 因此显示为 `2026/Q3开始`；普通任务继续显示原始 ISO 日期。

**Step 5: 运行聚焦测试**

Run:

```bash
pytest -q \
  tests/test_quarterly_average_frontend.py \
  tests/test_monthly_average_frontend_target_month.py \
  tests/test_frontend_overview_contract.py
```

Expected: 除最终 cache hash 测试外全部 PASS。

### Task 4: 固定静态资源版本并完成前端验收

**Files:**
- Modify: `frontend/index.html`
- Test: `tests/test_quarterly_average_frontend.py`
- Test: `tests/test_frontend_overview_contract.py`
- Test: `tests/test_frontend_asset_versions.py`

**Step 1: 用最终 JavaScript 内容计算 cache hash**

Run:

```bash
shasum -a 256 frontend/aifin-shell.js
```

把输出的完整 SHA-256 更新到：

```html
<script src="aifin-shell.js?v=<sha256>"></script>
```

**Step 2: 运行前端相关测试**

Run:

```bash
pytest -q \
  tests/test_quarterly_average_frontend.py \
  tests/test_monthly_average_frontend_target_month.py \
  tests/test_frontend_overview_contract.py \
  tests/test_frontend_asset_versions.py
```

Expected: PASS。

**Step 3: 运行全量测试，确认无回归**

Run:

```bash
pytest -q
```

Expected: 全部 PASS；允许保留仓库既有 warning，但不得新增失败。

**Step 4: 核对变更范围并提交前端实现**

Run:

```bash
git status --short
git diff --check
git diff -- frontend/aifin-shell.js frontend/index.html tests/test_quarterly_average_frontend.py
```

确认只包含本计划三份文件，且两份用户未跟踪的月均计划文件未被暂存。然后提交：

```bash
git add frontend/aifin-shell.js frontend/index.html tests/test_quarterly_average_frontend.py
git commit -m "fix: show quarterly average target quarters"
```

**Step 5: 停在前端子项目完成点，不创建中间 release**

记录测试证据和提交 SHA。不要构建 archive、切换 ECS `current`、重启 backend 或执行 gap-fill；下一步按已批准总设计继续季度 gap-scope，实现完成后两部分合并为唯一最终 immutable release。
