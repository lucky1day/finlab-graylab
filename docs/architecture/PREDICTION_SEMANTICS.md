# 预测日期与实盘阶段语义

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：算法、平台、回测、API 和前端开发人员
本文是平台关于 `predict_date` / `feature_date` / `target_date` 与灰度实盘阶段的强制语义。前端、后端、回测、SOP、方案文档和测试用例必须使用同一套术语；如与旧文档冲突，以本文为准，并回写对应文档。

Source-backed 方案还必须遵守 [SOURCE_ALGORITHM_FIDELITY.md](SOURCE_ALGORITHM_FIDELITY.md)。日期字段映射是平台适配，不是修改原始算法时间窗口、测试区间或 batch/PIT 口径的许可。

## 0. 运行时期限口径

前端和业务分列只使用 `target_tenor + task_type`，不得用 horizon 猜测任务。Blackbox Contract 1.0 固定组合为：

| `task_type` | `horizon` | `target_rule` |
|---|---:|---|
| `T+1` | 1 | `target_date_yield_vs_feature_date_yield` |
| `T+5` | 5 | `target_date_yield_vs_feature_date_yield` |
| `weekly_point` | 1 | `target_week_end_yield_vs_feature_week_end_yield` |
| `weekly_average` | 1 | `target_week_average_yield_vs_feature_week_average_yield` |
| `monthly` | 1 | `target_month_observation_yield_vs_feature_month_observation_yield` |

Native V1 既有周频 `horizon=6` 和月频 `horizon=30` 是历史平台计日兼容值，只允许保留在政策清单中的存量方案，不得用于新方案或推导 Blackbox Request。Blackbox 的周/月 horizon 按后续周频/月频观测计数。

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

Blackbox 上游自测和平台 Onboarding 验收必须绑定同一 DataBridge
五文件 generation。完整输入身份由 `generation_id + 五文件 SHA256 +
data_snapshot_id` 表达。任一部分不同，结果差异先归类
`data_vintage_mismatch`，必须同代重跑后才能归因算法。该验收约束不
永久冻结生产；scheduled live 仍使用当天当前且通过校验的 DataBridge
generation。平台注册日历由调用方只读数据库连接捕获，不再绑定第二份
Native generation。

## 2. 三种运行口径

| 口径 | run `prediction_phase` | `predict_date` | `feature_date` | `target_date` | 写库位置 |
|------|--------------------|----------------|----------------|---------------|----------|
| 历史回测 | 不写入 run phase | `T` | `T` | `T + horizon` | 证据写 `t_backtest_*`；首次激活发布到 `t_scheme_predictions` |
| 灰度实盘 | `gray_live` | `T + 1` | `T` | `T + horizon` | `t_scheme_predictions` |
| 正式实盘 | `scheduled_live` | `T + 1` | `T` | `T + horizon` | `t_scheme_predictions` |

灰度实盘也属于实盘观察区。它与正式实盘的区别不是预测日期公式，而是来源阶段：灰度通常是方案部署前后的受控补齐或观察，正式实盘是 scheduler 在真实时钟自然触发。

### 2.1 原始 benchmark 的 T 对齐规则

原始算法回测或 benchmark 文件中的 `T`、`date`、`t`、历史列名 `predict_date` 都表示“原始算法站在 T 这一刻预测”，也就是预测锚点 / 数据站位日。进入平台后，这个 T 必须对齐数据库明细里的 `feature_date`，不得对齐实盘语义下的 `predict_date`。

因此：

```text
原始算法 benchmark.T = 平台 feature_date
历史回测 predict_date = feature_date = T
灰度/正式实盘 predict_date = T + 1, feature_date = T
```

