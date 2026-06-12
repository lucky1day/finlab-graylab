# 方案契约形式化规范（机器可校验）

**更新日期**: 2026-06-12
**定位**: 把散落在 [SCHEME_ONBOARDING_SOP.md](sop/SCHEME_ONBOARDING_SOP.md) §4/§5 的方案约束收敛成**单一权威契约**，供 harness 的 `StaticGate` / `DryRunGate` 机器校验。
**边界**: 本文是规范，不含校验器实现代码。校验逻辑由 `harness/contracts/*` 按本文落地，harness 边界见 [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md)。

> SOP 仍是人类执行手册；本文是机器契约。两者一致，本文更细、可判定。任何冲突以本文为准并回写 SOP。
> `predict_date` / `feature_date` / `target_date` / `prediction_phase` 的业务语义以 [PREDICTION_SEMANTICS.md](PREDICTION_SEMANTICS.md) 为准。

---

## 1. `config.yaml` Schema

每个 `schemes/{scheme_id}/config.yaml` 必须满足下表。`StaticGate` 调 `harness/contracts/config_schema.py::validate_config(raw, dirname)`，返回错误列表，空=通过。

| 字段 | 类型 | 必填 | 约束 |
|------|------|:----:|------|
| `scheme_id` | str | ✅ | `^[a-z][a-z0-9_]*$` 且 `== 目录名`（`scheduler.discovery` 已强制） |
| `name` | str | ✅ | 非空 |
| `description` | str | ✅ | 非空 |
| `horizon` | int | ✅ | `> 0`；日频 `1`/`5`，当前周频 `6` |
| `tenors` | list[str] | ✅ | 非空，⊆ 已注册 Y 标的 key（`3Y/5Y/7Y/10Y` ...） |
| `frequency` | str | ✅ | ∈ `{daily, weekly, monthly}` |
| `schedule.cron` | str | ✅ | 合法 5 段 cron |
| `schedule.timezone` | str | ➖ | 默认 `Asia/Shanghai`，合法时区 |
| `entry_point` | str | ➖ | 默认 `predict.run`，必须 `== predict.run` |
| `status` | str | ✅ | ∈ `{active, paused}`（新方案先 `paused`） |
| `input_spec.data_version` | str | ✅(新) | 对应主输入 `InputArtifact.data_version`，如 `shared_data_service_daily.v1` / `shared_data_service_weekly.v1` / `shared_data_service_monthly.v1`。**同时约束 live adapter 与 backtest runner**：两者产出的主 `InputArtifact.data_version` 必须等于本字段，保证历史回测与实盘预测同一数据口径（见 §7 与 [sop/SCHEME_ONBOARDING_T0.md](sop/SCHEME_ONBOARDING_T0.md)） |
| `input_spec.required_columns` | list[str] | ✅(新) | InputGate 据此校验列覆盖 |
| `input_spec.weekly_variant` | str | `frequency==weekly` 时✅(新) | 对应 `data_service.weekly_variant`，如 `unified` / `wind_export_0529` |
| `input_spec.auxiliary_inputs` | list[map] | ➖ | 辅助输入声明。每项为 `{frequency, data_version, required_columns}`；`frequency ∈ {daily, weekly, monthly}`，不得等于方案主 `frequency`，同一方案内不得重复。InputGate 对每项执行与主输入相同的 source / data_version / required_columns / coverage 校验 |
| `target_rule` | str | `frequency==weekly` 时✅(新) | 目标日语义（如 `next_week_last_trading_day_vs_current_week`） |
| `backtest.runner` | str | ➖(新) | `backtests/{scheme_id}_reproduction.py` 模块名；参与历史排行时必填 |
| `backtest.start_date` | str | `frequency in {daily, monthly}` 且参与回测时✅ | 必须等于 `2025-01-01`。含义是历史回测**预测发出起点**，即回测输出样本必须满足 `predict_date >= 2025-01-01`；训练、筛因子、模型更新和输入 artifact 可以使用更早历史数据 |
| `backtest.predict_start_date` | str | `frequency==weekly` 且参与回测时✅ | 必须等于 `2025-01-01`。周频 `start_week/end_week` 仍表示输入/训练周范围；输出样本必须按 `predict_date >= 2025-01-01` 过滤 |

