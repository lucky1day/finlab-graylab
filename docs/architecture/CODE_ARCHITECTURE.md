# 代码架构设计（Code Architecture）

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：平台开发和代码审计人员
**最后核验日期**：2026-08-07
**定位**：本仓库的代码架构主蓝图，定义分层模型、包依赖方向、运行时调用图和扩展边界。
**与既有文档的关系**:
- [ARCHITECTURE.md](ARCHITECTURE.md) = **系统架构**（部署、DB schema、API 契约、数据流）。
- 本文 = **代码架构**（包/模块/依赖方向/调用图/扩展点）。二者互补，不重叠。
- [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) 边界总纲 → [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) 共享方案契约 → [onboarding/README.md](../onboarding/README.md) 统一入库导航。本文把它们统一到一张依赖图上。
- [SOURCE_ALGORITHM_FIDELITY.md](SOURCE_ALGORITHM_FIDELITY.md) 是 source-backed 方案的源算法保真总纲；它约束 L2 core 与 L4 backtest runner 不得借平台适配改变原始算法逻辑。

> 本文同时记录设计约束与机器门禁的真实覆盖范围。历史 V1–V4 基于
> 2026-06-08 扫描；2026-07-24 新增的 repo-wide AST 扫描检出并推动清零
> V5–V6，当前扫描结果为零违规。

---

## 1. 架构风格

**约定式插件 + 分层 + 横切 harness**。

- **分层（Layered）**：自下而上 5 层，依赖只能向下，禁止向上与跨层回指。
- **插件（Plugin）**：方案通过 `schemes/{scheme_id}/config.yaml` 被发现，再按显式 `runtime_type` 分派；所有新身份只能由 Blackbox V2 Intake 创建。
- **横切（Cross-cutting）**：`harness/` 横切所有层，只读探测 + 编排 + 留证，不被任何层依赖；`python -m harness` 是统一机器入口。
- **环境隔离（Process isolation）**：Native 原生算法在 `forecast_env`，Blackbox V2 只按 `blackbox-v2-v1` Runtime Profile 选择环境和 sandbox，服务在 `bond_factor_lab_service`。运行驱动不同，统一输出均收敛到 `PredictionRecord`。

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

## 4. 现状依赖与违规

初次扫描（2026-06-08）发现的 4 处违规已经处理；2026-07-24 首次用
repo-wide gate 扫描全部生产层 Python 文件后，另发现 2 处此前 onboarding
StaticGate 覆盖不到的包级逆向依赖：

| # | 状态 | 违规边 | 位置 | 违反规则 | 处置（归属文档） |
|---|------|--------|------|----------|------------------|
| V1 | ✅ 已清零(S1) | 当时的旧周频 adapter 间跨方案 import `read_source_week_id_for_date` | 跨方案 import | §3.2 跨方案禁止 | S1：adapter 改用 `shared.calendar_service.week_id_for_date`；旧周频批次已退役，当前 active 周频 5Y/7Y 已按日历单点重新入库 |
| V2 | ✅ 已清零(S2) | 当时旧周频 10Y 方案 `core/weekly_data_service.py → shared.data_service`（含 `create_sqlalchemy_engine`） | core 连库 | §3.2 ✗ⁱ core 零 DB | S2：该文件已确认为死代码并删除（连同 `weekly_output_0529_columns.json` 与对应测试） |
| V3 | ✅ 已清零(S1) | 当时旧周频 adapter 直接取 `shared.data_service.create_sqlalchemy_engine` 传给日历查询 | adapter 直接取引擎传给日历查询 | §3.1 过渡期容忍，目标消除 | S1 已让日历查询走 `calendar_service`；当前 active 周频 5Y/7Y adapter 不直接取 DB engine |
| V4 | ✅ 已清零(S3) | `backtests/daily_0529_reproduction.py → shared.data_service.build_daily_output_from_db` | 回测绕过 `input_artifacts` 拼日频输入 | §3.3 输入单点 | S3：daily backtest runner 已改走 `build_daily_input_artifact` |
| V5 | ✅ 已清零 | `shared/blackbox_v2/contracts.py → harness.contracts.config_schema` | L1 依赖 L5 | §3.1 `shared` 无上行依赖 | 纯 schema 实现下沉至 `shared.scheme_config_schema`；harness 兼容模块只做从 L1 向上 re-export |
| V6 | ✅ 已清零 | `scheduler/discovery.py → harness.contracts.config_schema` | L3 依赖 L5 | §3.1 `scheduler` 不依赖 harness | `scheduler.discovery` 改为直接依赖 `shared.scheme_config_schema` |

