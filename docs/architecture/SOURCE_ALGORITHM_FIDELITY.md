# 源算法保真强约束

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：上游算法、平台与 W4 运行维护人员
本文定义 source-backed 算法的保真责任。Native V1 与 Blackbox V2 的可观察边界不同，不能使用同一套内部核验声明。

## 0. 运行时责任

| 运行时 | 谁保证内部保真 | 平台可检查的证据 | 平台不得宣称 |
|---|---|---|---|
| Native V1 | 平台维护人员与原算法所有者共同负责 | core diff、source runner、original/current benchmark、方向和内部 score | 未核验内部字段时“算法完全一致” |
| Blackbox V2 | 上游算法工程师负责 | 脚本/Metadata 摘要、CLI 和标准 Result | 已检查模型参数、特征、内部 score 或训练路径；确定性、predict/backtest 一致、分批/顺序一致、截止隔离也不由平台验证，属上游交付契约义务 |

W4 九套 Native 固定当前版本，仅保持既有输入、依赖和日/周/月调度；运行故障按[W4 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)定位和恢复。后续算法与版本修订全部走 Blackbox，不再使用 Native Harness 修订或重新激活。L0/L1/L2 和 Native 对账标准保留用于理解历史适配、识别故障与算法变化，不构成现行 Native 入库路径。Blackbox 上游交付前完成自身 source 对账；平台不反编译、不拆分或改写交付脚本。

只有需要将上游自测与平台输出作等价对比时，双方才须绑定相同 Request 和同一输入 generation、文件摘要及
snapshot 身份；不一致时先归类为输入 vintage 差异，不能据此归因算法。各部署环境独立入库使用自己的
数据库和输入，不要求两机数据相同。对账不授权重跑、覆盖或搬迁已发布事实。

## 1. 总原则

历史 Native 适配将取数、日期映射、调用、落库、缓存和对比置于平台边界，算法计算路径须与原始脚本一致。下列界限用于保护现存 W4 及解释旧版本，不授权继续修订 Native 算法或适配层。

以下事项均属于算法逻辑，默认不得修改：

- 历史输入起点、训练起点、测试起点、source batch 终点、`test_start/test_end/test_ranges`。
- PIT / batch / rolling / walk-forward / monthly fast path 的执行口径，包括分组键、每组 `source_end`、`current_start/current_end` 和最终抽取样本范围。
- daily / weekly / monthly 对齐规则，包括周频值放置日、ffill 方向、月频滞后规则和缺失列处理。
- label、horizon、target 日期、样本筛选、灰度/回测截断之前的算法内部样本定义。
- 特征集合、信号族、跨期限组合、rolling 窗口、`min_periods`、fillna/ffill/bfill、符号定义。
- LGBM 或其它模型的 grid、seeds、权重、early stopping、subsample/colsample、线程/进程策略中会影响结果的参数。
- ensemble、selector、vote、fallback、streak、threshold、seasonal VT、report mask 和 score/confidence 映射。
- 原始脚本里的固定算法锚点，即使它们看起来像日期窗口，也不得跟随平台 PIT 窗口移动。

如果原始脚本里某个变量名与平台字段同名，不得按名字直接映射。必须先读原始脚本如何使用它，再决定它对应平台的 `predict_date`、`feature_date` 或 `target_date`。

## 2. 允许的适配

历史 Native 适配中的下列变换属于平台边界，判断其保真仍须保持输出等价；这不恢复新 Native 版本维护入口：

- 把原始文件读取改为接收 `pd.DataFrame` 或平台 input artifact。
- 把原始输出转换为 `PredictionRecord`、backtest row 或 benchmark CSV。
- 把 source T 映射为平台 `feature_date`，再由平台日历推导 `target_date`。
- 把路径、缓存、日志、extra、artifact hash、run summary、授权和写库从算法外层接入平台。
- 为了复现原始执行口径，把 source batch 的 `source_start/source_end/current_start/current_end` 显式传给 core。
- Native 正常无信号补平属于 L0 输出适配，前提见 §2.2，不改变 core 算法。

允许的适配不得改变算法计算结果。若改造后方向或内部模型分数发生变化，先查输入 artifact、日期窗口、对齐规则和原始脚本 diff，不得通过调参或改信号去贴结果。

复核历史 source runner 时，必须逐行区分“runner 明确 patch 的字段”和“原始算法保留的固定字段”。不要因为外层改了 `context_start/latest_start/data_end`，就同步移动未被 runner patch 的筛因子起点、训练 warmup、report mask 或校准窗口。

## 2.1 算法改动分级与停止条件

复核历史 Native 适配或诊断 W4 差异时，使用下表区分平台边界、上下文传递和算法变化；发现需要形成新版本，转 Blackbox 交付，不进入旧 Native Gate：

