# 方案与运行事故归档

> 本文件保存仍有审计价值的方案入库、benchmark、gray/live 补齐、性能优化和生产运行事故。它不是日常 SOP 入口；主文档只保留可执行索引。

## 分组索引


### source-backed PIT 与 benchmark 口径

| 编号 | 标题 | 阅读目的 |
|------|------|----------|
| 坑 0b | V28 这类 test window 敏感算法不能把窗口当普通参数 | 追溯方案入库/运行事故证据，抽象规则已在主索引提示。 |
| 坑 11 | 方案激活时跳过了"源文件原始回测 vs 入库后回测"对比 | 追溯方案入库/运行事故证据，抽象规则已在主索引提示。 |
| 坑 14 | CompareGate 的 confidence 差异术语不清，容易被误解成平台新指标 | 追溯方案入库/运行事故证据，抽象规则已在主索引提示。 |
| 坑 25 | `liwei_0616 5Y_01` 源复现报告按 source T 归月，不能直接和前端 target 月数字比较 | 追溯方案入库/运行事故证据，抽象规则已在主索引提示。 |
| 坑 27 | `liwei_0616 7Y_01` 不能用月末窗口或短历史输入伪装 PIT | 追溯方案入库/运行事故证据，抽象规则已在主索引提示。 |
| 坑 29 | monthly fast path 只能作为已验证的 PIT 等价优化 | 追溯方案入库/运行事故证据，抽象规则已在主索引提示。 |
| 坑 30 | source `latest_oos` batch 不是平台 strict PIT 真值 | 追溯方案入库/运行事故证据，抽象规则已在主索引提示。 |
| 坑 33 | targeted sample 跨灰度边界时不能沿用 historical input_end 和 live cutoff | 追溯方案入库/运行事故证据，抽象规则已在主索引提示。 |

### gray/live 补齐与前端样本完整性

| 编号 | 标题 | 阅读目的 |
|------|------|----------|
| 坑 12 | 方案激活后未回补实盘预测，前端缺失实盘观察序列 | 追溯方案入库/运行事故证据，抽象规则已在主索引提示。 |
| 坑 26 | `liwei_0616 5Y_01` 灰度补齐不能按 6 月 predict_date 起步 | 追溯方案入库/运行事故证据，抽象规则已在主索引提示。 |
| 坑 32 | source benchmark 边界验证不能替代完整 gray_live 网格回补 | 追溯方案入库/运行事故证据，抽象规则已在主索引提示。 |

### 周频算法与运行事故

| 编号 | 标题 | 阅读目的 |
|------|------|----------|
| 坑 15 | 周频原始 batch 回测被误改成逐周 PIT，导致样本覆盖与 benchmark 不一致 | 追溯方案入库/运行事故证据，抽象规则已在主索引提示。 |
| 坑 34 | 周度实盘需要提前补齐目标周 `api_wind_date` | 追溯方案入库/运行事故证据，抽象规则已在主索引提示。 |

### 性能与并发实现事故

| 编号 | 标题 | 阅读目的 |
|------|------|----------|
| 坑 28 | 7Y V31 严格 PIT 分片不能用线程并发 | 追溯方案入库/运行事故证据，抽象规则已在主索引提示。 |

---

## 归档全文

## 坑 0b：V28 这类 test window 敏感算法不能把窗口当普通参数

**严重等级**：Critical。同一个 `feature_date`，只要 core 的 test window 不同，输出就可能改变；这会造成 benchmark 通过一套口径、实盘写入另一套口径。

**现象**（实际发生）：`daily_5y_2_v28` 的逐方案 original May 2026 benchmark 中，`feature_date=2026-05-28` 的方向为 `1`。平台灰度实盘旧 `run_id=42` 却写出了不同方向，导致 original benchmark 与 `t_scheme_predictions.feature_date` 对齐核验失败。

**根因**：源算法按“月度 test window”运行。V28 core 的 Phase C 会基于 `test_months` 做 monthly ensemble / signal selection，所以 `2026-05-01..2026-05-28` 与连续窗口 `2024-07-01..2026-05-28` 不是同一个算法输入。旧平台 wrapper 把窗口当成普通回测范围，导致 `predict.py` 与回测/benchmark 口径不一致。

**修复**：
1. 新增 `schemes.daily_5y_2_v28.inference` 作为唯一核心预测入口。
2. `v28_feature_month_window(feature_date)` 固定返回 `feature_date` 所在月第一天到 `feature_date`，不得越过 `feature_date`。
3. `predict.py`、gray/live 补齐、benchmark current 生成和 backtest runner 均调用同一个 inference helper。
4. V28 benchmark current 由 `scripts/rebuild_v28_scheme_benchmark.py` 调平台 helper 生成，不复制 source CSV。
5. 旧错误 `run_id=42` 的 V28 灰度预测明细已删除，保留 run/log 审计；新 `run_id=58` 写入 `predict_date=2026-05-29`、`feature_date=2026-05-28`、`target_date=2026-06-04`、`predicted_direction=1`、`confidence=1.0`。

**检查清单**：
- [ ] 方案是否存在 test-window-sensitive 的 selector、ensemble、rolling top-K、分月校准或信号组合逻辑？
- [ ] adapter 与 backtest runner 是否调用同一个 inference helper？
- [ ] live/gray 的核心窗口是否以 `feature_date` 截止，而不是当前 DB 最新日？
- [ ] 逐方案 original benchmark 的 `date/T` 是否对齐 `feature_date`，而不是实盘 `predict_date`？
- [ ] benchmark current 侧是否由平台 helper 生成，且 strict key 逐行一致？

## 坑 11：方案激活时跳过了"源文件原始回测 vs 入库后回测"对比

**严重等级**：Critical。跳过该对比等于上线了未经行为验证的方案——平台改造可能悄悄改变了算法行为而无人发现。

**现象**（实际发生）：`weekly_7y_cross_d_overlay_0529` 首次入库时，因为 `schemes/{id}/benchmarks/` 目录没有 benchmark 文件，CompareGate 返回 `skipped`。由于 ActivationGate 把 `skipped` 视同通过（坑 3 的修复），方案被直接激活上线。"源文件原始脚本输出 vs 入库后框架输出"的逐样本对比完全没有发生，事后才补做。

**根因链条**：
1. `generate_benchmark_samples.py` 只支持日频方案，周频方案的 benchmark 文件需要手工准备——容易被遗漏。
2. CompareGate 缺文件时 `skipped` 不阻断 `onboard --stage all`。
3. ActivationGate 接受 `skipped`（坑 3 修复的副作用）——本意是放过"无基准的纯实验方案"，但也放过了"有基准却没准备文件"的方案。
4. SOP 的 Step 10 激活条件没有显式列出"源 vs 入库对比必须完成"。

