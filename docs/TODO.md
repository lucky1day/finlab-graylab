# 统一后续推进计划

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-06

本文是未完成工作的唯一当前入口：定义优先级、并行关系、停止条件和授权边界。带日期的执行证据只保留在
[状态记录](records/status/README.md)，已验证事实只保留在[当前状态](CURRENT_STATUS.md)，生产调度规则以
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)为准。历史 `docs/superpowers/plans/`
和专项 status 记录不能替代本文的顺序或授权。

## 全局边界

- 当前开发分支为 `codex/audit-bugfixes-20260613`；`master` 合并、覆盖和远程推送均须用户再次明确确认。
- `launchd + installed plist` 是唯一生产调度控制面。仓库配置、无写库模拟或前端验收均不证明现场已挂载或
  已自然触发。
- 不新增 scheduler、ledger、occurrence、epoch 或 daily-gray 生产路径；不把历史 `gray_live` 修复伪称为
  `scheduled_live`。
- 没有相应的专项授权，不执行 launchctl、installed plist、服务操作、业务写入、持久化回测、DDL 或手工 SQL。
- 现有 `KNOWN_ISSUES` 修改、`.superpowers/`、`reports/operations/` 和诊断脚本是用户工作区文件；任何阶段
  都不得暂存或混入提交。
- 每个有副作用的子计划均须先重新核对 active scope、精确版本、输入 authority 和工作区；发生漂移即停止并
  重新规划。

## 已关闭基线（不重开）

### G3.1：日频覆盖闭环（2026-08-06）

- 五个精确 Blackbox identity 已仅获 `launchd_one_shot`；没有 legacy、ledger 或 direct capability，且没有执行
  launchd/plist/服务操作。
- 一次 Harness `signal-gap-fill` 已以 insert-only `gray_live` 补齐 2026-08-04/05 的 12 个 T+1 key / 11 个
  原子组。它实际完成的范围大于最初仅修复 2026-08-05 五条的设想；不得重复补写或扩大历史日期。
- 写后受限 gap plan 的 20 个键均为 `SKIP_PRESENT`；2026-08-03/04/05 均为 T+1 `10/10`、T+5 `24/24`，
  DB raw、Dashboard canonical、fresh API 和前端 `target_date` 行集一致。
- **Task 1 — 只读重新冻结与无写库验证**、**Task 2 — exact Blackbox admission**、Task 3 历史补写与
  Task 4 端到端读回均已完成。完整证据见
  [G3.1 闭环记录](records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md)。

G1/G2、G3 的 8 月 3 日补写、G4、G5/G6 及 G8.1–G8.24 的 repo-only 零消费者清理同样保持关闭；它们不授予
下列任何新操作。

### U0/U1：因子实验室 UX 紧凑化与准确率高亮（2026-08-06）

- 已在不新增模块、控件、API、数据库字段或前端持久化状态的边界内完成 CSS 紧凑化：任务格由 `92px` 收紧为
  `76px`，趋势图/空态由 `300px` 收紧为 `244px`，并移除矩阵的 `min-height: 560px`。
- 用户已确认最终视觉方案：沿用既有 `is-accuracy-highlighted` 语义 class 和准确率判定；当当前筛选后的最优方案在
  `overall`、`upPrecision` 或 `downPrecision` 达到 `>= 60` 时，仅准确率数字显示为红色。`samples`、缺失和
  `< 60` 均不高亮；hover/selected、点击、排序、筛选和键盘行为不变。
- CSS 通过精确内容 SHA-256 URL 交付；测试会拒绝 CSS 内容变更后复用 immutable URL。实现、静态缓存与行为测试均已
  通过；当前证据见 [红色高亮设计](superpowers/specs/2026-08-06-task-cell-red-accuracy-design.md)。

## 队列总览与依赖

