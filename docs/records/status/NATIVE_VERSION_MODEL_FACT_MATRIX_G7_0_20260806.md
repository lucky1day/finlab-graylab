# G7.0：Native 版本模型事实矩阵与目标决策稿（2026-08-06）

**文档状态**：DRAFT_FOR_DECISION。本文不是已批准决策、当前生产控制面或任何生产操作授权。

**范围**：只整理仓库和既有带日期证据中可核验的 Native V1 身份、精确版本、composite Registry、
Gate/admission 与 API/前端可见性关系，并提出最小目标语义。没有读取数据库、served API、
installed plist、launchctl 或日志；没有运行 Harness、激活、Registry 同步或任何业务写入。

**证据截点**：2026-08-06；计算型事实基于本工作树在新增本文前的提交
8a32708。Native 方案版本只由方案目录的 predict.py、core 下 Python 文件和 config.yaml
内容计算，因此本文本身不改变下面的候选版本值。

## 阅读约定

本文严格区分三类内容：

- **事实**：可由当前仓库代码、配置、迁移、测试或既有状态记录直接复核。
- **推论**：由多个事实得出的风险或语义结论，不改变现行规则。
- **建议**：供用户选择的目标模型；未获批准前不得实现或据此写库。

矩阵缩写：

- **P**：在 deploy/onboarding_policy_v1.json 的 Native allowlist 中。
- **C**：当前检出 config.yaml 的事实。所有 C 行均为 runtime_type=native_adapter、
  status=active；所有这些 config 都没有单独的 version_status 键，discovery 因而把
  version_status 派生为 active。
- **V**：按 shared.versioning 的现行规则从当前检出代码和配置计算的本地候选精确版本。
  V 不是 t_scheme_versions 中存在、当前 active 或已批准的证明。
- **H**：既有、带日期的历史证据。H 仍需以新的只读读回确认其现场是否未漂移。
- **U**：仓库无法证明，必须由后续专项的只读现场证据确认。

## A. 可核验事实

### A.1 身份层次与权威边界

1. **Native 维护资格**由 policy 的 26 个 base_scheme_id 决定；白名单只允许存量维护，
   不是激活、Registry 或 scheduler 权限。仓库测试断言 policy 与所有 Native config
   目录恰好一一对应。
2. **算法执行身份**是 base_scheme_id，即目录名、config.scheme_id、predict.py 的
   SCHEME_ID 和预测/运行/回测底表使用的 scheme_id。
3. **业务/前端身份**是每个目标期限一行的 composite Registry ID：
   base_scheme_id__h{horizon}__{target_tenor}。Registry 不保存 exact scheme_version。
4. **精确版本**是 current scheme directory 的 predict.py、core 代码和 config.yaml
   字节的 hash 组合前 12 位；它不是 Git commit，也不会因仅编辑本文而改变。
5. **Native business identity snapshot**用于 maintenance admission，字段只有
   scheme_id、runtime_type、horizon、task_type、frequency、排序后的 tenors 与所有
   composite Registry IDs；它明确不包含代码、config 或 exact version hash。
6. **可见性**只由 active Registry composite 行决定：/api/schemes、/api/metrics、
   /api/predictions、factor-lab dashboard/backtest 和前端当前矩阵均只读
   Registry status=active。executor 另外要求当前 config 的 exact version 也在
   t_scheme_versions 中为 active。

### A.2 完整 Native 基础身份事实矩阵

以下 26 行覆盖 policy 的全部 Native 身份及其 30 个预期 composite Registry IDs。业务元组
写作 horizon / task_type / frequency / tenors。除特别标注的 H 外，持久化 version 状态、
Registry 状态、Harness/admission 证据和 served API/前端的当前状态均为 U，不能由 C 或 V
替代。

