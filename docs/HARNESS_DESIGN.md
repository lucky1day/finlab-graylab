# Harness 实现级设计（可执行强约束）

**更新日期**: 2026-06-08
**定位**: 本文把 [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) §3「未来 Harness Gate」从"形态描述"升级为**实现级设计骨架**——目录、契约、Gate 接口、机器判定规则、CLI、授权机制。
**边界**: 本文是设计蓝图，不含实现代码。`harness/` 的 `.py` 留到实现阶段按本文落地。

> 总纲（边界、DB 安全边界、验收证据）以 `HARNESS_ARCHITECTURE.md` 为权威；执行手册以 [SCHEME_ONBOARDING_SOP.md](sop/SCHEME_ONBOARDING_SOP.md) 为权威；方案契约以 [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) 为权威。本文只负责"harness 怎么实现"。

---

## 1. 设计目标

把现有"人工敲 conda 命令 + 散落脚本"的 Gate，升级为**可机器执行、fail-fast、可审计**的统一 harness：

```
python -m harness onboard t1_daily --predict-date 2026-06-06 --stage all
```

一条命令按 SOP 顺序串联所有自动段 Gate，任一失败即停，逐步落审计证据到 `reports/harness/`。带副作用的动作（写库、激活）必须显式授权，否则 BLOCKED。

设计三原则：

- **只检查/编排/留证**：harness 不承载算法、不定义数据口径、不替代 `scheduler` 做正式调度。
- **复用而非重造**：每个 Gate 都委托一个已存在的入口（`discovery` / `scheme_runner` / `executor` / `backtests runner`），harness 只做"调用 + 断言 + 取证"。
- **fail-closed**：副作用动作无授权一律 BLOCKED，绝不默认放行。

---

## 2. 目录结构与每文件职责

```text
harness/
├── __init__.py
├── __main__.py            # `python -m harness ...` 入口，委托 cli.main()
├── cli.py                 # argparse 子命令：gate / onboard / activate / report
├── result.py             # 统一契约：GateStatus / Evidence / GateResult / OnboardReport
├── context.py            # GateContext：scheme_id/predict_date/config/report_dir/engine_factory/authorization
├── registry.py           # gate 注册表 + stage→gate 顺序映射
├── orchestrator.py       # 按 stage 顺序串联 gate，fail-fast，聚合 OnboardReport
├── authorization.py      # Authorization 数据结构 + verify()，副作用卡点
├── gates/
│   ├── __init__.py
│   ├── base.py           # Gate 抽象基类：name / requires_authorization / run(ctx)->GateResult
│   ├── static_gate.py    # 目录/命名/接口/危险导入/config schema（纯 AST+FS，零副作用）
│   ├── input_gate.py     # 调 shared.input_artifacts，校验列/覆盖/source/quality
│   ├── unit_gate.py      # 触发 tests/ 中该 scheme 的用例
│   ├── dry_run_gate.py   # 调 scheduler.scheme_runner，断言写库表行数不变
│   ├── backtest_gate.py  # 调 backtests/{id}_reproduction，--no-persist 自动 / persist 需授权
│   ├── live_gate.py      # 授权后单方案写库 + 行数保护
│   └── api_gate.py       # /api 只读探针（矩阵格子不消失）
├── contracts/
│   ├── __init__.py
│   ├── config_schema.py  # config.yaml 完整 schema 校验（见 SCHEME_CONTRACT.md）
│   ├── predict_contract.py # predict.py AST 校验：SCHEME_ID / run 签名 / 不 import 写库
│   └── import_rules.py   # 危险 import / 写库调用 / SQL 写关键字 规则表
├── probes/
│   ├── __init__.py
│   ├── table_guard.py    # 受保护表行数快照 before/after diff
│   └── api_probe.py      # 只读 HTTP 探针
└── report/
    ├── __init__.py
    └── writer.py         # 写 JSON + Markdown 到 reports/harness/{scheme_id}/{run_ts}/
```

审计证据统一落 `reports/harness/{scheme_id}/{run_ts}/`，与现有 `reports/` 约定一致（当前 `reports/` 已存放周频对比报告）。

---

## 3. 统一结果契约

所有 Gate 返回同一个 `GateResult`，orchestrator 聚合成 `OnboardReport`。字段级伪代码（实现阶段用 `@dataclass(frozen=True)`）：

