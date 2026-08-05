# 当前治理待办

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-06

当前唯一执行计划见
[active 日频覆盖与治理闭环计划](records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md)。任何生产
副作用须单独授权；生产控制面规则以
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)为准。

## P0：G3.1 日频覆盖闭环

按以下顺序推进，任何日期/版本/active scope 漂移均停止并重新冻结：

1. **Task 1 — 只读重新冻结与无写库验证**：核对 active Registry、exact version、日历、DataBridge、
   DB/API/前端行集，并生成仅含 12 个 T+1 key 的 gap plan。
2. **Task 2 — exact Blackbox admission（需独立授权）**：仅让五个列明 identity 获得
   `launchd_one_shot`；不得同时授予 legacy、ledger 或 direct capability。
3. **Task 3 — 历史补写（需独立业务写入授权）**：仅经 Harness `signal-gap-fill` 以
   `gray_live` insert-only 补齐 12 key；不写 8 月 1 日、T+5 或其他日期。
4. **Task 4 — 端到端读回**：验收 8 月 3/4/5 均为 T+1 `10/10`、T+5 `24/24`，并更新当前状态。

## 后续（不抢跑）

- **G7**：Native 版本语义收敛；待 G3.1 关闭后另立最小计划。
- **G8**：先完成 replay/ledger 的最终设计决策；installed plist、migration 019 和表/外键 DDL 另行授权。

## 已关闭

P-1、G0、G1/G2 功能闭环、G3 的 8 月 3 日补写、G4、G5/G6 功能验收与 G8.1–G8.24 已从待办移除；
证据保留在 Git、Harness、run/prediction 审计与现行架构文档中。
