# 运行信号与调度治理

**文档状态**：`CURRENT`

**目标读者**：平台开发、运维、审计和方案维护人员

本文定义 Mac3 生产与 ECS 独立灰度的调度控制面。当前稳定事实查看[当前状态](../CURRENT_STATUS.md)；具体运行证据由 installed state、日志、run、prediction、Harness 和数据库审计保存，不在文档复制一次性计划。

## 1. 唯一控制面与证明标准

每个部署目标只有一个宿主调度控制面：Mac3 生产使用 `launchd + installed plist`，ECS 独立灰度
使用 `systemd + installed unit/timer`。只有 installed 配置、对应 loaded state、任务日志、run 和
prediction 相互一致，才能证明任务已挂载；仓库 `deploy/launchd/` 与 `deploy/systemd/` 只是期望配置，
Python 模块只是被宿主控制面调用的一次性执行器。

一个部署目标和数据库 authority 内，每个 cadence 只能有一个自然 writer。Mac3 与 ECS 当前写各自
独立数据库，不构成同一 business key 上的双写；任一主机都不得让常驻 APScheduler、已退役 writer、
预检进程或手工进程同时拥有自然写入权。

生产调度控制面与生产源码 authority 也必须分开。ECS service 只从 `/opt/bond-factor-lab/current`
immutable release 启动；Mac3 仓库候选只从 `/Users/macstudio0/bond-factor-lab-production/current`
启动，并由 `scripts/run_launchd_release.py` 核验 `.bfl-release.env` 后 `exec` 既有入口。launcher 不增加
调度权，只保证 commit、runtime root 和第三方 cache 与当前精确 release 一致。仓库模板不能证明
installed 状态；在 Mac3 installed plist 完成独立切换前，现场仍可能运行旧 Git 工作区，必须以
plist、`launchctl` 和进程 cwd 读回判定，不能提前切换或清理开发工作区。

两个宿主控制面使用相同业务日历：DataBridge 06:30、daily 工作日 07:03、weekly 周六 11:30、
close-period 每日 18:00、Actuals 每日 08:30/19:00/23:45。close-period 复用原 monthly 控制面：
自然月 15 日运行既有月中收任务，交易日又是 MID/CQ/SF 锚点时运行到期周期均值任务，普通日期直接 no-op。
Mac3 对应
`com.bond-factor-lab.*` LaunchAgent，ECS 对应 `bond-factor-lab-*.timer`。仓库模板只表达 desired
state；当前 installed/loaded 状态以各自主机读回为准。

daily、weekly、close-period one-shot runner 都先严格发现方案；其自然候选集合只由
`status=active`、Blackbox exact `version_status=active` 与明确 `task_type` 匹配当前 cadence
决定。`paused`、`draft` 和其它 cadence 不进入本批次，不再另设 release queue、`mode` 或
capability 准入。Blackbox Admission 代码与配置已经退役；历史身份变化只通过 Git、Harness
run 和授权审计追溯，不再保留第二份当前权限矩阵。

平台只允许宿主控制面调用一次性 runner；不得恢复常驻 Python scheduler、manual writer 或其它能够拥有
自然写入权的第二控制面。历史 installed 配置的物理清理仍须只读核对与独立生产授权。

DataBridge 在 Mac3 使用 `BFL_DATABRIDGE_PRODUCER=launchd-one-shot`，在 ECS 使用
`BFL_DATABRIDGE_PRODUCER=systemd-one-shot`；两者都是防误操作的准入标记，不是宿主控制面身份认证。
仓库代码的同 UID 调用者属于受信任边界；不能由环境标记或 Python 内部
调用单独证明 natural writer 身份，仍需 installed/loaded/log/run/prediction 现场证据。

`ledger`、`occurrence` 和 `epoch` 不得新增、扩容、迁移或补建，也不得作为新的或过渡生产调度路径。
历史 migration 与现存数据库对象只用于审计和受控 recovery；物理归档或 DDL 必须另行设计和授权。

## 2. 自然信号、历史修复与输入新鲜度

自然时钟触发的合格生产写入使用 `scheduled_live`。历史缺口的受控、insert-only 修复使用
`gray_live`；两者不能互相伪装、覆盖或以日期标签替代 provenance。回测继续写入
`t_backtest_*`，不与实盘预测混用。

历史缺口保留单日入口；单个 active Blackbox `weekly_point/h1` 或日频 `T+5/h5` 方案还可以按 target 半开区间批量补齐。命令内部先执行只读 planner，任一 blocker 都会在算法或 repository 写入前终止：

```bash
python -m harness signal-gap-fill --predict-date YYYY-MM-DD
python -m harness signal-gap-fill --predict-date YYYY-MM-DD --scheme-id <base_scheme_id>
python -m harness signal-gap-fill --scheme-id <base_scheme_id> --target-date-from YYYY-MM-DD --target-date-before YYYY-MM-DD
```

