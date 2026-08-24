# 周期均值 Actuals 历史范围修复设计

> **实施结果：已完成。** 历史范围修复已进入 exact release `053d562fc40d7ecf5596f56f1beb00e4a3b58178`。Mac3 后续从本地权威日频数据生成 20 条已到期周期 Actual，并在写前与 ECS 参考逐字段一致；未进行 Actual 跨库复制。

## 背景

因子实验室只展示并验收 `2025-01-01` 之后的历史结果，但 ECS 的 Actuals
总入口调用周期均值更新器时只传入 `end_date`，没有传入 `start_date`。因此更新器
从每个期限的最早行情开始计算，并在 2014 年历史桶遇到空值后提前失败，导致本来
可以计算的 2026/06、2026/07 月均 Actuals 没有写入。

## 目标

- systemd 自然 Actuals 刷新只生成平台历史政策范围内的周期均值 Actuals，即
  `target_date >= 2025-01-01`。
- 2025 年之前的历史行情缺口不再阻断当前周期均值 Actuals 刷新。
- 2025 年之后、实际参与计算的桶仍必须通过完整交易日事实校验；不得跳过或填补
  范围内的缺失值。
- 不修改行情源表、交易日历、预测算法、回测算法或既有 Actuals 表结构。

## 方案比较

### 方案 A：在 Actuals 总入口传入固定历史政策起点（采用）

在 `scheduler.actuals_runner` 定义周期均值 Actuals 的政策起点
`2025-01-01`，调用 `update_period_average_actuals` 时同时传入 `start_date`
和 `end_date`。

优点是改动最小，并与前端、后端和回测已经采用的 `2025-01-01` 历史政策一致。
底层更新器和独立 CLI 继续支持调用方显式选择其他范围。

### 方案 B：从 `t_scheme_predictions` 动态查询最早目标日期

这种方式看似自动，但周期 Actuals 同时服务于回测详情；只查询实盘预测表会漏掉
仅存在于 `t_backtest_predictions` 的历史范围。若再复刻 Dashboard 的 canonical
回测选择逻辑，改动和耦合都会明显扩大。

### 方案 C：修补 2025 年以前的行情或日历

这会把一个调用范围错误扩大成源数据治理和历史回补操作，且不能解决调度入口与
平台历史政策长期不一致的问题，因此不在本次范围内。

## 设计

### 调用边界

`run_actuals_job` 继续按运行日确定 `end_date`。仅对
`update_period_average_actuals` 增加 `start_date="2025-01-01"`：

```python
PERIOD_AVERAGE_ACTUALS_START_DATE = "2025-01-01"

period_average_written = update_period_average_actuals(
    start_date=PERIOD_AVERAGE_ACTUALS_START_DATE,
    end_date=target_date,
)
```

日频、周频和普通月频 Actuals 的调用保持不变。

### 日期语义

周期均值构造器的 `start_date` 作用于平台记录的原始 `target_date` 指针。对月均
任务，该指针仍是 feature 桶锚点后一自然日；前端展示目标月的“加一个自然月”
规则不变。`2025-01-01` 起点会保留第一条满足平台历史政策的周期记录，同时允许
计算该记录所需的 2024 年末 feature 桶行情。

### 错误处理

构造器已经在计算桶均值前跳过 `target_date < start_date` 的记录。本次不修改该
逻辑：起点以前的桶不会进入完整性校验；起点以后任一 feature/target 桶缺少应有
交易日事实时仍 fail-closed，且在统一写入前停止。

### 数据与部署边界

- 代码变更只涉及 Actuals 调用参数及其测试。
- ECS 发布继续使用不可变 release；不在 ECS 上修改 checkout。
- 发布后通过受控 Actuals one-shot 生成范围内记录，不直接执行 SQL。
- 不修改 systemd unit/timer 内容或触发时间。

## 验证

1. 单元测试断言交易日和非交易日入口都向周期均值更新器传入
   `start_date="2025-01-01"`，其他 Actuals 调用参数不变。
2. 回归测试证明调用方指定起点以前的缺口被忽略、起点以内的缺口仍报错。
3. ECS 候选 release 只读构建验证应能计算 2026/06、2026/07 五个期限共 10 条
   月均 Actuals。
4. 激活后运行 Actuals one-shot，核对服务成功、周期 Actuals 表出现对应 10 条，
   Dashboard/API 不再显示“待验证”。

## 非目标

- 不回补或解释 2025 年以前的周期均值 Actuals。
- 不改变季度均值、年度均值的桶定义；它们仅共享相同的平台历史起点。
- 不删除既有 Actuals，不覆盖预测记录，不改变前端日期格式。