**修复**：
1. SOP 新增 **Step 10a（强制）**：激活前必须完成 POST_ONBOARDING_TEST_SOP 的 S3–S5（源脚本基准 → 框架复现 → 方向零容差对比），benchmark 文件落位，CompareGate 必须 `passed` 而非 `skipped`。
2. 新方案 `config.yaml` 必须设 `backtest.benchmark_required: true`，使 CompareGate 缺文件时 FAIL 而非 SKIP。
3. 对比证据（matched 数、direction diff）必须写入 `CURRENT_STATUS.md`。

**事后补做的对比结果**（weekly_7y_cross_d_overlay_0529）：
- 初始源脚本窗口 43 条样本 vs DB run_id=89：matched 43/43，direction/label/confidence/target_date 全部零差异。
- 2026-06-13 周频历史回测统一排除灰度实盘 target 后，历史 benchmark 覆盖窗口保留 `target_date < 2026-06-01` 的 42 条；latest run_id=110 的 `original_benchmark_validation` 为 42/42 matched。
- 补齐四份 benchmark 文件 + `benchmark_required: true` 后重跑 onboard，CompareGate `passed`（direction_match_rate=1.0）。

**检查清单**（每个新方案激活前逐项核对）：
- [ ] POST_ONBOARDING_TEST_SOP S3：源文件原始脚本（或静态基准）已跑出基准序列
- [ ] S4：入库后框架代码已跑出复现序列（同一数据接入层）
- [ ] S5：逐样本对比，方向零容差，全部 matched
- [ ] `schemes/{id}/benchmarks/` 四份文件齐全
- [ ] `config.yaml` 含 `backtest.benchmark_required: true`
- [ ] `harness onboard --stage all` 中 CompareGate 状态为 `passed`（不是 `skipped`）
- [ ] 对比证据已写入 `CURRENT_STATUS.md`

---

## 坑 12：方案激活后未回补实盘预测，前端缺失实盘观察序列

**现象**（实际发生）：`weekly_7y_cross_d_overlay_0529` 激活后，前端没有"实盘发出起点"分隔线，缺少 6月6日预测的 06/12"待验证"行。第一次补救时只补了 `predict_date=2026-06-06 -> target_date=2026-06-12`，前端 2026-06 周度表仍缺 `target_date=2026-06-05`；实际还必须补 `predict_date=2026-05-30 -> feature_date=<上一完整周> -> target_date=2026-06-05`。原因是激活时只完成了回测落库，没有补跑灰度起点（2026-06-01）以来应当存在的灰度实盘预测。

**根因**：方案在灰度起点之后才入库，错过了此前的调度窗口。回补时如果按 `predict_date >= 2026-06-01` 枚举，会漏掉周度 6 月第一条目标周，因为 `target_date=2026-06-05` 的预测发出日是上一轮周六 `predict_date=2026-05-30`。激活只让"未来"的调度生效，不会自动补"过去"缺失的预测。

**修复**：SOP 新增 **Step 10b（强制）**：激活后必须按调度日历回补 target_date 从灰度起点至今的全部灰度实盘预测，先枚举应有 `target_date`，再反推对应 `predict_date` 和 `feature_date`。`predict_date` 可以早于灰度起点；禁止只按 `predict_date` 起点过滤。补齐记录必须标识 `prediction_phase=gray_live`。

**前端联动逻辑**（无需改前端代码，数据补齐后自动生效）：
- "实盘发出起点"分隔线：日频和周度统一取该方案 live rows 的最小 `predict_date`；灰度区间和正式调度起点由 `/api/metrics/{scheme_id}` 的 `phase_ranges` 展示。
- 尚无 actuals 的 target 自动显示"待验证"。

**检查清单**（激活后逐项核对）：
- [ ] DB 中实盘预测 target_date 连续覆盖 2026-06-01 至今
- [ ] 周频方案 6 月第一条 target（例如 2026-06-05）已存在，即使其 predict_date 是 2026-05-30
- [ ] 补齐记录有 `feature_date`，且输入只使用 `feature_date` 及以前数据
- [ ] 补齐记录可判定为 `prediction_phase=gray_live`
- [ ] 前端有"实盘发出起点"分隔线
- [ ] 未回填的 target 显示"待验证"
- [ ] t_scheme_run_log 有回补的运行记录

---

## 坑 14：CompareGate 的 confidence 差异术语不清，容易被误解成平台新指标

**现象**（实际发生）：`weekly_10y_d_overlay_0529` CompareGate 通过后，报告里出现 `max_confidence_abs_diff=1.1102230246251565e-16`。用户追问“最大 confidence 差异是什么？原本的文件就有 confidence？”说明文档没有讲清 `confidence` 的来源和比较含义。

**根因**：
1. `PredictionRecord.confidence` 是平台统一字段，但不同原始算法可能叫 `confidence`、`probability`、`score` 或 `prob_up`。
2. CompareGate 只比较 original/current benchmark CSV 中同名 `confidence` 字段的数值差，并不判断这个值的金融含义。
3. 浮点计算从原始脚本迁移到框架后，可能出现 `1e-16` 量级的二进制舍入差异；这不是算法行为差异。
4. SOP 只写了“confidence diff”，没有说明该字段是原始算法输出的归一化承接字段，也没有说明缺失时如何处理。

**修复**：
1. T0 / SOP / SCHEME_CONTRACT 明确：`confidence` 承接原始算法已有置信度、概率或分数；不是平台新增标签。
2. 如果原始算法没有天然 `confidence`，original/current benchmark 必须使用同一确定性代理值，并在 `CURRENT_STATUS.md` 写清映射。
3. CompareGate 的硬门槛是 `predicted_direction` 零容差；`max_confidence_abs_diff` 只做辅助一致性检查，`1e-16` 量级按 0 看待。
4. benchmark CSV 的必需字段改为 `feature_date(or source_t)/target_date/tenor(or target_tenor)/direction/confidence`；历史旧列名 `predict_date/date` 只能解释为 source T / 平台 `feature_date`。周度方案额外保留 `feature_week_id` 方便审计，月份归属仍按 `target_date`。