benchmark 逐样本核验必须以 `feature_date + target_date + target_tenor + horizon + benchmark_role` 为主键；周频方案还必须包含或可唯一映射 `feature_week_id`，月频方案还必须包含或可唯一映射 `feature_month_id + target_month_id`。`benchmark_role` 表示该行所属的可比较执行口径（如 source-original 历史段或 source-compatible extension），不是 original/current 文件来源；文件来源应由 `original_backtest_summary.json` / `current_backtest_summary.json` 的 provenance 表达。`predict_date` 只用于校验信号发出时点：历史回测要求 `predict_date == feature_date`，灰度/正式实盘要求 `predict_date` 是站在 `feature_date` 后按调度规则应发出的日期。

回测证据保留完整 Request 批次；首次激活只把不存在的业务键发布为产品事实。后续自然 Writer 已存在的同一业务键永久优先，回测发布不得覆盖。公开的回测/实盘分类只看 `target_date=2026-06-01` 分界，不反向改变回测执行口径或 run phase。

这条规则优先于旧文件列名。旧 benchmark CSV 即使列名仍叫 `predict_date`，也只能解释为 source T / 平台 `feature_date`；新增 benchmark 文件应显式写 `feature_date` 或 `source_t`，避免把原始算法站位日误读为平台信号发出日。

### 2.1.1 Source 执行口径不得被静默改写

原始算法可能是 strict PIT，也可能是一次性 batch、固定历史窗口、月度窗口、walk-forward 或带全局校准的 source-original reproduction。平台必须先分类再执行：

- `source_original_reproduction`: 按原始脚本真实口径复现，current/backtest 应与 source 输出逐样本对齐。
- `source_strict_pit`: 原始脚本本身逐 `feature_date` 硬截止，平台 PIT helper 必须与它等价。
- `platform_live_pit_variant`: 原始交付不是 strict PIT，但业务明确要求构造 live-like PIT 变体；该变体必须获批、命名并记录与 source-original 的差异。

不能因为平台 live 语义需要 `feature_date` 硬截止，就直接修改原始算法内部的 `test_start/test_end/test_ranges`、历史起点、周/月频对齐、特征或投票逻辑。若同一 `feature_date` 的 source-original 与平台 PIT 变体不同，差异必须作为口径差异记录，不能通过调参或改算法抹平。

同一份 `original_predictions_sample.csv` 跨过灰度边界时，必须把每行标成 `historical/source-original`、`live-same-context` 或 `source-evidence-only`。只有同执行口径行可以被声明为与 DB/API/live 完全一致；`source-evidence-only` 行不能用来证明 live 成功或失败，也不能要求 live 内部 score 贴合固定 future `source_end` 的 batch 输出。

### 2.2 旧 core 参数名不得直接映射为平台字段

legacy/core 里的参数名不一定等于平台标准字段。遇到 `current_date`、`date`、`predict_date` 等旧参数时，必须先读 core 内部如何使用它，再决定映射到平台的 `predict_date`、`feature_date` 还是 `target_date`；不得只按名字猜。

历史案例：`t1_daily` 旧 core 曾使用 `current_date` 作为参数名，但内部语义是“目标验证日”，并选择最后一个 `< current_date` 的交易日作为模型站位。该旧 Native 路径已退役；以下日期仅说明原样本语义，不要求重跑历史，当前 T1 按 Blackbox 标准 Request/Result 合同执行：

```text
target_date  = 2026-05-29
feature_date = 2026-05-28
predict_date = 2026-05-28  # 历史回测中 predict_date=feature_date
```

这不是新增第四类日期字段；它只是把旧 core 的内部参数语义改名到平台已有的 `target_date`。维护 Native V1 存量方案时，若 legacy 参数名含糊，必须在维护记录中写明它对应的平台字段；Blackbox 新方案不得把内部变量扩展成平台合同字段。

### 2.3 test-window 敏感算法规则

部分源算法把 test window 当作模型选择、ensemble 或信号组合的一部分；这类窗口不是展示参数，改变窗口就可能改变同一个 `feature_date` 的预测结果。`daily_5y_2_v28` 是当前已确认案例：源算法按月度 test window 运行，Phase C 会基于 `test_months` 做 monthly ensemble / signal selection，因此平台不得用连续窗口替代月度窗口。