> 标注「新」的字段是本设计**新增的必填项**——让 harness 无需读算法即可知道输入口径、列要求、目标语义。现有方案在数据层重构阶段补齐这些字段。
> `auxiliary_inputs` 只声明辅助 artifact 的机器校验口径，不改变 §3 `extra` 的通用必填键；需要审计辅助 artifact 的方案应在 `extra` 中额外记录自己的路径、source 和 data_version。
> 全平台历史回测样本起点统一为 `predict_date >= 2025-01-01`。这条规则不改变月度指标按 `target_date` 分组，也不改变灰度实盘观察区按方案级 `target_date` 起点判定。

```python
# harness/contracts/config_schema.py（设计签名）
def validate_config(raw: dict, dirname: str) -> list[str]:
    """纯字典校验，返回错误信息列表（空=通过）。不 import 算法、不连库。"""
```

---

## 2. `predict.py` 接口契约

`StaticGate` 调 `harness/contracts/predict_contract.py::validate_predict_module(path, scheme_id)`——**纯 AST，不 import、不执行**。

```python
# predict.py 必须满足：
SCHEME_ID = "<scheme_id>"                       # 模块级常量，== config.scheme_id == 目录名
def run(predict_date: str) -> list[PredictionRecord]: ...   # 顶层函数，单位置参 predict_date
```

机器判定：

1. 存在模块级赋值 `SCHEME_ID = "<scheme_id>"`，值 `== scheme_id`。
2. 存在顶层 `def run`，参数恰为一个位置参 `predict_date`。
3. 模块 import 不含写库符号（见 §4 黑名单）；不 `import scheduler.repository` / `scheduler.executor`。
4. 必须 import `shared.input_artifacts`（强制走唯一输入入口）。

```python
# harness/contracts/predict_contract.py（设计签名）
def validate_predict_module(predict_path: Path, scheme_id: str) -> list[str]:
    """AST 解析 predict.py，校验 SCHEME_ID / run 签名 / import 边界。"""
```

---

## 3. `PredictionRecord` 运行期契约

`run(predict_date)` 返回 `list[PredictionRecord]`（定义见 `shared/models.py`）。`DryRunGate` 在 dry-run 输出的 JSON 上断言：

日期语义（每条记录）：

- 历史回测：`predict_date = feature_date = T`，`target_date = T + horizon`。
- 灰度实盘：`prediction_phase = gray_live`，`predict_date = T + 1`，`feature_date = T`，`target_date = T + horizon`。
- 正式实盘：`prediction_phase = scheduled_live`，`predict_date = T + 1`，`feature_date = T`，`target_date = T + horizon`。
- `feature_date` 是平台对外唯一数据截止字段；`anchor_date` 不得作为业务字段使用。如为审计兼容保留在 `extra` 中，必须等于 `feature_date`。

字段一致性（每条记录）：

- `scheme_id == config.scheme_id`
- `horizon == config.horizon`
- `target_tenor ∈ config.tenors`
- `predicted_direction ∈ {1, -1, 0}`（`1`=收益率上行/空，`-1`=下行/多，`0`=平）
- `confidence is None` 或为有限浮点数。它承接算法自身输出的置信度、概率或分数，不改变方向判定；如果原始算法没有天然置信度，source/current benchmark 必须使用同一确定性代理值并在状态文档说明。
- 返回条数 `== 本次有效 tenors 数量`

`extra` 必填键：

| 频率 | 必填键 |
|------|--------|
| 通用 | `input_artifact_path`, `input_artifact_source` |
| daily | `feature_date` |
| weekly | `feature_week_id`, `target_week_id`, `feature_date`, `target_date`, `target_rule` |