| base_scheme_id | C：业务元组 | V：当前检出候选 exact version | 预期 composite Registry ID | 状态、证据与 API/前端可见性 |
|---|---|---|---|---|
| daily_10y_lgbm_10y04_0629 | h1 / T+1 / daily / 10Y | 6907040e2e62 | daily_10y_lgbm_10y04_0629__h1__10Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| daily_1y_xgb_1y13_0629 | h1 / T+1 / daily / 1Y | c0f2971ccb5c | daily_1y_xgb_1y13_0629__h1__1Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| daily_5y_2_v28 | h5 / T+5 / daily / 5Y | fce0d1126dc5 | daily_5y_2_v28__h5__5Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| daily_5y_lgbm_5y10_0629 | h1 / T+1 / daily / 5Y | 4e22b346d723 | daily_5y_lgbm_5y10_0629__h1__5Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| daily_7y_1_v28 | h5 / T+5 / daily / 7Y | febc16e47919 | daily_7y_1_v28__h5__7Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| liwei_0616_10y01_cons_say_k3_div_k10 | h5 / T+5 / daily / 10Y | 481f79b25fae | liwei_0616_10y01_cons_say_k3_div_k10__h5__10Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| liwei_0616_10y01_full_oos_k3_div_k10 | h5 / T+5 / daily / 10Y | 35e461e60705 | liwei_0616_10y01_full_oos_k3_div_k10__h5__10Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| liwei_0616_10y02_cons_say_k3_div_k5 | h5 / T+5 / daily / 10Y | 99077e04ed02 | liwei_0616_10y02_cons_say_k3_div_k5__h5__10Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| liwei_0616_5y01_full_oos_k3_div_k10 | h5 / T+5 / daily / 5Y | b03b275b7bf9 | liwei_0616_5y01_full_oos_k3_div_k10__h5__5Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| liwei_0616_5y_auc_static_all_k3_div_k10 | h5 / T+5 / daily / 5Y | f0bf56b80fb4 | liwei_0616_5y_auc_static_all_k3_div_k10__h5__5Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| liwei_0616_5y_auc_yearly_all_k3_div_k10 | h5 / T+5 / daily / 5Y | d5de982fbfc6 | liwei_0616_5y_auc_yearly_all_k3_div_k10__h5__5Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| liwei_0616_5y_ic_yearly_all_k3_div_k10 | h5 / T+5 / daily / 5Y | 5e716cd7ed7b | liwei_0616_5y_ic_yearly_all_k3_div_k10__h5__5Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| liwei_0616_7y01_cons_say_k3_div_k10 | h5 / T+5 / daily / 7Y | 84ed148f96e5 | liwei_0616_7y01_cons_say_k3_div_k10__h5__7Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| liwei_0616_7y03_cons_all_k3_div_k8 | h5 / T+5 / daily / 7Y | 6b8d4047dc8a | liwei_0616_7y03_cons_all_k3_div_k8__h5__7Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| liwei_0616_cons_sda_k3_div_k10 | h5 / T+5 / daily / 5Y | bdb322ef65fd | liwei_0616_cons_sda_k3_div_k10__h5__5Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| monthly_10y_rf_top5_0629 | h30 / monthly / monthly / 10Y | 3a3790efaddd | monthly_10y_rf_top5_0629__h30__10Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| monthly_1y_rf_top30_0629 | h30 / monthly / monthly / 1Y | 38ed3c462048 | monthly_1y_rf_top30_0629__h30__1Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| monthly_5y_knn_top20_0629 | h30 / monthly / monthly / 5Y | f0255a38ba6b | monthly_5y_knn_top20_0629__h30__5Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| t1_daily | h1 / T+1 / daily / 5Y, 10Y | 7898b9e47a9a | t1_daily__h1__5Y; t1_daily__h1__10Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| t5_daily | h5 / T+5 / daily / 3Y, 5Y, 7Y, 10Y | a7df258abafe | t5_daily__h5__3Y; t5_daily__h5__5Y; t5_daily__h5__7Y; t5_daily__h5__10Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| weekly_10y_d_overlay_0529 | h6 / weekly_point / weekly / 10Y | e50ad79a6c2f | weekly_10y_d_overlay_0529__h6__10Y | P+C+V+H：2026-08-04 的记录称该 exact version 与该 Registry 均已 active；run 2106 的唯一 gray_live key 有 DB 与 served API 读回。当前 DB、API、前端与 scheduler 状态仍 U |
| weekly_5y_direct_0529 | h6 / weekly_point / weekly / 5Y | 04855c6b11d6 | weekly_5y_direct_0529__h6__5Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| weekly_7y_cross_d_overlay_0529 | h6 / weekly_point / weekly / 7Y | 66046c7216f8 | weekly_7y_cross_d_overlay_0529__h6__7Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| weekly_avg_10y_lgbm_0529 | h6 / weekly_average / weekly / 10Y | 4b22f3210b14 | weekly_avg_10y_lgbm_0529__h6__10Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| weekly_avg_1y_lgbm_0529 | h6 / weekly_average / weekly / 1Y | 03f8e0fa9f11 | weekly_avg_1y_lgbm_0529__h6__1Y | P+C+V；DB version、Registry、admission、API/前端均 U |
| weekly_avg_5y_lgbm_0529 | h6 / weekly_average / weekly / 5Y | a43b6c9ae0b2 | weekly_avg_5y_lgbm_0529__h6__5Y | P+C+V；DB version、Registry、admission、API/前端均 U |

