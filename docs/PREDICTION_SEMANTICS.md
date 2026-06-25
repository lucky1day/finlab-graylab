# 预测日期与实盘阶段语义

**更新日期**: 2026-06-14

本文是平台关于 `predict_date` / `feature_date` / `target_date` 与灰度实盘阶段的强制语义。前端、后端、回测、SOP、方案文档和测试用例必须使用同一套术语；如与旧文档冲突，以本文为准，并回写对应文档。

Source-backed 方案还必须遵守 [SOURCE_ALGORITHM_FIDELITY.md](SOURCE_ALGORITHM_FIDELITY.md)。日期字段映射是平台适配，不是修改原始算法时间窗口、测试区间或 batch/PIT 口径的许可。

## 1. 三个标准日期字段

| 字段 | 平台含义 | 是否给前端/业务使用 |
|------|----------|:------------------:|
| `predict_date` | 信号发出日 / 调度运行日 | 是 |
| `feature_date` | 数据截止日 / 预测站位日，模型只能使用该日及以前允许可见的数据 | 是 |
| `target_date` | 验证目标日，用于展示、去重、actual join 和月度统计归属 | 是 |

`feature_date` 是平台、业务和前端唯一标准数据截止字段。`anchor_date` 只允许作为方案内部算法变量或历史审计 extra 保留；任何对外语义、前端展示、灰度规则、回测规则都不得依赖 `anchor_date`。如果 `extra` 同时保留 `anchor_date`，它必须等于 `feature_date`。

## 2. 三种运行口径

| 口径 | `prediction_phase` | `predict_date` | `feature_date` | `target_date` | 写库位置 |
|------|--------------------|----------------|----------------|---------------|----------|
| 历史回测 | 不写入实盘 phase | `T` | `T` | `T + horizon` | `t_backtest_*` |
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

benchmark 逐样本核验必须以 `feature_date + target_date + target_tenor + horizon` 为主键；周频方案还必须包含或可唯一映射 `feature_week_id`。`predict_date` 只用于校验信号发出时点：历史回测要求 `predict_date == feature_date`，灰度/正式实盘要求 `predict_date` 是站在 `feature_date` 后按调度规则应发出的日期。

如果原始 benchmark 的某条样本 `target_date` 已进入灰度/实盘观察区，例如 T 在 5 月末而 target 落到 6 月，则这条样本不能强行要求出现在 `t_backtest_predictions`。核验时必须按 `target_date` 分流：

- `target_date < 灰度实盘起点`：与 `t_backtest_predictions.feature_date` 对齐核验。
- `target_date >= 灰度实盘起点`：与 `t_scheme_predictions.feature_date` 对齐核验，并同时校验 `prediction_phase`。

这条规则优先于旧文件列名。旧 benchmark CSV 即使列名仍叫 `predict_date`，也只能解释为 source T / 平台 `feature_date`；新增 benchmark 文件应显式写 `feature_date` 或 `source_t`，避免把原始算法站位日误读为平台信号发出日。

### 2.1.1 Source 执行口径不得被静默改写

原始算法可能是 strict PIT，也可能是一次性 batch、固定历史窗口、月度窗口、walk-forward 或带全局校准的 source-original reproduction。平台必须先分类再执行：

- `source_original_reproduction`: 按原始脚本真实口径复现，current/backtest 应与 source 输出逐样本对齐。
- `source_strict_pit`: 原始脚本本身逐 `feature_date` 硬截止，平台 PIT helper 必须与它等价。
- `platform_live_pit_variant`: 原始交付不是 strict PIT，但业务明确要求构造 live-like PIT 变体；该变体必须获批、命名并记录与 source-original 的差异。

不能因为平台 live 语义需要 `feature_date` 硬截止，就直接修改原始算法内部的 `test_start/test_end/test_ranges`、历史起点、周/月频对齐、特征或投票逻辑。若同一 `feature_date` 的 source-original 与平台 PIT 变体不同，差异必须作为口径差异记录，不能通过调参或改算法抹平。

### 2.2 旧 core 参数名不得直接映射为平台字段

legacy/core 里的参数名不一定等于平台标准字段。遇到 `current_date`、`date`、`predict_date` 等旧参数时，必须先读 core 内部如何使用它，再决定映射到平台的 `predict_date`、`feature_date` 还是 `target_date`；不得只按名字猜。

已确认案例：`t1_daily` 旧 core 曾使用 `current_date` 作为参数名，但内部语义是“目标验证日”，并选择最后一个 `< current_date` 的交易日作为模型站位。因此它现在已重命名为 `target_date`，且不保留旧参数兼容入口。T1 回测最后一条 5 月样本必须是：