**检查清单**：
- [ ] `CURRENT_STATUS.md` 写明逐方案 original benchmark 的 `confidence` 来源或代理规则。
- [ ] original/current benchmark 两侧使用同一 `confidence` 映射。
- [ ] 对外解释 CompareGate 时同时报告 direction match 和 confidence diff，且说明浮点容差。
- [ ] 不把 `confidence` 差异当成月度准确率或前端展示月份的依据。

---

## 坑 15：周频原始 batch 回测被误改成逐周 PIT，导致样本覆盖与 benchmark 不一致

**现象**（实际发生）：`weekly_10y_d_overlay_0529` latest backtest run 只从 2025-07 之后开始，前端“10Y国债活跃 · 周度”历史月度结果缺少 2025 年上半年。随后复查 `weekly_5y_direct_0529` / `weekly_7y_cross_d_overlay_0529`，也发现严格 PIT 重建后的 latest run 与原始 benchmark 样本覆盖不一致，导致同为周度、同为 2025-01-01 输出起点、同为 2026-06-01 灰度起点的方案，总样本数仍不一致。直接检查输入 weekly artifact 时，2025H1 周数据存在；问题不在前端隐藏，也不是 DB 缺数据。

**根因**：这三个 2025-05-29 来源批次周频方案的源文件历史回测是全历史 batch reproduction。把它们改造成逐周 point-in-time 切片，会改变原始 benchmark 的评价对象。10Y 的冲突最明显：活跃 core 中 Model2 固定分段包含 `2023_2024`、`2025H1`、`2025H2_2026`；逐周 PIT 切片在 2025H1 运行时，未来半年度段为空，`build_model2_predictions()` 会因为空 train/test segment 抛错，导致 runner 跳过这些 feature week。5Y/7Y 的缺口没有 10Y 大，但严格切片同样会改变原始方案的样本覆盖和候选排行比较口径。

**修复**：
1. `weekly_5y_direct_0529`、`weekly_7y_cross_d_overlay_0529`、`weekly_10y_d_overlay_0529` 历史回测作为批准例外，使用 source-original batch reproduction：全历史 weekly artifact 只传给 core 一次，再按 `feature_week_id` 映射平台 row。
2. 回测输出仍遵守平台日期字段：`predict_date=feature_date`，`target_date` 由 DB 日历取下一实际周。
3. 回测仍按灰度实盘边界过滤：`target_date < 2026-06-01`。
4. 落库前必须和已有 `original_predictions_sample.csv` 覆盖区间逐行一致；方向、target_date、label/is_correct 零差异，confidence 只允许 `1e-12` 以内浮点误差。
5. 实盘/灰度 adapter 不享受该例外，仍必须用 `end_week=feature_week_id`、`as_of_date=feature_date` 的严格 T+1/T 语义。

**2026-06-14 补充：重建为 source-original batch 后，样本数仍可能不同。** 三个周频方案的窗口和截断边界已经一致，但平台写入的是 core 实际发出的有效信号，不是所有日历周占位。当前 latest：5Y run_id=`109` 共 71 条，缺 `202534`；7Y run_id=`110` 共 68 条，缺 `202529/202534/202547/202608`；10Y run_id=`108` 共 72 条，无缺周。5Y 的 `202534` 是 `five_year_1y_momentum_4w` 原始信号为 0，`np.sign(0)=0` 后被规则投票排除；7Y 的缺周来自 7Y 主规则信号为 0 或 5Y 辅助信号缺失后被 `inner join` 排除。结论：这是算法输出本身，不是前端、DB、latest view 或回测窗口错误。除非业务明确批准改变算法语义，否则不得补写 flat/no-prediction 行来凑齐样本数。

**检查清单**：
- [ ] latest 5Y/7Y/10Y backtest summary 标记 `backtest_mode=original_batch_reproduction`。
- [ ] latest 5Y/7Y/10Y backtest summary 标记 `backtest_point_in_time=false`。
- [ ] benchmark 覆盖区间逐行一致校验通过后才允许 persist。
- [ ] `target_date >= 2026-06-01` 的周频样本只来自 `t_scheme_predictions` live 表，不来自 `t_backtest_*`。
- [ ] 对比候选方案样本数时，先区分“日历周数”和“算法有效预测行数”；有效预测行数可以不同，缺周必须记录 `feature_week_id` 和 core 过滤原因。

---

## 坑 25：`liwei_0616 5Y_01` 源复现报告按 source T 归月，不能直接和前端 target 月数字比较

**严重等级**：High。这个问题不会改变逐样本预测结果，但会让“原始回测结果是否和平台对齐”的判断被月份归属口径误导。

**现象**（实际发生）：入库 `liwei_0616_cons_sda_k3_div_k10` 时，用户提供的源复现报告 `/Users/macstudio0/Desktop/models-liwei-0616/bond_predict_merged/bond_predict_merged_targeted_reproduction_report_20260616.md` 已包含 `5Y_01` 月度结果。直接拿报告月度数字和前端月度数字比较会发现数字不一样，容易误判为平台回测不一致。

**根因**：
1. 源报告的 `5Y_01` 月度归属来自 source `date` / `predict day`，进入平台后应解释为 `feature_date`。源 runner 里按 `pd.to_datetime(tmp["date"]).dt.to_period("M")` 分月。
2. 平台前端、后端 `/api/backtests/factor-lab` 和 `/api/metrics/{scheme_id}` 统一按 `target_date` 归月。
3. `5Y_01` 是 T+5 日频方案，5 月末 source T 会预测 6 月初 target；因此 feature 月和 target 月天然会错开，月度样本数和正确数不能直接相等。

**核验结果**：
1. 用 `schemes/liwei_0616_cons_sda_k3_div_k10/benchmarks/original_predictions_sample.csv` 按 `feature_date` 分组，能复现源报告数字：
   - `2026-05`: samples=`18`，traded=`16`，correct=`9/16`，accuracy=`56.2%`
   - `2026-06`: samples=`3`，traded=`3`，correct=`3/3`，accuracy=`100%`
2. 同一份 21 行样本按平台 `target_date` 分组时，月度归属会变成目标月口径；这才是前端应展示和核验的口径。
3. 最终平台对齐不是比较源报告月度行，而是逐样本分流：
   - `target_date < 2026-06-01` 的 13 行对齐 `/api/backtests/factor-lab` latest run_id=`120`。
   - `target_date >= 2026-06-01` 的 8 行对齐 `/api/metrics/liwei_0616_cons_sda_k3_div_k10__h5__5Y` 灰度实盘 daily rows。
   - 21 行的 `direction/confidence/label/is_correct` 均为零差异。

