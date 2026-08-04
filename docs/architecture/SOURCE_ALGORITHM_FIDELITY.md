# 源算法保真强约束

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：上游算法、Native 维护、Harness 和审计人员
**最后核验日期**：2026-08-04

本文定义 source-backed 算法的保真责任。Native V1 与 Blackbox V2 的可观察边界不同，不能使用同一套内部核验声明。

## 0. 运行时责任

| 运行时 | 谁保证内部保真 | 平台可检查的证据 | 平台不得宣称 |
|---|---|---|---|
| Native V1 | 平台维护人员与原算法所有者共同负责 | core diff、source runner、original/current benchmark、方向和内部 score | 未核验内部字段时“算法完全一致” |
| Blackbox V2 | 上游算法工程师负责 | 脚本/Metadata 摘要、CLI、确定性、predict/backtest 一致、分批/顺序一致、截止隔离和标准 Result | 已检查模型参数、特征、内部 score 或训练路径 |

Native V1 的 L0/L1/L2 分级仅用于政策清单中的存量维护；发现 L2 或形成新算法时，停止 Native 修改并创建独立 Blackbox V2 trial。Blackbox 上游应在交付前完成自身 source 对账，平台不反编译、不拆分也不改写交付脚本。

### 0.1 首次技术入库与已入库修订

Native 的 source benchmark 与 CompareGate 是首次技术入库的硬证据：新 Native 身份、新算法、新 target、新 task 或 runtime 迁移不得绕过该路径。ActivationGate 的 current-full-`all` 与 maintenance 路径互斥：当前 exact version 完整 `all` 通过时使用 `full_initial_onboarding_v1`，仅要求当前七个 Gate（含 Compare），不要求 prior snapshot；只有未走该 full-`all` profile 的已有 Native 修订，在 prior `all` 的 `static.business_identity` 已持久化、且与当前 `scheme_id`、`runtime_type`、`horizon`、`task_type`、`frequency`、target tenors 和全部 active composite Registry IDs 精确匹配时，才可使用 `native-maintenance`（`static -> native-maintenance-admission -> input -> unit -> dry-run -> api-readiness`）激活；该快照不含代码、config 或 version hash。

该 maintenance 路径不执行当前 historical `compare/backtest`，但必须留存旧 version 的 passed `all + compare`、匹配的 prior `static.business_identity`、当前六个 Gate、精确 version、Registry identity、授权与 live-safe oracle。legacy admission 没有该快照时必须 fail-closed：当前唯一已实现 fallback 是 current exact version 重跑完整 `all`。`legacy admission identity attestation` 尚未设计或实现；未来即使建设也必须先独立设计、实现并取得明确专项授权，当前不得由 Gate 自动生成、推断或执行。满足任一已实现 profile 的同一身份修订，其历史 source-benchmark 输入 vintage 漂移仅作归档诊断，不能单独阻断 activation、gap repair、`gray_live`、`scheduled_live` 或 API/前端读回；这不允许修改 Native core、算法参数、source benchmark、输入截止、统一周历、日期语义或 L0/L1/L2 边界，也不改变 Blackbox CompareGate。

## 1. 总原则

对于 Native V1，平台可以维护原始算法的适配层，但不得重写原始算法。`predict.py`、backtest runner、harness 和输入 artifact 只能负责平台边界：取数、日期映射、调用、落库、缓存、审计和对比。算法本身的计算路径必须与原始脚本保持一致。

以下事项均属于算法逻辑，默认不得修改：

- 历史输入起点、训练起点、测试起点、source batch 终点、`test_start/test_end/test_ranges`。
- PIT / batch / rolling / walk-forward / monthly fast path 的执行口径，包括分组键、每组 `source_end`、`current_start/current_end` 和最终抽取样本范围。
- daily / weekly / monthly 对齐规则，包括周频值放置日、ffill 方向、月频滞后规则和缺失列处理。
- label、horizon、target 日期、样本筛选、灰度/回测截断之前的算法内部样本定义。
- 特征集合、信号族、跨期限组合、rolling 窗口、`min_periods`、fillna/ffill/bfill、符号定义。
- LGBM 或其它模型的 grid、seeds、权重、early stopping、subsample/colsample、线程/进程策略中会影响结果的参数。
- ensemble、selector、vote、fallback、streak、threshold、seasonal VT、report mask 和 score/confidence 映射。
- 原始脚本里的固定算法锚点，即使它们看起来像日期窗口，也不得跟随平台 PIT 窗口移动。例如 10Y02 `latest_oos` runner 只 patch `test_idx` 的 source batch 窗口；`IC screening` 仍按原始 `2024-01-01` 截止点做 pre-test 特征筛选，平台不得改成 `current_start/test_start`。

