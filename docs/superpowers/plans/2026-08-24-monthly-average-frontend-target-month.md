# 月度平均前端“目标月”最小修复实施计划

> **状态：已完成并闭环。** 最小展示方案由 `06a4a09`、`0dce1a0`、`54ecaf0` 实现，最终随 exact release `053d562fc40d7ecf5596f56f1beb00e4a3b58178` 在 ECS 与 Mac3 生效。目标月显示为 `YYYY/MM`，预测日显示为 `MM/DD`；Mac3 已补齐目标月 2026/06 五条 Prediction，并从本地权威数据生成相关 Actual。正式前端回归测试保留。

> **执行约束：** 本计划只修改前端展示层与对应的聚焦测试，按仓库 `inline-first` 规范由主 agent 执行。保留 `2026-08-24-monthly-average-target-month.md` 完整重构计划，不在本轮实施数据库字段、后端语义或历史数据迁移。

**目标：** 在不改变现有后端数据、API 契约、算法结果、Actuals、准确率和业务写入的前提下，让 `monthly_average` 在月度汇总、预测明细和实盘分界文案中统一展示真正的“下一个目标月”。

**核心做法：** 当前月均记录的 `target_date` 是 `feature_date + 1 natural day` 的内部日期指针。前端仅对 `task_type === "monthly_average"` 做一条确定性展示映射：读取 `target_date` 所在自然月，再前进一个自然月，输出 `YYYY-MM`。日期型任务和其他周期任务继续沿用当前逻辑。

**技术范围：** 原生 JavaScript、静态 HTML、pytest + Node VM 前端契约测试。无数据库迁移，无 FastAPI/SQLAlchemy 修改，无生产写入。

---

## 1. 本轮确定的语义

### 1.1 字段仍保持现状

以 2026 年 1 月发出、预测 2026 年 2 月月均涨跌的记录为例：

| 数据/展示项 | 示例值 | 本轮含义 |
|---|---|---|
| `predict_date` | `2026-01-15` | 业务信号发出日，完整显示 |
| `feature_date` | `2026-01-15` | 输入数据截止锚点，不改变 |
| 原始 `target_date` | `2026-01-16` | 后端现有内部日期指针，不直接展示为目标日 |
| 前端目标月 | `2026-02` | 原始指针所在月的下一个自然月 |

本轮不会新增 `target_month` 字段，也不会覆盖或重写 `target_date`。

### 1.2 唯一展示换算规则

~~~text
monthly_average_display_month(target_date):
    解析 target_date 的 YYYY-MM-DD
    取 year 和 month
    month 1..11 -> 同年 month + 1
    month 12    -> 下一年 01
    返回 YYYY-MM
~~~

必须覆盖：

| 原始 `target_date` | 前端目标月 |
|---|---|
| `2026-01-16` | `2026-02` |
| `2026-04-16` | `2026-05` |
| `2026-12-16` | `2027-01` |

不得用 `Date + 30 days` 实现。固定 30 天会在 28、29、31 天月份产生漂移，也会受本地时区影响。这里要做的是“下一个自然月”，不是“30 天后的日期”。

### 1.3 前端文案

月度平均选中时：

- 月度表最后一列：`预测明细`
- 抽屉标题：`2026-02 月度平均预测明细`
- 明细列：`预测日 | 目标月 | 预测方向 | 实际方向 | 结果`
- 目标月单元格：只显示 `2026-02`
- 空状态：`当前月份暂无预测明细`
- 图标按钮无月份辅助句，不生成 `查看2026-02预测明细`；使用不重复业务值的通用可访问名称 `打开月度平均预测明细`
- 不显示固定“月均口径”说明段落

其他任务继续显示原有“目标日”或“目标周”语义，不因本轮月均修复而变更业务含义。

---

## 2. 明确不修改的边界

