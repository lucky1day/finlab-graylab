# 阿里云 ECS 候选部署与手工真写库设计

**日期：** 2026-08-17  
**目标主机：** 阿里云 ECS `47.103.45.193`  
**目标数据库：** ECS 本机 MySQL `127.0.0.1:3306/bond_db`  
**发布性质：** 候选环境部署与手工一次性真写库验收；不切流量、不启用定时器

## 1. 决策摘要

本轮先把 Bond Factor Lab 的 C56 候选版本部署到阿里云 ECS，并通过 systemd 一次性服务手工执行 DataBridge、日/周/月预测和实际值更新，验证这些生产调度入口能够在 Linux 上执行并真实写入 ECS 候选数据库。

所有 timer 只安装、不启用，并在验收结束后保持 `disabled` 且 `inactive`。本轮不验证 macOS 与 Linux 的算法数值偏差，不运行 CompareGate，不切换 Nginx、DNS、安全组或公网流量，也不修改 Mac Studio 的 launchd、crontab、数据库或运行状态。

本轮接受的成功定义不是“命令退出码为 0”，而是每条调度链路都留下可读回的运行记录、日志和业务表证据，同时没有失败运行、残留子进程或 OOM。

## 2. 范围与不变量

### 2.1 本轮包含

1. 在 ECS 安装一个不可变的精确 release，并通过 `/opt/bond-factor-lab/current` 原子软链接指向它。
2. 复用已在 ECS 建成并验证的三个 conda-forge-only 环境：
   - `/opt/miniconda3/envs/bond_factor_lab_service`
   - `/opt/miniconda3/envs/forecast_env`
   - `/opt/miniconda3/envs/forecast_env_blackbox_v1`
3. 创建 root-only 的 `/etc/bond-factor-lab/bond-factor-lab.env`，应用只连接 ECS 本机候选库。
4. 将 C56 精确范围同步到 ECS 候选 Registry：56 个 active base scheme、60 个 active composite、9 个 paused composite。
5. 增加并安装 Linux systemd 一次性服务和 timer 模板。
6. 手工启动一次性服务，真实执行 DataBridge、日/周/月预测和实际值更新。
7. 读回数据库、文件产物、systemd 状态和进程状态，形成部署验收证据。

### 2.2 本轮明确不包含

- 不运行 macOS/Linux CompareGate 或算法数值等价验证。
- 不修改任何算法实现、特征、模型参数、窗口、内部 score 或预测方向。
- 不运行或持久化 backtest。
- 不启用、启动或自然等待任何 systemd timer。
- 不恢复 APScheduler、daily ledger、occurrence、epoch 或第二套 Python 调度控制面。
- 不修改 Mac Studio 上的代码、数据库、launchd、plist、crontab 或运行服务。
- 不修改 ECS 上现有 BondPrediction 的 28 条 cron。
- 不配置 Nginx、DNS、安全组、证书或公网入口，不切换生产流量。
- 不合并或推送 `master`；分支发布仍需单独授权。

## 3. 精确发布物与目录

当前 C56 Linux 候选基线为提交 `537e8ba50a60`，已在 ECS 暂存并验证三套运行环境。因为本轮还需加入 Linux systemd 控制面和 `systemd_one_shot` 身份支持，最终部署对象必须是这些最小代码变更完成、测试通过后产生的新精确提交，不能把旧基线静默描述成最终 release。

目标目录：

```text
/opt/bond-factor-lab/
├── releases/<exact-commit>/       # 不可变源码 release
└── current -> releases/<exact-commit>

/etc/bond-factor-lab/
└── bond-factor-lab.env            # root:root，0600

/var/log/bond-factor-lab/          # systemd 服务日志或补充审计输出
```

Liwei Phase A 缓存继续使用已经在 ECS 暂存且通过 7/7 lineage 校验的物料。首次候选部署允许把运行期 `backtest_artifacts` 和 DataBridge current 产物放在当前 release 的运行目录下；它们不属于源码 release 摘要，不得混入 Git 或被当作源码完整性证据。后续 release 晋级前再独立设计稳定的共享运行目录，不在本轮引入额外路径抽象。

部署顺序必须是：复制到新的 release 目录、校验提交与物料摘要、完成运行前检查，再原子替换 `current` 软链接。不得原地覆盖当前 release。

## 4. 运行环境与秘密

systemd 服务统一使用以下基本条件：

- `WorkingDirectory=/opt/bond-factor-lab/current`
- 服务 Python：`/opt/miniconda3/envs/bond_factor_lab_service/bin/python`
- `PATH=/opt/miniconda3/condabin:/opt/miniconda3/bin:/usr/local/bin:/usr/bin:/bin`
- `EnvironmentFile=/etc/bond-factor-lab/bond-factor-lab.env`
- 数据库主机固定为 `127.0.0.1`，数据库名固定为 `bond_db`
- 日志和验收命令不得回显 DSN、用户名、密码或环境文件内容