唯一的 H 不是一个可外推 waiver。它仅说明 weekly_10y_d_overlay_0529 的固定 prior、
receipt、maintenance、activation 和单一 gray_live key 曾按各自独立授权闭环；它不证明
其它身份、其它 key、scheduler admission 或 installed control plane。

### A.3 当前状态转换、唯一写入者与失败闭环

下表描述的是仓库中当前可见的实现路径，不是对现场状态的断言。

| 当前转换或读路径 | 已实现前置条件 | 仓库中的写入者/唯一职责 | 对 API/前端与执行的效果 | 失败闭环 |
|---|---|---|---|---|
| 方案内容变更或首次发现 → 本地 V | config、predict.py、core 内容可读 | 文件系统变更本身；shared.versioning 只计算，不写 DB | 无可见性、无执行权 | 不是生命周期转换；V 不足以证明任何 DB 行 |
| 发现/同步 → exact version draft 与 Registry paused 投影 | 当前 config 可 discovery | scheduler.repository.sync_scheme_registry 是 DB 写入实现；backend.services 的 config sync 和 legacy replay operator 都调用它 | 未知 Native active config 在测试中会得到 version draft、Registry paused；paused 不进入 API/前端，也不允许 live target | 单事务；不满足完整 active identity 时不 grandfather active |
| 现有 exact active + 完整 active Registry → 同步后仍 active | config active、当前 exact version native_adapter/active、所有预期 Registry 行完整且 active | 同一 sync 函数只保留既有生命周期，不提升它 | 可继续满足 API/前端和 executor 的 Registry 条件 | 缺行、错 target 或 paused 时同步把当前预期行投影为 paused |
| full all 技术验证 | 当前 exact version 的七 Gate 结果满足 full profile；benchmark-required 不接受 compare skipped | Harness persistence 写 t_harness_runs 和 t_harness_gate_results；它不是 Registry/version active 写入者 | api-readiness 只表示结构准备，不是 active API 验收 | 无 Gate 完成或 Gate 不通过则 ActivationGate fail-closed |
| native-maintenance 技术验证 | policy 内同一业务身份、prior active Native all+compare、匹配 static.business_identity 或唯一固定 receipt、当前 version draft/active、Registry 全 paused 或全 active | NativeMaintenanceAdmissionGate 只读核验；Harness persistence 记录六 Gate 证据 | 预激活 profile 要求 public API/metrics 隐藏；维护阶段不写业务表、不改变生命周期 | draft+active Registry、缺/重复/错 identity、缺持久证据均阻断 |
| 激活/重批准 → current exact version active + 预期 Registry 全 active | 一次性 activate token 精确绑定 validation version；严格 discovery；full all 或 maintenance profile 之一通过 | ActivationGate 是唯一的激活授权面；其唯一非测试调用点使用 scheduler.repository.apply_native_activation_state，在一个事务内写 version 和 Registry | active composite 行进入 API/前端；executor 仍须验证 config exact version active | Gate 在 DB 同步失败时回滚 config status；version/Registry 读回不精确则事务失败 |
| executor live 路径 | 当前 config exact version 在 t_scheme_versions 为 active，且至少有 active Registry target | scheduler.executor 只读验证；预测/run/日志写入仍由 scheduler.repository | 只可写 active Registry target；active 以外 target 被拒绝 | 版本缺失、非 active 或 Registry 无 active target 均 fail-closed |
| Registry active → paused/archived 或 Native active → retired | 没有在本轮检索中找到一个专用 Native deactivation/retirement Gate | generic sync 可在 candidate/identity 不满足时写 paused，并会 archive 不再在 config 预期集合内的旧 Registry 行；显式 retired 更新在仓库中出现于 Blackbox revision 路径 | paused/archived 不可 API/前端/trigger；旧 Native exact version 是否仍 active 不能由 Registry 行反推 | 当前语义没有一条同样清晰的 Native retire transaction，必须在 G7.1 前先决定 |

