# 当前状态

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-06

本文只保留当前已验证事实；带日期的执行证据在[状态记录](records/status/README.md)。生产调度规则以
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)为准；最近的 G3.1 闭环证据见
[日频覆盖与治理闭环计划](records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md)。

## 当前政策

- `launchd + installed plist` 是唯一生产调度控制面；仓库代码/模板不单独证明生产挂载。
- 自然时钟写 `scheduled_live`；经授权、insert-only 的历史修复写 `gray_live`；两者不可互相替代。
- `ledger`、`occurrence`、`epoch`、daily-gray、常驻 APScheduler 和旧预检不再是新建或
  过渡生产路径。
- 新的 installed plist、launchctl、服务、激活、admission、业务写入、持久化回测和 DDL 都须先只读核对并取得
  独立授权。

## 已闭环的生产治理

### G3.1：日频覆盖闭环（2026-08-06）

- 2026-08-01 是非交易日，不应有日频信号。
- 五个精确 Blackbox identity 已仅获得 `launchd_one_shot` admission（commit `e830f9e`）；没有 legacy、ledger 或
  direct capability，也没有执行 launchd/plist/服务操作。
- 写前受限 plan SHA `cda60ed5dd9c604223620c46cbb269be371da78399c9b0ec2659c234d107331e` 冻结 12 个 T+1 key / 11 组；
  一次 `signal-gap-fill` Gate 以 `gray_live` insert-only 写入，runs `2156`–`2166` 全部 success。
- 写后 plan SHA `aa5e915519aa754aa9518b42231f5a5ec20b3026cdf454e76eeea351f45c8596` 为 20 个 `SKIP_PRESENT`；没有新增
  `scheduled_live`、T+5、8 月 1 日或范围外 target。
- 本次 `gray_live` 历史修复本身**不授予 scheduler admission**，也不证明 installed plist 已挂载或自然时钟已现场触发；
  五项 exact admission 是独立、仅限 `launchd_one_shot` 的仓库变更。
- 34 个 active 日频 composite scope 在 DB raw、Dashboard canonical 与 fresh served API 一致：2026-08-03/04/05
  均为 T+1 `10/10`、T+5 `24/24`。前端以 `target_date` 过滤 `2026-08` 的 live 行集为 207，未将指标“样本”
  数当作 live 行数。
- DataBridge current authority 为 `refresh_date=2026-08-06`，8 月 3/4 的 cutoff 精确匹配；12 条 provenance 为
  10 条 DataBridge current generation 和 2 条 Native current-snapshot artifact。HMAC token、DSN 与凭据未写入文档。

## 已验证的 7Y 灰度闭环与阶段摘要

- 两套 7Y v2 已完成 Gate、入库、历史 `gray_live`、served API 与前端读回；其列入 G3.1 的 exact version
  现为 `launchd_one_shot` only。该事实不外推到其它 7Y identity 或控制面。
- G1/G2 的 DataBridge 与 launchd-only 单 writer、G3 的 8 月 3 日补写、G4 的 D-overlay 唯一 key、
  G5/G6 的无写库功能验收均已闭环；自然时钟继续作为非阻塞观测。
- G8.1–G8.24 的 repo-only 零消费者清理已完成；不能继续零散删除仍有消费者的 replay/ledger 闭包。

## 未完成的生产治理

- G7：Native 版本模型收敛，G3.1 已关闭后仍须另行计划。
- G8 最终退役：先决定 replay/recovery、legacy mode 和历史 ledger 数据保留，再考虑 installed plist 或
  数据库迁移；均不在当前授权范围。