如果原始脚本里某个变量名与平台字段同名，不得按名字直接映射。必须先读原始脚本如何使用它，再决定它对应平台的 `predict_date`、`feature_date` 或 `target_date`。

## 2. 允许的适配

以下改动属于平台适配，允许存在，但必须保持输出等价：

- 把原始文件读取改为接收 `pd.DataFrame` 或平台 input artifact。
- 把原始输出转换为 `PredictionRecord`、backtest row 或 benchmark CSV。
- 把 source T 映射为平台 `feature_date`，再由平台日历推导 `target_date`。
- 把路径、缓存、日志、extra、artifact hash、run summary、授权和写库从算法外层接入平台。
- 为了复现原始执行口径，把 source batch 的 `source_start/source_end/current_start/current_end` 显式传给 core。
- 对经批准的投票类方案，在当前 feature 输入及必要字段有效、core 正常完成且输出非空/feature key 合法、但当前 feature key 缺少最终输出时，由 adapter/backtest 输出层按 `no_signal_to_flat_v1` 生成平台平记录。该规则属于 L0 业务输出适配，不得修改 core、投票、fallback、阈值或内部 score；平台政策行必须从 source-original/current benchmark 和 compact CompareGate 输出中排除。

允许的适配不得改变算法计算结果。若改造后方向或内部模型分数发生变化，先查输入 artifact、日期窗口、对齐规则和原始脚本 diff，不得通过调参或改信号去贴结果。

移植 source runner 时，必须逐行区分“runner 明确 patch 的字段”和“原始算法保留的固定字段”。不要因为外层改了 `context_start/latest_start/data_end`，就同步移动未被 runner patch 的筛因子起点、训练 warmup、report mask 或校准窗口。

## 2.1 算法改动分级与停止条件

Native source-backed 存量方案修复或复核时，所有改动必须先分级，再进入 gate。分级不是事后说明，而是维护 Intake 的硬约束：

| 等级 | 定义 | 处理规则 |
|------|------|----------|
| L0 平台适配 | 只改变文件路径、输入 artifact、日期字段映射、输出 schema、extra、缓存、日志、授权、写库或 API 展示 | 允许，但必须证明输出等价 |
| L1 source runner 上下文 | 把原始 runner 明确 patch 的 `source_end/current_start/current_end/test_ranges` 等外层上下文参数显式传给 core | 允许，但必须逐项列出 runner 明确 patch 的字段和不可移动的固定锚点 |
| L2 算法内部改动 | 改变原始算法的历史起点、筛因子起点、test sequence、分组键、特征构造、周/月频对齐、模型参数、selector、streak、fallback、VT、投票或内部 score 映射 | 默认禁止；发现后停止 Native 修复并创建独立 Blackbox V2 trial |

本轮 Liwei 方案复核中已经确认的 L2 误改类型，后续不得重复：

- **固定算法锚点被误随窗口移动**：把 10Y02 `IC screening` 截止点从原始 `2024-01-01` 移到 source batch `test_start=2025-05-01`，会改变 baseline 选择并导致 `2026-05-25` 方向漂移。修复方式是只 patch source runner 明确 patch 的 test index，保留未 patch 的固定筛因子锚点。
- **source test sequence 被替换**：把 10Y01 / 7Y03 source `latest_oos_20260616` 的 prior-year + latest 两段测试集改成单段 full-OOS，或把 10Y02 full-OOS sequence 改成逐日局部 PIT / `latest_oos` 局部窗口，会改变 monthly ensemble、selector、streak、fallback 和内部 score。修复方式是按 source runner 原始 test sequence 复现。
- **回测分组键被替换**：把 10Y02 target-date 月度回测改成 feature 月分组或所有日期共用一个全局 `source_end`，会改变 4 月 target-date 结果。修复方式是按 `target_date` 月分组，每组 `source_end` 固定为该 target 月最后一个目标交易日。
- **特征、VT 或内部 score 映射被替换**：5Y01/V31 中改变 `build_bond_features` 列顺序、`vt_mode=seasonal`、weekly last-trading-day ffill、`vs_full` score 映射或 baseline dir 映射，都会造成内部数值不一致。修复方式是按 source 构造顺序和 source pkl score 口径恢复。
- **raw source batch 与 live-safe 混用**：把固定 `source_end=2026-06-10` 的 source batch 边界样本当成 live 逐日内部数值真值，会把正常口径差异误判为算法错误。修复方式是 historical/source-original 与 live-safe 分开验收。

