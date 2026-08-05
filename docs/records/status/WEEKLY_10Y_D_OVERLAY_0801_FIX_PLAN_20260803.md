# 生产信号与调度治理调研及分阶段计划（2026-08-03）

**文档状态**：`ACTIVE GOVERNANCE PLAN / NEW-SESSION HANDOFF`

**适用分支**：`codex/audit-bugfixes-20260613`

**总体目标**：第一优先把新交付的两套 7Y T+1 方案放入灰度实验室并做到前端可见；随后回到最基本的生产要求——在正确的数据截止条件下，每个应运行的方案按时且只产生一次信号，失败时明确告警，不用旧数据伪装成功。

**初始调研边界（2026-08-03）**：本文最初只固化调研结论、治理顺序和验收口径；后续受控生产动作、授权和现场证据均以各阶段执行记录为准。当前仍不得把未经过自然时钟的挂载状态混同为生产闭环。

> **给新会话的首要说明**：本文不是代码实现说明。开始每个阶段前，模型必须重新检查现场，再基于当时的代码选择最小实现；不得把 2026-08-03 的数量、hash 或 loaded state 当作永久事实。

---

## 1. 新会话如何接手

新会话应先完成以下动作，再开始任何治理：

1. 阅读根目录 `AGENTS.md`、`CLAUDE.md` 和本文，确认仍在指定开发分支。
2. 检查工作区状态，区分本文、用户草稿、诊断脚本和临时 `outputs/`，不得使用全量暂存。
3. 重新只读核对 active Registry、当前方案 hash、缺失信号、DataBridge current、installed plist、`launchctl` loaded state 和最新日志。
4. 将新证据与本文“2026-08-03 快照”比较；若数量或根因变化，先更新本文，不能机械执行旧结论。
5. 一次只推进一个未完成阶段。当前必须先执行 P-1；完成后将证据日期、结果和遗留风险回写本文，再进入后续阶段。

任何数据库写入、方案激活、历史补数、installed plist 替换、`launchctl bootstrap/bootout/kickstart`、服务停止或重启，都是独立的生产动作，必须先取得用户明确授权。代码验证不自动包含这些权限。

### 1.1 三种“完成”必须分开

后续会话不得把以下状态混称为“已解决”：

| 状态 | 含义 |
|---|---|
| 代码完成 | 开发分支上的实现和测试已通过，但未改变生产现场 |
| 已挂载 | installed plist 和 loaded state 已按授权切换，但尚未经过真实时钟触发 |
| 生产闭环 | 已由真实时钟自然触发，并用日志、run、prediction 和页面/API 读回证明结果正确 |

只有达到各阶段的“完成定义”，才可以把该阶段标为完成。

---

## 2. 已确认的治理原则

这些原则是后续探索和取舍的边界：

1. **launchd + plist 是唯一生产调度控制面。** Python runner 只是被 plist 启动的一次性执行器，不是第二套调度系统；ledger、occurrence 和 epoch 永远不再作为生产或过渡方案。
2. **一个 cadence 只能有一个生产 writer。** 不能让常驻 APScheduler、daily-gray 和新 plist 同时写同一业务信号。
3. **自然调度与历史修复语义分离。** launchd 自然触发写 `scheduled_live`；经授权补历史缺口写 `gray_live`。
4. **新鲜度优先于“看起来成功”。** DataBridge 不满足当日 refresh 与正确 feature cutoff 时必须失败，不能回退到旧 artifact。
5. **最小修正不等于绕过门禁。** 激活、Harness、repository、输入截止和源算法保真约束继续有效。
6. **P0 先恢复稳定出信号。** 不启用、不扩容、不迁移或补建 ledger，不删除历史版本，不重写 Native 算法，不顺带清空历史表。
7. **兼容开关不等于调度权。** P0 期间现有底层代码仍可能需要 `BOND_DAILY_COORDINATOR_MODE=legacy` 才能运行；它只是临时兼容条件，不代表 legacy 是生产控制面。待 P2 再移除。
8. **7Y 灰度可见性优先于效果完善。** 新方案允许带着已知效果问题或非致命算法 bug 进入隔离灰度观察；但不能绕过 Contract、确定性、输入截止、写库边界和授权。灰度可见不等于已获得定时调度权。
9. **Native benchmark 只承担首次技术入库证据。** ActivationGate 的 Native profile 互斥：current exact version 完整通过 `all` 时使用 `full_initial_onboarding_v1`，只核验当前七个 Gate（含 Compare），不要求 prior snapshot 或 `native-maintenance`；只有未走该 full-`all` profile、且 prior `all` 的 `static.business_identity` 已持久化并与当前业务身份精确匹配时，才能使用 `native-maintenance` 当前日期验证。快照只含 scheme/runtime/horizon/task/frequency/tenors/composite IDs，不含代码、config 或 version hash。legacy admission 缺快照仍 fail-closed；唯一已实现的固定 scope 是 `weekly_10y_d_overlay_0529` 的 `native-legacy-admission-attest`，它要求 maintenance 选定的 prior 唯一 `all + compare=passed`、StaticGate 已通过且 identity 字段明确缺失、以及 issuer/exact prior version/run 绑定的 ≤900 秒一次性 token。receipt 只写两张 Harness 表、不能重放或改历史、只作为 `legacy_operator_attestation_v1` 身份来源，之后仍须六段 Gate 和常规 activation。满足任一已实现 profile 后，历史 source-benchmark 输入 vintage 漂移只归档，不单独阻断激活、补数、`gray_live`、`scheduled_live` 或 API。该政策不放宽 L0/L1/L2、输入截止、统一周历、日期语义、Registry、live-safe oracle 或授权，也不改变 Blackbox CompareGate。

---

## 3. 2026-08-03 调研快照

以下内容是交接证据，不是永久验收常量。

| 调研面 | 当时状态 |
|---|---|
| active 方案发现 | daily 28 个 execution、weekly 14 个、monthly 8 个；daily Registry 为 32 个业务 target |
| 8 月 3 日日频 | T+1 应有 8、已有 3；T+5 应有 24、已有 16；共缺 13 个 target |
| DataBridge | HTTP 入口上游 MySQL connection refused；本机 `bond_db` 六张必要源表存在且可读 |
| 日频调度 | `daily-gray` 与旧常驻 `scheduler` 同时具备写入能力，存在双 writer |
| actuals | 独立 actuals plist 与常驻 scheduler 的 actuals job 重叠 |
| preflight | `v2-preflight` 一天重复运行四次，但没有解决 8 月 3 日刷新失败 |
| `t1_daily` | 当前精确 hash 在 DB 中未激活，因此 5Y/10Y 两个 target 被正确门禁拦截 |
| D-overlay | 缺 2026-08-01 一条 gray live；稳定排序修复已存在，但当前精确版本未激活 |
| D-overlay benchmark | 45 个 source benchmark 样本中有 14 个平台 DB 复现不一致；与 8 月 1 日排序缺口是两个问题。2026-08-04 已确认该漂移只作归档诊断，不是 G4 单独 blocker；fixed receipt 与后续 maintenance 已恢复 post-admission 证据 |
| Native 版本 | 约 111 条 active 版本覆盖约 26 个 base scheme，存在大量 active sibling |
| ledger 表 | `t_schedule_occurrences/items/targets` 当时均为空；后续明确不使用、不扩容，只在 P2 按授权退役 |
| 新 7Y 交付 | 原始 `001/002 v1` 两文件交付已冻结保留；经专项授权新增本地因果修订版 `001/002 v2`，不宣称 source-algorithm full parity |
| 新 7Y 平台状态 | 两个 v2 composite registry 均已 active；各有持久化回测 337 条和 `gray_live` 44 条，尚无 `scheduled_live` |

8 月 3 日的 13 个缺口由两类原因组成：11 个 DataBridge 方案未获得当日 artifact；`t1_daily` 一次执行对应的 5Y/10Y 两个 target 因精确版本未激活而跳过。

预测唯一业务键不包含 `prediction_phase`，所以两个 writer 对同一个 scheme/tenor/horizon/target date 写入时会互相覆盖 run 与 phase。这是数据完整性风险，不是单纯的重复日志。

---

## 4. 总体阶段与当前进度

| 阶段 | 目标 | 当前状态 |
|---|---|---|
| P-1 | 两套 7Y T+1 方案进入灰度实验室并前端可见 | **授权范围闭环完成**：本地 v2 已全 Gate、入库、历史回补、Dashboard 读回和 formal served-API Gate；未授予 scheduler admission，`scheduled_live=0` |
| G0 | 统一文档和治理口径 | **完成（开发分支）**：CURRENT 文档、SOP、部署说明和文档测试已收敛到 launchd-only 单 writer 口径；未修改 installed plist 或 loaded state |
| G1 | 恢复本机 MySQL → DataBridge 的可靠刷新 | **受控 publish/retry 已闭环**：sealed local MySQL current 与 V2 ready Gate 已读回；待自然时钟观察 |
| G2 | 收敛为 launchd-only 单 writer 调度 | **已受控切换**：旧 scheduler/daily-gray/v2-preflight 已退出，四个 one-shot label 已 loaded；待自然时钟观察 |
| G3 | 补齐 8 月 3 日日频缺口 | **完成**：15 个 `gray_live` 缺口已按 14 个冻结原子组回补；最终 T+1 `10/10`、T+5 `24/24`，DB、served API 与 Dashboard 均已读回 |
| G4 | 闭环 D-overlay 8 月 1 日缺口 | **完成：fixed receipt、六段 `native-maintenance`、独立 activation 与受控 signal-gap fill 均已闭环；run `2106` 写入唯一 `gray_live` key，DB、served API 与 dashboard 均已读回** |
| G5 | 挂载并验证周度、月度自然调度 | **已挂载，等待首次自然 weekly/monthly 触发证据** |
| G6 | 完成 P0 生产观察闭环 | **观察中**：仍缺完整 natural daily/weekly/monthly 三频周期证据 |
| G7 | 收敛 Native 版本模型 | **P1，未开始** |
| G8 | 删除 legacy/ledger/旧调度债务 | **P2，须在 P0 稳定后开始** |