**修复规则**：
1. 外部复现报告给出月度数字时，先确认其月份字段是 source T、feature_date、predict_date 还是 target_date。
2. 若报告按 source T / `feature_date` 归月，只能与 `original_predictions_sample.csv` 按 `feature_date` 重算结果比较；不得直接和前端月度表比较。
3. 前端/API 验收必须以逐样本 strict key 为准：`feature_date + target_date + target_tenor + horizon`。跨灰度边界时，按 `target_date` 分流到 backtest 或 live metrics。
4. `CURRENT_STATUS.md` 或方案验收记录中必须同时写清 source report 月份口径与平台 target 月份口径，避免后续复核时再次混淆。

**检查清单**：
- [ ] 源报告的月份归属字段已明确记录。
- [ ] `original_predictions_sample.csv` 按源报告同一字段重算后，与源报告月度数字一致。
- [ ] 前端/API 验收按 `target_date` 归月，不用 source report feature 月数字替代。
- [ ] 跨灰度边界样本已拆成 backtest/live 两段逐行对齐，且记录 matched 行数与 diff 结果。

---

## 坑 26：`liwei_0616 5Y_01` 灰度补齐不能按 6 月 predict_date 起步

**严重等级**：High。日频 T+5 方案如果只按 `predict_date >= 2026-06-01` 补灰度，会漏掉 `target_date=2026-06-01..2026-06-04` 这类 feature 在 5 月、target 在 6 月的样本。

**现象**（实际发生）：`liwei_0616_cons_sda_k3_div_k10` 激活后，历史回测 latest run_id=`120` 正确截断到 `target_date < 2026-06-01`，但 `original_predictions_sample.csv` 中仍有 8 行 target 落在 2026-06-01 到 2026-06-10。只有补齐 gray_live 后，这些样本才会出现在 `/api/metrics/{scheme_id}`，并完成最终前端/API 对齐。

**根因**：
1. 灰度观察起点按 `target_date` 判定，不按 `predict_date` 或部署日判定。
2. 对 T+5 日频方案，`target_date=2026-06-01` 对应 `feature_date=2026-05-25`、`predict_date=2026-05-26`；若从 6 月 predict_date 开始补，会漏掉 6 月初多个 target。
3. 激活和 scheduler 挂载只负责未来调度，不会自动补已经错过的灰度观察窗口。

**本次补齐结果**：
- 使用 LiveGate + `live_write` token，按时间顺序补齐 18 条 `gray_live`。
- `predict_date=2026-05-26..2026-06-18`
- `feature_date=2026-05-25..2026-06-17`
- `target_date=2026-06-01..2026-06-25`
- `t_scheme_runs` run_id=`92..109`，均为 success。
- `/api/metrics/liwei_0616_cons_sda_k3_div_k10__h5__5Y` 返回 `phase_ranges`，灰度区间 rows=`18`。
- scheduler 已挂载 `3 7 * * 1-5`，但截至该阶段尚未观察到自然产生的 `scheduled_live` 行；这是 **Onboarding Complete**，不是 **Production Observed**。

**修复规则**：
1. 日频 T+N 灰度补齐先枚举 `target_date >= gray_start` 的目标交易日，再用交易日历反推 `feature_date = target_date - N 个交易日` 和 `predict_date = next_trading_day(feature_date)`。
2. 每条补齐都必须走 LiveGate 或同等授权边界，显式写 `prediction_phase=gray_live`，不得手写 SQL。
3. 补齐后必须从 `/api/metrics/{registry_scheme_id}` 验证 live rows、`phase_ranges`、actual join 和待验证行。
4. scheduler 挂载验收只证明未来调度已注册；首条 `scheduled_live` 必须等真实调度自然触发后单独记录。

**检查清单**：
- [ ] 灰度补齐范围按 `target_date` 枚举，未漏掉 feature 在灰度起点前、target 在灰度起点后的样本。
- [ ] 每条 gray_live 记录满足 `predict_date=T+1`、`feature_date=T`、`target_date=T+horizon`。
- [ ] DB 中 `t_scheme_predictions/t_scheme_runs/t_scheme_run_log` 均有对应 success 记录。
- [ ] `/api/metrics/{registry_scheme_id}` 的 `phase_ranges` 能区分 `gray_live` 与后续 `scheduled_live`。
- [ ] 文档明确当前处于 `Onboarding Complete` 还是 `Production Observed`。

---

## 坑 27：`liwei_0616 7Y_01` 不能用月末窗口或短历史输入伪装 PIT

**严重等级**：Critical。这个问题会让 CompareGate 文件通过，但最终 `/api/backtests/factor-lab` 或 `/api/metrics` 与 original benchmark 不一致。

**现象**（实际发生）：入库 `liwei_0616_7y01_cons_say_k3_div_k10` 时，`original_predictions_sample.csv` 和 `current_predictions_sample.csv` 21 行完全一致，但激活并写入 gray_live 后，最终 API 对齐发现差异：

1. live 首行 `feature_date=2026-05-25,target_date=2026-06-01` 曾写成 `0`，original 为 `-1`。
2. latest backtest run_id=`121` 中 `feature_date=2026-05-18,target_date=2026-05-25` 曾写成 `-1`，original 为 `0`。

**根因**：
1. live adapter 初版只回看 `feature_date - 8*365`，而 backtest/benchmark 使用源历史起点 `2010-07-27`。V31 对训练历史长度敏感，导致 live 与 benchmark 漂移。
2. backtest runner 初版按“当月最后 feature_date”批量跑整月窗口，早期日期等价于看到了同月未来窗口状态；V31 的 seasonal VT / Phase C 选择对 test window 敏感，不能用月末窗口替代逐 `feature_date` PIT。
3. `DIV` 不在投票基线 `STD/ACCWT/CROSS_5Y` 中，但作为 streak-break fallback 仍必须计算；只按投票基线计算会在长 streak 后输出错误。
4. 源 `MONTHLY_COLS` 中 `M0041340/M0041341/M0041342` 在平台 monthly artifact 中不存在，且源代码对缺失月频列采用 optional 处理；config 的 required input 不能声明平台无法提供的 optional source 列。
5. LiveGate 目前要求 `t_scheme_predictions` 行数增加。对已存在灰度行做 UPSERT 纠偏时，底层 `execute_scheme` 会 success 并覆盖 prediction row，但 gate 会因 delta=0 fail；这暴露了 correction 场景的 harness 缺口。