补充事实：

- ActivationGate 的根级 config status 从 paused 翻成 active 时，该 status 字节参与
  config hash，所以 activated scheme_version 可以不同于 token 所绑定、通过 Gate 的
  validation_scheme_version。已有测试分别覆盖“报告两个版本”和“只变 lifecycle/派生版本
  字段”。
- apply_native_activation_state 不会在同一事务中把旧 Native active version 标为 retired；
  与之相对，Blackbox revision activation 有显式 retired 更新。故“某 base 只有一个
  Native active exact version”不是当前代码可由数据库状态自动保证的已证明不变量。
- 当前 repository 架构把业务写库实现收敛在 scheduler.repository；Harness 的
  control-plane persistence 仅写 Harness 表，不能产生预测/回测业务数据。

### A.4 API/前端、admission 和 scheduler 的不同含义

1. Registry active 是当前 API 和前端可见性的必要条件；它不是 Gate 通过、admission
   或 installed launchd 的证明。
2. exact version active 是 executor 的额外必要条件；它不含 per-composite target
   映射，因 Registry 表没有 scheme_version 列。
3. full all 与 native-maintenance 都只提供技术验证路径。activation token、live 或
   persistent backtest、scheduler admission、installed plist 和 launchctl 均是独立授权面。
4. native-maintenance 的 api-readiness 预激活 profile 反而要求 public factor-lab 和
   每个 composite metrics endpoint 不可见。它避免 draft candidate 在 activation 前泄露，
   但使“api-readiness”这个名称不能被解释为“已经对前端可见”。
5. launchd + installed plist 是生产调度的唯一控制面。仓库中的 config、Registry、
   active version、API 或无写库测试都不能证明自然时钟已经挂载或触发。

## B. 推论：重复或相互容易混淆的语义

以下是基于 A 节事实的推论，不是对任何数据损坏的断言。

1. **三个 active 不是一个状态。** config status=active 表示检出配置；exact version
   active 表示某个内容 hash 的 DB 生命周期；Registry active 表示一个业务 target 的
   API/前端曝光。unknown active Native config 被 sync 成 draft/paused 的测试证明三者不能互推。
2. **当前 exact version 同时承载内容与生命周期字节。** config.yaml 整体参与 hash，
   而 ActivationGate 可以改 status。因此“技术验证的精确版本”和“激活后的精确版本”
   可能不同，不能在审计语言中把两者无条件称为同一 version。
