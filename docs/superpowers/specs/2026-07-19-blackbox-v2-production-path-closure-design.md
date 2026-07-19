# Blackbox V2 生产路径阻塞项关闭设计

日期：2026-07-19  
状态：已确认，待实施计划  
适用运行时：`blackbox_v2`  
对应审计：`docs/blackbox_v2/records/FULL_PIPELINE_STABILITY_AUDIT_20260719.md`

## 1. 背景与结论

Blackbox V2 已通过真实上游交付、统一 DataBridge 输入、七个自动 Gate、单点预测、批量 no-persist 回测和 `shadow + paused` 登记，但全链路审计仍有七个生产阻塞项：

- `BBV2-01`：`backtest --persist` 被静默忽略。
- `BBV2-02`：公共 ActivationGate 使用 Native 版本算法，无法匹配 Blackbox Gate 历史。
- `BBV2-03`：缺少 Blackbox 专用生产批准和 live hard-stop。
- `BBV2-04`：sandbox 可读取任意本机文件并继承父进程环境变量。
- `BBV2-05`：JSON Result 将字符串方向隐式转换为整数。
- `BBV2-06`：scheduler 单次运行失败后进程仍返回退出码 0。
- `BBV2-07`：Shadow/Activation 跨配置文件和数据库多步更新，失败后不能自动对账。

本设计采用“统一生命周期原语 + 运行时分派”：两类方案继续共用 Registry、版本表、业务结果表、API 和前端；Blackbox 只在版本计算、执行驱动、生产批准、回测驱动和安全策略上使用专用实现。

关闭 `BBV2-01` 至 `BBV2-07` 后，本轮最高认证结论为：

```text
PRODUCTION_PATH_READY
```

该结论表示一个真实 Blackbox 方案可以通过正式 ActivationGate，在隔离测试库完成回测落库、live、actual、API 和前端闭环。它不等于所有算法类型已经广泛稳定；`BBV2-08` 仍要求至少三个真实交付包覆盖日、周、月频率。

## 2. 实施边界

### 2.1 本轮包含

- 修复 `BBV2-01` 至 `BBV2-07`。
- 补充 Blackbox 专用单元、集成和故障恢复测试。
- 在独立 worktree 和隔离测试 Schema 中执行正式 ActivationGate、回测落库、live、actual、API 和前端认证。
- 更新生产准备清单、稳定性报告和机器证据。
- 使用现有 `t_scheme_versions.approved_by/approved_at` 保存生产批准，不新增第二套 Registry。

### 2.2 本轮不包含

- 不激活生产仓库中的 `weekly_10y_lgbm_point_v1`。
- 不向生产业务表写入 Blackbox prediction、run、actual 或 backtest。
- 不迁移或重命名现有 29 个 Native V1 方案。
- 不把当前试验方案表述为正式生产上线。
- 不关闭 `BBV2-08`，不使用机器夹具替代第二、第三个真实上游交付包。
- 不修改上游算法脚本的模型逻辑。

生产 trial 在整个实施和认证期间必须保持：

```text
config.status = paused
config.version_status = shadow
registry.status = paused
production business row delta = 0
```

## 3. 总体架构

```text
Harness / Scheduler / Admin CLI
             |
             v
       Runtime Dispatcher
        /             \
NativeLifecycle    BlackboxLifecycle
NativeBacktest     BlackboxBacktest
NativeRunner       BlackboxSandboxRunner
        \             /
         Shared Registry / Version / Run / Backtest repositories
                              |
                              v
                  PredictionRecord + existing API/frontend
```

共享层负责状态模型、数据库事务、结果表和审计结构；运行时驱动负责各自的版本身份、执行协议和验收规则。禁止把 Blackbox 交付转换成 Native `predict.py + core/` 目录。

## 4. BBV2-02：统一 Blackbox 版本身份

### 4.1 Canonical 版本输入

Blackbox 的 `scheme_version` 固定由三部分计算：

