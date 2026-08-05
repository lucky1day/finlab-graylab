# 当前治理待办

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-06

最近的 G3.1 闭环证据见
[日频覆盖与治理闭环计划](records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md)。任何新的生产
副作用仍须单独授权；生产控制面规则以
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)为准。

## 后续（不抢跑）

- **G7**：Native 版本语义收敛；待 G3.1 关闭后另立最小计划。
- **G8**：先完成 replay/ledger 的最终设计决策；installed plist、migration 019 和表/外键 DDL 另行授权。

## 已关闭

- **G3.1 日频覆盖闭环（2026-08-06）**：五个 exact Blackbox identity 已仅获
  `launchd_one_shot`；一次 Harness Gate 以 `gray_live` insert-only 补齐 12 个 T+1 key / 11 个原子组，
  写后 plan 为全部 present，DB/canonical/API/前端读回一致。G7/G8 未随之授权。
  1. **Task 1 — 只读重新冻结与无写库验证**：已完成刷新后 scope、DataBridge authority、admission 与 no-write
     simulation 冻结。
  2. **Task 2 — exact Blackbox admission（需独立授权）**：已完成五个 identity 的 `launchd_one_shot` only 收敛。
  3. **Task 3 — 历史补写（需独立业务写入授权）**：已由单次 `signal-gap-fill` 写入 12 条 `gray_live`。
  4. **Task 4 — 端到端读回**：已完成 DB、canonical、API 与前端 `target_date` 月筛选验收。
- P-1、G0、G1/G2 功能闭环、G3 的 8 月 3 日补写、G4、G5/G6 功能验收与 G8.1–G8.24 已从待办移除；
  证据保留在 Git、Harness、run/prediction 审计与现行架构文档中。
