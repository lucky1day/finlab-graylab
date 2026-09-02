# 部署期望配置

本目录保存版本控制的部署**期望配置**。它不描述任何机器的安装、加载或停用状态，也不能
替代生产变更授权。

## 双平台一次性控制面

Mac Studio 调度与刷新控制面继续使用 `launchd_one_shot`，阿里云 ECS 独立灰度使用
`systemd_one_shot`。两者只承载一次性 Python 调度/刷新入口，共用同一套严格发现、
DataBridge Gate、repository 写库和进程清理语义；不得同时恢复常驻 scheduler、
APScheduler、ledger 或其它 Python 调度控制面。Backend 是独立常驻只读服务，不拥有
自然写入调度权。

`deploy/systemd/*.service` 与 `deploy/systemd/*.timer` 是 Linux 的仓库期望模板。
文件存在或被复制到 `/etc/systemd/system` 不代表 timer 已启用；现场状态必须用 `systemctl` 读回。
以后替换模板、改变触发、停用、重启或重新启用仍是独立操作，不从仓库文件推断授权。

Linux timer 全部声明 `Persistent=false`，停机或禁用期间不补跑。Backend 模板只监听
`127.0.0.1:8100`，本目录不授权 Nginx、DNS、安全组或公网切流。

ECS 自然 DataBridge、daily、weekly、monthly 四个 service 不得加载共享
`/run/bond-factor-lab/manual-run.env`。这些 unit 的 `EnvironmentFile` 只允许先读取
`/etc/bond-factor-lab/bond-factor-lab.env`，再读取当前 release 的 `.bfl-release.env`，仓库测试精确
守护该合同。历史日期的手工补缺只允许走 `harness signal-gap-fill`：使用
`--predict-date YYYY-MM-DD` 单日执行，或对受支持的 Blackbox `weekly_point/h1`、日频 `T+5/h5`
使用 `--scheme-id/--target-date-from/--target-date-before` target 半开区间执行；systemd/launchd runner 的显式
`--predict-date` 只是一次性入口参数，不建立第二套补缺授权。当天 natural one-shot 部分失败后的受控
`scheduled_live` 重试可以重复传入 `--scheme-id` 精确缩小候选集合；无该参数的 installed timer 行为不变，
该过滤也不绕过 deployment、active cadence、Registry、exact version、日历、输入或 insert-only 控制。

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
`source.tar.gz`、manifest v2 和 archive SHA256。manifest v2 精确仅包含
`schema_version`、`commit`、`root_prefix` 与 `archive.filename/sha256`。
`scripts/install_source_release.py` 要求 operator 另行提供批准的
`--expected-archive-sha256`；安装器校验实际 archive SHA-256 同时等于该独立批准值和 manifest
记录值、archive pax commit 标记等于 manifest commit，以及 archive、解包目录与已安装目录的
source digest 一致。默认只做校验、隔离解包和只读预安装。`--activate` 只接受已经预安装且
重新通过上述校验的 release，并在
显式 `--expected-current` 匹配时更新 `previous/current`；它不包含 SSH、systemctl、launchctl、
数据库、Registry、Nginx 或 DNS 操作。

目标主机必须使用**候选 archive 同版本**的安装器完成预安装与激活，不能从旧 `current` 调用旧版
安装器生成候选 release 环境。任何可能 import 候选/current immutable release 中项目模块的运维或
审查命令，解释器都必须显式携带 `-B`；若同时使用隔离模式，必须写成 `python -I -B`。
`PYTHONDONTWRITEBYTECODE=1` 只能作为非隔离模式下的附加防护，不能替代 `-B`，因为 `-I` 会忽略
`PYTHON*` 环境变量。可以从已激活的 `current` 执行健康检查，也可以直接运行 release 内脚本；禁止
的是在未禁用 bytecode 写入时 import 项目模块。只导入标准库、不 import 项目模块的 release launcher
不受此条限制。当前 launcher 实现只使用标准库；未来若引入项目模块，必须同步改为
`python -I -B`、补充相应回归并重新完成 immutable release 核验。任何环境合同不完整或 source
digest 不一致的未激活目录都应整体隔离，不得现场补写后继续激活。

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

