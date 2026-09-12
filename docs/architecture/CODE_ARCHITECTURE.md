# 代码架构设计（Code Architecture）

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：平台开发和代码审计人员
**定位**：本仓库的代码架构主蓝图，定义分层模型、包依赖方向、运行时调用图和扩展边界。
**与既有文档的关系**:
- 本文是代码和系统调用关系的唯一架构总图；DB schema 以 migrations 为准，API 以 Backend
  路由与 dashboard 合同为准，部署控制面以生产调度治理为准。
- [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) 边界总纲 → [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) 共享方案契约 → [onboarding/README.md](../onboarding/README.md) 统一入库导航。本文把它们统一到一张依赖图上。
- [SOURCE_ALGORITHM_FIDELITY.md](SOURCE_ALGORITHM_FIDELITY.md) 是 source-backed 方案的源算法保真总纲；它约束 L2 core 与 L4 backtest runner 不得借平台适配改变原始算法逻辑。

---

## 1. 架构风格

**约定式插件 + 分层 + 横切 harness**。

- **分层（Layered）**：自下而上 5 层，依赖只能向下，禁止向上与跨层回指。
- **插件（Plugin）**：方案通过 `schemes/{scheme_id}/config.yaml` 被发现，再按显式 `runtime_type` 分派；所有新身份只能由 Blackbox V2 Intake 创建。
- **横切（Cross-cutting）**：`harness/` 横切所有层，只读探测 + 编排 + 留证，不被任何层依赖；`python -m harness` 是统一机器入口。
- **环境隔离（Process isolation）**：Native 原生算法在 `forecast_env`，Blackbox V2 只按 `blackbox-v2-v1` Runtime Profile 选择环境与执行预算，服务在 `bond_factor_lab_service`。运行驱动不同，统一输出均收敛到 `PredictionRecord`。

Native 路径仅为 Mac3 W4 九方案及其必要依赖保留；其余 17 个原 ID canonical 使用 Blackbox V2。
T1/T5 每 target 独立两文件交付，每 base 一个整体 exact version、一个调度任务、全部目标一次原子提交。
已退役 Native 算法、Phase-A 缓存、迁移原件 loader 和历史搬迁命令不构成新的平台能力；旧证据只读追溯。
同 ID 迁移只切未来唯一 Writer，不新建 `_bbv2` 业务身份、不重跑或搬删历史；实际双机部署进度只读当前状态与现场。

---

## 2. 分层模型

```
┌──────────────────────────────────────────────────────────────────────┐
│  L5  harness/                     横切：Gate 检查 / 编排 / 审计          │  ── 只读，不被依赖
└──────────────────────────────────────────────────────────────────────┘
        ▲ 只读探测 / 调用，从不被下层 import
┌──────────────────────────────────────────────────────────────────────┐
│  L4  接口与验证层                                                       │
│      backend/ (FastAPI 查询)   backtests/ (历史复现)   tests/ (验证)    │
└──────────────────────────────────────────────────────────────────────┘
        │ backend→scheduler   backtests→schemes.core
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│  L3  预测任务层  scheduler/                                             │
│      discovery / scheme_runner / executor / repository / *_actuals     │
└──────────────────────────────────────────────────────────────────────┘
        │ 运行时 importlib → schemes.{id}.predict（conda 子进程）
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│  L2  算法层  schemes/{scheme_id}/                                      │
│      native: predict.py → core/       blackbox: delivery/*.py + *.json │
└──────────────────────────────────────────────────────────────────────┘
        │ adapter→shared.input_artifacts / calendar_service / models
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│  L1  统一公共层  shared/                                                │
│      data_service / input_artifacts / data_bridge / calendar_service    │
│      models / db_config / artifact_paths                                │
└──────────────────────────────────────────────────────────────────────┘
        ▼
     MySQL bond_db（源表只读 / 写库表白名单）
```

每层一句话职责：

