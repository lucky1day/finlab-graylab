# Active Daily Coverage & Governance Closure Implementation Plan（2026-08-06）

> **For agentic workers:** REQUIRED SUB-SKILL: 在用户逐阶段授权后，使用 `superpowers:executing-plans` 按任务执行；不得把本计划当成生产授权。

**目标：** 以最小边界恢复 2026-08-04/05 的 active T+1 日频完整性，并保持 launchd-only、单 writer 和历史修复 provenance 不变。

**架构：** 前端按 `target_date` 展示，DB raw、Dashboard canonical 和 served API 必须是同一行集。修复分成三道不可跳过的门：先无写库证明五个精确 Blackbox identity 能走 one-shot 编排，再在独立授权下收敛它们的 exact admission，最后以既有 Harness/repository 的 insert-only `gray_live` 路径补写冻结业务键。

**技术栈：** Python 3.12、FastAPI、MySQL、launchd、Harness、原生 HTML/JS Dashboard。

**计划状态：** `COMPLETED (2026-08-06)`。G3.1 已在授权边界内闭环；冗长历史过程不复制到本文，可从 Git 提交、Harness/DB receipt、run 和 prediction 审计追溯。它不重定义当前生产控制面，后者以 `AGENTS.md`、`CLAUDE.md` 和 `docs/architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md` 为准。

---

## 1. 范围、数据语义与不可做事项

- 本计划只覆盖 active 日频 T+1/T+5 scope 的 2026-08-03 至 2026-08-05 覆盖验收，以及由此确认的
  2026-08-04/05 T+1 历史缺口。
- 前端、API 和验收一律以 `target_date` 为日期语义；不能以 `predict_date`、feature date 或
  backtest 日期替代。
- 2026-08-01 是周六、交易日历为非交易日：不生成、也不补任何日频信号。
- 不修改算法、Blackbox delivery、交易日历、DataBridge、Registry 业务身份、T+5 policy、旧 scheduler，
  或将历史补数写为 `scheduled_live`。
- 8 月 5 日 Liwei consumer 对未来 target 的单独失败不属于本窗口；前端压缩/高亮是独立 UX 工作，
  不与本数据修复合并。
- 不执行手工 SQL、手动 runner、installed plist/launchctl/服务操作或数据库 DDL。

## 2. 已闭环范围（仅保留摘要）

| 范围 | 闭环结论 | 当前含义 |
|---|---|---|
| P-1 / G0 | 两套 7Y v2 已入库、历史 `gray_live`、API/前端读回；文档口径已统一 | 不等于已获得 scheduler admission |
| G1 / G2 | DataBridge publish/retry 与 launchd-only 单 writer 功能闭环 | 自然时钟仍是非阻塞观测 |
| G3（仅 8 月 3 日） | T+1 `10/10`、T+5 `24/24` 已读回 | 不外推到 8 月 4/5 |
| G4 | weekly 10Y D-overlay 唯一 `gray_live` key 已闭环 | 不授予其它写入或 scheduler 权限 |
| G5 / G6 | 三 cadence 无写库控制面模拟与 P0 功能验收完成 | 不把模拟伪称为自然时钟 |
| G8.1–G8.24 | repo-only 零消费者清理已完成 | 剩余 replay/ledger 闭包不是可逐函数删除的死代码 |

以上阶段的长篇步骤、旧快照和已完成 checklist 已从本文移除。若需要审计，先查对应 Git commit、
Harness receipt、`t_scheme_runs`/`t_scheme_predictions` 和现行架构文档，而不是重新启用旧路径。

## 3. G3.1 — 闭环证据（2026-08-06）

- 刷新后只读冻结确认 DataBridge current 为
  `full-20260806-063108-e08812802aff`（`refresh_date=2026-08-06`），8 月 3/4 日的 daily cutoff
  分别精确到 `2026-08-03` / `2026-08-04`。受限 selection 为 `target_date=2026-08-04..05`、
  `task_type=T+1`；写前 plan SHA
  `cda60ed5dd9c604223620c46cbb269be371da78399c9b0ec2659c234d107331e` 冻结 20 个预期键：8 个 present、
  12 个 `GRAY_LIVE_GAP`、11 个原子组，且无 T+5、8 月 1 日或范围外 target。
- 提交 `2227922` 将 gap-plan selection 纳入 SHA/replay；提交 `e830f9e` 将五个 exact Blackbox identity
  收敛为 `mode=formal + {launchd_one_shot}`，仍拒绝 legacy、ledger 与 direct，未执行 launchd、plist
  或服务操作。
- 为 `t1_daily` 的 8 月 4 日双 target 准备并登记 sealed current-snapshot Native artifact
  `native-8499778c42a91c94ca6cfc0a`（feature `2026-08-03`）；其余十个键使用冻结的 DataBridge authority。
  未记录 HMAC token、DSN 或凭据。
