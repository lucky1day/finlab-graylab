# 阿里云 ECS 独立灰度运行与迁移交接

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-21

本文是 ECS 灰度运行、双主机 release 治理和未来生产切换边界的当前入口。迁移实施过程、旧候选、
一次性验收脚本和逐次运行证据不保留在工作树；需要追溯时使用 Git、systemd/launchd journal、数据库
审计和目标机 root-only evidence。

## 1. 当前结论

- ECS 独立灰度部署已经完成：独立 MySQL、增量数据链、DataBridge、三套 Python 环境、Backend、
  daily/weekly/monthly/Actuals 和五个 systemd timer 均已落地。
- Mac3 immutable release 解耦已经完成：生产应用从不可变 `current` 启动，运行配置、日志、DataBridge
  状态和第三方 cache 位于 release 外，生产进程不再依赖 Git 开发工作区。
- Mac3 与 ECS 共用唯一 `codex/develop` source release 代码线，不维护环境专用长期分支。发布按 ECS
  先验证、Mac3 后晋级分阶段执行，因此灰度验证期间两端 `current` 可以不同。
- Mac3 继续承载生产域名、生产数据库和 Writer；ECS 只监听 loopback，独立运行和观察，不接生产流量。

因此本轮“ECS 灰度部署 + 双主机单一 source release 代码线治理”已经闭环。未来把域名或 Writer 切到 ECS
是新的生产项目，不属于本轮完成条件，也未由本轮授权。

## 2. 双主机运行边界

```mermaid
flowchart LR
    U["生产用户"] --> M["Mac3 生产域名 / 前端"]
    MW["Mac3 launchd / Writer"] --> MDB["Mac3 bond_db"]
    M --> MDB

    O["灰度观察者"] -->|"受控 SSH 隧道"| E["ECS API / 前端\n127.0.0.1:8100"]
    BP["ECS BondPrediction"] --> EDB["ECS 本地 MySQL"]
    ST["ECS systemd timers"] --> W["DataBridge / 方案 / Actuals"]
    W --> EDB
    E --> EDB

    MDB -.->|"不复制、不双写"| EDB
```

- 两端各自维护数据库、DataBridge、Registry、run、prediction、调度状态和回滚指针。
- ECS 不是 Mac3 热备或共享数据库节点；本阶段不安装生产 Nginx/TLS，不修改 DNS 或安全组公网入口。
- 仓库模板只表示期望配置。Mac3 的现场 authority 是 installed plist 与 `launchctl`；ECS 的现场
  authority 是 installed unit/timer 与 `systemctl`。
- 灰度健康不会自动授权切流；Web、Writer、数据库 authority、域名、窗口和回滚范围必须分别评审。

## 3. 当前部署基线

两端属于同一 source release 代码线，当前已晋级到同一 R4 source：

| 主机 | 当前 release | archive SHA-256 | 回滚基线 |
|---|---|---|---|
| ECS | `92a93713656d8534e68312f123676b5d2054d8a6`（`bfl-source-r4-frontend-colors-20260821`） | `e2f1c59c5e183940903de0d3eac39c9cd27e24a3b518ce28446a5d87db96ee37` | `previous` 指向 R3 `08645a87852bbc307d3f6def22d7d457b1014bcb` |
| Mac3 | `92a93713656d8534e68312f123676b5d2054d8a6`（`bfl-source-r4-frontend-colors-20260821`） | `e2f1c59c5e183940903de0d3eac39c9cd27e24a3b518ce28446a5d87db96ee37` | `previous` 指向 R2 `e692285d47e41c384dc915758abe0c51f9ac3aaf`；R2 可执行回滚包外置保留 |

R4 source digest 为 `406b407c624f26137b0d7a84317dcb7cdee8a6d4fa20eccbf9fc7dec9c0bb2b4`。

ECS R3 基线包含 live prediction insert-only 三态；其不可变预安装、CAS 激活、Backend 重启、健康
与 timer/unit 读回均已通过；五个 timer 为 `enabled/active/waiting`，五个 writer 均 idle，11 个 installed
unit hash 未改变。Backend 切换窗口观测约 1.0036 秒，激活窗口内 ECS 灰度主库 `bond_db`
无业务写入，binlog 读回为 `binlog.000033:158`。验证前后 ECS 灰度主库 `bond_db` 的
`t_scheme_predictions`、`t_scheme_versions`、`t_scheme_registry`、`t_scheme_runs`、
`t_scheme_run_log` 五表 count 与全行摘要、五表 schema 摘要以及 17 条 migration history 均一致；
Mac3 生产域名、数据库 authority 与 Writer 均保持在 Mac3。

