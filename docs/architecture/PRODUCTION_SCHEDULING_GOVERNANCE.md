# 生产信号与调度治理

**文档状态**：`CURRENT`

**目标读者**：平台开发、运维、审计和方案维护人员

**最后核验日期**：2026-08-03

本文定义生产信号的唯一控制面。具体现场事实查看[当前状态](../CURRENT_STATUS.md)，
分阶段治理和带日期的证据查看
[2026-08-03 生产信号与调度治理计划](../records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md)。

## 1. 唯一控制面与证明标准

`launchd + installed plist` 是唯一生产调度控制面。只有 installed plist、对应
`launchctl` loaded state、任务日志、run 和 prediction 相互一致，才能证明任务已生产挂载；
仓库 `deploy/launchd/*.plist` 只是期望配置，Python 模块只是被 plist 调用的一次性执行器。

一个 cadence 只能有一个生产 writer。DataBridge refresh、daily prediction、weekly
prediction、monthly prediction 和 actuals 分别由明确的 LaunchAgent 触发；不得让常驻
APScheduler、daily-gray、预检进程或任何手工进程同时拥有同一 business key 的自然写入权。

仓库期望模板的单 writer 映射为：`com.bond-factor-lab.data-bridge-refresh` 于 06:30
发布 DataBridge；`com.bond-factor-lab.daily-predictions` 于工作日 07:03、
`com.bond-factor-lab.weekly-predictions` 于周六 11:30、
`com.bond-factor-lab.monthly-predictions` 于自然月 15 日 18:00 分别启动相应 cadence 的
one-shot runner；`com.bond-factor-lab.actuals` 保持 08:30、19:00、23:45 的既有唯一
writer。这些是仓库 desired state，不是机器安装、加载或停用的现场结论。

`com.bond-factor-lab.scheduler`、`com.bond-factor-lab.daily-gray` 和
`com.bond-factor-lab.v2-preflight` 在仓库模板中为 `Disabled=true` 且没有自然日历触发。
它们不承担新的生产日历，也不具备 writer 权限。

DataBridge 的 `BFL_DATABRIDGE_PRODUCER=launchd-one-shot` 是防误操作的准入标记，不是
launchd 身份认证。仓库代码的同 UID 调用者属于受信任边界；不能由环境标记或 Python 内部
调用单独证明 natural writer 身份，仍需 installed/loaded/log/run/prediction 现场证据。

`ledger`、`occurrence` 和 `epoch` 不得新增、扩容、迁移或补建，也不得作为新的或过渡生产调度
路径。`daily-gray`、常驻 scheduler 和旧预检仅是待退役兼容代码或历史证据，不是可扩展的
生产入口；`BOND_DAILY_COORDINATOR_MODE=legacy` 在尚存代码中只表示兼容条件，不授予调度权。

## 2. 自然信号、历史修复与输入新鲜度

自然时钟触发的合格生产写入使用 `scheduled_live`。历史缺口的受控、insert-only 修复使用
`gray_live`；两者不能互相伪装、覆盖或以日期标签替代 provenance。回测继续写入
`t_backtest_*`，不与实盘预测混用。

DataBridge 必须由本机 MySQL 原子发布标准日/周/月 artifact，并继续通过源表、schema、
连续性、稳定轮次和 `feature_date` 截止验证。输入不新鲜、源表异常或两轮不稳定时必须
fail-closed：不得发布半成品、不得回退旧 artifact、不得以旧数据制造“成功”信号。

## 3. 生产操作授权

installed plist 的替换或编辑、loaded state 的改变、服务停止或重启、激活、持久化回测、
live 写入和历史补数均是独立生产操作。每项操作先做只读现场核验：比较 repo desired
template、installed plist、loaded state、日志和 run/prediction 证据；再取得明确授权。
开发测试、仓库 plist 或代码通过不自动授予这些权限。本文不提供任何 bootstrap、bootout
或 kickstart 的可执行指令。

自然调度的目标时点为：DataBridge refresh 约 06:30、daily predictions 约 07:03、weekly
predictions 周六 11:30、monthly predictions 自然月 15 日 18:00；actuals 保留既有三个时点，
但任何时点只能有一个 writer。目标时点是治理合同，不是已安装或已观察的现场结论。

## 4. 阶段顺序与停止条件

当前按 G0 → G1/G2 → G3/G4/G5 → G6 推进。G1/G2 未形成真实 launchd 观察闭环前，不授予
新的 scheduler admission。G4 还必须由用户在证据绑定的 scoped waiver 与同源输入/环境重建
之间作出选择；此前不得修改 Native core、source benchmark 或补写该信号。

若 active 集、精确版本、输入截止、installed/loaded state、日志或缺口与已记录证据不符，
或发现两个 writer 可能写同一 business key，立即停止副作用、更新调查结论并重新取得授权。
