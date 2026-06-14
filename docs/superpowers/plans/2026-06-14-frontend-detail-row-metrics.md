# 前端基于明细行计算指标实施计划

> **给 agentic workers：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按任务逐项执行本计划。所有步骤使用 checkbox（`- [ ]`）跟踪状态。

**目标：** 前端所有展示用的月度指标和汇总指标，只能从预测明细行计算得到，并且只能调用一套公共前端指标计算逻辑；不再从月度汇总行兜底，也不再通过 precision/recall 反推 TP。

**架构：** API 返回的 `monthly_metrics` 只保留为传输上下文、调试信息或对照信息；前端展示指标统一由 `daily_rows` 明细按 `target_date` 归属月份后计算。`metricFromSampleRows()` 成为前端明细指标计算的唯一主入口；所有通过月度 precision/recall 反推 true positive 的展示路径必须删除或变成不可达。

**技术栈：** 原生 JavaScript 前端 `frontend/aifin-shell.js`，FastAPI 静态资源服务，Python `unittest` 前端回归测试 `tests/test_frontend_factor_lab.py`，现有 DB/API 验证脚本。

---

## 范围与规则

- 前端展示的月度指标必须只从明细行计算。
- 月份归属必须使用 `target_date`，与后端和业务语义一致。
- 月度样本数包含已验证的“平”预测样本。
- 所有指标分母都排除“平”预测样本。
- 每日验证表中，预测为“平”的结果显示为 `-`，不能显示为 `x`。
- 对于需要展示的已验证月份，如果缺少明细行，必须 fail-closed；不能 fallback 到 `monthly_metrics`。
- `/api/metrics/{scheme_id}` 和 `/api/backtests/factor-lab` 返回的 `monthly_metrics` 可以继续存在于 payload 中，但不能作为前端展示指标的计算来源。
- 本计划不做后端 schema 变更。

## 文件映射

- 修改 `frontend/aifin-shell.js`
  - 新增“明细行 -> 月度展示行”的 helper。
  - 回测和实盘 task 构建都改为从明细行生成月度行。
  - 移除展示逻辑对 `metricFromMonthlyRows()` 的依赖。
  - 删除或隔离 `inferTruePositive()`，确保任何展示路径都不能调用它。
  - 保留 `_source` 在 backtest/live 明细行上的传播，用于前端数据源筛选。

- 修改 `tests/test_frontend_factor_lab.py`
  - 增加测试，证明月度展示指标来自 `daily_rows` 明细。
  - 增加测试，证明当 `monthly_metrics` 被污染或误导时，只要有明细，前端会忽略它。
  - 增加测试，证明缺少明细行时 fail-closed，而不是静默使用月度行。
  - 增加测试，证明“平”预测计入样本数、不计入指标分母，并在每日验证表显示为 `-`。

- 修改 `docs/sop/METRIC_FAIL_CLOSED_OPEN_ISSUES_2026-06-14.md`
  - 关闭“前端两层指标计算”的遗留问题。
  - 记录月度行不再是前端展示指标来源。

- 可选修改 `docs/PREDICTION_SEMANTICS.md`
  - 如果现有指标文档没有明确指标来源规则，则补一条简短规则。

## 任务 1：先补“只从明细算月度指标”的失败测试

**文件：**
- 修改：`tests/test_frontend_factor_lab.py`

- [ ] **步骤 1：增加一个 monthly_metrics 故意错误、daily_rows 正确的回归测试**

测试中注入一个方案，结构如下：

```javascript
monthly_metrics: [{
  month: "2026-05",
  samples: 99,
  metric_samples: 99,
  correct: 99,
  accuracy: 100,
  up_precision: 100,
  up_recall: 100,
  down_precision: 100,
  down_recall: 100,
  actual_dist: { up: 99, down: 0, flat: 0 },
  predicted_dist: { up: 99, down: 0, flat: 0 },
  metric_actual_dist: { up: 99, down: 0, flat: 0 },
  metric_predicted_dist: { up: 99, down: 0, flat: 0 }
}],
daily_rows: [
  {
    predict_date: "2026-05-01",
    feature_date: "2026-05-01",
    target_date: "2026-05-08",
    predicted_direction: 1,
    actual_direction: 1,
    is_correct: true
  },
  {
    predict_date: "2026-05-02",
    feature_date: "2026-05-02",
    target_date: "2026-05-09",
    predicted_direction: -1,
    actual_direction: 1,
    is_correct: false
  },
  {
    predict_date: "2026-05-03",
    feature_date: "2026-05-03",
    target_date: "2026-05-10",
    predicted_direction: 0,
    actual_direction: -1,
    is_correct: false
  }
]
```

