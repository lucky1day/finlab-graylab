# 当前优先级与待办

**文档状态**：`CURRENT`

**目标读者**：项目负责人、平台开发、运维和审计人员

**最后核验日期**：2026-07-30

本文是当前未完成工作的唯一权威排序。已验证结论见
[当前状态](CURRENT_STATUS.md)，日频不变量见
[日频信号 SLA](architecture/DAILY_SIGNAL_SLA.md)。

## P0：日频真实 ledger 收口

已批准目标生产口径是 1 coordinator、每交易日 1 occurrence、25 base execution
（17 Native + 8 V2）、Native 最大并发 2、V2 最大并发 2，最终验收 29/29
signal。日常生产必须使用 warm cache；不新增冷启动压力门禁、额外性能准入层或
双重签名信任链。该口径尚未切换到 production；当前仍为 migration 017、
`rollout=legacy`，ledger 未启用。

按顺序完成：

1. 保留 2026-07-30 真实事实：06:30 没有 occurrence，08:00 为 0/29；不得按日期
   补造 25 item、29 target、receipt 或 `scheduled_live` provenance。完成切换后的
   未来首个交易日，才核对真实 occurrence 的 winning run fence、prediction
   linkage、target receipt、visibility 和 write-once SLA。
2. 使用已由 PR #16 合并的 Native current-snapshot artifact prepare/register
   路径，为 feature=2026-07-27、2026-07-28 生成并登记两份 production
   authority。入口强制真实 capture date、历史 feature cutoff、专项 HMAC、磁盘
   重验、每个 feature 唯一 SEALED authority，并与正式 ledger generation 隔离；
   当前代码已就绪，但 production artifact 尚未生成或登记。
3. 对 50 条历史日频缺口生成受控 insert-only 计划并分批复核：7/23 为 1、7/24
   为 1、7/27 为 2、7/28 为 17、7/29 为 29；T+1 共 5、T+5 共 45。全部只能
   写 `gray_live`，不得倒签 `scheduled_live`。当前 DataBridge 已使 20 个 V2
   target 具备 freshness，30 个 Native target 仍因 production authority 未登记而
   阻断；
   旧阻断态 plan 不得执行。
4. production 当前停在 017；先经 canonical CLI 应用 migration 018，再在授权
   维护窗口执行 machine-global epoch 与 ledger cutover。切换前 backend 保持 legacy/
   HTTP 200，scheduler 与 v2-preflight 保持未加载；切换成功后只启动一个
   scheduler，并核验 `current_run_id + attempt_no` fence、ProcessStartGuard、当天
   Native/DataBridge generation 和 08:30 cutoff。

已完成但仍须保持的生产输入基线：

- Liwei 7 个 production schema 3 family 已完成一次性 bootstrap；同 authority
  二次运行为 7/7 `hit`、零训练，cache-local direct-ready。
- DataBridge `refresh_date=2026-07-30` 已晚到发布并通过 `--check-only`；它不改变
  7 月 30 日 0/29 和无 occurrence 的事实。

## P1：周度剩余对账与后端启用

已完成 Actual Registry 化、统一 `predict_date >= 2025-01-01` 展示起点、三个错误
point-backed 周平均身份删除、月度 updater 幂等复核，以及
`weekly_10y_lgbm_point_v1` 的 7 个 gray 缺口补齐。

剩余步骤：

1. 取得独立部署/重启授权后，在维护窗口重启 BFL Python 后端，使 Registry
   Actual 范围、统一展示起点和 coverage 诊断由服务端生效；不得修改或重启
   BondProjectPro。
2. 专项研究 `weekly_10y_d_overlay_0529` 当前 DB 输入 vintage 与旧 benchmark
   的差异：45 个 benchmark 周中 14 周有内部字段差异，`202538/202548/202602`
   方向翻转。CompareGate 继续 fail-closed；禁止调算法、改 benchmark 或使用旧
   generation fallback 贴合。
3. 输入 vintage 问题闭合后，重新执行该 Native 的完整 `--no-persist`，
   要求 72 行、17 个月且 original benchmark `45/45`。

`weekly_10y_d_overlay_0529` 的生产 `202625` 冲突、四个灰度缺口和历史
`200951` 日历边界均已闭合；`weekly_10y_lgbm_point_v1` 的 7 月 31 日信号也已
存在。当前 6 个 active `weekly_point` 均为 `81/80`，周度 point 数量对齐已经
完成；10Y D-overlay 的历史 CompareGate 漂移是独立研究项。

## P2：平台增强

以下项目不改变已批准的日频目标合同，按独立需求、设计和授权推进：

1. 周/月自然频率自动调度及其 occurrence 治理；
2. 三个 0629 公共 generation adapter 和逐方案 CompareGate；
3. migrations 019/020、generation 归档、内容去重、磁盘保留和灾难恢复；
4. 长期生产可用性、恢复时间和统计分布观测；这些数据用于持续改进，不成为另一个
   与 29/29 ledger 并行的生产准入系统；
5. 周频/月频逐步迁入统一多频率 occurrence 账本。

## 已完成灰度批次

- 10Y T+5 四方案的 `controlled activate`、`persistent backtest`、
  `manual gray_live` 和前端验收已完成，不再列为待办。
- FengRL 五个月度方案已达到 `MANUAL_GRAY_ACCEPTED_5_OF_5`；每方案 16 条历史和
  3 条手工 `gray_live`，本批 `80 + 15 = 95`。完成手工灰度仍不授予其它
  Blackbox identity 的生产权限。
