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
- **G4**：weekly 10Y D-overlay 的 2026-08-01 `gray_live` 缺口仍待处理。source
  benchmark/CompareGate 仍只服务首次 Native 技术入库；14/45、3 个翻向的历史输入 vintage
  漂移仅归档，不是单独 blocker。2026-08-04 已执行唯一的 `native-legacy-admission-attest`：receipt
  `lna_hr_20260611T055610Z_8742d5bc99c9` 绑定 maintenance 当前选择的 prior
  `63ffb52105ee / hr_20260611T055610Z_8742d5bc99c9` 与 frozen 10Y/h6/weekly-point identity，
  只写两张 Harness 控制面表，verifier 已读回 `legacy_operator_attestation_v1`。初次
  `native-maintenance` run `hr_20260804T092226Z_5d84b9d45fd9` 因预激活 API 状态混同失败；修正后
  exact version `e50ad79a6c2f` 的 `hr_20260804T102103Z_91fa9e7db871` 已通过全部六个 Gate。随后独立
  activation 已通过，exact version 与 composite Registry 均为 `active`，served `/api/schemes` 与
  composite metrics API 均读回 200。目标业务键 prediction count 仍为 0；唯一 2026-08-01
  `gray_live` gap write 仍须另取专项授权，activation 不授予调度或其他业务写入权限。
- **G5/G6**：周/月自然调度与完整日/周/月真实时钟观察尚未完成。

完整阶段定义、旧快照和停止条件见
[2026-08-03 生产信号与调度治理计划](records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md)。