R4 在 R3 之上只调整前端颜色映射、相关测试和静态资源缓存标识。R3 可执行回滚包已在切换前复验；
R4 不可变预安装、CAS 激活和 Backend 重启通过，重启窗口约 1.2 秒，切换前后 binlog 位点精确保持
`binlog.000033:7444533`。真实浏览器已验收任务格、排行、逐月和每日明细的红/绿/灰规则，并确认趋势图
系列色与普通文字未改变。Mac3 随后使用同一 archive 完成候选复验、CAS 激活和仅 Backend 重启；
切换窗口约 1.8 秒，`current/previous` 为 R4/R2，五张关键业务表 count、migration 与 Registry 快照未变。
窗口内 binlog 增量全部属于既有 `qrtz_scheduler_state` 心跳，不是 Bond Factor Lab 业务写入；真实浏览器
复验同样通过，installed plist、生产域名、数据库 authority 与 Writer 主机均未改变。

| 范围 | ECS | Mac3 |
|---|---|---|
| 角色 | 独立灰度实验室 | 当前生产 Web / DB / Writer |
| 控制面 | systemd one-shot + timer | launchd one-shot + plist |
| Backend | `127.0.0.1:8100` | 生产域名现有入口 |
| release | `/opt/bond-factor-lab/current` | `/Users/macstudio0/bond-factor-lab-production/current` |
| 外置状态 | `/var/lib/bond-factor-lab/state` | `/Users/macstudio0/bond-factor-lab-runtime` |
| 本机配置 | root-only database env | `runtime/config/service.env` |
| Git 工作区 | 无 | `/Users/macstudio0/bond-factor-lab`，仅开发使用 |

ECS 使用 conda-forge-only 的 service、forecast 和 Blackbox 环境；BondPrediction requirements 已包含
`cryptography`。ECS discovery 为 56 active base / 60 active composite，9 个 Mac-only base 不进入
ECS runner。两端 release 的环境差异只由部署模板、目标矩阵和平台依赖 manifest 表达，不复制
scheduler、repository、算法或 scheme config。

## 4. 调度与运行上限

| 职责 | ECS 触发 | 总时限 |
|---|---|---:|
| DataBridge | 每日 06:30 | 1 小时 |
| daily | 工作日 07:03 | 2 小时 |
| weekly | 周六 11:30 | 2 小时 |
| monthly | 自然月 15 日 18:00 | 2 小时 |
| Actuals | 每日 08:30、19:00、23:45 | 1 小时 |

五个 timer 均为 `Persistent=false`，停机或禁用期间不补跑；自然 timer 继续承担日常运行与监控。实际
服务、timer 和 writer 状态以 installed unit/timer 与 `systemctl` 读回为准。

ECS 自然 DataBridge、daily、weekly、monthly unit 不得共享手工环境文件；历史日期的手工补缺只允许
执行 `python -m harness signal-gap-fill --predict-date YYYY-MM-DD`。installed unit 变更必须独立授权；
现场事实见[当前状态](../CURRENT_STATUS.md)，后续步骤见[统一后续推进计划](../TODO.md)。

## 5. 数据修订与缓存规则

- 已写入 `t_scheme_predictions` 的历史预测表达当时可用数据下的决策，事后数据修订不得回写。
- Phase-A cache 是未来预测的内部加速状态，可以在安全边界内更新。
- 普通同结构数值修订只有在 spec 携带 canonical dependency proof 时才能重算有限 suffix；
  `safe_lookback=max(horizon, purge_gap)`，当前十个 Liwei adapter 的边界为 5 个交易日。
- suffix 之前的 cache 必须保持 parent digest 不变；受影响 suffix 必须通过 cold equivalence 和 lineage。
- 算法、spec、ABI、publisher、schema、字段类型、历史日期删除、交易日或 date-to-week 结构变化仍然
  full/fail-closed，不得伪装成增量成功。

实现继续复用 `build_mode=suffix`、manifest、CompareGate、acceptance lineage 和 `current.json` 原子
发布；不增加 revision ledger、数据库表、后台服务、第二套 cache 或算法分支。

## 6. Immutable release 与本机配置合同

`scripts/build_source_release.py` 只从 clean HEAD 生成 deterministic archive；目标机使用候选同版本的
`scripts/install_source_release.py` 校验 archive、manifest 和 source digest，并通过
`expected-current` compare-and-swap 更新 `current/previous`。release 目录无 `.git` 且只读，目标机
不得 `git pull` 或现场修改。

任何可能 import 候选/current immutable release 中项目模块的运维或审查命令，解释器都必须显式
携带 `-B`；若同时使用隔离模式，必须写成 `python -I -B`。`PYTHONDONTWRITEBYTECODE=1` 只能作为
非隔离模式下的附加防护，不能替代 `-B`，因为 `-I` 会忽略 `PYTHON*` 环境变量。可以从已激活的
`current` 执行健康检查，也可以直接运行 release 内脚本；禁止的是在未禁用 bytecode 写入时 import
项目模块。当前 release launcher 只使用标准库、不 import 项目模块，因此不受此条限制；未来若引入
项目模块，必须同步改为 `python -I -B`、补充相应回归并重新完成 immutable release 核验。