- 不修改 `scheduler`、`backtests`、`backend`、`shared`、`harness`。
- 不修改预测方向、置信度、三个原始日期、`prediction_phase`、方案版本或输入 lineage。
- 不修改数据库表、已有预测、Actuals、回测记录和 Registry。
- 不改变后端 Actual join、准确率计算、phase 判定、去重键或 Dashboard API schema。
- 不把前端转换后的 `YYYY-MM` 回传后端或作为写库业务键。
- 不对季度平均和年度平均顺带应用 `+3 月`、`+1 年`；两者待月均验收后单独设计。
- 不补造缺失月份，不把相邻月份的数据复制成缺失月份。
- 不在前端静默合并两个来源落到同一目标月的冲突记录。
- 不执行 release 构建、生产切换或公网部署；这些属于代码完成后的独立发布步骤。

---

## 3. 统一实现设计

### 3.1 一个纯函数，禁止两套算法

在 `frontend/aifin-shell.js` 增加一个纯函数：

~~~javascript
function monthlyAverageTargetMonth(targetDate) {
  // 严格读取 YYYY-MM-DD，按整数处理年月，返回 YYYY-MM。
}
~~~

约束：

1. 先复用现有 ISO 日期校验；非法值直接报错，不回退到当前月份。
2. 只做整数年月进位，不使用浏览器本地时区的 `Date` 运算。
3. 不读取 `predict_date`、`feature_date`、`horizon` 或方案名称猜测月份。
4. 函数通过 `window.__factorLabTestHooks` 暴露给 Node VM 聚焦测试。

再增加统一的任务感知入口：

~~~javascript
function targetDisplayMonth(targetDate, taskType) {
  if (taskType === "monthly_average") {
    return monthlyAverageTargetMonth(targetDate);
  }
  return targetDate.slice(0, 7);
}
~~~

Dashboard v1 与旧 metrics 兼容路径必须共同调用这个入口，不能各写一份“月份 + 1”。

### 3.2 原始字段与展示字段分离

前端明细行继续保留：

~~~text
targetDate = API 返回的原始 YYYY-MM-DD
~~~

月均展示模型额外生成：

~~~text
targetMonth = monthlyAverageTargetMonth(targetDate)
~~~

使用规则：

| 场景 | 使用字段 |
|---|---|
| 后端 payload 校验 | 原始 `targetDate` |
| 原始行去重、排序 | 原始 `targetDate` |
| phase/cutoff 内部判定 | 原始 `targetDate` |
| 月度表分组键 | 月均用 `targetMonth`；其他任务保持现状 |
| 月均抽屉“目标月” | `targetMonth` |
| 普通任务“目标日/目标周” | 原始现有字段 |

这样可以保证本轮只是 presentation mapping，不会把展示值误用于后端业务判断。

### 3.3 两条读取路径必须同时覆盖

当前前端有两条数据构造路径：

1. Dashboard v1：`decodeDashboardRows -> groupDashboardDetails -> buildFactorLabViewModel`
2. 旧 metrics 兼容：`dailyRowsByMonth -> buildBacktestTaskSchemes/buildLiveTaskSchemes`

两条路径都要显式传入 `task_type/taskType`，并通过同一个 `targetDisplayMonth` 产生目标月。只改其中一条会造成接口切换或降级时页面口径漂移，因此不允许。

### 3.4 只改变展示分组，不改变 phase 规则

以下函数继续按原始日期工作：

- `decodeDashboardRows` 的 schema、日期合法性、重复行校验
- `deriveDashboardPhaseRanges`
- `liveBacktestCutoffTargetDate`
- `trimBacktestAtLiveStart`

月均的分组函数改为任务感知：

~~~javascript
groupDashboardDetails(rows, taskType)
dailyRowsByMonth(rows, frequency, horizon, taskType)
~~~

它们只把月均行挂到转换后的 `YYYY-MM` 分组下；行内仍携带原始日期。

### 3.5 不隐藏真实数据冲突

转换后如果同一个月均方案的 backtest 与 live 同时落到同一个目标月，前端不得简单相加或覆盖。应在视图模型构造阶段 fail-closed，显示数据错误，并保留原始 payload 便于排查。

这条检查只针对“同一方案、同一目标月、跨 source 冲突”；同一目标月内合法的多期限/多明细仍按既有方案结构处理。