**修复/处置**：
1. `predict.py` 已把 live 输入起点改为 `2010-07-27`，与 backtest/benchmark 一致；修正后只读 `scheme_runner` 对 `predict_date=2026-05-26` 输出 `predicted_direction=-1`，与 benchmark 一致。
2. `backtests/liwei_0616_7y01_cons_say_k3_div_k10_reproduction.py` 已新增逐 `feature_date` PIT 调用逻辑和单元测试，禁止月末窗口替代当天窗口。
3. 已重跑 gray_live 覆盖 18 条 live 行，最新 `t_scheme_predictions` 使用 scheme_version=`b071ae4b1bc0`，run_id=`128..145`，`/api/metrics` live 段与 original benchmark 的 8 条灰度样本已对齐。
4. persisted historical backtest 仍需通过可接受耗时的严格 PIT runner 重建 latest run；在该 run 完成前，不得宣称 7Y_01 的 source-backed 全流程已经 Onboarding Complete。

**检查清单**：
- [ ] live adapter、backtest runner、current benchmark 使用同一个历史输入起点和同一组 daily/weekly/monthly artifact as-of 规则。
- [ ] `current_predictions_sample.csv` 由平台 PIT helper 生成，不复制 source `latest_oos`。
- [ ] final API 对齐必须拆分 13 条 backtest + 8 条 live，并逐行比较 `direction/confidence/label/is_correct`。
- [ ] 对 test-window-sensitive V31 方案，历史回测不能用月末窗口给早期 feature_date 产出结果；若直接逐日 PIT 耗时不可接受，必须先做通用缓存/因果组合能力改造，再落库 latest backtest。
- [ ] 已存在 live 行需要纠偏时，先设计受控 correction/delete-and-rewrite 流程；不要把 LiveGate UPSERT failed 当作通过证据。

---

## 坑 28：7Y V31 严格 PIT 分片不能用线程并发

**严重等级**：High。线程级分片会让 Numba workqueue 在同一 Python 进程内被并发访问，可能直接 `Abort trap: 6`，不是可重试的普通模型失败。

**现象**（实际发生）：入库 `liwei_0616_7y03_cons_all_k3_div_k8` 时，3 个 targeted sample 先用串行严格 PIT 跑通；随后把外层 shard 实现成 `ThreadPoolExecutor`，执行：

```bash
PYTHONNOUSERSITE=1 conda run -n forecast_env \
  python -m backtests.liwei_0616_7y03_cons_all_k3_div_k8_reproduction \
  --no-persist \
  --sample-dates 2026-05-06,2026-05-18,2026-05-22 \
  --parallel-shards 2 \
  --disable-cache
```

进程在两个 shard 同时进入 V31 特征/训练阶段后 abort，错误为 `Numba workqueue threading layer is terminating: Concurrent access has been detected`。

**根因**：
1. V31 特征或训练路径里会触发 Numba parallel/workqueue。
2. Numba workqueue threading layer 不是线程安全的，不能从多个 Python thread 同时进入。
3. 外层线程分片和内层模型并行叠加后，在同一进程内形成并发 workqueue 访问。

**修复/处置**：
1. 外层 `parallel_shards > 1` 默认必须使用进程级隔离，例如 `ProcessPoolExecutor` 或独立 subprocess shard；测试/mock 场景才允许显式 `parallel_backend="inline"`。
2. shard worker 只计算 rows，不写 DB；父进程负责合并、排序、去重和最终 persist。
3. `outer_shards * inner_n_workers` 必须受控。实测 7Y_03 三个 targeted dates：
   - 串行严格 PIT：`417.185s`
   - 进程级 `--parallel-shards 2`：`272.723s`
   - 同一单日 cache 首跑：`157.337s`
   - 同一单日 cache 命中：`17.15s`
4. cache key 必须包含 scheme/source/model/config/code hash、feature_date、input artifact hashes、输入窗口和周频 as-of 信息；不能只按日期缓存。

**检查清单**：
- [ ] 严格 PIT 分片默认 backend 是进程或独立 subprocess，不是线程。
- [ ] 单元测试覆盖默认 `parallel_shards > 1` 不走线程 backend。
- [ ] 真实 targeted sample 已验证 serial 与 shard 的 rows 完全一致。
- [ ] 真实 cache 首跑和命中均已验证，且命中不再打印模型训练日志。
- [ ] full backtest 落库前确认 sample mode 不能 persist，full output 全量 materialize 后才可授权 persist。

---

## 坑 29：monthly fast path 只能作为已验证的 PIT 等价优化

**严重等级**：High。未经验证把同月多日合并为一次窗口，容易和“月末窗口替代逐 feature_date PIT”混淆，最终导致前端/API 与 strict benchmark 不一致。

**现象**（7Y_03 入库中发现）：7Y V31 严格逐日 PIT 很慢，直觉上可以把同一个自然月的多天合并成一次窗口运行，再从窗口结果中抽取每个 `feature_date`。这能显著减少 LGBM 训练次数，但不能直接视为 correctness reference。

**风险**：
1. V31 core 内部包含 test window、ensemble score、seasonal VT 和 streak fallback 等状态逻辑。
2. 只有当这些状态对同月早期日期不受后续同月样本影响时，monthly batch 才是性能优化；否则就是未来信息泄漏。
3. mock 级单元测试只能证明 runner 抽取和排序正确，不能证明真实 V31 core 因果等价。

**规则**：
1. `batch_mode=daily` 是 correctness reference；BacktestGate 默认不得因为性能压力改成 monthly。
2. `batch_mode=monthly` 只能显式启用，并且必须先用 `--disable-cache` 对代表性日期做真实 core diff：逐日 daily vs monthly 的 `direction/label/confidence/is_correct` 必须完全一致。
3. 验证日期至少覆盖 source latest OOS 关键日、每月首/中/末、灰度边界前后；若要用于正式 full no-persist/persist，还必须保留可审计的 diff=0 证据。
4. monthly fast path 的证据要记录在 runner summary 或状态文档中；不能把 source latest_oos batch 或月末窗口当成平台 PIT 真值。

**7Y_03 当前证据**：`2026-05-06,2026-05-18,2026-05-22` 在 `--batch-mode daily --disable-cache` 和 `--batch-mode monthly --disable-cache` 下三行一致；monthly 样本耗时约 `167.6s`，daily 三窗口耗时约 `417.2s`。这只是 targeted evidence，不足以把 monthly 设为默认 gate。