Mac3 的 `runtime/config/service.env` 是唯一生产应用本机配置 authority；Git 根 `.env` 只服务开发。
两者仅首次迁移时复制一次，此后不自动同步。launcher 使用目录 fd、`O_NOFOLLOW`、owner/mode、
语法、保留键和必要变量检查后才加载配置；任何 `PYTHON*`、release 身份或控制面覆盖均 fail-closed。
真实 secret 不写入 Git、release manifest、plist、日志或审计 JSON。

以后每次 release 更新仍按以下最小顺序执行：

1. clean HEAD 构建并固定 commit 和 archive SHA-256；
2. 目标机使用候选同版本 installer 做只读预检和只读预安装，校验独立批准的
   archive SHA-256、archive pax commit 标记与 manifest commit，以及 archive/解包/已安装
   source digest 一致；
3. 候选 Backend、DataBridge check-only 和 release identity 验证；
4. 在独立授权窗口执行 `current` CAS；仅当 installed 配置确有差异且已取得独立授权时才替换并重载，
   本次及默认路径只按需重启 Backend；
5. 读回健康、source identity、loaded state、调度空闲状态和 tunnel；
6. 保留可用 `previous`、installed 配置备份和运行审计。

具体工具、环境合同和 drift audit 见 [`deploy/README.md`](../../deploy/README.md)。

## 7. ECS 只读接手检查

当前灰度 ECS 的受控网络 authority 是 `47.103.45.193`。`bond.finailab.cn` 当前不是这台灰度 ECS，
不得用该 DNS 名称替代 ECS authority 或据此执行接手命令。公网 IP 本身不是凭据，可以在本运维文档
中固定；SSH 仍必须使用批准的本机密钥、已钉住的 Host Key 和严格校验，禁止临时接受未知 Host Key：

```bash
ECS_GRAY_HOST=47.103.45.193
ssh -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -i "<approved-private-key-path>" \
  root@"${ECS_GRAY_HOST}"
```

进入该 authority 后执行只读检查：

```bash
readlink -f /opt/bond-factor-lab/current
readlink -f /opt/bond-factor-lab/previous

systemctl list-timers 'bond-factor-lab-*' --all
systemctl show bond-factor-lab-prediction-daily.service \
  -p ActiveState -p SubState -p Result -p ExecMainStatus \
  -p ExecMainStartTimestamp -p ExecMainExitTimestamp

curl --fail --silent http://127.0.0.1:8100/api/health

cd /opt/bond-factor-lab/current
env BFL_DATABASE_ENV_FILE=/etc/bond-factor-lab/bond-factor-lab.env \
  PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  /opt/miniconda3/envs/bond_factor_lab_service/bin/python -B \
  scripts/check_production_daily_health.py --predict-date YYYY-MM-DD --strict-runs
```

出现任务超时/failed、残留 running、预测缺失或重复、DataBridge 日期/摘要不一致、cache lineage 拒绝、
source digest 漂移、Backend 不健康、timer 漂移或磁盘不足时停止外推结论。数据库 schema、Registry、
业务数据、unit/timer、Nginx 或 DNS 的修改必须另行设计和授权。

## 8. 回滚与长期保留边界

- 回滚同时考虑 source release、installed 控制面和外置状态；只切代码 symlink 不是完整回滚。
- ECS `previous` 的 R3 release 保留 insert-only rollback 基线和可用回滚材料；R4 只含前端颜色变更，
  但 source-only 回滚仍不能撤销已经写入的 R3/R4 数据库/runtime 状态，禁止只切回旧 release 后恢复预测调度，
  必须先独立评审数据库状态、单 Writer 边界和可执行回滚范围。
- Mac3 `previous` 的 R2 仍含旧 prediction UPSERT 语义；在任何 R4 prediction one-shot 运行后，不得只把
  `current` 切回 R2 并恢复 Writer。回滚必须先停止相关 Writer，独立核对数据库状态与重复键边界，再决定
  是否允许 source 回退；R2 外置可执行包只提供代码回滚能力，不自动回滚业务数据。
- ECS installed 的 DataBridge、daily、weekly、monthly 四个 natural service 仍保留旧
  `/run/bond-factor-lab/manual-run.env` 引用；该文件当前不存在，因而没有生效的旧日期覆盖。替换这些
  installed unit、执行 `daemon-reload` 并重新读回是独立后续操作，不由本次 release 治理授权。
- 对已经发布 proof-bearing suffix generation 的 family，回滚旧代码时必须恢复发布前保存的 7 个
  `current.json` parent pointer。
- 回滚不得删除 run、prediction、日志或失败 evidence，不得修改历史预测伪造成功。
- `master` 保持冻结备份，未经授权不得移动；日常修改只进入 `codex/develop`。
- 仓库只保留通用 release/launchd/systemd/cache 回归测试。一次性迁移脚本、候选专项测试、时点报告
  和已完成实施计划不得重新进入工作树；需要追溯时使用 Git 和现场审计证据。
