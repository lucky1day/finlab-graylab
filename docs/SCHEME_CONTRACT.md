# 方案契约形式化规范（机器可校验）

**更新日期**: 2026-06-08
**定位**: 把散落在 [SCHEME_ONBOARDING_SOP.md](sop/SCHEME_ONBOARDING_SOP.md) §4/§5 的方案约束收敛成**单一权威契约**，供 harness 的 `StaticGate` / `DryRunGate` 机器校验。
**边界**: 本文是规范，不含校验器实现代码。校验逻辑由 `harness/contracts/*`（见 [HARNESS_DESIGN.md](HARNESS_DESIGN.md) §6）按本文落地。

> SOP 仍是人类执行手册；本文是机器契约。两者一致，本文更细、可判定。任何冲突以本文为准并回写 SOP。

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
| `input_spec.data_version` | str | ✅(新) | 对应 `InputArtifact.data_version`，如 `daily_v1` / `wind_export_0529` |
| `input_spec.required_columns` | list[str] | ✅(新) | InputGate 据此校验列覆盖 |
| `input_spec.weekly_variant` | str | `frequency==weekly` 时✅(新) | 对应 `data_service.weekly_variant`，如 `unified` / `wind_export_0529` |
| `target_rule` | str | `frequency==weekly` 时✅(新) | 目标日语义（如 `next_week_last_trading_day_vs_current_week`） |
| `backtest.runner` | str | ➖(新) | `backtests/{scheme_id}_reproduction.py` 模块名；参与历史排行时必填 |

> 标注「新」的字段是本设计**新增的必填项**——让 harness 无需读算法即可知道输入口径、列要求、目标语义。现有方案在数据层重构阶段补齐这些字段。

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

字段一致性（每条记录）：

- `scheme_id == config.scheme_id`
- `horizon == config.horizon`
- `target_tenor ∈ config.tenors`
- `predicted_direction ∈ {1, -1, 0}`（`1`=收益率上行/空，`-1`=下行/多，`0`=平）
- 返回条数 `== 本次有效 tenors 数量`

`extra` 必填键：

| 频率 | 必填键 |
|------|--------|
| 通用 | `input_artifact_path`, `input_artifact_source` |
| daily | `feature_date` |
| weekly | `feature_week_id`, `target_week_id`, `feature_date`, `target_date`, `target_rule` |

> 周频 `target_rule` 与 `t_scheme_weekly_actuals` / `WeeklyActualRecord.target_rule` 对齐，保证预测与实际方向口径一致。

---

## 4. `core/` 约束

| 约束 | 判定 |
|------|------|
| DataFrame in / 结果对象 out | core 函数签名接收 `pd.DataFrame`，返回算法结果对象 |
| 零 DB | 非 legacy `core/*.py` 不得 import `sqlalchemy`/`pymysql`，不得 `create_engine`/`read_sql`/`text(` |
| 零写库 | 不得 import `scheduler.repository`/`scheduler.executor`，不得出现 `INSERT/UPDATE/DELETE/ALTER/DROP` 字面量 |
| 零跨方案 | 不得 `from schemes.<other_scheme>...` import（见 [DATA_LAYER_DESIGN.md](DATA_LAYER_DESIGN.md) §1.2 问题 B） |
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

状态最近更新 2026-06-08（S2 完成后）：

| 契约项 | `t1_daily` | `weekly_10y_d_overlay` | `weekly_5y/7y`（paused） |
|--------|:----------:|:----------------------:|:------------------------:|
| `config.yaml` 基础字段 | ✅ | ✅ | ✅ |
| 新增 `input_spec.*` / `target_rule` | 待补(S3) | 待补(S3) | 待补(S3) |
| `predict.py` SCHEME_ID + run 签名 | ✅ | ✅ | ✅ |
| core 零 DB | ✅ | ✅（S2 删除 `core/weekly_data_service.py`） | ✅（S1 改用 `shared.calendar_service`，已无跨方案 import） |
| extra 必填键 | ✅ | ✅ | ✅ |

> 「core 零 DB」三方案已全部转绿（V1/V2 清零）。剩余「`input_spec.*` / `target_rule`」字段在 S3 强化 `InputArtifact` 时随 `config.yaml` 补齐。补齐后 StaticGate 可对全部方案持续守护。

---

## 6. 校验责任归属

| 契约 | 校验时机 | 校验器 |
|------|----------|--------|
| §1 config schema | StaticGate（静态） | `contracts/config_schema.py` |
| §2 predict 接口 | StaticGate（AST） | `contracts/predict_contract.py` |
| §4 core 约束 | StaticGate（AST） | `contracts/import_rules.py` + `static_gate.py` |
| §3 PredictionRecord 运行期 | DryRunGate（dry-run JSON） | `gates/dry_run_gate.py` |

> 本文为契约规范。未创建或修改任何代码。
