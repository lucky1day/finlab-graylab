# 年均前端与目标年度 2026 缺口修复设计

> **实施结果：已完成。** 目标年度展示和 2026 live scope 已进入 exact release `053d562fc40d7ecf5596f56f1beb00e4a3b58178`。ECS、Mac3 均有五条 2026 Prediction；年度未完成，Actual 继续为 `null/待验证`。本文中的“只晋级 ECS”描述保留为原设计阶段边界。

## 背景

五个年均 M0 Blackbox V2 方案已经完成入库并在 ECS Registry 中处于 active，
但当前 Dashboard 只有目标年度 2025 的 canonical backtest，没有目标年度 2026
的 live Prediction。前端仍沿用普通月份和每日明细语义，把 raw target pointer
显示成 `2025-01`，无法表达春节年度平均任务的目标年度。

ECS 当前权威数据为：

- 目标年度 2025：五个期限各有一条 backtest，原始日期为
  `predict_date=feature_date=2025-01-27`、`target_date=2025-01-28`；五条
  Actual 已存在，方向均为 `-1`。
- 目标年度 2026：应有的原始业务键为
  `predict_date=feature_date=2026-02-13`、`target_date=2026-02-14`；五条
  live Prediction 均不存在，目标年度尚未结束，因此 Actual 不应存在。

当前 `signal-gap-plan` 直接比较 raw `target_date` 与平台 live start
`2026-06-01`。这会把 `2026-02-14` 判在 live scope 之外并返回
`SKIP_NOT_DUE`，但两者都属于目标春节年度 2026，业务身份判断不正确。

## 目标

- 年均方案在汇总、趋势、tooltip 和详情抽屉统一显示简洁目标年度，例如 `2026`。
- 年均预测日显示为 `MM/DD`，例如 `2026-02-13` 显示为 `02/13`。
- 补齐目标年度 2026 的五条 `gray_live` Prediction。
- 目标年度 2025 继续显示已有 Actual；目标年度 2026 继续显示待验证。
- 前端与 gap-scope 只形成一个最终 immutable release，不创建中间 release。
- 不修改交付算法、Actuals 算法、数据库表、API schema 或原始日期字段。

## 方案选择

采用 `annual_average` 专属的前端渲染和 live-scope 分支。

不建立通用 Period Formatter，也不向 Dashboard API 增加 `target_year`。月均和
季均已经完成并验证，重新抽象三类周期任务会扩大回归面；后端新增 display-only
字段也会无必要地修改 API contract。年均专属分支能把改动限制在现有两个边界：

- `frontend/aifin-shell.js` 的文本渲染。
- `harness/signal_gap_plan.py` 的平台 live-scope 业务身份判断。

## 日期与年度语义

春节年度桶 `SF-Y` 从 Y 年春节后第一个交易日开始，到 Y+1 年春节前最后一个交易日
结束。预测在 feature 桶结束时发出，raw `target_date` 是下一春节年度桶的自然日
指针。

| 业务目标年度 | predict/feature | raw target pointer | 前端显示 |
|---|---|---|---|
| 2025 | `2025-01-27` | `2025-01-28` | `2025` |
| 2026 | `2026-02-13` | `2026-02-14` | `2026` |

前端内部继续使用 raw target pointer 的 `YYYY-MM` 作为排序、筛选和详情索引键：

- 目标年度 2025 的内部 key 为 `2025-01`。
- 目标年度 2026 的内部 key 为 `2026-02`。

只在文字渲染边界提取年份。原始 `predict_date`、`feature_date`、`target_date`、
Dashboard compact row 和数据库业务键均保持不变。

目标年度格式化必须 fail-closed：输入必须是合法 `YYYY-MM` key；预测日必须是合法
ISO 日期。前端不自行重新识别春节日历，也不根据当前时间猜测目标年度。

## 前端设计

### 专属判断与格式化

在现有月均、季均 helper 附近增加：

- `isAnnualAverageTask(task)`：仅匹配 `taskType === "annual_average"`。
- `formatAnnualAveragePredictDate(predictDate)`：ISO 日期转 `MM/DD`。
- `formatAnnualAverageTargetYear(targetMonth)`：合法 `YYYY-MM` key 转 `YYYY`。
- 扩展 `formatFactorPeriodLabel(task, month)`：年均返回目标年度，季均继续返回
  `YYYY/Qn`，其他任务保持原值。

`targetDisplayMonth` 对年均仍返回 raw `YYYY-MM` key，只调用年度格式函数完成形状
校验。这样 Dashboard 与 legacy candidate 在提交前 fail-closed，但排序和筛选结构
不变。

### 汇总与趋势

- 年均汇总表第一列显示 `2025`、`2026`。
- 趋势横轴和 tooltip 显示相同的目标年度。
- 汇总准确率、样本数、来源分隔、分页和月份筛选仍使用内部 key，不修改计算。
- 表头继续沿用平台现有“月份”总表头；本次不为单一任务动态改表结构。