对 test-window 敏感方案必须遵守：

1. adapter 和 backtest runner 必须共享同一个 inference helper，不得各自拼 `test_start/test_end`。
2. 实盘/灰度必须先确定 `feature_date=T`，再使用 `feature_date` 所在月第一天到 `feature_date` 作为核心预测窗口；窗口结束不得超过 `feature_date`。
3. 回测仍输出 `predict_date=feature_date=T`，但核心预测窗口必须与同一 `feature_date` 的实盘路径一致。
4. benchmark current 侧必须由平台 inference helper 生成，不能复制 source CSV 冒充 current。
5. 逐方案 original benchmark 的 `date/T` 只对齐平台 `feature_date`；如 `target_date` 进入灰度/实盘区间，只有同执行口径时才与 `t_scheme_predictions.feature_date` 对齐核验，否则必须生成 live-safe oracle。这里的 original benchmark 位于 `schemes/{scheme_id}/benchmarks/`，不是 `source_evidence/benchmark_batches/{benchmark_id}/` 的外部批次证据。
6. helper 只能封装原始算法的执行口径；不得把“更短历史”“同月去年+本月”“previous complete week”等平台便利窗口替代 source 中实际使用的固定历史、batch end 或周频对齐规则。

`daily_5y_2_v28` 已统一为原 ID Blackbox，当前唯一执行入口由 canonical config 的 `delivery` 指定，
`predict/backtest` 共用包内算法路径，并保留上述月度窗口语义。旧 Native inference 附件和专属复现 runner
已退役，仅通过 Git/旧 immutable release 追溯，不得用于当前 Blackbox 补算历史。

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

scheduler 可以为了降低机器负载对同一业务 cron 下的 active 方案做分钟级物理错峰，并限制同时进入算法子进程的预测任务数。错峰只改变进程实际启动时间，不改变 `predict_date`、`feature_date`、`target_date`、`prediction_phase` 或方案 `config.yaml` 中登记的业务基准 cron。

日频正式实盘由 `scheduler.executor` 在写库前做统一日期语义校验：记录中的 `predict_date` 必须等于本次 run 日期，`feature_date` 必须等于 `previous_trading_day(predict_date)`，`target_date` 必须等于该 `feature_date` 后第 `horizon` 个交易日。若算法因为源表水位不足而复用旧 `feature_date` 或旧 `target_date`，必须 fail-closed，不得写入 `t_scheme_predictions`；前端显示的“待验证”不能通过人工补写旧预测解决。

`t_scheme_predictions` 的业务键为 `scheme_id + target_tenor + horizon + target_date`；其中 `scheme_id` 保存 base scheme / 算法执行身份，不是 Registry composite `scheme_id`。它是 Dashboard 唯一产品事实源：live/补缺事实引用 `run_id`，首次回测发布事实引用 `backtest_run_id`，两者严格互斥。run phase 只保存在 `t_scheme_runs`。所有写入均为 insert-only；完整业务键集合已存在时保持原有 skipped 语义，部分重复整批失败，任何事后修订不得更新或替换已发布预测。

普通 active completion 的 benign `skipped` 是算法已经执行、records 已返回并通过该方案写入前复核之后产生的 per-scheme publication outcome，不是 scheduler preflight skip；算法计算成本已经发生。one-shot batch 的 exit code `0` 只表示该批次没有 actionable failure：同一摘要可以同时包含首次发布的 `success` 与完整重复的 benign `skipped`，不能据此声称整个批次没有执行候选方案。

Authorized gray-gap 使用更严格的例外语义：只要授权组内任一业务键已经存在，就必须拒绝整个 gray-gap 组并保持 `records_written=0`，未存在的键也不得写入；该结果不得转换为 benign `skipped`。