环境文件创建后必须读回权限与 owner，但只以脱敏方式验证必需变量存在。数据库身份还需通过应用自己的 SQLAlchemy/cryptography 连接路径只读验证 `DATABASE()`、主机身份和 MySQL 版本，禁止在日志中输出凭据。

## 5. C56 方案与 Registry 范围

源码中保持 65 个方案和 69 个 composite owner 映射完整，只暂停以下 9 个 Native 方案，不删除代码、不删除政策清单、不删除历史版本：

```text
daily_1y_xgb_1y13_0629
daily_5y_lgbm_5y10_0629
daily_10y_lgbm_10y04_0629
monthly_1y_rf_top30_0629
monthly_5y_knn_top20_0629
monthly_10y_rf_top5_0629
weekly_avg_1y_lgbm_0529
weekly_avg_5y_lgbm_0529
weekly_avg_10y_lgbm_0529
```

Registry 更新必须走现有 repository 的同步入口，不使用临时 `mysql` DML。同步后必须从 ECS 候选库读回并精确满足：

- 65 个 base 配置，其中 56 active、9 paused。
- 69 个 composite Registry 行，其中 60 active、9 paused。
- 26 个 Native 政策身份仍完整。
- 69 个 composite owner 映射仍完整。
- 9 个暂停方案在本轮三个预测入口中的执行次数为 0。

历史 active exact-version 行不删除。若 Registry 读回与上述范围不一致，立即停止手工写库，不得靠扩大执行范围继续。

## 6. Linux systemd 控制面

### 6.1 控制面身份

平台现有 `launchd_one_shot` 语义保留。Linux 调度入口增加且只增加 `systemd_one_shot`，executor、repository、DataBridge publisher 和 health/readback 只接受这两个枚举值。Linux 服务必须如实记录 `systemd_one_shot`，不得冒充 launchd。

### 6.2 单元清单

安装以下服务和对应 timer：

```text
bond-factor-lab-backend.service
bond-factor-lab-data-bridge.service
bond-factor-lab-data-bridge.timer
bond-factor-lab-prediction-daily.service
bond-factor-lab-prediction-daily.timer
bond-factor-lab-prediction-weekly.service
bond-factor-lab-prediction-weekly.timer
bond-factor-lab-prediction-monthly.service
bond-factor-lab-prediction-monthly.timer
bond-factor-lab-actuals.service
bond-factor-lab-actuals.timer
```

预测和更新服务继续调用仓库现有的一次性入口；不新增常驻 scheduler。backend 只监听 `127.0.0.1:8100`，用于本机健康检查，不接入公网。

所有 timer 必须满足：

- `Persistent=false`，ECS 停机或 timer 禁用期间不补跑。
- `RandomizedDelaySec=0`，避免验收语义含糊。
- 文件安装后只执行 `systemctl daemon-reload`，禁止 `systemctl enable`。
- 安装前和验收后都显式执行 `systemctl disable --now <全部 timers>`，并读回 `is-enabled=disabled` 与 `is-active=inactive`。

手工验收只启动 `.service`，不启动 `.timer`。

## 7. 手工真写库流程

### 7.1 写入前快照

在任何一次性服务启动前，先只读记录：

- 数据源最新可用日、周、月日期和交易日历范围。
- 每个 cadence 的 active scheme/target 数量。
- 待使用业务日期上已有的 prediction、run、log 和 actual 行数。
- 39 个 active Blackbox 的精确版本、审批和 runtime profile 可执行状态。
- MySQL 可用空间、ECS 内存、swap、当前 OOM/failed unit 状态。
- 所有 Bond Factor Lab timer 均为 disabled/inactive。

日、周、月手工运行日期只能根据上述只读数据水位选择：使用已有完整输入的最近合法日期，不制造未来输入，也不把不完整数据日期强行当作成功样本。最终选定日期必须在执行前记录到验收报告。

### 7.2 执行顺序

严格串行执行，任一步失败即停止后续写入：

1. `bond-factor-lab-data-bridge.service`：发布最新 DataBridge generation/current。
2. `bond-factor-lab-prediction-daily.service`：执行 daily active 范围。
3. `bond-factor-lab-prediction-weekly.service`：执行 weekly active 范围。
4. `bond-factor-lab-prediction-monthly.service`：执行 monthly active 范围。
5. `bond-factor-lab-actuals.service`：执行日/周/月实际值更新入口。

每一步都在进入下一步前完成 systemd 状态、应用日志、数据库结果和残留进程检查。算法任务保持串行，不为追求速度提高并发或修改运行 profile。

