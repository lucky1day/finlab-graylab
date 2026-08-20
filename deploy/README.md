# 部署期望配置

本目录保存版本控制的部署**期望配置**。它不描述任何机器的安装、加载或停用状态，也不能
替代生产变更授权。

## 双平台一次性控制面

Mac Studio 继续使用 `launchd_one_shot`，阿里云 ECS 独立灰度使用
`systemd_one_shot`。两者只承载一次性 Python 入口，共用同一套严格发现、
DataBridge Gate、repository 写库和进程清理语义；不得同时恢复常驻 scheduler、
APScheduler、ledger 或其它 Python 调度控制面。

`deploy/systemd/*.service` 与 `deploy/systemd/*.timer` 是 Linux 的仓库期望模板。
文件存在或被复制到 `/etc/systemd/system` 不代表 timer 已启用。ECS 五个 timer 已于 2026-08-18
经专项授权启用，当前现场状态仍须用 `systemctl` 读回；以后替换模板、改变触发、停用、重启或重新
启用仍是独立操作，不从仓库文件推断授权。

Linux timer 全部声明 `Persistent=false`，停机或禁用期间不补跑。Backend 模板只监听
`127.0.0.1:8100`，本目录不授权 Nginx、DNS、安全组或公网切流。

## 部署目标与方案矩阵

Mac3 的应用 launchd 模板固定声明
`BFL_DEPLOYMENT_TARGET=mac3-production`，ECS 的应用 systemd service 固定声明
`BFL_DEPLOYMENT_TARGET=aliyun-gray`。生产 one-shot 的目标与控制面必须分别为
`launchd_one_shot/mac3-production` 和 `systemd_one_shot/aliyun-gray`；缺失或交叉配对会在
DataBridge 配置、数据库连接和算法子进程之前失败。

`deploy/scheme_deployment_matrix_v1.json` 是唯一主机资格清单。未设置目标的开发和 Harness
发现保持全量；生产服务设置目标后，discovery 严格校验矩阵与全部 config 一一覆盖再过滤。
矩阵不自动删除或暂停 Registry 行；目标移除与加入仍须分别完成受控 Registry 生命周期和读回。
模板变更不表示 installed launchd/systemd 已更新，现场安装、重载、启停和 timer 状态改变均需独立授权。

## 源码 release 与外置状态

`scripts/build_source_release.py` 只接受 clean Git worktree 的当前 `HEAD`，生成 deterministic
`source.tar.gz`、精确 commit/tree manifest 和 archive SHA256。`scripts/install_source_release.py`
要求 operator 另行提供批准的 `--expected-archive-sha256`；默认只做校验、隔离解包、source tree
digest 和只读预安装。`--activate` 只接受已经预安装且重新通过 archive/tree 校验的 release，并在
显式 `--expected-current` 匹配时更新 `previous/current`；它不包含 SSH、systemctl、launchctl、
数据库、Registry、Nginx 或 DNS 操作。

目标主机必须使用**候选 archive 同版本**的安装器完成预安装与激活，不能从旧 `current` 调用旧版
安装器生成候选 release 环境。运行安装器时必须设置 `PYTHONDONTWRITEBYTECODE=1`，并从候选 release
目录之外调用；不得为二次校验直接执行只读 release 内的 Python 文件，以免 root 生成 `__pycache__`
后触发 source integrity 拒绝。任何环境合同不完整或 source digest 不一致的未激活目录都应整体隔离，
不得现场补写后继续激活。

激活拒绝同 SHA 重试，避免覆盖可用的 `previous`。revision intent 和 `previous` 都在切换前完成，
`current` 的原子替换是最后一个强制文件动作；命令返回后以 `current` 现场读回作为是否激活的最终
authority。既有 deploy/runtime root 必须是当前 operator 所有的真实目录，且不能 group/world
writable；工具不会静默修正不安全目录。

