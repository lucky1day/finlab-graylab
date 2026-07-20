# 历史归档：SCHEME_ONBOARDING_T0.md

> 文档状态：HISTORICAL
> 适用运行时：native_adapter（冻结前历史）
> 目标读者：历史审计人员
> 最后核验日期：2026-07-19
> 原文件 SHA256：`42ca39088d2c43e8c2986dabdd0ce22a5090c20f66bcb9e8b7112eef4ae170d7`
> 当前替代文档：[../../sop/NATIVE_V1_MAINTENANCE_T0.md](../../sop/NATIVE_V1_MAINTENANCE_T0.md)

本文件保留冻结前原文；仅将相对链接迁移到归档后的有效路径。不得用于新增方案或当前验收。

## 原始正文

# 新增方案 T0 强约束范式

**更新日期**：2026-07-06
**适用范围**：任何新增预测方案（daily / weekly；未来 monthly 也按同一范式扩展）。

> 这是新增方案前的 **T0 必读文档**。它只定义不可破坏的范式和 gate 顺序，不替代详细 SOP。执行细节继续看 [PREDICTION_SEMANTICS.md](../../architecture/PREDICTION_SEMANTICS.md)、[SOURCE_ALGORITHM_FIDELITY.md](../../architecture/SOURCE_ALGORITHM_FIDELITY.md)、[SCHEME_CONTRACT.md](../SCHEME_CONTRACT.md) 和 [SCHEME_ONBOARDING_SOP.md](../../sop/SCHEME_ONBOARDING_SOP.md)。

## 0. 必读顺序

新增方案开工前，按顺序读：

1. 本文：确认新增方案的不可破坏边界。
2. [PREDICTION_SEMANTICS.md](../../architecture/PREDICTION_SEMANTICS.md)：确认 `predict_date` / `feature_date` / `target_date` / `prediction_phase` 的唯一语义。
3. [SOURCE_ALGORITHM_FIDELITY.md](../../architecture/SOURCE_ALGORITHM_FIDELITY.md)：确认 source-backed 方案不得修改原始算法逻辑。
4. [SCHEME_CONTRACT.md](../SCHEME_CONTRACT.md)：确认 config / predict.py / core 的机器契约。
5. [SCHEME_ONBOARDING_SOP.md](../../sop/SCHEME_ONBOARDING_SOP.md)：按 gate 执行完整入库。

## 1. 新增方案不改框架

普通方案入库的默认边界是：只接入一个新的算法方案，不顺手改平台能力。开工前必须先确认本次任务属于普通方案入库，还是平台能力改造 + 方案入库。

普通新增方案只能新增或修改这些位置：

- `schemes/{scheme_id}/`
- `schemes/{scheme_id}/benchmarks/`
- `backtests/{scheme_id}_reproduction.py`
- `tests/test_{scheme_id}.py`
- `tests/test_{scheme_id}_backtest.py`
- 只服务该方案、且命名含 `{scheme_id}` 的测试文件
- `docs/CURRENT_STATUS.md` 中该方案的状态记录
- `docs/legacy_sources/` 中该方案的来源归档或说明

普通新增方案不得修改这些位置或语义：

- 已有方案目录：`schemes/{other_scheme_id}/`
- 公共输入层：`shared/data_service.py`、`shared/input_artifacts.py`
- 公共日历层：`shared/calendar_service.py`
- 公共模型与版本层：`shared/models.py`、`shared/versioning.py`
- 调度和写库层：`scheduler/`
- 后端 API 和前端服务层：`backend/`
- 横切 gate 和契约层：`harness/`
- 公共回测框架：`backtests/_base_runner.py`、`backtests/repository.py`
- 前端：`frontend/`
- DB schema / 迁移：`migrations/`
- 部署配置：`deploy/`
- 项目依赖和运行环境：`pyproject.toml`、conda 环境定义、launchd plist

