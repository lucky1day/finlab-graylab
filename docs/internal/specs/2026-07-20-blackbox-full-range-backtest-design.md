# Blackbox V2 完整区间分批回测设计

**文档状态**：`APPROVED_DESIGN`

**决策日期**：2026-07-20，`Asia/Shanghai`

## 1. 背景与根因

当前四个 `1Y国债活跃 × T+5` Blackbox V2 方案各自只有 100 条持久化回测。其开始时间落在 2026 年 2 月，不是上游算法不支持更早历史，而是平台把两个不同约束混成了一个约束：

- Blackbox Contract 1.0 正确要求一次交付脚本调用只接收 `1..100` 条 Request；
- 平台 Persist Backtest Gate 错误要求整次持久化回测只能有 100 条，并从全部合格历史样本中选取最后 100 条。

平台 runner 已经具备按 Runtime Profile 的 `max_batch_requests=100` 自动分批执行能力。因此本次不修改上游算法交付脚本，而是把持久化回测升级为“完整日期区间、平台自动分批、全部成功后原子写入”。

同时修复一个独立的前端合并问题：当回测和 live 位于同一个目标月份时，当前前端以 live 明细替换该月全部回测明细，导致 2026 年 7 月的 13 条回测被一条 pending `gray_live` 覆盖，页面统计从数据库中的 100 条错误显示为 87 条。

## 2. 目标与非目标

### 2.1 目标

- 持久化回测默认从 `2025-01-01` 开始，并允许 CLI 显式覆盖起点。
- 回测截止继续使用 `target_date < predict_date`，不侵入当前灰度或正式实盘观察区。
- 完整区间可超过 100 条，由平台拆成多个不超过 100 条的交付脚本调用。
- 所有批次全部成功后，只新增一个不可变回测 run，并在单一数据库事务内写入 run、预测明细和月度指标。
- 授权 token 绑定实际回测起点，签发后不能扩大或改变持久化范围。
- API 自动选择新的 canonical latest success run；旧的 100 条 run 保留为审计历史。
- 前端同月回测和 `gray_live/scheduled_live` 明细并存，任何 live 行不得覆盖历史回测样本。
- 更新上游交付 SOP 和平台入库 SOP，明确“单批上限”和“完整持久化区间”的区别。

### 2.2 非目标

- 不修改四个方案的 `.py/.json` 交付文件、模型窗口、特征、方向映射或 Metadata。
- 不改变 Blackbox Contract 1.0 的单次 `1..100` Request 限制。
- 不删除、覆盖或原地扩充已存在的 100 条回测 run。
- 不新增 live 预测，不改变 scheduler 计划，也不因本次回测升级重启 scheduler。
- 回测继续明确标记为 current snapshot as-of replay，不宣称历史 vintage PIT。

## 3. 采用方案

采用平台完整区间编排方案：

1. 平台按日期策略生成完整 HistoricalCase 列表。
2. 复用 Blackbox runner，根据 Runtime Profile 自动分成 `<=100` 条的子批次。
3. 在同一个总执行预算内按序执行全部子批次。
4. 校验合并后的数量、顺序、日期唯一性和 Request/Result echo。
5. 生成一次完整 RunOutput 和全区间月度指标。
6. 全部计算成功后，再通过一个数据库事务写入一个回测 run。

不采用以下方案：

- 外层脚本逐批持久化多个 run：会造成 API、月度指标和审计碎片化，并允许部分批次入库。
- 放宽上游脚本为单次接收超过 100 条：破坏现有契约且重复平台 runner 已有能力。

## 4. 日期与 CLI 契约

Persist Backtest Gate 新增：

```text
--backtest-start-date YYYY-MM-DD
```

规则如下：

- 未提供时解析为 `2025-01-01`。
- 提供时必须是严格 ISO 日期，并满足起点早于 `predict_date`。
- `predict_date` 是本次历史回测的 exclusive target cutoff：只选择 `target_date < predict_date` 的样本。
- 最早样本是 `predict_date >= backtest_start_date` 的第一个合格算法站位日，不要求自然起点当天一定是交易日。
- `--sample-size` 只用于 no-persist 稳定性 Gate；显式同时使用 `--persist --sample-size` 时 fail-closed。
- no-persist 的现有 1..1000 样本和不同批次切分一致性测试保持不变。