若出现方向一致但内部 `vote_score`、baseline `*_score/*_vs`、`*_dir/*_sign` 或 probability/confidence 不一致，不能先写“通过”。首次技术入库、或差异仍可能是 L0/L1/L2 当前算法问题时，必须先定位属于 L0 数据/导出差异、L1 上下文误传，还是 L2 算法内部误改；未完成分级和归因前，不得进入 backtest persist、live repair 或 activation。已有首次技术入库后已归因的历史输入 vintage 漂移除外：它只能在匹配 prior `static.business_identity` 的后续维护路径中归档，不能被重写为 current benchmark pass，也不是满足该身份前提的同一业务身份修订的独立阻断项。

## 3. Source 口径分类

每个 Native source-backed 存量方案在维护复核前必须先分类，并写入方案 benchmark README、summary 或状态文档：

| 口径 | 含义 | 平台要求 |
|------|------|----------|
| `source_original_reproduction` | 复现原始脚本 / 原始 batch 的真实执行方式 | 历史 benchmark、current benchmark、backtest 输出必须与原始结果逐样本对齐 |
| `source_strict_pit` | 原始算法本身就是逐 `feature_date` 硬截止 PIT | 平台 PIT helper 必须与原始 PIT 入口等价 |
| `platform_live_pit_variant` | 原始交付是 batch/事后窗口，但业务另行要求构造 live-like PIT 变体 | 必须显式批准并标为平台变体；不得宣称它复现了原始 source 输出 |

默认口径是 `source_original_reproduction`。只有在用户明确批准或原始 source 文档明确要求 live-like PIT 时，才能采用 `platform_live_pit_variant`。即使采用平台 PIT 变体，也不得修改原始算法内部逻辑；只能改变外层传入的可见数据截止和测试上下文，并必须记录它与 source-original 输出的差异。

历史回测与实盘逐日调度必须分开验收。若 source-original batch 使用了晚于某个样本 `feature_date` 的固定 `source_end`、later test window、selector/streak 状态或同批次未来样本，那么这些内部数值只能作为 source-original backtest/benchmark 的真值，不能直接要求 `gray_live` / `scheduled_live` 逐日记录相等；实盘记录必须保持 `feature_date` 硬截止，只能与同一 live-safe 数据截止和同一外层上下文生成的 live-safe oracle 对齐。反过来，也不得把 live-safe PIT 输出冒充 source-original batch reproduction。

2026-06-27 Liwei source 方案裁决固定如下：`10Y01/10Y02/7Y01/7Y03/5Y01` 的 source-original historical backtest 已按各自原始 batch/window 与 latest benchmark 对齐；live/scheduled_live 行已修正到当前 version、`baseline_scores`、`model_scope=*_source_compatible_context` 和 `model_source_end=feature_date`。这只证明 live 行结构和 live-safe 截止正确，不证明固定 `source_end=2026-06-10` 的 `original_predictions_sample.csv` live 边界样本可作为逐日 live 内部数值真值。任何报告若同时引用 source batch benchmark 与 live rows，必须显式拆成 `historical/source-original` 和 `live-safe` 两段，并分别给出 diff；不得用“benchmark 文件完全一致”替代 DB/API/live 对齐证据，也不得用 `TOTAL_BAD=0` 断言所有预测值与原始 batch benchmark 完全一致。

## 4. 验收标准

Source-backed 方案首次技术入库的最低验收标准：

1. `predicted_direction` 逐样本零差异。
2. `label/actual/is_correct` 逐样本零差异。
3. `target_date/target_tenor/horizon` 逐样本零差异。
4. 原始算法暴露的内部模型分数、baseline score、baseline direction、probability 或 confidence 必须进入逐方案 original/current benchmark 和 CompareGate 比对。能做到 bitwise/导出精度一致时必须一致；仍有残差时，必须证明残差来自输入 artifact 或导出精度，而不是算法逻辑变更。
5. 若只做到方向一致但内部模型分数不一致，不得宣称“算法逻辑完全一致”；只能宣称“最终方向一致，内部数值仍有残差待归因”。