当前执行顺序是 **P-1 → G0 → G1/G2 → G3/G5 → G6**；G4 已独立闭环。P-1 如果被 DataBridge 新鲜度阻塞，只允许把 G1 中满足这两套方案运行所需的最小前置修复提前，不得借机展开其它治理。G7、G8 不得抢跑进入 P0。

---

## 5. P-1 — 两套 7Y T+1 方案进入灰度实验室（第一优先级）

### 当前问题与状态

用户提供的交付目录为：

`/Users/macstudio0/Downloads/BLACKBOX_V2_7Y_INCREMENTAL_PASS_DELIVERY_V1/`

其中包含两套独立的 Blackbox V2 方案：

- `seven_y_current55_lgbm_001_v1`：7Y T+1 基准窗方案；
- `seven_y_current55_lgbm_002_v1`：7Y T+1 长窗方案。

每套方案各有一份同名 `.py + .json` 交付。平台 Intake 需要按方案分别处理，不能把四个文件当成一个方案收包，也不能修改上游交付字节来迁就平台。

**原始交付状态（历史记录）：** 交付身份和文件完整性已只读确认；截至最初 Intake 前，仓库、Registry、版本、预测和回测中均没有这两个身份。

**当前状态（2026-08-03）：** 原始 `v1` 交付保持冻结，未被覆盖。用户已专项授权以独立的
本地 Blackbox `v2` 身份修订 T+1 时序和跨进程持久 cache；两套 v2 已完成受控入库、历史
回测、`gray_live` 回补和 Dashboard 读回。该修订保留原特征变换，但只声明为本地因果 V2
trial，不宣称完整 source-algorithm parity。

控制面风险仍需单独处理。当前仓库中的 `daily-gray` runner 会把 policy 外 active 日频身份隔离在
冻结的 28 个执行项之外，因此两套 v2 不会被该批次执行；但交易日发现隔离身份时 runner 会以
非零状态报告不完整批次。该行为避免了把 v2 意外挂入自然调度，却不构成 scheduler admission
或“原有自然运行完全不受影响”的证明；本轮没有核验或变更 installed plist / loaded state，也
不能为迁就它建设 ledger、扩容 frozen policy 或把两套方案纳入日常调度。

### 造成的影响

- 灰度实验室的 `7Y国债活跃 × T+1` 任务格子缺少这两套新候选，无法尽早观察和比较。
- 如果继续等待整个调度治理完成，算法的真实问题也无法通过前端灰度数据尽早暴露。
- 如果只登记为 shadow/paused，前端 active-only API 不会返回它们，不能满足“上到灰度实验室”的要求。
- 如果直接激活却不处理旧 daily-gray 的 exact active-set 约束，现有全部日频方案可能在下一次触发时整批失败。

### 为什么必须解决

灰度实验室的第一性目的就是让候选方案尽快进入隔离、可观察的真实数据链路。效果不佳、参数仍需观察或存在非致命算法问题，都应该在灰度中暴露，而不是在平台外无限等待；平台只需要守住不会污染数据、不会越过输入截止、不会破坏其它方案的最低边界。

因此，本阶段追求的是 **GRAY_VISIBLE**，不是一次性宣称方案已经完全正确或生产稳定。

### 什么叫解决完毕

- 两套 v2 交付分别通过 Blackbox V2 Intake；原始 v1 `.py/.json` 字节及其 scheme identity 保持冻结不变。
- 最低平台门槛通过：Contract 可执行、结果可追溯、相同输入确定、feature cutoff 隔离有效、无越权写库或网络副作用。效果、准确率和非致命算法缺陷不作为本阶段阻塞项，只需如实记录。
- 两套 v2 exact version 和 composite Registry 均为 active，业务身份分别为：
  - `seven_y_current55_lgbm_001_v2__h1__7Y`
  - `seven_y_current55_lgbm_002_v2__h1__7Y`
- 每套方案至少存在一组可供前端读取的有效历史回测和一条经授权写入的 `gray_live`，且 backtest/live target 不重叠。
- 本地及实际使用的灰度实验室 API 都能返回两套方案；前端 `7Y国债活跃 × T+1` 格子能够选择两套候选，并正常显示回测、灰度明细和待验证状态。
- 已知 bug、效果风险、数据口径限制和未完成项写入专项灰度记录，页面和记录不得把 waiver、pending 或失败伪装成通过。
- 激活前已经完成最小调度隔离：旧 daily-gray 不会因为新增 active 身份而整批失败，也不会执行这两套尚未获调度权的 7Y 方案；现有日频方案的自然运行能力不受影响。具体采用何种最小方式，必须基于当时 installed plist 和 loaded state 重新判断。
- 本阶段不授予两套 7Y 的 scheduler admission，不产生 `scheduled_live`，也不建设 ledger 或扩容 frozen 28/32 清单。是否纳入日常定时调度，留到 G1/G2/G6 在 DataBridge 和单 writer 治理完成后决定；届时仍只能使用 launchd + plist。

若当前 DataBridge 无法提供满足截止要求的输入，本阶段只能先执行必要的最小刷新修复；若旧 writer 阻止安全激活，只能先执行必要的最小调度隔离。两者都属于 P-1 的前置依赖，不得扩张成其它治理，也不得使用 stale artifact、伪造回测或 gray live。Activation、回测持久化、gray live、backend reload、installed plist 或 `launchctl` 变更等生产副作用仍须逐项取得授权。

### P-1 执行记录（2026-08-03，开发分支）

本轮已按两个独立的两文件收包目录完成 `seven_y_current55_lgbm_001_v1` 与
`seven_y_current55_lgbm_002_v1` 的 Blackbox V2 Intake。仓库内 delivery 与经用户
授权日历修复后的交付文件逐字节一致；两个 `config.yaml` 均为
`runtime_type=blackbox_v2`、`platform_inputs=[api-wind-date-v1]`、`status=paused`、
`version_status=draft`。这只是代码库 Intake，未写 Registry/version/prediction/backtest，
也未激活、重载或变更任何调度控制面。

两方案均已通过 Static、Input 和 Unit Gate。Input 使用的同一只读快照为
`generation_id=full-20260802-161326-1a919c484947`，其日频截止为 2026-07-31；对应的
合法日频 T+1 请求为 `predict_date=2026-08-03`、`feature_date=daily_cutoff=2026-07-31`。
两方案的 Dry-run 均确定性失败：delivery 先将 daily 输入严格截到 feature cutoff，随后
在 `Engine.predict` 中无条件访问 `feature_idx + 1`，因而报
`feature_date has no next trading-day model index`。此问题不是平台日历/I/O 适配问题；
向 artifact 提供未来一行会违反 feature-date 硬截止，直接改为当前行又会改变交付算法的
标签、训练和反转统计语义。

因此**当时**的 v1 P-1 记录停在 Contract/Dry-run 门槛；不得用空壳 active、伪造 gray live
或放宽截止绕过。最初路径要求上游书面确认 T+1 live 站位并提供修订后的两文件交付（不可原地
覆盖已经 Intake 的不可变 delivery）；随后用户以本文件下一节记录的专项授权，改为新的本地
V2 identity 并重新执行完整 Gate。cache 的跨调用持久化设计也不得作为该失败的绕过方式。

用户随后在 2026-08-03 明确授权本地语义修订，继续完成受控入库、合规历史数据回补与前端
可见性更新；该授权不包含 installed plist、`launchctl` 或定时调度 admission。收包必须使用
新的独立目录，不能覆盖本段所述失败 delivery；除完整自动 Gate 外，还必须证明 feature cutoff
后数据不影响结果、两个冷进程结果一致，且没有隐式跨进程模型 cache。原下载目录仍只有与失败
Intake 版本逐字节相同的四个文件；本地 v2 是单独的新 identity，而非对它们的覆盖。

### P-1 本地语义修订执行记录（2026-08-03，专项授权范围）

在上述原始 v1 交付失败后，用户明确授权不等待上游，以新的本地 Blackbox V2 identity
完成语义修订。新增身份为 `seven_y_current55_lgbm_001_v2` 和
`seven_y_current55_lgbm_002_v2`；v1 四个 delivery 文件的 SHA256 均已复核未变。V2 将
`feature_date=T` 固定为模型状态和出信号行，目标为下一交易日 `T+1`；训练标签严格早于
该 feature 状态，反转统计同样使用 T→T+1 标签。三频输入继续通过平台
`api_wind_date.csv` 映射日历，不在算法内写死周历；去除了 pickle、`--cache-dir`、磁盘模型
cache 和其他跨进程持久化。修订没有改变 v1 文件，也不以结果对齐为由修改 Native/source
算法逻辑。