V5–V6 没有建立基线豁免；修复后 repo-wide gate 的全仓扫描为零违规。
方案级输入、写库、Native core/predict 等细粒度约束仍由 onboarding StaticGate
持续检查。

---

## 5. 运行时调用图

### 5.1 预测路径（调度 / 手动触发）

launchd + plist 是真实生产调度控制面。任务是否挂载、触发时点、环境、重启和日志
均由 installed plist 与 `launchctl` 现场状态决定；常驻 `scheduler.main`/APScheduler 已从
仓库删除，专用 runner 只作为对应 plist 的一次性子进程实现。仓库已移除 disabled
`com.bond-factor-lab.scheduler` 模板；这不表示任何 installed plist 已被安装、停用、替换或
物理删除。后续不得仅新增 Python job 或直调入口就宣称进入生产调度。

当前目标入口由 launchd 的一次性 plist 触发：refresh、daily、weekly、monthly 和 actuals
各自只有一个 writer。常驻 APScheduler 与 ledger/occurrence/epoch runtime 闭包均已从仓库移除；
历史 migration/数据库对象只作为审计和受控 recovery 证据，不能被加入新的或过渡生产路径。backend health
与手动 direct trigger 不读取这些历史控制面，也不路由 daily recovery；daily-gray 与 v2-preflight 的
repo writer/template 已退役并移除。完整治理规则见[生产信号与调度治理](PRODUCTION_SCHEDULING_GOVERNANCE.md)。

```text
launchd installed plist（单一 cadence writer）
  → discovery.py 按 runtime_type 发现 active SchemeConfig
  → shared.input_artifacts 校验 artifact freshness 与 feature cutoff
  → scheduler.executor.execute_scheme(cfg, predict_date, prediction_phase)
       ├─ native_adapter: run_scheme_subprocess → schemes.{id}.predict.run
       ├─ blackbox_v2: build_blackbox_input_snapshot → sandbox CLI
       ├─ strict PredictionRecord/日期语义校验
       └─ scheduler.repository 的专用原子完成边界
            → t_scheme_runs + t_scheme_predictions + run log
```

历史 gap harness 只能经专项授权以 `gray_live` insert-only 修复；自然 launchd 触发才可以
写 `scheduled_live`。早期失败/跳过只写审计日志，不能伪装为成功完成。

入口（后端手动触发）：`backend.main POST /api/trigger/{scheme_id}` →
`scheduler.direct_prediction` 的精确准入/单方案闭包 → 同一 `execute_scheme`。

日期语义由 `shared.prediction_context` 和各频率 adapter 统一落地：日频实盘为 `predict_date=T+1, feature_date=T`；周频实盘先由 `predict_date` 反推上一交易日 `feature_date`，再映射 `feature_week_id`；月频 source-backed 方案若声明自然 15 号触发，则 `predict_date` 保留自然月 15 号，`feature_date` / `target_date` 分别取当前月/目标月 15 号及以前最近交易日。`scheduler.executor` 在日频 live 写库前再次校验 `predict_date/feature_date/target_date`，防止源表水位不足时算法复用旧 feature/target 覆盖旧 target 明细。常驻 scheduler 的 startup catch-up 与 cron 路径已删除：服务启动不会按 cron 推断或补跑错过的预测任务，`scheduled_live` 只由对应的一次性 launchd 自然时钟写入。`shared.calendar_service` 和 `scheduler.weekly_actuals_updater` 共享 `shared.week_calendar_normalizer`，只对源周历孤立 forward jump 做只读归一化，确保预测 target 与 weekly actuals 使用同一周历事实。所有前端月份归属、actual join 和 gray/backtest 分流仍以 `target_date` 为事实键。

`schedule.timeout_sec` 是 L3 调度执行层的运行预算配置，不是算法输入。它只控制
`scheduler.executor` 等待算法子进程的最长时间，用于慢速 source-backed 方案；不得让
adapter/core 根据该字段改变窗口、特征、fallback 或输出。Native 版本变化的激活遵循
Native SOP 的 Gate 与授权边界，不再存在需要维护的 frozen daily-gray policy。任何后续
LaunchAgent 切换都必须先复核 installed plist、`launchctl` 状态和对应日志，不能笼统以
“重启 scheduler”代替控制面验收。

