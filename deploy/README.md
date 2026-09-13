# 部署、发布与恢复

**文档状态**：`CURRENT`

本文维护部署期望配置、不可变 release、环境加载、数据库迁移及恢复流程。固定地址、SSH/转发、
生产路径和只读现场命令只在[双机部署与访问入口](../docs/operations/DEPLOYMENT_ACCESS.md)维护；
已核验版本及备份位置见[当前状态](../docs/CURRENT_STATUS.md)。

生产权限遵守[根规范](../AGENTS.md#开发与操作权限)；仓库模板不证明 installed/loaded。
自然 Writer、手工补缺和现场证据标准见[调度治理](../docs/architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。

## 部署目标与一次性模板

| 部署目标 | 唯一自然控制面 | 期望模板 |
|---|---|---|
| `mac3-production` | `launchd_one_shot` | [`launchd/`](launchd/) |
| `aliyun-gray` | `systemd_one_shot` | [`systemd/`](systemd/) |

生产 one-shot 必须使用对应的 `BFL_DEPLOYMENT_TARGET` 与控制面组合；缺失或交叉配对在
DataBridge 配置、数据库连接和算法子进程之前失败。
[`scheme_deployment_matrix_v1.json`](scheme_deployment_matrix_v1.json) 是主机资格唯一清单：
未设目标的开发/Harness 发现全量，生产 discovery 校验矩阵完整覆盖 config 后过滤。
矩阵增删不自动建立、暂停或删除 Registry，仍须受控生命周期事务与读回。

两平台期望日历均使用 Asia/Shanghai：

| 职责 | Mac3 label 后缀 / ECS unit 后缀 | 期望日历 | 一次性入口 |
|---|---|---|---|
| DataBridge | `data-bridge-refresh` / `data-bridge` | 每日 06:30 | `scripts/refresh_data_bridge_current.py --publish` |
| 日频预测 | `daily-predictions` / `prediction-daily` | 工作日 07:03 | 对应宿主 prediction runner，`--cadence daily` |
| 周频预测 | `weekly-predictions` / `prediction-weekly` | 周六 11:30 | 对应宿主 prediction runner，`--cadence weekly` |
| 月频/周期均值 | `monthly-predictions` / `prediction-monthly` | 每日 18:00 到期判断 | `scripts/run_close_predictions.py --control-plane <launchd或systemd> --refresh-start 18:00 --refresh-deadline 18:55` |
| Actuals | `actuals` / `actuals` | 每日 08:30、19:00、23:45 | `scheduler.actuals_runner` |

Mac3 label 前缀为 `com.bond-factor-lab.`，ECS 为 `bond-factor-lab-` 的 service/timer。
所有 one-shot 使用 `bond_factor_lab_service`、绝对 cwd 和独立外置日志，不保存凭证。
Linux timer 的 `Persistent=false` 表示停机或禁用期间不补跑；错过的点按调度治理处理。
Backend 是独立常驻服务，只监听 loopback，不拥有自然预测写入权。

ECS 自然 DataBridge、daily、weekly、monthly service 的 `EnvironmentFile` 只允许依次读取
本机私有服务配置和 current 的 `.bfl-release.env`，不得加载共享 `manual-run.env`。
正式模板和 installed 环境不得声明 `BOND_DAILY_COORDINATOR_MODE`。
DataBridge 的 producer 标记及其权限边界见[调度治理](../docs/architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md#唯一控制面与现场证明)。

## 不可变 release 与环境

### 构建、预安装和晋级

1. [`build_source_release.py`](../scripts/build_source_release.py) 只从 clean Git worktree 的当前 HEAD
   生成 deterministic archive、SHA-256 和 manifest v2。manifest 仅包含 `schema_version`、`commit`、
   `root_prefix`、`archive.filename/sha256`，生产包不含 `.git`。
2. 目标机使用**候选 archive 同版本**的 [`install_source_release.py`](../scripts/install_source_release.py)，
   不能用旧 current 的安装器生成候选环境。显式提供 `--manifest`、`--archive`、
   `--expected-archive-sha256`、`--deploy-root`、`--runtime-root`；摘要须是另行核准值。
   安装器检查实际摘要同时匹配核准值与 manifest、archive pax commit 匹配 manifest，及 archive、
   解包目录和已安装目录的 source digest 一致。默认校验、隔离解包和预安装，不切换 current。
3. 从候选 release 完成各机自己的输入、schema、环境、方案及数据验收；ECS 通过后，Mac3 只能晋级
   同一份已验证 archive，不能从环境分支重建。两机 current 可在晋级期间暂时不同。
4. 切换前保存 current/previous、archive、安装记录、控制面和数据库/运行期基线，核对在途 Writer、
   下一触发窗口和恢复兼容性。在已获授权范围内以同版本安装器重新校验已预安装目录，再传
   `--activate --expected-current <精确旧提交>`。安装器不操作数据库、Registry、服务或入口网络。
5. 激活拒绝同 SHA 重试，避免覆盖 previous；revision intent 和 previous 先写，current 原子替换最后执行。
   返回后以 current 读回判定切换结果。需要服务刷新时单独执行已授权动作，核对进程实际 cwd、
   release 身份和健康，再完成数据、Dashboard/页面及调度验收；不能只看链接或刷新成功。

任何导入 immutable release 项目模块的运维或审查命令必须显式 `python -B`；隔离模式为
`python -I -B`，因为 `-I` 忽略 `PYTHON*`，环境变量不能替代 `-B`。
仅导入标准库的 `scripts/run_launchd_release.py` 当前可用 `/usr/bin/python3 -I`；若以后引入项目模块，
须同步改为 `-I -B` 并重验。环境合同不完整或 source digest 不符的未激活目录整体隔离，不现场补写。

### 外置状态与启动环境

deploy/runtime root 必须是 operator 拥有的真实目录，不能 group/world writable；工具不自动修正权限。
`runtime_root` 必须是非根、仅含字母、数字及 `/._-` 的绝对 ASCII-safe 路径，非法形式在文件修改前拒绝。

安装器生成 release 内 `.bfl-release.env`，唯一维护：

- `BFL_RELEASE_COMMIT`、`BFL_RUNTIME_ROOT`；
- `NUMBA_CACHE_DIR=<runtime-root>/cache/native/<commit>/numba`；
- `MPLCONFIGDIR=<runtime-root>/cache/native/<commit>/matplotlib`。

不得在私有配置或 plist 重复覆盖这些变量。同一主机、runtime root、commit 的第三方 cache 可复用，
不同 commit 路径隔离；路径存在不证明内容可跨版本复用。DataBridge、输入 artifact 和 Blackbox
派生状态须按[输入合同](../docs/blackbox_v2/data_bridge_v1/README.md)及[状态操作](../docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#51-增量方案的显式预热重建)核验，不能以第三方 cache 代替。
Blackbox frozen manifest 的 selector 对 Linux x86_64 使用 linux-64、Mac arm64 使用 osx-arm64，其他平台拒绝。

Mac3 launcher 从已经解析的精确 release 读取 `.bfl-release.env`，再唯一派生 runtime 下
`config/service.env`，最后 exec 服务环境入口。它拒绝 Git 工作区、非 `releases/<commit>` 目录、
commit/runtime/cache 漂移、外层同名覆盖及外置 `PYTHON*` 变量；配置目录须为用户拥有的真实 `0700`
目录，文件为同用户真实 `0400/0600` 普通文件。通过目录 fd、`O_NOFOLLOW`、同一 fd 属性校验和限长读取
防止替换竞态；只解析唯一 `KEY=VALUE`，不执行 shell 展开。生产目标缺少所需 release 环境时 fail-closed。

安装器创建 runtime 下 logs，模板日志不写开发目录。SSH tunnel 不执行项目代码，以用户主目录为 cwd；
其 key/user、cwd 和日志由本页漂移审计核对，不参与应用代码切换。

### 手工 Harness 的目标环境绑定

SOP 中的 `python -B -m harness ...` 只展示操作参数，**不会自动加载目标机服务配置**。进入候选目录或激活 conda 也不等于已经绑定目标环境。执行手工回测、激活、补缺或项目数据库查询前，按操作所需能力准备并核验本次子进程；只读 identity 查询无需启动算法或准备算法输入。该环境核验也适用于手工 migration 和认证 CLI：

| 项目 | 来源与核验 |
|---|---|
| 源码与解释器 | cwd 为本次选定并核验的 `releases/<commit>` 实际目录（候选或 current 的真实指向），使用访问入口列出的本机服务 Python，显式 `-B`；不能从开发工作区导入项目 |
| release/runtime/cache | 使用该候选 `.bfl-release.env` 中的四个键，逐项核对提交、runtime root 和 cache 路径；不得混入旧 current 或另一机值 |
| 部署目标 | 显式使用访问入口中的本机 `BFL_DEPLOYMENT_TARGET`，否则开发/Harness discovery 可能发现全量方案 |
| 数据库 | `BFL_DATABASE_ENV_FILE` 显式指向访问入口列出的本机私有服务配置；清除操作者环境中既有 `BOND_DB_*` 覆盖值，再通过本机只读 identity 核对实际数据库；只记录核验结论，不输出密码/DSN |
| 算法环境与输入 | 涉及算法执行或生命周期证据校验时，按选定 release 的 Runtime Profile/frozen manifest 核验解释器、依赖、输入 ready 身份；增量方案还核对[SOP 的 locale/环境身份](../docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#51-增量方案的显式预热重建) |

数据库读取行为以 [`shared.db_config`](../shared/db_config.py)为准：`BFL_DATABASE_ENV_FILE` 只加载数据库键，不能代替完整 release 或算法环境；ambient `BOND_DB_*` 优先于文件值，未提供配置时存在默认连接值。上述核验未通过时，不执行持久化命令。

自然调度的 EnvironmentFile 和 Mac3 launcher 按上文加载环境，但不因此自动覆盖手工 Harness；手工调用也不能靠设置自然控制面标记冒充真实触发。不使用 shell `source` 执行私有配置，不打印整个环境。当前没有供本 SOP 直接调用的通用跨平台手工启动入口；具体调用的环境映射须在操作前可审查，缺少这一环时先停止并补齐，能力缺口见[TODO](../docs/TODO.md#候选-release-手工启动入口)。

## 数据库迁移

[`migrations.runner`](../migrations/runner.py) 是唯一迁移实现，只接收 caller-supplied Engine，负责
manifest、inspect、apply 和 APPLYING recovery；不承担环境变量、CLI 或授权解析。
[`scripts/apply_migrations.py`](../scripts/apply_migrations.py) 是生产/候选 schema 的唯一运维包装器，
不得直跑 migration SQL 或在其他层复制 runner 行为。

- `--inspect-applying-NNN` 只读分类中断状态，可使用 named advisory lock；不执行 DDL/DML，
  不需要写入身份参数，也不接受 `--apply` 或 `--state-digest`。
- `--apply` 必须同时显式提供 `--expected-database-name` 和 `--expected-server-uuid`。
- `--recover-applying-NNN` 支持 **017、018、019、021、022、023、024、025**；每次须同时提供
  `--apply`、上述两个身份参数，以及先前同机只读 inspect 返回的 `--state-digest`（64 位小写 hex）。
- CLI 在创建 Engine 前校验参数，并在首个写动作前精确比较 `DATABASE()` 与 `@@server_uuid`。
  UUID 只能取自只读 inspect/identity 查询；文档、提交和示例不保存生产 UUID、DSN 或凭据。

执行前取得目标环境的明确授权，核对 manifest、当前 schema、在途任务、可恢复备份和 current/previous
兼容性；隔离 MySQL 测试通过不等于生产已迁移。失败或状态漂移时保留原始证据，先 inspect 再选择对应
recovery，不手工改迁移历史。

Migration [021](../migrations/021_registry_owner.sql) 的历史 owner authority 只服务一次性回填：DDL 前须证明
Registry 与该 authority 双向闭集，已有非空 owner 冲突即拒绝；只接受缺列、精确 nullable partial 或完整终态，
其他状态不恢复。完成后 owner 为 VARCHAR(64) NOT NULL，运行时不保留第二份映射，新写入须提供/保留合法 owner。

Migration [025](../migrations/025_drop_platform_confidence.sql) 删除两张预测表的统一 confidence 列，不改其他属性或
JSON 审计。两表闭世界校验覆盖列类型、空值/默认值、主键、索引、外键和 check；除 confidence 两列分别存在/缺失外，
任何 schema 漂移都拒绝，校验与恢复实现见 [runner](../migrations/runner.py)。删列前须有独立授权及兼容 current/previous 的可恢复备份；
删后不得回滚到仍依赖该列的 release。已执行批次的备份位置、原始 JSON 精度保护与实际恢复限制只在
[当前状态](../docs/CURRENT_STATUS.md)维护，恢复前必须读取；备份与 immutable 原件不随文档清理删除。

## 回滚与认证部署

回滚同时评估 source、installed 控制面、数据库和外置 runtime；先隔离受影响 Writer，核对其已写事实、
exact、输入和可执行范围，再恢复兼容版本和调度。保留 previous、安装记录与回滚审计。
切 current/previous 不撤销业务写入，不能用整库恢复覆盖持续增长的事实；先在隔离库验证精确恢复集。

Backend 私有服务配置必须提供 `BFL_AUTH_TRUSTED_ORIGIN`，取值须精确匹配
[访问入口](../docs/operations/DEPLOYMENT_ACCESS.md#ecs-ssh-与临时本地转发)中的本部署目标 Origin，
不得写入 `.bfl-release.env` 或 one-shot 配置。管理员初始化/离线重置只走
[认证合同](../docs/architecture/AUTHENTICATION_AND_ACCOUNT_MANAGEMENT.md#初始管理员与离线恢复)。

认证恢复须保持[认证访问边界](../docs/architecture/AUTHENTICATION_AND_ACCOUNT_MANAGEMENT.md#api-与访问边界)。Mac3 的
[`bond-factor-lab-lockdown.conf`](nginx/bond-factor-lab-lockdown.conf) 是完整 replacement site，
与正常 site 互斥；对应用入口及子路径统一 503，未知路径 403。预安装与 `nginx -t` 不授权切换。
Nginx、tunnel、Clash 的独立故障处置与验收见[Dashboard 运行验收](../docs/operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#故障定位与恢复)。

## 只读配置漂移审计

运行方式及目标机路径见[访问入口的只读读回](../docs/operations/DEPLOYMENT_ACCESS.md#每次操作前的只读读回)。
[`audit_launchd_config_drift.py`](../scripts/audit_launchd_config_drift.py) 只比较仓库模板、installed plist 和
`launchctl print`，无启停或修改功能。报告仅含变量名、存在性、语义差异、启动/触发 hash，不输出凭据值。

除已核验的 SSH key/user 本机差异外，其余 argv、cwd、触发器、环境和日志漂移均失败；
环境审计同时检查本页的禁用变量及调度治理定义的 producer 标记。
私有 service.env 顶层校验一次；Backend/tunnel 须 running，七个任务日志均与外置 runtime 模板一致。
退出 0 仅证明配置审计通过，不证明自然运行成功或授予生产操作权。