| 层 | 包 | 职责 | 对外稳定符号（节选） |
|----|----|------|----------------------|
| L1 | `shared/` | 唯一数据接入与公共模型 | `build_*_input_artifact`、`data_bridge_current` 刷新/快照、`get_calendar`、`PredictionRecord` |
| L2 | `schemes/{id}/` | 原生 adapter/core 或 Blackbox 原始两文件 | `predict.run()` 或 Blackbox CLI |
| L3 | `scheduler/` | 发现、dry-run、写库、actuals、调度 | `discover_schemes`、`run_scheme`、`execute_scheme`、`create_scheme_run`、`complete_active_native_run`、`complete_approved_blackbox_run`、`complete_gray_gap_run` |
| L4 | `backend/` `backtests/` `tests/` | 只读 API、历史复现、验证 | `/api/*`、`run_<scheme>_reproduction` |
| L5 | `harness/` | Gate 检查 / 编排 / 审计 | `python -m harness ...`、`GateResult` |

---

## 3. 包依赖方向规则（强约束核心）

这是"强约束 harness 工程"的骨架：**每条允许的 import 边都明确列出，未列出的即禁止**。
onboarding StaticGate 与 repo-wide CI gate 分别守护方案局部契约和全仓静态依赖图（§9）。

### 3.1 允许的静态 import 边

```
shared/        → （无；仅依赖第三方库 pandas/sqlalchemy 与 shared 内部）
schemes/*/predict.py → shared.{input_artifacts, calendar_service, models}        [+ data_service 仅限引擎，过渡期]
schemes/*/core/      → （无本仓库依赖；仅 pandas/numpy/sklearn/lgbm）
scheduler/     → shared.{models, db_config, data_service}  + scheduler 内部
backend/       → scheduler.{repository, discovery, executor, main} + shared + backend 内部
backtests/     → shared.{input_artifacts, data_service, calendar_service}
                 + schemes/*/{core,inference}（调用方案算法，不依赖 predict adapter）
                 + backtests 内部
harness/       → 读取/调用 scheduler、shared、backtests（编排用）；不被任何层 import
tests/         → 任意（验证需要）
```

### 3.2 依赖规则矩阵（行=源，列=能否依赖目标）

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
- `backtests → schemes/{id}/inference` 与 `backtests → schemes/{id}/core` 是按同一方案复现算法的允许边；`backtests → schemes/{id}/predict` 禁止，避免历史复现调用 live adapter。
- 跨方案：`schemes/A` **禁止** import `schemes/B`（任何子模块）。

### 3.3 四条不可破坏的不变量

1. **依赖只向下**：上层可依赖下层，下层永不依赖上层（`shared` 不知道 `schemes` 存在；`schemes` 不知道 `scheduler` 存在）。
2. **写库单点**：只有 `scheduler.repository` / `backtests.repository` / `*_actuals_updater` 能写库；其余层零写库。
3. **输入单点**：算法输入只能经 `shared.input_artifacts` 产出；adapter / backtest runner 不得自拼 DB 输入。
4. **源算法保真**：source-backed 方案的 L2 core 必须复现原始算法的时间起点、窗口、特征、对齐、模型参数、投票/fallback 和内部 score 映射。平台适配只能发生在算法外层；若 source-original 输出与平台 current 不一致，先查输入 artifact 与 source 口径，不得调算法贴结果。若 source-original batch 的 `source_end` 或 test window 晚于样本 `feature_date`，该 batch 只能验收 source-original backtest；gray/live/scheduled live 必须保持 `feature_date` 硬截止并用 live-safe oracle 验收。所有改动必须先分级为 L0/L1/L2；L2 算法内部改动默认禁止。

---

## 4. 依赖合规证明

依赖合规只以 onboarding StaticGate 与 repo-wide import gate 的当前扫描结果为准，不在架构文档维护
已修复违规清单或历史扫描快照。门禁覆盖范围见 §9。

---

## 5. 运行时调用图

### 5.1 预测路径（自然调度 / 单日补缺）

宿主 launchd/systemd 是唯一调度控制面，仓库 runner 只是一次性执行器；refresh、daily、weekly、
close-period 和 actuals 每个 cadence 只能有一个 writer。Backend 不提供手动预测入口，常驻 Python scheduler
与 ledger 类控制面不得恢复。installed 状态、触发和失败恢复的完整规则见
[生产信号与调度治理](PRODUCTION_SCHEDULING_GOVERNANCE.md)。

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