对多 baseline 方案，`STD/ACCWT/V55_7Y/DIV` 这类内部输出属于验收对象，不是可忽略调试字段。它们必须写入 benchmark CSV；实盘/backtest 输出应在 `extra`、cache 或 compare report 中保留同名映射，便于后续审计。

10Y02 的 source-original 回测必须保留原始 full-OOS test sequence：先用 `("2024-01-01", source_end)` 作为唯一测试序列完整运行 baseline，再从结果中抽取 current target window 的 feature_date。不得把它替换成逐 feature_date strict PIT，也不得替换成 `latest_oos` runner 的“去年同期窗口 + 最新窗口”局部测试集；这些局部窗口会改变 monthly ensemble top-K、signal selection、seasonal VT 和 streak 状态。target-date 月度回测还必须按 `target_date` 所属月份分组：每组 `source_end` 固定为该 target 月最后一个目标交易日，`current_start/current_end` 固定为该组 feature_date 的首尾；不得改成 feature 月分组，也不得把所有历史 feature_date 合成一个全局窗口。2026-04 target-date 复查已证明：`feature_date=2026-03-25..2026-04-23`、`target_date=2026-04-01..2026-04-30`、`source_end=2026-04-30` 的 full-OOS 口径可与用户 CSV 21/21 对齐，而局部窗口或全局 source_end 会产生方向或内部数值漂移。

10Y02 的保真验收基线必须包含 `2026-05-25`：原始 `STD/ACCWT/DIV` 为正、`V55_7Y` 为负，最终共识结果为 `0`。若平台输出 `-1`，优先检查 IC screening cutoff 是否误随 `test_start` 从 `2024-01-01` 移到了 source batch 的 `2025-05-01`。若最终方向一致但 `V55_7Y_vs` 仍有 `1e-2` 量级残差，不能直接判定算法不一致，也不能调参贴数；必须先固定并记录原始 CSV 对应的生成脚本、输入三件套、`api_wind_date`、LightGBM/NumPy/Pandas 版本和 source `bond_common.py` hash，再做 source-vs-platform 同环境对比。

5Y01 的保真验收必须同时检查 V31 口径：`vt_mode` 必须保持 `seasonal`，`build_bond_features` 的列生成顺序必须与 source 一致（spread features before streak/up-fraction features），内部 `STD/DIV/ACCWT` 的 `vs_full` 必须作为 baseline score 对比。若最终方向一致但 `vs_full` 有残差，优先检查特征列顺序、signal accuracy lookback、weekly last-trading-day ffill 和 source pkl score 映射。

## 5. 禁止项

- 不得为了通过 CompareGate 修改原始算法的时间起点、窗口、特征、信号、模型参数、投票或 fallback。
- 不得把“平台认为更合理”的 PIT 口径替代原始 source 口径，除非明确标成平台变体并获批。
- 不得把 source batch 差异解释为“正常”后继续声称 source reproduction 已通过。
- 不得复制 benchmark 结果当作 current 输出，也不得手工补预测方向。
- 不得把 `signal_policy_applied=true` 的平台补平行写入或导出为 source-original/current benchmark。平台补平是可审计的确定性输出规则，不是手工预测，也不是原始算法输出。
- 不得用后验结果调节内部 score，使其只在当前样本上贴合原始 CSV。
- 不得让 adapter、backtest runner 或缓存策略改变同一 `feature_date` 的算法输出。

## 6. 必留证据

每个 source-backed 方案至少保留：

- 原始脚本或原始输出文件路径、hash、版本说明。
- 原始算法执行口径分类。
- 平台输入 artifact 路径、hash、data_version、source_start/source_end 或 as-of 信息。
- original vs current 的逐样本主键、方向、actual、correctness 对比。
- 内部模型分数对比：字段名、最大绝对差、方向差异数、最大差异日期。
- 若内部数值不完全一致，写明残差归因和下一步，且不得把它包装成“完全一致”。
- 对已入库同一身份的 Native 修订，若当前 exact version 走 full `all`，保留 `full_initial_onboarding_v1` 的七个 Gate；若走 maintenance，保留 prior passed `all + compare`、匹配的 `static.business_identity` 业务快照、当前 `native-maintenance` 六个 Gate、精确 Registry identity、输入 cutoff/统一周历/日期语义和 live-safe oracle。legacy snapshot 缺失时记录 fail-closed 的 full-`all` 路径；未实现的 attestation 不得作为当前路径。历史 benchmark vintage 漂移必须明确标为归档诊断。