1. 上游 `{scheme_id}.py` 文件摘要。
2. 上游 `{scheme_id}.json` 文件摘要。
3. 平台执行配置的 canonical 摘要。

平台执行配置摘要包含：

- `runtime_type`
- `input_source`
- `runtime_profile`
- `data_schema_version`
- `schedule.cron`
- `schedule.timezone`
- `schedule.timeout_sec`
- `delivery.script`
- `delivery.metadata`

以下生命周期或运行证据字段不参与版本计算：

- `status`
- `version_status`
- `environment_fingerprint`
- `data_snapshot_id`
- `generation_id`
- 授权人和授权时间

因此，同一交付从 `draft -> shadow -> active -> paused` 的 `scheme_version` 不变；算法文件、Metadata 或功能性平台执行配置改变时才生成新版本。

### 4.2 唯一计算入口

`scheduler.discovery.load_scheme_config()` 产出的 `SchemeConfig.scheme_version` 是 Blackbox 版本身份的唯一入口。StaticGate、all-stage 历史、Shadow、Activation、scheduler 和 run 记录不得各自重新拼装版本。

Native V1 继续沿用现有 `predict.py/core + config.yaml` 版本算法，不在本轮迁移。

### 4.3 现有 trial 的处理

canonical 规则可能使当前 trial 产生一个新的版本号。通用 discovery 只能把未知 Blackbox 版本登记为 `draft`，不能根据配置中的 `version_status: shadow` 自动晋级。

隔离认证环境重新执行 all-stage 和 shadow-register。生产库不在本轮自动重登记；旧 shadow 版本保留为历史记录，新版本如需进入生产 shadow，必须后续单独授权。

## 5. BBV2-01：Blackbox 回测持久化

### 5.1 区分两类回测验证

保留当前合同压力测试：

- 100、101、500、1000 条 Request。
- 验证分批、顺序、重复执行和截止隔离。
- `persist=false`。
- 可以复用少量日期模板，不写业务表。

新增正式持久化回测：

- 平台按任务语义生成日期唯一的历史 Request。
- 默认取最近 100 个具备完整 feature/target/actual 的有效点。
- 同一 run 内每个 `predict_date` 只出现一次，满足现有唯一键。
- Request 数量不足 100 时明确失败，不用重复日期补足。

### 5.2 历史 Request 生成

新增 Blackbox 历史 Request 工厂，复用平台日历和当前 DataBridge 快照：

- `T+1`：feature 日后的第 1 个交易日为 target。
- `T+5`：feature 日后的第 5 个交易日为 target。
- `weekly_point`：当前周频点到下一周频点。
- `weekly_average`：当前周平均到下一周平均。
- `monthly`：当前月观测到下一月观测。

每条 Request 都包含合同规定的七个字段，三个 cutoff key 必须在同一快照中真实存在。算法仍只按 Request 和 `--data-dir` 执行，不自行推导日期。

### 5.3 Actual 和 label

平台复用现有 actual 构建口径生成回测标签：

- 日频复用 `daily_actuals_updater` 的 T+1/T+5 收益率方向。
- 周频复用 `weekly_actuals_updater` 的 point/average 目标规则。
- 月频复用 `monthly_actuals_updater` 的月度目标规则。

Result 与 actual 必须按 `target_tenor + target_rule + predict_date + feature_date + target_date` 唯一匹配。缺失、多匹配或日期不一致均整批失败，不写入无标签的“成功回测”。

### 5.4 原子落库

在 `backtests.repository` 增加 connection-aware 的事务接口，一次事务完成：

1. 创建 `t_backtest_runs`。
2. 写入全部 `t_backtest_predictions`。
3. 写入需要的月度指标。
4. 更新 run summary 和 `status=success`。

任何一步失败，整笔事务回滚，不留下 success run、部分 predictions 或部分 metrics。授权使用现有 `backtest_persist` action，并绑定 `scheme_id + scheme_version + harness_run_id`。

Gate 证据必须记录：