两套 v2 均已完成独立 Intake，并以同一只读 DataBridge 快照通过
`harness onboard --stage all` 的 Static、Input、Unit、Dry-run、Compare、Backtest、
API-readiness 七段 Gate：

- `001_v2`：scheme version `cd0624ef3ead`，harness run
  `hr_20260803T084404Z_3b5dd72461f4`；
- `002_v2`：scheme version `57e956513471`，harness run
  `hr_20260803T084511Z_2bc5609fe2b2`。

专用时序测试还验证了：feature cutoff 当天没有 target 日 daily 数据时仍可运行、追加未来三频
数据不改变结果、冷进程重复调用确定、且不产生 `.pkl` 或 `.blackbox_model_cache`。独立规格和
质量审查均未发现可验证的 P0/P1/P2 问题。

受控 lifecycle 已依次完成 draft register、shadow register、持久化回测和 activate。两套均为
`active` registry：

- `seven_y_current55_lgbm_001_v2__h1__7Y`；
- `seven_y_current55_lgbm_002_v2__h1__7Y`。

持久化回测分别为 run `203`、`204`，均使用
`blackbox_v2_current_snapshot_as_of`，各有 337 条预测、17 个按月指标；目标日期范围均为
2025-01-03 至 2026-05-29，`target_date >= 2026-06-01` 的回测行数均为 0。随后基于现有
平台交易日历回补 2026-06-01 至 2026-07-31 的 44 个历史 `gray_live` target：每个 v2
均写入 44 条、无重复 target、与回测 target 无重叠，且 `scheduled_live=0`。`001_v2` 在
2026-07-22 首次写入前因源 daily 表在 precommit 期间变化而被 LiveGate 安全拒绝，未写任何
prediction；随后重试成功，最终 44 个 target 完整。`002_v2` 的 44 次均首次成功。

当前运行中的前端服务已直接读回两个 active composite identity：`/api/schemes`、各自的
`/api/metrics/{composite_scheme_id}` 以及 `/api/factor-lab/dashboard` 均返回 HTTP 200；Dashboard
snapshot 为 fresh，显示正确的 `7Y`、`T+1`、horizon 1、active 状态和各 44 条 gray 记录。

此前 formal served-API Gate 的两个环境性阻断已在 2026-08-03 受控闭合：Harness 改为仅按
已验证 `benchmark_id` 查询 backtest，避免全量响应超过 1 MiB；经用户专项授权，installed
backend plist 仅新增固定实例 nonce，并完成 `bootout → bootstrap → kickstart` 的受控重载。
重载后服务实例指纹与 installed nonce 匹配，两套 v2 的 fresh formal Gate 均通过：health、
schemes、metrics 和精确 backtest API 均为 HTTP 200，Registry/live/backtest 均可见。
此证据只证明 served API 与灰度链路闭合，不授予 scheduler admission，也不产生
`scheduled_live`。

本轮没有执行当前日期的 `live_write`，也没有修改 installed plist、调用 `launchctl`、重启服务
或赋予 scheduler admission；因此两个方案目前只有经授权历史 `gray_live`，没有
`scheduled_live`。仓库级单测已验证 daily-gray 会把两个 v2 从冻结的 28 个执行项排除；但它在
交易日仍会因存在隔离身份返回非零状态。该隔离实现仅是代码层保护，不在这里宣称已验证任何
installed 控制面的加载状态或既有自然批次的生产观察结果。

---

## 6. G0 — 统一文档和治理口径

### 当前问题与状态

根规范已经说明 launchd + plist 是真实生产控制面，但部分“当前”架构、SLA、SOP、部署说明和文档测试仍把 ledger、daily-gray 冻结 policy 或常驻 scheduler 当作目标方案。本文属于状态记录，不能单独覆盖这些现行规范。

**状态：已完成（开发分支）。** CURRENT 架构、SLA、SOP、部署说明与文档测试已统一；
旧 ledger/daily-gray 内容只保留为明确的历史或待退役兼容语境。该文档收敛没有修改
installed plist、`launchctl` state、数据库或生产信号。

### 造成的影响

- 同一个问题会得到相互冲突的实现方案。
- 日频 Native 激活可能被要求继续更新即将退役的 frozen policy。
- 测试可能强制保留已经不再符合生产方向的 daily-gray/ledger 设计。
- 交接会依赖口头记忆，新会话容易重复犯错。

### 为什么必须解决

从第一性原理出发，代码、运维和验收必须共享同一个生产事实模型。若“谁负责触发、谁有写入权”在文档层都不唯一，就无法证明系统只有一个 writer，也无法安全删除旧路径。

### 什么叫解决完毕

- 所有标为“当前”的架构、SLA、SOP、部署说明和对应文档测试都一致表达 launchd-only 目标。
- ledger/daily-gray 内容若需要保留，只能明确标为历史背景或待退役兼容，不再作为新功能、过渡方案或生产入口。
- Native 激活 SOP 不再要求维护即将退役的 daily-gray 冻结清单。
- 本文进入状态记录索引，新会话可以从项目文档入口找到它。
- 根目录 `AGENTS.md` 与 `CLAUDE.md` 继续保持一致。

### G0 执行记录（2026-08-03，开发分支）

- 新增当前[生产信号与调度治理](../../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)，明确
  `launchd + installed plist`、每个 cadence 单 writer、`gray_live`/`scheduled_live`
  分离、本机 MySQL DataBridge freshness fail-closed 与独立生产授权边界。
- `CURRENT_STATUS`、`TODO`、架构索引、Blackbox/Native SOP、部署说明和旧日频 SLA 已指向
  该治理合同；后者以及旧 ledger rollout 明确标为历史/不可执行，而非过渡方案。
- 文档契约测试已以新治理替换旧 ledger/epoch 目标断言，并保留历史证据索引校验；
  `tests/test_onboarding_docs.py` 为 `47 passed, 18 subtests passed`。

---

## 7. G1 — 恢复本机 MySQL DataBridge

### 当前问题与状态

现有生产刷新路径依赖 HTTP DataBridge；8 月 3 日该服务无法连接其上游 MySQL，导致当日 artifact 未发布。用户已明确 DataBridge 的本质就是从本机 MySQL 导出，本机必要源表已确认可读。

**状态：受控 publish/retry 与严格读回已闭环；自然时钟观察仍待完成。** 2026-08-04 已按专项授权安装并加载本机 MySQL DataBridge one-shot；首次且仅一次受控 `kickstart` 以 `feature_date=2026-08-03` 运行。它在旧 v1 current 的周度 key `202629` 与本机 source exact key `202630` 不一致处安全失败，未发布新 current、未写预测，旧 current 保持不变。取得新的单次 retry 授权后，第二次受控 `kickstart` 成功发布 sealed current：`refresh_date=2026-08-04`、`feature_date=2026-08-03`、daily max `2026-08-03`、weekly max `202630`、monthly max `202608`，同日 V2 Gate 已为 `ready`。这证明本机 MySQL 路径和迁移边界修复生效；daily/weekly/monthly 的自然时钟证据仍不得以手工补跑替代。

### 造成的影响

- 依赖 DataBridge 的 11 个日频业务 target 在 8 月 3 日缺失。
- 周度和月度即使按时触发，也可能因为 artifact refresh date 不满足要求而失败。
- 如果为了出信号而放宽新鲜度，会把旧数据结果伪装成当日预测。

### 为什么必须解决

信号系统最基本的输入契约是“数据已按目标时点完整截止”。既然真实数据源就在本机 MySQL，最短可靠链路应是 MySQL → 标准 DataBridge artifact → 方案，而不是依赖一个当前失效的额外 HTTP 跳板。

### 什么叫解决完毕

- 由一个 launchd one-shot 任务每日从本机 MySQL 生成并原子发布标准日/周/月 artifact。
- 刷新过程继续执行六表存在性、schema、连续性、稳定轮次和输入截止校验，不复制或弱化现有验证规则。
- 工作日、周六和自然月 15 日都能获得符合当日 refresh 语义的 artifact；数据最大日期仍严格截止到应有的上一交易日。
- 数据缺失、源表异常或两轮结果不稳定时明确失败，不发布半成品，也不回退旧 artifact。
- 至少一次真实 launchd 触发的日志和 current state 证明本机 MySQL 路径已生效；HTTP 路径不再是生产依赖。

### G1 开发执行记录（2026-08-03，未触碰生产控制面）

- 新增本机 MySQL round exporter：每一个稳定性 round 都在单一 `REPEATABLE READ`、
  `WITH CONSISTENT SNAPSHOT, READ ONLY` 事务内完成三频构造、来源检查与水位证据采集；实际检查六张因子表、metadata 和两张日历依赖。
- 日频 artifact 的最大日期现在必须**等于** feature cutoff；日/周/月原始读取和输出构造均以
  `rdate <= feature_date` 截断。两个连续 round 除 output digest 外还必须匹配本机 source token。