```python
# harness/result.py
class GateStatus(str, Enum):
    PASSED  = "passed"
    FAILED  = "failed"
    SKIPPED = "skipped"     # 该方案不适用此 gate（如纯 live 方案无 backtest）
    BLOCKED = "blocked"     # 需要人工授权但未授权 → orchestrator 停止

@dataclass(frozen=True)
class Evidence:
    key: str                # 如 "input_row_count" / "predictions_table_delta"
    value: Any
    detail: str | None = None

@dataclass(frozen=True)
class GateResult:
    gate_name: str
    status: GateStatus
    passed: bool            # == (status is PASSED)；便于布尔短路
    evidence: list[Evidence]
    errors: list[str]
    started_at: str         # ISO8601 UTC
    finished_at: str
    report_path: Path | None = None

@dataclass(frozen=True)
class OnboardReport:
    scheme_id: str
    predict_date: str
    stage_requested: str
    results: list[GateResult]
    overall_passed: bool
    report_dir: Path
```

约定：每个 `Gate.run()` **失败安全**——内部异常被捕获并转成 `GateResult(status=FAILED, errors=[...])`，绝不向 orchestrator 抛穿。

---

## 4. 执行上下文与 Gate 基类

```python
# harness/context.py
@dataclass
class GateContext:
    scheme_id: str
    predict_date: str
    config: SchemeConfig                       # 复用 scheduler.discovery.SchemeConfig
    project_root: Path
    report_dir: Path                           # reports/harness/{scheme_id}/{run_ts}/
    algo_env: str = "forecast_env"
    engine_factory: Callable[[], Engine] | None = None   # 只读引擎工厂；InputGate/DryRunGate/probes 用
    authorization: "Authorization | None" = None
    persist_backtest: bool = False             # BacktestGate 是否落 t_backtest_*

# harness/gates/base.py
class Gate(ABC):
    name: str
    requires_authorization: bool = False       # live_gate / backtest-persist = True
    @abstractmethod
    def run(self, ctx: GateContext) -> GateResult: ...
```

`config` 由 `scheduler.discovery.load_scheme_config()` 加载（已强制 `scheme_id == 目录名`、`tenors` 非空），harness 不重复实现发现逻辑。

---

## 5. 各 Gate 的输入/输出契约

| Gate | 委托的现有入口 | 关键检查 | evidence 键（示例） | 授权 |
|------|----------------|----------|----------------------|------|
| `StaticGate` | 纯 AST + FS（见 §6） | 目录/命名/接口/危险导入/config schema | `dir_name_ok`, `run_signature_ok`, `scheme_id_const_ok`, `dangerous_imports`, `config_schema_errors` | 否 |
| `InputGate` | `shared.input_artifacts.build_*_input_artifact` | 生成 artifact，校验 required_columns、coverage、source、quality | `input_artifact_path`, `frequency`, `row_count`, `coverage`, `null_ratio`, `missing_required_cols` | 否 |
| `UnitGate` | `python -m unittest`（按 scheme 选择器） | 跑 `tests/*{scheme_id}*` | `tests_run`, `tests_passed`, `failures` | 否 |
| `DryRunGate` | `scheduler.executor.run_scheme_subprocess`（conda 子进程→`scheme_runner`） | JSON 输出 + 字段契约 + 写库表行数不变 | `prediction_count`, `predictions_table_delta=0`, `run_log_delta=0`, `sample_record` | 否 |
| `BacktestGate` | `backtests/{scheme_id}_reproduction.py` | `--no-persist` summary；persist 需授权 | `sample_count`, `accuracy`, `monthly_distribution`, `persisted` | persist 时是 |
| `LiveGate` | `scheduler.executor.execute_scheme`（单方案） | 授权后写库 + 行数保护 | `written_rows`, `protected_tables_delta`, `authorized_scheme` | 是 |
| `ApiGate` | `probes/api_probe`（只读 HTTP） | `/api/metrics/{id}` 或 `/api/backtests/factor-lab` 可读、格子存在 | `http_status`, `matrix_cell_present`, `monthly_rows` | 否 |

DryRunGate 是关键的"只读运行"边界：它在 conda 算法环境跑真实算法但**断言正式表零增量**——通过 `probes/table_guard` 在子进程前后快照 `t_scheme_predictions` / `t_scheme_run_log` 行数，delta 必须为 0。

DryRunGate 同时承担 `PredictionRecord` 运行期契约校验（见 [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) §3）：每条记录 `scheme_id/horizon/target_tenor` 与 config 一致、`extra` 含必填键。

---

## 6. StaticGate 机器判定规则清单

StaticGate **不运行算法、不连库**，全部基于文件系统 + `ast` 解析。每条规则产出一个 evidence；任一 FAIL → gate FAIL。

```python
# harness/contracts/import_rules.py
DANGEROUS_CORE_IMPORTS = {        # core/*.py 命中即 FAIL
    "sqlalchemy", "pymysql",
    "scheduler", "shared.input_artifacts",
}
WRITE_CALL_NAMES = {              # 任意 core/predict 命中即 FAIL
    "create_engine", "create_sqlalchemy_engine", "read_sql",
    "upsert_predictions", "write_run_log", "execute_scheme",
}
SQL_WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP")   # 字符串字面量扫描
```

