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

`BFL_DATABRIDGE_PRODUCER` 仅是防止普通 shell 误 publish 的操作准入标记，不是 launchd
身份认证；它和仓库模板都不能单独证明某个进程由 launchd 启动。

`com.bond-factor-lab.scheduler` 是保留的 disabled 兼容模板，不带自然日历触发，不得成为
新的生产 writer。已退役的 `daily-gray` 与 `v2-preflight` writer 及其仓库模板已移除；该
仓库变更不说明任何 installed plist 的状态。

## 生产操作边界

替换 installed plist、修改 loaded state、启动、停止或重载服务均为独立生产操作。生产
授权之前只能进行只读核对：比较仓库模板、目标机器上的 installed plist、loaded state、
相关日志和 run/prediction 证据；任一项不一致时 fail-closed 并重新取得授权。

本文件不提供 bootstrap、bootout 或 kickstart 的可执行指令，也不声称任何机器已经安装、
加载或停用上述模板。实际生产治理、证据标准与停止条件见
[生产信号与调度治理](../docs/architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。