- `persist=true`
- `backtest_run_id`
- 请求数、结果数和写入数
- 表前后计数
- `scheme_version`
- `data_snapshot_id`
- `generation_id`
- 日期范围和 target rule

`--persist` 不得再降级为 no-persist，也不得在零写入时返回 passed。

## 6. BBV2-03：生产批准和执行 hard-stop

### 6.1 专用授权

Blackbox Activation 使用专用 action：

```text
blackbox_activate
```

token 必须绑定：

- `scheme_id`
- canonical `scheme_version`
- 最近一次通过的 all-stage `harness_run_id`
- 短有效期
- `issued_by`

Blackbox Activation 在没有 `HARNESS_AUTH_SECRET` 时 fail-closed；隔离认证使用临时测试密钥。Native 既有授权行为本轮不改变。

### 6.2 激活条件

Blackbox Lifecycle Driver 只有在以下条件全部满足时才允许激活：

- config 为 `paused + shadow`。
- exact `scheme_version` 在 `t_scheme_versions` 中为 `shadow`。
- 最近 all-stage 七个 Gate 全部 passed。
- Harness 审计记录已持久化并可读取。
- 环境指纹与冻结 Runtime Profile 一致。
- DataBridge generation 和 snapshot evidence 完整。
- 授权 token 签名、作用域、版本、run id 和有效期均通过。

激活完成后：

```text
config.status = active
config.version_status = active
version.status = active
version.approved_by = token.issued_by
version.approved_at = current UTC time
registry.status = active
```

### 6.3 Scheduler 和人工 live 门禁

Blackbox 执行前必须重新查询数据库并验证：

- config 为 active。
- composite Registry 为 active。
- exact version 为 active。
- `approved_by` 和 `approved_at` 非空。
- runtime type、task type、target tenor 和 horizon 与 config 一致。

任一条件不满足时不启动算法、不写 prediction，并返回明确的 failed/blocked 结果。仅靠编辑 `config.yaml`、Registry 或单独修改 version 状态均不能绕过门禁。

通用 discovery/sync 对未知 Blackbox 版本最多创建 `draft`；它不能提升 version，也不能把 Registry 变成 active。正式状态提升只能通过 Blackbox Lifecycle Driver。

## 7. BBV2-04：Sandbox 与环境变量隔离

### 7.1 文件读取 allowlist

删除 `(allow file-read*)`。每次运行动态生成只读 allowlist：

- 冻结 Python 解释器及其环境前缀。
- Runtime Profile 声明的必要系统库目录。
- 当前交付脚本。
- 当前 Request 文件。
- 当前只读三频快照目录。
- 进程启动所需的最小系统设备和动态链接信息。

写入只允许本次运行临时目录和 `--output` 所在目录。`--data-dir` 显式 deny write。项目其它目录、用户主目录、`/etc/hosts` 和任意未声明路径均不可读取。

Runtime Profile 增加版本化的 `read_roots`、`environment_allowlist` 和 `environment_defaults`。实际路径必须在运行前 resolve，并拒绝 symlink 越界。

### 7.2 最小环境

子进程环境从空字典构建，不再使用 `os.environ.copy()`。只注入：

- 运行所需的 `PATH`、locale 和时区。
- Runtime Profile 明确允许的线程数变量。
- 指向本次隔离临时目录的 `HOME`、`TMPDIR` 和缓存目录。

以下变量及其同类变量不得继承：

- 数据库账号和密码。
- DataBridge 账号和密码。
- Harness 授权密钥和 token。
- 云服务凭据、代理配置和用户自定义 secrets。

网络继续 deny。测试必须证明 LightGBM、NumPy 和 pandas 在收紧后仍能正常 import 和执行，同时 `/etc/hosts`、临时 secret 文件和父进程 secret 变量不可见。

## 8. BBV2-05：按载体严格解析 Result

JSON 与 CSV 使用不同的方向解析策略：

