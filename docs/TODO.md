# 当前治理待办

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-04

本文只定义未完成工作的顺序与前置条件。生产控制面规则见
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)，动态事实见
[当前状态](CURRENT_STATUS.md)，带日期的完整调研见
[2026-08-03 治理计划](records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md)。

## P0：生产信号治理

按以下顺序推进；每一阶段开始前都重新只读核对 active Registry、输入截止、installed
plist、`launchctl` loaded state、日志与实际缺口。任何生产副作用须单独授权。

1. **G0 — 文档与口径（已完成，开发分支）**：CURRENT 架构、SLA、SOP、部署说明和
   文档测试已统一为 launchd-only 单 writer 模型；ledger/daily-gray 只保留历史或待退役语境。
2. **G1 — DataBridge refresh（下一阶段）**：用 launchd one-shot 从本机 MySQL 原子发布标准
   artifact；保留源表、schema、连续性、稳定轮次和 cutoff 校验，失败不得回退 stale
   artifact。
3. **G2 — 调度单 writer**：将 refresh、daily、weekly、monthly 和 actuals 收敛为各自
   明确的 launchd writer；旧 scheduler、daily-gray 和重复预检不再拥有生产写权。
4. **G3 — 日频历史缺口**：仅在 G1/G2 完成并取得专项授权后重新枚举 2026-08-03 缺口，
   对仍缺的 business key 写 `gray_live`，不覆盖既有记录。
5. **G4 — weekly 10Y D-overlay**：历史 source-benchmark 输入 vintage 漂移已确认只作归档
   诊断。已实现的专项 `native-legacy-admission-attest` 只适用于
   `weekly_10y_d_overlay_0529`，只在 maintenance 当前选择的 prior `all + compare=passed` 且
   StaticGate 已通过、仅缺 identity 字段时，以 issuer 和 exact prior version/run 绑定的 ≤900 秒
   一次性 token 写入两张 Harness 控制面表 receipt。2026-08-04 已写入
   `lna_hr_20260611T055610Z_8742d5bc99c9`，并由 verifier 识别为
   `legacy_operator_attestation_v1`；它不激活、不补数，下一步是正常六段
   `native-maintenance`。当前 exact candidate 的 `t_scheme_versions` 为 `native_adapter/draft`、
   预期 Registry 统一 `paused`，是正常预激活态。G4 在六段 Gate 和受控 activation 前不得补数；
   之后才可仅补 `weekly_10y_d_overlay_0529 / 10Y / h6 / predict_date=2026-08-01 / feature_date=2026-07-31 /
   target_date=2026-08-07 / gray_live`。不得改 Native core、调参、覆盖 source benchmark，
   或扩展补数范围。
6. **G5 — 周/月自然调度**：形成独立 installed/loaded plist，并观察真实周六和自然月
   15 日触发。历史修复只可经授权写 `gray_live`。
7. **G6 — P0 观察闭环**：至少观察一个完整日/周/月周期，证明每个 cadence 只有一个
   writer、输入 fail-closed、API/页面与数据库一致且可控回退。

## 后续阶段

- **G7（P1）**：在 P0 稳定后收敛 Native 版本模型；保留审计历史，不按创建时间猜测或
  删除 sibling。
- **G8（P2）**：仅在 P0 真实观察完成后，按“先替代并观察、再删代码、最后删表”退役
  legacy、ledger、daily-gray、旧 scheduler 和相关债务。删表另需零读写证据、备份/恢复
  方案与专项授权。

## 已完成但不外推的 7Y 灰度工作

两套 `seven_y_current55_lgbm_*_v2` 已完成受控入库、回测、历史 `gray_live`、前端读回和
formal served-API Gate。它们没有 scheduler admission，也没有 `scheduled_live`；该事实
不改变以上 G1/G2 的前置顺序。