周频实盘也遵守同一条 T/T+1 规则：adapter 必须先用平台冻结交易日历计算 `feature_date = previous_trading_day(predict_date)`，再由与本次输入身份绑定的 `api_wind_date.csv` 将 `feature_date` 映射为 `feature_week_id`，并以 `end_week=feature_week_id`、`as_of_date=feature_date` 构建周频输入。禁止直接用 `predict_date` 所在周作为 feature week，也禁止由日期自行换算 ISO 周；否则交易日手工运行或灰度补齐可能读到当前周未来数据。

源周历可能在调度日附近提前切到新 `week_id`，而 `previous_trading_day(predict_date)` 所在周在 DB 周历中暂时找不到下一实际周。平台允许 `shared.prediction_context.build_weekly_live_context()` 做受限日历 fallback：只有当触发日所在源周已经拥有完整的上一交易日、且可由 DB 周历推导出目标周时，才用触发日源周确定完整输入周。该 fallback 只解决周历上下文，不得把旧 `feature_week_id` 的算法信号复用到新周。对已批准 `no_signal_to_flat_v1` 的投票类方案，只有在输入、日历和 core 正常完成、core 结果非空、但当前 `feature_week_id` 缺少最终输出时，才生成审计可识别的平信号；label、selector 所需上下文缺失，或输入、周历、模型、超时、代码异常，仍必须 fail-closed。

源周历还可能出现孤立 forward jump，例如某个交易日提前标为下一周，但随后的非交易日又回到上一周。平台不得手工改源表；`shared.calendar_service` 与 `scheduler.weekly_actuals_updater` 只允许通过 `shared.week_calendar_normalizer` 对这类“单个交易日跳周、随后非交易日回落”的明显不连续周历行做只读归一化，保证预测侧 `target_date` 与 actuals updater 使用同一周历事实。该归一化不能推广为任意重算周编号，也不能用于绕过 source core 的信号水位检查。周度 actual 的事实匹配键是 `target_tenor + target_date + target_rule`；actual 表中的 `predict_date` 是审计字段，不能要求它与周六调度预测的 `predict_date` 完全相同。

月频 0629 source-backed 方案使用独立的自然月触发语义：每个自然月 **15 号预测一次，无论 15 号是否交易日**。平台不得把 `predict_date` 顺延到 15 号之后的首个交易日；非交易日 15 号时，`predict_date`、`trigger_date`、`scheduled_trigger_date` 和 `db_rdate` 仍为自然 15 号，`feature_date` 取当前月 15 号及以前最近交易日，`target_date` 取下一个自然月 15 号及以前最近交易日。例如 `predict_date=2025-02-15` 时，如果 2025-02-15 与 2025-03-15 都不是交易日，则平台记录应为：

```text
predict_date = 2025-02-15
feature_date = 2025-02-14
target_date  = 2025-03-14
```

月度 actual join 和前端月度统计仍以 `target_date + target_rule + target_tenor` 为事实键；不得依赖 actual 表中历史遗留的顺延 `predict_date` 来判断是否有真实方向。

## 5. 回测规则

历史回测必须保持 T 语义：

```text
predict_date = T
feature_date = T
target_date  = T + horizon
```

回测执行只写 immutable `t_backtest_*` 证据，不读取产品事实拼历史结果。首次 Blackbox `activate` 才在同一激活事务中把已批准 exact-version 回测的缺失业务键发布到 `t_scheme_predictions`；revision 回测和 activation 不重写历史产品事实。参与前端历史排行的样本统一要求 `predict_date >= 2025-01-01`；这是输出样本起点，不是训练起点。

当方案已有灰度实盘观察区时，历史回测 runner 必须按 `target_date < gray_target_start` 截断，避免同一 target 同时由 backtest 和 live 区间解释。日频、周频和月频都使用同一条 target 边界；月频仍按自然月 15 号的触发语义计算三日期，不能用 `predict_date` 替代 `target_date` 判断分区。

`target_date` 是回测明细的必填事实字段。runner、Dashboard 和前端月度聚合只能用 `target_date` 归属月份；如果 `t_backtest_predictions` 明细缺 `target_date`，必须 fail-closed。禁止用 `predict_date`、`feature_date`、月份字段或旧 `monthly_metrics` 表推断、替代或回填 `target_date`。

