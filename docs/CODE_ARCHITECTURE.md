# 代码架构设计（Code Architecture）

**更新日期**: 2026-06-09
**定位**: 本仓库的**代码架构主蓝图**。定义分层模型、包依赖方向规则、运行时调用图、扩展模型与横切关注点。是所有其它设计文档的总索引。
**与既有文档的关系**:
- [ARCHITECTURE.md](ARCHITECTURE.md) = **系统架构**（部署、DB schema、API 契约、数据流）。
- 本文 = **代码架构**（包/模块/依赖方向/调用图/扩展点）。二者互补，不重叠。
- [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) 边界总纲 → [HARNESS_DESIGN.md](HARNESS_DESIGN.md) harness 实现 → [DATA_LAYER_DESIGN.md](DATA_LAYER_DESIGN.md) 数据层 → [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) 方案契约。本文把它们统一到一张依赖图上。

> 本文为设计文档，不含实现代码。所示"现状违规"基于真实 import 扫描（2026-06-08），是数据层重构与 StaticGate 的目标。

---

## 1. 架构风格

**约定式插件 + 分层 + 横切 harness**。

- **分层（Layered）**：自下而上 5 层，依赖只能向下，禁止向上与跨层回指。
- **插件（Plugin）**：方案（scheme）是约定式插件——放进 `schemes/{scheme_id}/` 即被发现，新增方案零框架改动。
- **横切（Cross-cutting）**：`harness/` 横切所有层，只读探测 + 编排 + 留证，不被任何层依赖；当前已落地 27 个 Python 模块，`python -m harness` 可运行。
- **环境隔离（Process isolation）**：算法在 `forecast_env`，服务在 `bond_factor_lab_service`，通过 conda 子进程 + JSON stdout 解耦依赖。

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
│      predict.py (adapter)   ──静态调用──▶   core/ (纯算法 + legacy)      │
└──────────────────────────────────────────────────────────────────────┘
        │ adapter→shared.input_artifacts / calendar_service / models
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│  L1  统一公共层  shared/                                                │
│      data_service / input_artifacts / calendar_service                  │
│      models / db_config / artifact_paths                                │
└──────────────────────────────────────────────────────────────────────┘
        ▼
     MySQL bond_db（源表只读 / 写库表白名单）
```

每层一句话职责：

| 层 | 包 | 职责 | 对外稳定符号（节选） |
|----|----|------|----------------------|
| L1 | `shared/` | 唯一数据接入与公共模型 | `build_*_input_artifact`、`build_*_output_from_db`、`get_calendar`、`PredictionRecord` |
| L2 | `schemes/{id}/` | 算法（core）+ 平台适配（predict.py） | `run(predict_date)->list[PredictionRecord]`、`SCHEME_ID` |
| L3 | `scheduler/` | 发现、dry-run、写库、actuals、调度 | `discover_schemes`、`run_scheme`、`execute_scheme`、`upsert_predictions` |
| L4 | `backend/` `backtests/` `tests/` | 只读 API、历史复现、验证 | `/api/*`、`run_<scheme>_reproduction` |
| L5 | `harness/` | Gate 检查 / 编排 / 审计 | `python -m harness ...`、`GateResult` |

---

## 3. 包依赖方向规则（强约束核心）

这是"强约束 harness 工程"的骨架：**每条允许的 import 边都明确列出，未列出的即禁止**。StaticGate 据此机器判定（§9）。

### 3.1 允许的静态 import 边

```
shared/        → （无；仅依赖第三方库 pandas/sqlalchemy 与 shared 内部）
schemes/*/predict.py → shared.{input_artifacts, calendar_service, models}        [+ data_service 仅限引擎，过渡期]
schemes/*/core/      → （无本仓库依赖；仅 pandas/numpy/sklearn/lgbm）
scheduler/     → shared.{models, db_config, data_service}  + scheduler 内部
backend/       → scheduler.{repository, discovery, executor, main} + shared + backend 内部
backtests/     → shared.{input_artifacts, data_service, calendar_service}
                 + schemes/*/core（调用方案算法）+ backtests 内部
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
- 跨方案：`schemes/A` **禁止** import `schemes/B`（任何子模块）。

### 3.3 三条不可破坏的不变量