日频、周频、月频 actuals 由独立
`com.bond-factor-lab.actuals` LaunchAgent 启动
`scheduler.actuals_runner` 一次性刷新；仓库不再保留常驻 scheduler 或 `--run-once actuals` CLI。
当前生产节奏为
`08:30/19:00/23:45`，其中夜间 `23:45` 用于承接上游 Wind 日频晚间导入；非交易日
daily/weekly actuals 刷新到上一交易日，monthly actuals 仍刷新到自然 run date，以同时
覆盖周末补刷和自然 15 号月度规则。常驻 APScheduler 不得再注册 `actuals:*` job。

### 5.2 入库 harness 路径（首次入库与 Native 后续维护）

```
python -m harness onboard {scheme_id} --stage all
  └─ harness.orchestrator.onboard(ctx, stage)            ← fail-fast + fail-closed
       ├─ StaticGate   → runtime-aware contracts（原生 AST / 黑盒两文件与 Metadata）
       ├─ InputGate    → shared.input_artifacts.build_*  (只读生成 artifact)
       ├─ UnitGate     → unittest（tests/*{scheme_id}*）
       ├─ DryRunGate   → scheduler.executor.run_scheme_subprocess + probes.table_guard(行数不变)
       ├─ CompareGate  → schemes/{id}/benchmarks original/current strict compare
       ├─ BacktestGate → backtests/{id}_reproduction(--no-persist)
       └─ ApiReadinessGate → paused registry row + latest backtest + public API 不泄漏
  Blackbox 首轮授权卡点：ShadowRegisterGate → version=shadow + registry=paused，不写业务表
  Native 首次授权卡点：BacktestGate(--persist) / LiveGate(execute_scheme) / activate  ← 需 token，否则 BLOCKED
  激活后验收：ApiGate(active-only public API 可见性)
```

上图的七段 `all` 是所有首次技术入库的固定路径；Native 的 source benchmark/CompareGate
只在这里作为保真硬证据，Blackbox Compare 也保持原有确定性与截止隔离检查。已入库 Native
修订仅在不同 prior Native version 的 passed `all + compare` 所属 StaticGate 已持久化
`static.business_identity`，且该快照与当前身份精确匹配时，才可走：

```text
static -> native-maintenance-admission -> input -> unit -> dry-run -> api-readiness
```

快照只保存 `scheme_id`、`runtime_type`、`horizon`、`task_type`、`frequency`、target tenors
与 composite Registry IDs，不保存代码、config 或 version hash。maintenance 的 current exact
`t_scheme_versions` 行必须为 `runtime_type='native_adapter'` 且 status 为 `draft|active`；expected
Registry identity 可在预激活时统一为 `paused`，或在激活后统一为 `active`，但 draft version 配 active
Registry 必须 fail-closed。只有 ActivationGate 可在严格 discovery、精确版本与一次性授权核验后原子
建立 active 状态。缺少 prior snapshot 的 legacy admission 仍必须 fail-closed；唯一已实现例外是
`weekly_10y_d_overlay_0529` 的专用 `native-legacy-admission-attest`，只接受 maintenance 选定、唯一
passed `all + compare` 的 prior，且 StaticGate 已通过但 identity 字段明确缺失。短期一次性 token 必须
绑定 issuer 与 exact prior version/run；receipt 仅写两张 Harness 控制面表、无当前 hash、不改历史，且只作
`legacy_operator_attestation_v1` 身份来源。它不是通用命令或 waiver，不能自动生成、推断、激活或写业务表。
该六段路径不运行当前 historical `compare/backtest`、不写业务表，且不适用于 Blackbox；其后
activation 仍要核验当前精确 version、六个 Gate 与一次性 token。反之，current exact version 的
完整 `all` 通过时，ActivationGate 走互斥的 `full_initial_onboarding_v1`，不要求此 prior snapshot
或六段路径。

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
       └─ canonical 选择 → compact V1 response → gzip/identity 表示

数据库或构建失败时 route 直接返回 `503 dashboard_data_unavailable`；不保留进程内
last-known-good 数据，也不对前端返回 stale 快照。