- 单次 `signal-gap-fill` Gate 对该 SHA 返回 `PASSED`：runs `2156`–`2166` 全部 success，11 组 insert-only
  共写入 12 条 `gray_live`。其 provenance 为 10 条 `databridge_current_generation` 与 2 条
  `native_current_snapshot_artifact`；没有新增 `scheduled_live`、T+5、8 月 1 日或其它 target。
- 写后受限 plan SHA
  `aa5e915519aa754aa9518b42231f5a5ec20b3026cdf454e76eeea351f45c8596` 为 20 个 `SKIP_PRESENT`，open gap
  为 0，无控制面 blocker 或范围外项。

| target_date | T+1（10 scopes） | T+5（24 scopes） | DB raw / canonical / fresh API |
|---|---:|---:|---|
| 2026-08-03 | 10/10 | 24/24 | 一致 |
| 2026-08-04 | 10/10 | 24/24 | 一致 |
| 2026-08-05 | 10/10 | 24/24 | 一致 |

前端按 `target_date` 切换到 `仅实盘`、`2026-08..2026-08` 后已就绪；其 canonical duplicate 防线未触发。
DB、Dashboard canonical 与 fresh API 的最后可见月 live 行数均为 `207`，前端的“样本”指标未被当作 live 行数。

精确 version 仍为：`one_y_t1_quote_state_hv_v1@1fd56dfcc264`、
`three_y_adyn_lb1_k3_v1@98233f0cb9ef`、`three_y_adyn_lb2_k1_v1@47c7c1776db0`、
`seven_y_current55_lgbm_001_v2@cd0624ef3ead`、`seven_y_current55_lgbm_002_v2@57e956513471`，以及
`t1_daily@7898b9e47a9a`。

---

## 4. 执行任务

### Task 1：只读重新冻结与无写库准入验证

**目的：** 在任何代码、admission 或业务数据变更前，确认当前 active set、exact version、输入截止和
12 个缺口仍与本计划一致。

**文件：**

- 只读：`scheduler/blackbox_scheduler_admission.py`
- 只读：`deploy/blackbox_scheduler_admission_v1.json`
- 只读：`harness/signal_gap_plan.py`、`harness/gates/signal_gap_fill_gate.py`
- 验证证据：对应实施提交中的 launchd runner 回归；当前仓库不永久保留该次事故测试。

- [x] 已核对 Git 工作区、active Registry、精确版本、交易日历、DataBridge current、DB/API/前端行集，并在
  变更 admission 后重新冻结精确 scope。
- [x] fake engine 三 cadence no-write control-plane simulation 保持零 DB、算法子进程、cache 和业务写入访问。
- [x] 五个 exact identity 的最终 admission 为 formal + `launchd_one_shot` only；`t1_daily` 的 8 月 4 日
  输入 generation 以专项 current-snapshot artifact 处理。
- [x] 受限 plan 仅含 12 个 T+1 open key、11 个原子组；8 月 1 日、T+5 与 8 月 11 日均不在 selection 内。

**通过条件：** 输出的 scope、version、target multiset 和输入 authority 全部匹配；否则不进入 Task 2。

**提交：** 若 Task 1 仅产生报告，不提交报告或 `reports/` 产物；仅在本计划需要修订时单独提交文档。

### Task 2：五个 exact Blackbox admission 的最小收敛（需独立授权）

**目的：** 只让已验证的五个 exact daily T+1 identity 获得 `launchd_one_shot` 能力，不以
`status=active` 自动放开其它 Blackbox。

**文件：**

- 修改：`scheduler/blackbox_scheduler_admission.py`
- 修改：`deploy/blackbox_scheduler_admission_v1.json`
- 修改：`tests/test_blackbox_scheduler_admission.py`
- 当次修改包含 launchd runner 回归；当前仓库只保留可复用入库合同。

- [x] 在两个测试模块写入精确身份断言：上述五个 `scheme_id + scheme_version` 为
  `mode=formal`、daily/T+1/h1/正确 tenor，且 capabilities 精确等于仅含
  `launchd_one_shot` 的 frozen set；其他 admission 不变。
- [x] 已以测试先行验证旧 admission 不满足新断言。
- [x] 已同步更新 Python frozen map 与 JSON frozen map；新增
  `_LAUNCHD_ONE_SHOT_ONLY_CAPABILITIES = frozenset({LAUNCHD_ONE_SHOT})`。不得复用
  `_FORMAL_DAILY_CAPABILITIES`（它包含 `legacy_automatic`、`daily_ledger` 和
  `direct_scheduled`），也不改 `VALID_CONTROL_PLANES` 或恢复 `recurring`、ledger、
  direct/旧 scheduler capability。