---

## 4. 具体实施任务

### Task 1：先锁定月均日期转换契约

**Files**

- Create: `tests/test_monthly_average_frontend_target_month.py`
- Modify: `frontend/aifin-shell.js`

步骤：

1. 新建一个聚焦测试文件，复用仓库现有 Node VM 加载方式，不建立新的浏览器测试框架。
2. 先写三个精确映射测试：普通月份、31 天月份、12 月跨年。
3. 增加非法/缺失 `target_date` fail-closed 测试。
4. 增加非 `monthly_average` 保持原分组月份的回归测试。
5. 实现并导出 `monthlyAverageTargetMonth`、`targetDisplayMonth`。

验证：

~~~bash
python -m pytest -q tests/test_monthly_average_frontend_target_month.py
~~~

Expected：映射、跨年、非法输入和非月均隔离全部 PASS。

### Task 2：接入 Dashboard v1 视图模型

**Files**

- Modify: `frontend/aifin-shell.js`
- Modify: `tests/test_monthly_average_frontend_target_month.py`

步骤：

1. `dashboardDetailRow` 接收 `taskType`，月均行增加 `targetMonth` 展示属性，同时保留原始 `targetDate`。
2. `groupDashboardDetails` 接收 `taskType`，月均按转换后的目标月分组。
3. `buildFactorLabViewModel` 从 scheme 的 canonical `taskType` 向下传递，不从 frequency 或 horizon 推断。
4. 月度聚合仍使用现有 `monthlyRowsFromGroupedDetails`，只改变分组键，不改方向/准确率公式。
5. 增加一个最小 payload 测试，证明 `target_date=2026-01-16` 出现在 `2026-02`，且行数、预测方向、实际方向和正确性不变。
6. 增加跨 backtest/live 同目标月冲突测试，要求 fail-closed，而不是静默合并。

验证：

~~~bash
python -m pytest -q \
  tests/test_monthly_average_frontend_target_month.py \
  tests/test_dashboard_contract_no_drift.py
~~~

Expected：Dashboard schema 不漂移，月均展示月份正确。

### Task 3：接入旧 metrics 兼容路径

**Files**

- Modify: `frontend/aifin-shell.js`
- Modify: `tests/test_monthly_average_frontend_target_month.py`

步骤：

1. 给 `dailyRowsByMonth` 增加 `taskType` 参数。
2. `buildBacktestTaskSchemes` 和 `buildLiveTaskSchemes` 传入 API 返回的显式 `task_type`。
3. 月均明细行保留原始 `targetDate`，额外保存 `targetMonth`。
4. 对同一组 fixture 同时经过 Dashboard v1 与 legacy 构造，断言目标月、数量、方向和结果完全一致。

验证：

~~~bash
python -m pytest -q \
  tests/test_monthly_average_frontend_target_month.py \
  tests/test_frontend_overview_contract.py
~~~

Expected：主路径与兼容路径无口径差异。

### Task 4：更新月均表格和预测明细抽屉

**Files**

- Modify: `frontend/index.html`
- Modify: `frontend/aifin-shell.js`
- Modify: `tests/test_frontend_overview_contract.py`
- Modify: `tests/test_frontend_live_divider_contract.py`

步骤：

1. 月度表入口列使用中性标题 `预测明细`，避免所有任务共享的静态 HTML 固定写成“每日明细”。
2. `renderFactorDetail` 对月均图标按钮使用 `aria-label="打开月度平均预测明细"`，不拼接月份辅助文案。
3. `renderFactorDailyRows` 增加 `isMonthlyAverageTask(task)` 分支：
   - 标题为 `${month} 月度平均预测明细`
   - 第二列标题为 `目标月`
   - 第二列直接显示 `row.targetMonth` 的 `YYYY-MM`
   - 不调用日格式化函数，不显示 `MM/DD`
   - 固定说明段落清空并隐藏
   - 空状态改为 `当前月份暂无预测明细`
