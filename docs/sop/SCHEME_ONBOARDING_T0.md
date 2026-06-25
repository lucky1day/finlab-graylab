# 新增方案 T0 强约束范式

**适用范围**：任何新增预测方案（daily / weekly；未来 monthly 也按同一范式扩展）。

> 这是新增方案前的 **T0 必读文档**。它只定义不可破坏的范式和 gate 顺序，不替代详细 SOP。执行细节继续看 [PREDICTION_SEMANTICS.md](../PREDICTION_SEMANTICS.md)、[SCHEME_ONBOARDING_SOP.md](SCHEME_ONBOARDING_SOP.md)、[SCHEME_CONTRACT.md](../SCHEME_CONTRACT.md) 和 [PITFALLS_2026-06-10.md](PITFALLS_2026-06-10.md)。

## 0. 必读顺序

新增方案开工前，按顺序读：

1. 本文：确认新增方案的不可破坏边界。
2. [PREDICTION_SEMANTICS.md](../PREDICTION_SEMANTICS.md)：确认 `predict_date` / `feature_date` / `target_date` / `prediction_phase` 的唯一语义。
3. [SOURCE_ALGORITHM_FIDELITY.md](../SOURCE_ALGORITHM_FIDELITY.md)：确认 source-backed 方案不得修改原始算法逻辑。
4. [PITFALLS_2026-06-10.md](PITFALLS_2026-06-10.md)：重点看 predict vs target、周度日历、source-vs-onboarded 对比、实盘回补。
5. [SCHEME_CONTRACT.md](../SCHEME_CONTRACT.md)：确认 config / predict.py / core 的机器契约。
6. [SCHEME_ONBOARDING_SOP.md](SCHEME_ONBOARDING_SOP.md)：按 gate 执行完整入库。

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

## 3. 日期语义不许混

- `predict_date` 是信号发出日 / 调度运行日。
- `feature_date` 是数据截止日 / 预测站位日，是前端和业务使用的唯一数据截止字段。
- `target_date` 是展示、分组、去重、actual join、月度指标、唯一键的日期。
- `anchor_date` 不得作为业务字段使用；如方案内部或审计 extra 保留，必须等于 `feature_date`。
- 回测必须满足 `predict_date = feature_date = T`，`target_date = T + horizon`。
- 灰度实盘和正式实盘都必须满足 `predict_date = T + 1`，`feature_date = T`，`target_date = T + horizon`。
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

| 项 | daily | weekly |
|----|-------|--------|
| `horizon` | `1` 或 `5` | 当前用 `6` |
| 输入入口 | `build_daily_input_artifact()`；如依赖 weekly/monthly，声明 `input_spec.auxiliary_inputs` 后再调用对应 artifact builder | `build_weekly_input_artifact()` |
| 日期来源 | 交易日历 / 源数据日期 | `week_id` 必须来自 `api_wind_date`，经 `shared.calendar_service` |
| target 规则 | 按 T+N 目标交易日 | `feature_week_id` 的下一实际 DB 周 `target_week_id`，目标日为该周最后交易日 |
| 回测样本起点 | `backtest.start_date: "2025-01-01"`，按 `predict_date` 过滤输出样本 | `backtest.predict_start_date: "2025-01-01"`，`start_week/end_week` 仍是输入/训练范围 |
| 禁止项 | 用 `predict_date` 做展示月 | 任何 `*_to_friday` / `*_to_monday` / 计算型 week_id 作为实盘或回测对齐依据 |
| 数据加载 | 覆盖特征窗口和 target 计算所需数据 | live/gray 必须 `end_week=feature_week_id`、`as_of_date=feature_date`；不得读取 feature 周之后的周频原始行 |

周度方案尤其要验证：周六 `predict_date` 不是交易日时，只能向前找最近 DB 周作为 `feature_week_id`；`target_week_id/target_date` 必须由 DB 日历从 `feature_week_id` 推导到下一实际周及其最后交易日，不允许用公式 `week_id + 1` 或未来周数据存在性决定 target。

## 5. 源文件对比是激活前强制项

如果方案来自原始脚本或原始输出文件，激活前必须完成：