### 详情抽屉

年均详情使用：

- 标题：`2026 年度平均预测明细`。
- 日期列标题：`目标年度`。
- 空状态：`当前年度暂无预测明细`。
- 打开按钮 aria-label：`打开年度平均预测明细`。
- 预测日：`02/13`。
- 目标年度：`2026`。

预测方向、实际方向和结果渲染保持不变。目标年度 2026 的 Actual 为 `null` 时继续
显示问号和待验证状态，不提前生成或伪造 Actual。

## 目标年度 2026 缺口设计

五个目标业务键为：

- base scheme：`m0_annual_avg_sf_{1y,3y,5y,7y,10y}_v1`
- target tenor：`1Y`、`3Y`、`5Y`、`7Y`、`10Y`
- `horizon=1`
- `predict_date=feature_date=2026-02-13`
- `target_date=2026-02-14`

`_case_is_in_platform_live_scope` 为 `annual_average` 增加目标年度比较：

- case 的目标年度取 raw target pointer 的年份。
- 平台 live start 的目标年度取 `2026-06-01` 的年份。
- 目标年度 2026 与 live start 年度相同，因此进入 gap scope。
- 目标年度 2025 早于 live start 年度，继续排除。

普通任务继续按 raw date 比较；月均继续按下一自然月身份比较；季均继续按季度身份
比较。年均分支不改变 `_is_frequency_due`、春节桶识别或预测上下文构建。

补缺继续使用现有 `harness signal-gap-fill` 和 `scheduler.repository` insert-only
路径。每个 base scheme 独立执行；任一业务键已存在即对应授权组拒绝，不更新、
不删除、不覆盖。

## Actuals

本次不修改 Actuals 代码，也不重新生成年度事实。

- 目标年度 2025 的五条 Actual 已存在：
  `target_rule=target_year_average_yield_vs_feature_year_average_yield`、
  `target_date=2025-01-28`。
- 目标年度 2026 的桶要到 2027 年春节前才完整结束；在此之前 Actual 必须为空。

Dashboard 最终应显示目标年度 2025 五条已验证记录，以及目标年度 2026 五条待验证
live 记录。

## 测试

### 前端

- `2025-01` 格式化为 `2025`，`2026-02` 格式化为 `2026`。
- `2026-02-13` 格式化为 `02/13`。
- 非法 ISO 日期、非法或空年度 key 均 fail-closed。
- Dashboard 和 legacy candidate 保留 raw `targetDate` 与内部 `YYYY-MM` key。
- 汇总、趋势、tooltip、实盘分隔和抽屉使用目标年度显示。
- 月均、季均和普通任务格式回归不变。

### Gap scope

- 目标年度 2026 的 raw pointer `2026-02-14` 进入 live scope。
- 目标年度 2025 的 raw pointer `2025-01-28` 被拒绝。
- 月均、季均和普通任务现有边界测试保持通过。

### 集成

- 本地聚焦测试和全量测试通过。
- ECS candidate targeted gap plan 对五个年均 scheme 均恰好报告一条
  `GRAY_LIVE_GAP`，`expected=1`、`actionable=1`、`blocked=0`。
- 写前五个业务键均不存在，五个 Registry 与 exact version 均 active。
- gap-fill 后数据库和 Dashboard 恰好新增五条目标年度 2026 live Prediction。
- 目标年度 2025 的五条 Actual 非空；目标年度 2026 的五条 Actual 为空。

## 单次发布流程

1. 本地完成前端和 gap-scope 实现，不创建中间 release。
2. 运行聚焦与全量测试并提交精确代码。
3. 从精确提交构建两次确定性 archive，比较 archive 和 manifest SHA-256。
4. ECS 只读核对 current/previous、数据库身份、Registry、Exact version、五个缺失键、
   backend 和五个 timer。
5. 预安装 candidate，运行 candidate 测试、静态资源校验和五个 targeted gap plan。
6. 原子激活同一 candidate，只重启 backend。
7. 按 `1Y → 3Y → 5Y → 7Y → 10Y` 逐条执行 `signal-gap-fill`，每次成功后立即
   读回。
8. 最终核对数据库、Dashboard、HTTP、timer 和现有 SSH tunnel 前端，并将浏览器
   停留在目标年度 2026 明细供用户 review。

## 非目标

- 不修改年均 Blackbox 交付算法、春节桶算法或 exact scheme version。
- 不提前生成目标年度 2026 Actual。
- 不修改月均、季均或普通任务的业务语义。
- 不修改 Registry、systemd unit/timer、Nginx、DNS 或数据库 schema。
- 不晋级 Mac3，不移动 `master`，不推送远程分支。