| 等级 | 定义 | 处理规则 |
|------|------|----------|
| L0 平台适配 | 只改变文件路径、输入 artifact、日期字段映射、输出 schema、extra、缓存、日志、授权、写库或 API 展示 | 历史适配须有输出等价依据 |
| L1 source runner 上下文 | 把原始 runner 明确 patch 的 `source_end/current_start/current_end/test_ranges` 等外层上下文参数显式传给 core | 复核 runner 明确 patch 的字段和不可移动的固定锚点 |
| L2 算法内部改动 | 改变原始算法的历史起点、筛因子起点、test sequence、分组键、特征构造、周/月频对齐、模型参数、selector、streak、fallback、VT、投票或内部 score 映射 | 不属于平台适配；停止 Native 修改，按 Blackbox 修订另行交付 |

方向一致但必要内部数值不同，仍须区分 L0 输入/导出差异、L1 上下文误传与 L2 算法变化；
未归因前不得把差异结果用于持久化或补缺。历史输入 vintage 漂移应归为输入差异，不能改写成当前 benchmark 通过；无需为保持 W4 日常调度补做旧 Harness 准入。历史数值对账标准见 §4。

### 2.2 正常完成后的无信号补平

允许在算法正常完成、输入和日期上下文有效、仅缺当前时点最终信号等条件下生成一条“平”记录，主要用于投票模型。这个业务原则适用于 Native 与 Blackbox，不是仅供历史 Native 使用的例外；也不表示两种运行时已经接入相同的平台补平能力。

执行链须确认算法正常完成，并区分“正常计算后没有当前信号”与执行失败。输入、日历、模型、超时、代码异常，以及整批空或非法输出均不能被补平掩盖；不能仅凭进程退出码为 0 或结果缺行认定为正常无信号。补平不得改变投票、selector、fallback、阈值或内部模型分数。

平台当前没有通用补平实现。Blackbox 可由交付脚本按其模型定义输出方向 0，但必须满足完整的[标准 Result](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)合同；平台不能将缺失或非法 Result 自动补成成功。以后接入通用补平时，须先明确并验证算法正常完成、仅缺当前信号的判据及失败边界。