期望前端计算结果：

```text
samples = 3
metricSamples = 2
correct = 1
overall = 50
upPrecision = 100
upRecall = 50
downPrecision = 0
downRecall = 0
```

- [ ] **步骤 2：运行前端聚焦测试，确认先失败**

运行：

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest tests.test_frontend_factor_lab -v
```

实施前预期：失败。原因是当前前端仍会把 `monthly_metrics` 通过 `rowFromMetric()` 转成月度行，并且在没有可见明细行时仍可能走到 `metricFromMonthlyRows()`。

- [ ] **步骤 3：增加缺少明细行的回归测试**

构造一个方案：

```javascript
monthly_metrics: [...]
daily_rows: []
```

期望行为：

```text
前端必须明确报错或将该方案标记为无法计算指标。
前端不能从 monthly_metrics 计算排行或详情指标。
```

测试断言渲染状态或错误信息包含：

```text
detail rows
```

并且不能出现从 `monthly_metrics` 计算出的成功指标值。

- [ ] **步骤 4：增加“平”预测展示测试**

使用一个 `daily_rows` 明细：

```javascript
predicted_direction: 0,
actual_direction: 1,
is_correct: false
```

期望每日验证表单元格：

```text
-
```

期望指标行为：

```text
samples 包含这条记录
metricSamples 排除这条记录
correct 排除这条记录
```

## 任务 2：新增“明细行生成月度行”的前端 helper

**文件：**
- 修改：`frontend/aifin-shell.js`

- [ ] **步骤 1：新增把分组明细行转成月度展示行的 helper**

在 `appendPendingMonths()` 附近实现：

```javascript
function metricRowFromSampleRows(month, rows) {
  var metric = metricFromSampleRows(rows);
  return {
    month: month,
    samples: metric.samples,
    metricSamples: metric.metricSamples,
    actualDist: distText(directionCounts(rows, "actualDirection", false)),
    predictedDist: distText(directionCounts(rows, "predictedDirection", false)),
    actualCounts: directionCounts(rows, "actualDirection", false),
    predictedCounts: directionCounts(rows, "predictedDirection", false),
    metricActualCounts: directionCounts(rows, "actualDirection", true),
    metricPredictedCounts: directionCounts(rows, "predictedDirection", true),
    overall: metric.overall,
    correct: metric.correct,
    upPrecision: metric.upPrecision,
    upRecall: metric.upRecall,
    downPrecision: metric.downPrecision,
    downRecall: metric.downRecall
  };
}
```

同时新增上面用到的 helper：

```javascript
function directionCounts(rows, key, metricOnly) {
  var counts = { up: 0, down: 0, flat: 0 };
  (rows || []).forEach(function (row) {
    var predicted = normalizeDirection(row.predictedDirection);
    var value = normalizeDirection(row[key]);
    if (value === null) return;
    if (metricOnly && predicted !== 1 && predicted !== -1) return;
    if (value === 1) counts.up += 1;
    else if (value === -1) counts.down += 1;
    else if (value === 0) counts.flat += 1;
  });
  return counts;
}
```

注意：`metricOnly` 的过滤条件基于预测方向是否非平，而不是基于实际方向。

- [ ] **步骤 2：新增从 grouped detail rows 构造所有月度行的 helper**

实现：

```javascript
function monthlyRowsFromGroupedDetails(groupedDailyRows) {
  return Object.keys(groupedDailyRows || {}).sort().map(function (month) {
    var rows = (groupedDailyRows[month] || []).filter(function (row) {
      return normalizeDirection(row.predictedDirection) !== null && normalizeDirection(row.actualDirection) !== null;
    });
    return metricRowFromSampleRows(month, rows);
  });
}
```

这个 helper 内部禁止读取 `scheme.monthly_metrics`。

- [ ] **步骤 3：回测 task 构建改为从明细生成月度行**

在 `buildBacktestTaskSchemes(payload)` 中，将：

```javascript
var monthlyRows = (scheme.monthly_metrics || []).map(rowFromMetric);
monthlyRows = appendPendingMonths(monthlyRows, groupedDailyRows);
```

替换为：

```javascript
var monthlyRows = monthlyRowsFromGroupedDetails(groupedDailyRows);
```

如果 `scheme.daily_rows` 为空，但 `scheme.monthly_metrics` 非空，则直接抛错：

```javascript
throw new Error("backtest scheme " + (scheme.scheme_id || "") + " has monthly_metrics but no detail rows");
```

- [ ] **步骤 4：实盘 task 构建改为从明细生成月度行**

在 `buildLiveTaskSchemes(payload, metricsByKey)` 中，将：

```javascript
var monthlyRows = (metrics.monthly_metrics || []).map(rowFromMetric);
monthlyRows = appendPendingMonths(monthlyRows, groupedDailyRows);
```

替换为：

```javascript
var monthlyRows = monthlyRowsFromGroupedDetails(groupedDailyRows);
```

如果 `metrics.daily_rows` 为空，但 `metrics.monthly_metrics` 非空，则直接抛错：

```javascript
throw new Error("live scheme " + (scheme.scheme_id || "") + " has monthly_metrics but no detail rows");
```

删除旧的 daily-month filtering 代码块，因为 `monthlyRows` 已经完全来自明细行。

## 任务 3：移除展示路径上的月度汇总反推逻辑

**文件：**
- 修改：`frontend/aifin-shell.js`

- [ ] **步骤 1：修改 `aggregateScheme()`，要求必须有明细行**

将：

```javascript
function aggregateScheme(scheme) {
  var dailyRows = getVisibleDailyRowsForScheme(scheme);
  if (dailyRows.length) return metricFromSampleRows(dailyRows);
  return metricFromMonthlyRows(getVisibleRowsForScheme(scheme));
}
```

替换为：

```javascript
function aggregateScheme(scheme) {
  var dailyRows = getVisibleDailyRowsForScheme(scheme);
  if (!dailyRows.length) {
    throw new Error("scheme " + ((scheme && scheme.schemeId) || "") + " has no detail rows for selected metric range");
  }
  return metricFromSampleRows(dailyRows);
}
```

- [ ] **步骤 2：从活跃展示逻辑中移除 `inferTruePositive()`**

如果没有测试或代码路径再调用 `inferTruePositive()` 和 `metricFromMonthlyRows()`，直接删除这两个函数。

如果静态 fixture 仍需要 monthly-only helper，则只能保留在明确命名的 test-only 或 legacy diagnostic 函数里，并且这些入口绝不能被以下展示路径调用：

```javascript
aggregateScheme()
buildBacktestTaskSchemes()
buildLiveTaskSchemes()
rankingMetricValue()
renderFactorSchemeDetail()
```

优先方案是完整删除。

- [ ] **步骤 3：让 UI 错误显式暴露**

当 `renderFactorLab()` 或排行/详情渲染调用 `aggregateScheme()` 时，优先让构建阶段的错误被现有全局加载错误路径捕获。禁止吞掉错误，也禁止替换成 0 指标。

如果某个渲染时调用会在加载 promise 之外抛错，则只包住最小调用点，并显示：

```text
指标明细缺失，无法计算
```

不能显示陈旧指标或补零指标。

## 任务 4：保留数据源筛选能力，但禁止月度 fallback

**文件：**
- 修改：`frontend/aifin-shell.js`

- [ ] **步骤 1：合并 backtest/live 时，继续在明细行上保留 `_source`**

确认现有合并逻辑仍然给复制出来的明细行打标：

```javascript
d._source = "backtest";
d._source = "live";
```

不能只给月度行打标；数据源筛选必须作用在明细行上。

- [ ] **步骤 2：确保 `getVisibleDailyRowsForScheme()` 是指标计算唯一的 source/range 过滤入口**

保留：

```javascript
if (month < factorLabState.startMonth || month > factorLabState.endMonth) return;
if (src === "all" || dr._source === src) rows.push(dr);
```

然后确认没有任何指标展示路径再调用 `getVisibleRowsForScheme()` 来计算指标。

- [ ] **步骤 3：`getVisibleRowsForScheme()` 只允许服务月度表展示**

如果月度表仍需要 `monthlyRows`，它展示的也必须是 `monthlyRowsFromGroupedDetails()` 生成的行，不能展示或计算来自后端月度汇总的指标。

## 任务 5：更新测试覆盖新契约

**文件：**
- 修改：`tests/test_frontend_factor_lab.py`

- [ ] **步骤 1：更新所有 monthly-only mock 数据**

任何测试 fixture 如果有：

```javascript
monthly_metrics: [...]
daily_rows: []
```

必须改为以下两种之一：

```javascript
monthly_metrics: [],
daily_rows: [...]
```

或者显式断言 fail-closed 行为。

- [ ] **步骤 2：新增测试证明月度指标行来自明细**

构造一个断言：后端 `monthly_metrics` 声称 `accuracy: 100`，但明细行实际计算为 `accuracy: 50`，最终前端展示必须使用 `50`。

- [ ] **步骤 3：新增测试证明月份筛选作用于明细行**

构造两个月的 `daily_rows`，把前端月份范围设置为其中一个月，断言只有该月份参与：

```text
samples
metricSamples
overall
upPrecision
upRecall
downPrecision
downRecall
```

- [ ] **步骤 4：新增测试证明 source 筛选作用于明细行**

同一个月份中构造一条 backtest 明细和一条 live 明细。断言：

```text
dataSource = "backtest" 只统计 backtest 明细
dataSource = "live" 只统计 live 明细
dataSource = "all" 同时统计两类明细
```

## 任务 6：更新文档

**文件：**
- 修改：`docs/sop/METRIC_FAIL_CLOSED_OPEN_ISSUES_2026-06-14.md`
- 可选修改：`docs/PREDICTION_SEMANTICS.md`

- [ ] **步骤 1：关闭前端月度 fallback 问题**

补充：

```markdown
前端月度指标和汇总指标只从预测明细行计算，月份按 `target_date` 归属。API 返回的 `monthly_metrics` 不再是前端展示指标来源。
```

- [ ] **步骤 2：记录“平”预测规则**

补充：

```markdown
预测为“平”的样本计入展示用月度样本数，但排除在所有指标分母之外。每日验证表中，预测为“平”的结果显示为 `-`。
```

- [ ] **步骤 3：记录 fail-closed 行为**

补充：

```markdown
如果某个需要展示的方案/月度只有月度汇总数据、没有预测明细行，前端必须 fail-closed，不能从月度汇总反推或回填指标。
```

## 任务 7：验证

**文件：**
- 除前面任务修改的文件外，无额外代码修改。

- [ ] **步骤 1：运行前端聚焦测试**

运行：

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest tests.test_frontend_factor_lab -v
```

