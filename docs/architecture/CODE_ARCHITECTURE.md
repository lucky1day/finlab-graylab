# 代码架构

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：平台开发和代码审计人员
**定位**：本仓库的代码架构主蓝图，定义分层模型、包依赖方向、运行时调用图和扩展边界。

身份与接口见[共享契约](SCHEME_CONTRACT.md)，验证与事务见[Harness 架构](HARNESS_ARCHITECTURE.md)，算法适配见[保真规则](SOURCE_ALGORITHM_FIDELITY.md)。本文描述代码职责，不代替[入库 SOP](../onboarding/README.md)或[部署操作](../../deploy/README.md)。

## 1. 架构风格

**约定式插件 + 分层 + 横切 harness**。

- **分层（Layered）**：自下而上 5 层，依赖只能向下，禁止向上与跨层回指。
- **插件（Plugin）**：方案通过 `schemes/{scheme_id}/config.yaml` 被发现，再按显式 `runtime_type` 分派；所有新身份只能由 Blackbox V2 Intake 创建。
- **横切（Cross-cutting）**：`harness/` 横切所有层，合同验证与入库编排，不被任何层依赖；`python -m harness` 是统一机器入口。
- **环境隔离（Process isolation）**：Native 原生算法在 `forecast_env`，Blackbox V2 只按 `blackbox-v2-v1` Runtime Profile 选择环境与执行预算，服务在 `bond_factor_lab_service`。运行驱动不同，统一输出均收敛到 `PredictionRecord`。

方案运行时和目标环境范围由配置与部署矩阵决定，当前实际部署见[当前状态](../CURRENT_STATUS.md)。

## 2. 分层模型

| 层 | 包 | 职责 |
|---|---|---|
| L1 | `shared/` | 数据接入、日历、输入 artifact 与公共模型 |
| L2 | `schemes/{id}/` | Native adapter/core 或 Blackbox 原始交付 |
| L3 | `scheduler/` | 方案发现、算法执行、预测事务与 Actuals |
| L4 | `backend/`、`backtests/`、`tests/` | 产品与认证接口、Blackbox 回测和验证 |
| L5 | `harness/` | Blackbox 验证、激活及公共展示验收与受控补缺 |

具体实现入口集中在 §6；表中的层级描述依赖位置，不表示所有上层都能调用任一下层，允许边以 §3 为准。

---

## 3. 包依赖方向规则（强约束核心）

以下矩阵定义生产包之间允许的依赖；未列出的跨层边不得自行增加。
现行仓库静态依赖检查的扫描范围与局限见 §7。

### 3.1 依赖规则矩阵（行=源，列=能否依赖目标）

| 源 \ 目标 | shared | schemes.core | schemes.predict | scheduler | backend | backtests | harness |
|-----------|:------:|:------------:|:---------------:|:---------:|:-------:|:---------:|:-------:|
| **shared** | 自身 | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **schemes.core** | ✗ⁱ | 自身 scheme 内 | ✗ | ✗ | ✗ | ✗ | ✗ |
| **schemes.predict** | ✅ | 本 scheme 内 ✅ | — | ✗ | ✗ | ✗ | ✗ |
| **scheduler** | ✅ | ✗ | 运行时 importlibᵈ | 自身 | ✗ | ✗ | ✗ |
| **backend** | ✅ | ✗ | ✗ | ✅ | 自身 | ✗ | ✗ |
| **backtests** | ✅ | ✅ | ✗ | ✗ | ✗ | 自身 | ✗ |
| **harness** | ✅ | 只读AST | 只读AST | ✅ | ✅ | ✅ | 自身 |

- ✗ⁱ：`schemes.core` **禁止** import `shared`（含 `data_service`/`input_artifacts`）——core 必须是纯算法，输入由 adapter 注入。这是"core 零 DB"的根。
- importlibᵈ：`scheduler.scheme_runner` 在 **conda 子进程运行时**用 `importlib.import_module(f"schemes.{id}.predict")` 动态加载，不是静态 import 边——保持 scheduler 对具体方案零静态耦合（插件模型的关键）。
- 静态规则仍保留 `backtests → 同方案 core/inference` 的兼容边；当前平台仅提供 Blackbox 回测入口，不据此恢复 Native 历史重跑。`backtests → schemes/{id}/predict` 禁止，避免历史复现调用 live adapter。
- 跨方案：`schemes/A` **禁止** import `schemes/B`（任何子模块）。

### 3.2 输入、写入与算法边界