**10Y_01 当前证据**：`liwei_0616_10y01_cons_say_k3_div_k10` 在 `2026-05-06,2026-05-13,2026-05-19,2026-05-20,2026-05-21,2026-05-22,2026-05-25,2026-05-29,2026-06-03` 上完成真实 core daily strict 与 monthly fast path 逐行 diff，`direction/label/confidence/is_correct` 全部一致、`diff_count=0`；因此该方案的 BacktestGate 明确使用 `runner_args=["--batch-mode","monthly"]` 加速，但仍保留 daily strict 作为 correctness reference 和 sample 验证入口。

**10Y_02 当前证据**：`liwei_0616_10y02_cons_say_k3_div_k5` 在 `2026-05-06,2026-05-13,2026-05-19,2026-05-20,2026-05-21,2026-05-22,2026-05-25,2026-05-26,2026-05-27,2026-05-28,2026-05-29,2026-06-01,2026-06-02,2026-06-03` 上完成真实 core daily strict 与 monthly fast path 逐行 diff，`direction/label/confidence` 全部一致、`diff_count=0`；14 日期 daily strict 耗时 `1978.344s`，monthly fast path 耗时 `178.541s`，两者均使用 `backtest_input_end=2026-06-10` 以覆盖灰度边界 label。因此该方案的 BacktestGate 明确使用 `runner_args=["--batch-mode","monthly"]` 加速，但仍保留 daily strict 作为 correctness reference 和 sample 验证入口。

**7Y_03 另一个边界修正**：historical input artifact 可以保留到 `2026-05-29` 用于计算 `2026-05-22` 的 T+5 label，但 full historical feature list 必须截止到 `2026-05-22`。`2026-05-25 + 5 trading days = 2026-06-01`，属于 gray/live 边界，不得进入 historical full runner；否则 require-labels 模式会在月批量尾部缺失 `2026-05-25..2026-05-29`，并且语义上也越过 `target_date < 2026-06-01` 的历史截断。

**检查清单**：
- [ ] default `batch_mode` 保持 `daily`。
- [ ] monthly fast path 必须显式传参。
- [ ] 真实 core daily/monthly diff 证据已落文档或报告。
- [ ] historical feature cutoff 按 `target_date < gray_start` 反推，不能直接等于 input artifact end。
- [ ] full output 全量 materialize 并通过 strict key 去重、target cutoff、summary 明细一致性校验后，才可授权 persist。

---

## 坑 30：source `latest_oos` batch 不是平台 strict PIT 真值

**严重等级**：Critical。把一次性事后 batch 当成 live-like 历史回测，会把后续窗口状态带回早期 `feature_date`，形成数据泄漏风险。

**现象**（7Y_03 入库中确认）：`liwei_0616_7y03_cons_all_k3_div_k8` 的 source latest_oos batch 共 21 行，source batch active accuracy 为 `15/20=75.0%`。但平台按每个 `feature_date` 独立截止重建 strict PIT 后，21 行中的 3 行与 source batch 不同：

| feature_date | source batch | strict PIT |
|--------------|-------------:|-----------:|
| `2026-05-19` | `-1` | `1` |
| `2026-05-20` | `1` | `0` |
| `2026-05-21` | `-1` | `0` |

平台 canonical 结果因此为 18 个 active 样本、13 个 correct，accuracy=`13/18=72.22%`；不是 source batch 的 `75.0%`。

**根因**：
1. source latest_oos 是一次性窗口输出，不保证每个早期 `feature_date` 都只使用当日可见窗口状态。
2. V31 方案含 consensus、streak fallback、selector/window 状态，后续样本可能影响 batch 内早期日期的状态路径。
3. live/gray/scheduled live 的业务语义要求 `feature_date` 是硬截止；历史回测如果想模拟 live，必须逐 `feature_date` 或经过证明等价的 PIT fast path。

**修复/处置**：
1. `schemes/liwei_0616_7y03_cons_all_k3_div_k8/benchmarks/original_predictions_sample.csv` 和 current benchmark 使用 strict PIT canonical rows，而不是直接复制 source batch。
2. source batch 的原始 `15/20=75.0%` 只作为 source evidence 记录在 benchmark README / summary，不作为平台 front-end/API 验收真值。
3. `source_batch_differences` 已写入 summary JSON，记录差异日期、source batch 方向与 strict PIT 方向。
4. latest persisted backtest run_id=`122` 的 13 条 historical benchmark 覆盖行与 DB/API 0 diff；8 条 gray/live 边界样本与 `/api/metrics/liwei_0616_7y03_cons_all_k3_div_k8__h5__7Y` 0 diff。

**10Y_01 当前证据**：`liwei_0616_10y01_cons_say_k3_div_k10` 的 source latest_oos batch 共 21 行，source batch 证据为 16 个 traded、`12/16=75.0%`、5 个 no-trade。平台 strict PIT canonical benchmark 为 19 个 metric samples、15 个 correct、2 个 no-trade，`source_batch_differences` 记录 12 个差异行；这不是手工修正，而是用同一个平台 PIT inference 入口按每个 `feature_date` 硬截止重建得到。latest persisted backtest run_id=`123` 的 13 条 historical benchmark 覆盖行与 `/api/backtests/factor-lab?benchmark_id=liwei_0616_10y_01&data_source=framework_db_aligned` 0 diff；8 条 gray/live 边界样本与 `/api/metrics/liwei_0616_10y01_cons_say_k3_div_k10__h5__10Y` 0 diff。

**10Y_02 当前证据**：`liwei_0616_10y02_cons_say_k3_div_k5` 的 source latest_oos batch 共 21 行，source batch 证据为 20 个 traded、`14/20=70.0%`、1 个 no-trade。平台 strict PIT canonical benchmark 为 19 个 metric samples、15 个 correct、2 个 no-trade，`source_batch_differences` 记录 9 个差异行；这不是手工补结果，而是用同一个平台 PIT inference 入口按每个 `feature_date` 硬截止重建得到。latest persisted backtest run_id=`124` 的 13 条 historical benchmark 覆盖行与 `/api/backtests/factor-lab?benchmark_id=liwei_0616_10y_02&data_source=framework_db_aligned` 0 diff；8 条 gray/live 边界样本与 `/api/metrics/liwei_0616_10y02_cons_say_k3_div_k5__h5__10Y` 0 diff。

**规则**：
1. Source batch 可以证明“源报告如何得到某个数字”，不能自动证明“平台 live-like PIT 应该输出同一个数字”。
2. 若平台选择 strict PIT，benchmark current、historical backtest 和 live/gray rows 都必须由同一个 PIT inference 入口生成。
3. 若 source batch 与 strict PIT 不同，不得手工补预测结果，也不得为了通过 CompareGate 修改 current 为 source batch；必须记录差异和原因。
4. 只有明确批准的 source-original batch reproduction 例外，才允许 historical backtest 保留非 PIT 口径；该例外不得改变 gray/live/scheduled live 的 feature cutoff 规则。