Blackbox 完整持久化回测、activation、revision activation 和环境验证 CLI 通过同一个 selector 选择 frozen
manifest：Linux x86_64 使用 `linux-64`，Mac arm64 使用 `osx-arm64`，其它平台 fail-closed。

这些是仓库候选能力，不表示任一 installed unit/plist 已替换。Mac3 仓库 launchd 模板使用
`/Users/macstudio0/bond-factor-lab-production/current` 作为工作目录，并先由
隔离模式 `/usr/bin/python3 -I` 执行 `scripts/run_launchd_release.py`，从当前已经解析的精确 release
读取 `.bfl-release.env`，再从其可信 `BFL_RUNTIME_ROOT` 唯一派生并加载
`config/service.env`，最后 `exec`
既有 conda 入口。启动器拒绝 Git 工作区、非 `releases/<commit>` 目录、可写/软链接环境文件、commit、
runtime root、Numba/Matplotlib cache 漂移、外层同名环境覆盖以及所有 `PYTHON*` 外置变量。外置配置
目录必须是运行用户拥有的真实 `0700` 目录，文件必须是同一用户拥有的真实 `0400/0600` 普通文件；
launcher 通过目录 fd 与 `O_NOFOLLOW` 打开文件，并在同一 fd 上完成属性检查和限长读取，避免轮换时的
路径替换竞态。解析只识别唯一 `KEY=VALUE`，不会执行 shell 展开。release
安装器同时创建外置
`/Users/macstudio0/bond-factor-lab-runtime/logs` 期望目录；仓库模板的 stdout/stderr 不再写入 Git
工作区。SSH tunnel 不执行项目代码，只使用用户主目录作为工作目录；夜间闭环仍须独立替换并读回
该 plist，保留真实 key/user，同时验证日志精确外置。

上述 `/usr/bin/python3 -I scripts/run_launchd_release.py` 是明确的 stdlib-only launcher 例外：launcher
当前不 import release 内的项目模块，因此不会依赖 `PYTHONDONTWRITEBYTECODE` 防止项目模块 pyc。
若未来 launcher 引入项目模块，模板必须同步改为 `python -I -B`、补充相应回归并重新完成 immutable
release 验收。

分阶段晋级允许两端 `current` 暂时不同，但每个准备晋级另一主机的版本仍必须使用已验证的同一份
精确 archive，不得从环境分支重建“近似版本”。以后预安装、激活、替换 installed plist、
bootstrap/bootout/kickstart 和服务重启仍分别属于生产操作，不能从某次授权外推。

回滚必须同时评估 source release、installed 控制面、数据库和外置 runtime 状态；只切
`current/previous` 不能撤销 writer 已写入的业务数据。保留可用 `previous`、安装记录和回滚审计，任何
prediction writer 已运行后的回滚都必须先重新核对数据库状态、单 Writer 边界和可执行范围。

## Mac Studio launchd 单 writer 目标

Mac Studio 当前生产调度的唯一控制面是 `launchd + installed plist`。仓库中的
`deploy/launchd/*.plist` 只定义候选期望状态；Python runner 仅是对应 plist 启动的
一次性子进程。

| 业务职责 | 仓库模板 | 目标日历 | 一次性入口 |
| --- | --- | --- | --- |
| DataBridge refresh | `com.bond-factor-lab.data-bridge-refresh` | 每日 06:30 | `scripts/refresh_data_bridge_current.py --publish` |
| 日频预测 | `com.bond-factor-lab.daily-predictions` | 工作日 07:03 | `scheduler.launchd_prediction_runner --cadence daily` |
| 周频预测 | `com.bond-factor-lab.weekly-predictions` | 周六 11:30 | `scheduler.launchd_prediction_runner --cadence weekly` |
| 月频/周期均值预测 | `com.bond-factor-lab.monthly-predictions` | 每日 18:00 到期判断 | `scripts/run_close_predictions.py --control-plane launchd --refresh-start 18:00 --refresh-deadline 18:55` |
| Actuals | `com.bond-factor-lab.actuals` | 每日 08:30、19:00、23:45 | `scheduler.actuals_runner` |