```text
target_date  = 2026-05-29
feature_date = 2026-05-28
predict_date = 2026-05-28  # 历史回测中 predict_date=feature_date
```

这不是新增第四类日期字段；它只是把旧 core 的内部参数语义改名到平台已有的 `target_date`。新增方案入库时，若 legacy 参数名含糊，必须在方案文档和 SOP 验收记录中写明它对应的平台字段。

### 2.3 test-window 敏感算法规则

部分源算法把 test window 当作模型选择、ensemble 或信号组合的一部分；这类窗口不是展示参数，改变窗口就可能改变同一个 `feature_date` 的预测结果。`daily_5y_2_v28` 是当前已确认案例：源算法按月度 test window 运行，Phase C 会基于 `test_months` 做 monthly ensemble / signal selection，因此平台不得用连续窗口替代月度窗口。

对 test-window 敏感方案必须遵守：

1. adapter 和 backtest runner 必须共享同一个 inference helper，不得各自拼 `test_start/test_end`。
2. 实盘/灰度必须先确定 `feature_date=T`，再使用 `feature_date` 所在月第一天到 `feature_date` 作为核心预测窗口；窗口结束不得超过 `feature_date`。
3. 回测仍输出 `predict_date=feature_date=T`，但核心预测窗口必须与同一 `feature_date` 的实盘路径一致。
4. benchmark current 侧必须由平台 inference helper 生成，不能复制 source CSV 冒充 current。
5. 逐方案 original benchmark 的 `date/T` 只对齐平台 `feature_date`；如 `target_date` 进入灰度/实盘区间，则与 `t_scheme_predictions.feature_date` 对齐核验。这里的 original benchmark 位于 `schemes/{scheme_id}/benchmarks/`，不是 `source_evidence/benchmark_batches/{benchmark_id}/` 的外部批次证据。
6. helper 只能封装原始算法的执行口径；不得把“更短历史”“同月去年+本月”“previous complete week”等平台便利窗口替代 source 中实际使用的固定历史、batch end 或周频对齐规则。

`daily_5y_2_v28` 的唯一入口是 `schemes.daily_5y_2_v28.inference`：`v28_feature_month_window(feature_date)` 返回当月月初到 `feature_date`，`predict.py` 与 `backtests.daily_5y_2_v28_reproduction` 都必须通过该模块调用 core。

## 3. 灰度实盘规则

灰度实盘用于补齐从灰度 target 起点到正式部署前的实盘观察序列。当前 V28 批次的灰度 target 起点是 `target_date >= 2026-06-01`；后续方案必须按方案生命周期登记自己的灰度起点和正式调度起点，不能把日期写成全局常量。

灰度补齐必须满足：

1. 灰度记录必须标识为 `prediction_phase = gray_live`。
2. 记录的 `predict_date` 仍是应当发出信号的调度日，日频通常为 `T + 1`。
3. `feature_date` 必须是 `T`，所有输入 artifact、辅助输入映射和模型训练窗口都不得越过 `feature_date`。
4. 当前数据库在事后补齐时可能已经拥有 `T+1` 或更晚的数据；补齐逻辑必须显式按 `feature_date` 约束输入，不能只依赖当前 DB 最新状态。
5. 如源表提供 `create_time` 或等价 as-of 字段，灰度补齐应进一步证明数据在预期发出时点可见；没有该证据时，不得把灰度样本宣称为 point-in-time 绩效，只能称为灰度实盘观察。

## 4. 正式实盘规则

正式实盘记录必须标识为 `prediction_phase = scheduled_live`。正式实盘起点来自方案激活并由 scheduler 自然成功发出的第一条记录，不能由灰度 target 起点反推。

正式实盘同样只能使用 `feature_date = T` 及以前可见的数据。日频工作日早盘预测的典型形态是：

```text
predict_date = T + 1
feature_date = T
target_date  = T + horizon
```

周频实盘也遵守同一条 T/T+1 规则：adapter 必须先用交易日历计算 `feature_date = previous_trading_day(predict_date)`，再由 `feature_date` 映射 `feature_week_id`，并以 `end_week=feature_week_id`、`as_of_date=feature_date` 构建周频输入。禁止直接用 `predict_date` 所在周作为 feature week；否则交易日手工运行或灰度补齐可能读到当前周未来数据。

## 5. 回测规则

历史回测必须保持 T 语义：

```text
predict_date = T
feature_date = T
target_date  = T + horizon
```

回测结果只写 `t_backtest_*`，不得读取或复制 `t_scheme_predictions` 中的灰度/正式实盘记录来拼历史结果。参与前端历史排行的样本统一要求 `predict_date >= 2025-01-01`；这是输出样本起点，不是训练起点。训练、筛因子、模型 warmup 和定期更新可使用更早历史数据，但每个预测点的输入和标签可见性都必须严格停在对应 `feature_date`。