**检查清单**：
- [ ] source `date/T/predict_date` 已解释为平台 `feature_date`。
- [ ] 已判断 source `latest_oos` 是 strict PIT 还是事后 batch。
- [ ] canonical benchmark rows 由平台 PIT 入口生成，或已记录批准的 batch reproduction 例外。
- [ ] source batch 与 PIT 差异写入 summary/README/CURRENT_STATUS，不再靠口头说明。
- [ ] 前端/API 验收使用 `target_date` 分流到 historical backtest 与 gray/live metrics，逐行 key 为 `feature_date + target_date + target_tenor + horizon`。

---

## 坑 32：source benchmark 边界验证不能替代完整 gray_live 网格回补

**严重等级**：High。source latest_oos / benchmark 边界样本通过，只能说明跨历史/灰度边界的对齐完成；不能证明前端当前月份的 live 样本已经补齐。

**现象**（7Y_03 入库中发生）：`liwei_0616_7y03_cons_all_k3_div_k8` 首轮 gray_live 只写入 source latest_oos 边界中的 8 行，`target_date=2026-06-01..2026-06-10`。当 `t_scheme_actuals` 已经覆盖到 `2026-06-17` 时，前端 6 月 live 明细仍只有 8 条，漏掉了 `target_date=2026-06-11/12/15/16/17` 这些已经可以验证的样本，也漏掉了后续 target 尚未出 actual 的待验证样本。

**根因**：
1. source benchmark 的灰度边界样本是“源证据覆盖范围”，不是“平台 gray_live 应补范围”。
2. SOP Step 10b 的目标是补齐平台观察网格：从 `gray_start` 起，按交易日历枚举所有应当存在的 `target_date`，再反推 `feature_date` 和 `predict_date`。
3. 前端/API 的总样本数按平台 DB 中 live rows + actual join 动态展示；只补 benchmark 边界会让已出 actual 的日期没有预测行，样本数自然对不上。

**本次补齐结果**：
- 首轮 7Y_03 gray_live：run_id=`146..153`，8 条，`target_date=2026-06-01..2026-06-10`。
- 补齐后 7Y_03 gray_live：run_id=`146..163`，18 条，`predict_date=2026-05-26..2026-06-18`，`feature_date=2026-05-25..2026-06-17`，`target_date=2026-06-01..2026-06-25`。
- `/api/metrics/liwei_0616_7y03_cons_all_k3_div_k8__h5__7Y` 返回 `daily_rows=18`，其中 13 条已有 actual、5 条待验证。
- run_id=`156` 对应 `predict_date=2026-06-09` 的 LiveGate 因外部 DataBridge 在 gate 窗口写入 `api_wind_weekly +4` 而 fail-closed；模型执行和 prediction row 均已成功写入。剩余补齐在临时静止 DataBridge 后完成，source table deltas 为 0。
- 10Y_01 gray_live 从一开始即按完整平台网格补齐：run_id=`164..181`，18 条，`predict_date=2026-05-26..2026-06-18`，`feature_date=2026-05-25..2026-06-17`，`target_date=2026-06-01..2026-06-25`；source benchmark 的 8 条 gray/live 边界样本逐 key 0 diff。2026-06-22 已观察到首条自然 `scheduled_live`，predict_date=`2026-06-22`、target_date=`2026-06-26`，因此该方案状态已从 **Onboarding Complete** 升级为 **Production Observed**；`/api/metrics/liwei_0616_10y01_cons_say_k3_div_k10__h5__10Y` 当前返回 18 条 `gray_live` 加 1 条 `scheduled_live`。
- 10Y_02 gray_live 从一开始即按完整平台网格补齐：run_id=`190..208`，19 条，`prediction_phase=gray_live`，`predict_date=2026-05-26..2026-06-22`，`feature_date=2026-05-25..2026-06-18`，`target_date=2026-06-01..2026-06-26`；source benchmark 的 8 条 gray/live 边界样本逐 key 0 diff。由于 2026-06-22 activation 发生在当日 07:03 scheduler 之后，当前是 **Onboarding Complete**，不是 **Production Observed**。2026-06-23 首次自然调度 run_id=`210` 因源数据缺失失败，数据补齐后受控补跑 run_id=`233` 成功写入 1 条 `scheduled_live`（`predict_date=2026-06-23`、`feature_date=2026-06-22`、`target_date=2026-06-29`）；`/api/metrics/liwei_0616_10y02_cons_say_k3_div_k5__h5__10Y` 当前返回 19 条 `gray_live` 加 1 条 `scheduled_live`，但 Production Observed 仍等待下一次真实时钟自然触发成功。

**修复规则**：
1. source latest_oos 的 gray/live 边界样本只用于 strict key 对齐验证，不作为 gray_live 补齐范围的上限。
2. gray_live 补齐范围必须由平台 target-date 网格决定：`target_date >= gray_start` 且 `feature_date` 已可由当前源数据构建。
3. 补齐后必须同时核验 DB 网格、`/api/metrics` 的 `daily_rows` / `phase_ranges`、已出 actual 样本数和待验证样本数。
4. 若 LiveGate 因外部源表写入 fail-closed，先确认预测写库结果和外部写入来源；后续补齐应在源库静止窗口内重跑 gate，不能把外部 delta 当作方案通过证据。

**检查清单**：
- [ ] benchmark 边界样本与完整 gray_live 补齐范围分开验收。
- [ ] 已枚举完整 `target_date` 网格，而不是只跑 source latest_oos 行。
- [ ] live rows 覆盖所有已经有 actual 的 target_date。
- [ ] 后续 target 尚未出 actual 的预测行保留为 pending，不因未验证而漏写。
- [ ] `CURRENT_STATUS.md` 记录最终 run_id 范围、date 范围、API daily_rows 和 pending 样本数。

---

## 坑 33：targeted sample 跨灰度边界时不能沿用 historical input_end 和 live cutoff

**严重等级**：Medium。targeted sample 是验证工具，不是正式 historical persist；如果它跨过 `target_date >= gray_start`，仍沿用 full historical 的 input end 和 cutoff，会让验证样本被错误丢弃或因为未来 label 缺失而无输出。