入口（单日历史补缺）：`python -m harness signal-gap-fill --predict-date YYYY-MM-DD
[--scheme-id <base_scheme_id>]`。单个 active Blackbox `weekly_point/h1` 或日频 `T+5/h5` 方案还可以使用
`--scheme-id <base_scheme_id> --target-date-from YYYY-MM-DD --target-date-before YYYY-MM-DD`
执行 target 半开区间。区间计划在一个只读快照中解析一次 DataBridge authority，按调度日分组 run，
但同一方案只核对一次 producer-ready receipt、物化一个私有运行视图并启动一个算法 batch；全部业务键预检通过后由 repository 在一个事务中
insert-only 提交所有 `gray_live` prediction。命令本身就是补数授权，不接收外部 plan、operator、HMAC token
或 plan SHA；任一算法或提交失败时 prediction 零提交。Native 仍只支持单日入口。两种入口都不产生
`scheduled_live`，也不读取 Native 历史 generation。

日期语义由 `shared.prediction_context` 和各频率 adapter 统一落地：日频实盘为 `predict_date=T+1, feature_date=T`；周频实盘先由 `predict_date` 反推上一交易日 `feature_date`，再映射 `feature_week_id`；月频 source-backed 方案若声明自然 15 号触发，则 `predict_date` 保留自然月 15 号，`feature_date` / `target_date` 分别取当前月/目标月 15 号及以前最近交易日。`scheduler.executor` 在日频 live 写库前再次校验 `predict_date/feature_date/target_date`，防止源表水位不足时算法复用旧 feature/target 覆盖旧 target 明细。常驻 scheduler 的 startup catch-up 与 cron 路径已删除：服务启动不会按 cron 推断或补跑错过的预测任务，`scheduled_live` 只由对应宿主 one-shot 自然时钟写入。`shared.calendar_service` 和 `scheduler.weekly_actuals_updater` 共享 `shared.week_calendar_normalizer`，只对源周历孤立 forward jump 做只读归一化，确保预测 target 与 weekly actuals 使用同一周历事实。所有前端月份归属、actual join 和 gray/backtest 分流仍以 `target_date` 为事实键。

`schedule.timeout_sec` 是 L3 调度执行层的方案预算申请，不是算法输入；Native 用它控制
`scheduler.executor` 等待子进程的最长时间。Blackbox predict 还受
`deploy/blackbox_v2/runtime_profile_v1.json` 的平台上限约束，调用方显式 operation deadline
只形成第三个收紧约束；最终取三者最小值。任何 adapter/core 都不得根据运行预算改变窗口、
特征、fallback 或输出。Native 版本变化的激活遵循
Native SOP 的 Gate 与授权边界，不再存在需要维护的 frozen daily-gray policy。任何后续
宿主调度配置切换都必须先复核 installed plist/unit、控制面状态和对应日志，不能笼统以
“重启 scheduler”代替控制面验收。

日频、周频、月频和周期均值 actuals 由宿主控制面调用 `scheduler.actuals_runner` 一次性刷新；具体
触发时点只在生产调度治理文档维护。

### 5.2 入库路径（首次入库与 Native 后续维护）

```
Blackbox V2:
  intake-blackbox          → 新 ID：两文件 + Metadata + 固定 Profile/Schema + 安全静态边界
  gate backtest --persist  → 复验脚本安全边界 + producer-ready snapshot + 完整批量执行 + immutable backtest
  activate                 → 匹配 canonical exact-version 回测证据 + 单事务 insert-only 建立 active identity

Native V1:
  onboard --stage all      → StaticGate + DryRunGate + CompareGate + BacktestGate(--no-persist)
  onboard --stage native-maintenance → StaticGate + admission + DryRunGate

  单日补缺入口：signal-gap-fill  ← planner 绑定 active identity、业务键与 input authority
  可选产品读模型检查：DashboardGate → GET /api/factor-lab/dashboard
```

技术 `all` 只属于 Native 且不访问 Backend。`DashboardGate` 验证 active
composite、展示身份、任务字段与回测分区可见；空 live 明细合法。dashboard payload 不携带 exact version，因此版本身份仍由
生命周期和数据库权威回读证明。

