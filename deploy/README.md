# 部署期望配置

本目录保存版本控制的部署**期望配置**。它不描述任何机器的安装、加载或停用状态，也不能
替代生产变更授权。

## launchd 单 writer 目标

生产调度的唯一控制面是 `launchd + installed plist`。仓库中的
`deploy/launchd/*.plist` 只定义候选期望状态；Python runner 仅是对应 plist 启动的
一次性子进程。

| 业务职责 | 仓库模板 | 目标日历 | 一次性入口 |
| --- | --- | --- | --- |
| DataBridge refresh | `com.bond-factor-lab.data-bridge-refresh` | 每日 06:30 | `scripts/refresh_data_bridge_current.py --publish` |
| 日频预测 | `com.bond-factor-lab.daily-predictions` | 工作日 07:03 | `scheduler.launchd_prediction_runner --cadence daily` |
| 周频预测 | `com.bond-factor-lab.weekly-predictions` | 周六 11:30 | `scheduler.launchd_prediction_runner --cadence weekly` |
| 月频预测 | `com.bond-factor-lab.monthly-predictions` | 自然月 15 日 18:00 | `scheduler.launchd_prediction_runner --cadence monthly` |
| Actuals | `com.bond-factor-lab.actuals` | 每日 08:30、19:00、23:45 | `scheduler.actuals_runner` |

所有一次性模板使用 `bond_factor_lab_service`、绝对工作目录和独立 stdout/stderr 日志。
DataBridge 模板还声明 `BFL_DATABRIDGE_PRODUCER=launchd-one-shot`。模板中不保存 DSN、
凭证、admin token 或实例 nonce。

DataBridge 与 daily / weekly / monthly 四个 one-shot **仓库期望模板**均不声明
`BOND_DAILY_COORDINATOR_MODE`。平台代码已经不再读取该旧变量，算法子进程使用显式 allowlist，
不会转发它；backend 仓库模板也不再声明该变量。旧 installed 环境若仍携带它，只是惰性兼容配置，
不授予调度权。修改任一
installed 或仓库 plist 都仍是独立生产操作，不由本次代码清理推断授权。

`BFL_DATABRIDGE_PRODUCER` 仅是防止普通 shell 误 publish 的操作准入标记，不是 launchd
身份认证；它和仓库模板都不能单独证明某个进程由 launchd 启动。

仓库已移除 `com.bond-factor-lab.scheduler` 的 disabled legacy 模板和常驻
`scheduler.main` 模块。actuals 由独立的 `scheduler.actuals_runner` 负责；Backend 不注册
手动预测路由。已退役的
`daily-gray` 与 `v2-preflight` writer 及其仓库模板也已移除。任何已安装 disabled legacy
plist 的物理删除仍是独立生产操作，不由仓库期望配置推断或执行。

## 生产操作边界

替换 installed plist、修改 loaded state、启动、停止或重载服务均为独立生产操作。生产
授权之前只能进行只读核对：比较仓库模板、目标机器上的 installed plist、loaded state、
相关日志和 run/prediction 证据；任一项不一致时 fail-closed 并重新取得授权。

本文件不提供 bootstrap、bootout 或 kickstart 的可执行指令，也不声称任何机器已经安装、
加载或停用上述模板。实际生产治理、证据标准与停止条件见
[生产信号与调度治理](../docs/architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。

## 只读配置漂移审计

在生产变更前或 installed plist 调整后，可运行以下只读检查：

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/audit_launchd_config_drift.py
```

脚本只读取 `deploy/launchd/*.plist`、`~/Library/LaunchAgents/*.plist` 与
`launchctl print`；它没有修改、reload、bootout、bootstrap 或 kickstart 功能。JSON 只报告
变量名、存在性、语义差异路径及启动参数/触发器 hash，不输出 token、DSN、SSH 用户、密钥路径或
环境变量值。

审计固定禁止所有正式模板和 installed 环境出现
`BOND_DAILY_COORDINATOR_MODE`，并要求 DataBridge 保留
`BFL_DATABRIDGE_PRODUCER=launchd-one-shot`。Backend 的 admin token/日志路径，以及 SSH 隧道
模板明确标注的本机身份参数/日志路径，只作为批准的本机差异；其余启动参数、工作目录、调度触发器
和环境变量差异仍使脚本退出 `1`。脚本退出 `0` 只表示当前只读配置审计通过，不代表生产任务已
自然运行成功，也不授予任何生产操作权限。