安装器在 release 内生成 `.bfl-release.env`，包含精确的
`BFL_RELEASE_COMMIT`、`BFL_RUNTIME_ROOT`，以及
`NUMBA_CACHE_DIR=<runtime-root>/cache/native/<exact-release-commit>/numba` 和
`MPLCONFIGDIR=<runtime-root>/cache/native/<exact-release-commit>/matplotlib`。ECS 的六个仓库
systemd 候选模板读取该文件，使无 `.git` release 仍有稳定代码身份，并让状态路径位于源码 release
外。安装器生成这些路径；operator 不得在 ECS `/etc` 或 Mac3 launchd 配置中重复声明或覆盖它们。
同一 host、同一 `BFL_RUNTIME_ROOT` 且同一精确 commit 复用同一 Numba/Matplotlib cache；不同 commit
路径保持隔离。`runtime_root` 必须是
非根目录、仅含字母、数字、`/._-` 的绝对 ASCII-safe 路径；不安全形式会在安装器进行任何文件系统
变更前 fail-closed。这些第三方 cache 位于 immutable source 外，不改变既有 per-scheme cache 的
优先级：例如 `LIWEI_0616_PHASE_A_CACHE_ROOT` 仍优先于统一根，因此可以原样复用已校验缓存；没有
单项覆盖时，DataBridge、artifact 和 source cache 才从 `BFL_RUNTIME_ROOT` 派生。设置了生产部署
目标却缺少所需 release 环境时，代码 fail-closed。

Blackbox Gate、activation、revision activation 和环境验证 CLI 通过同一个 selector 选择 frozen
manifest：Linux x86_64 使用 `linux-64`，Mac arm64 使用 `osx-arm64`，其它平台 fail-closed。

这些是仓库候选能力，不表示任一 installed unit/plist 已替换。Mac3 仓库 launchd 模板使用
`/Users/macstudio0/bond-factor-lab-production/current` 作为工作目录，并先由
隔离模式 `/usr/bin/python3 -I` 执行 `scripts/run_launchd_release.py`，从当前已经解析的精确 release
读取 `.bfl-release.env`，再 `exec`
既有 conda 入口。启动器拒绝 Git 工作区、非 `releases/<commit>` 目录、可写/软链接环境文件、commit、
runtime root、Numba/Matplotlib cache 漂移、外层同名环境覆盖以及 `PYTHONPATH/PYTHONHOME`。release
安装器同时创建外置
`/Users/macstudio0/bond-factor-lab-runtime/logs` 期望目录；仓库模板的 stdout/stderr 不再写入 Git
工作区。SSH tunnel 不执行项目代码，只使用用户主目录作为工作目录；夜间闭环仍须独立替换并读回
该 plist，保留真实 key/user，同时验证日志精确外置。

2026-08-20 R1 已冻结为 tag `mac3-immutable-r1-20260820`；但 installed Mac3 plist 和 loaded
launchd 尚未替换，生产仍由原 Git 工作区运行。预安装、激活、替换 installed plist、
bootstrap/bootout/kickstart、Backend 重启和开发工作区切分均属于后续夜间窗口的独立生产操作。

## Mac Studio launchd 单 writer 目标

Mac Studio 当前生产调度的唯一控制面是 `launchd + installed plist`。仓库中的
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

替换 installed plist/unit/timer、修改 loaded state、启动、停止或重载服务均为独立操作。操作
授权之前只能进行只读核对：比较仓库模板、目标机器上的 installed 配置、loaded state、
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
`BFL_DATABRIDGE_PRODUCER=launchd-one-shot`。只有 Backend 的真实 admin token 值，以及 SSH 隧道
模板明确标注且已验证的本机 key/user，属于批准的本机差异；Backend/tunnel 必须处于 running，七个
任务的日志路径必须与外置 runtime 模板精确一致。其余启动参数、工作目录、调度触发器和环境变量
差异仍使脚本退出 `1`。脚本退出 `0` 只表示当前只读配置审计通过，不代表生产任务已自然运行成功，
也不授予任何生产操作权限。