1. **依赖只向下**：上层可依赖下层，下层永不依赖上层（`shared` 不知道 `schemes` 存在；`schemes` 不知道 `scheduler` 存在）。
2. **写库单点**：只有 `scheduler.repository` / `backtests.repository` / `*_actuals_updater` 能写库；其余层零写库。
3. **输入单点**：算法输入只能经 `shared.input_artifacts` 产出；adapter / backtest runner 不得自拼 DB 输入。

---

## 4. 现状依赖与违规

初次扫描（2026-06-08）发现 4 处违规。随数据层重构推进，清零进度如下表「状态」列（最近更新 2026-06-08，S2 完成后）：

| # | 状态 | 违规边 | 位置 | 违反规则 | 处置（归属文档） |
|---|------|--------|------|----------|------------------|
| V1 | ✅ 已清零(S1) | `schemes/weekly_5y,7y/predict.py → schemes.weekly_10y_d_overlay.core.weekly_data_service` | 跨方案 import `read_source_week_id_for_date` | §3.2 跨方案禁止 | S1：adapter 改用 `shared.calendar_service.week_id_for_date` |
| V2 | ✅ 已清零(S2) | `schemes/weekly_10y/core/weekly_data_service.py → shared.data_service`（含 `create_sqlalchemy_engine`） | core 连库 | §3.2 ✗ⁱ core 零 DB | S2：该文件已确认为死代码并删除（连同 `weekly_output_0529_columns.json` 与对应测试） |
| V3 | ◑ 部分(S1) | `schemes/weekly_*/predict.py → shared.data_service.create_sqlalchemy_engine` | adapter 直接取引擎传给日历查询 | §3.1 过渡期容忍，目标消除 | S1 已让日历查询走 `calendar_service`；adapter 仍自建 engine 传入。完全消除待引擎工厂下沉（建议并入 S3 收尾或单列） |
| V4 | ✅ 已清零(S3) | `backtests/daily_0529_reproduction.py → shared.data_service.build_daily_output_from_db` | 回测绕过 `input_artifacts` 拼日频输入 | §3.3 输入单点 | S3：daily backtest runner 已改走 `build_daily_input_artifact` |

合规的关键边（已正确）：

- 所有 `predict.py` 都经 `shared.input_artifacts.build_*_input_artifact` 取输入 ✅
- `scheduler` 只依赖 `shared` + 自身，对具体方案零静态耦合 ✅
- `backtests` 通过 import `schemes/*/core/predictors` 调用算法，不碰 adapter ✅
- `shared` 无任何上行依赖 ✅

> 进度：V1（S1）、V2（S2）、V4（S3）已清零；V3 部分完成（日历查询已收敛到 `calendar_service`，adapter 仍自建 engine 传入——属白名单内 `adapter→shared` 边，不阻塞 StaticGate，engine 工厂下沉作为独立小重构后续处理）。数据层依赖白名单（§3.1）实质成立。每步均通过等价闸（`scripts/compare_refactor_outputs.py`，diff_count=0）验证行为保持。

---

## 5. 运行时调用图

### 5.1 预测路径（调度 / 手动触发）

```
APScheduler(scheduler.main)  ──cron──▶  run_prediction_job(scheme_id)
  └─ scheduler.executor.execute_scheme(cfg, predict_date)
       ├─ run_scheme_subprocess(scheme_id, predict_date, algo_env="forecast_env")
       │     └─[conda 子进程]─ python -m scheduler.scheme_runner --scheme-id --predict-date
       │           └─ importlib → schemes.{id}.predict.run(predict_date)        ← 运行时插件边
       │                 ├─ shared.input_artifacts.build_*_input_artifact(...)   ← L1 唯一输入
       │                 │     └─ shared.data_service.build_*_output_from_db()   ← 读源表
       │                 ├─ shared.calendar_service.get_calendar()               ← 日历查询
       │                 └─ schemes.{id}.core.*  (纯算法)                        ← 算法
       │           └─ print(JSON list[PredictionRecord])  → stdout
       ├─ records = parse(stdout)
       ├─ scheduler.repository.upsert_predictions(engine, records)  → t_scheme_predictions  ← 写库单点
       └─ scheduler.repository.write_run_log(...)                   → t_scheme_run_log
```

入口（后端手动触发）：`backend.main POST /api/trigger/{scheme_id}` → 同一 `execute_scheme`。