如果新方案看起来必须改上述公共层、DB schema、API、前端列、registry 规则、`task_type` 枚举、公共日历或公共输入 artifact，立即停止普通入库流程。这类变更必须先被定义为平台能力改造，单独评审、单独分支、单独验证；平台改造完成并合入开发分支后，再重新按本 SOP 入库方案。

不得为了让单个方案通过 gate 而临时放宽公共层、添加静默 fallback、修改已有方案输出、修改公共 benchmark 规则，或在 adapter/backtest runner 中绕过统一输入和写库边界。

唯一允许的慢速执行适配是方案级 `config.yaml.schedule.timeout_sec`：它属于 scheduler/executor 等待预算，不属于算法语义。不得为了规避 timeout 而缩短 source 窗口、复用旧信号、跳过 baseline 或改变 fallback。

## 2. 四条平台不变量

1. **输入单点**：方案 adapter 和 backtest runner 只能通过 `shared.input_artifacts` 取输入。
   - daily 用 `build_daily_input_artifact()`。
   - weekly 用 `build_weekly_input_artifact()`。
   - monthly 用 `build_monthly_input_artifact()`。
   - daily 方案如果依赖 weekly/monthly 辅助数据，必须在 `config.yaml` 的 `input_spec.auxiliary_inputs` 声明辅助频率、data_version 和 required_columns；adapter/backtest runner 仍只能通过 `shared.input_artifacts` 构造输入，禁止自拼 DB 输入或读取外部 CSV。
2. **写库单点**：实盘预测只通过 `scheduler.executor` / `scheduler.repository` 写库；回测只通过 `backtests.repository` 写库。
3. **core 纯净**：`schemes/{scheme_id}/core/` 不访问 DB、不写库、不跨方案 import、不调用调度器。
4. **源算法保真**：source-backed 方案不得修改原始算法逻辑。时间起点、窗口、特征、周/月频对齐、模型参数、投票/fallback、内部 score 映射都按原始脚本复现；平台只能做 I/O、日期、落库和审计适配。
   - 移植 source runner 时，只能移动原始 runner 明确 patch 的日期窗口；未被 patch 的固定算法锚点必须保留。例如 10Y02 `latest_oos` 的 `IC screening` 截止点仍是原始 `2024-01-01`，不能跟随 batch `test_start=2025-05-01` 移动。
   - 回测 runner 的 monthly/fast path 也属于 source 口径：分组键、每组 `source_end`、`current_start/current_end` 和抽样窗口必须与原始算法一致。若 source 按 `target_date` 月份生成 target-date 回测，平台不得改成 feature 月分组或全局窗口。
   - 跨灰度边界的 benchmark 必须先判定每行 role。source-original batch 若固定未来 `source_end`，只能验收 historical backtest；live 行必须使用 `feature_date` 硬截止和 live-safe oracle。`TOTAL_BAD=0` 只说明 live 结构/版本/scope 通过，不说明 live 内部数值等于 raw source batch。
   - 所有改动必须先做 L0/L1/L2 分级。L0 是平台外壳适配；L1 是原始 runner 明确 patch 的上下文传递；L2 是算法内部改动，默认禁止。移动筛因子截止点、替换 test sequence、替换 target-date 分组键、改变特征列顺序、VT/selector/streak/fallback 或内部 score 映射，均属于 L2。

## 3. 日期语义不许混