命令不接收外部 plan、operator、HMAC token 或 plan SHA。单日全量模式扫描当天所有应运行的 active 方案；区间模式只检查一个指定的 active Blackbox `weekly_point/h1` 或日频 `T+5/h5` base scheme，并按同一权威交易日历枚举日期。`SKIP_NOT_DUE` 和 `SKIP_PRESENT` 都是正常无写入结果；只有真实 `GRAY_LIVE_GAP` 才进入执行。Native 从当前数据库按指定日期推导的 `feature_date` 截止重建；Blackbox 严格重放该批次计划绑定的冻结 DataBridge authority。

单日模式按 base scheme 独立执行和提交；一个方案失败不回滚其它已经成功的方案，重试由既有 immutable
业务键自然缩小到剩余缺口。单方案多 target 与区间模式仍保持组内原子性：区间模式先复核全部业务键，
再在一个事务中提交该方案的所有日期，最后逐日期权威读回。计划异常、Blackbox 权威输入缺失或执行失败
均直接暴露；不回退旧版本、不覆盖、不自动重试。

自然 daily one-shot 的 Liwei Phase-A publisher 可以在既有校验证明影响范围时，对最多 32 个交易日期做
bounded suffix reconcile；consumer 只读发布后的 generation。`full`、未知修订、无法映射的周/月修订或
cache 身份漂移必须在训练前失败。人工 `signal-gap-fill` 仍只允许 cache hit 或追加一个尾部日期。当天自然
运行部分失败后的受控重试可以向现有 launchd/systemd runner 重复传入 `--scheme-id <base_scheme_id>`，但
该参数只缩小 active cadence 候选，不能绕过 deployment、Registry、exact version、日历、输入或 insert-only
检查，也不能把历史日期重标为 `scheduled_live`。

DataBridge 必须由本机 MySQL 原子发布标准日/周/月 artifact，并继续通过源表、schema、
连续性、稳定轮次和 `feature_date` 截止验证。输入不新鲜、源表异常或两轮不稳定时必须
fail-closed：不得发布半成品、不得回退旧 artifact、不得以旧数据制造“成功”信号。

close-period 入口在任何预测前先校验 current ready 的 `feature_date` 是否精确覆盖当日所需锚点。若已覆盖，
同一次已发布快照可供月中收和周期均值顺序复用；若未覆盖，入口只执行一次 DataBridge 收盘刷新并再次核验。
刷新失败或截止不匹配时本批预测零执行。这个按需刷新属于原 monthly 控制面的前置动作，不新增周期均值 timer、
常驻 scheduler 或第二个 DataBridge writer。

## 3. 生产操作授权

installed plist/unit/timer 的替换或编辑、loaded state 的改变、服务停止或重启、激活、持久化回测和
单日历史补数均是独立操作。每项操作先做只读现场核验：比较 repo desired template、
installed 配置、loaded state、日志和 run/prediction 证据；再取得明确授权。开发测试、仓库模板或
代码通过不自动授予这些权限。本文不提供 `launchctl` 或 `systemctl` 的变更指令。

## 4. 生命周期与停止条件

Blackbox activation 前必须完成 Intake、同 exact version/当前校验策略的一次完整持久化回测、生产准备核验与专项授权；activation 本身不安装或加载 plist/unit。Blackbox 不运行 Native Gate 编排。首次 Native 技术入库使用 current exact version 的完整四段 `all`（`static → dry-run → compare → backtest`，DryRun 含真实输入合同）；同一存量身份维护只有在 prior `all` 已有匹配 `static.business_identity` 时才可使用三段 `native-maintenance`（`static → native-maintenance-admission → dry-run`）。两条 Native profile 互斥，缺失或不一致时直接失败。

`static.business_identity` 只包含 scheme/runtime/horizon/task/frequency/tenors/composite IDs，不含代码、config 或 version hash。maintenance 的 current exact version 必须为 native `draft|active`，Registry 必须统一 paused（预激活）或 active（激活后），draft version 配 active Registry 必须失败。缺少标准 prior snapshot 时直接回到完整 `all`，不再读取方案级历史 receipt。

技术 `all` 不访问 Backend。方案激活后唯一产品读模型检查为 `dashboard` Gate，它只读取
`/api/factor-lab/dashboard` 并验证 active composite、信号和回测分区可见；dashboard payload
不携带 exact version，因此该 Gate 不能证明某个 exact version，版本身份仍由本机数据库 exact version 与
Registry 权威回读证明。Blackbox 首次激活和 revision 均为单数据库事务；失败整体回滚，不存在跨文件
补偿、pending journal 或 reconcile 控制面。

出现以下任一情况时立即停止副作用并保留证据：

- active scope、exact version、Registry identity、输入截止或日期语义与预检不一致；
- 对应主机的 installed/loaded state、日志、run 和 prediction 不能相互证明同一自然运行；
- 发现两个 writer 可能写同一 business key；
- 需要 fallback、旧版本切换、覆盖、自动重试或恢复已退役控制面才能继续。