- `prediction.json`：`predicted_direction` 必须是 JSON integer，且不是 bool，只允许 `-1/0/1`。
- `backtest.csv`：CSV 单元格天然是文本，允许精确 token `-1`、`0`、`1`，再转换为整数。

JSON 的 `"1"`、`1.0`、`true`、空值和其它数字全部拒绝。CSV 的 `1.0`、`+1`、空白、空值和其它文本全部拒绝。

合同层提供显式入口，不允许调用方依赖一个会隐式宽松转换的公共函数：

```text
load_prediction_result()  -> strict JSON direction
load_backtest_results()   -> strict CSV token direction
```

## 9. BBV2-06：Scheduler 退出语义

`run_prediction_job()` 返回 `SchemeRunResult`，`run_all_prediction_jobs()` 返回结果列表。CLI 统一映射：

| 场景 | 退出码 |
|---|---:|
| 全部 success，或按日历/paused 正常 skipped | 0 |
| 任一 failed 或 partial | 1 |
| scheme 不存在、参数或平台配置错误 | 2 |

APScheduler 使用包装函数执行；当结果为 failed/partial 时包装函数抛出异常，使 APScheduler 也记录任务失败。业务执行函数本身继续返回结构化结果，便于 API、测试和批量聚合。

CLI 必须输出可识别的状态摘要，不能只依赖日志文本。stale generation、算法超时和 Result 非法的回归测试必须验证退出码为 1 且 prediction 增量为 0。

## 10. BBV2-07：生命周期事务与自动对账

### 10.1 不虚假承诺跨介质事务

MySQL 事务不能与仓库中的 `config.yaml` 形成真正的单事务。本设计采用：

```text
数据库内单事务
+ config 原子替换
+ 持久 lifecycle journal
+ fail-closed 运行门禁
+ 自动 reconciliation
```

### 10.2 数据库事务原语

repository 增加 connection-aware 私有写入函数，并提供 Blackbox lifecycle 事务入口。在同一数据库事务中更新：

- exact `t_scheme_versions` 行。
- 对应 composite `t_scheme_registry` 行。

通用 `sync_scheme_registry()` 不得覆盖已批准 Blackbox 版本，也不得将未批准版本提升为 active。

### 10.3 Lifecycle journal

每次 shadow 或 activation 操作先在 gitignore 的运行期目录写入 journal：

```text
backtest_artifacts/blackbox_v2_lifecycle/{scheme_id}/{operation_id}.json
```

journal 记录：

- operation id 和 action。
- scheme id/version/harness run id。
- config、version、Registry 的 previous state 和 target state。
- 各阶段时间、状态和错误。
- 授权 token 的哈希，不保存 token 或 secret。

阶段固定为：

```text
prepared -> config_written -> db_committed -> verified
                                  |
                                  v
                     compensated / unresolved
```

### 10.4 更新顺序和恢复

操作顺序：

1. 完成全部只读 preflight。
2. 写入 `prepared` journal 并 fsync。
3. 记录授权审计并消费一次性 token；后续重试必须使用新 token。
4. 原子写入目标 config。
5. 执行 version + Registry 数据库事务。
6. 独立重读 config、version 和 Registry，验证三方一致。
7. journal 标记 `verified`。

若步骤 4 或 5 失败：

- Shadow 操作恢复到原始 `paused + draft/validated` 状态。
- Activation 操作恢复到安全的 `paused + shadow` 状态。
- 数据库补偿在一个新事务中完成，config 使用原子替换恢复。
- 补偿成功标记 `compensated`；失败标记 `unresolved` 并阻断后续授权。

进程崩溃后，下次 shadow、activation、live 或 scheduler 执行前检查未完成 journal。自动 reconciliation 只允许收敛到 journal 记录的 previous safe state，不会自动完成到 active。人工命令可输出差异并执行同一安全对账逻辑。

由于 scheduler 同时校验 config、Registry 和 exact approved version，任何中间态都不能产生 live prediction。

## 11. 错误处理与可观测性

所有新增路径遵循：