**现象**（10Y_02 入库中发现）：执行 `--no-persist --sample-dates 2026-05-06,...,2026-06-03 --batch-mode daily` 时，`feature_date=2026-05-25` 报错 `produced no row`。该日期的 T+5 `target_date=2026-06-01` 已进入灰度边界，而原 runner 仍使用 full historical 默认 `BACKTEST_INPUT_END=2026-05-29`；`require_labels=True` 时无法看到 `2026-06-01` 的 label，同时 `build_backtest_rows` 还会按 full historical 规则过滤 `target_date >= 2026-06-01`。

**根因**：
1. full historical runner 必须排除 gray/live 区间，但 targeted sample 常用来验证 gray/live 边界；二者语义不同。
2. sample mode 需要把 input artifact end 扩展到 `max(sample target_date)`，否则 label 或辅助输入 as-of 不足。
3. sample mode 禁止 persist，因此保留 gray/live 边界 rows 不会污染 historical backtest 表。

**修复规则**：
1. sample_dates 非空时，runner 先用 DB 交易日历计算每个 sample 的 `target_date=T+horizon`，并把 daily/weekly/monthly artifact end/as_of 扩展到 `max(BACKTEST_INPUT_END, max_target_date)`。
2. sample mode 下 `build_backtest_rows` 不应用 full historical 的 `target_date < gray_start` 过滤；full historical no-persist/persist 仍必须过滤。
3. sample mode 必须继续禁止 persist，并在 output summary 标记 `backtest_scope=targeted_sample`、记录 `sample_dates` 与有效 `backtest_input_end`。
4. daily strict 与 monthly fast path 的 gray-boundary 等价验证必须使用同一组 sample_dates 和同一个 effective input_end，否则 diff 结论不可比。

**10Y_02 当前证据**：修复后新增单测覆盖 sample mode 扩 input_end 与保留 live-boundary rows；14 日期 daily strict 与 monthly fast path 均输出 14 行、`backtest_input_end=2026-06-10`，逐行 diff_count=0。

**检查清单**：
- [ ] sample mode 禁止 persist。
- [ ] sample output summary 记录 `backtest_scope=targeted_sample`。
- [ ] sample 跨灰度边界时，effective input_end 至少覆盖最大 target_date。
- [ ] full historical runner 仍排除 `target_date >= gray_start`。
- [ ] fast path diff 的 daily/monthly 两边使用同一 effective input_end。

---

## 坑 34：周度实盘需要提前补齐目标周 `api_wind_date`

**严重等级**：High。周度模型的特征输入只截止到 `feature_date`，但 `target_week_id/target_date` 解析需要知道下一权威周的最后交易日；如果 `api_wind_date` 只补当天，周六周度实盘会缺目标周日历，导致 target_date 过早、样本缺失或 scheduler failed run。

**现象**（2026-06-20 周度补跑中确认）：
- `predict_date=2026-06-20` 是周六，实盘语义应为 `feature_date=2026-06-18`、`feature_week_id=202623`、`target_week_id=202624`、`target_date=2026-06-26`。
- 事故发生时 `api_wind_date` 的未来目标周不完整，缺少 `2026-06-24/25/26` 的 `week_id=202624`；周度目标周只能解析到已存在的局部日期，前端 2026-06 周度验证样本不完整。
- 同期日频关键 Y 输入也停在 `2026-06-18`，导致 `predict_date=2026-06-23` 的部分 daily scheme 因 `feature_date=2026-06-22` 无源数据而失败。两类问题同源于上游数据/日历未补齐，但修复顺序不同。

**根因**：
1. `api_wind_date` 不只是历史源数据日期表，也是平台 `week_id -> last trading day` 的权威映射来源。
2. 日频预测只需要前一交易日的源数据；周度预测虽然不能读取未来周特征值，但必须提前知道目标周的交易日历，否则无法稳定写入正确 `target_date`。
3. BondPrediction 原有 `insertDateList.py` 的默认语义是只补当天；该假设对日频够用，对周度目标周解析不够。

**修复/处置**：
1. 先补齐 `api_wind_date` 未来窗口，确保目标周最后交易日已存在。本次人工补齐后，`2026-06-23..2026-07-07` 无缺口，`2026-06-24/25/26` 均为 `week_id=202624`。
2. 在 BondPrediction 增加周五 `22:30` 的未来两周窗口任务：`insertDateList.py --ensure-ahead-days 14`，日志写入 `insert_date_weekly_window.log`。脚本仍保留无参/单日入口，窗口补齐只由该周五任务显式触发。
3. 日历补齐后先刷新 actuals，再按 scheme 白名单补跑，不跑无参 `--run-once predictions`：
   - `python -m scheduler.main --run-once actuals --date 2026-06-22 --force`
   - 周度三方案补跑 `predict_date=2026-06-20`。
   - 修复 `t5_daily` 的 `2026-06-22 -> target_date=2026-06-26` 覆盖问题。
   - 重跑 `2026-06-23` 全部 active daily。
4. 保留 failed runs 作为审计证据，不删除失败记录；新的 success run 补上业务结果。

**本次验收证据**：
- 周度补跑成功：`weekly_10y_d_overlay_0529` run_id=`221`、`weekly_5y_direct_0529` run_id=`222`、`weekly_7y_cross_d_overlay_0529` run_id=`223`，三条均为 `feature_date=2026-06-18`、`target_date=2026-06-26`。
- 关键 Y 输入已补齐到 `2026-06-22`：`TB1YWI0C/TB3YWI0C/TB5YWI0C/TB7YWI0C/TB0YWI0C` 的 `MAX(rdate)=2026-06-22`。
- `2026-06-23` active daily 全部重跑成功，全部 `feature_date=2026-06-22`；之前失败的 `liwei_0616_10y02_cons_say_k3_div_k5` 以 run_id=`233` 成功落库。
- `t5_daily` 同时保留 `2026-06-22 -> 2026-06-26` 和 `2026-06-23 -> 2026-06-29` 两组 scheduled_live 结果。

**检查清单**：
- [ ] 周五晚或周六周度预测前，`api_wind_date` 至少覆盖未来两周自然日。
- [ ] 目标周最后交易日所在行存在，且 `week_id` 与 feature 下一权威周一致。
- [ ] 周度 adapter 的输入 artifact 仍使用 `end_week=feature_week_id`、`as_of_date=feature_date`，不得读取未来周特征。
- [ ] 补跑时只按明确 scheme 白名单执行，禁止无参 `--run-once predictions` 误触发 weekly。
- [ ] failed run 保留，success run 和 prediction row 作为业务修复结果。