本机兼容/回滚路径：
前端 legacy fallback → backend.main GET /api/metrics/{scheme_id}
  └─ backend.services.scheme_metrics(engine, ...)
       └─ JOIN t_scheme_predictions × t_scheme_actuals|t_scheme_weekly_actuals → 月度准确率
```

dashboard snapshot 是 L4 只读展示优化：不提供算法输入、不写库、不修改任何方案 core，
因此不改变 §3.3 的输入单点、写库单点、Native core 纯净和源算法保真四条不变量。
公网正常路径只允许一个 dashboard GET；legacy 路由保留在本机用于 rollout 和回滚，
是否公网放行由精确 Nginx 策略控制。

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

发现与加载的约定锚点（全部已存在，无需改框架）：

| 约定 | 机制 | 代码位置 |
|------|------|----------|
| 目录即方案 | `schemes/*/config.yaml` glob 扫描 | `scheduler/discovery.py::discover_schemes` |
| `scheme_id==目录名` | 加载时强制校验 | `scheduler/discovery.py::load_scheme_config` |
| 显式分派 | `runtime_type` 选择 Native import 或 Blackbox CLI | `scheduler/scheme_runner.py::run_configured_scheme` |
| Native 入口 | `importlib.import_module("schemes.{id}.predict").run` | `scheduler/scheme_runner.py::run_scheme` |
| Blackbox 入口 | 隔离执行 delivery 脚本的 `predict/backtest` CLI | `shared/blackbox_v2/` 与 runner |
| 统一输出 | `list[PredictionRecord]` → JSON | `scheduler/scheme_runner.py` |
| 统一写库 | `create_scheme_run` 建立 running 审计行；最终写入按 runtime/operation 进入 `complete_active_native_run` / `complete_approved_blackbox_run` / `complete_gray_gap_run` 原子完成 API | `scheduler/executor.py` + `harness/gates/signal_gap_fill_gate.py` + `scheduler/repository.py` |

因此“用户给新方案”的代码落点是 Blackbox Intake 原样保存两文件并生成平台配置，再由 harness 按运行时驱动 Gate。不得手工创建新的 Native `predict.py + core/` 目录；Native StaticGate 与 ActivationGate 会拒绝政策清单外身份。

---

## 7. 横切关注点

| 关注点 | 现状 | 目标设计 |
|--------|------|----------|
| **DB 引擎生命周期** | 各 adapter/backtest 各自 `create_sqlalchemy_engine()` 再 `engine.dispose()` | 引擎工厂收敛：adapter 经 `calendar_service`/`input_artifacts` 间接用引擎，不再裸取（消除 V3） |
| **配置** | `shared/db_config.py` 读环境变量；`config.yaml` 方案级 | Native 契约与 Blackbox Runtime Profile 分开维护，共享身份由 `SCHEME_CONTRACT.md` 约束 |
| **执行预算** | executor 有全局默认 timeout，`config.yaml.schedule.timeout_sec` 可按方案覆盖 | 维持；仅控制算法子进程等待时间，不进入 L2 core 语义 |
| **产物路径** | `shared/artifact_paths.py` 统一 `RUNTIME_INPUT_ROOT`；运行期 `backtest_artifacts/runtime_inputs/{scheme_id}/`，回测 `backtest_artifacts/backtests/{benchmark_id}/` | 维持；harness 报告 `reports/harness/{scheme_id}/{ts}/` |
| **进程/依赖隔离** | Native `forecast_env`、Blackbox Runtime Profile、服务 `bond_factor_lab_service`；子进程 + JSON | Runtime Profile 是 Blackbox 环境、资源和权限的唯一配置源 |
| **错误处理** | executor 捕获子进程失败写 `run_log(status=failed)` | harness Gate 失败安全（异常→`GateResult(FAILED)`），不抛穿 |
| **命名标识符** | `scheme_id`(方案) / `benchmark_id`(基准批次) / `data_source`(口径) 三者分离 | 维持；StaticGate 校验命名规范子集 |
| **写库安全** | UPSERT 幂等；唯一键隔离 scheme | harness `table_guard` 行数保护 + 授权 token |

### 7.1 数据库迁移的库层与 operator 边界

`migrations.runner` 是迁移行为的唯一实现，并且只接受 caller-supplied `Engine`：
manifest 校验、schema inspect、pending apply 与 `APPLYING` recovery 都在这里实现。
它不读取环境变量、不解析 CLI 参数，也不决定某个连接是否有生产写权限。
`scripts/apply_migrations.py` 是唯一受控运维包装器：它负责受限 CLI 参数、环境连接
与写目标身份围栏；scheduler、harness 和其他 scripts 不得复制 apply/recovery 行为。

所有 CLI 写路径（普通 `--apply`、017/018/019 recovery）都必须在建 Engine 前提供
`--expected-database-name` 与 `--expected-server-uuid`，再以首次数据库语句
`SELECT DATABASE(), @@server_uuid` 精确验证实际连接。`--inspect-applying-017` 与
`--inspect-applying-018`、`--inspect-applying-019` 是只读模式，不要求这两个参数。UUID 只能来自 inspect JSON
或受控只读 identity query；不得在仓库或运行手册中记录生产 UUID、DSN 或凭据。

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
| `scheduler/repository.py` | L3 | 写库单点；按 runtime/operation 原子提交 prediction + run + log | `create_scheme_run`、`complete_active_native_run`、`complete_approved_blackbox_run`、`complete_gray_gap_run`、`write_run_log`、`sync_scheme_registry`；`_insert_run_predictions_conn` 仅内部使用 |
| `scheduler/{daily,weekly,monthly}_actuals_updater.py` | L3 | actuals 刷新 | `update_*_actuals` |
| `scheduler/actuals_runner.py` | L3 | launchd one-shot actuals 刷新 | `run_actuals_job`、`main` |
| `scheduler/direct_prediction.py` | L3 | backend 手动单方案的精确准入与执行闭包；不含 APScheduler、cron、daily recovery、Registry sync 或 DataBridge publish。没有显式 manual 实盘阶段时，API 必须在入队前 fail-closed，不能伪装为自然 `scheduled_live` | `run_prediction_job` |
| `backend/main.py` `services.py` `db.py` | L4 | 只读 API + 静态前端 serve | `/api/*`、`scheme_metrics` |
| `tests/isolated_mysql.py` | 测试支持 | migration 回归专用的隔离 MySQL 生命周期；不读取生产 env，不应用生产 migration | `isolated_replay_mysql`、`IsolatedReplayMySQL.create_replay_database` |
| `backtests/{id}_reproduction.py` | L4 | 历史复现 | `run_<scheme>_reproduction` |
| `backtests/repository.py` | L4 | 回测写库单点 | `t_backtest_*` 写入 |
| `migrations/runner.py` | 运维库层 | 唯一 migration 行为实现；caller-supplied `Engine` | manifest、inspect、apply、recovery |
| `scripts/apply_migrations.py` | 受控 operator CLI | 唯一 migration 运维包装器与写目标身份围栏 | `--apply`、inspect/recover 017/018/019 |
| `tests/` | L4 | 单元/集成验证 | unittest |
| `harness/` | L5 | Gate / 编排 / 审计 | `python -m harness`、`GateResult` |
| `scripts/` | 工具 | 审计/对比/受控 admin | 一次性命令 |

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

repo-wide gate 曾精确阻断 §4 的 V5–V6；依赖下沉后全仓扫描为零违规。
后续任何同类逆向依赖都会直接令 CI 测试失败。

---

## 10. 演进路线

> 本节是方向；S0–S8 已完成，最新落地状态见 [CURRENT_STATUS.md](../CURRENT_STATUS.md)。

```
现状（onboarding harness 与 repo-wide gate 均已落地）
  │
  ① 数据层重构（已落地到 `shared.data_service` / `shared.input_artifacts` / `shared.calendar_service`）
  │    新建 calendar_service → 消 V1/V3；周频去重收编 → 消 V2；backtest 统一输入 → 消 V4
  ▼
依赖图全合规（repo-wide CI gate 全绿）
  │
  ② harness/ 持续演进（见 [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md)）
  │    onboarding StaticGate（方案局部）+ repo-wide gate（全仓 import 图）
  │    → 其余 Gate → 授权 → orchestrator/CLI
  ▼
强约束自动化入库（用户给方案 → harness 驱动改造-测试-验证-实盘）
```

> 本文为代码架构主蓝图。§4 的状态以 repo-wide gate 的实际扫描结果为准。