| 波次 | 工作流 | 当前状态 | 可并行性 | 结束产物 |
|---|---|---|---|---|
| 0 | R0：开发分支处置 | `WAITING_EXPLICIT_RELEASE_DECISION` | 不阻塞只读设计 | 用户确认 keep / PR / merge / push 中的明确路径 |
| A | D1：Liwei 8/11 T+5 失败诊断 | `REPOSITORY_REMEDIATION_VERIFIED_DATA_SCOPE_UNRESOLVED` | 不阻塞 G7/G8 | [代码修复验证与当前快照的独立授权边界](records/status/LIWEI_10Y01_T5_0811_FAILURE_DIAGNOSIS_20260806.md) |
| A | G7.0：Native 版本模型事实矩阵 | `DECISION_DRAFT_READY` | 等待用户确认，未授权实施 | [26 identity / 30 composite 的事实矩阵](records/status/NATIVE_VERSION_MODEL_FACT_MATRIX_G7_0_20260806.md) |
| A | G8.0：replay/ledger 保留决策矩阵 | `DECISION_DRAFT_READY` | 等待用户确认，未授权实施 | [保留/迁移/退役候选矩阵](records/status/G8_REPLAY_LEDGER_DECISION_MATRIX_20260806.md) |
| A | D0：文档生命周期审计 | `AUDIT_COMPLETE_AWAITING_CONFIRMATION` | 不阻塞 G7/G8；不自动删除 | [保留边界与 2 份删除候选](records/status/D0_DOCUMENT_LIFECYCLE_AUDIT_20260806.md) |
| B | G7.1：Native 专项实施计划 | `BLOCKED_ON_G7.0_DECISION_APPROVAL` | 可与 G8.1 设计并行 | 已批准的最小实施计划 |
| B | G8.1：replay/ledger 专项实施计划 | `BLOCKED_ON_G8.0_DECISION_APPROVAL` | 可与 G7.1 设计并行 | 已批准的分阶段收尾计划 |
| C | G7/G8 已批准实施 | `NOT_AUTHORIZED` | 按子计划确定 | 独立提交、独立验收与独立生产授权 |

`R0` 是发布闸门而非其它只读工作的前置条件：默认保持开发分支，不自动合并或推送。`D1` 的结论也不阻塞 G7 或
G8，除非诊断证实存在会影响它们的共享控制面缺陷。

## R0：开发分支处置

**目标：** 审阅 G3.1 的三笔已验证提交 `2227922`、`e830f9e`、`cbe03e9`，由用户决定分支后续处置。

- [ ] 只读核对提交差异、测试证据、当前工作树和目标分支差异。
- [ ] 用户明确选择以下之一：保持开发分支、推送并创建 PR、合并到 `master`、或其它明确路径。
- [ ] 仅在选择中明确包含该动作时，执行相应的 push/PR/merge；合并后重新运行相关测试。

**禁止：** 把“G3.1 已关闭”解释为可自动合并、推送或发布。

## Wave A：可并行的只读设计与审计

### D1：Liwei 2026-08-11 T+5 单次失败诊断

**目标：** 独立诊断 `liwei_0616_10y01_cons_say_k3_div_k10` 的 2026-08-11 T+5 单次失败，不把它混入 G3.1、
前端改动或日频 T+1 补写。

**状态：** `REPOSITORY_REMEDIATION_VERIFIED_DATA_SCOPE_UNRESOLVED`。完整材料见
[D1 诊断记录](records/status/LIWEI_10Y01_T5_0811_FAILURE_DIAGNOSIS_20260806.md)。

- [x] 只读核对 exact version、Registry、交易日历、input authority、cache generation 与 publisher/consumer 顺序。
- [x] 形成最高置信的条件因果推断：事故前稳定 discovery 顺序使 consumer 先于共享 cache publisher，满足已知
  fail-closed 分支的条件；未读取原始 run-log 异常，因此它不是绝对确证或其它运行期异常的绝对排除。
- [x] 确认已观察到的直接影响为一个 future T+5 key；原始异常未读前不绝对排除其它运行期错误，任何读取、修复或补数
  均须另起精确 scope、重新冻结并获得独立授权。
- [x] 复核 `d440091` 已是当前开发分支祖先，且 publisher-first 回归测试通过；没有需要重复提交的代码差异。
- [x] 在当前配置的受控只读快照中重建该单键的受限 plan：未观察到该 key，且缺少精确 Native generation，
  因而没有运行 recovery/write Gate；这不对其它环境中声称已补齐的数据作推断。

**禁止：** 不重复写入或伪造 `scheduled_live`；不手工 SQL；不以 G3.1 token、admission 或 provenance 外推权限。

### G7.0：Native 版本模型事实矩阵与决策稿

**目标：** 统一 Native 的业务身份、精确版本、Registry 生命周期和维护/激活语义，不改变算法或当前前端身份。

**状态：** `DECISION_DRAFT_READY`。本阶段是 `BLOCKED_DRAFT`，完整材料见
[G7.0 事实矩阵](records/status/NATIVE_VERSION_MODEL_FACT_MATRIX_G7_0_20260806.md)。

- [x] 只读绘制 26 个 `base_scheme_id`、30 个预期 composite Registry identity、current candidate exact version、
  runtime type 与可证明的历史证据；未由仓库证明的 DB/API/现场状态明确标为 unknown。
