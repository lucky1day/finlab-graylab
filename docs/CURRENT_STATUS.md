# 当前状态

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-04

本文只保留当前已验证结论；带日期的调查、历史快照和执行证据位于
[状态记录](records/status/README.md)，未完成工作的排序位于[TODO](TODO.md)。生产调度
规则以[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)为准。

## 当前政策

- `launchd + installed plist` 是唯一生产调度控制面。仓库 plist、Python runner 或源码
  修改都不能单独证明生产已挂载；必须由 installed plist、`launchctl`、日志、run 和
  prediction 共同证明。
- `ledger`、`occurrence`、`epoch`、daily-gray、常驻 APScheduler 和旧预检均不再是新建或
  过渡生产路径。它们保留为待退役兼容代码或历史证据，不能获得新的 writer 权。
- 自然时钟合格写入为 `scheduled_live`；经授权的历史 insert-only 修复为 `gray_live`。
  两类 provenance 不可互相替代。
- installed plist 编辑/替换、`bootstrap/bootout/kickstart`、服务重启、激活、live 写入、
  持久化回测和历史补数均须先只读核对并取得独立生产授权。

## 已验证的 7Y 灰度闭环

两套本地因果 Blackbox V2 trial 均保持 active：

- `seven_y_current55_lgbm_001_v2__h1__7Y`；
- `seven_y_current55_lgbm_002_v2__h1__7Y`。

各方案均有 337 条持久化回测和 44 条不重叠的历史 `gray_live`。在受控 backend plist
重载后，固定实例 nonce 已生效；2026-08-03 两套 formal served-API Gate 均 fresh passed，
`/api/health`、`/api/schemes`、metrics 和精确 backtest 查询均返回 200。前端/API 可读取
两套方案。

这不授予 scheduler admission：两套方案仍没有 `scheduled_live`，也没有被纳入任何自然
调度 writer。

## 未完成的生产治理

- **G1**：本机 MySQL → DataBridge artifact 的原子、fail-closed refresh 尚未形成真实
  launchd one-shot 观察证据。
- **G2**：daily、weekly、monthly、actuals 的单 writer launchd-only 收敛尚未完成；进入
  任何 installed/loaded 控制面变更前必须重新只读核对现场。
- **G3**：在 G1/G2 和专项授权后，重新枚举并仅补仍缺失的 2026-08-03 日频 business key。
- **G4（已完成）**：`weekly_10y_d_overlay_0529` 的历史 benchmark 输入 vintage 漂移继续只作
  归档诊断，不是 blocker。固定 receipt、六段 `native-maintenance` 和独立 activation 均已完成；随后在
  独立的 `signal-gap-fill` 授权下，用 DB-`SEALED` 的 current-snapshot 制品
  `native-cc249e2aec88fad7bcfc7c1c` 补写唯一 `2026-08-01 / 2026-07-31 / 2026-08-07 / 10Y / h6`
  `gray_live` key。run `2106` 为 `success`，预期/返回/写入均为 1，方向 `-1`、置信度 `0.32`、
  exact version `e50ad79a6c2f`；无任何 scheduler 关联字段。DB、`/api/schemes`、精确 predictions、
  metrics 与 dashboard 均已读回，postfill planner 为 14 条 `SKIP_PRESENT`、0 条 live gap。该闭环不授予
  scheduler admission、其他业务写入或 installed 控制面操作。
- **G5/G6**：周/月自然调度与完整日/周/月真实时钟观察尚未完成。

完整阶段定义、旧快照和停止条件见
[2026-08-03 生产信号与调度治理计划](records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md)。