判定规则：

1. **目录名一致**：`schemes/{scheme_id}/` 存在；委托 `discovery.load_scheme_config` 校验 `config.scheme_id == 目录名`（已内建）。
2. **必需文件**：`config.yaml`、`predict.py`、`__init__.py`、`core/__init__.py` 存在。
3. **predict 接口**：AST 解析 `predict.py`——存在模块级常量 `SCHEME_ID == scheme_id`；存在顶层 `def run(predict_date)` 且单位置参（见 `contracts/predict_contract.py`）。
4. **core 零 DB**：遍历**活跃** `core/*.py`（文件名不含 `legacy`）的 import 与调用名，命中 `DANGEROUS_CORE_IMPORTS` / `WRITE_CALL_NAMES` / `text(` → FAIL。
5. **legacy 隔离（warning 级，不硬失败）**：`core/legacy_*.py` 允许保留旧实现。活跃模块 import legacy 模块时——若被 import 的 legacy 模块**本身零 DB / 零写库 / 零跨方案**——记为 warning（evidence 标注，不判 FAIL），因为这是受控的算法复用；仅当 legacy 模块含 DB/写库且被活跃模块 import 时才升级为 FAIL。
   > 历史示例：曾有周度方案 `core/predictors.py` import `legacy_*_0529`（零 DB，纯算法+CSV，运行时 monkey-patch 注入 DataFrame）属受控复用，记 warning 不阻断。相关周度方案现已退役/代码未实现，此豁免仅作规则说明保留。
6. **predict 不写库**：`predict.py` 命中 `WRITE_CALL_NAMES`、或 `import scheduler.repository/executor` → FAIL。
7. **强制公共输入入口**：`predict.py` 与 `backtests/{scheme_id}_*.py` 必须 import `shared.input_artifacts`；不得自建 `read_sql` 拼输入 → 否则 FAIL。
8. **config schema**：委托 `contracts/config_schema.py`（见 [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) §1）。
9. **命名规范**（可机器子集）：文件 snake_case；backtest runner 带 scheme_id 前缀。

> 历史背景：早先周度方案曾自带 `core/weekly_data_service.py` DB 访问，是 StaticGate 规则 4 的典型 FAIL 案例，也是 [DATA_LAYER_DESIGN.md](DATA_LAYER_DESIGN.md) 周频去重的目标。相关周度方案现已退役/代码未实现，此处仅作规则说明保留。

---

## 7. Orchestrator 与 CLI

### 7.1 stage 顺序

```python
# harness/registry.py
ALL_SEQUENCE = ["static", "input", "unit", "dry-run", "backtest", "api"]
# live / activate 不在 all 内，必须显式调用且需授权
```

### 7.2 编排逻辑（fail-fast + fail-closed）

```python
# harness/orchestrator.py
def onboard(ctx: GateContext, stage: str) -> OnboardReport:
    """按 SOP 顺序执行到 stage（含），fail-fast。"""
    results = []
    for gate in resolve_gates(sequence_up_to(stage)):
        if gate.requires_authorization and not authorized_for(ctx, gate):
            results.append(blocked(gate))     # 不执行，留证据，停止
            break
        res = gate.run(ctx)
        report.writer.write(ctx, res)          # 每步落盘，可审计
        results.append(res)
        if not res.passed:
            break                              # fail-fast
    return OnboardReport(..., overall_passed=all(r.passed for r in results))
```

### 7.3 CLI 命令

```bash
# 单 gate
python -m harness gate static   --scheme-id t1_daily
python -m harness gate input    --scheme-id t1_daily --predict-date 2026-06-06
python -m harness gate dry-run  --scheme-id t1_daily --predict-date 2026-06-06

# 串联到某 stage（fail-fast）
python -m harness onboard t1_daily --predict-date 2026-06-06 --stage all

# 授权卡点（写库 / 激活）
python -m harness gate backtest --scheme-id t1_daily --persist --authorize <TOKEN>
python -m harness gate live     --scheme-id t1_daily --predict-date 2026-06-06 --authorize <TOKEN>
python -m harness activate      --scheme-id t1_daily --authorize <TOKEN>

# 报告
python -m harness report t1_daily --latest
```

退出码：全 PASS=`0`；任一 FAIL=`1`；BLOCKED（缺授权）=`2`。便于 CI / 自动化判定。

---

## 8. 复用现有组件映射

harness 不重造任何执行逻辑，每个 Gate 委托一个已存在的入口：