> 周频 `target_rule` 与 `t_scheme_weekly_actuals` / `WeeklyActualRecord.target_rule` 对齐，保证预测与实际方向口径一致。
> 实盘落库必须写入一等字段 `prediction_phase`（`gray_live` / `scheduled_live`）。`extra.prediction_phase` 仅作为过渡审计副本，不能替代平台字段。

`CompareGate` 中的 `max_confidence_abs_diff` / `mean_confidence_abs_diff` 是 original/current benchmark 对 `confidence` 字段的浮点差异统计；`1e-16` 量级属于浮点舍入误差，按 0 看待。方案行为一致性的硬门槛仍是 `predicted_direction` 逐样本零容差。

---

## 4. `core/` 约束

| 约束 | 判定 |
|------|------|
| DataFrame in / 结果对象 out | core 函数签名接收 `pd.DataFrame`，返回算法结果对象 |
| 零 DB | 非 legacy `core/*.py` 不得 import `sqlalchemy`/`pymysql`，不得 `create_engine`/`read_sql`/`text(` |
| 零写库 | 不得 import `scheduler.repository`/`scheduler.executor`，不得出现 `INSERT/UPDATE/DELETE/ALTER/DROP` 字面量 |
| 零跨方案 | 不得 `from schemes.<other_scheme>...` import；跨方案复用只能沉到 `shared/` 公共层，并通过架构评审 |
| legacy 隔离 | `core/legacy_*.py` 可保留旧代码，但活跃模块不得 import 它 |

危险符号黑名单（`harness/contracts/import_rules.py`）：

```python
DANGEROUS_CORE_IMPORTS = {"sqlalchemy", "pymysql", "scheduler", "shared.input_artifacts"}
WRITE_CALL_NAMES       = {"create_engine", "create_sqlalchemy_engine", "read_sql",
                          "upsert_predictions", "write_run_log", "execute_scheme"}
SQL_WRITE_KEYWORDS     = ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP")
```

（`shared.input_artifacts` 在 core 中是禁止项——输入应由 adapter `predict.py` 注入；在 predict.py 中则是必需项。）

---

## 5. 契约与现有方案对账

状态最近更新 2026-06-11：当前在册 active 方案为 `t1_daily`、`t5_daily`、`weekly_5y_direct_0529`、`weekly_7y_cross_d_overlay_0529`、`weekly_10y_d_overlay_0529`；旧周度方案（`weekly_10y_d_overlay` / `weekly_5y_direct_production` / `weekly_7y_cross_d_overlay`）已退役。

| 契约项 | `t1_daily` | `t5_daily` | `weekly_5y_direct_0529` | `weekly_7y_cross_d_overlay_0529` | `weekly_10y_d_overlay_0529` |
|--------|:----------:|:----------:|:-----------------------:|:--------------------------------:|:-------------------------------:|
| `config.yaml` 基础字段 | ✅ | ✅ | ✅ | ✅ | ✅ |
| `input_spec.*` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `target_rule` | 不适用 | 不适用 | ✅ | ✅ | ✅ |
| `predict.py` SCHEME_ID + run 签名 | ✅ | ✅ | ✅ | ✅ | ✅ |
| core 零 DB | ✅ | ✅ | ✅ | ✅ | ✅ |
| extra 必填键 | ✅ | ✅ | ✅ | ✅ | ✅ |

上述状态由 StaticGate / UnitGate / DryRunGate 持续守护；新增方案开工前先读 [sop/SCHEME_ONBOARDING_T0.md](sop/SCHEME_ONBOARDING_T0.md)。

---

## 6. 校验责任归属

| 契约 | 校验时机 | 校验器 |
|------|----------|--------|
| §1 config schema | StaticGate（静态） | `contracts/config_schema.py` |
| §2 predict 接口 | StaticGate（AST） | `contracts/predict_contract.py` |
| §4 core 约束 | StaticGate（AST） | `contracts/import_rules.py` + `static_gate.py` |
| §3 PredictionRecord 运行期 | DryRunGate（dry-run JSON） | `gates/dry_run_gate.py` |
| §7 落库后完整性 | LiveGate / BacktestGate（落库后） | `probes/table_guard.py` + 完整性校验器（**待落地**） |