补平不要求额外的来源区分、专属审计标记或计数报告；算法原生平与补平统一按[预测语义](PREDICTION_SEMANTICS.md#6-指标统计口径)统计。已有历史记录与统计结果不改写。

## 3. Source 口径分类

解读历史 Native source benchmark 或对照运行差异时，先辨认其口径；不要求 W4 日常维护新增分类报告：

| 口径 | 含义 | 平台要求 |
|------|------|----------|
| `source_original_reproduction` | 复现原始脚本 / 原始 batch 的真实执行方式 | 历史 benchmark、current benchmark、backtest 输出必须与原始结果逐样本对齐 |
| `source_strict_pit` | 原始算法本身就是逐 `feature_date` 硬截止 PIT | 平台 PIT helper 必须与原始 PIT 入口等价 |
| `platform_live_pit_variant` | 原始交付是 batch/事后窗口，但业务另行要求构造 live-like PIT 变体 | 必须显式批准并标为平台变体；不得宣称它复现了原始 source 输出 |

历史默认口径是 `source_original_reproduction`。`platform_live_pit_variant` 须有当时明确批准或原始 source 要求；不能从字段名推断。已有 PIT 变体也必须区分其可见数据截止、测试上下文与 source-original 输出，不将平台变体冒充原算法复现。

历史回测与实盘逐日调度必须分开验收。若 source-original batch 使用了晚于某个样本 `feature_date` 的固定 `source_end`、未来 test window、selector/streak 状态或同批次未来样本，那么这些内部数值只能作为 source-original backtest/benchmark 的真值，不能直接要求 `gray_live` / `scheduled_live` 逐日记录相等；实盘记录必须保持 `feature_date` 硬截止，只能与同一 live-safe 数据截止和同一外层上下文生成的 live-safe oracle 对齐。反过来，也不得把 live-safe PIT 输出冒充 source-original batch reproduction。

## 4. 验收标准

以下解释既有 Native 对账结果如何判定，不再作为 W4 新版本准入要求，也不适用于 Blackbox 包装验收；不得为满足旧标准重跑或修改历史事实：

1. `predicted_direction` 逐样本零差异。
2. `label/actual/is_correct` 逐样本零差异。
3. `target_date/target_tenor/horizon` 逐样本零差异。
4. 旧对账除方向外，还按方案声明比较内部模型分数、baseline score、baseline direction 和 probability 等必要数值。算法内部概率、阈值、排序、投票和方向计算必须原样保留；原始 benchmark 与已有审计不改写。对保留的必要内部数值，能做到 bitwise/导出精度一致时必须一致；残差须证明来自输入 artifact 或导出精度，而不是算法逻辑变更。
5. 若只做到方向一致但内部模型分数不一致，不得宣称“算法逻辑完全一致”；只能宣称“最终方向一致，内部数值仍有残差待归因”。

## 5. 证据不得伪造

不得复制 source CSV 冒充 current 输出、手工补预测方向或用后验结果调节内部数值来贴合样本。
PIT 变体只能按 §3 的批准口径表达。
算法不一致、尚未归因的数值残差或失败记录都不能通过改名、删除诊断或放宽比较条件写成通过。

## 6. 必留证据

已有 Native 原件、执行口径、输入截止和对账结果仍用于解释历史来源，不因停止 Native Harness 维护而改写或删除；无需为 W4 日常调度继续生成完整 Gate/benchmark 报告。需要定位既有材料时，从[当前状态](../CURRENT_STATUS.md)、Git 或对应 immutable release 查找。Blackbox 普通入库按平台 SOP 保留交付字节、标准调用及 Result，不复制内部算法测试。

### 6.1 历史 batch 例外的保留范围

已批准的 source-original batch reproduction 例外只解释指定方案的历史回测口径。
固定未来分段、全局校准或 selector 无法按逐点 PIT 复现时，必须保留原始批准范围、不能逐点复现的原因、
对应版本与输入、original benchmark 对齐结果，以及排除灰度 target 的证据。例外不得扩散为 live 真值：
live 仍须按自己的 `feature_date` 截止，并与同口径 live-safe oracle 对比。

历史周度单点例外涉及 `weekly_5y_direct_0529`、`weekly_7y_cross_d_overlay_0529`、
`weekly_10y_d_overlay_0529`。这些身份已使用 Blackbox，旧例外仅供已有历史追溯，不授权恢复旧 runner、
重跑迁移历史或套用到周平均任务。具体算法窗口与实施过程通过 Git、旧 immutable release 和原始证据追溯。
周度单点与周平均的 label/Actual 口径不可互用，日期与 target rule 见[预测语义](PREDICTION_SEMANTICS.md)。

## 7. 同算法 Native → Blackbox 迁移

本节解释已经明确批准的同算法运行时迁移及其历史保护边界，不授权启动 W4 迁移，不放宽后续 Blackbox 算法修订的入库合同。

1. 保留原 `scheme_id`、`base_scheme_id` 和业务 Registry ID，以真实新 exact `scheme_version` 区分运行时。
   不为包装变化新建 `_bbv2` 业务身份，不以手改 `runtime_type` 冒充迁移。
2. 固定最终包和原身份，以既有 Native 结果为基线；同一冻结输入下一次对应 Request 的 Blackbox 标准调用，
   对比 `predict_date`、`feature_date`、`target_date` 和 `predicted_direction`。同算法包装迁移无需重跑全历史，
   也不把 Native 内部模型数值扩展为 Blackbox 标准 Result 字段。
3. 只切换未来唯一 Writer；原 ID 的 prediction/run/backtest 身份、版本和来源保持不变。已发布事实永久
   insert-only，不因包装升级重算、复制、覆盖、删除或制造灰度缺口；已完成的历史物化也不回删。
4. 临时身份不得继续拥有 Writer，不建立长期历史别名；临时历史默认只读保留。物理退役必须另获精确清单授权，
   完成备份、隔离恢复和引用审查，不删除原 ID 仍依赖的共享来源。
5. Blackbox canonical 只保留 Blackbox 交付，不恢复已退役的 Native 附件临时例外；W4 原件保留在其 Native canonical/source package，不作为 Blackbox 附件。移除旧 Native 附件须形成真实新 exact；仅当算法脚本和 Metadata 原字节一致时才可复用已有验收。
   派生状态只允许受控调整版本封装，原 payload 与输入来源不变，不重跑历史；不能任意复用不匹配状态。
6. 等价、受控模拟、目标环境接管和可恢复回滚边界均通过后，才能删除旧 adapter、source/backtest runner、
   专属 Gate 和一次性测试。公共合同、安全、事务与调度测试保留。旧 Native 原件由 Git 和 immutable release 追溯。
7. W4 九套 Native 维持 Mac3 现有运行方式及所有必要 source package、输入和执行依赖，不改造或部署 ECS。

T1/T5 多目标按 target 独立两文件交付，仍每 base 一个 canonical、整体 exact version 和调度任务，
全部目标原子提交。周/月 Request 使用 Blackbox 业务桶 horizon=1；原 Registry/事实中的 6/30 由持久化边界
显式投影保留，不改写历史键或借旧 horizon 推导 Request 日期。

保留证据应能串起原身份、旧/新 exact、原始字节摘要、同输入 Request/Result 对比、Writer 接管、
受控模拟和回滚边界。模拟不冒充自然运行；迁移闭环不以等待多天自然触发为门槛。