- `predict_date` 是信号发出日 / 调度运行日。
- `feature_date` 是数据截止日 / 预测站位日，是前端和业务使用的唯一数据截止字段。
- `target_date` 是展示、分组、去重、actual join、月度指标、唯一键的日期。
- `anchor_date` 不得作为业务字段使用；如方案内部或审计 extra 保留，必须等于 `feature_date`。
- 回测必须满足 `predict_date = feature_date = T`，`target_date = T + horizon`。
- 灰度实盘和正式实盘都必须满足 `predict_date = T + 1`，`feature_date = T`，`target_date = T + horizon`。
- 月度方案是唯一当前例外：若 source 声明每月自然 15 号预测，则 `predict_date` 必须保留自然月 15 号，无论是否交易日；`feature_date` 取当前月 15 号及以前最近交易日，`target_date` 取目标月 15 号及以前最近交易日。
- 全平台历史回测输出样本统一从 `predict_date >= 2025-01-01` 开始；日频/月频方案在 `config.yaml` 写 `backtest.start_date: "2025-01-01"`，周频方案写 `backtest.predict_start_date: "2025-01-01"`。
- 历史训练、筛因子、模型更新、warmup 和输入 artifact 可以使用 `2025-01-01` 之前的数据；不要把训练起点误当成回测输出样本起点。
- 灰度实盘也算实盘，必须标识 `prediction_phase = gray_live`；正式 scheduler 自然发出的实盘标识 `prediction_phase = scheduled_live`。
- 灰度实盘观察起点按方案级 `target_date` 判定。当前 V28 批次起点为 `target_date >= 2026-06-01`；历史回测必须只覆盖起点之前的 target，不得把 live target 月写进 `t_backtest_*`。
- `deployed_at` / 前端“部署时间”只是展示字段，不参与月份归属、回测截断、实盘回补范围或唯一键计算。
- `t_scheme_predictions` 唯一语义是 `(scheme_id, target_tenor, horizon, target_date)`，写入必须保持 UPSERT 语义。
- 新增方案不得依赖 serving pointer 来决定前端展示哪条预测。

## 3.1 指标统计和前端明细口径

- 前端任务格子由 `target_tenor + task_type` 定义，`task_type` 只允许 `T+1`、`T+5`、`weekly_point`、`weekly_average`、`monthly`。
- `frequency` 和 `horizon` 仍分别表达输入频率和目标日计算规则，但前端不得再用它们推断任务格子列。
- `predicted_direction=0` 表示预测为“平”或无方向信号。
- 月度样本数必须包含预测为“涨/跌/平”的全部已验证交易日或预测周。
- 所有准确率、precision、recall 指标必须排除预测为“平”的样本；分母使用 `metric_samples` 或 `metric_*_dist`，不得使用总样本数 `samples`。
- `correct` 只统计有方向预测中的正确数；预测为“平”的样本既不算正确，也不算错误。
- 前端每日/周度验证表中，预测为“平”的结果列统一显示 `-`，不得显示 `×` 或 `✓`。

## 3.2 confidence 术语

- `confidence` 是平台统一预测记录里的可选数值字段，用来承接原始算法的置信度、概率或分数（例如 `probability` / `score` / `prob_up`），不是平台额外生成的新标签。
- 如果原始算法没有天然 `confidence`，可以使用确定性的代理数值，但 original/current 两侧必须使用同一映射，并在 `CURRENT_STATUS.md` 写清。
- CompareGate 的 `max_confidence_abs_diff` / `mean_confidence_abs_diff` 只表示 original/current 两份 benchmark 的 `confidence` 数值差异；`1e-16` 量级属于浮点舍入误差，等同于 0。
- 方案正确性先看 `predicted_direction` 零容差；`confidence` 是辅助一致性检查，不能用来替代方向一致。

## 4. 频率分支规则