1. 原始脚本 / 原始输出生成逐方案 original benchmark。
2. 入库后框架代码生成 current benchmark。
3. 逐样本对齐，原始算法 source T 对齐平台 `feature_date`，`predicted_direction` 零容差。
4. 四份 benchmark 文件落在 `schemes/{scheme_id}/benchmarks/`。
5. `config.yaml` 设置 `backtest.benchmark_required: true`。
6. `harness onboard --stage all` 中 CompareGate 必须是 `passed`，不能是 `skipped`。

这里的 original benchmark 指 `schemes/{scheme_id}/benchmarks/original_predictions_sample.csv` 等逐方案基准文件，不是 `source_evidence/benchmark_batches/{benchmark_id}/` 的批次级外部证据归档。benchmark CSV 至少包含 `feature_date(or source_t)/target_date/tenor(or target_tenor)/direction/confidence`；周度方案还必须保留 `feature_week_id` 等审计列。历史旧列名 `predict_date/date` 只能解释为原始算法 source T，也就是平台 `feature_date`，不得解释为实盘信号发出日。月度指标、前端展示、回测/live 分区仍按 `target_date` 归属；跨灰度边界的 benchmark 样本要按 `target_date` 分流到 `t_backtest_predictions` 或 `t_scheme_predictions` 核验。

Source `latest_oos` / batch 结果不自动等于平台 canonical benchmark。若 batch 是一次性事后窗口生成，它可能包含 later test window、selector/streak 状态或标签可见性，与 live-like strict PIT 不一致。平台 current/backtest/live 结果必须先声明 source 执行口径：`source_original_reproduction` 按原始 batch/window 完整复现，`source_strict_pit` 按原始 PIT 入口复现，`platform_live_pit_variant` 则必须获批并记录与 source-original 的差异。不得为了让 CompareGate 通过而复制 batch 输出，也不得手工补预测结果。

同时，source batch 与 strict PIT 的差异不能成为修改算法内部逻辑的理由。必须先按 [SOURCE_ALGORITHM_FIDELITY.md](../SOURCE_ALGORITHM_FIDELITY.md) 声明 source 执行口径：复现 source-original 就按原始 batch/window 完整复现；构造平台 live-like PIT 变体则必须获批并单独命名，且只能改变外层传入的可见数据截止/上下文，不能改特征、模型、投票或 fallback。

纯框架内实验方案如果没有原始基准，必须在 `docs/CURRENT_STATUS.md` 明确说明为什么 CompareGate 可以没有 original benchmark。

## 6. 实盘回补按 target_date

激活后必须补齐灰度观察起点以来应当存在的实盘预测。灰度补齐写入实盘预测表，但必须标识为 `gray_live`，不能和正式 scheduler 自然发出的 `scheduled_live` 混淆。

- 回补范围按 `target_date >= 2026-06-01` 判断。
- `predict_date` 按调度日历反推，可以早于灰度起点。
- 每条补齐记录必须显式满足 `feature_date = T`，输入 artifact、辅助周/月映射和模型训练窗口都不能越过 `feature_date`；禁止因为当前 DB 已有 `T+1` 或更晚数据而读入未来信息。
- 周度例子：`target_date=2026-06-05` 对应 `predict_date=2026-05-30`；下一条 `target_date=2026-06-12` 对应 `predict_date=2026-06-06`。
- 验收时同时查 `t_scheme_predictions`、`t_scheme_run_log` 和 API 返回。

不要只按 `predict_date >= 2026-06-01` 回补；这会漏掉周度 6 月第一条目标周。

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
- [ ] 原始算法文件和逐方案 original benchmark 来源已定位；仅有 `source_evidence/` 批次文件不算完成。
- [ ] Source-backed 方案已完成 source 口径分类，并确认不会修改原始算法逻辑。
- [ ] 已逐项标出原始 runner 明确 patch 的日期字段，以及必须保留不动的固定算法锚点（筛因子起点、warmup、校准窗口、report mask 等）。
- [ ] 如源方提供 `latest_oos` / batch 结果，已确认它是 strict PIT 还是事后批量口径；若是批量口径，已规划 source evidence 与平台 canonical strict PIT 的差异记录。
- [ ] 已规划内部模型分数 / baseline score / confidence 的对比证据；不能只看最终方向。
- [ ] 已选同频率参考方案和回测 runner。
- [ ] 已确认只会改允许范围内文件。
- [ ] 已计划 original-vs-onboarded 对比、回测落库、API 验证、激活、实盘回补和文档留痕。