- current publication 新增可兼容读取的 v2 marker：`source_mode=local_mysql`、feature cutoff 和规范化 source evidence 与 marker 一起原子封存；旧 v1 current 只可作为连续性基线，不能满足新的 strict freshness read。
- direct refresh CLI 改为共享交易日历 + 本机 MySQL source，不再导入 `scheduler.main` 或 HTTP client；`--publish` 先写 blocked、发布后 strict read 再写 ready，异常会覆盖为 blocked，且不 restart/kickstart scheduler。V2 consumer gate 也改为 strict read + sealed local provenance。
- 开发验证覆盖 DataBridge、CLI、V2 gate、generation、data-contract 与相关 scheduler；最新合并回归为
  `154 passed, 151 warnings, 36 subtests passed`。没有执行真实 `--publish`、installed plist 变更、`launchctl`、服务重启、数据库写入或预测补数。
- `SourceCommitEvidence` 证明的是同一一致性快照和可用 create-time/metadata 水位；当前通用因子表证据不声称检测 `create_time` 不变的原地更新，后续如需该性质必须先完成字段能力审计。

### G1 受控挂载与边界修复记录（2026-08-04）

- installed `com.bond-factor-lab.data-bridge-refresh` 已 loaded；首次运行的日志仅记录脱敏失败类别，且 `v2_scheduler_gate/2026-08-04.json` 保持 `blocked`。现场只读核对确认 source 同一 RR snapshot 可解析 weekly `202630`、monthly `202608`，但旧 v1 current 的 weekly max 为 `202629`；这正是首次发布被拒绝的原因。
- 为修复这一**旧 v1 迁移边界**，连续性 authority 仅在 verified v1 manifest、`publish=True` 且 `BFL_DATABRIDGE_PRODUCER=launchd-one-shot` 时允许周/月向后选择不前跳的 predecessor。普通 dry-run、harness 与 sealed v2 current 仍严格要求 exact key。
- authority 会同时冻结 fallback 前 source exact weekly/monthly key 并绑定其摘要；稳定候选选定后、state 构造及 publish 之前必须实际包含该 key。这样既不强留旧 current 的 future-labelled monthly key，也不会把仍缺 `202630` 的候选发布成新的 v2 current。
- 开发验证使用 `bond_factor_lab_service` Python 3.12：372 个 DataBridge、input-artifact、launchd 和 scheduler 相关测试通过；默认 Python 3.13 因缺少 `apscheduler` 无法导入 `scheduler.main`，不作为代码回归依据。未执行第二次 `kickstart`、publish、预测写入或任何其它生产副作用。
- 在新的单次 retry 授权内，仅执行一次 `launchctl kickstart -k gui/501/com.bond-factor-lab.data-bridge-refresh`。现场从 `runs=1 / last exit=1` 变为 `runs=2 / last exit=0`；raw state、publication manifest 和 `v2_scheduler_gate/2026-08-04.json` 共同绑定 generation `full-20260804-233740-8e4cf7fb94e9` 与同一 business digest。两轮稳定导出均完成，sealed local MySQL provenance 的 feature cutoff 为 `2026-08-03`；没有触发预测、actuals 或其它 launchd job。
- 读回时发现 `--check-only` 的白名单投影把成功 state 中合法的 `last_attempt.error=null` 误显示为 `refresh_failed`。原始 state、strict current read 和 V2 Gate 均未受影响；已以最小修正保留该显式 `null`，失败/缺失/非白名单 error 继续脱敏为 `refresh_failed`。回归验证为 94 个 DataBridge current/CLI/authority 测试和 42 个 V2 Gate、input generation、MySQL exporter 测试全部通过。

---

## 8. G2 — 收敛 launchd-only 单 writer 调度

### 当前问题与状态

8 月 3 日现场同时存在 daily-gray 和一个未随源码更新而重启的常驻 scheduler；两者均可能写日频。actuals 也由常驻 scheduler 与独立 plist 重复触发。周/月仍依赖常驻 APScheduler，尚未由独立 plist 承担。

**状态：受控切换已完成，尚未形成真实时钟闭环。** 2026-08-04 已以 installed plist 和 `launchctl` 现场状态为准：旧 `com.bond-factor-lab.scheduler`、`com.bond-factor-lab.daily-gray`、`com.bond-factor-lab.v2-preflight` 均已 bootout，installed template 已置 `Disabled=true`；新的 `data-bridge-refresh`、`daily-predictions`、`weekly-predictions`、`monthly-predictions` 均已 loaded 且尚无自然执行。既有 `actuals` 保持 loaded、最后退出为 0。这只证明控制面已切换，不证明 DataBridge、预测或周/月任务已在真实时钟成功运行。

### 造成的影响

- 同一业务键可能被不同 phase/run 覆盖，破坏审计链。
- resident process 可能继续运行旧内存代码，源码正确也无法保证生产正确。
- 服务重启和 startup catch-up 可能产生不可预测的补跑。
- 周/月任务是否会执行依赖常驻进程状态，不符合用户要求的简单生产模型。

### 为什么必须解决

从第一性原理出发，一次业务时点只能有一个明确的触发者和一个 writer。launchd 已能提供时钟、进程启动、日志和现场状态，就不需要再叠加第二个常驻调度控制面。

### 什么叫解决完毕

生产信号相关控制面收敛为以下职责，不再互相重叠：

| 职责 | 目标触发语义 |
|---|---|
| DataBridge refresh | 每日约 06:30，一次运行后退出 |
| daily predictions | 交易日约 07:03，一次运行后退出 |
| weekly predictions | 周六 11:30，一次运行后退出 |
| monthly predictions | 自然月 15 日 18:00，一次运行后退出 |
| actuals | 保留既有三个时间点，但只有一个 writer |

并且满足：

- 旧常驻 scheduler、daily-gray 和重复 v2-preflight 不再 loaded，也不再拥有生产写入机会。
- 新预测任务按运行时 active discovery 选择方案，不冻结 scheme ID 数量或 scheme hash 快照；方案数量变化时明确报告，而不是静默漏跑。
- runner 不包含自己的 cron、ledger、epoch 或 startup catch-up；自然触发只写 `scheduled_live`。
- G2 不存在“先切到 ledger、再切到 launchd”的中间阶段；所有迁移直接收敛到 launchd + plist。
- 每个频率有进程级防重入和清晰退出状态；单方案失败可定位，整批不能伪装成功。
- installed plist、loaded state、日志、run 和 prediction 五类证据相互一致。

P0 可以暂时保留底层 `legacy` 环境开关，以兼容现有 executor/repository，但绝不切换为 `ledger`；只有旧调度器仍在生产触发，才叫“legacy 控制面未清除”。该兼容开关在 G8 处理。

### G2 开发执行记录（2026-08-03，未触碰生产控制面）

- `scheduler.main` 的 DataBridge writer、startup refresh/catch-up 与 `v2_daily_preflight` 的正常 writer
  入口已退役为稳定的非写入结果；repository desired templates 中旧 resident scheduler、daily-gray 和
  v2-preflight 均为 `Disabled=true` 且没有自然日历触发。
- 新的 DataBridge publisher 只走本机 MySQL direct CLI，并以全局 publisher lock 覆盖 pre-block →
  refresh → strict reread → ready/blocked；新的 daily/weekly/monthly runner 以全局 nonblocking lock 覆盖
  strict discovery、精确 Blackbox admission、V2 Gate 和逐方案执行。daily 使用共享交易日历在非交易日
  零执行；weekly 是 launchd `Weekday=6`（周六）11:30，monthly 是自然月 15 日 18:00。
- `launchd_one_shot` 只授予冻结的 formal daily/weekly Blackbox 精确身份；7Y 与 gray Blackbox 在
  Gate、engine 和执行之前被拒绝。natural run 只能由 runner 内部传入 one-shot control plane；executor
  CLI 不接受该参数，任何无 `schedule_item_id` 的 `scheduled_live` repository 创建也只允许该 plane。
- 新增的 repo desired plist 分别定义 DataBridge 06:30、daily 工作日 07:03、weekly 周六 11:30、monthly
  自然月 15 日 18:00；actuals 保留 08:30/19:00/23:45 的既有 one-shot template。部署文档只说明 desired
  state 与授权前只读核对，不提供安装/重载命令。
- `BFL_DATABRIDGE_PRODUCER=launchd-one-shot` 是操作准入标记而非 launchd 身份认证；同 UID 的任意受信任
  Python 代码可调用内部 API 或伪造环境标记，因而仓库测试不把它称作“真实 launchd origin proof”。这一
  same-UID trust boundary 已被明确保留；生产 writer 身份只能在专项授权下由 installed plist、loaded state、
  日志、run 和 prediction 的一致性观察证明。
- 本记录只说明开发分支代码/模板与测试。没有执行 real publish、installed plist 修改、`launchctl`、服务
  restart、`scheduled_live`、Registry 变更或 G3 历史补数。

### G2 受控切换记录（2026-08-04）