4. 抽屉关闭后或切换到其他 task 时恢复原有日/周标题、列名和说明，避免状态残留。
5. 月均实盘分界文案如包含目标日期，改为任务感知的目标月份；内部 cutoff 仍使用原始日期。
6. 增加静态契约断言，确保月均路径不出现“目标日”“每日验证表”“查看2026-02预测明细”。

验证：

~~~bash
python -m pytest -q \
  tests/test_monthly_average_frontend_target_month.py \
  tests/test_frontend_overview_contract.py \
  tests/test_frontend_live_divider_contract.py
~~~

Expected：月均文案和目标月正确，日频/周频原行为不变。

### Task 5：验证月份连续性，但绝不补造数据

**Files**

- Test only: existing frontend fixtures and read-only API/public-page checks during later deployment acceptance

步骤：

1. 用连续 raw pointers 验证转换后的目标月也是连续的。
2. 用故意缺少一个 pointer 的 fixture 验证前端保持缺口，不生成占位预测。
3. 特别核对 `2026-06`：只有 payload 中存在可映射到 `2026-06` 的真实行时才显示。
4. 如果修复后仍缺少 `2026-06`，将其判定为后端/数据缺口，单独报告；不得扩大本前端任务去写库或复制预测。

Expected：前端只纠正月份归属，不掩盖真实缺数。

### Task 6：完整回归、代码审阅与后续发布门禁

**Files**

- Modify only if implementation actually requires: `CURRENT_STATUS.md`、`TODO.md`
- 本次临时展示适配不修改长期架构语义文档

本地验证：

~~~bash
python -m pytest -q \
  tests/test_monthly_average_frontend_target_month.py \
  tests/test_frontend_overview_contract.py \
  tests/test_frontend_live_divider_contract.py \
  tests/test_dashboard_contract_no_drift.py \
  tests/test_factor_lab_dashboard_api.py \
  tests/test_period_average_dashboard.py
git diff --check
cmp -s AGENTS.md CLAUDE.md
~~~

人工前端验收矩阵：

| 场景 | 验收 |
|---|---|
| 月均 2026-01 指针 | 月度行与抽屉显示 `2026-02` |
| 月均跨年 | `2026-12` 指针显示 `2027-01` |
| 月均明细 | 标题、列名、单元格均无“目标日”语义 |
| 月均指标 | 样本数、方向分布、准确率与修改前原始记录计算一致 |
| 日频 | 仍显示目标日 |
| 周点/周均 | 仍显示原有目标日/目标周 |
| 季均/年均 | 本轮行为不变 |
| 缺失月份 | 不由前端伪造 |

后续生产发布只有在用户另行批准后才执行，且必须使用 immutable release 流程。公网验收至少包括：HTML/JS/CSS HTTP 200、静态摘要与 current release 一致、目标月跨年显示、月均完整月份序列、DashboardGate 以及其他 active 方案无回归。

---

## 5. 预期改动规模

计划中的实际代码改动应保持很小：

- 一个自然月进位纯函数；
- 一个任务感知的展示月份入口；
- 两条现有 view-model 路径传递 `taskType`；
- 一个月均抽屉渲染分支；
- 一组聚焦前端测试与少量既有契约断言。

若实施时需要修改后端模型、API schema、数据库或算法，说明前提与当前只读核对不一致，应立即停止本计划并回到保留的完整 `target_month` 重构方案重新 review，而不是继续扩大补丁。

## 6. 完成定义

只有同时满足以下条件才算本前端修复闭环：

1. 月均目标月严格按“原始 pointer 所在月的下一自然月”显示，含跨年。
2. Dashboard v1 与 legacy 路径结果一致。
3. 月均详情只出现 `预测日`、`目标月` 和 `预测明细` 语义。
4. 原始 payload、记录数、方向、Actual、phase 和准确率不变。
5. 不重复月、不静默合并冲突、不伪造缺失月份。
6. 日频、周频、普通 monthly、季均、年均无回归。
7. 所有聚焦测试、相关回归、`git diff --check` 通过。
8. 完整后端 `target_month` 重构计划仍原样保留，未被本轮最小修复替代或删除。