周频公共回测的无信号策略默认是 `skip`。只有明确声明 `no_signal_policy="flat"` 的方案，才能在 feature、target、日期和 label 上下文均有效，且整批 core 输出非空、`week_id` 全部合法、当前 feature key 单独缺少输出时生成平台平记录。该记录必须保留完整 `feature_week_id/target_week_id`、日期、artifact 和 `no_signal_to_flat_v1` 审计字段；core 整体空/非法输出、输入或日历异常不得被捕获补平。source-original/current benchmark 仍只包含原算法实际输出行，平台补平行只进入平台 backtest/live 明细；runner payload 的 `row_count` 表示平台明细总数，`benchmark_row_count` 表示过滤政策行后的 compact benchmark 数量。

### 5.1 已批准的 source-original batch reproduction 例外

默认历史回测优先使用原始算法声明的 source 执行口径；如果该口径本身是 point-in-time，则按 PIT 复现。如果原始方案本身是全历史 batch reproduction，并且算法内部存在固定未来分段、全局校准或一次性 selector 这类无法逐点切片复现的结构时，可以批准为方案级例外。例外必须同时满足：

1. 只适用于历史回测写入 `t_backtest_*`，不得扩散到 gray/live/scheduled live adapter。
2. 对已有 original benchmark 覆盖区间逐行一致；方向、`target_date`、`label/is_correct` 必须零差异。统一平台 `confidence` 不属于必需输出或比较项；算法内部必要数值仍按源算法保真规则验证。
3. 回测输出仍必须使用平台统一日期字段：`predict_date=feature_date`，`target_date` 由平台日历确定。
4. 回测仍必须排除该方案 `target_date >= gray_target_start` 的灰度/实盘区间。
5. 方案 benchmark 证据必须写明为什么不能使用逐点 PIT；被替代的旧运行只保留在 run/数据库审计，不复制到当前架构文档。

若 source-original batch reproduction 的 benchmark row 跨入 gray/live target 区间，该 row 仍不得扩散为 live 数值真值；它只能证明 historical/source-original 口径。gray_live/scheduled_live adapter 与补齐必须继续按 `feature_date` 硬截止，并使用 live-safe oracle 或同口径 live benchmark 验收。

以下三个 2025-05-29 来源批次周度单点源算法曾使用 Native batch reproduction 例外；它们的原 ID canonical 现为 Blackbox，旧例外仅解释已存历史，不能授权恢复旧 runner 或重跑迁移历史：

- `weekly_5y_direct_0529`
- `weekly_7y_cross_d_overlay_0529`
- `weekly_10y_d_overlay_0529`

批准原因是这三个源算法家族的源文件历史评价均为 source-original batch reproduction，候选排行需要复现原始 benchmark 口径，而不是把源算法事后改造成逐周 PIT 口径。`weekly_10y_d_overlay_0529` 的冲突最明显：Model2 固定分段包含 `2025H2_2026`，逐周 PIT 切片在 2025H1 无法构造未来半年度测试段，会导致 2025 年上半年没有有效 D-overlay 当前周信号。`weekly_5y_direct_0529` 和 `weekly_7y_cross_d_overlay_0529` 虽然缺口较小，但逐周切片仍会改变源 benchmark 的样本覆盖和对比口径，因此同样按历史 batch 例外处理。

当前周平均 0529 身份为 `weekly_avg_1y_lgbm_0529`、`weekly_avg_5y_lgbm_0529` 和 `weekly_avg_10y_lgbm_0529`。它们不得复用 `weekly_*` 周度单点方案的 label、Score、Model2、D-overlay 或 point runner；actual/label 固定为 `next_week_average_yield_vs_current_week_average_yield`，即“目标周平均收益率 vs 当前周平均收益率”。source 输出出现内容一致的重复 strict key 时，平台只按既有 strict-key 契约折叠，并在 benchmark summary 记录，不能依赖固定行数或历史 run。