上图的 `all` 仅用于政策清单内 Mac3 W4 九个存量身份的完整准入，不能用于新增 Native；source benchmark/CompareGate 在其中作为保真硬证据。Blackbox 由上游负责交付可运行性和内部性质，平台的完整持久化回测验证批量调用与标准输出。已入库 Native
修订仅在不同 prior Native version 的 passed `all + compare` 所属 StaticGate 已持久化
`static.business_identity`，且该快照与当前身份精确匹配时，才可走：

```text
static -> native-maintenance-admission -> dry-run
```

快照只保存 `scheme_id`、`runtime_type`、`horizon`、`task_type`、`frequency`、target tenors
与 composite Registry IDs，不保存代码、config 或 version hash。maintenance 的 current exact
`t_scheme_versions` 行必须为 `runtime_type='native_adapter'` 且 status 为 `draft|active`；expected
Registry identity 可在预激活时统一为 `paused`，或在激活后统一为 `active`，但 draft version 配 active
Registry 必须 fail-closed。只有 ActivationGate 可在严格 discovery、精确版本与标准 Gate 核验后原子
建立 active 状态。缺少、重复、损坏或不匹配的 prior snapshot 一律 fail-closed，不再保留方案级历史
receipt。该三段路径不运行当前 historical `compare/backtest`、不写业务表，且不适用于 Blackbox；其后
activation 仍要核验当前精确 version 与两个 Gate。反之，current exact version 的
完整 `all` 通过时，ActivationGate 走互斥的 `full_initial_onboarding_v1`，不要求此 prior snapshot
或三段路径。

详见 [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md)。

### 5.3 历史复现路径

```
python -m backtests.{scheme_id}_reproduction [--no-persist]
  ├─ shared.input_artifacts.build_*_input_artifact(...)         ← L1 唯一输入（V4 待统一）
  ├─ schemes.{id}.core.predictors.*  (逐历史点跑算法)
  ├─ 按 target_date 归月生成运行内月度指标（只用于输出/summary）
  └─ backtests.repository → t_backtest_runs / _predictions / _reproduction_checks   ← 回测写库单点
```

前端历史回测指标只能由 `t_backtest_predictions` 明细动态聚合。

### 5.4 查询路径

```
前端 → backend.main GET /api/factor-lab/dashboard
  └─ backend.factor_lab_dashboard.build_factor_lab_dashboard(engine)
       ├─ 每个请求直接以 dashboard 专用只读 Engine 建立当前视图
       ├─ 同一 connection / repeatable-read readonly transaction
       ├─ active registry + live predictions + scoped actuals + latest backtest 批量 SELECT
       └─ canonical 选择 → compact V3 response → gzip/identity 表示

数据库或构建失败时 route 直接返回 `503 dashboard_data_unavailable`；后端不保留进程内
last-known-good 数据，也不返回 stale 快照。浏览器若已经提交过成功快照，则在刷新失败时保留该
完整视图并明确标为 `stale`；首次读取失败仍进入不可用状态。

```

dashboard snapshot 是 L4 唯一前端读模型：不提供算法输入、不写库、不修改任何方案 core，
因此不改变 §3.3 的输入单点、写库单点、Native core 纯净和源算法保真四条不变量。
Dashboard 是唯一业务读模型；失败时前端按当前是否存在成功快照进入 stale 或不可用状态。不得恢复细粒度
schemes、metrics 或 backtest 展示 API，也不得在浏览器中重建第二套聚合。
Dashboard 不读取 run、DataBridge 日期或交易日历，也不推导调度是否到期或缺失；这些结论只由
scheduler、run、宿主调度日志和受控 gap-fill 链路处理。

指标查询路径必须保留两层分母语义：`samples` 是月度样本总数，包含预测为“平”的样本；`metric_samples` 是准确率、precision、recall 的真实分母，只包含预测为“涨/跌”的有方向样本。前端每日/周度验证表中预测为“平”的行只显示 `-`，不得显示 `×` 或 `✓`。

---

## 6. 双运行时扩展模型

发现层统一扫描 `schemes/*/config.yaml`，执行层按 `runtime_type` 分派：