- 切换前已保存 installed plist 备份；旧 writer label 已从 loaded state 移除，不能再与新的 cadence one-shot 并发写同一业务键。该切换不修改算法、Registry、历史预测或 actuals 数据。
- G1 的第二次、独立授权 DataBridge retry 已成功，V2 ready Gate 也已严格读回。daily/weekly/monthly 尚未经过自然时钟；它们的 `runs=0` 不是缺陷结论，只表示尚无可作为生产闭环证据的自然执行。
- 下一步只观察 DataBridge、daily、weekly、monthly 和 actuals 的自然时钟日志、run、prediction 与 API/页面读回；不能用手工补跑代替自然时钟证据。

---

## 9. G3 — 补齐 2026-08-03 日频缺口

### 当前问题与状态

8 月 3 日初次审计时，T+1 为 3/8、T+5 为 16/24，共缺 13 个业务 target。11 个由 DataBridge 失败造成；另外 2 个是 `t1_daily` 的 5Y/10Y，因当时当前精确版本未激活而被门禁拦截。

**状态：已闭环。** 13 只是历史快照。2026-08-04 在 DataBridge strict current 发布、G2 单 writer 切换及 `t1_daily` 激活后，重新计划得到日频 T+1=10、T+5=24，共 34 个应发 target；19 个已存在，15 个 `gray_live` 缺口构成 14 个原子算法组。所有 14 组均按冻结计划完成，最终为 34/34 present、0 个 `GRAY_LIVE_GAP`；这不替代后续自然 `scheduled_live` 验收。

### 造成的影响

- 前端当日方案覆盖不完整，后续指标与实盘观察链断裂。
- 手工绕过 activation 或 repository 会制造不可追溯版本和 run。
- 若在双 writer 尚未解除时补数，补好的记录仍可能被旧进程覆盖。

### 为什么必须解决

业务所需的不是“批次大部分成功”，而是 active Registry 中每个应发 target 都存在且可追溯。补数必须恢复这个业务事实，同时保留真实失败历史，不能通过改状态或裸 SQL 掩盖事故。

### 什么叫解决完毕

- 在 G1 可提供合格输入、G2 已消除并发 writer 后，重新枚举 8 月 3 日真实缺口。
- `t1_daily` 当前精确版本完成既有全量 Gate 和受控激活；不能手改版本表。
- 仅对仍缺失的 business key 进行授权补数，历史修复统一标记 `gray_live`。
- 8 月 3 日最终读回为 T+1 10/10、T+5 24/24，且 feature date、target date、scheme version、run 和 artifact provenance 正确。
- 已存在的记录没有被覆盖；补数失败会停在明确方案，不继续伪造全量成功。

### G3 开发与准入记录（2026-08-04）

- `harness.signal_gap_plan` 已移除 44 target / 40 execution / daily=29、weekly=7、
  monthly=8 的过期静态数量断言。它仍只读取 `status='active'` 的 Registry，仍保留
  每个 target 的版本、hash、runtime、task/horizon/frequency、输入 authority 和观测契约
  校验；若 active Registry 为空则以 `NO_ACTIVE_REGISTRY_TARGETS` fail-closed。
- 新的隔离回归以 53 target / 50 execution（daily=32、weekly=13、monthly=8）验证动态
  范围可通过，同时验证空范围与 T+1 task/horizon 漂移仍被拒绝。两轮独立代码审查均未发现
  越出最小边界的修改。
- `t1_daily` 以 paused current candidate `5041d725097a` 完成完整七段 `all`（run
  `hr_20260804T161423Z_dc051c3f5bd0`），随后经独立 ActivationGate 进入 active current
  `7898b9e47a9a`；其 5Y、10Y 两个 composite Registry 均已读回 active，formal API Gate 也已通过。
  paused Native 的 UnitGate 例外严格仅限这一次 `t1_daily` 预激活路径，且只排除三项已退役的
  daily control-plane 测试；active `t1_daily` 和任何其它 paused Native 仍保留完整测试选择。
- 对 `2026-08-03` 的最新只读 `signal-gap-plan` 已无 live blocker：active scope 为 56 target /
  52 execution（daily=34、weekly=14、monthly=8）；34 个日频应有 case 中 19 个已存在、15 个为
  `GRAY_LIVE_GAP`、`blocked=0`，计划 SHA-256 为
  `3957f8251a0803ba20f7de028441cdca621f22e2290f9449fde548fd0f19c9c7`。其中 13 个 Blackbox 单 target
  组使用 sealed DataBridge generation `full-20260804-233740-8e4cf7fb94e9`；`t1_daily` 的两个 target
  共用已验证的 Native current-snapshot artifact `native-cc249e2aec88fad7bcfc7c1c`。canonical-only 的
  历史诊断不与本窗口的 live action 相交，因而不阻断本次 fill。
- 本记录不等于历史补数或自然调度恢复。下一步仍须在临近执行时重新冻结同一窗口计划，并为 14 个
  `(base_scheme_id, predict_date)` 原子组签发各自绑定 plan SHA、exact version、完整 target multiset 与
  source authority 的一次性 `signal_gap_fill_write` token；fill 只写 `gray_live`，不触发 launchd。

### G3 受控补写与读回记录（2026-08-04 至 2026-08-05）

- 经逐组授权，14 个冻结原子组写为 runs `2121` 至 `2134`：13 个 Blackbox 单 target run 与
  `t1_daily` 的一个双 target Native run `2128`，合计新增 15 条 `gray_live` prediction。初始失败的
  runs `2107` 至 `2120` 保留为历史审计证据，未被改写或删除。
- 写后重新生成同一业务窗口的只读计划，日频 T+1 为 `10/10`、T+5 为 `24/24`，共 `34/34` present；
  无 `GRAY_LIVE_GAP` 或 live blocker。所有补写仍为 `gray_live`，没有产生 `scheduled_live` 或调用
  launchd runner。
- 对 13 个已成功 Blackbox run 发现的历史 `t_scheme_runs.data_snapshot_id` 缺失，仅以其既存
  canonical request、exact active version、完整 record count 和唯一 snapshot 进行受控 provenance
  回填；不修改 prediction、Registry、version 或业务键。新写路径已在同一事务中绑定 run snapshot，
  并以预测日期与 run 日期一致性 fail-closed。
- 2026-08-05 经独立授权受控重启 backend 后，`one_y_t1_quote_state_hv_v1` 的 formal served-API Gate
  通过：实例 fingerprint 精确匹配当前服务，health/schemes/metrics/backtest 均为 HTTP 200，Registry、
  45 条 live rows、backtest 与 current snapshot 均可读。`/api/factor-lab/dashboard` 同时读到该 1Y
  identity 与 `weekly_10y_d_overlay_0529__h6__10Y`，响应为 fresh 51,171 bytes。这证明 G3 的
  DB/API/页面读回闭环，不外推为自然调度证据。

---

## 10. G4 — 闭环 WEEKLY 10Y D-overlay 8 月 1 日缺口

### 当前问题与状态

`weekly_10y_d_overlay_0529` 当前缺的是一条历史实盘信号，而不是配置区间内的历史回测：

- `predict_date=2026-08-01`
- `feature_date=2026-07-31`
- `target_date=2026-08-07`
- `target_tenor=10Y`、`horizon=6`
- 应写 `gray_live`

跨年 week_id 的稳定排序修复已经存在，no-write 结果可以生成方向 `-1`、置信度 `0.32`；当前精确版本已在 2026-08-04 通过独立 activation，随后该信号已按受控边界写入业务表。

另有一个独立历史诊断：平台 DB 对原始 benchmark 的 45 个样本中有 14 个不一致，其中 3 周方向翻转。该漂移起点早于 8 月 1 日排序问题，不能把两者混为一个 bug。用户已确认：source benchmark/CompareGate 继续作为首次技术入库的证据；满足 post-admission 身份证据前提的当前 Native 修订，其 historical input-vintage 漂移只归档，不要求 scoped waiver 或同源输入/环境重建，也不单独阻断 G4。可是本方案的 legacy prior admission 没有可匹配的持久化 `static.business_identity`，不能仅凭当前 Registry 反推历史身份。

**状态：已闭环。** 算法排序问题已修；benchmark 政策已确认；固定 10Y canonical receipt、`hr_20260804T102103Z_91fa9e7db871` 的六段 `native-maintenance` 和 `native_post_admission_revision_v1` activation 均已通过。随后以 DB-`SEALED` current-snapshot 制品 `native-cc249e2aec88fad7bcfc7c1c` 冻结唯一计划并执行 `signal-gap-fill`；run `2106` 成功写入 `weekly_10y_d_overlay_0529 / 10Y / h6 / predict_date=2026-08-01 / feature_date=2026-07-31 / target_date=2026-08-07 / gray_live`，方向 `-1`、置信度 `0.32`、version `e50ad79a6c2f`。DB、served API 和 dashboard 已读回，postfill planner 为 14 条 `SKIP_PRESENT`、0 条 live gap。该记录不授予 scheduler admission 或任何其它业务写入权限。

### 造成的影响

- 周度实盘序列缺一条，页面和后续准确率观察不完整。
- 直接关闭 benchmark 校验会让系统错误宣称“源算法完全一致”；本政策不关闭首次技术入库 CompareGate。
- 为消除 benchmark 漂移而改 Native core、调参、改 benchmark 或退回旧 generation，会违反源算法保真原则并扩大 P0。

### 为什么必须解决

