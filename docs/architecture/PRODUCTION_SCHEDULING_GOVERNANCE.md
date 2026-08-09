# 生产信号与调度治理

**文档状态**：`CURRENT`

**目标读者**：平台开发、运维、审计和方案维护人员

**最后核验日期**：2026-08-09

本文定义生产信号的唯一控制面。具体现场事实查看[当前状态](../CURRENT_STATUS.md)，
分阶段治理和带日期的证据查看
[2026-08-03 生产信号与调度治理计划](../records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md)。

## 1. 唯一控制面与证明标准

`launchd + installed plist` 是唯一生产调度控制面。只有 installed plist、对应
`launchctl` loaded state、任务日志、run 和 prediction 相互一致，才能证明任务已生产挂载；
仓库 `deploy/launchd/*.plist` 只是期望配置，Python 模块只是被 plist 调用的一次性执行器。

一个 cadence 只能有一个生产 writer。DataBridge refresh、daily prediction、weekly
prediction、monthly prediction 和 actuals 分别由明确的 LaunchAgent 触发；不得让常驻
APScheduler、已退役 writer、预检进程或任何手工进程同时拥有同一 business key 的自然写入权。

仓库期望模板的单 writer 映射为：`com.bond-factor-lab.data-bridge-refresh` 于 06:30
发布 DataBridge；`com.bond-factor-lab.daily-predictions` 于工作日 07:03、
`com.bond-factor-lab.weekly-predictions` 于周六 11:30、
`com.bond-factor-lab.monthly-predictions` 于自然月 15 日 18:00 分别启动相应 cadence 的
one-shot runner；`com.bond-factor-lab.actuals` 保持 08:30、19:00、23:45 的既有唯一
writer，并启动 `scheduler.actuals_runner`。这些是仓库 desired state，不是机器安装、加载或
停用的现场结论。

daily、weekly、monthly one-shot runner 都先严格发现方案；其自然候选集合只由
`status=active`、Blackbox exact `version_status=active` 与 `frequency` 匹配当前 cadence
决定。`paused`、`draft` 和其它 cadence 不进入本批次，不再另设 release queue、`mode` 或
capability 准入。Blackbox Admission 代码与配置已经退役；历史身份变化只通过 Git、Harness
run 和授权审计追溯，不再保留第二份当前权限矩阵。

仓库已移除 `com.bond-factor-lab.scheduler` 的 `Disabled=true` legacy 模板和常驻
`scheduler.main` 模块。actuals 的唯一 runner 为 `scheduler.actuals_runner`；Backend 不注册
手动预测路由，也不存在可把手工请求写成第三种实盘阶段的 manual writer。
已退役的 `daily-gray` 与 `v2-preflight` writer 及其仓库模板也已移除。已安装 disabled legacy
plist 是否仍存在、何时物理删除，仍须只读核对与独立生产授权。

DataBridge 的 `BFL_DATABRIDGE_PRODUCER=launchd-one-shot` 是防误操作的准入标记，不是
launchd 身份认证。仓库代码的同 UID 调用者属于受信任边界；不能由环境标记或 Python 内部
调用单独证明 natural writer 身份，仍需 installed/loaded/log/run/prediction 现场证据。

`ledger`、`occurrence` 和 `epoch` 不得新增、扩容、迁移或补建，也不得作为新的或过渡生产调度
路径。相应的 repository/runtime/replay/policy 闭包已从仓库退役；017 历史 migration 与仍可能
存在的数据库对象只保留为审计和受控 recovery 证据，任何物理归档或 DDL 仍须独立设计和授权。
`BOND_DAILY_COORDINATOR_MODE` 不再被平台代码读取，算法子进程环境也不会转发它，仓库模板亦不再
声明该变量。旧 installed 环境若仍携带它，只是惰性兼容配置，不授予任何调度权，也不构成现场状态结论。

## 2. 自然信号、历史修复与输入新鲜度

自然时钟触发的合格生产写入使用 `scheduled_live`。历史缺口的受控、insert-only 修复使用
`gray_live`；两者不能互相伪装、覆盖或以日期标签替代 provenance。回测继续写入
`t_backtest_*`，不与实盘预测混用。

历史缺口先用 `signal-gap-plan` 只读检查；获授权的运维补齐只保留单日入口：

```bash
python -m harness signal-gap-fill --predict-date YYYY-MM-DD
```

该命令扫描当天所有应运行的 active 方案，冻结计划，仅对真实缺口按原子方案组在进程内签发
精确短期 token，并统一 insert-only 写入 `gray_live`。Native 从当前数据库按指定日期推导的
`feature_date` 截止重建，token 的 source authority 为 `null`；Blackbox 继续严格绑定冻结的
DataBridge authority。缺少 HMAC secret、Blackbox 权威输入、计划异常或算法失败均直接退出；
不回退旧版本、不覆盖、不重试。写后必须由同日期权威 plan 确认缺口为零。

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

当前按 G0 → G1/G2 → G3/G5 → G6 推进；G4 已独立闭环。G1/G2 是否形成真实 launchd 观察闭环
只决定能否宣称已生产挂载或 `Production Observed`，不改变 active + exact active + cadence 的
自然候选规则。Activation 前仍须完成原有 Gate、生产准备核验与一次性专项授权；Activation
本身不安装或加载 plist。G4 已确认不需要 scoped waiver 或同源输入/环境重建选择：首次
Native 技术入库仍必须通过 source benchmark/CompareGate；ActivationGate 的 full-`all` 与
`native-maintenance` profile 互斥：current exact version 完整 `all` 通过时使用
`full_initial_onboarding_v1`，不要求 prior snapshot；只有 maintenance profile 才需要 prior
`all` 的匹配 `static.business_identity` 快照，历史 benchmark input-vintage 漂移才只归档。
该快照只含业务字段（scheme/runtime/horizon/task/frequency/tenors/composite IDs），不含代码、
config 或 version hash。maintenance 的 current exact `t_scheme_versions` 行必须为
`runtime_type='native_adapter'` 且 status 为 `draft|active`；expected Registry identity 可在预激活时
统一为 `paused`，或在激活后统一为 `active`，但 draft version 配 active Registry 必须 fail-closed。
只有 ActivationGate 可在严格 discovery、精确版本与一次性授权核验后原子建立 active 状态。legacy admission 缺快照时仍 fail-closed；唯一保留的固定 scope 是 `weekly_10y_d_overlay_0529` 已持久化的 canonical receipt；平台只读校验 maintenance 选定的 prior、缺 identity 的 StaticGate 与固定业务身份，writer 和 token action 已退役。该 receipt 不改历史、不启动调度、不激活或写业务表；2026-08-04 receipt 后，exact version `e50ad79a6c2f` 的六段 maintenance 与独立 activation 均已通过，DB version 与 Registry 均为 active。其历史补缺证据仍保留在 run 与状态记录中，但不构成当前 Native artifact/generation 控制面。任何后续修订仍必须满足输入 cutoff、统一周历、日期语义、
Registry、live-safe oracle 与专项授权，随后也只能补其精确授权的 `gray_live` key；不得修改
Native core 或 source benchmark。

若 active 集、精确版本、输入截止、installed/loaded state、日志或缺口与已记录证据不符，
或发现两个 writer 可能写同一 business key，立即停止副作用、更新调查结论并重新取得授权。
