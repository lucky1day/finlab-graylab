# 生产信号与调度治理

**文档状态**：`CURRENT`

**目标读者**：平台开发、运维、审计和方案维护人员

**最后核验日期**：2026-08-10

本文定义生产信号的唯一控制面。当前稳定事实查看[当前状态](../CURRENT_STATUS.md)；具体运行证据由 installed state、日志、run、prediction、Harness 和数据库审计保存，不在文档复制一次性计划。

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

历史缺口可以先用 `signal-gap-plan` 只读检查；运维补齐只保留单日入口，并可选限定一个
`base_scheme_id`：

```bash
python -m harness signal-gap-fill --predict-date YYYY-MM-DD
python -m harness signal-gap-fill --predict-date YYYY-MM-DD --scheme-id <base_scheme_id>
```

该命令在进程内生成 `single-date-active-live-gap-plan-v1` 计划；不接收外部 plan、日期范围、
operator、HMAC token 或 plan SHA。全量模式扫描当天所有应运行的 active 方案，单方案模式只检查
指定的 active base scheme。`SKIP_NOT_DUE` 和 `SKIP_PRESENT` 都是正常无写入结果；只有真实
`GRAY_LIVE_GAP` 才进入执行。Native 从当前数据库按指定日期推导的 `feature_date` 截止重建；
Blackbox 严格重放该批次计划绑定的冻结 DataBridge authority。

所有缺口算法必须先全部成功，任一算法失败则 prediction 零提交；算法全部成功后才按
base scheme group 执行 insert-only `gray_live` 写入，并只做一次同日期最终权威读回。计划异常、
Blackbox 权威输入缺失或执行失败均直接暴露；不回退旧版本、不覆盖、不自动重试。

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

## 4. 生命周期与停止条件

Activation 前必须完成对应 Gate、生产准备核验与一次性专项授权；Activation 本身不安装或加载 plist。首次 Native 技术入库使用 current exact version 的完整六段 `all`（`static → input → unit → dry-run → compare → backtest`）；同一存量身份维护只有在 prior `all` 已有匹配 `static.business_identity` 时才可使用五段 `native-maintenance`（`static → native-maintenance-admission → input → unit → dry-run`）。两条 profile 互斥，缺失或不一致时直接失败。

`static.business_identity` 只包含 scheme/runtime/horizon/task/frequency/tenors/composite IDs，不含代码、config 或 version hash。maintenance 的 current exact version 必须为 native `draft|active`，Registry 必须统一 paused（预激活）或 active（激活后），draft version 配 active Registry 必须失败。唯一固定的 legacy identity receipt 只服务 `weekly_10y_d_overlay_0529` 的既有缺快照 prior，并仍须完整五段 maintenance 与独立 activation；它不能写业务表、启动调度或外推到其它身份。

技术 `all` 不访问 Backend。方案激活后唯一产品读模型检查为 `dashboard` Gate，它只读取
`/api/factor-lab/dashboard` 并验证 active composite、信号和回测分区可见；dashboard payload
不携带 exact version，因此该 Gate 不能证明某个 exact version，版本身份仍由生命周期与数据库
权威回读证明。

Blackbox 任一 lifecycle journal 处于 pending 时，新的 shadow、activate 或 revision activate
必须直接阻断，不得在授权前隐式恢复。唯一恢复入口是显式 HMAC 授权的
`blackbox_reconcile`：它只回退到原 journal 记录的 previous safe state，保留原 journal 不变，
并新建与其关联的 reconciliation journal 记录全过程；失败继续保留 pending 证据。

出现以下任一情况时立即停止副作用并保留证据：

- active scope、exact version、Registry identity、输入截止或日期语义与预检不一致；
- installed/loaded state、日志、run 和 prediction 不能相互证明同一自然运行；
- 发现两个 writer 可能写同一 business key；
- 需要 fallback、旧版本切换、覆盖、自动重试或恢复已退役控制面才能继续。