3. **Registry 是身份/曝光表，不是 version pointer。** 已知 composite ID 可以说明
   target 身份，却不能单独回答“它目前映射哪一个 exact version”。当同一 base 可能保留
   多个 active Native version 时，事后读 Registry 更无法消除歧义。
4. **共享生命周期图比 Native 实现更宽。** 共享契约列出 draft、validated、shadow、
   active、paused、retired，但当前 Native 的明确执行路径主要使用 draft/active 加 Gate
   evidence；未发现 Native 专用 retired 迁移。若不先定义每个状态的 writer，枚举值会
   被误读为现成可用的 Native 状态机。
5. **generic sync 既是元数据同步又会改变 Registry 生命周期投影。** 它不是 active
   promotion writer，但当前 candidate 漂移或 identity 不完整时会造成 active → paused 的
   业务可见性变化。这与“ActivationGate 是唯一激活者”并不矛盾，却需要在目标模型中
   明确两者的权限边界。
6. **当前证据覆盖度不均衡。** weekly_10y_d_overlay_0529 有带日期的 exact evidence；
   其他 25 个身份仅有 P+C+V。把完整矩阵中 25 行的 U 读取为 paused、active 或 retired
   都会是臆测。

## C. 建议：供用户确认的最小目标生命周期

### C.1 建议的语义模型（未批准）

建议把 Native 最小模型分成四个彼此不替代的对象，而不新增算法身份或 target：

| 对象 | 建议的唯一含义 | 建议的最小状态/关系 | 不应再推导的含义 |
|---|---|---|---|
| 业务身份 | allowlisted base_scheme_id 加 runtime_type、horizon、task_type、frequency、tenors、完整 composite Registry 集合 | 维护前后必须逐字段不变；变化即为新 Blackbox trial，而非 Native revision | 不是代码版本、admission 或可见性 |
| exact content version | 某一不可变方案内容指纹；现存 scheme_version 值先原样保留 | candidate/draft → active → retired；Gate passed 是关联证据，不先引入没有唯一 writer 的 Native validated/shadow 转换 | 不是 Registry ID，也不等于 scheduler admission |
| Registry exposure | 每个 composite ID 的业务/API/前端曝光状态 | active、paused、archived；同一业务身份的预期集合必须完整、target 不增不减 | 不保存或猜测历史 code hash |
| admission/activation evidence | full all 或 maintenance 的精确 Gate history、prior identity、token 和 activation receipt | evidence 只证明满足某条路径；activation 才能原子改变 active 状态 | 不授予 live、persistent backtest、scheduler 或 launchd |

建议的关系约束如下：

1. 一个 business identity 在任意时刻最多一个 **current active exact version**。在成功激活
   successor 时，将前一 active Native version 转为 retired，并保留其 hash、approval、
   Harness、run 与 prediction 引用，不删除任何历史行。
2. 一个 active Registry composite 必须属于完整的当前 business identity，且该 identity
   有一个 current active exact version。反向允许 active version 配全 paused Registry，
   但只把它解释为经明确管理的“已批准、未曝光”状态；不能把它误读为 API 或 scheduler active。
3. Registry 的 paused/archived 是 **exposure** 变化，不自动把 exact version 降级或删除；
   版本 retired 是 **内容版本**变化，不重写历史 Registry、run、prediction 或 backtest。
4. generic discovery/sync 只可登记未知 current content 为 draft，或执行经明确授权的
   identity reconciliation；它不应在没有独立审批记录的情况下替代 activation/deactivation
   决定。
5. 对 status 翻转造成的 validation version 与 activated version 不同，最小兼容选择是
   **保留现有 hash 算法**，并持久记录 immutable 的 activation_from_validation_version
   链接。这样不重算或覆盖历史 version。把生命周期字段从未来 hash 排除属于另一项、更大
   的兼容决策，不能在 G7.1 中默认实施。