### 5.2 入库 harness 路径（已实现，自动化方案入库）

```
python -m harness onboard {scheme_id} --stage all
  └─ harness.orchestrator.onboard(ctx, stage)            ← fail-fast + fail-closed
       ├─ StaticGate   → contracts/* (纯 AST，复用 discovery.load_scheme_config)
       ├─ InputGate    → shared.input_artifacts.build_*  (只读生成 artifact)
       ├─ UnitGate     → unittest（tests/*{scheme_id}*）
       ├─ DryRunGate   → scheduler.executor.run_scheme_subprocess + probes.table_guard(行数不变)
       ├─ BacktestGate → backtests/{id}_reproduction(--no-persist)
       └─ ApiGate      → probes.api_probe(只读 /api)
  授权卡点：BacktestGate(--persist) / LiveGate(execute_scheme) / activate  ← 需 token，否则 BLOCKED
```

详见 [HARNESS_DESIGN.md](HARNESS_DESIGN.md)。

### 5.3 历史复现路径

```
python -m backtests.{scheme_id}_reproduction [--no-persist]
  ├─ shared.input_artifacts.build_*_input_artifact(...)         ← L1 唯一输入（V4 待统一）
  ├─ schemes.{id}.core.predictors.*  (逐历史点跑算法)
  ├─ 按 feature_date 归月生成月度指标
  └─ backtests.repository → t_backtest_runs / _predictions / _monthly_metrics   ← 回测写库单点
```

### 5.4 查询路径

```
前端 → backend.main GET /api/metrics/{scheme_id}
  └─ backend.services.scheme_metrics(engine, ...)
       └─ JOIN t_scheme_predictions × t_scheme_actuals|t_scheme_weekly_actuals → 月度准确率
```

---

## 6. 扩展模型（约定式插件）

新增一个方案，框架代码**零改动**——这是分层 + 约定发现的收益：

```
schemes/{scheme_id}/
├── __init__.py
├── config.yaml          # 被 scheduler.discovery 约定发现（glob "*/config.yaml"）
├── predict.py           # 暴露 SCHEME_ID + run(predict_date)；被 scheme_runner 运行时 importlib 加载
└── core/                # 纯算法，零本仓库依赖
```

发现与加载的约定锚点（全部已存在，无需改框架）：

| 约定 | 机制 | 代码位置 |
|------|------|----------|
| 目录即方案 | `schemes/*/config.yaml` glob 扫描 | `scheduler/discovery.py::discover_schemes` |
| `scheme_id==目录名` | 加载时强制校验 | `scheduler/discovery.py::load_scheme_config` |
| 统一入口 | `importlib.import_module("schemes.{id}.predict").run` | `scheduler/scheme_runner.py::run_scheme` |
| 统一输出 | `list[PredictionRecord]` → JSON | `scheduler/scheme_runner.py` |
| 统一写库 | `upsert_predictions` UPSERT | `scheduler/executor.py` + `repository.py` |

因此"用户给方案 → 自动入库"在代码层的落点是：把方案塞进 `schemes/`，再由 harness（L5）按 SOP 驱动 Gate，框架（L1–L3）一行不改。

---

## 7. 横切关注点