期望：

```text
OK
```

- [ ] **步骤 2：运行保护指标契约的后端/前端集成测试**

运行：

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest \
  tests.test_backend_serving \
  tests.test_backtest_factor_lab_readonly \
  tests.test_postonboard_scripts \
  tests.test_frontend_factor_lab \
  -v
```

期望：

```text
OK
```

- [ ] **步骤 3：运行全量回归**

运行：

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest discover tests -v
```

期望：

```text
OK
```

- [ ] **步骤 4：运行 diff 检查**

运行：

```bash
git diff --check
```

期望：无输出，退出码为 `0`。

- [ ] **步骤 5：运行前端 DB 对比**

运行：

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m scripts.verify_frontend_db \
  --scheme-id daily_5y_2_v28 \
  --api-base-url http://127.0.0.1:8100
```

期望：

```text
0 mismatch
```

如果该脚本只比较后端 API 与 DB，则刷新浏览器后增加一次小型人工检查：选择一个已知存在“平”预测的月份，确认前端展示指标与明细行计算结果一致。

## 任务 8：Git 提交

**文件：**
- 只 stage 本计划明确修改的文件。

- [ ] **步骤 1：查看变更文件**

运行：

```bash
git status --short
git diff -- frontend/aifin-shell.js tests/test_frontend_factor_lab.py docs/sop/METRIC_FAIL_CLOSED_OPEN_ISSUES_2026-06-14.md docs/PREDICTION_SEMANTICS.md
```

- [ ] **步骤 2：显式 stage 文件**

运行：

```bash
git add \
  frontend/aifin-shell.js \
  tests/test_frontend_factor_lab.py \
  docs/sop/METRIC_FAIL_CLOSED_OPEN_ISSUES_2026-06-14.md
```

如果 `docs/PREDICTION_SEMANTICS.md` 有修改，再显式加入：

```bash
git add docs/PREDICTION_SEMANTICS.md
```

- [ ] **步骤 3：提交**

运行：

```bash
git commit -m "fix: derive frontend metrics from detail rows"
```

## 自检

- 需求覆盖：本计划移除前端展示指标对月度汇总行的 fallback，保留“平”预测语义，保留 source/month 筛选，并要求补齐测试与文档。
- 占位检查：没有 `TBD`、`TODO` 或未展开的实施占位。
- 类型一致性：计划中使用现有前端字段 `predictedDirection`、`actualDirection`、`monthlyRows`、`dailyRowsByMonth`、`_source`，以及现有指标名 `samples`、`metricSamples`、`overall`、`upPrecision`、`upRecall`、`downPrecision`、`downRecall`。