该建议有意不创建新 Native ID、不修改算法、target、task type、Registry composite 格式、
source benchmark、输入截止或 launchd 语义。

### C.2 历史兼容与只读保留边界（建议）

无论最终目标模型如何，以下现有数据应只读保留，不得因“版本收敛”删除、重写或反推：

- t_scheme_versions 的所有 legacy exact version、hash、approval 和创建时间；
- t_harness_runs、t_harness_gate_results 及唯一 legacy-admission receipt；
- t_scheme_runs、t_scheme_predictions、run log、t_backtest 记录、输入 artifact 引用与
  source/benchmark evidence；
- 既有 Registry 行及其历史审计信息，尤其是 composite ID 与 base/tenor/horizon 的映射；
- weekly_10y_d_overlay_0529 的 prior version 63ffb52105ee、receipt
  lna_hr_20260611T055610Z_8742d5bc99c9、maintenance run
  hr_20260804T102103Z_91fa9e7db871、exact active version e50ad79a6c2f
  和其单一 gray_live closure evidence。

建议的迁移边界是“只为之后的状态转换增加清晰链接/约束”，不是重写过去：

1. 首先用只读数据库清单给每个当前候选 version 和 composite Registry 行分类为
   confirmed、absent、drifted 或 unresolved。
2. 只有 confirmed current active identity 才可考虑在后续、单独授权的事务里收敛
   “旧 active → retired / 当前 version → active / 全部 composite exposure”的关系。
3. 对 absent 或 unresolved 身份，不得用 YAML、Git hash 或本文件补造 prior evidence；
   必须按现行 full all 或满足条件的 native-maintenance 路径处理。
4. 不建立 daily ledger、occurrence、epoch 或第二 scheduler；不把历史 gray_live 改称
   scheduled_live。

## D. G7.1 最小专项计划的输入与重新授权清单

G7.1 在用户确认 C 节中的目标语义前保持阻断。经确认后，最小专项计划至少需要以下输入：

1. 用户选择是否接受“每个 Native business identity 最多一个 current active exact version”
   及旧 active 的 retired 语义。
2. 用户选择 validation version 与 activated version 的兼容策略：推荐先记录
   activation_from_validation_version 链接，不在第一阶段改 hash 算法；若选择改 hash，
   必须另列历史 alias/回滚设计。
3. 用户选择 active version + all Registry paused 是否是合法的已批准但未曝光状态，以及
   谁能作出 pause/archive 决定。
4. 一份新鲜、只读的 26 base / 30 composite 现场清单：t_scheme_versions、t_scheme_registry、
   t_harness_runs、t_harness_gate_results、最新相关 API/dashboard 结果。它必须按本文件的
   V 和 expected composite IDs 精确比对，不能只按 base ID 汇总。
5. 对每个拟修改 identity 的 prior admission、business identity snapshot、Gate profile、
   input cutoff 和 live-safe oracle 证据；任何缺失项都保留 fail-closed。
6. 精确的回滚定义：数据库事务失败、Registry/version 读回漂移、API 可见性异常和
   config/status hash 漂移时分别如何停止，不覆盖历史版本。

以下动作不因本文、G7.0 的阅读范围或未来只读清单而获得授权，均须用户再次明确授权：

| 动作 | 需要的重新授权 |
|---|---|
| 连接/查询生产或候选数据库、served API、installed plist、launchctl、服务或日志 | 本任务明确限制为仓库/既有证据；任何现场读取先取得新的明确只读 scope |
| 修改 repository、Harness、backend、config、文档规范或测试 | 用户批准 G7.0 决策和 G7.1 专项实施计划后，才可进行仓库写入 |
| DDL、迁移、数据修复、Registry/version 状态变更 | 独立数据库/生产授权；迁移仍只能走受控 migration CLI，不能手工 SQL |
| native-maintenance receipt、activate token 消费、activation 或 re-approval | 每个 exact scheme_version、action、operator 与时点的独立一次性授权 |
| gray_live、scheduled_live、persistent backtest、scheduler admission | 分别的业务写入/生产授权；G7 版本模型不外推这些能力 |
| installed plist 替换、launchctl、服务重启或自然调度观察 | 独立生产操作授权；不得与 G7.1 仓库变更混合 |
| 合并 master、推送或发布 | 独立的分支/发布明确决定 |