第一性原理要求同时满足两件事：应发信号不能缺失；验证结论必须诚实。首次技术入库已经保留 source benchmark/CompareGate，后续修订还必须用 prior `static.business_identity` 快照证明与当前 Registry 是同一业务身份，并证明输入截止、统一周历、日期语义、授权和 live-safe oracle。平台不把归档诊断伪装成 current benchmark pass，也不为了贴合旧 benchmark 改变原算法。

### 什么叫解决完毕

不再有 waiver/rebuild 的预先选择。G4 完成必须依次满足：

1. Native core 和 source-original benchmark 文件未被修改，排序修复仍通过 no-write 验证；唯一 canonical receipt 已以专用命令写入，仅影响 Harness run/gate 表，并已被 maintenance verifier 识别为 `legacy_operator_attestation_v1`。
2. exact version `e50ad79a6c2f` 已以 `hr_20260804T102103Z_91fa9e7db871` 完整通过六段 `native-maintenance`，并已由 ActivationGate 以 `native_post_admission_revision_v1`、精确 current version 和独立一次性 activation token 激活；DB version 与 Registry 均已读回 active，served API 返回 HTTP 200。receipt 绝不替代六段 Gate，maintenance 也不替代 activation。
3. 已在前两项完成后的独立专项授权下，通过受控 signal-gap 路径写入唯一业务键 `weekly_10y_d_overlay_0529 / 10Y / h6 / predict_date=2026-08-01 / feature_date=2026-07-31 / target_date=2026-08-07 / prediction_phase=gray_live`；没有扩展为任何其它日期、target 或 `scheduled_live`。
4. repository 原子提交后已从 DB 与 served API/前端读回日期、方向、置信度、版本、run 与 `gray_live` provenance；历史 benchmark 漂移仍标为归档诊断，没有写成 current Compare pass。

---

## 11. G5 — 挂载并验证周度、月度调度

### 当前问题与状态

周度 14 个、月度 8 个 active execution 已能被代码发现，但当时只由旧常驻 APScheduler 负责触发；用户需要的独立 launchd plist 尚未形成 installed/loaded/observed 证据。用户观察到 7 月 31 日可能缺失，也说明不能只看“配置里有 cron”。

**状态：功能验收已通过，进入非阻塞自然观察。** 2026-08-05 只读现场核对确认 weekly/monthly
的 installed plist 与仓库模板一致并已 loaded；两者仍为 `runs=0`、`last exit=(never exited)`，
但上文经授权的三 cadence 无写库控制面模拟已取代首次自然触发作为 G5 的阻塞证据。8 月 1 日周度
只读完整性计划为 14/14 `SKIP_PRESENT`、0 open gap、0 blocker，没有可在当前时点扩大为历史补写的
事项。

### 造成的影响

- 日频修好后，周度和月度仍可能不执行。
- 代码配置、仓库 plist 与现场 loaded state 可能各说各话。
- 缺失 7 月 31 日 feature date 的信号会破坏最近窗口展示和连续性。

### 为什么必须解决

一个方案“已上线”的最低条件不是配置中写了 cron，而是它有清晰且独立的 launchd 责任边界、正确的
输入与调用编排。真实生产时钟仍继续观察；本阶段功能验收按已授权的无写库模拟执行。

### 什么叫解决完毕

- 周度与月度均有独立 installed plist 和 loaded state，且旧 APScheduler 不再承担它们的触发。
- 当前 strict discovery/admission policy 的 weekly/monthly 无写库控制面模拟通过；所有 active
  方案完整分类为一次 dispatch 或合法排除，且不会触达数据库、子进程或 cache。
- 重新按 Registry 和交易日历检查全部历史实盘点，特别核对 `feature_date=2026-07-31` 附近；发现缺口时逐条解释并经授权补齐。
- 自然运行继续以 `scheduled_live` 记录、历史补齐保持 `gray_live`，并作为非阻塞观察，不混写。

---

## 12. G6 — P0 生产治理闭环

### 验收口径修订（2026-08-05，用户明确授权）

用户确认不以等待首次自然周六或自然月 15 日运行为 P0/G8 的阻塞条件。为保持
launchd-only 的最小边界，同时避免手工运行生产 runner，新增的验收证据必须是**测试内**
的无写库控制面模拟：它使用当前严格方案发现与精确 admission policy，分别覆盖
daily、weekly、monthly；以 fake engine、交易日历、V2 Gate 与 `execute_scheme` 替身阻止
DB 连接、业务写入、子进程与 cache 写入，并断言每个获准方案恰好一次以
`scheduled_live + launchd_one_shot` 编排、Liwei publisher 先于 consumer。

该模拟只替代 G5/G6 的**功能编排验收**，不伪称自然时钟已经发生；installed plist/
loaded state 继续是生产控制面证据，后续自然运行继续作为非阻塞观察与告警输入。模拟
通过前不得提前把 G5/G6 标为完成或启动 G8；模拟通过后，首次自然 weekly/monthly 不再
阻塞 G8 的代码与配置清理。它不授权手工 runner、业务库写入、cache 写入、installed
plist 或 launchd 变更。

### 无写库控制面模拟结果（2026-08-05）

`tests.test_launchd_prediction_runner.LaunchdPredictionRunnerTests.`
`test_real_active_scope_three_cadences_use_no_write_control_plane_simulation`
以当前 strict discovery 与 admission policy 验证 daily、weekly、monthly。每个 active
方案都恰好归入一次获准 dispatch 或合法 `control_plane_excluded`，没有 blocked、denied、
skipped 或 failed；获准项均以 `scheduled_live + launchd_one_shot` 调用，DataBridge Gate
只在获准的 `blackbox_v2 + data_bridge_current` 项上收到相应 predict/feature 日期，所有
Liwei cache publisher 均在非 publisher 前执行。测试的 engine 只允许 `dispose()`，其余
任何数据库属性访问都会失败；`execute_scheme`、V2 Gate、日历与锁均为替身，因此没有
真实 DB 连接、业务写入、算法子进程、cache 写入或 launchd 操作。

**状态：G5/G6 的功能编排验收已通过；首次自然 weekly/monthly 改为非阻塞观察。**

### 当前问题与状态

单项代码通过不能证明整条生产链稳定。DataBridge、daily、weekly、monthly、actuals、API/页面和日志必须共同经过一致的控制面与数据边界验证。

**状态：功能验收完成，进入非阻塞自然观察。** 2026-08-05 只读现场确认 DataBridge、daily、
weekly、monthly 均为新的 loaded one-shot，旧 scheduler/daily-gray/v2-preflight 均未 loaded
且 installed template 为 `Disabled=true`；DataBridge 已有成功 publish/strict-read 证据，actuals
已在切换后自然成功一次。上方无写库模拟已覆盖三种 cadence 的当前 active scope，取代首次
weekly/monthly 自然触发作为本 Gate 的阻塞条件。

### 造成的影响

若没有观察期，旧 writer、日期边界、周末/月中触发、环境变量或日志权限问题可能在发布后才暴露。此时贸然删除旧代码会失去可控回退路径。

### 为什么必须解决

系统需要同时具备单一 writer、正确参数编排、完整 scope 和可审计 provenance。真实生产行为仍
持续观察，但用户已选择以严格无写库模拟替代首次周/月自然时钟作为功能验收阻塞项。

### 什么叫解决完毕

- installed plist/loaded state 与 launchd-only 单 writer 现场状态一致；旧 scheduler、daily-gray
  和 v2-preflight 均不加载。
- 当前 strict discovery/admission policy 的 daily、weekly、monthly 无写库控制面模拟全部通过，
  且模拟不触达数据库、子进程或 cache。
- 已有历史 active target、actuals、API 与页面读回保持完整；后续自然批次若失败，仍能从日志直接
  定位原因。
- 每个 business key 只有一个自然 writer，没有旧 scheduler/daily-gray/actuals 双写证据。
- 页面/API 与数据库读回一致，未因 archived/paused 版本或错误 phase 展示旧结果。
- DataBridge 失败演练能够 fail-closed，且不会自动使用 stale artifact。
- 回退只涉及 control-plane，不删除 predictions、runs、versions 或 artifact；回退步骤已经过只读审查。

首次自然 daily/weekly/monthly 此后作为非阻塞观测：它们仍必须记录、异常仍须定位并修正，但不再
延后 G8 的代码与配置退役。

达到以上功能验收条件后，P0 可标记为完成。用户已于 2026-08-05 明确授权：届时在当前开发分支
完成验证与阶段提交后，可将精确候选以非强制 fast-forward 同步到 `master`，并推送开发分支与
`master`；自然时钟继续观察，但不再单独阻塞 G8。

---

## 13. G7 — P1：收敛 Native 版本模型

### 当前问题与状态

Native hash 当前覆盖完整 config 和文件文本，展示、状态、schedule、注释或删除死代码等非算法变化也可能生成新版本。Activation 又与 config 生命周期耦合，数据库因此出现大量 active sibling。

**状态：问题已确认，P1 尚未开始。** 当前版本表规模不是 P0 存储事故，不应为了“看起来干净”直接删除历史。

### 造成的影响