上述周度单点历史例外不扩展 live 读取权限。当前周度单点 Blackbox 使用标准 Request 的截止日期；保留的 W4 周平均 Native adapter 使用 `feature_date=previous_trading_day(predict_date)`，输入 artifact 传 `end_week=feature_week_id`、`as_of_date=feature_date`，不得读取未来周或当前 DB 最新全量数据。

### 5.2 历史批次与灰度区间批次

批量计算是执行优化，不是第四种 prediction phase。平台先冻结 exact scheme version、输入 generation/snapshot/business digest、lineage、`gray_target_start` 和应有 Request 集合，再执行两个边界清晰的批次：

| 分区 | 执行与去向 |
|---|---|
| `target_date < gray_target_start` | 一次持久化历史回测，写入新的 immutable canonical backtest run |
| `target_date >= gray_target_start` 且早于正式调度 target | 激活后按 target 半开区间执行一次 live-safe batch，并由 repository insert-only 写为 `gray_live` |
| 已有 `gray_live` 或 `scheduled_live` 业务键 | 整个授权区间拒绝，不运行算法、不覆盖、不删除后重写 |

灰度区间的每个 Request 必须使用权威任务日历生成自己的 live `predict_date`、`feature_date` 和 `target_date`，输入只能看到 `feature_date` 及以前的数据。一个方案的一个授权区间只解析一次 DataBridge authority，并把它与 producer-ready snapshot receipt 的 generation、refresh date、business digest 和对应输入文件身份精确比对；随后只物化一次私有运行视图并启动一个算法 batch。不得逐日期重复启动进程、读取/裁剪 CSV、重写快照或准备运行视图。

历史与 gray/live 是两个独立、可审计的持久化边界。平台不为了省去历史与灰度之间的一次进程启动而增加跨激活候选表、临时结果文件、Harness 报告或新的生命周期状态。灰度批次全部 Result 合法后，repository 在一个事务中复核 active version、Registry、run、输入 provenance 和所有业务键，再写入全部 prediction 并完成各调度日 run；任一复核或写入失败都不得留下部分 prediction。

批量物化只适用于已证明逐 Request 截止、批内首/中/末独立复算和 predict/backtest 等价，且没有晚于样本 `feature_date` 的固定 `source_end`、未来 test window、全局 selector/calibration 或跨样本未来状态的交付。任一前提不成立时必须改用逐点 live-safe 计算。

最终验收必须证明：canonical backtest 全部早于 gray 起点；live 全部位于 gray 起点及以后且早于正式调度 target；两侧 target 交集为空；方向、置信度、`feature_date`、`target_date` 和 scheme version 与 batch Result 零漂移；每个自然月不因 phase 分区错误出现重复行；Actual、准确率事实和其它方案不变；全部 active 方案 DashboardGate 通过。

## 6. 指标统计口径

预测方向 `predicted_direction=0` 表示“平”。它可能是算法原生平，也可能是 `no_signal_to_flat_v1` 生成的平台无信号平；两者必须通过 `extra.signal_policy_applied` 区分，原生平不得冒充平台补平。这类样本必须计入样本总数和方向分布，但不得进入准确率、上涨准确率、上涨召回率、下跌准确率、下跌召回率等任何指标的分母。

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

回测 summary 还必须单独报告 `policy_generated_flat_count` 及对应的缺失 feature key 清单。source-original/current benchmark 的生成与导出必须过滤 `signal_policy_applied=true` 的平台行，不能把业务输出规则生成的平记录声明为原始算法输出。

历史回测证据的唯一明细源是 `t_backtest_predictions`；产品前端指标的唯一逐点源是 `t_scheme_predictions`。回测发布行以 `backtest_run_id` 追溯证据，并保存 immutable `backtest_actual_direction`；live 行该字段必须为空并关联 Actual 权威表。Dashboard 不得读取回测明细参与逐点选择。

前端展示指标的唯一事实源是 Dashboard 返回的预测明细行。前端必须按 `target_date` 把明细行归属到月份，再调用统一的明细指标计算逻辑生成月度表、候选排行、趋势图和汇总卡。