当方案已有灰度实盘观察区时，历史回测 runner 必须按 `target_date` 截断，避免同一 target 月同时由 backtest 和 live 区间重复解释。当前 V28 批次的历史回测只保留 `target_date < 2026-06-01`。

`target_date` 是回测明细的必填事实字段。runner、`/api/backtests/factor-lab` 和前端月度聚合只能用 `target_date` 归属月份；如果 `t_backtest_predictions` 明细缺 `target_date`，必须 fail-closed。禁止用 `predict_date`、`feature_date`、月份字段或旧 `monthly_metrics` 表推断、替代或回填 `target_date`。

### 5.1 已批准的 source-original batch reproduction 例外

默认历史回测优先使用原始算法声明的 source 执行口径；如果该口径本身是 point-in-time，则按 PIT 复现。如果原始方案本身是全历史 batch reproduction，并且算法内部存在固定未来分段、全局校准或一次性 selector 这类无法逐点切片复现的结构时，可以批准为方案级例外。例外必须同时满足：

1. 只适用于历史回测写入 `t_backtest_*`，不得扩散到 gray/live/scheduled live adapter。
2. 对已有 original benchmark 覆盖区间逐行一致；方向、`target_date`、`label/is_correct` 必须零差异，`confidence` 只允许浮点舍入误差。
3. 回测输出仍必须使用平台统一日期字段：`predict_date=feature_date`，`target_date` 由平台日历确定。
4. 回测仍必须排除灰度/实盘 target 区间，即当前 V28 批次 `target_date >= 2026-06-01` 不能进入 backtest latest。
5. 文档必须写明为什么不能使用逐点 PIT，以及哪些 run 是被删除或替代的旧口径。

当前已批准的例外是三个 2025-05-29 来源批次周频方案的历史回测：

- `weekly_5y_direct_0529`
- `weekly_7y_cross_d_overlay_0529`
- `weekly_10y_d_overlay_0529`

批准原因是这三个方案的源文件历史评价均为 source-original batch reproduction，候选排行需要复现原始 benchmark 口径，而不是把源算法事后改造成逐周 PIT 口径。`weekly_10y_d_overlay_0529` 的冲突最明显：Model2 固定分段包含 `2025H2_2026`，逐周 PIT 切片在 2025H1 无法构造未来半年度测试段，会导致 2025 年上半年没有有效 D-overlay 当前周信号。`weekly_5y_direct_0529` 和 `weekly_7y_cross_d_overlay_0529` 虽然缺口较小，但逐周切片仍会改变源 benchmark 的样本覆盖和对比口径，因此同样按历史 batch 例外处理。

这三个方案的回测窗口已经对齐为同一历史输出窗口和同一灰度截断边界，但样本总数不强制相同。平台写入的是算法 core 实际产出的“有效信号行”，不是日历周占位行；如果某一周的规则信号为 0、NaN 或被源算法判定为无效，该周就不应被平台补成一条预测。当前 latest 的有效输出为：5Y run_id=`109` 共 71 条，缺 `feature_week_id=202534`；7Y run_id=`110` 共 68 条，缺 `202529/202534/202547/202608`；10Y run_id=`108` 共 72 条，无缺周。该差异是算法输出本身，不是前端隐藏、latest view 分组错误或 DB 日历缺失。

这三个例外只允许用于历史回测和 benchmark 复现。它们的灰度实盘、正式实盘 adapter 仍必须严格遵守周频 T+1/T 规则：`feature_date=previous_trading_day(predict_date)`，输入 artifact 传 `end_week=feature_week_id`、`as_of_date=feature_date`，不得读取未来周或当前 DB 最新全量数据。

## 6. 指标统计口径

预测方向 `predicted_direction=0` 表示“平”或“无方向信号”。这类样本必须计入样本总数和方向分布，但不得进入准确率、上涨准确率、上涨召回率、下跌准确率、下跌召回率等任何指标的分母。

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

例如某月共有 8 条已验证预测，其中 1 条预测为平、3 条方向预测正确、4 条方向预测错误，则样本数展示为 `8`，整体准确率展示为 `3/7`，而不是 `3/8`。前端候选排行、月度详情、后端 live metrics、回测 runner 和 `/api/backtests/factor-lab` 必须遵守同一口径。

历史回测前端指标的唯一事实源是 `t_backtest_predictions` 明细表。`/api/backtests/factor-lab` 必须从 latest run 的明细动态聚合 `monthly_metrics` 和 `summary`；如果 latest run 缺少明细或明细不可评价，接口必须 fail-closed。新代码不得新增、读取或写入独立的回测月度指标汇总表。

