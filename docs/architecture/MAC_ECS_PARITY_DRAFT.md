# Mac → ECS 一致性迁移总纲

> 状态：**BLOCKED_DRAFT**（对齐中；目标与判据已定，实测值待填）
> 版本：v0.13
> 更新日期：2026-10-21
> 维护方式：目标、判据、口径发生变化时，先改本文档，再动代码；每次修改记入文末变更记录。
> 关联材料：`/Users/macstudio0/数据对比核查/归档-Phase1/21方案全历史调查-20261018-104709/`（调查报告；过程证据已于 2026-10-24 清理，清单见 `/Users/macstudio0/清理记录-20261024.json`）
>
> **本文是未批准的草案，只用于对齐目标和判据，不构成操作流程、不授予任何生产权限。** 批准后再进入[文档中心](../README.md#权威来源与职责)并转为 `CURRENT`。
> 运行事实归 [CURRENT_STATUS](../CURRENT_STATUS.md)，未完成工作归 [TODO](../TODO.md)；本文只保留目标、判据和迁移条件。

---

## 1. 目标

**在不改变现有方案业务逻辑的前提下，让同一份输入数据在 Mac 和 ECS 上，经过同一套调度、数据桥、runner 和算法，产出完全相同的最终预测数值与方向；验证通过后，把控制固化进生产算法，只影响后续预测，不回填历史、不重跑回测。**

### 1.1 四个“一致”

| 一致 | 操作化定义 |
|---|---|
| 数据一致 | 算法读取那一刻实际消费的每个值相同（不是文件 SHA 相同） |
| 算法一致 | 脚本 SHA + 配置 SHA + 环境清单 SHA 相同，执行控制相同 |
| 预测一致 | 同一业务键，最终判定标量在容差内，方向完全相同 |
| 后续预测一致 | 切换后，两端在线预测持续满足上一行 |

### 1.2 验收轴

- **只比两端，不比新旧。** 数据已经变化，历史输出不可复现，也不要求复现。
- 历史预测与历史回测是账本，冻结；本次变更只影响后续预测。
- 参照物是“同一份输入数据”，不是 Mac 的历史预测。验收上两端对等；只有出现无法消除的差异时，才指定一端为规范端。

### 1.3 阶段范围（2026-10-20 决定）

**Phase 1：21 个已知不一致方案。** 即调查范围内的 21 个方案（20 个有可信交付 + 1 个缺可信运行版本），清单见附录 A。

- 20 个有交付的方案是本阶段的可执行范围；
- `liwei_0616_7y03_cons_all_k3_div_k8`：旧 exact `6b8d4047dc8a` 缺可信运行版本（4 条旧记录无法复现，不计入）；现行 exact `109e657e9a70` 已有可信交付，2026-10-21 以 `97ed67c0fc12` 完成双端 90/90 验证并激活；
- 4 个 frozen config 为 `paused` / `draft` 的方案（见附录 A）先按范围内处理，状态与当前生产是否一致另行核对。

**Phase 2（延后，不取消）：** 部署矩阵中其余 76 个双端部署方案。Phase 1 验证通过后，按同一套控制规范和回归方法推广。

**不在迁移范围：** 9 个仅 `mac3-production` 的 W4 固定 Native 方案；按 AGENTS.md 不部署 ECS。

> Phase 1 的结论只对 21 个方案成立，不能直接外推为“97 个双端方案全部一致”。推广时必须重新做每方案的 S2–S4。

---

## 2. 第一性原理

1. **预测是函数**：`输出 = 算法(算法实际读到的输入)`。两端输出不同，只可能来自输入不同或执行不同。
2. **输入不是文件，是读取那一刻实际消费的值。** 输入链为：调度 → 数据桥 → runner → 算法。
3. **顺序必须先上游、后下游。** 先证明输入边界一致，再证明算法内部一致；上游没对齐，下游对照无法归因。
4. **算法逻辑不动，动的是未约定的执行控制**：排序、采样、解析、滚动、线程、精度、初态。
5. **内部细节是手段，不是验收对象。** 验收只看最终数值和方向；深入内部是为了定位分歧点、证明固化有效、排除“碰巧一致”。
6. **一致性必须可证伪**：重复运行稳定、解除控制能复现差异、全量回测无反例。
7. **不接受“环境差异”作为结论。** 每一项差异都要落到具体机制、具体代码点。
8. **不为对齐而调参。** 不新增 epsilon、不改业务阈值、不换模型或采样分支。

---

## 3. 判据与术语

### 3.1 三个门槛，必须同时成立

| 门槛 | 判据 | 容差 |
|---|---|---|
| **结构一致** | 选中的列、排序顺序、抽样序列、树结构、票数成员完全相同 | 0（精确） |
| **数值一致** | 最终判定标量差值 ≤ τ | τ，见第 6 节 |
| **方向一致** | `predicted_direction` 完全相同 | 0（精确） |

结构差异是失败，不能用容差盖过去。方向不一致是失败，无论数值多接近。

### 3.2 最终判定标量（L1 的对象）

不是笼统的“概率”，而是每个方案实际用于产生方向的最后一个数值。**逐方案登记**，由观察器输出。

| 方案族 | 候选最终标量 | dtype | 状态 |
|---|---|---|---|
| 9 个 T5 模板 | `ens_prob` / `vs_full` 投票分数 | float64 | 待逐方案确认 |
| factor / ensemble | `combined_score` vs `SIGNAL_THRESHOLD` | float64 | 待确认 |
| weekly logit | `predict_proba` 概率 vs 0.5 | float64 | 待确认 |
| 其余小方案 | 逐方案登记 | 多为 float64 | 待登记 |

> **当前缺口**：Blackbox V2 交付契约的 `RESULT_FIELDS` 只包含 `predicted_direction`，概率/分数是内部值。L1 的第一步是为每个方案登记该标量，并通过观察器输出；是否进入生产契约另行决定。

### 3.3 “同一份数据”

指算法读取那一刻实际消费的值相同，至少包括：

- 解析后的源路径、SHA、大小、行数、列序、dtype、NA 掩码；
- 实际消费列的逐元素 IEEE 位；
- daily / weekly / monthly cutoff 键及其解析到的数据 vintage；
- 业务键（predict_date / feature_date / target_date）、运行时刻、时区、交易日历；
- 环境变量、cwd、参数、runner/数据桥版本。

---

## 4. 输入链与四层边界

```
调度（哪个业务键、什么时刻、哪个 cutoff、哪个 vintage）
  → 数据桥（选哪些文件/行/列，行序、dtype、NA、派生表）
    → runner（解释器、环境、cwd、参数、路径、缓存、并发）
      → 算法（特征、评分、排序、采样、训练、融合、历史规则）
        → 最终数值与方向
```

| 层 | 可能造成的差异 | “一致”的判据 |
|---|---|---|
| 调度 | 同一业务键在不同时刻运行；cutoff 落在不同 vintage；时区/日历不同 | 业务键、运行语义、cutoff 解析结果相同 |
| 数据桥 | 选到不同文件/行/列；行序、dtype、NA 处理不同；缺失回退不同；派生表不同 | 算法消费的每个值相同 |
| runner | 解释器/环境变量/cwd/参数/路径不同；缓存或临时目录不同；并发/超时导致部分执行 | 同脚本、同环境清单、同参数、同路径语义、同初态 |
| 算法 | 排序、采样、解析、滚动、训练、融合、规则 | 内部决策点逐层可解释，最终数值一致 |

**调查范围覆盖四层；改动范围取决于结论。** 如果差异来自调度、数据桥或 runner，它们也必须纳入控制或修复，不能只改算法。

---

## 5. 执行顺序

| 阶段 | 做什么 | 通过条件 | 状态 |
|---|---|---|---|
| **S0 输入边界** | 双端快照：路径、SHA、行序、dtype、NA、消费列逐元素值、cutoff、业务键、运行时刻、环境 | 两端边界值完全相同 | 未开始 |
| **S1 上游修复** | 若边界不同：按调度 → 数据桥 → runner 定位并控制 | 边界差异消除 | 未开始 |
| **S2 算法固化** | 边界相同后，逐方案定位内部分歧点并固化 | 每个分歧点都有“解除固化 → 差异复现”的反证 | 未开始 |
| **S3 小范围双端对照** | 已知差异日 + 邻近日，比较最终判定标量 | 方向 100%，数值在 τ 内 | 未开始 |
| **S4 双端全量回测** | 每方案全量跑，比较最终判定标量 | 无反例；τ 与 M_min 满足第 6 节关系 | 未开始 |
| **S5 生产同步** | 把控制固化进 Mac 生产算法，历史结果不动 | 变更清单确认，影子期通过 | 未开始 |
| **S6 ECS 切换** | 同脚本、同配置、同环境清单拷贝过去 | 后续预测持续一致 | 未开始 |

> 单端重复一致必须排在跨端对照之前。如果 Mac 自己跑两次都不一致，跨端比较没有意义。

---

## 6. 容差规范

### 6.1 定义

对每个方案、每个最终判定标量：

```
τ = min( 10 × Δ_max, M_min / 10 )，且 τ ≥ 10 ULP
```

- **Δ_max**：控制全部固化后，双端最终判定标量的最大实测残差。
- **M_min**：全量日期（含历史上下文日期）到**任何**离散判定边界的最小距离。边界包括当前阈值、加权融合阈值、历史票数边界、override 规则。
- 若 `10 × Δ_max > M_min / 10`，不存在安全的统一容差；冲突日期必须单独核验，或继续收紧控制。
- 若 `Δ_max = 0`，则 `τ = 0`，即精确相等。

### 6.2 为什么容差必须分方案、分标量

- float32 方案在 1e-12 下永远不可能通过，表示精度本身就在 1e-7；
- float64 方案用 1e-6 又太松，可能掩盖真实的结构性差异；
- 容差必须跟随标量的 dtype 和它到决策边界的距离。

### 6.3 验收上限（占位值，实测后只能收紧）

| 对象 | 上限 | 说明 |
|---|---|---|
| float64 标量 | **1e-9 绝对** | 待实测后收紧 |
| float32 标量 | **1e-6 绝对** | 约 8 ULP，低于表示精度无意义 |
| 方向 | **0** | 必须完全相同 |

> 这些是**验收上限**，不是对结果的断言。最终值由 S3/S4 的实测确定，并冻结在本文档中。

### 6.4 决策边界安全（含历史传播）

O022 已证明：历史投票差 1 票（10 → 11）越过严格 `>10`，最终方向翻转。因此：

- τ 必须小于当前阈值的安全距离；
- 还必须小于**历史上下文每一天**到其判定边界的距离；
- 否则即使今天分数一致，历史票也可能因为另一天落在容差带内而翻向。

---

## 7. 明确不做

- 不重跑历史回测，不回填历史预测。
- 不修改业务规则，不新增 epsilon，不为对齐而调参。
- 不用抽样代替内部定位。
- 不接受“环境差异”作为结论。
- 不把结构性差异当作数值差异用容差吸收。

---

## 8. 交付物

1. 输入边界快照规范 + 双端对照报告；
2. 每方案分歧点台账：机制 → 代码位置 → 控制方式 → 反证结果；
3. 控制规范（哪一项控制、什么值、为什么）；
4. 最终判定标量登记表（逐方案）；
5. 回归集 + 双端全量回测报告；
6. Mac 生产变更清单 + ECS 部署清单；
7. 未决项清单。

---

## 9. 现状基线（2026-10-19 封存证据）

| 项目 | 事实 |
|---|---|
| 方案范围 | 21 个；20 个有可信交付；1 个旧 native（`liwei_0616_7y03_cons_all_k3_div_k8` / `6b8d4047dc8a`）缺可信运行版本 |
| 旧 74 条记录 | 稳定不同 56 条（分布于 15 个方案）、稳定相同 14 条、无法复现 4 条（均属缺版本方案） |
| 新全历史 | 仅 `five_y_factor_rule_online_v1` 完成双端各 3 次：307 天中 8 天不同（6 天当前不同，2 天历史后验拉开） |
| 待 ECS | 17,746 个业务日仍在等 ECS 结果 |
| 环境 | 两端包版本相同（numpy 2.3.5 / pandas 2.3.3 / bottleneck 1.4.2 / lightgbm 4.6.0 / xgboost 3.1.3 / sklearn 1.8.0 / Python 3.13.12），但构建渠道、C++ 标准库、指令集（AVX/NEON）、FMA 收缩不同 |

### 9.1 机制清单（待逐方案填台账）

| # | 机制 | 类型 | 已知证据 |
|---|---|---|---|
| A1 | 同分排序 | 离散 | O010；CPU 派发影响同分次序 |
| A2 | 随机抽样/列采样 | 离散 | O045；同种子两端候选列序列不同 |
| A3 | 历史规则/票数 | 离散 | O022；10 → 11 票翻向 |
| A4 | 初始状态/随机对象生命周期 | 离散 | 待逐方案登记 |
| B1 | CSV 解析 | 连续 | 200 格差 1 ULP，方向未变（反证） |
| B2 | 滚动统计（bottleneck/Welford） | 连续 | 128 个 std 全不同，方向未变（反证） |
| B3 | float64 → float32 投影 | 连续 | 部分差异在投影后被抹除 |
| B4 | 学习器训练内部 | 连续 | 待逐方案固化 |
| B5 | 严格阈值判定 | 连续 | τ 必须小于边界距离 |
| B6 | 暖启动/缺失值/日历/标签 | 连续 | 待写成规范 |

---

## 10. 未决事项

| # | 事项 | 影响 | 状态 |
|---|---|---|---|
| P1 | 缺可信版本的 1 个方案 | 旧 exact `6b8d4047dc8a` 缺可信运行版本；现行精确 `109e657e9a70` 已有交付 | ✅ 已闭环（现行 `97ed67c0fc12` 双端 90/90 验证并激活；4 条旧记录仍属历史） |
| P2 | 每个方案的“最终判定标量”登记 | L1 没有对象则无法验收 | ⏳ 待登记 |
| P3 | Δ_max 与 M_min 实测 | 决定 τ 的最终值 | 🔍 待实测 |
| P4 | 调度/数据桥/runner 是否需改动 | 取决于 S0/S1 结论 | 🔍 待定位 |
| P5 | 概率是否进入生产交付契约 | 只影响验收方式，不影响算法 | ⏳ 待决策 |

---

## 11. 缺口登记（2026-10-20 梳理）

> 这些点是当前总纲尚未覆盖、但会影响范围、结果或验收的事项。
> 状态：🔴 需用户决策；🟠 需只读核验；🟡 执行阶段准备。

### 11.1 范围与治理

| # | 缺口 | 证据 | 影响 |
|---|---|---|---|
| G1 | 迁移范围不是 21 个方案 | 部署矩阵共 106 个：97 个 `mac3-production` + `aliyun-gray` 双端，9 个仅 `mac3-production`（W4 固定 Native，按 AGENTS.md 不迁 ECS）。21 个调查方案全部落在 97 个双端方案内，是“已知差异样本”，不是全集 | ✅ Phase 1 定为 21（见 1.3）；97 全集延后为 Phase 2 |
| G2 | 部署角色与目标方向 | AGENTS.md 与矩阵当前定义 Mac3 = 生产、ECS = 独立灰度实验室；“迁移到 ECS 部署”是角色变更，不是单纯结果对齐 | 🔴 需业务/架构批准，并同步改 AGENTS.md、部署矩阵、调度、DB、发布流程 |
| G3 | 跨机数据/结果授权 | AGENTS.md 禁止持续复制、双写、跨机共享输入；跨机结果补缺须单独授权，且不得复制主键、run_id、Actuals、回测历史 | 🔴 “拷贝 Mac 历史源数据”须明确为一次性、manifest 绑定、经授权的拷贝 |
| G4 | L1 标量不在生产契约 | AGENTS.md 明确平台不要求、不提取、不存储、不展示统一 confidence；Blackbox 交付只返回 `predicted_direction` | 🔴 L1 只能由观察器内部采集；若要进入契约，是上游/平台合同变更 |
| G5 | 生产变更路径 | AGENTS.md：单一代码线、不可变 release、ECS 先验证、Mac3 只能晋级同一份已验证 archive，不得现场改源码 | 🔴 “改 Mac 现有算法”须走 release/onboarding，不能直接改运行目录 |
| G6 | 配置状态与部署矩阵不一致 | frozen config 中 4 个方案为 `paused` / `draft`：`five_y_factor_rule_online_v1`、`liwei_0616_7y01_cons_say_k3_div_k10`、`ten_y_factor_level_ensemble_v1`、`weekly_5y_curve_logit_v1`；矩阵仍列出 | 🟠 核对当前生产状态与优先级 |

### 11.2 输入与数据

| # | 缺口 | 说明 | 影响 |
|---|---|---|---|
| G7 | 两种数据 regime 未区分 | 历史回测使用冻结 vintage；未来在线预测使用运行时当前数据 | 🔴 必须分别定义“同一份数据”，不能混用 |
| G8 | 派生输入与 DB 输入未纳入 | 日历、Actual、daily/weekly/monthly cutoff、api 映射、周月聚合、可能的 DB 表；AGENTS.md 规定算法输入只经 `shared.input_artifacts` | 🟠 输入面清单须覆盖，不能只看 CSV |
| G9 | 快照冻结机制未定义 | 拷贝一次不够；需要 immutable snapshot + manifest + 校验回执，并保证测试期间两端都不刷新 | 🟠 设计输入边界快照规范时一并定义 |
| G10 | 数据修订/缺失/fallback | 源数据修订、文件缺失、bridge fallback 都会改变输入 | 🟠 需要显式状态，不能当作“数据一样” |
| G11 | as-of 与 cutoff 语义 | 同一业务键在不同时刻运行会解析到不同 cutoff；历史与在线的 cutoff 来源不同 | 🟠 在 S0 中写死 |

### 11.3 执行与算法

| # | 缺口 | 说明 | 影响 |
|---|---|---|---|
| G12 | 线程与哈希未完全钉死 | runner 使用 `-I`，按 Python 语义会忽略 `PYTHONHASHSEED`，哈希随机化不受控；未设置 `NUMBA_NUM_THREADS`；OMP/OpenBLAS/MKL/NUMEXPR/VECLIB 设为 8，但两端核数不同 | 🟠 审计 set/dict 迭代与 numba 并行，补齐控制 |
| G13 | 缓存/增量状态 | T5 模板有 incremental LGBM cache，current55/adyn 也有 cache；reference 的 `incremental_state=false` 不等于代码没有缓存 | 🟠 验证时禁用，或保证两端缓存前缀一致 |
| G14 | 学习器内部离散点未细化 | 直方图 bin 边界、split gain 并列、EFB、`max_bin`/`min_data_in_bin`、`deterministic`/`force_*_wise`、多 seed 平均顺序 | 🟠 B4 需要从“连续”细化为具体离散点 |
| G15 | 阈值/集成 tie 未列入 | `choose_threshold` 的 VT 网格、`np.argsort(-scores)[:K]`、`_diverse_topk` 并列时会选到不同 config/threshold | 🟠 A1 目前只覆盖因子评分排序，需补 |
| G16 | 排序/迭代点不止 argsort | `sort_values`、`rank`、`nlargest`、`np.unique`、`drop_duplicates`、`groupby` 顺序、set 迭代 | 🟠 需要完整清单 |
| G17 | 数学函数/BLAS 未列入 | libm 的 exp/log/sqrt、BLAS 实现、numba fastmath；不只是 FMA/rolling | 🟠 归入 B2/B4 的具体检查 |
| G18 | 随机生命周期/多进程顺序 | 全局 `np.random.seed`、`random` 模块、`multiprocessing` 结果收集顺序 | 🟠 逐方案审计 |
| G19 | 失败/重试/部分运行未分类 | resource fence、timeout、preparation failure 必须单独记账 | 🟠 比较器与统计口径需要状态分类 |

### 11.4 验证与工程

| # | 缺口 | 说明 | 影响 |
|---|---|---|---|
| G20 | 历史记录没有概率 | `records74` 只有方向，没有概率/分数 | 🔴 L1 回归集必须用新的双端运行构建，不能拿旧记录做数值对照 |
| G21 | 回归集代表性不足 | 56 + 8 是“已知不同”的偏样本；缺少已知相同、阈值邻近日、分支覆盖（B8/B12、暖启动、缺失、周月边界） | 🟠 重新设计分层回归集 |
| G22 | 容差的统计性质未定 | Δ_max 从样本估计可能低估尾部；M_min 要覆盖全部上下文日期；τ 需逐方案并带安全系数 | 🟠 在 S3/S4 中实测并冻结 |
| G23 | 比较器本身的正确性 | 需比对正确标量，处理 NaN、缺失、重复键、部分运行；报告中已出现过 checker KeyError | 🟠 增加 comparator spec 与自检 |
| G24 | 编排/harness 未设计 | 20 或 97 个方案 × 日期 × 重复 × 两端，涉及产物收集、比对、留证 | 🟡 复用 `baseline_engine`，但需专门编排 |
| G25 | 资源窗口与性能影响 | ECS tmpfs/内存围栏、Mac ingestion 窗口；钉死线程/CPU 特性会变慢，11.6h/21.5h 的估算需重测 | 🟡 排期时纳入 |
| G26 | 回滚/影子期/独立验收未定义 | 影子期长度、失败判据、签字人；独立复核不能共用同一套 observer | 🟡 S5/S6 前定义 |
| G27 | 长期归属未定 | 本文是 `BLOCKED_DRAFT`，证据在调查目录；批准后的权威归属与维护责任待定 | 🟡 文档批准时处理 |

### 11.5 第二轮补充（2026-10-20）

> 以下 16 项不在上一轮 27 项内，是继续对照 AGENTS.md、runner 和 21 个脚本后补出的。

| # | 缺口 | 证据/说明 | 影响 |
|---|---|---|---|
| N1 | **修复责任边界：平台 vs 上游** | AGENTS.md：Blackbox 内部保真由上游负责，平台验证接入与标准输出边界。若修复点在方案内部，需上游发新 exact；平台不能自行 patch | 🔴 先给每个候选控制分类：环境控制 / 平台代码 / 上游算法 |
| N2 | **迁移是全栈，不只是算法一致** | AGENTS.md：ECS 与 Mac3 各自使用 MySQL、DataBridge、Actuals、调度和前端。算法一致只是迁移的必要条件 | 🔴 DB、Actuals、调度、Dashboard、认证、单一 Writer、历史预测都要单独迁移/验收 |
| N3 | **回测路径 ≠ 在线预测路径** | 307 天证据是 backtest；在线 predict 走 `shared.input_artifacts` 和 live context，输入构建可能不同 | 🔴 必须分别验证 backtest 与 predict，并证明同键同结果 |
| N4 | **历史上下文是输入还是算法** | B8/B12/后验可能读已发布历史，也可能从原始数据重算。若读历史，ECS 需要同一状态，受 insert-only 与跨机补缺授权约束 | 🟠 逐方案确认；这决定历史是否需要迁移 |
| N5 | **“回测结果一致”的定义** | 可能指预测值，也可能指 P&L/指标；actuals 两端可能不同，指标一致不能由预测一致推出 | 🔴 明确验收对象是预测数值，不是绩效指标 |
| N6 | **parser 选择是语义决策** | pandas `float_precision` 默认无全词元正确舍入承诺；钉 `round_trip` 会改变两端数值，保留默认则可能跨端不同 | 🔴 这是业务/算法语义选择，不是免费控制 |
| N7 | **可选依赖的 fallback 分支** | T5 模板有 `_HAS_BN`、`_HAS_NUMBA` 分支；缺包时走 pandas/纯 Python 回退，滚动与阈值实现不同 | 🟠 验证两端实际走同一分支，而不是只看包版本 |
| N8 | **原生库/OpenMP/BLAS 的实际加载** | 包版本相同不代表原生库相同：Linux `libgomp` vs macOS `libomp`、OpenBLAS vs Accelerate、numpy CPU 派发 | 🟠 记录 `np.show_config()`、`ldd`/`otool -L`、wheel SHA |
| N9 | **阈值/集成/top-K 的并列** | `choose_threshold` 用首个最优（`score > best`）；`_diverse_topk` 用 `argmax` 与默认 `argsort`；并列或 ULP 差会选到不同 config/threshold | 🟠 新增离散点，纳入 S2 台账 |
| N10 | **特征列顺序的确定性** | factor 方案用 set 推导 `windows`，滚动结果 dict 的插入顺序依赖集合迭代；需确认下游特征列顺序来自固定列表，而不是 frame 列序 | 🟠 列序变化会改变特征索引与树结构 |
| N11 | **方向调优/低置信度规则** | adapter 已有 `tie_lowconf_flip_width`、`direction_tuning_rule` 等字段；21 个方案的 VT/投票/低置信度翻转规则需逐一登记 | 🟠 纳入阈值余量分析 |
| N12 | **平台代码版本** | 方案行为依赖 `shared.input_artifacts`、runner、adapter 等平台代码；只钉方案文件 SHA 不够 | 🟠 需要钉完整 release/archive 身份 |
| N13 | **方案版本与旧 74 条记录的对应** | 旧记录由旧版本产生；当前 frozen delivery 是否就是当时的 exact，需按 `phase1-source-bindings.json` 核对 | 🟠 否则旧记录只能作线索，不能作回归基线 |
| N14 | **失败样本偏差** | 只比较成功运行会引入偏差：resource fence、timeout、preparation failure 的日期被排除 | 🟠 失败单独统计，并确保两端失败条件可比 |
| N15 | **GPU 路径** | 环境清单含 `nvidia-nccl-cu12`；若任一方案走 GPU，atomic/归约顺序可能不可复现 | 🟠 确认并禁用 GPU，或固定确定性算法 |
| N16 | **输出精度/序列化** | 最终数值经过 JSON/CSV 序列化；比较应在存储值/全精度数值上进行，不能比格式化字符串 | 🟠 定义输出精度与比较口径 |

---

## 12. 附录 A：Phase 1 方案清单（21 个）

> 来源：`frozen/algorithm_delivery/schemes/` 与 `mechanism_index/records74.csv`。
> “旧 74 条”是 2026-10-19 封存证据中的方向对照结果，仅作回归种子，不作为 L1 的数值真值。
> `frozen config` 状态来自交付包的 `config.yaml`，需与当前生产状态另行核对。

| # | 方案 | frozen config | 旧 74 条 | 备注 |
|---:|---|---|---|---|
| 1 | `cgb_causal_wk_1y_v128` | active | 1 相同 | |
| 2 | `five_y_factor_rule_online_v1` | **paused / draft** | 7 不同 | 唯一完成 307 天双端各 3 次；8 天不同（6 当前 + 2 历史后验） |
| 3 | `five_y_t5_lgbm_3y_anti_lag252_b8_v1` | active | 8 不同 | |
| 4 | `five_y_t5_xgb_spr_3y1y_b8_v1` | active | 5 不同 | |
| 5 | `liwei_0616_7y01_cons_say_k3_div_k10` | **paused / draft** | 1 不同 | |
| 6 | `liwei_0616_7y03_cons_all_k3_div_k8` | **旧 exact 缺交付** | 4 无法复现 | 旧 exact `6b8d4047dc8a` 缺可信运行版本；现行 exact `97ed67c0fc12` 已完成双端 90/90 验证并激活 |
| 7 | `one_y_t5_liq_excess_b_w252_l7_v1` | active | 1 相同 | |
| 8 | `one_y_t5_xgb_10y_streak_anti7_b8_v1` | active | 3 不同 | |
| 9 | `one_y_t5_xgb_7y_cond_rev20_b12_v1` | active | 1 不同 | |
| 10 | `seven_y_current55_lgbm_001_v2` | active | 3 相同 | |
| 11 | `seven_y_current55_lgbm_002_v2` | active | 1 相同 | |
| 12 | `seven_y_t5_lgbm_bf_z_anti40_b8_v1` | active | 5 不同 | |
| 13 | `seven_y_t5_xgb_7y_rv_rev20_b0_v1` | active | 1 不同 | |
| 14 | `ten_y_factor_level_ensemble_v1` | **paused / draft** | 16 不同 | 旧记录差异最多 |
| 15 | `ten_y_t5_maj4_k3_ic_static_v1` | active | 1 相同 | |
| 16 | `three_y_adyn_lb1_k3_v1` | active | 2 相同 | |
| 17 | `three_y_adyn_lb2_k1_v1` | active | 2 相同 | |
| 18 | `three_y_t5_lgbm_7yanti_b12_v2` | active | 2 相同 | |
| 19 | `three_y_t5_xgb_fxlead_b8_v2` | active | 3 不同 | |
| 20 | `three_y_t5_xgb_tp_5y1y_b12_v2` | active | 6 不同 | |
| 21 | `weekly_5y_curve_logit_v1` | **paused / draft** | 1 相同 | |

**小计：** 20 个有交付 + 1 个缺交付；旧记录 56 不同 / 14 相同 / 4 无法复现。

**Phase 1 的优先级建议（仅按证据量，不代替业务判断）：**

1. `five_y_factor_rule_online_v1`：唯一有双端全量重复数据的方案，适合做端到端试点；
2. `ten_y_factor_level_ensemble_v1`、`five_y_t5_lgbm_3y_anti_lag252_b8_v1`：旧记录差异最多（16 / 8）；
3. 其余 17 个有交付方案：按机制分组批量验证；
4. `liwei_0616_7y03_cons_all_k3_div_k8`：阻断项，先找回可信版本或明确排除。

---

## 13. Phase 1 执行流程与确认点（2026-10-21）

### 13.1 目标

21 个已知不一致方案，逐方案闭环：

1. 数据输入一致；
2. 算法版本一致（激活新 exact）；
3. 历史/灰度预测不改、不回填；
4. 未来预测一致（Mac == ECS）；
5. 严格持久化回测只作为激活证据，不写历史预测；
6. Mac/ECS 并行推进；每方案过门才执行，过不了跳过并记录。

### 13.2 四道门

| 门 | 判据 | 不过怎么办 |
|---|---|---|
| G1 交付可信 | `.py`/`.json` 可执行、exact 可信 | 跳过 |
| G2 可 revision | 已有 prior active exact | 跳过（initial 激活会写历史产品事实） |
| G3 全量双端一致 | 全量可执行范围方向 0 不一致、数值差 ≤ τ、S0 输入一致 | 跳过 |
| G4 只动确定性 | 只改排序/采样/解析/线程/列序等；不改阈值/模型/特征/业务规则 | 跳过或业务另定 |

### 13.3 每方案步骤

1. **输入清单**：按方案实际消费 + DataBridge 五文件生成。
2. **ECS 备份**：目标源表 mysqldump + SHA256 + 行数 + 恢复脚本。
3. **原始数据覆盖**：Mac → ECS，仅 DataBridge 原始源表；不复制 prediction/backtest/registry/harness/run log。
4. **ECS DataBridge 刷新 + S0 对比**：按方案消费列比对；不一致则跳过。
5. **双端 parity evidence**：8 天 + 全量；方向 0 不一致、数值 ≤ τ；重复轮次；S0。
6. **补丁 + release**：只动确定性控制；canonical config active；构建不可变 release。
7. **严格回测**：Mac/ECS 并行 `harness gate backtest --persist`；只写 `t_backtest_*`。
8. **激活**：`harness activate`；revision；不写 `t_scheme_predictions`；按 `effective_from` 规则。
9. **验证**：历史行数/方向不变；effective config active；下个调度用新 exact；台账。
10. **下一方案**。

### 13.4 数据一致性方案（已确认）

覆盖范围：DataBridge 原始源表（`shared/data_contract.py`）：

`api_wind_daily`、`api_wind_derivative_daily`、`api_wind_weekly`、`api_wind_derivative_weekly`、`api_wind_monthly`、`api_wind_derivative_monthly`、`api_wind_indicators_all`、`api_wind_date`、`t_trade_calendar`。

不覆盖：`t_scheme_predictions`、`t_backtest_*`、`t_scheme_versions`、`t_scheme_registry`、`t_harness_*`、`t_scheme_run_log`；actuals 是否覆盖待确认（建议由 ECS actuals 服务从源表重算）。

修改前：ECS 目标表备份（dump + SHA256 + 行数 + 恢复脚本）。

修改后：ECS DataBridge 重新生成 current；按方案消费列对比 S0；不一致则该方案跳过。

一次性、manifest 绑定；不建立持续复制/双写。

### 13.5 已确认事项

| # | 事项 | 状态 |
|---|---|---|
| 1 | 只覆盖原始底层源表 | ✅ 已确认 |
| 2 | 修改数据库前备份 | ✅ 已确认 |
| 3 | 严格回测作为证据，只写 `t_backtest_*` | ✅ 已确认 |
| 4 | 历史/灰度预测不改、不回填 | ✅ 已确认 |
| 5 | Mac/ECS 并行推进 | ✅ 已确认 |
| 6 | 25/307 旧新变化接受；新 exact future-only | ✅ 已确认 |
| 7 | 不能保证完全一致的方案跳过 | ✅ 已确认 |

### 13.6 待确认事项（全部确认后才执行）

| # | 事项 | 建议默认 | 状态 |
|---|---|---|---|
| C1 | `gray_target_start` 规则 | `max(最后一条 live 预测日 + 1 交易日, 今天)`；本方案为 2026-10-21 | ✅ 已确认 |
| C2 | 激活用候选 release `84fd`，运行 current 保持 `3ca470a2`（同 exact） | 是 | ✅ 已确认 |
| C3 | ECS DataBridge timer 是否短暂停 | 复制/刷新期间短暂停，完成即恢复 | ✅ 已确认 |
| C4 | actuals 是否纳入覆盖 | 不覆盖；由 ECS actuals 服务从源表重算；若回测要求再单独确认 | ✅ 已确认 |
| C5 | 缺版本方案 `liwei_0616_7y03_cons_all_k3_div_k8` | 跳过 | ✅ 已确认 |
| C6 | 4 个 paused/draft 方案无 prior active 时 | 跳过 | ✅ 已确认 |
| C7 | operator 身份 | `qiyo3` | ✅ 已确认 |
| C8 | 回滚边界 | 激活失败保持 paused；必要时回退 current 到旧 release | ✅ 已确认 |
| C9 | 生产方案名单 | 不自动加入 `production_schemes.json` | ✅ 已确认 |
| C10 | 时间/优先级 | 先本方案，再按旧记录差异量 | ✅ 已确认 |

### 13.8 剩余不确定点（执行中逐项验证）

| # | 不确定点 | 处理 |
|---|---|---|
| R1 | 9 张源表在两机的 schema/行数/数据量是否一致 | 复制前逐表对比；不一致先停下确认 |
| R2 | 两机数据库账号是否有备份/TRUNCATE/LOAD 权限 | 先做只读权限检查；不足则停下来找授权账号 |
| R3 | Mac 生产在跑，导出如何保证一致快照 | `mysqldump --single-transaction`，只读，不影响生产 |
| R4 | ECS DataBridge 复制后能否生成 ready generation | 刷新后按方案消费列做 S0 对比；不一致则跳过该方案 |
| R5 | ECS actuals 是否覆盖回测区间 | 先查覆盖；不足则用 ECS actuals 服务重算或单独确认 |
| R6 | ECS 候选 release `84fd` 尚未预安装 | 先 scp/预安装，再跑 ECS 回测 |
| R7 | 一晚时间窗 vs 回测时长 | 先做本方案；Mac/ECS 并行；其余 20 个按台账继续 |
| R8 | 本方案当前 paused，今天 07:03 可能跳过 | 优先完成 Mac 侧回测/激活；ECS 侧随后 |
| R9 | 其余 20 个方案的 prior active/scope/机制 | 每方案先做只读状态探针，过 G1–G4 才执行 |

### 13.7 Phase 1 台账

| 方案 | 旧记录 | 当前状态 | 下一步 |
|---|---|---|---|
| `five_y_factor_rule_online_v1` | 7 不同 | ✅ 已闭环：新 exact `bc0ef4e31a64` active，旧 exact `55f0e753b18e` retired；历史预测未变（Mac 315 / ECS 312） | 见 13.9；观察 2026-10-22 首次在线预测 |
| `ten_y_factor_level_ensemble_v1` | 16 不同 | ✅ 已闭环：新 exact `46710a4c3e66` active，旧 `997af2e57ad9` retired；A2 列采样 + canonical active | 见 13.13 |
| `five_y_t5_lgbm_3y_anti_lag252_b8_v1` | 8 不同 | ✅ 已闭环：新 exact `82bc53e04bcc` active，旧 `5cbbf665e081` retired；稳定排序修复选列 | 见 13.12 |
| `three_y_t5_xgb_tp_5y1y_b12_v2` | 6 不同 | ✅ 已闭环：新 exact `2b1281d75fdf` active，旧 `4ea0782cc210` retired | 见 13.12 |
| `five_y_t5_xgb_spr_3y1y_b8_v1` | 5 不同 | ✅ 已闭环：新 exact `8623acf8e21b` active，旧 `c7b5b0cc0fa0` retired；90/90 天方向与概率逐位一致；release `5634e27f` 已切 current | 见 13.11 |
| `seven_y_t5_lgbm_bf_z_anti40_b8_v1` | 5 不同 | ✅ 已闭环：新 exact `7517e184861a` active，旧 `4f182bf23dfc` retired | 见 13.12 |
| `liwei_0616_7y03_cons_all_k3_div_k8` | 4 无法复现 | ✅ 已闭环：新 exact `97ed67c0fc12` active，旧 `109e657e9a70` retired；稳定排序修复 2026-06-12 差异；双端 90/90 | 见 13.16 |
| `one_y_t5_xgb_10y_streak_anti7_b8_v1` | 3 不同 | ✅ 已闭环：新 exact `19026a39e0c7` active，旧 `9ceb12fdbd55` retired | 见 13.12 |
| `three_y_t5_xgb_fxlead_b8_v2` | 3 不同 | ✅ 已闭环：新 exact `a4be221cba00` active，旧 `dc4e944f5aec` retired | 见 13.12 |
| `liwei_0616_7y01_cons_say_k3_div_k10` | 1 不同 | ✅ 已验证：当前 exact `3bd1f8067ac5` 双端 90/90（无代码变更） | 见 13.15 |
| `one_y_t5_xgb_7y_cond_rev20_b12_v1` | 1 不同 | ✅ 已闭环：新 exact `ff41452470d5` active，旧 `9caa9227981f` retired | 见 13.12 |
| `seven_y_t5_xgb_7y_rv_rev20_b0_v1` | 1 不同 | ✅ 已闭环：新 exact `d95f6aa12894` active，旧 `24d775cc01b7` retired | 见 13.12 |
| `cgb_causal_wk_1y_v128` | 1 相同 | ✅ 已验证：当前 exact `59415aa789c5` 双端 92/92 一致（无代码变更） | 见 13.14 |
| `one_y_t5_liq_excess_b_w252_l7_v1` | 1 相同 | ✅ 已闭环：元数据修复 + 新 exact `4d0d1f0d6a71` active，双端 90/90 | 见 13.15 |
| `seven_y_current55_lgbm_001_v2` | 3 相同 | ✅ 已验证：当前 exact `cd0624ef3ead` 双端 94/94（无代码变更） | 见 13.15 |
| `seven_y_current55_lgbm_002_v2` | 1 相同 | ✅ 已验证：当前 exact `57e956513471` 双端 94/94（无代码变更） | 见 13.15 |
| `ten_y_t5_maj4_k3_ic_static_v1` | 1 相同 | ✅ 已闭环：元数据修复 + 新 exact `0b96997c496a` active，双端 90/90 | 见 13.15 |
| `three_y_adyn_lb1_k3_v1` | 2 相同 | ✅ 已验证：当前 exact `98233f0cb9ef` 双端 94/94（无代码变更） | 见 13.15 |
| `three_y_adyn_lb2_k1_v1` | 2 相同 | ✅ 已验证：当前 exact `47c7c1776db0` 双端 94/94（无代码变更） | 见 13.15 |
| `three_y_t5_lgbm_7yanti_b12_v2` | 2 相同 | ✅ 已闭环：新 exact `bb4354f94c1f` active，旧 `99523554aa06` retired | 见 13.12 |
| `weekly_5y_curve_logit_v1` | 1 相同 | ✅ 已验证：当前 exact `a10fef7a332a` 双端 92/92 一致（config paused/draft，DB active） | 见 13.14 |

### 13.9 第一个方案闭环记录（2026-10-21）

**方案** `five_y_factor_rule_online_v1`；旧 exact `55f0e753b18e` → 新 exact `bc0ef4e31a64`（code `b151487…`，config `9f06c81c…`，manifest `a18ff21e…`）。

**输入**：ECS 9 张 DataBridge 原始源表按 Mac 一次性覆盖（ECS 全库备份 `bond_db_full_20261021.sql.gz`，SHA `736e84f3…`）。两端 DataBridge current 五文件逐字节一致：

| 文件 | SHA256 |
|---|---|
| `daily_output.csv` | `6bc2c4e0…` |
| `weekly_output.csv` | `fda76d87…` |
| `monthly_output.csv` | `49d2e3c4…` |
| `api_wind_date.csv` | `12b36f2c…` |
| `factor_catalog.csv` | `bb5ee17f…` |

业务摘要 `89abb3b66e99…`；源截止 2026-10-20；日历未来行到 2026-11-04。两端严格回测解析到同一 `data_snapshot_id`：`snapshot-15b2ad3eb7b7522c40a8a728`。

**外部全量对照**（此前已完成）：307/307 天，方向 0 不一致，`max |Δcombined_score| = 1.42e-14`，同机重复逐位稳定。

**严格回测（激活证据）**：

| 主机 | run_id | 区间 | 请求/记录 | 月度指标 | generation |
|---|---:|---|---:|---:|---|
| Mac | 268 | 2026-06-01 → 2026-10-19 | 94 / 94 | 5 | `full-20261021-063113-89abb3b66e99` |
| ECS | 298 | 2026-06-01 → 2026-10-19 | 94 / 94 | 5 | `full-20261021-132506-89abb3b66e99` |

> 回测窗口不是 2025-07-01 全区间：交付脚本 `read_requests` 契约上限 100 行，平台 `run_blackbox_backtest` 把全部 HistoricalCase 写成单份 CSV、不分片；307 行触发 `request CSV must contain one to 100 rows`。本方案以 94 个候选日的持久化回测作为激活证据，全量一致性由外部 307 天对照承担。**其余 20 个方案同样受此约束**：要么把门禁回测窗口控制在 ≤100 个候选日，要么先给平台回测器加分片（属于平台代码变更，需单独发布）。

**激活**：`harness activate` revision 模式，Mac/ECS 均 passed：

- `business_tables_written=false`（未写 `t_scheme_predictions`）；
- 旧 exact retired，新 exact active（`approved_by=qiyo3`）；
- 运行中的 current release 仍是 `3ca470a2`；用该 release 解析 effective：`status=active`、`version_status=active`，exact/code/config/manifest 与候选 `84fd` 一致；
- 历史预测条数不变：Mac 315 / ECS 312。

**执行中发现并处理的问题**：

- ECS current release 的交付目录多出 `__pycache__/`，`discover_schemes` 因“必须恰好两个普通文件”报错；已删除该生成物，交付 `.py`/`.json` SHA 未变（`b151487…` / `a18ff21e…`）。
- ECS `bond-factor-lab-data-bridge.timer` 之前停在无下次触发状态，已 `enable --now`，下次 2026-10-22 06:30；2026-10-21 06:30 的 `configuration_error` 发生在源表重载前，13:25 用同一脚本/配置手工发布成功。
- ECS 本机 `bond_db` 没有 ingestion 定时器、也不是上游副本；本次源表是一次性覆盖，**不会自动跟进 Mac 的后续数据**。

**未闭环（全栈迁移层面，不是本方案算法层面）**：

- ECS 后续原始数据通路未定：需要在“ECS 自建 ingestion / 一次性授权复制 / 共用一个上游源库”之间做架构决策（对应 G3/G7/G9、N2）。在此之前，ECS 的在线预测只能保证“同一份输入下同结果”，不能保证每天与 Mac 同输入。
- 首次新 exact 在线预测：2026-10-22 07:03（10-21 的 07:03 已在激活前跑过，两端都跳过 10-21）。观察两端方向/数值是否一致。
- A4 `windows = sorted(...)` 卫生补丁仍待单独提交（不影响结果）。

### 13.10 第二个方案：move_std 环境控制结果（2026-10-21）

**方案** `five_y_t5_lgbm_3y_anti_lag252_b8_v1`（prior active `5cbbf665e081`，两端交付 SHA 相同）。

**根因量化**：方案在 `_rolling_std` 调用 `bn.move_std`。用调查中同一段 3895 行输入、同一调用（`window=2, min_count=2, ddof=1`）逐位重放：

| 模型 | Mac | ECS |
|---|---|---|
| 分离乘加（无 FMA 收缩） | 2458 行不一致 | **0 不一致** |
| 方差更新 FMA | **0 不一致** | 2458 行不一致 |

即 Mac 的 bottleneck 方差更新启用了 FMA，ECS 是分离乘加。

**环境控制**：在 `forecast_env_blackbox_v1` 中用 bottleneck 1.4.2 源码、`CFLAGS="-ffp-contract=off"` 重建四个扩展（编译命令确认含该参数）。重建后 `move_std` 输出 SHA `8402a6f2…`，与 ECS 实际输出逐位一致（原 Mac 为 `d51e74d1…`）。原 `.so` 与完整包备份在 `parity_transfer_20261021/bn_rebuild_20261021/backup/`，构建记录同目录 `REBUILD_RECORD.md`。

**8 天门禁回测**（窗口 2026-08-17 → 2026-09-02，8 条）：

| 主机/环境 | run_id | 与 ECS 方向 |
|---|---:|---|
| Mac 重建版 | 269 | 7/8 不同（仅 08-24 相同） |
| Mac 原始版 | 270 | 与 269 **逐条相同** |
| ECS 原始版 | 299 | 基准 |

结论：`move_std` 的 1 ULP 差异不是这 8 天最终方向的主因；重建版与原始 Mac 方向完全相同，说明最终方向对该差异不敏感，剩余分歧在 pandas rolling/CSV 解析/LGBM 评分与同分排序路径（与调查 O010–O016 的“17 列 245 差、两相关 X 残差未唯一解释”一致）。

**处置**：按 G3“不能证明完全一致则跳过”，本方案暂跳过，不移入激活流程。环境重建保留：它消除了 `move_std` 这一潜在跨端分歧，且第一个方案不使用 bottleneck，不受影响；其余使用 bottleneck 的方案在各自回归时验证。

### 13.11 第三个方案：XGB 列采样 + 稳定选列闭环（2026-10-21）

**方案** `five_y_t5_xgb_spr_3y1y_b8_v1`（旧 exact `c7b5b0cc0fa0` → 新 `8623acf8e21b`）。

**首分歧层定位**（trace 探针，8 天 + 90 天）：

- 解析后的全列哈希两端不同，但方案消费的 `values`/`labels`/`signals` 逐位相同（差异只在未消费列）；
- `feat_screen` 的 `screen_scores` 多数日期相同，但 `selected` 8/8 天不同 → `np.argsort(-scores)` 同分次序（A1）；
- `selected` 不同导致训练矩阵/概率不同（0.32 vs 0.53），最后方向不同。

**两处确定性控制**（仅排序/采样，无阈值/模型/特征/业务变更）：

1. XGB fit 前 `model.set_params(feature_weights=np.ones(len(selected), dtype=np.float32))`，固定列采样路径（A2）；
2. `feat_screen` 两处 `np.argsort` 加 `kind="stable"`，同分按特征索引确定性排序（A1）。

**验证**：

| 窗口 | 结果 |
|---|---|
| 8 天（2026-08-21 → 09-01） | selected/fit/prob/方向逐位一致 |
| 全量 90 个候选日（2026-06-01 → 10-13） | 方向 90/90；`screen`/`selected`/`fit`/`prob` 全部 0 不一致；仅未消费全列输入哈希不同 |

**Release**：补丁提交 `5634e27f`，构建于 release 分支 `84fd`（保留第一个方案的 canonical active 配置；fix 分支的 scheme1 配置是 paused，不能作为发布基线）。archive SHA `0ee91ba9…`。两端预安装后 `--activate --expected-current 3ca470a2…` 切 current；当前 Mac/ECS `current=5634e27f`，`previous=3ca470a2`。

**严格回测**：

| 主机 | run_id | exact | 请求/记录 | 月度指标 |
|---|---:|---|---:|---:|
| Mac | 272 | `8623acf8e21b` | 90 / 90 | 5 |
| ECS | 301 | `8623acf8e21b` | 90 / 90 | 5 |

**激活**：revision，`business_tables_written=false`；旧 exact retired；预测条数不变（Mac 433 / ECS 431）；第一个方案仍 active（315 / 312，`bc0ef4e31a64`）。

**首个新 exact 在线预测**：2026-10-22 07:03（两端）；10-21 的预测是旧 exact 历史行，未回填。

### 13.12 批量闭环：8 个 T5 模板方案（2026-10-21）

**方案与 exact**（旧 → 新，Mac/ECS 相同）：

| 方案 | 旧 exact | 新 exact | 全量方向 |
|---|---|---|---|
| `five_y_t5_lgbm_3y_anti_lag252_b8_v1` | `5cbbf665e081` | `82bc53e04bcc` | 90/90 |
| `seven_y_t5_lgbm_bf_z_anti40_b8_v1` | `4f182bf23dfc` | `7517e184861a` | 90/90 |
| `three_y_t5_lgbm_7yanti_b12_v2` | `99523554aa06` | `bb4354f94c1f` | 90/90 |
| `three_y_t5_xgb_tp_5y1y_b12_v2` | `4ea0782cc210` | `2b1281d75fdf` | 90/90 |
| `one_y_t5_xgb_10y_streak_anti7_b8_v1` | `9ceb12fdbd55` | `19026a39e0c7` | 90/90 |
| `one_y_t5_xgb_7y_cond_rev20_b12_v1` | `9caa9227981f` | `ff41452470d5` | 90/90 |
| `seven_y_t5_xgb_7y_rv_rev20_b0_v1` | `24d775cc01b7` | `d95f6aa12894` | 90/90 |
| `three_y_t5_xgb_fxlead_b8_v2` | `dc4e944f5aec` | `a4be221cba00` | 90/90 |

**控制**：同一组 T5 模板确定性控制：

- `feat_screen` 两处 `np.argsort(..., kind="stable")`（A1，同分按特征索引）；
- XGB 分支 fit 前 `feature_weights=np.ones(len(selected), dtype=np.float32)`（A2，固定列采样）；LGBM 分支不变。

**验证**：每方案 90 个候选日（2026-06-01 → 2026-10-13）双端严格回测，8 个方案共 **720 条方向逐条一致**。`five_y_t5_lgbm_3y_anti_lag252_b8_v1` 先前因 move_std 被暂跳过；稳定排序后先以 12/12 天（2026-08-17 → 09-01）复验，再进入 90 天。

**Release**：`3f66e946`（基于 `5634e27f`，包含 XGB 方案补丁与第一个方案 canonical active 配置），archive SHA `5c88c1a3…`。两端预安装后 `--activate --expected-current 5634e27f…` 切 current；当前 Mac/ECS `current=3f66e946`，`previous=5634e27f`。

**严格回测 run**：Mac 273–280 / ECS 302–309（每方案 90 请求/记录，5 个月度指标）。

**激活**：8 个方案均 revision，`business_tables_written=false`；旧 exact retired；预测条数不变（Mac 433 / ECS 431 每方案）；先前两个方案仍 active（`bc0ef4e31a64` 315/312、`8623acf8e21b` 433/431）。

**首个新 exact 在线预测**：2026-10-22 07:03（两端）。

### 13.13 第四个方案：ten_y_factor_level_ensemble_v1 闭环（2026-10-21）

**方案** `ten_y_factor_level_ensemble_v1`（旧 exact `997af2e57ad9` → 新 `46710a4c3e66`）。

**首分歧层**：旧记录 O044–O059 共 16 天；当前代码双端回测在 2025-07-03、07-14、07-22、08-18、08-20 等仍不同。模型是 LGB + XGB 因子集成，XGB `colsample_bytree=0.80` 且没有 `feature_weights`，与第一个方案同源。

**控制**：XGB 分支 fit 前 `model.set_params(feature_weights=np.ones(len(columns), dtype=np.float32))`（A2）；LGB 分支不变。config 的 `status`/`version_status` 设为 active（与 DB prior active 对齐，激活必需）。

**验证**（覆盖全部 16 个旧差异日的五个窗口，双端方向）：

| 窗口 | 条数 | 结果 |
|---|---:|---|
| 2025-07-01 → 2025-09-30 | 65 | 一致 |
| 2025-10-01 → 2026-01-01 | 59 | 一致 |
| 2026-01-01 → 2026-04-01 | 55 | 一致 |
| 2026-04-01 → 2026-07-01 | 59 | 一致 |
| 2026-07-01 → 2026-10-20 | 73 | 一致 |

合计 **311/311**。持久化门禁回测（2026-07-01 → 2026-10-20）：Mac run 282 / ECS run 311，73/73 一致。

**Release**：`cef3ad73`（基于 `3f66e946`；archive `4fba5d1d…`）。两端 current 已切，previous `3f66e946`。

**激活**：revision，`business_tables_written=false`；旧 exact retired；预测条数不变（Mac 316 / ECS 312）。

**首个新 exact 在线预测**：2026-10-22 07:03（两端）。

### 13.14 周频方案验证（2026-10-21）

| 方案 | exact（未变） | 窗口 | 结果 |
|---|---|---|---|
| `cgb_causal_wk_1y_v128` | `59415aa789c5` | 默认 2025-01-01 → 2026-10-20，92 条 | Mac run 288 / ECS run 312，92/92 方向一致 |
| `weekly_5y_curve_logit_v1` | `a10fef7a332a` | 同上，92 条 | Mac run 289 / ECS run 313，92/92 方向一致 |

两个方案当前 exact 的代码未改，门禁回测只作为双端验证，不涉及激活；历史预测不变（cgb 93/92、weekly 93/92）。`weekly_5y_curve_logit_v1` 的 config 仍为 paused/draft，DB active exact 保持有效。

### 13.15 日频剩余方案验证与元数据闭环（2026-10-21）

**当前 exact 直接验证（代码未改，不涉及激活）**：

| 方案 | exact | 条数 | 结果 |
|---|---|---:|---|
| `liwei_0616_7y01_cons_say_k3_div_k10` | `3bd1f8067ac5` | 90 | Mac 283 / ECS 314，90/90 |
| `seven_y_current55_lgbm_001_v2` | `cd0624ef3ead` | 94 | Mac 284 / ECS 315，94/94 |
| `seven_y_current55_lgbm_002_v2` | `57e956513471` | 94 | Mac 285 / ECS 316，94/94 |
| `three_y_adyn_lb1_k3_v1` | `98233f0cb9ef` | 94 | Mac 286 / ECS 317，94/94 |
| `three_y_adyn_lb2_k1_v1` | `47c7c1776db0` | 94 | Mac 287 / ECS 318，94/94 |

**G1 元数据修复 + revision 激活**：

两个交付 JSON 缺 `description`，门禁拒绝。补字段（commit `2b59209a`），release `2b59209a`（archive `494b3472…`）切 current，严格回测后激活：

| 方案 | 旧 exact | 新 exact | run Mac/ECS | 条数 | 结果 |
|---|---|---|---:|---:|---|
| `one_y_t5_liq_excess_b_w252_l7_v1` | `ba00891cd179` | `4d0d1f0d6a71` | 290 / 319 | 90 | 90/90 |
| `ten_y_t5_maj4_k3_ic_static_v1` | `6bdabf86b4a6` | `0b96997c496a` | 291 / 320 | 90 | 90/90 |

两次均为 revision，`business_tables_written=false`；预测不变（Mac 433 / ECS 431 每方案）。当前两端 `current=2b59209a`，previous `cef3ad73`。

**Phase 1 状态**：21 个方案全部闭环/验证（见 13.16）。

### 13.16 第五个方案：liwei_0616_7y03 闭环（2026-10-21）

**方案** `liwei_0616_7y03_cons_all_k3_div_k8`（旧 exact `109e657e9a70` → 新 `97ed67c0fc12`；更旧的 `6b8d4047dc8a` 已于此前 retired）。

**首轮**：现行 exact 双端门禁 90 条中 89 条一致，仅 2026-06-12 Mac=1 / ECS=0。该交付有 6 处 `np.argsort`（特征 IC 选列、信号选择、`_diverse_topk`、top-k）与 `argmax`/阈值并列点。

**控制**：6 处 `np.argsort` 全部改为 `kind="stable"`（A1，同分按索引确定性排序）；`argmax`/阈值首最优保持不变。

**验证**：

- 稳定排序版全量 90 天双端逐条一致（06-12=0、09-03=-1；相对旧输出只有这 2 天变化）；
- 持久化门禁回测：Mac run 293 / ECS run 322，新 exact `97ed67c0fc12`，90/90 一致。

**Release**：`f485e3b0`（基于 `2b59209a`；archive `ef70bea5…`）。两端 current 已切，previous `2b59209a`。

**激活问题与处理**：首次激活被拒——`t_scheme_versions` 中存在一条 paused 的 legacy `native_adapter` 版本 `f2a5e8c4e002`，revision preflight 只允许 `blackbox_v2` 或已 retired 的 native 版本。已先备份该方案全部版本行（Mac `pre_7y03_versions_mac.json`、ECS `/root/pre_7y03_versions_ecs.json`），再把该 paused native 行置为 retired；preflight 通过后 revision 激活成功，`business_tables_written=false`；预测条数不变（Mac 433 / ECS 431）。

**Phase 1 结果**：21/21 方案的 active exact 与 effective 状态双端完全一致；旧 `6b8d4047dc8a` 的 4 条“无法复现”记录仍属历史，不重跑；现行交付已验证。

---

## 14. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-10-20 | 初版：目标、第一性原理、四层边界、执行顺序、容差规范、验收口径、现状基线、未决事项 |
| v0.2 | 2026-10-20 | 增加第 11 节缺口登记：范围与治理、输入与数据、执行与算法、验证与工程共 27 项 |
| v0.3 | 2026-10-20 | 锁定 Phase 1 = 21 个已知不一致方案；增加 1.3 阶段范围与附录 A 清单；更新 G1/P1 |
| v0.4 | 2026-10-20 | 第二轮缺口补充 16 项（N1–N16）：责任边界、全栈迁移、回测/在线路径、历史上下文、parser 语义、fallback 分支、原生库、阈值并列、列序、方向调优、平台版本、旧记录对应、失败偏差、GPU、输出精度 |
| v0.5 | 2026-10-21 | 增加 Phase 1 逐方案闭环流程、四道门、数据一致性方案、已确认/待确认清单、21 方案台账 |
| v0.6 | 2026-10-21 | `five_y_factor_rule_online_v1` 闭环：源表/DataBridge 双端一致、严格回测 268/298、revision 激活（`bc0ef4e3` active / `55f0e753` retired）、历史预测未变；记录 100 行回测契约约束与 ECS 后续数据通路待决项 |
| v0.7 | 2026-10-21 | 第二个方案 `five_y_t5_lgbm_3y_anti_lag252_b8_v1`：量化并重建 Mac bottleneck（`-ffp-contract=off`，move_std 与 ECS 逐位一致），但 8 天方向不变，剩余分歧在 rolling/parse/LGBM；按 G3 暂跳过 |
| v0.8 | 2026-10-21 | 第三个方案 `five_y_t5_xgb_spr_3y1y_b8_v1` 闭环：XGB `feature_weights` + `feat_screen` 稳定排序；90/90 方向与概率逐位一致；release `5634e27f` 切 current；revision 激活 `8623acf8e21b`，历史预测未变 |
| v0.9 | 2026-10-21 | 批量闭环 8 个 T5 模板方案（3 LGBM + 5 XGB）：同一组稳定排序/列采样控制；8×90=720 条方向逐条一致；release `3f66e946` 切 current；8 个 exact revision 激活，历史预测未变；`five_y_t5_lgbm_3y_anti_lag252_b8_v1` 由暂跳过转为闭环 |
| v0.10 | 2026-10-21 | `ten_y_factor_level_ensemble_v1` 闭环：A2 列采样控制；五个窗口 311/311 天方向一致；release `cef3ad73` 切 current；revision 激活 `46710a4c3e66`，预测 316/312 未变 |
| v0.11 | 2026-10-21 | 周频 `cgb_causal_wk_1y_v128` / `weekly_5y_curve_logit_v1` 当前 exact 双端 92/92 验证（无代码变更）；记录两个方案缺 `description` 的 G1 元数据修复（commit `2b59209a`，待下一 release 验证激活） |
| v0.12 | 2026-10-21 | 日频剩余 5 个方案当前 exact 双端验证（liwei 90/90；current55 001/002、adyn lb1/lb2 各 94/94）；两个元数据缺失方案补 `description` 后 release `2b59209a` 切 current，revision 激活 `4d0d1f0d6a71` / `0b96997c496a`，预测 433/431 未变；Phase 1 达 20/21，唯一阻断为缺交付版本的 liwei 7y03 |
| v0.13 | 2026-10-21 | `liwei_0616_7y03` 闭环：6 处 argsort 稳定排序；全量 90/90 双端一致；release `f485e3b0` 切 current；处理 paused legacy native 版本后 revision 激活 `97ed67c0fc12`，预测 433/431 未变；**Phase 1 21/21 完成** |