```text
Native V1（仅存量维护）           Blackbox V2（所有后续新增）
schemes/{id}/                     schemes/{id}/
├── config.yaml                   ├── config.yaml
├── predict.py                    └── delivery/
└── core/                             ├── {id}.py
                                      └── {id}.json
```

发现与加载的约定锚点：

| 约定 | 机制 | 代码位置 |
|------|------|----------|
| 目录即方案 | `schemes/*/config.yaml` glob 扫描 | `scheduler/discovery.py::discover_schemes` |
| `scheme_id==目录名` | 加载时强制校验 | `scheduler/discovery.py::load_scheme_config` |
| 显式分派 | `runtime_type` 选择 Native import 或 Blackbox CLI | `scheduler/executor.py::run_configured_scheme` |
| Native 入口 | `importlib.import_module("schemes.{id}.predict").run` | `scheduler/scheme_runner.py::run_scheme` |
| Blackbox 入口 | 隔离执行 delivery 脚本的 `predict/backtest` CLI | `shared/blackbox_v2/` 与 runner |
| 统一输出 | `list[PredictionRecord]` → JSON | `scheduler/scheme_runner.py` |
| 统一写库 | `create_scheme_run` 建立 running 审计行；最终写入按 runtime/operation 进入 `complete_active_native_run` / `complete_approved_blackbox_run` / `complete_gray_gap_run` 原子完成 API | `scheduler/executor.py` + `harness/signal_gap_fill.py` + `scheduler/repository.py` |

因此“用户给新方案”的代码落点是 Blackbox Intake 原样保存两文件并生成平台配置，再依次执行完整持久化回测和激活；Blackbox 不进入 Native Gate 编排。不得手工创建新的 Native `predict.py + core/` 目录；Native StaticGate 与 ActivationGate 会拒绝政策清单外身份。

---

## 7. 横切关注点

| 关注点 | 现状 | 目标设计 |
|--------|------|----------|
| **DB 引擎生命周期** | 各 adapter/backtest 各自 `create_sqlalchemy_engine()` 再 `engine.dispose()` | adapter 经 `calendar_service`/`input_artifacts` 间接使用统一引擎工厂，不得裸取连接 |
| **配置** | `shared/db_config.py` 读环境变量；`config.yaml` 方案级 | Native 契约与 Blackbox Runtime Profile 分开维护，共享身份由 `SCHEME_CONTRACT.md` 约束 |
| **执行预算** | Native 使用 `config.yaml.schedule.timeout_sec`；Blackbox predict 取方案申请、Runtime Profile 上限和显式 operation deadline 的最小值，backtest 使用独立 Profile 预算 | operation deadline 只能缩短 Blackbox 方案/Profile 预算；所有预算仅控制子进程等待，不进入 L2 core 语义 |
| **产物路径** | W4 Native scheduled/gap-fill/DryRun 输入使用作业级临时根并在结束后清理；Blackbox 显式派生状态与 DataBridge ready snapshot 各自受控；回测使用 `backtest_artifacts/backtests/{benchmark_id}/` | 不再积累 `runtime_inputs` 或灰度二次快照目录；Harness 不创建方案级报告目录 |
| **进程/依赖隔离** | Native `forecast_env`、Blackbox Runtime Profile、服务 `bond_factor_lab_service`；子进程 + JSON | Runtime Profile 是 Blackbox 环境、资源和权限的唯一配置源 |
| **错误处理** | executor 捕获子进程失败写 `run_log(status=failed)` | harness Gate 失败安全（异常→`GateResult(FAILED)`），不抛穿 |
| **命名标识符** | `scheme_id`(方案) / `benchmark_id`(基准批次) / `data_source`(口径) 三者分离 | 维持；StaticGate 校验命名规范子集 |
| **写库安全** | 所有 live prediction insert-only，四字段唯一键拒绝覆盖；仅普通 Native/Blackbox active completion 的完整重复记 benign `skipped`、部分冲突整批失败 | repository 单事务 + harness 授权边界 |

旧 Native Phase-A generation/prune 实现已退役，不是当前缓存管理接口；保留的 Blackbox 状态由
exact version、输入身份、完整性和方案独占约束，标准 Result 校验后原子发布，失败不自动 fallback。

