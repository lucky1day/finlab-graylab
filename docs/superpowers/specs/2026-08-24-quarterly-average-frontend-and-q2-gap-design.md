# 季均前端与 2026/Q2 缺口修复设计

## 背景

五个季均 M0 方案已经完成入库并在 ECS 可见，但当前前端仍沿用普通月度/日频
展示语义：季度记录按 `target_date` 的月份显示，抽屉标题是每日验证表，日期列显示
完整 ISO 日期。当前 canonical 回测覆盖目标季度 `2025/Q2` 至 `2026/Q1`，实盘
只有目标季度 `2026/Q3`，目标季度 `2026/Q2` 五个期限各缺一条预测。

周期均值 Actuals 已按 `target_date >= 2025-01-01` 生成。`2026/Q2` 五个期限的
Actual 已存在；`2026/Q3` 尚未结束，Actual 为空是正确状态。

## 目标

- 季均方案在前端使用目标季度语义，不再显示为普通月份和每日验证表。
- 补齐目标季度 `2026/Q2` 五个期限共五条 `gray_live` 预测。
- `2026/Q2` 补齐后立即显示已有 Actual；`2026/Q3` 继续待验证。
- 所有代码和数据操作只生成一个最终 immutable release，不为前端和 gap-scope
  分别发布中间 release。
- 不修改预测算法、Actual 算法、原始日期字段、数据库表结构或 API schema。

## 方案选择

采用季均任务边界的专属展示与 live-scope 解释。

不建立通用 Period Formatter，也不向 Dashboard API 增加 display-only 字段。这样
可以保持 API、数据库和其他七类任务完全不变，并把改动限制在前端渲染边界和
`signal_gap_plan` 的业务目标范围判断。

## 日期与季度语义

季均 Contract 原始字段保持：

- `predict_date`：feature 季最后一个交易日。
- `feature_date`：等于 `predict_date`。
- `target_date`：feature 季锚点后一自然日，也是目标季第一自然日指针。

示例：

| 字段 | 原始值 | 前端显示 |
|---|---|---|
| `predict_date` | `2026-03-31` | `03/31` |
| `target_date` | `2026-04-01` | `2026/Q2` |

前端内部继续使用 `YYYY-MM` 作为排序、筛选和详情索引键。例如目标季度
`2026/Q2` 的内部 key 仍是 `2026-04`。只在文字渲染边界转换为 `YYYY/Qn`，避免
改动全局月份筛选器和趋势数据结构。

季度转换必须 fail-closed：季均目标 key 的月份只能是 `01`、`04`、`07`、`10`；
其他月份表示上游目标日期语义漂移，前端拒绝提交该 Dashboard candidate。

## 前端设计

### 专属任务判断与格式化

在 `frontend/aifin-shell.js` 增加：

- `isQuarterlyAverageTask(task)`。
- `formatQuarterlyAveragePredictDate(predictDate)`：ISO 日期转 `MM/DD`。
- `formatQuarterlyAverageTargetQuarter(targetMonth)`：季度首月 key 转 `YYYY/Qn`。
- `formatFactorPeriodLabel(task, month)`：季均返回季度标签，其他任务返回原始月份。

这些函数只影响 `taskType === "quarterly_average"`。月均现有
`YYYY/MM`、`MM/DD` 和下一自然月逻辑不变。

### 汇总表和趋势图

- 月度汇总数据仍按内部月份 key 排序、分页和筛选。
- 季均汇总表第一列显示 `YYYY/Qn`。
- 季均趋势图横轴和 tooltip 使用 `YYYY/Qn`。
- 回测/实盘分隔、准确率计算和样本数不变。

### 详情抽屉

季均详情使用：

- 标题：`2026/Q2 季度平均预测明细`。
- 日期列标题：`目标季度`。
- 空状态：`当前季度暂无预测明细`。
- 打开按钮 aria-label：`打开季度平均预测明细`。
- 预测日：`MM/DD`。
- 目标季度：`YYYY/Qn`。

预测方向、实际方向和验证结果的渲染不变。Actual 为 `null` 时继续显示问号和待验证
状态，不把未完成季度伪装为已验证。

## `2026/Q2` 缺口设计

目标季度 `2026/Q2` 的原始业务键为：

- `predict_date=2026-03-31`
- `feature_date=2026-03-31`
- `target_date=2026-04-01`
- `horizon=1`
- 五个 `target_tenor`：`1Y`、`3Y`、`5Y`、`7Y`、`10Y`

平台实盘边界是 `2026-06-01`。普通任务继续直接比较原始 `target_date`；季均改为
比较目标季度身份。`2026-04-01` 所属目标季度是 `2026/Q2`，与 live start 所属
季度相同，因此进入受控 gap-fill 范围。早于 `2026/Q2` 的季度仍被拒绝。

补缺继续使用既有 `harness signal-gap-fill` 和 `scheduler.repository`：每个 base
scheme 独立执行，任一业务键已存在即对应授权组拒绝，不更新、不删除、不覆盖。

## Actuals

本次不修改 Actuals 代码，也不重新定义季度桶。发布前只读核对
`t_scheme_period_average_actuals`：

- `target_rule=target_quarter_average_yield_vs_feature_quarter_average_yield`
- `target_date=2026-04-01`
- 五个期限各一条 Actual

补齐预测后 Dashboard 应把这五条 Actual 正确 join 到 `2026/Q2`。目标季度
`2026/Q3` 的原始指针为 `2026-07-01`，在季度结束前继续保持 Actual `null`。

## 测试

### 前端

- 目标季度 `2026-04` 格式化为 `2026/Q2`。
- 预测日 `2026-03-31` 格式化为 `03/31`。
- 非季度首月 key、非法日期和空值均 fail-closed。
- Dashboard 和 legacy candidate 都保留原始 `targetDate`，内部 key 为 `2026-04`，
  但汇总、趋势和抽屉渲染为 `2026/Q2`。
- 月均及其他任务格式回归不变。

### Gap scope

- `2026/Q2` 原始指针 `2026-04-01` 被纳入 live scope。
- `2026/Q1` 原始指针 `2026-01-01` 被拒绝。
- 普通任务仍按原始日期比较。

### 集成

- 本地聚焦测试和完整测试全部通过。
- ECS candidate 只读 gap plan 恰好报告五个 `2026/Q2` missing case。
- candidate 只读算法结果每个方案恰好一条，并与 exact version、DataBridge authority
  匹配。
- 补缺后数据库和 Dashboard 都恰好出现五条 `2026/Q2` 实盘记录，Actual 非空。
- `2026/Q3` 五条仍存在且 Actual 为空。

## 单次发布流程

1. 先在本地完成前端实现和测试，不发布。
2. 再完成 quarterly gap-scope 实现和测试，不发布。
3. 运行完整测试后形成一个精确代码提交。
4. 从该提交构建一份 deterministic archive。
5. ECS 只进行一次 preinstall candidate 验收。
6. 一次激活并只重启 backend 以加载新静态前端。
7. 通过五次受控 `signal-gap-fill` 写入五个 base scheme。
8. 最终核对数据库、Dashboard、HTTP 和现有 tunnel 前端。

## 非目标

- 不修改季均 M0 交付算法或 exact scheme version。
- 不提前生成 `2026/Q3` Actual。
- 不修改季度调度触发、systemd unit/timer、Registry、Nginx 或 DNS。
- 不晋级 Mac3，不移动 `master`，不推送远程分支。
- 不把年度均值前端修改夹带进本次季均任务。