## 7. 前端展示规则

前端任务格子由 `target_tenor + task_type` 定义。`task_type` 是业务任务类型，不是输入频率，也不是 `horizon` 的别名；固定取值为 `T+1`、`T+5`、`weekly_point`、`weekly_average`、`monthly`、`monthly_average`、`quarterly_average`、`annual_average`。日频任务才按后续交易日步长解释 horizon；Blackbox V2 周均、MID 月均、自然季均和春节年均的 `horizon=1` 表示下一个同类业务桶。周期均值的 `target_date=feature_date+1` 个自然日只是定位下一桶的日期指针，不是一天后的预测目标，也不是目标桶完成日。actual join 必须使用 `target_tenor + target_date + target_rule`，前端分列只能读取 Registry/API 返回的 `task_type`；缺失或非法值必须 fail-closed，不允许根据 `frequency/horizon` 猜列、桶或目标日期。

Dashboard V5 的公开结果类型只按 `target_date` 分类：

- `target_date < 2026-06-01`：`backtest` / 回测。
- `target_date >= 2026-06-01`：`live` / 实盘。

公开 API 和前端不使用物理来源、`predict_date` 或 run `prediction_phase` 判断结果类型，也不返回灰度/正式
实盘阶段。`gray_live` 与 `scheduled_live` 只保留在 scheduler/gap-fill run 审计中。Dashboard 逐点只读取
`t_scheme_predictions`，不存在跨表 preferred/fallback 或第二套去重逻辑。

前端与业务不读取 `anchor_date`。需要展示预测站位或数据截止时，统一显示 `feature_date`。月度行、明细归属、actual join、结果分类和去重均统一按 `target_date`。

前端展示的部署时间只能来自 active `t_scheme_registry.deployed_at`。`deployed_at` 的业务语义是该注册业务方案激活并进入业务可见状态的日期，不是定时任务已生产挂载的证据；缺失时说明 registry 数据不完整，后端 API 和前端都必须 fail-closed。生产调度挂载必须另由对应 installed plist、`launchctl` loaded state 和任务日志共同证明。禁止 hardcode 默认部署日、scheme_id override 或在前端用灰度起点/正式实盘起点替代部署时间。

前端指标展示必须遵守 §6 的两层分母：

- 月度表“样本数”列展示由明细行计算出的 `samples`，包含预测为平的交易日或预测周。
- 月度表、候选排行、趋势图和汇总卡中的整体准确率、上涨准确率、上涨召回率、下跌准确率、下跌召回率均由明细行直接计算；分母只包含预测为“涨”或“跌”的样本，排除预测为平的样本。
- 准确率括号展示 `correct/metric_samples`；不得回退成 `correct/samples`，也不得通过月度行的 precision/recall 反推出 true positive。
- 如果某个需要展示的方案/月度只有 `monthly_metrics` 汇总、没有预测明细行，前端必须 fail-closed，不能从月度汇总反推或回填指标。
- 每日/周度验证明细中，只要预测方向为“平”（`predicted_direction=0` 或前端归一化后 `predicted="平"`），结果列统一展示 `-`，不展示 `✓` 或 `×`。这条展示规则独立于 `actual_direction` 和 `is_correct`，因为“平”不进入指标计算。
- 待验证样本仍展示待验证符号；有方向预测才根据验证结果展示 `✓` 或 `×`。
- 当日频、周频、月中收或周期均值 actual 的源实际值水位尚未覆盖对应 `target_date + target_rule` 时，该样本属于待验证；API 和前端应展示 `actual_direction = null` / 准确率 `--`，不得把它计为错误、缺数据修复项或前端刷新失败。运维排查必须先查对应 actual 水位，再判断是否为后端 join 或前端计算问题；同一目标周期内不同 tenor 的源水位可以不同，已覆盖的 tenor 应立即验证，未覆盖的 tenor 继续待验证。