Authorized gray-gap 使用更严格的例外语义：任一授权业务键已经存在即整组拒绝并保持 `records_written=0`，未存在的键也不写入，且不得转为 benign `skipped`。

上述 insert-only 约束仅适用于 `t_scheme_predictions` 的 `gray_live` / `scheduled_live` 发布；actual 与 input artifact 等既有 UPSERT 路径仍按各自契约保持合法，不得将本规则扩张为全库禁用 UPSERT。

### 7.1 数据库迁移的库层与 operator 边界

`migrations.runner` 是迁移行为的唯一实现，并且只接受 caller-supplied `Engine`：
manifest 校验、schema inspect、pending apply 与 `APPLYING` recovery 都在这里实现。
它不读取环境变量、不解析 CLI 参数，也不决定某个连接是否有生产写权限。
`scripts/apply_migrations.py` 是唯一受控运维包装器：它负责受限 CLI 参数、环境连接
与写目标身份围栏；scheduler、harness 和其他 scripts 不得复制 apply/recovery 行为。

所有 CLI 写路径（普通 `--apply`、017/018/019/021 recovery）都必须在建 Engine 前提供
`--expected-database-name` 与 `--expected-server-uuid`，再以首次数据库语句
`SELECT DATABASE(), @@server_uuid` 精确验证实际连接。`--inspect-applying-017` 与
`--inspect-applying-018`、`--inspect-applying-019`、`--inspect-applying-021` 是只读模式，不要求这两个参数。UUID 只能来自 inspect JSON
或受控只读 identity query；不得在仓库或运行手册中记录生产 UUID、DSN 或凭据。

Migration 021 把 `t_scheme_registry.owner` 收敛为 `VARCHAR(64) NOT NULL`。它的临时 96 行 authority 只服务
历史回填：DDL 前必须证明数据库 Registry 与 authority 双向闭集，已有非空 owner 与 authority 冲突时拒绝；
迁移完成后运行时不保留第二份 owner 映射。该加列为向前兼容变更，旧 release 可忽略，但任何新 Registry
写入都必须提供或保留合法 owner。若进程在隐式提交 DDL 期间中断，必须先用只读
`--inspect-applying-021` 取得状态摘要，再以数据库 identity 与该摘要围栏执行 `--recover-applying-021 --apply`；
缺列、精确 nullable partial 和完整终态以外的状态一律拒绝。

---

## 8. 模块清单