- [x] 已扩展 no-write runner simulation：五个 identity 各被一次
  `scheduled_live + launchd_one_shot` dispatch 编排，engine、算法进程、cache 和业务写入仍为
  禁止访问的替身。
- [x] 已运行 admission、launchd runner、repository one-shot、architecture/document 相关测试，
  `compileall` 和 `git diff --check`；只暂存本任务四个文件及必要测试文件并单独提交。

**授权边界：** 这是正式调度语义变化；没有用户对这五个 exact identity 的独立授权，不得编辑上述
admission 文件，也不得通过重启/launchd 触发验证。

### Task 3：12 个历史 business key 的受控 `gray_live` 补写（需独立业务写入授权）

**目的：** 用现有 Harness/repository 路径补齐已冻结缺口，不改变自然调度 provenance。

**文件：**

- 不新增业务写路径；只使用 `harness/signal_gap_plan.py`、
  `harness/gates/signal_gap_fill_gate.py`、`scheduler/repository.py` 的既有边界。
- 执行证据仅写到既有 Harness、run 和 prediction 审计表；不把 `reports/` 暂存到 Git。

- [x] 已在执行前再次重建只读 plan，并以新 SHA、exact version、12-key multiset 与 source authority 作为唯一输入。
- [x] 十个 Blackbox 单 target 组和一个 `t1_daily` 双 target 组均以短期、精确绑定的
  `signal_gap_fill_write` 一次性授权；每个 token 必须绑定 exact version、predict/feature/target
  日期、完整 target multiset、plan SHA 与 input authority。
- [x] 仅经一次 `signal-gap-fill` Gate 写入 insert-only `gray_live`；未使用 SQL、手动 runner、
  `scheduled_live`、8 月 1 日周末、T+5、8 月 11 日或其他 target。
- [x] Gate 无失败组；receipt、run 与 prediction 审计保留，未做手工重试或状态修改。

**通过条件：** 仅新增冻结窗口的 12 个唯一业务键，保留既有记录；不存在 duplicate/overwrite。

### Task 4：写后端到端读回与文档闭环

**文件：**

- 修改：本计划、`docs/CURRENT_STATUS.md`、`docs/TODO.md`（仅在 Task 3 成功后）
- 验证：`tests/test_onboarding_docs.py`

- [x] 已用同一 active scope 重跑 DB raw、canonical、fresh served API 与前端 `target_date` 月份核对。
- [x] 2026-08-03/04/05 均为 T+1 `10/10`、T+5 `24/24`；冻结窗口增量恰为 12，
  且 DB/API/前端最后一个月信号数一致。全局总行数随自然批次变化，不使用 1,669 作为硬编码阈值。
- [x] 已记录 exact runs、versions、plan SHA、input authority 类型和 `gray_live` provenance；从现行计划中
  关闭 G3.1，不把证据复制成长篇历史过程。
- [x] 已运行文档测试、`git diff --check`、`compileall` 与任务相关完整测试；仅提交本任务文件。

---

## 5. 仍需设计、但不得抢跑的工作

### G7：Native 版本模型（P1）

在 G3.1 闭环后再单列计划。目标是版本语义收敛，不删除历史版本或改变算法/前端身份；当前没有授权的
代码或数据库动作。

### G8：legacy/ledger 最终退役

repo-only 的安全零消费者清理已结束。后续必须先由用户决定是否保留隔离 real replay/operator recovery，
再设计 `BOND_DAILY_COORDINATOR_MODE`、admission vocabulary、历史
`t_scheme_runs.schedule_item_id` 与 ledger 数据的保留策略。installed plist 清理、migration 019
和 ledger 表/外键 DDL 都是独立生产或数据库操作，不能与 G3.1 合并。

---

## 6. 统一停止条件

立即停止副作用并回写计划，如果出现以下任一情况：

- active scope、精确 version、12-key multiset、交易日口径、输入 cutoff 或 DataBridge authority 改变；
- 发现 DB raw、canonical、served API 或前端实际消费的行集不一致，或出现重复/覆盖；
- 任何步骤需要算法修改、降低 Gate、回退 stale artifact、恢复旧 scheduler/ledger，才能获得“成功”；
- 没有相应的用户授权却需要 admission、激活、业务写入、launchd、installed plist、服务或 DDL 操作；
- 工作区出现与本任务重叠的用户修改，或任何验证无法保持 no-write 边界。

## 7. 当前下一步

**G3.1 已关闭。** 后续仅可按独立计划推进 G7 的 Native 版本语义收敛，或 G8 的 replay/ledger 最终设计；
两者均不从本次 admission、artifact 或 `gray_live` 授权外推。