- [x] 列出状态转换的前置条件、写入者、失败闭环和 API/前端可见性，指出 config、exact version、Registry exposure
  与 admission evidence 不能互相推导。
- [x] 提出未批准的最小目标模型和历史只读保留边界；激活失败的 token 消费与 best-effort rollback 也已列为专项恢复边界。
- [ ] 用户确认决策稿后，才创建 G7.1 的独立实施计划和测试矩阵。

**禁止：** 不改算法、不删版本、不改 Registry、不过载 activation、不写业务表或控制面。

### G8.0：replay/ledger 保留决策矩阵

**目标：** 在退役前先决定 replay/recovery、legacy mode、历史 ledger 数据和外键/运行引用的保留语义。

**状态：** `DECISION_DRAFT_READY`。完整材料见
[G8.0 决策矩阵](records/status/G8_REPLAY_LEDGER_DECISION_MATRIX_20260806.md)。

- [x] 只读枚举 replay、ledger、legacy mode、`schedule_item_id`、generation registry、migration 019、相关 consumer/
  外键、repo plist template 与 installed 现场未知项。
- [x] 为每项给出未批准的 retain / isolated recovery / migrate / retire 建议，保留 `t_input_generations`，并将 replay、
  source/production 只读语义和 migration 019 的 `APPLYING` recovery 范围严格区分。
- [x] 形成 repo 清理、运行时控制面、DDL/migration、installed plist 四个互不外推的候选阶段；未设计并授权的受控只读
  schema/data inventory 前，任何 ledger 或 019 forward DDL 均保持阻断。
- [ ] 用户确认保留决策后，才创建 G8.1 分阶段实施计划。

**禁止：** 不删除仍有消费者的代码；不改变 installed plist；不执行 launchctl；不应用 migration 或 DDL。

### D0：文档生命周期审计

**目标：** 减少重复说明而不丢失当前规范、审计证据和可追溯性。

**状态：** `COMPLETE`。完整材料见
[D0 生命周期审计](records/status/D0_DOCUMENT_LIFECYCLE_AUDIT_20260806.md)。

- [x] 以 Git 跟踪文档清单、状态头与入站引用审计形成分类矩阵；候选列举产生的审计自引用已明确排除。
- [x] `CURRENT`、当前索引和可复核证据一律保留；已关闭的 G3.1 status 记录属于 `HISTORICAL EVIDENCE`，不得因
  “已闭环”直接删除。
- [x] 仅列出 2 份 `SUPERSEDED_DRAFT` 候选；用户已确认并仅删除这两份 front-end cutover 草案。
- [x] 已先提交精确清单并获得用户确认；删除后已更新索引和状态记录，运行文档测试与 `git diff --check`，并单独提交。

**禁止：** 不批量删除 `docs/records/`、不删除当前规范或唯一证据、不删除用户未提交文件、不以 Git 历史替代仍被
当前治理或审计需要的事实记录。

## Wave B：在各自设计闸门通过后执行

### G7.1 与 G8.1：专项实施计划

**前置条件：** 分别完成并获批准的 G7.0 / G8.0 决策稿。

- [ ] 每条工作流各自创建一份独立实施计划，包含精确文件、迁移/控制面边界、测试、回滚和提交切分。
- [ ] 任何生产或数据库动作都必须在该专项计划的对应闸门重新取得授权；不能将设计稿的阅读授权外推为写入授权。
- [ ] G7/G8 的任何实施均不得与 D1 或文档清理混入同一提交。

## Wave C：受控实施与发布

- [ ] 仅按已批准的 G7/G8 专项计划实施；每项完成后独立读回、测试和提交。
- [ ] 每个可发布提交均先在开发分支完成审阅和验证；是否 PR、合并 `master` 或推送由 R0 的明确用户选择决定。
- [ ] 任何 installed plist、launchd、服务或 DDL 操作均在独立的生产操作记录中执行和验收，绝不由仓库测试替代。

## 统一停止条件

立即停止当前工作流并回到对应设计/冻结阶段，如果出现任一情况：

- active scope、精确 version、交易日口径、DataBridge authority、输入 cutoff 或业务日期语义漂移；
- 需要修改算法、绕过 Gate、恢复旧 scheduler/ledger 路径、手工 SQL 或扩大原子范围才能“成功”；
- UI 需求要求新增 API/DB/控件、隐藏模块、改变数据语义，或高亮不再由当前准确率排序结果决定；
- 需要 launchd、installed plist、服务、DDL、业务写入、持久化回测或分支发布，但尚无对应专项授权；
- 工作区出现与当前子计划重叠的用户修改，或待删文档仍有当前引用/唯一审计价值。