- 配置、授权、版本、数据或环境不完整时 fail-closed。
- 失败信息不得包含密码、token、父进程环境变量值或原始业务数据。
- Gate 报告记录检查过的事实，不把常量声明成数据库/API 实测证据。
- 业务写入前后计数必须来自独立查询。
- 临时 Request、Output、snapshot 和 sandbox 目录在成功或失败后清理。
- unresolved lifecycle journal、残留快照或部分数据库状态进入健康检查 error。

## 12. 测试策略

### 12.1 测试驱动顺序

每个阻塞项先增加失败测试，再实现最小修复：

1. canonical Blackbox version 测试。
2. JSON/CSV Result 严格解析测试。
3. scheduler 退出码测试。
4. sandbox 文件与环境 allowlist 测试。
5. Blackbox 生产批准 hard-stop 测试。
6. lifecycle 事务、崩溃点和 reconciliation 测试。
7. 历史 Request、actual join 和回测原子落库测试。

### 12.2 自动测试

- lifecycle 字段变化不改变 Blackbox version；脚本、Metadata 或功能配置变化必须改变版本。
- Native version 回归不变。
- 未批准、版本不匹配、Registry/config 单边 active 均无法执行 Blackbox。
- generic discovery/sync 不能把未知 Blackbox 版本提升到 shadow/active。
- JSON 字符串和浮点方向被拒绝，CSV 精确 token 通过。
- `/etc/hosts`、任意 secret 文件和父进程 secret 环境变量不可读取。
- 冻结环境中的真实 LightGBM 交付仍可 predict/backtest。
- 100 个持久化 Request 日期唯一、结果和 actual 一一对应。
- 回测插入中间失败时四类相关表无部分写入。
- shadow/activation 在每一个故障点均恢复到安全状态。
- unresolved journal 阻断授权和执行。
- failed/partial scheduler run-once 返回 1；配置错误返回 2。

### 12.3 隔离全链路认证

使用独立 Git worktree 和独立 MySQL 测试 Schema：

```text
原始两文件 Intake
-> canonical version all-stage 连续回归
-> shadow-register
-> 正式 blackbox ActivationGate
-> 100/500/1000 条 no-persist 回归
-> 100 条 persist 回测及 API/前端回测验证
-> gray_live
-> scheduler run-once
-> actual updater
-> Registry/API/前端实盘验证
-> 故障注入和 reconciliation
-> pause 与三方状态对账
```

禁止使用直接 SQL 强制 active 或绕过 Gate 的测试证据申请正式通过。

## 13. 验收标准

`BBV2-01` 至 `BBV2-07` 只有满足以下条件才算关闭：

- 每项都有修复前失败、修复后通过的自动测试。
- 正式 ActivationGate 在隔离库通过，不使用 `FORCED_ACTIVE_TEST_ONLY`。
- persist 回测产生预期 run/prediction/metric 行，API 和前端可读取。
- live 与 scheduler 各自产生且仅产生预期的一条 run 和 prediction。
- actual 与 API 指标逐字段一致。
- sandbox 安全负向测试全部通过。
- lifecycle 故障注入后没有 unresolved 状态和生产副作用。
- 生产 trial 的 config、Registry 和四类业务表前后保持不变。

最终报告必须分别给出：

```text
SHADOW_READY
PRODUCTION_PATH_READY
PRODUCTION_READY
```

本轮预期前两项可以 PASS；在 `BBV2-08` 关闭前，`PRODUCTION_READY` 仍保持 `CONDITIONAL/NOT_CERTIFIED`，不得用单一周频方案外推全部新方案。

## 14. Git 与发布边界

- 实施使用独立 `codex/` 修复分支或 worktree。
- 每组改动只提交相关代码、测试和文档，不纳入现有未跟踪 launchd 文件或 `outputs/`。
- 不自行合并到 `master`，不推送生产发布。
- 修复和隔离认证完成后，由用户决定是否合并回 `codex/audit-bugfixes-20260613`。