- 非算法修改也要求重新激活，增加漏跑风险。
- 多个 active sibling 让“当前生产版本”含义不清楚。
- 每次小修复都扩大操作面，维护者容易错误选择 created_at 最新记录。

### 为什么必须解决

算法版本的第一性定义应是“影响算法输入、计算和输出语义的内容”。展示、生命周期和调度元数据不应改变算法身份；历史版本应作为审计证据存在，但不能都充当当前生产版本。

### 什么叫解决完毕

- Native 使用经过验证的语义版本规则：算法、输入契约、目标和回测语义变化才改变版本；展示、状态、schedule、注释和格式不改变版本。
- 每个 Native base scheme 只有一个与当前代码完全对应的 active 版本；其余版本标记为 retired，不删除历史证据。
- 收敛前生成可审查清单，逐方案证明保留的是当前精确版本，不能按创建时间猜测。
- Blackbox 的不可变 delivery/version 模型不被顺带改造。
- 版本收敛不会改变任何预测值、前端业务身份或 source fidelity 结论。

---

## 14. G8 — P2：删除 legacy、ledger 和旧调度债务

### 当前问题与状态

仓库仍保留常驻 APScheduler、daily-gray frozen policy、v2-preflight、ledger/occurrence/epoch、serving pointer 和相关测试/文档。部分底层执行路径仍要求 `legacy` 兼容开关；空 ledger 表并不代表代码已经不可达，但用户已经明确这些能力不会再投入使用。

**状态：可在本次 G5/G6 功能验收提交后开始。** 自然周/月时钟保留为非阻塞观察；G8 仍必须按
“先删代码与配置、验证、最后删表”的顺序，不得删除历史业务或 Harness 证据。

### 造成的影响

- 新维护者可能误把旧模块重新挂回生产。
- 两套控制面增加认知负担、测试成本和误操作入口。
- 空表和无运行价值的策略代码会持续制造“是不是还在用”的不确定性。

### 为什么必须解决

在 launchd-only 已被生产证明后，任何仍能表达另一套生产调度权的代码都会增加系统熵和事故面。最简单的稳定系统应只有一条可理解、可观测、可回退的生产路径。

### 什么叫解决完毕

- P0 功能验收已完成；旧路径的 installed label、runtime 调用和静态引用在 G8 结束时均为零，
  自然时钟观察不再单独阻塞该清理。
- actuals 等仍有价值的一次性能力先脱离常驻 scheduler，再删除常驻 APScheduler 入口。
- daily-gray、v2-preflight、ledger/occurrence/epoch 和 serving pointer 按“先替代并观察、再删代码、最后删表”的顺序治理。
- G8 结束前仍须从 executor/repository/DataBridge 移除 `BOND_DAILY_COORDINATOR_MODE=legacy` 的
  底层依赖；完成后生产 plist 才不再需要该兼容变量。
- 文档、测试和部署模板不再暗示旧控制面可用于新生产任务。
- 删除数据库表前已有零读写证明、备份/恢复方案和用户专项授权；历史 run、prediction、version、harness 证据不因清债被删除。

### G8.1 — 第一刀：已禁用 writer / preflight 退役计划（2026-08-05）

**目标：** 删除仓库中已不再承担任何 launchd writer 职责的 `daily-gray` 与
`v2-preflight` 链路，不改变实际安装状态、自然时钟、业务数据库或仍在使用的
`v2_daily_gate`。

**边界：** 本提交只删除下列已 disabled 的 runner、专属 policy / plist 和对应测试；更新
当前部署与架构文档以说明它们已经退役。历史 records / evidence 保留原文。`scheduler.main`
仍被 actuals plist 使用，`scheduler.main`、ledger / occurrence / epoch、`t_input_generations`、
`t_scheme_runs` 的 nullable 审计字段、migration `017/018` 与 serving pointer 均不在本刀范围。

**文件结构：**

- 删除：`scheduler/daily_gray_runner.py`、`scheduler/daily_gray_launchd_policy.py`、
  `scheduler/v2_daily_preflight.py`、`deploy/daily_gray_launchd_policy_v1.json`、两个 disabled
  legacy plist 及其专属测试模块。
- 修改：`tests/test_architecture_boundaries.py` 固化“第一刀遗留物不得回归”的 fail-closed
  断言；`tests/test_prediction_launchd.py`、`tests/test_onboarding_docs.py` 与
  `harness/gates/unit_gate.py` 清除已删除模块 / 模板的当前契约；当前部署、架构与本计划文档
  只描述保留的控制面。

- [x] 先在 `tests/test_architecture_boundaries.py` 写入一个失败断言，精确要求上述三个 Python
  模块、daily-gray policy 和两个 legacy plist 均不存在；运行该单测确认它在删除前失败。
- [x] 删除第一刀遗留物；保留 `scheduler/v2_daily_gate.py` 与 daily / weekly / monthly
  `scheduler.launchd_prediction_runner` 入口不变。
- [x] 更新当前测试与文档，删除对已移除 runner / template 的“当前存在”断言；不改历史记录。
- [x] 运行该架构测试、预测 launchd / 文档 / harness selector 相关测试、G5/G6 无写库模拟、
  `compileall` 与 `git diff --check`；完成后将本节标为已完成并单独提交。

### G8.2 — actuals 脱离 `scheduler.main` 的代码切片（2026-08-05，已授权）

**目标：** 让仓库 desired actuals plist 直接启动一个一次性 `scheduler.actuals_runner`，避免
actuals 进程加载 APScheduler / prediction CLI；现有 `scheduler.main --run-once actuals` 保留为
已安装旧 plist 的兼容入口，直到另一次独立的生产切换完成。

**边界：** 新 runner 只复现既有 actuals 日期语义：交易日三种 actuals 同用目标日；非交易日
daily / weekly 使用上一交易日，monthly 保留自然目标日。它不调用 `scheduler.main`、不启动
APScheduler、没有 cron / ledger / epoch 逻辑。updater 仍暂时通过既有 repository 访问数据库；
ledger import 的进一步脱钩留给后续切片，不能在本刀重写 repository。仓库 plist 的
`ProgramArguments` 和环境变量会更新，但 installed plist、loaded state、kickstart、手工 runner
调用和业务数据库均不在授权范围。

- [x] 新增 `tests/test_actuals_runner.py`，以 mock 锁定交易日、非交易日和 CLI 参数/错误码；
  已确认初始运行因 runner 不存在而失败。
- [x] 新增 `scheduler/actuals_runner.py`，将已有 actuals 计算与 CLI 以最小方式迁出；
  `scheduler.main` 仅委托该兼容函数，不改变旧 `--run-once actuals` 行为。
- [x] 更新仓库 actuals plist、其测试和当前部署/架构文档为新入口，移除仅为旧 main 兼容的
  `BOND_DAILY_COORDINATOR_MODE=legacy` 环境变量；未改 installed plist。
- [x] 已运行 actuals runner/main compatibility、actuals updater、launchd/document、架构边界与
  G5/G6 无写库模拟测试，以及 `compileall` / `git diff --check`；本提交后仍须独立授权与现场
  核验，才能切换 installed actuals plist。

### G8.3 — disabled legacy scheduler 模板退役（2026-08-05，repo-only）

**目标：** 删除唯一仍直接表达常驻 APScheduler 的仓库 desired 模板
`deploy/launchd/com.bond-factor-lab.scheduler.plist`，消除该 legacy 控制面表达；不涉及
backend、SSH 等非 writer 模板。

**边界：** 本刀只删除该 `Disabled=true` 模板及其“模板存在”测试/当前文档表述；不删除或
实质重构 `scheduler/main.py`。`scheduler.main --run-once actuals` 仍是已安装旧 actuals plist
的兼容入口。不得核对、替换或修改 installed plist、loaded state、服务、launchctl、业务数据库、
预测/actuals 日期语义，也不运行任何手工业务 runner。

**文件与验证：**

- [x] 先将 `tests/test_prediction_launchd.py` 改为精确断言该模板不存在，并在删除前运行该单测，
  确认它因模板仍存在而 RED。
- [x] 删除 `deploy/launchd/com.bond-factor-lab.scheduler.plist`；保留 daily / weekly / monthly
  prediction 与 actuals 的 desired one-shot 模板。
- [x] 仅更新 `deploy/README.md`、`docs/architecture/ARCHITECTURE.md`、
  `docs/architecture/CODE_ARCHITECTURE.md`、
  `docs/architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md` 的当前描述；不改其它历史 records。
- [x] 运行 prediction launchd、onboarding docs、architecture boundaries、actuals 目标测试，
  对每个剩余 `deploy/launchd/*.plist` 执行 `plutil -lint`，再执行相关 `compileall` 与
  `git diff --check`；全部通过。

**状态：** 已完成（repo-only）；仓库删除不代表 installed scheduler/actuals 已切换、停用或观察完成。

### G8.4 — root-only epoch transition operator 退役（2026-08-05，repo-only）

**目标：** 删除无 plist、生产 import 或 entrypoint 的 root-only
`scripts/daily_coordinator_epoch_operator.py`，移除一个不再采用的 epoch transition 操作入口。

**边界：** 本刀只删除该脚本；不删除或修改 epoch contract、genesis、rollout、shared mode、
daily runtime / ledger / repository、migration、数据库表、control-plane probe、DataBridge、backend
或环境变量。不得核对、替换或修改 installed plist、loaded state、launchd、服务或业务数据库，
也不运行任何业务 runner。