| 模块 | 层 | 职责 | 关键公共符号 |
|------|----|------|--------------|
| `shared/data_service.py` | L1 | 源表 → 日/周/月宽表 | `build_{daily,weekly,monthly}_output_from_db`、`create_sqlalchemy_engine` |
| `shared/input_artifacts.py` | L1 | 唯一输入工件入口 | `build_daily_input_artifact`、`build_weekly_input_artifact`、`InputArtifact` |
| `shared/calendar_service.py` | L1 | 唯一交易日历/周历 | `get_calendar`、`CalendarService` |
| `shared/models.py` | L1 | 公共数据模型 | `PredictionRecord`、`ActualRecord`、`WeeklyActualRecord` |
| `shared/{db_config,artifact_paths}.py` | L1 | 配置/路径 | `RUNTIME_INPUT_ROOT` 等 |
| `schemes/{id}/predict.py` | L2 | adapter | `SCHEME_ID`、`run` |
| `schemes/{id}/core/` | L2 | 纯算法 + legacy 归档 | 方案私有 |
| `schemes/{id}/delivery/` | L2 | Blackbox 原始两文件 | `predict/backtest` CLI、Metadata |
| `shared/blackbox_v2/` | L1/L3 边界 | Blackbox 合同、快照、执行和结果转换 | Contract 1.0 校验器 |
| `scheduler/discovery.py` | L3 | 约定发现 + 契约加载 | `discover_schemes`、`load_scheme_config`、`SchemeConfig` |
| `scheduler/scheme_runner.py` | L3 | 只读 dry-run（importlib 运行方案） | `run_scheme` |
| `scheduler/executor.py` | L3 | conda 子进程执行 + 写库编排 | `execute_scheme`、`run_scheme_subprocess`、`SchemeRunResult` |
| `scheduler/repository.py` | L3 | 写库单点；按 runtime/operation 原子提交 prediction + run + log | `create_scheme_run`、`complete_active_native_run`、`complete_approved_blackbox_run`、`complete_gray_gap_run`、`write_run_log`；`_insert_run_predictions_conn` 仅内部使用 |
| `scheduler/{launchd,systemd}_prediction_runner.py` | L3 | 双平台 one-shot active 方案编排 | `run`、`main` |
| `scheduler/{daily,weekly,monthly,period_average}_actuals_updater.py` | L3 | actuals 事实构建与写入 | `update_*_actuals` |
| `scheduler/actuals_runner.py` | L3 | 双平台 one-shot actuals 唯一入口 | `run_actuals_job`、`main` |
| `backend/main.py` `factor_lab_dashboard.py` `db.py` | L4 | 唯一 Dashboard 读模型 + 静态前端 serve；同一路径提供 V5 月度 summary 与单方案按月 detail | `/api/factor-lab/dashboard` |
| `backtests/{id}_reproduction.py` | L4 | 历史复现 | `run_<scheme>_reproduction` |
| `backtests/repository.py` | L4 | 回测写库单点 | `t_backtest_*` 写入 |
| `migrations/runner.py` | 运维库层 | 唯一 migration 行为实现；caller-supplied `Engine` | manifest、inspect、apply、recovery |
| `scripts/apply_migrations.py` | 受控 operator CLI | 唯一 migration 运维包装器与写目标身份围栏 | `--apply`、inspect/recover 017/018/019/021 |
| `tests/` | L4 | 单元/集成验证 | unittest |
| `harness/` | L5 | Gate / 编排 / 审计 | `python -m harness`、`GateResult` |
| `scripts/` | 工具 | 审计、发布构建和受控运维 | 一次性命令 |

---

## 9. 强约束如何被强制（onboarding StaticGate + repo-wide CI gate）

机器守护分为两个互补作用域，二者不可互相替代：

- **onboarding StaticGate**：以单个 `scheme_id` 为范围，检查方案身份、入口、
  Native core/predict、跨方案、输入与写库契约；它不会扫描 `shared/`、
  `scheduler/`、`backend/` 等全仓包。
- **repo-wide CI gate**：`harness.contracts.import_rules.repository_layer_import_violations`
  扫描 `shared/`、`schemes/`、`scheduler/`、`backend/`、`backtests/`、`harness/`
  中的生产 Python 文件；`tests/`、`outputs/` 和未纳入分层图的管理脚本不在扫描集。
  它报告精确路径、行号和 import，不以历史违规基线放行新旧逆向依赖。

| 架构规则 | 机器判定 |
|----------|----------|
| Native 白名单 | `native_adapter` ID 不在 `deploy/onboarding_policy_v1.json` → FAIL，ActivationGate 同样阻断 |
| Native core 零本仓库依赖（§3.2 ✗ⁱ） | onboarding StaticGate 扫 DB/I/O/写库规则；repo-wide gate 额外禁止 `core → shared` 和向上依赖 |
| 跨方案禁止（§3.2） | onboarding StaticGate 与 repo-wide gate 均按源文件 package 解析绝对/相对 import，再扫描 `schemes.<other>`；`from ..other.core` 不可绕过 |
| 生产层不得反向依赖一次性工具 | repo-wide gate 明确拒绝 `harness → scripts`；共用只读控制面能力必须下沉到 `scheduler/shared`，`scripts` 只能作为调用入口 |
| 写库单点（§3.3） | predict/core 命中 `insert_run_predictions`/`write_run_log`/`execute_scheme`/`INSERT…` → FAIL |
| 输入单点（§3.3） | predict 必须 import `shared.input_artifacts`；backtest runner 同 → 否则 FAIL |
| 运行时入口（§6） | Native 校验 `SCHEME_ID + run`；Blackbox 校验两文件、Metadata 与 CLI |
| 依赖只向下（§3.3） | repo-wide gate 扫描生产层 import；未列入 §3.1 的静态边 → FAIL |

任何不符合矩阵的逆向依赖都会直接令 CI 测试失败；不维护历史违规豁免。
