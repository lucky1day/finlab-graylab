# Actuals Launchd 单一调度权威设计

**文档状态**：`CURRENT`

**核验日期**：2026-08-01

## 背景与目标

生产机器已经安装独立的
`com.bond-factor-lab.actuals` LaunchAgent，在每日 `08:30`、`19:00`、`23:45`
调用现有一次性入口：

```text
launchd calendar trigger
  └─ python -m scheduler.main --run-once actuals
       └─ run_actuals_job(run_date)
            ├─ update_actuals
            ├─ update_weekly_actuals
            └─ update_monthly_actuals
```

但常驻 `scheduler.main` 的 `build_scheduler()` 同时在 APScheduler 中注册了相同三个
Actuals cron job。2026-07-31 `19:00/23:45` 和 2026-08-01 `08:30` 的本机日志均显示
两个入口分别完成了相同记录数的刷新。这不是两个不同职责，而是同一个幂等写入过程被
两套时钟重复触发。

本批目标是把独立 launchd/plist 定为 Actuals 的唯一生产调度权威，退役常驻
APScheduler 的 Actuals 时钟职责，同时保留成熟的一次性执行实现、CLI 和三个
updater。该修改不处理日度预测双入口、ledger coordinator 或 v2-preflight。

## 调用图结论

- `deploy/launchd/com.bond-factor-lab.actuals.plist` 已完整声明三个生产时点，并只调用
  `scheduler.main --run-once actuals`。
- `scheduler.main.run_actuals_job()` 是 launchd 和 APScheduler 当前共用的实际执行函数；
  它不是冗余代码，必须保留。
- `scheduler.main.ACTUALS_REFRESH_TIMES`、`_format_actuals_refresh_times()`、
  `build_scheduler()` 的 `actuals` executor 和三个 `scheduler.add_job()` 只服务于
  APScheduler 的第二套时钟，删除后没有其他调用方。
- `scheduler.daily_actuals_updater`、`weekly_actuals_updater`、
  `monthly_actuals_updater` 仍分别承担三类实际方向的读取、计算和写库职责，不属于本批
  清理对象。
- 当前架构文档仍把 Actuals 描述成 APScheduler job，且 `deploy/README.md` 没有把已存在
  的独立 Actuals LaunchAgent 写成现行生产入口；代码清理必须同步修正文档。

## 方案比较

### 方案 A：保留一次性执行，删除 APScheduler 时钟（采用）

保留 plist、CLI、`run_actuals_job()` 和三个 updater，只从 `build_scheduler()` 删除
Actuals executor 与 cron 注册。本方案删除的是重复调度权，不移动业务实现，也不改变
刷新时点、日期语义或写库代码。

### 方案 B：保留 APScheduler，删除 Actuals plist

改动代码最少，但与“launchd/plist 一次性任务是最终生产调度权威”的既定方向相反，
继续把 Actuals 生命周期绑定到常驻 scheduler 进程，不采用。

### 方案 C：同时拆出新的 Actuals CLI 模块

可让 plist 不再经过 `scheduler.main`，但会新增模块和迁移入口，扩大本批风险。当前
`--run-once actuals` 已稳定运行，没有必要在退役重复时钟时重构业务入口。

## 精确改动范围

### 删除 APScheduler Actuals 职责

修改 `scheduler/main.py`，精确删除：

- `ACTUALS_REFRESH_TIMES`；
- `_format_actuals_refresh_times()`；
- `BlockingScheduler` executor 映射中的 `actuals` executor；
- `build_scheduler()` 中注册 `actuals:0830`、`actuals:1900`、`actuals:2345` 的循环；
- `Scheduled actuals refresh at ...` 注册日志。

明确保留：

- `run_actuals_job()`；
- `--run-once actuals` 参数与分派；
- `update_actuals()`、`update_weekly_actuals()`、`update_monthly_actuals()` import 和调用；
- 非交易日 daily/weekly 回退上一交易日、monthly 保持自然运行日的现行语义；
- launchd plist 中的三个时点、`RunAtLoad=false` 和一次性进程模型。

不得新增兼容 executor、隐藏 cron、环境变量开关或第二个 Actuals wrapper。

### 测试职责收敛

修改 `tests/test_scheduler_main.py`：

- 删除 ledger/legacy scheduler 测试中对三个 `actuals:*` job 和 `actuals` executor 的
  正向断言；