示例：

```bash
python -m harness gate backtest \
  --scheme-id one_y_t5_liq_excess_a_v1 \
  --predict-date 2026-07-20 \
  --persist \
  --backtest-start-date 2025-01-01 \
  --timeout-sec 1800 \
  --authorize <token>
```

## 5. 授权设计

`backtest_persist` token 增加动作专属字段 `backtest_start_date`：

- token 继续绑定 `scheme_id`、`action`、`scheme_version`、`harness_run_id` 和 `predict_date`；
- `backtest_start_date` 写入经过默认值解析和 ISO 校验后的实际起点；
- Gate 执行参数必须与 token 中的起点完全一致；
- 缺少该字段的旧 `backtest_persist` token 不得用于新持久化流程；
- 其它授权动作的 token schema 保持不变；
- token 继续要求 HMAC、一次性消费、非空签发人和最多 900 秒有效期。

计算阶段失败时不消费 token，也不写业务表。进入提交阶段后先记录授权审计并消费 token；若随后的数据库事务失败，业务表整体回滚，但 token 保持已消费，重试必须重新签发。

## 6. 执行预算与合并校验

完整回测使用单一 `BacktestExecutionBudget`：

- `deadline_monotonic` 来自 Gate 的 `--timeout-sec`；
- `max_subprocesses = ceil(total_requests / profile.max_batch_requests)`；
- runner 的每个子进程同时受 Runtime Profile 单批限制和整次 Gate 总预算限制；
- 到达总超时、超出子进程数量、任一批次非零退出或 Contract 校验失败时，整次 Gate 失败。

合并结果必须满足：

- Result 总数严格等于 HistoricalCase 总数；
- Result 顺序与 Request 顺序一致；
- `request_id/predict_date/feature_date/target_date` echo 一致；
- `predict_date` 和 `request_id` 全区间唯一；
- `predicted_direction` 只能是 `-1/0/1`；
- 月度指标非空，且由完整合并后的明细重新计算。

## 7. 持久化、幂等与 latest 语义

- 每次授权成功的完整回测追加一个 immutable run，不修改旧 run。
- `benchmark_id` 继续绑定 base scheme 和最新通过的 all-stage `harness_run_id`。
- 同一 all-stage run 使用新 token 重试时允许追加新 run；canonical latest view 按现有 latest-success 规则选择最后成功记录。
- 一个 `persist_backtest_output_atomic` 事务写入 run、所有 prediction、所有 monthly metric，并把 run 更新为 success。
- 任一 insert 数量不匹配时事务回滚，不能出现只有部分批次的 success run。
- 受保护表增量校验不再写死 100，而是要求：run `+1`、prediction `+len(cases)`、monthly metric `+len(output.monthly_metrics)`。
- evidence 和 run summary 记录：请求总数、实际起止日、批次数、各批行数、scheme version、harness run、generation、snapshot、总预算和 replay semantics。

已有四个 100 条 run 原样保留。新完整 run 成功后，API 按现有 runtime scope 的 canonical latest 选择新 run，前端不再展示旧 run。

## 8. 前端同月合并规则

前端候选数据由 backtest 和 live 两类来源组成：

- 月度汇总继续按 `month + source` 分为历史回测和实盘观察两行；
- 日明细不能把 `dailyRowsByMonth[month]` 整体替换为 live 行；
- 合并时保留该月全部 backtest 行，再追加该月 `gray_live/scheduled_live` 行；
- 排行统计根据当前选择的来源读取相应明细，pending actual 不进入准确率分母，但不会删除或隐藏同月回测样本；
- 如不同来源未来出现相同目标日期，仍以来源字段隔离，不做跨来源覆盖。

该规则修复当前“数据库 100 条、页面只显示 87 条”的问题。

## 9. SOP 更新

上游交付 SOP 明确：