| 项 | daily | weekly | monthly |
|----|-------|--------|---------|
| `horizon` | `1` 或 `5` | 当前用 `6` | 当前 0629 月度用 `30`，实际目标日由自然月 15 号规则推导 |
| 输入入口 | `build_daily_input_artifact()`；如依赖 weekly/monthly，声明 `input_spec.auxiliary_inputs` 后再调用对应 artifact builder | `build_weekly_input_artifact()` | `build_monthly_input_artifact()` |
| 日期来源 | 交易日历 / 源数据日期 | `week_id` 必须来自 `api_wind_date`，经 `shared.calendar_service` | 自然月 15 号触发；`feature_date` / `target_date` 取对应月 15 号及以前最近交易日 |
| target 规则 | 按 T+N 目标交易日 | `feature_week_id` 的下一实际 DB 周 `target_week_id`，目标日为该周最后交易日 | `target_rule=next_month_observation_yield_vs_feature_month_observation_yield`，actual 为目标月观测收益率 vs 当前 feature 月观测收益率 |
| 回测样本起点 | `backtest.start_date: "2025-01-01"`，按 `predict_date` 过滤输出样本 | `backtest.predict_start_date: "2025-01-01"`，`start_week/end_week` 仍是输入/训练范围 | `backtest.start_date: "2025-01-01"`，输出样本仍按自然 15 号 `predict_date` 枚举 |
| 禁止项 | 用 `predict_date` 做展示月 | 任何 `*_to_friday` / `*_to_monday` / 计算型 week_id 作为实盘或回测对齐依据 | 把 15 号顺延为交易日 predict_date；用 `predict_date` 或部署时间切分 gray/backtest；让 target 月同时进入 backtest 和 live |
| 数据加载 | 覆盖特征窗口和 target 计算所需数据 | live/gray 必须 `end_week=feature_week_id`、`as_of_date=feature_date`；不得读取 feature 周之后的周频原始行 | live/gray 必须以 `feature_date` 硬截止，不能因当前 DB 已有目标月或后续月数据而读未来 |

周度方案尤其要验证：周六 `predict_date` 不是交易日时，只能向前找最近 DB 周作为 `feature_week_id`；`target_week_id/target_date` 必须由 DB 日历从 `feature_week_id` 推导到下一实际周及其最后交易日，不允许用公式 `week_id + 1` 或未来周数据存在性决定 target。若源周历出现单个交易日提前跳周、随后非交易日回落的孤立 forward jump，只能依赖 `shared.week_calendar_normalizer` 的只读归一化，不能在方案里另写周历修补逻辑。

若源周历在调度日附近提前切周，平台可以在 `shared.prediction_context` 做受限日历 fallback 来确定完整输入周。输入 artifact 必须包含当前 `feature_week_id` 且方案必要字段有效；输入水位、日历、模型、超时、代码异常或 core 整体空/非法输出均必须 fail-closed。只有经批准采用 `no_signal_to_flat_v1` 的投票类方案，才可在非空合法 core 结果明确缺少当前 feature key 时生成带政策审计字段的平信号。任何方案都不得把上一周预测、旧投票或旧 selector 状态复制成当前周预测。

## 5. 源文件对比是激活前强制项

如果方案来自原始脚本或原始输出文件，激活前必须完成：

1. 原始脚本 / 原始输出生成逐方案 original benchmark。
2. 入库后框架代码生成 current benchmark。
3. 逐样本对齐，原始算法 source T 对齐平台 `feature_date`，`predicted_direction` 零容差。
4. Source-backed 方案必须把原始算法能导出的内部字段一并放入 original/current benchmark，例如 `vote_score`、baseline `*_score`/`*_vs`、`*_dir`/`*_sign`、probability/confidence；只看最终方向不得进入落库或激活授权。
5. 四份 benchmark 文件落在 `schemes/{scheme_id}/benchmarks/`。
6. `config.yaml` 设置 `backtest.benchmark_required: true`。
6. `harness onboard --stage all` 中 CompareGate 必须是 `passed`，不能是 `skipped`。

这里的 original benchmark 指 `schemes/{scheme_id}/benchmarks/original_predictions_sample.csv` 等逐方案基准文件，不是 `source_evidence/benchmark_batches/{benchmark_id}/` 的批次级外部证据归档。benchmark CSV 至少包含 `feature_date(or source_t)/target_date/tenor(or target_tenor)/direction/confidence`；source-backed 多 baseline 方案还必须保留内部 `vote_score` 和 baseline score/dir 列，周度方案还必须保留 `feature_week_id` 等审计列。历史旧列名 `predict_date/date` 只能解释为原始算法 source T，也就是平台 `feature_date`，不得解释为实盘信号发出日。月度指标、前端展示、回测/live 分区仍按 `target_date` 归属；跨灰度边界的 benchmark 样本要按 `target_date` 和 benchmark role 分流，历史/source-original 行对 `t_backtest_predictions` 核验，同执行口径 live 行才对 `t_scheme_predictions` 核验，否则用 live-safe oracle。