| harness 组件 | 复用的现有代码（已核对存在） |
|---|---|
| 契约加载 / scheme_id==dir 校验 | `scheduler/discovery.py::load_scheme_config` |
| dry-run 只读执行 | `scheduler/scheme_runner.py::run_scheme` + `scheduler/executor.py::run_scheme_subprocess`（conda 子进程） |
| 输入工件 | `shared/input_artifacts.py::build_daily_input_artifact / build_weekly_input_artifact` |
| 回测 | `backtests/{scheme_id}_reproduction.py`（如 t1/t5 日度）等 `--no-persist` |
| Live 写库 | `scheduler/executor.py::execute_scheme`（单方案）→ `repository.upsert_predictions` / `write_run_log` |
| readiness 前置 | `scripts/check_weekly_10y_readiness.py` 的逻辑收编为 `live_gate` 前置检查 |
| 表行数审计 | `scripts/run_framework_repro.py` / `audit_daily_data_service.py` 思路 → `probes/table_guard.py` |

---

## 9. 授权机制（副作用卡点）

```python
# harness/authorization.py
@dataclass(frozen=True)
class Authorization:
    scheme_id: str                    # 绑定单一 scheme
    action: str                       # "backtest_persist" | "live_write" | "activate"
    predict_date: str | None
    token: str                        # 一次性，来自 CLI --authorize
    issued_by: str
    issued_at: str

def verify(auth: Authorization, ctx: GateContext) -> bool:
    """校验：action 与 gate 匹配、scheme_id 与 ctx 匹配、token 未过期未复用。"""
```

强约束特性：

- **作用域单一**：一个 token 只对 `(scheme_id, action)` 有效，不能跨 scheme、不能升级到 `all`、不能复用。
- **fail-closed**：无 token → gate 返回 `BLOCKED`，orchestrator 停止，绝不默认放行。
- **行数保护**（`probes/table_guard.py`）：Live / Backtest-persist 写前对**全部受保护表**快照，写后 diff——只授权表 `delta > 0`、其余表 `delta == 0`，否则判 FAIL 并记录证据。受保护表清单 = `HARNESS_ARCHITECTURE.md §5` 的源表 + 写库表全集（`api_wind_*`、`t_trade_calendar`、`t_scheme_predictions`、`t_scheme_run_log`、`t_scheme_actuals`、`t_scheme_weekly_actuals`、`t_backtest_*`）。
- **审计**：每次授权写入 `reports/harness/{scheme_id}/{run_ts}/authorization.json`。

---

## 10. 自动化入库 pipeline 边界

```text
用户给新方案
  │  [Intake]    人工：填 config.yaml（见 SCHEME_CONTRACT.md §1）
  │  [Normalize] 人工：归档 legacy 到 core/legacy_*，core 改造成 DataFrame 函数，写 predict.py
  │  ── 以下 harness 驱动 ──
  ▼
 全自动段（无外部持久副作用）：
   StaticGate → InputGate → UnitGate → DryRunGate → BacktestGate(--no-persist) → ApiGate
  │
  ▼ ★授权卡点 1   BacktestGate --persist     → 写 t_backtest_*
  ▼ ★授权卡点 2   LiveGate                   → 写 t_scheme_predictions / t_scheme_run_log（单方案+行数保护）
  ▼ ★授权卡点 3   Activate                   → config status paused→active + 重启 scheduler
  │
  ▼ [Documentation] 半自动：harness 输出报告路径，人工更新 CURRENT_STATUS 等
```

- **可全自动**：Static / Input / Unit / Dry-run / Backtest(--no-persist) / Api。
- **必须人工授权**：Backtest persist、Live 写库、Activation——三者都有持久副作用，对应 `requires_authorization=True`。

这一边界正是用户"把方案交给 Claude → 自动改造-测试-验证-实盘"的落点：自动段无需人介入，副作用卡点逐个显式授权。

---

## 11. 建议实施排序（实现阶段）

1. **先做 [DATA_LAYER_DESIGN.md](DATA_LAYER_DESIGN.md) 的数据层重构**（周频去重 + `calendar_service` + 强化 `InputArtifact`）——这是 StaticGate 规则 4/7 与 InputGate 能稳定断言的前提。
2. `harness/contracts` + `harness/result` + `StaticGate`（纯静态、零依赖，立即能扫出现有违规）。
3. `InputGate` / `DryRunGate` / `BacktestGate`（复用现有 runner + `table_guard`）。
4. `authorization` + `LiveGate` + `table_guard`（授权卡点）。
5. `orchestrator` + `cli` + `report`（串联与审计）。

> 本文为设计蓝图。未创建或修改任何 `harness/` 代码。