- 一次 `backtest` CLI 调用只需支持 1..100 条 Request；
- 平台可以针对同一 scheme version、snapshot 和 generation 多次调用脚本；
- 算法必须保证结果不受平台批次大小、分区方式和 Request 顺序影响；
- 上游不得通过扩大单批上限来实现完整历史回测。

平台入库 SOP 明确：

- 完整持久化回测由日期区间定义，不由单批样本数定义；
- 默认起点、可覆盖参数、exclusive target cutoff 和灰度边界；
- 平台自动分批、全量合并、全成全败写入；
- 授权绑定起点，token 不跨方案、不跨版本、不跨 harness run、不跨日期范围复用；
- latest run、历史 run 保留和 current snapshot replay 声明。

## 10. 测试策略

遵循测试先行：先增加能够复现当前缺陷的失败测试，再改生产代码。

### 10.1 历史样本与 runner

- 不限总数时返回起点以来全部合格 HistoricalCase；
- 默认起点和显式覆盖起点正确；
- 非法或不成立的日期范围 fail-closed；
- 超过 100 条时实际拆成多个 `<=100` 批次；
- 中间批次失败时没有 RunOutput 可进入持久化；
- 全区间顺序、echo、唯一性和月度指标正确。

### 10.2 Gate、CLI 与授权

- persist 默认解析为 `2025-01-01`；
- persist 显式起点进入 GateContext、token 和 evidence；
- token 起点缺失或不匹配时拒绝；
- persist 与显式 sample-size 组合拒绝；
- 事务成功只新增一个 run，动态 prediction/metric 增量正确；
- 事务异常时三个回测表零增量。

### 10.3 API 与前端

- canonical latest 返回新完整 run，而旧 100 条 run 保持可审计；
- 同一目标月包含 backtest 和 live 时，日明细均被保留；
- backtest 排行样本数不因同月 pending live 减少；
- live pending 不进入准确率分母；
- 四候选名称、任务格子和 scheme version 隐藏规则保持不变。

### 10.4 回归

至少执行 Blackbox V2、scheduler、backend API、frontend 和 onboarding docs 相关测试，随后执行全量测试。完成前按 verification-before-completion 规则重新运行最终验证，不能引用改动前结果。

## 11. 四方案生产回测刷新

代码、文档和测试通过后，对以下 active 方案逐个执行：

- `one_y_t5_liq_excess_a_v1`
- `one_y_t5_liq_excess_a_w252_l7_v1`
- `one_y_t5_liq_excess_a_w350_l7_v1`
- `one_y_t5_liq_excess_b_w252_l7_v1`

每个方案执行流程：

1. 使用当天有效 DataBridge generation 重新运行 `--stage all`；
2. 签发绑定 exact scheme version、latest passed all-stage run、`predict_date=2026-07-20` 和 `backtest_start_date=2025-01-01` 的独立 token；
3. 执行完整持久化回测；
4. 核对新 run、动态明细数、非空月度指标和日期边界；
5. 核对 API 已选择新 run，旧 run 仍存在但不再是默认展示；
6. 任一方案失败时停止该方案的后续动作，不修改算法脚本，不手改数据库。

本次不重复 activation，不新增 `gray_live`，也不重启 scheduler。

## 12. 最终验收

- 四个方案仍为 active，Registry 和 scheduler 配置不发生意外变化；
- 每个方案 latest backtest 的最早 `predict_date` 是 `2025-01-01` 当日或之后的首个合格站位日；
- 每个方案 latest backtest 的所有行满足 `target_date < 2026-07-20`；
- 每个方案明细数等于平台生成的完整合格 HistoricalCase 数，不使用预设常数冒充验收；
- 所有交付脚本调用批次均不超过 100 条；
- 月度指标覆盖完整区间且非空；
- API 与数据库逐方案行数一致；
- `1Y国债活跃 × T+5` 任务格子仍显示四个候选；
- 同月 backtest/live 并存，页面不再把 100 条显示成 87 条；
- actual 未到的 live 行继续显示 pending，不人工补写；
- 保存机器 evidence、页面截图和控制台零错误记录，并更新当前状态与生产准备文档。