## E. 建议的后续只读取证格式（未执行）

为避免把不同层级的 active 混在同一结论中，后续读取应对每个 base_scheme_id 输出以下四块，
但不得在本 G7.0 范围内执行：

| 证据块 | 最小字段 | 需要回答的问题 |
|---|---|---|
| Exact version | scheme_id、scheme_version、runtime_type、status、code/config/manifest hash、approved_by/at | 当前检出 V 是否存在？哪一行是 current active，是否有多个 active？ |
| Composite Registry | scheme_id、base_scheme_id、runtime_type、horizon、task_type、frequency、target_tenor、status | 30 个预期 ID 是否完整且状态统一？是否有旧/额外 active 行？ |
| Admission | harness run/version/stage/status、各 Gate、prior all+compare、business identity snapshot/receipt | full all 或 maintenance 的哪条路径被精确满足？ |
| Serving/production | served API/dashboard、latest run/prediction、installed plist/loaded state/log（仅需要 scheduler 结论时） | 当前 API/前端是否可见？自然生产 writer 是否真的挂载/触发？ |

## 结论（事实与建议分开）

**事实结论**：仓库可完整证明 26 个 Native maintenance identity、当前检出候选 exact version
和 30 个 expected composite Registry identity；不能仅凭这些证明现场 active。唯一已有的
精确 active 历史证据是 weekly_10y_d_overlay_0529 的 2026-08-04 闭环，且其当前现场状态仍待
新的只读读回。

**推论结论**：Native 的 config、exact version、Registry exposure、Gate evidence 与 scheduler
admission 目前使用重叠的 active/paused 词汇，却属于不同对象；version hash 又受 lifecycle
config 字节影响，因此需要先确认目标语义，不能直接做“状态统一”写入。

**建议结论（未批准）**：G7.1 应先以“一个 current active exact version + 独立 Registry
exposure + immutable admission/activation links + 历史只读保留”为最小模型，并先取得一份
按本矩阵逐行比对的只读现场证据。任何代码、数据库、激活、Registry、业务写入或 launchd
动作都留在获批后的独立授权中。

## 主要证据来源

- deploy/onboarding_policy_v1.json、全部 schemes 下 Native config.yaml，以及
  tests/test_onboarding_policy.py：Native allowlist、目录和 runtime 对应关系。
- shared/versioning.py、scheduler/discovery.py：exact version 的计算和 config
  version_status 派生规则。
- docs/architecture/SCHEME_CONTRACT.md、docs/native_v1/SCHEME_CONTRACT.md、
  docs/architecture/ARCHITECTURE.md：base/composite 身份、Registry 与 API 语义。
- scheduler/repository.py、harness/gates/activate_gate.py、
  harness/gates/native_maintenance_admission_gate.py、scheduler/executor.py：
  当前同步、maintenance、activation 与执行的读写边界。
- tests/test_repository_registry.py、tests/test_activation_gate.py、
  tests/test_native_maintenance_admission.py、tests/test_cli_activate.py：
  当前 lifecycle fail-closed、draft/paused 预激活、exact version drift 与 readback 契约。
- AGENTS.md、docs/CURRENT_STATUS.md、docs/architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md、
  docs/records/SCHEME_ISSUE_LEDGER.md：现行授权边界与 weekly_10y_d_overlay_0529 的既有历史证据。