所有一次性模板使用 `bond_factor_lab_service`、绝对工作目录和独立 stdout/stderr 日志。
DataBridge 模板还声明 `BFL_DATABRIDGE_PRODUCER=launchd-one-shot`。模板中不保存 DSN、
凭证。

所有 one-shot 仓库期望模板均不得声明 `BOND_DAILY_COORDINATOR_MODE`；该变量不授予调度权。
修改任一 installed 或仓库 plist 都仍是独立生产操作，不由本次代码清理推断授权。

`BFL_DATABRIDGE_PRODUCER` 仅是防止普通 shell 误 publish 的操作准入标记，不是 launchd
身份认证；它和仓库模板都不能单独证明某个进程由 launchd 启动。

Actuals 只由 `scheduler.actuals_runner` 驱动；Backend 不提供手动预测或 HTTP 写入路由，
也不得恢复常驻调度器或第二 Writer。已安装 legacy plist 的物理清理仍是独立生产操作，
不由仓库期望配置推断或执行。

## Backend 认证外置配置

认证启用后，Backend 外置配置必须提供与部署目标精确匹配的
`BFL_AUTH_TRUSTED_ORIGIN`：ECS `aliyun-gray` 只能使用
`http://localhost:18110`，Mac3 `mac3-production` 只能使用
`https://bond.finailab.cn`。缺失、交叉或其它 Origin 均使所有状态变更请求 fail-closed。
该变量不得写入 release 内 `.bfl-release.env`，也不得改变任何 one-shot unit/plist。

受保护初始管理员只通过 `scripts/manage_auth_admin.py` 初始化或离线重置；密码只从部署目标固定的
owner-only secret 文件读取，命令行、环境变量、日志和安装记录均不得承载密码。ECS 与 Mac3
独立初始化，不复制用户、会话、哈希或审计数据。详细合同见
[登录与账户管理](../docs/architecture/AUTHENTICATION_AND_ACCOUNT_MANAGEMENT.md)。

Mac3 认证回滚使用
`deploy/nginx/bond-factor-lab-lockdown.conf` 作为完整 replacement site；它不得与正常
site 同时启用，并对 `/bond-factor-lab` 及其全部子路径统一返回 503，未知路径仍返回
403。该文件只用于预安装和 `nginx -t`，是否切换仍需独立生产故障处置授权。

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
  python -B scripts/audit_launchd_config_drift.py
```

脚本只读取 `deploy/launchd/*.plist`、`~/Library/LaunchAgents/*.plist` 与
`launchctl print`；它没有修改、reload、bootout、bootstrap 或 kickstart 功能。JSON 只报告
变量名、存在性、语义差异路径及启动参数/触发器 hash，不输出 token、DSN、SSH 用户、密钥路径或
环境变量值。

审计固定禁止所有正式模板和 installed 环境出现
`BOND_DAILY_COORDINATOR_MODE`，并要求 DataBridge 保留
`BFL_DATABRIDGE_PRODUCER=launchd-one-shot`。SSH 隧道模板明确标注且已验证的本机 key/user 是唯一批准的 plist 本机差异。
`service.env` 在顶层只校验一次，报告只含变量名与错误类别；Backend/tunnel 必须处于 running，七个
任务的日志路径必须与外置 runtime 模板精确一致。其余启动参数、工作目录、调度触发器和环境变量差异
仍使脚本退出 `1`。脚本退出 `0` 只表示当前只读配置审计通过，不代表生产任务已自然运行成功，也不
授予任何生产操作权限。