Source `latest_oos` / batch 结果不自动等于平台 canonical benchmark。若 batch 是一次性事后窗口生成，它可能包含 later test window、selector/streak 状态或标签可见性，与 live-like strict PIT 不一致。平台 current/backtest/live 结果必须先声明 source 执行口径：`source_original_reproduction` 按原始 batch/window 完整复现，`source_strict_pit` 按原始 PIT 入口复现，`platform_live_pit_variant` 则必须获批并记录与 source-original 的差异。不得为了让 CompareGate 通过而复制 batch 输出，也不得手工补预测结果。

同时，source batch 与 strict PIT 的差异不能成为修改算法内部逻辑的理由。必须先按 [SOURCE_ALGORITHM_FIDELITY.md](../../architecture/SOURCE_ALGORITHM_FIDELITY.md) 声明 source 执行口径：复现 source-original 就按原始 batch/window 完整复现；构造平台 live-like PIT 变体则必须获批并单独命名，且只能改变外层传入的可见数据截止/上下文，不能改特征、模型、投票或 fallback。

source-original benchmark 跨到 gray/live 区间时，不得自动要求 live 逐日内部 score 与 source batch 相等。若 source batch 固定 `source_end` 晚于样本 `feature_date`，它只能验收 source-original backtest；gray_live/scheduled_live 必须保持 `feature_date` 硬截止，并用同一 live-safe 截止生成的 oracle 验收。任何文档、报告或口头状态都必须写清楚这是 source-original backtest、source strict PIT，还是 platform live PIT variant。若同一份 `original_predictions_sample.csv` 同时包含 historical 与 gray/live target，必须在状态文档或 summary 中把每行拆成 `historical/source-original`、`live-same-context` 或 `source-evidence-only`；只有同执行口径行能被宣称与 DB/API 完全一致。

纯框架内实验方案如果没有原始基准，必须在 `docs/CURRENT_STATUS.md` 明确说明为什么 CompareGate 可以没有 original benchmark。

## 6. 实盘回补按 target_date

激活后必须补齐灰度观察起点以来应当存在的实盘预测。灰度补齐写入实盘预测表，但必须标识为 `gray_live`，不能和正式 scheduler 自然发出的 `scheduled_live` 混淆。

- 回补范围按 `target_date >= 2026-06-01` 判断。
- `predict_date` 按调度日历反推，可以早于灰度起点。
- 每条补齐记录必须显式满足 `feature_date = T`，输入 artifact、辅助周/月映射和模型训练窗口都不能越过 `feature_date`；禁止因为当前 DB 已有 `T+1` 或更晚数据而读入未来信息。
- 周度例子：`target_date=2026-06-05` 对应 `predict_date=2026-05-30`；下一条 `target_date=2026-06-12` 对应 `predict_date=2026-06-06`。
- 月度例子：灰度起点为 `target_date >= 2026-06-01` 时，`predict_date=2026-05-15 -> target_date=2026-06-15` 已是灰度实盘，必须写入 `t_scheme_predictions.prediction_phase=gray_live`，不能留在 latest historical backtest；`predict_date=2026-06-15 -> target_date=2026-07-15` 同样是灰度实盘。
- 验收时同时查 `t_scheme_predictions`、`t_scheme_run_log` 和 API 返回。

不要只按 `predict_date >= 2026-06-01` 回补；这会漏掉周度 6 月第一条目标周，也会漏掉月度 `predict_date=2026-05-15 -> target_date=2026-06-15` 的首个灰度 target 月。

## 7. Gate 顺序

新增方案按以下顺序推进：

