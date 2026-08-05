# 当前状态

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-06

本文只保留当前已验证事实；带日期的执行证据在[状态记录](records/status/README.md)。生产调度规则以
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)为准；当前执行顺序以
[active 日频覆盖与治理闭环计划](records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md)为准。

## 当前政策

- `launchd + installed plist` 是唯一生产调度控制面；仓库代码/模板不单独证明生产挂载。
- 自然时钟写 `scheduled_live`；经授权、insert-only 的历史修复写 `gray_live`；两者不可互相替代。
- `ledger`、`occurrence`、`epoch`、daily-gray、常驻 APScheduler 和旧预检不再是新建或
  过渡生产路径。
- installed plist、launchctl、服务、激活、admission、业务写入、持久化回测和 DDL 都须先只读核对并取得
  独立授权。

## 未完成的生产治理

### G3.1：当前唯一 P0 数据完整性工作

- 2026-08-01 是非交易日，不应有日频信号。
- 34 个 active 日频 composite scope 的 DB raw、Dashboard canonical 与 served API live 行完全一致：
  T+1 为 458、T+5 为 1,211、合计 1,669；2026-08 的 T+1/T+5 信号数分别为 18/155。
- 2026-08-03 为 T+1 `10/10`、T+5 `24/24`；2026-08-04 为 T+1 `3/10`、T+5 `24/24`；
  2026-08-05 为 T+1 `5/10`、T+5 `24/24`。
- 后端真实缺 12 个 T+1 历史 business key：五个 exact Blackbox identity 各缺 8 月 4/5 两日，
  `t1_daily` 的 5Y/10Y 仅缺 8 月 4 日。不是重复、cache 或前端过滤问题。
- 下一步仅是无写库重新冻结与 admission 验证；admission 收敛和 `gray_live` 补写均尚未授权。

## 已验证的 7Y 灰度闭环与阶段摘要

- 两套 7Y v2 已完成 Gate、入库、历史 `gray_live`、served API 与前端读回；当前尚无该两套方案的
  one-shot admission。该事实不授予 scheduler admission；计划中的 Task 2 仍须独立授权。
- G1/G2 的 DataBridge 与 launchd-only 单 writer、G3 的 8 月 3 日补写、G4 的 D-overlay 唯一 key、
  G5/G6 的无写库功能验收均已闭环；自然时钟继续作为非阻塞观测。
- G8.1–G8.24 的 repo-only 零消费者清理已完成；不能继续零散删除仍有消费者的 replay/ledger 闭包。

## 延后工作

- G7：Native 版本模型收敛，待 G3.1 后另行计划。
- G8 最终退役：先决定 replay/recovery、legacy mode 和历史 ledger 数据保留，再考虑 installed plist 或
  数据库迁移；均不在当前授权范围。