### 7.3 “真写库”的判定

预测服务必须为每个 due active scheme 创建本次 `t_scheme_runs` 记录，并在 `t_scheme_run_log` 留下结构化执行结果。返回 target 集合必须与当前 active Registry 完全一致，预测结果必须能够按本次 run ID、exact version、predict/feature/target date 和 target tenor 从 `t_scheme_predictions` 精确读回。

若某业务键在克隆库中已存在，repository 的幂等 upsert 仍属于真实 writer 路径；但验收必须明确记录哪些业务键被更新，并确认其 run linkage 已指向本次运行。不得仅凭 `records_written` 或退出码推断写入完成，也不得为了制造新增行删除历史记录。

实际值更新只写符合现有业务规则且源数据已经具备的 eligible target。若三张 actual 表在选定水位上均无缺口，成功标准是 updater 正常完成、源与目标水位可读回且没有非法改动；不得伪造数据来强求新增行。若存在缺口，则必须读回新增或更新行及对应日期、tenor 和来源。

DataBridge 本身写文件而非数据库，其成功标准是新 generation、manifest、current 指针和 lineage 校验通过，且后续预测 run 引用该 generation。

## 8. 验收矩阵

| 链路 | 必须证据 | 失败条件 |
|---|---|---|
| Release | exact commit、archive/source 摘要、`current` 指向精确目录 | 原地覆盖、摘要不符、提交不明 |
| 环境 | 三个 conda env 的 Python、`pip check`、关键 import | 任一依赖检查失败 |
| DB 连接 | SQLAlchemy + cryptography 新连接成功，目标为 ECS `bond_db` | 连到错误主机/库或泄露凭据 |
| Registry | 56 active base、60 active composite、9 paused composite | 数量或精确 ID 不一致 |
| DataBridge | generation/current/manifest/lineage 通过 | current 不完整或 lineage 失败 |
| Daily | 每个 due active scheme 的成功 run、log、prediction 精确读回 | 缺 run、失败 run、target 不匹配 |
| Weekly | 同上 | 同上 |
| Monthly | 同上 | 同上 |
| Deferred 9 | 三类预测入口执行次数均为 0 | 任一暂停方案被执行或写 prediction |
| Actuals | updater 成功；有缺口则读回行，无缺口则证明幂等无非法变化 | 失败、越界写入或伪造数据 |
| 进程资源 | 无残留算法子进程、无 OOM、无 failed unit | 残留、OOM、unit failed 未解释 |
| Timers | 全部 `disabled` 且 `inactive` | 任一 timer enabled/active |
| Backend | `127.0.0.1:8100` 本机 health 成功 | 公网暴露或 health 失败 |

最终“可继续迁移”的结论必须同时满足矩阵全部条目。单个服务退出成功、单个方案成功或环境安装完成都不足以形成闭环。

## 9. 失败处理与回滚

任一阶段失败时：

1. 立即停止后续一次性服务，不启用 timer。
2. 保存失败 run ID、结构化日志、systemd status、数据库读回和资源状态。
3. 对可重试的幂等入口，先定位原因并验证修复，再使用同一受控日期重试；不删除历史 run 或业务行。
4. 若新 release 本身不可用，把 `/opt/bond-factor-lab/current` 原子切回部署前记录的目标；旧 release 不删除。
5. Registry 仅在确认需要整体撤回 C56 时，使用 repository 同步到部署前快照并精确读回；不得用临时 SQL 猜测恢复。
6. 环境文件和数据库凭据不进入 Git、命令输出或验收报告。

由于本轮没有切公网流量且 timers 始终禁用，回滚不涉及 DNS、Nginx、Mac Studio 或自然调度。手工写入产生的 run/log 和幂等 prediction 记录作为审计证据保留，不做破坏性清理。

## 10. 完成条件

本轮只有在以下条件同时成立时才完成：

- 新精确 release 已安装，`current` 可回滚。
- 三套 conda-forge-only 环境和 cryptography/MySQL 连接均正常。
- ECS 候选 Registry 精确为 60 active composite、9 paused composite。
- DataBridge、daily、weekly、monthly、actuals 五类一次性链路均已手工执行并按本设计读回。
- 所有 due active scheme 均成功；9 个暂停 Native 均未执行。
- 没有失败 run、残留子进程、OOM 或未解释的 failed unit。
- backend 仅在本机监听且健康。
- 所有 Bond Factor Lab timers 保持 disabled/inactive。
- 未触碰 Mac Studio、现有 BondPrediction cron、Nginx/DNS/安全组或公网流量。

满足上述条件后，才能单独讨论下一阶段的 timer 启用与流量迁移；本轮授权不自动延伸到该阶段。