**文件与验证：**

- [x] 先在 `tests/test_onboarding_docs.py` 对精确脚本路径写入不存在断言；旧文件仍在时已确认
  单测 RED。
- [x] 删除 `scripts/daily_coordinator_epoch_operator.py`；保留其余 epoch 相关契约与现存代码。
- [x] `tests/test_onboarding_docs.py` 保留 daily runtime 的 direct-authority / 无 legacy-admission
  断言，并对脚本回归 fail-closed；`tests/test_architecture_boundaries.py` 的通用
  harness-vs-one-shot-admin-script 边界改用既有 `scripts.refresh_data_bridge_current`。
- [x] 已运行 `tests.test_onboarding_docs`、`tests.test_architecture_boundaries`、
  `tests.test_daily_control_plane_probe`、`compileall scheduler shared tests` 和
  `git diff --check`；全部通过。

**状态：** 已完成（repo-only）；不表示 installed 状态、launchd、数据库或生产服务发生变化。

### G8.5 — Native one-shot 子进程的 daily-mode 隔离（2026-08-05，repo-only）

**目标：** 消除 `BOND_DAILY_COORDINATOR_MODE` 从 launchd one-shot 父环境泄漏到 Native 算法子进程的
路径，同时保留底层 direct / ledger / manual / `gray_live` 兼容行为，不能把这次隔离混同为删除
daily-mode、ledger 或 DataBridge/cache 逻辑。

**边界：** `scheduler.executor` 只新增 default-False 的
`strip_daily_coordinator_mode` 参数，并在 `run_scheme_subprocess` 完成正常 allowlist 环境构造后仅
`pop` 该变量。`execute_scheme` 只在 `prediction_phase=scheduled_live`、
`scheduled_control_plane=launchd_one_shot` 且 `runtime_type=native_adapter` 时传入 `True`；Blackbox
和其它调用不传递该标记。仓库 desired 的 DataBridge、daily、weekly、monthly 四份 one-shot plist
删除该 key；backend 不动，actuals 原本也没有该 key。不得改 installed plist、loaded state、
launchctl、业务数据库、DataBridge 行为、cache 内容、ledger/repository 或 Native 算法。

**TDD 与证据：**

- [x] RED 基线证据：`HEAD` 的 executor 与该测试均不存在 `strip_daily_coordinator_mode`
  （`git grep` 无匹配、退出码 1）；新增 wrapper 断言传入该关键字，因此该基线不能满足它。接手时
  工作区已含实现，未为了重复 RED 临时回滚安全行为。
- [x] `tests.test_executor_run_id` 同时锁定默认父环境仍透传、显式 flag 的 Native 子环境已 scrub、
  `run_configured_scheme` 只将 flag 转给 Native runner，以及 `execute_scheme` 仅在 Native
  `scheduled_live + launchd_one_shot` 转发它；另以 mutation RED 后的 Blackbox one-shot case 锁定
  `scheduled_live + launchd_one_shot + blackbox_v2` 不会收到该 Native 专属 flag。
- [x] `tests.test_prediction_launchd` 将该 key 列入 forbidden 环境变量，对 DataBridge 与三份预测
  one-shot 模板全部执行断言；`tests.test_daily_direct_cache_runtime` 证明 mode 缺失仍按 legacy
  解释，未重写 cache。
- [x] 绿测：`tests.test_executor_run_id tests.test_executor_cli`（81）、
  `tests.test_prediction_launchd tests.test_launchd_prediction_runner`（15）、
  `tests.test_data_bridge_cli tests.test_data_bridge_refresh
  tests.test_data_bridge_strict_current_read tests.test_daily_direct_cache_runtime
  tests.test_onboarding_docs`（154）均通过；最终相关回归扩展为 16 个模块、326 项并通过。所有
  `deploy/launchd/*.plist` 的 `plutil -lint`、`compileall -q scheduler shared tests scripts`、
  `git diff --check` 与根规范一致性检查均通过。全量 `pytest -q -x` 仍在 11 项后停于既有的
  `test_active_scheme_contracts`：冻结的 `seven_y_current55_lgbm_001_v1/002_v1` 被 discovery
  找到却不在 active config 目录；本切片未改这两套方案或其发现规则，故该既有基线失败已单独保留。

**状态：** 已完成（repo-only）；没有核对或改变任何 installed plist、
loaded state、服务、launchctl、DataBridge publish、cache 或业务写入。

### G8.6 — `scheduler.main` retired DataBridge CLI 退役（2026-08-05，repo-only）

**目标：** 删除 `scheduler.main` 中已无仓库 desired plist 或生产调用、只会返回
retired JSON 的 legacy DataBridge 兼容 CLI，避免它继续表达第二个 scheduler/DataBridge 入口。

**边界：** 仅删除 `LegacySchedulerWriterRetired`、
`run_data_bridge_refresh_job`、`data_bridge_refresh_is_current`、`run_startup_tasks`，以及
`--run-once data-refresh` choice/early-return 和三项专属旧行为测试；保留
`test_scheduler_does_not_own_daily_data_bridge_refresh` 的负向 scheduler ownership 保护。
不改 DataBridge 实现或 cache、ledger/epoch、actuals、backend、Native 算法、migration 或业务 runner，
也不核对或改变 installed plist、loaded state、launchctl、服务或数据库。

**TDD 与证据：**

- [x] 先新增
  `RetiredDataBridgeCliTests.test_legacy_data_bridge_cli_is_absent_and_rejected_by_parser`；在
  `bond_factor_lab_service` 环境运行该单测时，旧实现因 stdout 输出
  `legacy-scheduler-writer-retired` JSON 而 RED（`1 failed`）。
- [x] 最小删除后，该回归断言四个 retired symbol 不再由 module 暴露，
  `--run-once data-refresh` 由 argparse 作为 invalid choice 返回 `2`、stdout 为空且 stderr
  含 `invalid choice`；同一单测 GREEN（`1 passed`）。该测试独立于
  `SchedulerMainTests` 的环境/patch fixture，独立 `unittest` selector 与 pytest selector 均通过。
- [x] 聚焦回归：`tests.test_scheduler_main` 为 `80 passed, 37 subtests passed`；
  `tests.test_architecture_boundaries tests.test_prediction_launchd
  tests.test_launchd_prediction_runner` 为 `29 passed, 3 subtests passed`。
- [x] 文档回归 `tests.test_onboarding_docs` 为 `47 passed, 22 subtests passed`；
  最终相关回归扩展为 13 个模块、298 项并通过。`compileall -q scheduler shared tests scripts`、
  全部 repo plist 的 `plutil -lint`、根规范一致性检查与 `git diff --check` 均通过。全量
  `pytest -q -x` 仍在 11 项后停于既有 `test_active_scheme_contracts`：冻结的
  `seven_y_current55_lgbm_001_v1/002_v1` 被 discovery 找到却不在 active config 目录；本切片未改
  这两套方案或其发现规则，故该既有基线失败已单独保留。

**状态：** 已完成（repo-only）；没有运行或改变业务 runner、DataBridge publish、cache、数据库、
installed plist、loaded state、launchctl 或服务。

---

## 15. 执行中的统一停止条件

后续模型在任一阶段遇到以下情况必须停止副作用，先更新调研结论并向用户报告：

- active 方案数量、当前 hash、缺口列表或 installed plist 与本文快照不同，且原因尚未解释；
- DataBridge 的 refresh date、feature cutoff、generation 或连续性校验不通过；
- 发现两个仍可能写同一 business key 的生产进程；
- 需要改 Native core、source-original benchmark 或算法参数才能让测试通过；
- D-overlay 在已写入 receipt 后未通过完整六段 Gate/activation 就补数，或把 receipt 当作 `native-maintenance`、activation 或业务写入授权；两种 activation profile 不得混用，receipt 不能解除其余停止条件；
- 操作需要数据库写入、激活、历史补数、launchctl 或 installed plist 变更，但没有专项授权；
- 工作区存在来源不明或与当前阶段重叠的用户修改；
- 只能通过降低门禁、隐藏失败或使用旧数据才能获得“成功”。
- 7Y 交付身份或字节发生无法解释的变化，或者方案连一条合法输出都无法产生；“允许有 bug”不能被解释为允许空壳 active、伪造前端数据或破坏平台安全边界。

---

## 16. 下一步

P-1 的已授权算法、数据、Dashboard 和 served-API 闭环工作以及 G0 文档统一已完成；G3 的受控 `gray_live` 回补与 DB/API/页面读回亦已闭环，G4 的固定 10Y canonical receipt、六段 `native-maintenance`、独立 activation 与唯一 `gray_live` key 的 run `2106` 已完成。G1 已成功 publish 并通过 strict read/V2 ready Gate，G2 的 installed 控制面已按授权切换，G5/G6 的三 cadence 无写库功能模拟已通过。下一步在本阶段验证和提交后，按 G8 的最小边界清理 legacy/ledger/旧调度代码与配置；自然时钟继续记录为非阻塞观察，不扩大 7Y scheduler admission，也不把 gray/API 证据混同为自然生产运行。
