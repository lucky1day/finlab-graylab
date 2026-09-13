# 预测日期与实盘阶段语义

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：算法、平台、回测、API 和前端开发人员
本文是平台关于 `predict_date` / `feature_date` / `target_date` 与灰度实盘阶段的强制语义。前端、后端、回测、SOP、方案文档和测试用例必须使用同一套术语；如与旧文档冲突，以本文为准，并回写对应文档。

Source-backed 方案还必须遵守 [SOURCE_ALGORITHM_FIDELITY.md](SOURCE_ALGORITHM_FIDELITY.md)。日期字段映射是平台适配，不是修改原始算法时间窗口、测试区间或 batch/PIT 口径的许可。

## 0. 运行时期限口径

前端和业务分列只使用 `target_tenor + task_type`，不得用 horizon 猜测任务。
业务任务与期限见[共享方案契约](SCHEME_CONTRACT.md#4-任务类型与期限)；Blackbox 的固定 `task_type/horizon/target_rule` 组合由[上游 Metadata 合同](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md#2-metadata)定义。
日频 horizon 表示后续交易日步长；Blackbox 周/月及周期均值的 horizon=1 表示下一个同类业务桶。

历史周/月 6/30 只为已有身份和事实兼容保留，不能用于推导 Blackbox Request。
同 ID 运行时迁移的执行 horizon 与原事实 horizon 投影边界见
[源算法保真](SOURCE_ALGORITHM_FIDELITY.md#7-同算法-native--blackbox-迁移)。

## 1. 三个标准日期字段

| 字段 | 平台含义 | 是否给前端/业务使用 |
|------|----------|:------------------:|
| `predict_date` | 信号发出日 / 调度运行日 | 是 |
| `feature_date` | 数据截止日 / 预测站位日，模型只能使用该日及以前允许可见的数据 | 是 |
| `target_date` | 验证目标日，用于展示、去重、actual join 和月度统计归属 | 是 |

`feature_date` 是平台、业务和前端唯一标准数据截止字段。`anchor_date` 只允许作为方案内部算法变量或历史审计 extra 保留；任何对外语义、前端展示、灰度规则、回测规则都不得依赖 `anchor_date`。如果 `extra` 同时保留 `anchor_date`，它必须等于 `feature_date`。

### 1.1 日期、交易日与周键的权威来源

以下来源各自只负责一种语义，不能互相替代：

| 权威来源 | 唯一职责 | 禁止用法 |
|---|---|---|
| `daily_output.csv.date` | 算法日频业务观测轴和 `daily_cutoff_key` 定位 | 不得据此自行推导平台交易日或周键 |
| `api_wind_date.csv` | `rdate -> week_id` 的平台业务映射 | 不得把 `week_id` 当 ISO 周、连续数值或执行加减一 |
| `t_trade_calendar` / 平台冻结交易日历 | 平台内部计算前后交易日、调度日和目标日 | Blackbox 算法不得直接访问，也不得用它覆盖 Request |
| 七字段 Request | 本次运行的三日期与三个 cutoff 的唯一合同 | 算法不得修改、顺延、回退或重新生成 |

`t_trade_calendar` 是**工作日历**，跟随国务院节假日安排：法定假期期间的工作日
`trade_flag='0'`，而调休补班的周六/周日 `trade_flag` 同样为 `'1'`。平台标的在调休
补班日无行情，因此平台交易日必须在工作日基础上再排除周末：

> **交易日 = `t_trade_calendar.trade_flag='1'` 且 该日为周一至周五**

该判定的唯一实现是 `shared.calendar_service.is_trading_day_row()`，所有需要判断
交易日的代码都必须调用它，不得各自比较 `trade_flag`，也不得另行叠加星期过滤。
`week_id` 的权威来源仍然只有 `api_wind_date`，不得改用 `t_trade_calendar.week_id`。

`week_id` 是不透明的六位字符串业务键。算法要找相邻周，只能使用平台
给定的 Request、`weekly_output.csv` 中的有序实际键以及声明后的
`api_wind_date.csv` 映射，不能依赖数值连续性。

同一输入身份是结果等价对比的前提，不是各主机独立入库必须使用同一份数据的要求。
上游/平台对账的输入 vintage 归因及不重跑历史边界见[源算法保真](SOURCE_ALGORITHM_FIDELITY.md#0-运行时责任)。
自然运行使用本机当前且通过 ready 校验的 generation；平台注册日历由调用方只读数据库连接捕获。

## 2. 三种运行口径

| 口径 | run `prediction_phase` | `predict_date` | `feature_date` | `target_date` | 写库位置 |
|------|--------------------|----------------|----------------|---------------|----------|
| 历史回测 | 不写入 run phase | `T` | `T` | `T + horizon` | 证据写 `t_backtest_*`；首次激活发布到 `t_scheme_predictions` |
| 灰度实盘 | `gray_live` | `T + 1` | `T` | `T + horizon` | `t_scheme_predictions` |
| 正式实盘 | `scheduled_live` | `T + 1` | `T` | `T + horizon` | `t_scheme_predictions` |

表中的 `T + 1` / `T + horizon` 是日频示意，不是所有任务的自然日公式；周、月与周期任务按 §4 的日历语义生成三日期。

灰度实盘也属于实盘观察区。它与正式实盘的区别不是预测日期公式，而是来源阶段：灰度通常是方案部署前后的受控补齐或观察，正式实盘是 scheduler 在真实时钟自然触发。

### 2.1 原始 benchmark 的 T 对齐规则

对于采用 source T 站位约定的原始 benchmark，`T` 表示算法的数据站位日，进入平台后映射为 `feature_date`，
不能直接当作实盘信号发出日 `predict_date`。`date`、`t` 或旧 `predict_date` 列是否采用该约定，必须依据
原交付的字段定义和使用方式确认，不能仅凭名称推广到任意 Blackbox 输出。

因此：

```text
原始算法 benchmark.T = 平台 feature_date
历史回测 predict_date = feature_date = T
日频灰度/正式实盘 predict_date = 按调度规则发出日, feature_date = T
```

benchmark 逐样本核验必须以 `feature_date + target_date + target_tenor + horizon + benchmark_role` 为主键；周频方案还必须包含或可唯一映射 `feature_week_id`，月频方案还必须包含或可唯一映射 `feature_month_id + target_month_id`。`benchmark_role` 表示该行所属的可比较执行口径（如 source-original 历史段或 source-compatible extension），不是 original/current 文件来源；文件来源应由 `original_backtest_summary.json` / `current_backtest_summary.json` 的 provenance 表达。`predict_date` 只用于校验信号发出时点：历史回测要求 `predict_date == feature_date`，灰度/正式实盘要求 `predict_date` 是站在 `feature_date` 后按调度规则应发出的日期。

回测证据保留完整 Request 批次；产品事实引用与不可变性见 §4，公开分区见 §7。结果分类不反向改变执行口径或 run phase。

既有周平均 source 输出中内容一致的重复 strict key，只按既有 strict-key 合同折叠并在 benchmark summary
留证，不能依赖固定行数或某次历史 run。

已确认采用 source T 的旧 benchmark，即使列名叫 `predict_date`，也按该约定解释。新增 benchmark
明确写 `feature_date` 或 `source_t`，避免混淆；Blackbox 标准 Result 的三日期必须直接回显 Request。

### 2.2 算法内部日期的映射

legacy/core 的 `current_date`、`date` 或 `predict_date` 不一定对应同名平台字段。必须按变量实际用途映射
三日期，并在维护证据中注明；Blackbox 直接使用标准 Request，不把内部变量扩展为平台合同字段。
原始 batch/PIT、test window 和固定训练锚点属于算法语义，分类、对比与变更边界见
[源算法保真](SOURCE_ALGORITHM_FIDELITY.md#3-source-口径分类)。已退役算法的具体窗口案例从 Git 追溯，
不构成重跑历史的授权。

## 3. 灰度实盘规则

灰度实盘用于补齐从方案级 `gray_target_start` 到正式部署前的实盘观察序列。每个方案必须在生命周期证据中登记自己的灰度起点和正式调度起点，不能把任一历史批次日期写成全局常量。

灰度补齐必须满足：

1. 灰度 run 必须标识为 `prediction_phase = gray_live`；产品事实行不重复保存 phase。
2. 记录的 `predict_date` 仍是应当发出信号的调度日，日频通常为 `T + 1`。
3. `feature_date` 必须是 `T`，所有输入 artifact、辅助输入映射和模型训练窗口都不得越过 `feature_date`。
4. 当前数据库在事后补齐时可能已经拥有 `T+1` 或更晚的数据；补齐逻辑必须显式按 `feature_date` 约束输入，不能只依赖当前 DB 最新状态。
5. 如源表提供 `create_time` 或等价 as-of 字段，灰度补齐应进一步证明数据在预期发出时点可见；没有该证据时，不得把灰度样本宣称为 point-in-time 绩效，只能称为灰度实盘观察。

## 4. 正式实盘规则

正式实盘 run 必须标识为 `prediction_phase = scheduled_live`；产品事实行不重复保存 phase。正式实盘起点来自方案激活并由 scheduler 自然成功发出的第一条记录，不能由灰度 target 起点反推。

正式实盘同样只能使用 `feature_date = T` 及以前可见的数据。日频工作日早盘预测的典型形态是：

```text
predict_date = T + 1
feature_date = T
target_date  = T + horizon
```

同一业务时钟内的物理错峰不改变三日期或 run phase；控制面和错过触发的处理见
[生产调度治理](PRODUCTION_SCHEDULING_GOVERNANCE.md)。

日频正式实盘由 `scheduler.executor` 在写库前做统一日期语义校验：记录中的 `predict_date` 必须等于本次 run 日期，`feature_date` 必须等于 `previous_trading_day(predict_date)`，`target_date` 必须等于该 `feature_date` 后第 `horizon` 个交易日。若算法因为源表水位不足而复用旧 `feature_date` 或旧 `target_date`，必须 fail-closed，不得写入 `t_scheme_predictions`；前端显示的“待验证”不能通过人工补写旧预测解决。

`t_scheme_predictions` 的业务键为 `scheme_id + target_tenor + horizon + target_date`，其中 `scheme_id`
保存 base 执行身份，不是 Registry composite ID。它是 Dashboard 唯一产品事实源；所有预测写入均为 insert-only，
完整业务键集合重复保留原 skipped 语义，部分重复整批失败，事后修订不得更新或替换已发布预测。

live/补缺事实引用 `run_id`，首次回测发布事实引用 `backtest_run_id`，两者严格互斥。
回测发布行保存 immutable `backtest_actual_direction`；live 行该字段为空并关联 Actual 权威表。
回测证据明细保留在 `t_backtest_predictions`，run phase 仅在 `t_scheme_runs` 保存。

完整重复的 benign `skipped` 是写入结果，不等于调度前跳过；控制面退出码和执行证据见
[生产调度治理](PRODUCTION_SCHEDULING_GOVERNANCE.md)。授权 target 区间补缺更严格：
任一业务键已存在时整个区间拒绝，`records_written=0`，不得转成 benign `skipped`。
既有单日入口的完整结果跳过行为与区间拒绝规则不得混淆。

周频实盘也遵守同一条 T/T+1 规则：adapter 必须先用平台冻结交易日历计算 `feature_date = previous_trading_day(predict_date)`，再由与本次输入身份绑定的 `api_wind_date.csv` 将 `feature_date` 映射为 `feature_week_id`，并以 `end_week=feature_week_id`、`as_of_date=feature_date` 构建周频输入。禁止直接用 `predict_date` 所在周作为 feature week，也禁止由日期自行换算 ISO 周；否则交易日手工运行或灰度补齐可能读到当前周未来数据。

源周历可能在调度日附近提前切到新 `week_id`，而 `previous_trading_day(predict_date)` 所在周在 DB 周历中暂时找不到下一实际周。平台允许 `shared.prediction_context.build_weekly_live_context()` 做受限日历 fallback：只有当触发日所在源周已经拥有完整的上一交易日、且可由 DB 周历推导出目标周时，才用触发日源周确定完整输入周。该 fallback 只解决周历上下文，不得把旧 `feature_week_id` 的算法信号复用到新周。当前 key 缺信号与日历失败不同；正常完成后的补平前提与当前实现边界见[源算法保真 §2.2](SOURCE_ALGORITHM_FIDELITY.md#22-正常完成后的无信号补平)，不得以平信号掩盖日历或执行异常。

源周历还可能出现孤立 forward jump，例如某个交易日提前标为下一周，但随后的非交易日又回到上一周。平台不得手工改源表；`shared.calendar_service` 与 `scheduler.weekly_actuals_updater` 只允许通过 `shared.week_calendar_normalizer` 对这类“单个交易日跳周、随后非交易日回落”的明显不连续周历行做只读归一化，保证预测侧 `target_date` 与 actuals updater 使用同一周历事实。该归一化不能推广为任意重算周编号，也不能用于绕过 source core 的信号水位检查。周度 actual 的事实匹配键是 `target_tenor + target_date + target_rule`；actual 表中的 `predict_date` 是审计字段，不能要求它与周六调度预测的 `predict_date` 完全相同。

`monthly` 月中收任务使用自然月触发语义：每个自然月 **15 号预测一次，无论 15 号是否交易日**。平台不得把 `predict_date` 顺延到 15 号之后的首个交易日；非交易日 15 号时，`predict_date`、`trigger_date`、`scheduled_trigger_date` 和 `db_rdate` 仍为自然 15 号，`feature_date` 取当前月 15 号及以前最近交易日，`target_date` 取下一个自然月 15 号及以前最近交易日。例如 `predict_date=2025-02-15` 时，如果 2025-02-15 与 2025-03-15 都不是交易日，则平台记录应为：

```text
predict_date = 2025-02-15
feature_date = 2025-02-14
target_date  = 2025-03-14
```

月度 actual join 和前端月度统计仍以 `target_date + target_rule + target_tenor` 为事实键；不得依赖 actual 表中历史遗留的顺延 `predict_date` 来判断是否有真实方向。

### 4.1 周期均值的桶与日期

`shared.period_average_buckets` 从完整权威交易日历构建业务桶，桶内最后一个交易日为 feature 锚点：

| 任务 | 当前桶范围 |
|---|---|
| `monthly_average`（MID） | 上月 16 日至本月 15 日 |
| `quarterly_average`（CQ） | 完整自然季度 |
| `annual_average`（SF） | 本年春节后首个交易日至次年春节前最后一个交易日 |

在锚点收盘后发出信号，`predict_date = feature_date = 当前桶锚点`；`target_date = feature_date + 1` 个自然日
只定位下一同类桶，不能解读为一天后的预测或目标桶完成日。交易日历不足以唯一确定边界时直接失败，
不得按 30/90/365 天推测。Actual 比较完整目标桶与 feature 桶的交易日平均收益率；目标桶未完成或该期限数据
不完整时保持待验证。桶内日值必须完整唯一，不能用部分均值提前验证。

## 5. 回测规则

历史回测必须保持 T 语义：

```text
predict_date = T
feature_date = T
target_date  = T + horizon
```

回测执行只写 immutable `t_backtest_*` 证据，不读取产品事实拼历史结果。首次 Blackbox `activate` 才在同一激活事务中把已批准 exact-version 回测的缺失业务键发布到 `t_scheme_predictions`；revision 回测和 activation 不重写历史产品事实。参与前端历史排行的样本统一要求 `predict_date >= 2025-01-01`；这是输出样本起点，不是训练起点。

当方案已有灰度实盘观察区时，历史回测 runner 必须按 `target_date < gray_target_start` 截断，避免同一 target 同时由 backtest 和 live 区间解释。日频、周频和月频都使用同一条 target 边界；月频仍按自然月 15 号的触发语义计算三日期，不能用 `predict_date` 替代 `target_date` 判断分区。

`target_date` 是回测明细的必填事实字段。runner 和 Dashboard 服务端只用 `target_date` 及 §7 的任务映射确定月份；如果 `t_backtest_predictions` 明细缺 `target_date`，必须 fail-closed。禁止用 `predict_date`、`feature_date`、月份字段或旧 `monthly_metrics` 表推断、替代或回填 `target_date`。

正常无信号的补平前提与运行时实现边界由
[源算法保真 §2.2](SOURCE_ALGORITHM_FIDELITY.md#22-正常完成后的无信号补平)维护；是否为平台平不改变三日期或 target 分区。

### 5.1 已批准的 source-original batch reproduction 例外

历史 source-original 例外不改变三日期、业务键或公开分区：回测仍保留 `predict_date=feature_date`，
`target_date` 由对应任务日历确定，并排除 `target_date >= gray_target_start` 的区间。
跨入 live target 的原始 benchmark 行只能作为 source evidence，不能当作 live 真值或要求实时结果贴合。
例外适用身份、算法保真证据与历史保护见
[源算法保真](SOURCE_ALGORITHM_FIDELITY.md#61-历史-batch-例外的保留范围)，不授权恢复旧 runner 或重跑已发布事实。

### 5.2 历史批次与灰度区间批次

新方案先明确 exact version、输入来源和 lineage、`gray_target_start` 及应有 Request 集合。
本机历史与灰度绑定同一 exact version 和可核验的输入 lineage，保持两个独立证据边界；
各机使用自己的输入，两侧 target 必须零重叠。

| 分区 | 结果语义 |
|---|---|
| `target_date < gray_target_start` | 持久化历史回测，保存新的 immutable canonical backtest 证据 |
| `target_date >= gray_target_start` 且在正式调度接管前应当发出 | 受控补齐为 `gray_live`，保留本应发出的三日期 |
| 已有有效业务键 | 保留原事实；区间入口整组拒绝，单日入口按其完整结果跳过规则处理 |

“一次灰度 batch”只适用于既有区间入口支持且满足 live-safe 前提的交付。支持范围、命令、
输入冻结与原子提交要求只在[平台 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#6-灰度区间批量物化)维护；
其它情况使用既有单日入口，不能为省进程启动扩展新框架或放宽输入校验。
每条 Request 必须由权威任务日历生成自己的 `predict_date`、`feature_date`、`target_date`，
输入不能越过对应 `feature_date`。固定未来 `source_end`、未来 test window、全局 selector/calibration
或跨样本未来状态不能作为 live-safe batch；算法内部性质由上游保证，平台验证自身输入和 Result 边界。

完整性验收以应有业务键集合为准，不只看行数：历史全部早于灰度起点，灰度覆盖接管前所有应发点，
两侧 target 交集为空，三日期、方向、exact version 与标准 Result 一致，已有事实与其它方案不变。
不得因 target 尚未到验证日而遗漏已经应发出的预测，也不为跨批复用临时结果增加生命周期状态。

## 6. 指标统计口径

新回测保存按 target 月份的指标、按期限的全量汇总及必要身份和输入信息；不再生成旧固定 `sim/real/may` 分期或空排除摘要。已有历史 JSON 不改写。

预测方向 `predicted_direction=0` 表示“平”。算法原生平与补平统一统计：可评价后计入样本总数和方向分布，不进入准确率、precision 或 recall 的分母，不要求按来源区分。

分母排除条件只看预测方向：仅
`predicted_direction=0` 被排除。`actual_direction=0` 本身不是额外
排除条件；只要 actual 已到达且预测方向为 `-1` 或 `1`，该样本仍进入
指标分母，并按方向是否相等计为正确或错误。

这里必须始终区分两层数量：

- 样本总数：该月已经可评价的预测交易日 / 预测周数量，包含预测为“涨”“跌”“平”的全部样本。
- 指标分母：只包含预测为“涨”或“跌”的有方向样本；预测为“平”的交易日只参与样本总数和方向分布，不参与任何准确率、召回率或 precision 类指标。

平台统一字段含义如下：

| 字段 | 含义 |
|------|------|
| `samples` / `sample_count` | 可评价样本总数，包含预测为平的样本 |
| `metric_samples` / `metric_sample_count` | 指标分母，只包含 `predicted_direction in {-1, 1}` 的有方向预测样本 |
| `correct` / `correct_count` | 只在 `metric_samples` 范围内统计方向预测正确数；预测为平的样本不计入正确或错误 |
| `accuracy` / `overall` | `correct / metric_samples` |
| `actual_dist` / `predicted_dist` | 全部可评价样本的实际/预测方向分布，包含 `flat` |
| `metric_actual_dist` / `metric_predicted_dist` | 指标分母范围内的实际/预测方向分布，不包含预测为平的样本 |

例如某月共有 8 条已验证预测，其中 1 条预测为平、3 条方向预测正确、4 条方向预测错误，则样本数展示为 `8`，整体准确率展示为 `3/7`，而不是 `3/8`。前端候选排行、月度详情、Dashboard 和回测 runner 必须遵守同一口径。

产品指标只聚合 §4 定义的产品事实，不读取回测证据明细参与逐点选择。

V5 的唯一聚合表示是服务端 Summary：后端从产品事实按 `target_date` 确定月份与 source，计算统计计数。
浏览器可对 Summary 计数做筛选区间求和，并计算准确率、precision 和 recall 来展示月度表、排行、趋势及汇总卡；
不得重新扫描或聚合预测明细。
Detail 仅在用户打开某方案月份时按需请求；不能用 Detail 缓存或旧 `monthly_metrics` 重建第二套 Summary。

## 7. 前端展示规则

前端任务格子由 `target_tenor + task_type` 定义，只读取 Registry/API 的合法任务值；缺失或非法时 fail-closed，
不根据 `frequency/horizon` 猜列、桶或目标日期。actual join 使用 `target_tenor + target_date + target_rule`，
桶的日期指针遵守 §4.1。

Dashboard V5 的公开结果类型只按 `target_date` 分类：

- `target_date < 2026-06-01`：`backtest` / 回测。
- `target_date >= 2026-06-01`：`live` / 实盘。

公开 API 和前端不使用物理来源、`predict_date` 或 run `prediction_phase` 判断结果类型，也不返回灰度/正式
实盘阶段。`gray_live` 与 `scheduled_live` 只保留在 scheduler/gap-fill run 审计中。Dashboard 逐点只读取
`t_scheme_predictions`，不存在跨表 preferred/fallback 或第二套去重逻辑。

前端与业务不读取 `anchor_date`。需要展示预测站位或数据截止时，统一显示 `feature_date`。actual join、结果分类和去重均使用底层 `target_date`。
展示月份通常为 `target_date` 的自然月；`monthly_average` 使用其后一自然月作为 MID 展示标签。
Summary 与 Detail 必须使用同一映射，Detail 按展示月份反向定位底层 target 区间；
此标签转换不修改业务日期、Actual 键或公开历史/实盘分区。映射由
[Dashboard 实现](../../backend/factor_lab_dashboard.py)的 `_display_month` 与 `_detail_target_date_range` 统一执行，
公共合同见[Dashboard V5 测试](../../tests/test_dashboard_v5_builder.py)。

前端展示的部署时间只能来自 active `t_scheme_registry.deployed_at`。`deployed_at` 的业务语义是该注册业务方案激活并进入业务可见状态的日期，不是定时任务已生产挂载的证据；缺失时说明 registry 数据不完整，后端 API 和前端都必须 fail-closed。生产调度挂载必须另由对应 installed plist、`launchctl` loaded state 和任务日志共同证明。禁止 hardcode 默认部署日、scheme_id override 或在前端用灰度起点/正式实盘起点替代部署时间。

前端指标展示必须遵守 §6 的两层分母：

- 月度表、排行、趋势和汇总卡使用 Summary 计数，样本与各指标分母遵守 §6。
- 准确率括号展示 `correct/metric_samples`；不得回退成 `correct/samples`，也不得通过月度行的 precision/recall 反推出 true positive。
- Summary 不携带全部预测明细是合法合同，不得据此 fail-closed。浏览器只校验 Summary 自身完整性；服务端不得用旧 `monthly_metrics` 替代缺失产品事实或反推明细。
- 每日/周度验证明细中，只要预测方向为“平”（`predicted_direction=0` 或前端归一化后 `predicted="平"`），结果列统一展示 `-`，不展示 `✓` 或 `×`。这条展示规则独立于 `actual_direction` 和 `is_correct`，因为“平”不进入指标计算。
- Summary 必须为有预测事实的每个 month/source 保留月份行，即使该月全部待验证。纯待验证月份的统计计数为 0、准确率为 `--`，仍可打开 Detail；待验证记录不进入已验证样本数或指标分母，混合月份的统计不因待验证记录改变。
- 待验证样本仍展示待验证符号；有方向预测才根据验证结果展示 `✓` 或 `×`。
- 当日频、周频、月中收或周期均值 actual 的源实际值水位尚未覆盖对应 `target_date + target_rule` 时，该样本属于待验证；API 和前端应展示 `actual_direction = null` / 准确率 `--`，不得把它计为错误、缺数据修复项或前端刷新失败。运维排查必须先查对应 actual 水位，再判断是否为后端 join 或前端计算问题；同一目标周期内不同 tenor 的源水位可以不同，已覆盖的 tenor 应立即验证，未覆盖的 tenor 继续待验证。