| 关注点 | 现状 | 目标设计 |
|--------|------|----------|
| **DB 引擎生命周期** | 各 adapter/backtest 各自 `create_sqlalchemy_engine()` 再 `engine.dispose()` | 引擎工厂收敛：adapter 经 `calendar_service`/`input_artifacts` 间接用引擎，不再裸取（消除 V3） |
| **配置** | `shared/db_config.py` 读环境变量；`config.yaml` 方案级 | 维持；`config.yaml` schema 由 `SCHEME_CONTRACT.md` 形式化 |
| **产物路径** | `shared/artifact_paths.py` 统一 `RUNTIME_INPUT_ROOT`；运行期 `backtest_artifacts/runtime_inputs/{scheme_id}/`，回测 `backtest_artifacts/backtests/{benchmark_id}/` | 维持；harness 报告 `reports/harness/{scheme_id}/{ts}/` |
| **进程/依赖隔离** | conda：算法 `forecast_env`、服务 `bond_factor_lab_service`；子进程 + JSON stdout | 维持；这是 scheduler 与算法依赖解耦的关键边界 |
| **错误处理** | executor 捕获子进程失败写 `run_log(status=failed)` | harness Gate 失败安全（异常→`GateResult(FAILED)`），不抛穿 |
| **命名标识符** | `scheme_id`(方案) / `benchmark_id`(基准批次) / `data_source`(口径) 三者分离 | 维持；StaticGate 校验命名规范子集 |
| **写库安全** | UPSERT 幂等；唯一键隔离 scheme | harness `table_guard` 行数保护 + 授权 token |

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
| `scheduler/discovery.py` | L3 | 约定发现 + 契约加载 | `discover_schemes`、`load_scheme_config`、`SchemeConfig` |
| `scheduler/scheme_runner.py` | L3 | 只读 dry-run（importlib 运行方案） | `run_scheme` |
| `scheduler/executor.py` | L3 | conda 子进程执行 + 写库编排 | `execute_scheme`、`run_scheme_subprocess`、`SchemeRunResult` |
| `scheduler/repository.py` | L3 | 写库单点 | `upsert_predictions`、`write_run_log`、`sync_scheme_registry` |
| `scheduler/{daily,weekly}_actuals_updater.py` | L3 | actuals 刷新 | `update_*_actuals` |
| `scheduler/main.py` | L3 | APScheduler 调度 | `build_scheduler` |
| `backend/main.py` `services.py` `db.py` | L4 | 只读 API + 静态前端 serve | `/api/*`、`scheme_metrics` |
| `backtests/{id}_reproduction.py` | L4 | 历史复现 | `run_<scheme>_reproduction` |
| `backtests/repository.py` | L4 | 回测写库单点 | `t_backtest_*` 写入 |
| `tests/` | L4 | 单元/集成验证 | unittest |
| `harness/` | L5 | Gate / 编排 / 审计 | `python -m harness`、`GateResult` |
| `scripts/` | 工具 | 审计/对比/受控 admin | 一次性命令 |

---

## 9. 强约束如何被强制（依赖规则 → StaticGate）

代码架构的每条规则都映射到一条可机器执行的 StaticGate 判定（见 [HARNESS_DESIGN.md](HARNESS_DESIGN.md) §6 / [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) §4）：

| 架构规则 | StaticGate 判定 |
|----------|-----------------|
| core 零本仓库依赖（§3.2 ✗ⁱ） | AST 扫 `core/*.py` 命中 `sqlalchemy`/`scheduler`/`shared.input_artifacts`/`read_sql`/`text(` → FAIL |
| 跨方案禁止（§3.2） | AST 扫 `from schemes.<other>` → FAIL |
| 写库单点（§3.3） | predict/core 命中 `upsert_predictions`/`write_run_log`/`INSERT…` → FAIL |
| 输入单点（§3.3） | predict 必须 import `shared.input_artifacts`；backtest runner 同 → 否则 FAIL |
| 统一入口（§6） | `SCHEME_ID==目录名` + `def run(predict_date)` 单参 |
| 依赖只向下（§3.3） | 扫描 import 边不在 §3.1 白名单 → FAIL |

→ "强约束"不是文档口号，而是一组在 CI 可执行的 import-direction 断言。当前 4 处违规（§4 V1–V4）在数据层重构后清零，StaticGate 持续守护防回潮。

---

## 10. 演进路线

> 本节是方向；可执行、可验收、可回滚的分阶段执行计划（S0–S8 已完成，归档）见 [archive/ARCH_EXECUTION_PLAN.md](archive/ARCH_EXECUTION_PLAN.md)。最新落地状态见 [CURRENT_STATUS.md](CURRENT_STATUS.md)。

```
现状（依赖违规已清零，harness 已落地）
  │
  ① 数据层重构（DATA_LAYER_DESIGN.md）
  │    新建 calendar_service → 消 V1/V3；周频去重收编 → 消 V2；backtest 统一输入 → 消 V4
  ▼
依赖图全合规（§3.1 白名单 100% 成立）
  │
  ② harness/ 持续演进（HARNESS_DESIGN.md）
  │    contracts + StaticGate（守护依赖规则）→ 其余 Gate → 授权 → orchestrator/CLI
  ▼
强约束自动化入库（用户给方案 → harness 驱动改造-测试-验证-实盘）
```

> 本文为代码架构主蓝图。未创建或修改任何代码；§4 违规与 §10 演进为后续实现阶段的目标。