[根规范](../../AGENTS.md#算法与数据不变量)定义四条不变量；以下是代码落点：

| 边界 | 实现职责 |
|---|---|
| 源输入 | `shared.input_artifacts` 经数据服务/日历构造输入；adapter 与 backtest runner 不自行查询源表 |
| 预测与生命周期 | `scheduler.repository` 按 runtime/operation 在事务中复核身份并完成 prediction、run 与日志 |
| 回测 | `backtests.repository` 保存不可变 `t_backtest_*`；首次产品历史发布另经 scheduler repository |
| Actual | `scheduler/*_actuals_updater.py` 按任务事实合同更新，既有 UPSERT 不受预测 insert-only 规则外推限制；日频自然刷新从单一源快照写入且不删除历史，尾部删除只允许走独立的精确键修复计划 |
| 认证 | `backend.auth.repository` 仅写认证三表，账户/会话合同见[认证文档](AUTHENTICATION_AND_ACCOUNT_MANAGEMENT.md) |
| DDL | `migrations.runner` 接收 caller-supplied Engine；CLI 负责环境与授权围栏，见 §5 |

Native core 的输入由 adapter 注入，算法适配层级和历史/live-safe 口径以[保真规则](SOURCE_ALGORITHM_FIDELITY.md)为准。依赖合规以当前机器扫描证明，不保存历史违规豁免（§7）。

## 4. 运行时调用图

### 4.1 预测路径（自然调度 / 单日补缺）

宿主 launchd/systemd 调用一次性 runner；Python 不拥有独立时钟或额外 Writer。
[discovery](../../scheduler/discovery.py) 加载配置时核对目录名与 `scheme_id`，按 base 发现；
多 target 不另建运行发现，身份与组内原子提交遵守[共享契约](SCHEME_CONTRACT.md)。
控制面权属、installed/loaded 证明和恢复边界见[生产调度治理](PRODUCTION_SCHEDULING_GOVERNANCE.md)。

```text
宿主已安装 one-shot（launchd plist / systemd unit，单一 cadence writer）
  → discovery.py 按 runtime_type 发现 active SchemeConfig
  → shared.input_artifacts 校验 artifact freshness 与 feature cutoff
  → scheduler.executor.execute_scheme(cfg, predict_date, prediction_phase)
       ├─ native_adapter: run_scheme_subprocess → schemes.{id}.predict.run
       ├─ blackbox_v2: get_ready_blackbox_snapshot → 受控 CLI
       ├─ strict PredictionRecord/日期语义校验
       └─ scheduler.repository 的专用原子完成边界
            → t_scheme_runs + t_scheme_predictions + run log
```

历史 gap harness 只以 `gray_live` insert-only 修复；宿主 one-shot 自然触发才可以
写 `scheduled_live`。早期失败/跳过只写审计日志，不能伪装为成功完成。

区间/单日入口、支持任务与恢复步骤见[平台 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)。区间 planner 解析本机输入 authority，执行器完成算法与 Result 校验，repository 单事务复核全部键并提交；不能绕过其中任一边界。

`shared.prediction_context` 与任务日历生成三日期；executor 在写入前复核，预测与 Actuals 共用 `shared.week_calendar_normalizer`。语义与允许的归一化范围见[预测语义](PREDICTION_SEMANTICS.md)。Actuals 由 `scheduler.actuals_runner` 一次性入口编排；日频读取、构造和普通 UPSERT 共用一个事务，不在自然链路执行尾部删除。各 cadence 的期望时钟只在[部署手册](../../deploy/README.md)维护。

### 4.2 Blackbox 入库路径

入库与版本修订从[入库导航](../onboarding/README.md)进入；Gate 职责和事务边界见[Harness 架构](HARNESS_ARCHITECTURE.md)。

### 4.3 历史回测路径

```text
Blackbox: Harness → backtests.blackbox_v2 → 原始两文件 CLI + 标准 Result
  ├─ shared.input_artifacts → 本机可信输入
  ├─ backtests._base_runner → RunOutput 与运行内指标（使用该公共模型的 runner）
  └─ backtests.repository → t_backtest_* 不可变回测证据
```

Blackbox 首次 activation 通过 `scheduler.repository` 将获准的历史回测 insert-only 物化为
`t_scheme_predictions` 产品事实；回测 Gate 不承担该发布操作。回测表保留不可变证据与元数据，
Dashboard 只从产品事实表聚合逐点结果。
平台仅保留 Blackbox 回测执行入口；W4 独立历史重跑与旧分步写库接口已退役，已有结果和来源原件保留。W4 存量运行依赖按[运行 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)保护。

### 4.4 查询路径

```
前端 → backend.main GET /api/factor-lab/dashboard
  └─ backend.factor_lab_dashboard.build_factor_lab_dashboard(engine)
       ├─ 每个请求直接以 dashboard 专用只读 Engine 建立当前视图
       ├─ 同一 connection / repeatable-read readonly transaction
       ├─ active Registry + 产品预测事实 + scoped Actuals + canonical backtest 元数据批量 SELECT
       └─ canonical 选择 → V6 summary/detail response → gzip/identity 表示

```

该读模型不提供算法输入、不写业务库，不读取 run、DataBridge 日期或交易日历，也不推导调度是否到期或缺失。HTTP、Summary/Detail、刷新与故障行为以[Dashboard 合同](../operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md)为准；统计定义见[预测语义](PREDICTION_SEMANTICS.md)。

## 5. 环境、执行与迁移

| 关注点 | 当前实现边界 |
|---|---|
| 引擎 | 调度 Engine 接收显式 `DatabaseConfig` 或调用环境适配器；Backend 的认证与 Dashboard 各用独立、有界的 HTTP Engine，不复用调度连接池 |
| 配置 | `DatabaseConfig.from_mapping()` 只解析调用方数据；`from_env()` 才选择显式私有文件或当前环境，导入模块不加载 `.env`、不修改环境、必填连接信息无默认回退；canonical 定义方案；Runtime Profile 定义 Blackbox 执行环境和资源上限 |
| 执行预算 | executor/Blackbox runner 采用方案和 Runtime Profile 预算；具体合成与留证要求见[Harness 架构](HARNESS_ARCHITECTURE.md#2-验证与执行边界) |
| 输入与产物 | W4 运行使用作业临时输入根并清理；Blackbox ready snapshot 与私有状态分别受控；历史证据由 repository 与外置 artifact 保存 |
| 错误 | executor 记录失败 run/log；Harness 返回 Gate 结果，涉及持久化时核实相应 repository 的事务结果 |

`migrations.runner` 只接收 caller-supplied `Engine`，实现 manifest、inspect、apply 与 APPLYING recovery，不读环境变量或解析 CLI，也不判定生产授权。`scripts/apply_migrations.py` 负责受限参数、目标环境与数据库身份围栏；其他模块不得复制迁移行为。命令、恢复摘要、备份及版本兼容要求集中在[部署手册](../../deploy/README.md)。

## 6. 代码导航

| 职责 | 实现入口 |
|---|---|
| 输入与日历 | [input_artifacts](../../shared/input_artifacts.py)、[data_service](../../shared/data_service.py)、[calendar_service](../../shared/calendar_service.py)、[Blackbox 公共模块](../../shared/blackbox_v2/) |
| 身份与执行 | [discovery](../../scheduler/discovery.py)、[scheme_runner](../../scheduler/scheme_runner.py)、[executor](../../scheduler/executor.py) |
| 自然任务与 Actuals | [launchd runner](../../scheduler/launchd_prediction_runner.py)、[systemd runner](../../scheduler/systemd_prediction_runner.py)、[actuals_runner](../../scheduler/actuals_runner.py) |
| 业务写入 | [scheduler repository](../../scheduler/repository.py)、[backtest repository](../../backtests/repository.py) |
| 产品与认证 | [HTTP 入口](../../backend/main.py)、[Dashboard](../../backend/factor_lab_dashboard.py)、[认证](../../backend/auth/) |
| 平台验证 | [harness](../../harness/)；模块边界见[Harness 架构](HARNESS_ARCHITECTURE.md) |
| 数据模型与迁移 | [公共模型](../../shared/models.py)、[迁移库](../../migrations/runner.py)、[迁移 CLI](../../scripts/apply_migrations.py) |

## 7. 仓库静态依赖检查

`harness.contracts.import_rules.repository_layer_import_violations` 扫描 `shared/`、`schemes/`、
`scheduler/`、`backend/`、`backtests/`、`harness/` 中的生产 Python 文件，检查 §3 的静态依赖方向。
`tests/`、`outputs/` 和未纳入分层图的管理脚本不在扫描集。检查报告精确路径、行号和 import，
不以历史违规基线放行；它不能证明动态加载、运行期输入安全或写库授权。

[tests/test_architecture_boundaries.py](../../tests/test_architecture_boundaries.py)调用该扫描器检查当前仓库及违规样例。
Blackbox 两文件、Metadata、输入与 Result 由 Intake、持久化回测及执行边界校验；W4 的输入、执行器、
source isolation 和动态加载保护按[公共验证矩阵](../onboarding/README.md#可复用测试矩阵)选择，不能以静态扫描替代。
本地测试通过只证明所检查的边界；远端 CI 是否运行及结果须查实际配置与执行记录。
