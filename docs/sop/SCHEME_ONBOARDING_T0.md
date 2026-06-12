# 新增方案 T0 强约束范式

**适用范围**：任何新增预测方案（daily / weekly；未来 monthly 也按同一范式扩展）。

> 这是新增方案前的 **T0 必读文档**。它只定义不可破坏的范式和 gate 顺序，不替代详细 SOP。执行细节继续看 [SCHEME_ONBOARDING_SOP.md](SCHEME_ONBOARDING_SOP.md)、[SCHEME_CONTRACT.md](../SCHEME_CONTRACT.md) 和 [PITFALLS_2026-06-10.md](PITFALLS_2026-06-10.md)。

## 0. 必读顺序

新增方案开工前，按顺序读：

1. 本文：确认新增方案的不可破坏边界。
2. [PITFALLS_2026-06-10.md](PITFALLS_2026-06-10.md)：重点看 predict vs target、周度日历、source-vs-onboarded 对比、实盘回补。
3. [SCHEME_CONTRACT.md](../SCHEME_CONTRACT.md)：确认 config / predict.py / core 的机器契约。
4. [SCHEME_ONBOARDING_SOP.md](SCHEME_ONBOARDING_SOP.md)：按 gate 执行完整入库。

## 1. 新增方案不改框架

普通新增方案只能新增或修改这些位置：

- `schemes/{scheme_id}/`
- `backtests/{scheme_id}_reproduction.py`
- `tests/test_{scheme_id}.py`
- `tests/test_{scheme_id}_backtest.py`
- `docs/CURRENT_STATUS.md`

除非用户明确要求做平台改造，否则禁止修改：

- `scheduler/`
- `backend/`
- `harness/`
- `shared/data_service.py`
- `shared/input_artifacts.py`
- `shared/calendar_service.py`
- `shared/models.py`
- `shared/versioning.py`
- `backtests/_base_runner.py`
- `backtests/repository.py`
- `frontend/`
- `migrations/`
- `deploy/`
- `pyproject.toml`

如果新增方案看起来必须改框架层，先停下来说明原因；不要顺手改。

## 2. 三条平台不变量

1. **输入单点**：方案 adapter 和 backtest runner 只能通过 `shared.input_artifacts` 取输入。
   - daily 用 `build_daily_input_artifact()`。
   - weekly 用 `build_weekly_input_artifact()`。
   - monthly 用 `build_monthly_input_artifact()`。
   - daily 方案如果依赖 weekly/monthly 辅助数据，必须在 `config.yaml` 的 `input_spec.auxiliary_inputs` 声明辅助频率、data_version 和 required_columns；adapter/backtest runner 仍只能通过 `shared.input_artifacts` 构造输入，禁止自拼 DB 输入或读取外部 CSV。
2. **写库单点**：实盘预测只通过 `scheduler.executor` / `scheduler.repository` 写库；回测只通过 `backtests.repository` 写库。
3. **core 纯净**：`schemes/{scheme_id}/core/` 不访问 DB、不写库、不跨方案 import、不调用调度器。

## 3. 日期语义不许混

- `target_date` 是展示、分组、去重、月度指标、唯一键的日期。
- `predict_date` 只用于调度日志和运行记录。
- 全平台历史回测输出样本统一从 `predict_date >= 2025-01-01` 开始；日频/月频方案在 `config.yaml` 写 `backtest.start_date: "2025-01-01"`，周频方案写 `backtest.predict_start_date: "2025-01-01"`。
- 历史训练、筛因子、模型更新、warmup 和输入 artifact 可以使用 `2025-01-01` 之前的数据；不要把训练起点误当成回测输出样本起点。
- 灰度实盘观察起点也按 `target_date` 判定。当前起点为 `target_date >= 2026-06-01`；历史回测必须只覆盖起点之前的 target，不得把 live target 月写进 `t_backtest_*`。
- `deployed_at` / 前端“部署时间”只是展示字段，不参与月份归属、回测截断、实盘回补范围或唯一键计算。
- `t_scheme_predictions` 唯一语义是 `(scheme_id, target_tenor, horizon, target_date)`，写入必须保持 UPSERT 语义。
- 新增方案不得依赖 serving pointer 来决定前端展示哪条预测。

## 3.1 confidence 术语

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
| 数据加载 | 覆盖特征窗口和 target 计算所需数据 | `end_week >= current_week_id + 6`；目标周数据不存在时不能回退 feature 周 |

周度方案尤其要验证：周六 `predict_date` 不是交易日时，只能向前找最近 DB 周作为 `feature_week_id`；`target_date` 必须是下一周最后交易日，不是本周。

## 5. 源文件对比是激活前强制项

如果方案来自原始脚本或原始输出文件，激活前必须完成：

1. 原始脚本 / 原始输出生成 source benchmark。
2. 入库后框架代码生成 current benchmark。
3. 逐样本对齐，`predicted_direction` 零容差。
4. 四份 benchmark 文件落在 `schemes/{scheme_id}/benchmarks/`。
5. `config.yaml` 设置 `backtest.benchmark_required: true`。
6. `harness onboard --stage all` 中 CompareGate 必须是 `passed`，不能是 `skipped`。

benchmark CSV 至少包含 `predict_date/tenor/direction/confidence`；周度方案还应保留 `feature_week_id/target_date` 等审计列。CompareGate 当前按 `predict_date + tenor` 对齐预测样本，月度指标和前端展示仍按 `target_date` 归属。

纯框架内实验方案如果没有原始基准，必须在 `docs/CURRENT_STATUS.md` 明确说明为什么 CompareGate 可以没有 source benchmark。

## 6. 实盘回补按 target_date

激活后必须补齐灰度观察起点以来应当存在的实盘预测。

- 回补范围按 `target_date >= 2026-06-01` 判断。
- `predict_date` 按调度日历反推，可以早于灰度起点。
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
9. API Gate：验证 `/api/metrics/{scheme_id}?tenor=...` 和 `/api/backtests/factor-lab`。
10. All Gate：`python -m harness onboard {scheme_id} --stage all`。
11. Activate：签发 token，activate，重启 scheduler。
12. Live Backfill：按 `target_date` 回补实盘预测。
13. Documentation：更新 `docs/CURRENT_STATUS.md`。

没有 gate 证据，不得宣称方案完成或 live ready。

## 8. T0 出口检查

开始写代码前，确认：

- [ ] 方案是新增 scheme，不是平台框架改造。
- [ ] 已选 daily / weekly 频率，并知道对应输入入口；如有 weekly/monthly 辅助输入，已声明 `input_spec.auxiliary_inputs`。
- [ ] `target_date` / `predict_date` 语义已写清。
- [ ] 需要历史回测时，已声明统一输出样本起点：daily/monthly 用 `backtest.start_date: "2025-01-01"`，weekly 用 `backtest.predict_start_date: "2025-01-01"`。
- [ ] 周度方案已明确 DB 周历、target week、`end_week` 规则。
- [ ] 原始算法文件和 source benchmark 来源已定位。
- [ ] 已选同频率参考方案和回测 runner。
- [ ] 已确认只会改允许范围内文件。
- [ ] 已计划 source-vs-onboarded 对比、回测落库、API 验证、激活、实盘回补和文档留痕。