前端展示指标的唯一事实源是 API 返回的预测明细行。前端必须按 `target_date` 把明细行归属到月份，再调用统一的明细指标计算逻辑生成月度表、候选排行、趋势图和汇总卡。API 返回的 `monthly_metrics` 只允许作为传输上下文、调试信息或后端对照信息，不得作为前端展示指标的计算来源。

## 7. 前端展示规则

前端任务格子由 `target_tenor + task_type` 定义。`task_type` 是业务任务类型，不是输入频率，也不是 `horizon` 的别名；固定取值为 `T+1`、`T+5`、`weekly_point`、`weekly_average`、`monthly`。`horizon` 继续用于 `target_date = feature_date + horizon` 和 actual join，`frequency` 继续用于输入与 actuals 类型判断；前端分列只能读取 registry/API 返回的 `task_type`。缺失或非法 `task_type` 必须 fail-closed，不允许根据 `frequency/horizon` 猜列。

前端可以展示灰度实盘和正式实盘，但必须能区分 `prediction_phase`：

- `gray_live`：灰度实盘观察。
- `scheduled_live`：正式实盘。

前端与业务不读取 `anchor_date`。需要展示预测站位或数据截止时，统一显示 `feature_date`。月度行、明细归属、actual join 和去重仍统一按 `target_date`。

前端展示的部署时间只能来自 active `t_scheme_registry.deployed_at`。`deployed_at` 的业务语义是该注册业务方案挂载对应定时任务的日期；缺失时说明 registry 数据不完整，后端 API 和前端都必须 fail-closed。禁止 hardcode 默认部署日、scheme_id override 或在前端用灰度起点/正式实盘起点替代部署时间。

前端指标展示必须遵守 §6 的两层分母：

- 月度表“样本数”列展示由明细行计算出的 `samples`，包含预测为平的交易日或预测周。
- 月度表、候选排行、趋势图和汇总卡中的整体准确率、上涨准确率、上涨召回率、下跌准确率、下跌召回率均由明细行直接计算；分母只包含预测为“涨”或“跌”的样本，排除预测为平的样本。
- 准确率括号展示 `correct/metric_samples`；不得回退成 `correct/samples`，也不得通过月度行的 precision/recall 反推出 true positive。
- 如果某个需要展示的方案/月度只有 `monthly_metrics` 汇总、没有预测明细行，前端必须 fail-closed，不能从月度汇总反推或回填指标。
- 每日/周度验证明细中，只要预测方向为“平”（`predicted_direction=0` 或前端归一化后 `predicted="平"`），结果列统一展示 `-`，不展示 `✓` 或 `×`。这条展示规则独立于 `actual_direction` 和 `is_correct`，因为“平”不进入指标计算。
- 待验证样本仍展示待验证符号；有方向预测才根据验证结果展示 `✓` 或 `×`。

## 8. 当前 V28 判定

对 `daily_5y_2_v28` 当前已知记录：

| run_id | 日期范围 | 阶段 |
|--------|----------|------|
| `39`-`41`、`43`-`51`、`58` | `predict_date=2026-05-26` 至 `2026-06-11`，`target_date=2026-06-01` 至 `2026-06-17` | `gray_live` |
| `42` | 旧连续 test window 口径写入的灰度明细已删除，`t_scheme_runs/t_scheme_run_log` 保留审计 | 历史审计 |
| `52` | `predict_date=2026-06-12`，`feature_date=2026-06-11`，`target_date=2026-06-18` | `scheduled_live` |

这些阶段标识已由迁移 `010_prediction_semantics.sql` 落到 `t_scheme_predictions.prediction_phase` 和 `t_scheme_runs.prediction_phase`，并由 `/api/metrics/{scheme_id}` 的 `daily_rows[].prediction_phase` 与 `phase_ranges` 对前端输出。`run_id=39`-`41`、`43`-`51`、`58` 是灰度实盘，`run_id=52` 是正式 scheduler 实盘，二者不得混称。

2026-06-14 修复确认：旧 `run_id=42` 的 `feature_date=2026-05-28` 灰度明细使用了连续窗口 `2024-07-01..feature_date`，与源算法 May 2026 月度 test window 不一致，预测方向曾偏离 original benchmark。该明细已受控删除，并用共享 inference helper 重跑为 `run_id=58`：`predict_date=2026-05-29`、`feature_date=2026-05-28`、`target_date=2026-06-04`、`prediction_phase=gray_live`、`predicted_direction=1`、`confidence=1.0`。后续 V28 回测 latest 是否重落库，必须先完成 no-persist diff 并经人工确认。