1. Intake：确认 `scheme_id`、frequency、horizon、tenors、target rule、cron、原始算法来源。
2. Normalize：归档原始算法，拆出纯 core，`predict.py` 只做 adapter。
3. Input Gate：确认公共输入入口。
4. Static Gate：`python -m harness onboard {scheme_id} --stage static`。
5. Unit Gate：补测试。
6. Benchmark：准备 source/current benchmark。
7. Dry-run Gate：只读运行，确认 `target_date`。
8. Backtest Gate：`--no-persist` 验证后，授权 `--persist` 写库。
9. API Readiness Gate：激活前验证 paused registry row、latest backtest，并确认 `/api/metrics/{registry_scheme_id}` 和 `/api/backtests/factor-lab` 不泄漏 paused 方案。
10. All Gate：`python -m harness onboard {scheme_id} --stage all`（末段为 `api-readiness`，不是 active-only `api`）。
11. Activate：签发 token，activate，并同步 registry/version。
12. Post-activation API Gate：验证 active `/api/metrics/{registry_scheme_id}` 和 `/api/backtests/factor-lab`；`registry_scheme_id = {base_scheme_id}__h{horizon}__{target_tenor}`，不得再使用 `?tenor=...`。
13. Live Backfill：按 `target_date` 回补实盘预测。
14. Documentation：更新 `docs/CURRENT_STATUS.md`。

没有 gate 证据，不得宣称方案达到 Onboarding Complete，更不得宣称已经 Production Observed。

## 8. T0 出口检查

开始写代码前，确认：

- [ ] 方案是新增 scheme，不是平台框架改造。
- [ ] 已选 daily / weekly 频率，并知道对应输入入口；如有 weekly/monthly 辅助输入，已声明 `input_spec.auxiliary_inputs`。
- [ ] `target_date` / `predict_date` 语义已写清。
- [ ] 已统一使用 `feature_date` 表示数据截止日；没有让前端/业务依赖 `anchor_date`。
- [ ] 如涉及实盘补齐，已规划 `prediction_phase=gray_live/scheduled_live` 标识，并证明灰度补齐只使用 `feature_date` 及以前数据。
- [ ] 需要历史回测时，已声明统一输出样本起点：daily/monthly 用 `backtest.start_date: "2025-01-01"`，weekly 用 `backtest.predict_start_date: "2025-01-01"`。
- [ ] 周度方案已明确 DB 周历、target week、`end_week=feature_week_id` 与 `as_of_date=feature_date` 规则。
- [ ] 如方案运行时间可能超过默认 executor 预算，已声明 `schedule.timeout_sec`，并确认这是 L3 执行预算而非 L2 算法改动。
- [ ] 原始算法文件和逐方案 original benchmark 来源已定位；仅有 `source_evidence/` 批次文件不算完成。
- [ ] Source-backed 方案已完成 source 口径分类，并确认不会修改原始算法逻辑。
- [ ] 已完成 L0/L1/L2 改动分级；若出现 L2，已停止原方案入库/修复，或已按用户明确批准另立新实验方案。
- [ ] 已逐项标出原始 runner 明确 patch 的日期字段，以及必须保留不动的固定算法锚点（筛因子起点、warmup、校准窗口、report mask 等）。
- [ ] 已确认历史回测的分组键和 source context：按 source 要求使用 target-date 月、feature 月、单日 PIT 或完整 batch；不得用平台 fast path 静默替换。
- [ ] 如源方提供 `latest_oos` / batch 结果，已确认它是 strict PIT 还是事后批量口径；若是批量口径，已规划 source evidence 与平台 canonical strict PIT / live-safe oracle 的差异记录，且不会把 raw source batch live 边界行当成 live 数值真值。
- [ ] 已规划内部模型分数 / baseline score / confidence 的对比证据；不能只看最终方向。
- [ ] 已选同频率参考方案和回测 runner。
- [ ] 已确认只会改允许范围内文件。
- [ ] 已计划 original-vs-onboarded 对比、回测落库、API 验证、激活、实盘回补和文档留痕。