- 将“Actuals 三时点由 APScheduler 注册”的测试改为“无论 legacy/ledger mode，
  `build_scheduler()` 都不得包含 `actuals:*` job”；
- 保留 `run_actuals_job()` 在交易日、非交易日的业务测试；
- 新增 `--run-once actuals` 调用一次执行函数并返回成功的 CLI 分派测试，补齐当前只有
  plist 命令合同、没有直接覆盖 `scheduler.main` Actuals 分支的缺口。

保留并强化 `tests/test_actuals_launchd.py` 作为调度权威合同：

- exact label、命令、工作目录和环境；
- exact `08:30/19:00/23:45`；
- `RunAtLoad=false` 且没有 `KeepAlive`；
- plist 只调用一次性 Actuals 入口。

测试必须先新增“APScheduler 无 Actuals job”断言并在旧代码上观察预期失败，再删除
生产注册代码。

### 修正现行文档

只修改当前架构与部署入口：

- `docs/architecture/ARCHITECTURE.md`：系统图、Actuals 数据流和进程管理改为独立
  launchd 一次性任务；
- `docs/architecture/CODE_ARCHITECTURE.md`：Actuals 触发者从 APScheduler 改为
  `com.bond-factor-lab.actuals`；
- `deploy/README.md`：记录 Actuals plist 的安装形态、三个时点和一次性入口。

`docs/records/` 中旧时期由 scheduler 注册 `actuals:*` 的运行事实必须保留，不因当前
职责退役而改写。历史 specs、plans 和 audits 同样不做追溯性重写。

## 错误与运行语义

- launchd 每次触发一个独立进程；成功时 `scheduler.main` 返回 0。
- 任一 updater 抛出未处理异常时进程保持非零退出，launchd 日志继续暴露失败，不把
  部分失败静默改写为成功。
- 本批不增加重试、锁、告警或状态文件；这些属于独立可靠性设计，不与去重清理捆绑。
- 删除 APScheduler job 后，常驻 scheduler 的启动、重启或 startup catch-up 不再触发
  Actuals。手工执行 `--run-once actuals --date ...` 仍然可用。

## 行为不变量

本批完成后必须保持：

1. Actuals 三个生产时点仍为 `08:30/19:00/23:45`，时区语义不变。
2. daily、weekly、monthly Actuals 的读取、计算、UPSERT、源水位和非交易日规则不变。
3. 日度、周度、月度预测 job 的注册和执行行为不变。
4. 07:00 daily-gray、ledger coordinator、direct authority、v2-preflight 和 DataBridge
   行为不变。
5. launchd plist 内容不因本批代码清理而扩展新的权限、凭据或环境变量。
6. 数据库 schema、registry、方案配置、算法、API 和前端不变。

## 验证策略

实现时按以下顺序执行：

1. 新增 APScheduler 不得注册 Actuals 的失败测试，确认旧代码精确失败。
2. 删除 `scheduler.main` 中仅服务于 Actuals cron 注册的常量、formatter、executor 和
   job loop。
3. 运行 `tests.test_actuals_launchd` 与 `tests.test_scheduler_main`，确认 plist 权威合同、
   一次性 CLI 和 Actuals 业务函数均通过。
4. 运行 daily runtime、direct authority、scheduler 和三个 updater 的定向回归。
5. 更新当前架构/部署文档并运行文档索引、链接与入口门禁。
6. 运行完整测试、`compileall`、`git diff --check` 和源码引用扫描。
7. 对照本设计确认 `deploy/launchd/com.bond-factor-lab.actuals.plist`、三个 updater、
   daily/ledger runtime、migrations、schemes 和 shared 均无越界修改。

## 实施与发布边界

- 代码实现只提交到 `codex/audit-bugfixes-20260613`。
- 本批实现不操作 `launchctl`，不复制 installed plist，不重启常驻 scheduler，也不访问或
  写入生产数据库。
- 代码提交本身不会立即停止当前已加载进程中的 APScheduler job；生产生效必须留到整个
  清理阶段验收后，在用户明确授权的统一发布窗口完成。
- 不在本批后单独同步 `master`，也不推送任何远程分支。
- 日度预测单一 launchd 权威、`gray_live`/`scheduled_live` 正规化和 ledger 退役另立后续
  设计，不夹入本批。