> 本文为契约规范。未创建或修改任何代码。

---

## 7. 落库后数据完整性契约（机器可校验）

**定位**: 现有 `probes/table_guard.py` 只校验**行数 delta**（防误写其它表），缺**写入内容**的完整性校验——DB 静默写坏（方向越界、唯一键重复、样本数不符）当前无人发现。本节定义落库后必须成立的断言，由 LiveGate / BacktestGate 在写库后执行。

> **校验器待落地**：本节为规范，断言逻辑由后续 harness probe 实现（建议 `probes/integrity_guard.py`）。落地前由 [POST_ONBOARDING_TEST_SOP §S6](sop/SCHEME_POST_ONBOARDING_TEST_SOP.md#s6--落库写历史回测结果) 人工核验。

### 7.1 实盘预测落库（`t_scheme_predictions`）

LiveGate 写库后，对该 `scheme_id` + `predict_date` 断言：

| 维度 | 断言 | 失败含义 |
|------|------|----------|
| 行数 | 新增行数 `== 本次有效 tenors 数` | 漏写/重复写 tenor |
| 值域 | `predicted_direction ∈ {1, -1, 0}` | 方向越界，污染准确率 |
| 一致性 | `horizon == config.horizon`；`target_tenor ∈ config.tenors` | 方案身份漂移 |
| 唯一性 | 无重复 `(scheme_id, target_tenor, horizon, target_date)` | 违反 `t_scheme_predictions` 当前业务 UK；同一 target 被重复展示 |
| 阶段 | 实盘记录可判定 `prediction_phase ∈ {gray_live, scheduled_live}`；灰度补齐和 scheduler 自然发出不得混淆 | 前端和业务把灰度与正式实盘混算 |
| 日期 | `feature_date` 存在；日频灰度/正式实盘满足 `feature_date < predict_date` 且 `target_date` 来自 `feature_date + horizon`；回测满足 `predict_date == feature_date` | T/T+1 语义混淆，可能数据泄漏 |
| 受保护表 | 除 `t_scheme_predictions` / `t_scheme_run_log` 外，`PROTECTED_TABLES` 全部 `delta==0` | 越界写库 |

> 每 scheme 行数快照可复用 `probes/table_guard.py::snapshot_scheme_counts`。

### 7.2 历史回测落库（`t_backtest_*`）

BacktestGate 去掉 `--no-persist` 落库后断言：

| 维度 | 断言 | 失败含义 |
|------|------|----------|
| 样本数 | 落库样本数 `== --no-persist 复现样本数` | 落库丢样本/重样本 |
| 表隔离 | 仅 `t_backtest_runs/_predictions/_monthly_metrics` 该 run 相关行增加 | 误写实盘表 |
| 实盘表零变化 | `t_scheme_predictions/run_log/actuals` `delta==0` | 回测污染实盘 |
| 口径一致 | 落库 run 的 `data_version` 与 §1 `input_spec.data_version` 及 live 一致 | backtest↔live 口径漂移（见 §1） |
| 日期语义 | 回测 rows 必须满足 `predict_date == feature_date`，不得读取或复制灰度/正式实盘记录 | 用 T+1 实盘结果冒充 T 回测结果 |

### 7.3 与现有机制的关系

- §7 是 `table_guard`（行数 delta）的**内容层补强**，二者叠加：先 `delta` 守边界，再完整性守内容。
- 与 [POST_ONBOARDING_TEST_SOP §S6](sop/SCHEME_POST_ONBOARDING_TEST_SOP.md#s6--落库写历史回测结果) 验收点一致：S6 为人工执行版，§7 为机器契约版，落地后 S6 引用本节作为判据。
