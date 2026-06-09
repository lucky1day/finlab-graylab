# 统一公共层（数据接入）目标设计

**更新日期**: 2026-06-08
**定位**: 设计 `shared/` 数据接入层的**目标形态**——消除周频重复、统一交易日历、强化输入工件元数据。
**边界**: 本文**只设计、不改代码**。所有重构（删除/新建/改字段）留到实现阶段；本文是其蓝图与验收基线。

> 数据安全边界（源表只读、写库白名单）以 [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) §5 为权威。本文聚焦"数据怎么进来、谁负责、接口长什么样"。

---

## 1. 现状与问题（已核对源码）

### 1.1 当前数据接入链路

```
DB (api_wind_daily/weekly/monthly + derivative, api_wind_indicators_all)
  → shared/data_service.py        统一导出宽表（daily/weekly/monthly builders）
  → shared/input_artifacts.py     唯一输入工件入口，生成 CSV 并读回 DataFrame
  → schemes/{id}/predict.py       adapter 取 artifact.dataframe 交给 core
```

`shared/data_service.py` 已是权威导出层，含 `build_daily_output_from_db` / `build_weekly_output_from_db` / `build_monthly_output_from_db` / `create_sqlalchemy_engine` / `read_weekly_long_from_db` 等。

### 1.2 三个真实架构债

**问题 A — 周频逻辑双份并存**
`schemes/weekly_10y_d_overlay/core/weekly_data_service.py` 自带一套 DB 读取实现，与 `shared/data_service.py` 大量重复且违反"core 零 DB"约束：

| 功能 | `shared/data_service.py` | `weekly_10y_d_overlay/core/weekly_data_service.py` |
|------|--------------------------|---------------------------------------------------|
| 因子元数据筛选 | `select_factor_metadata` | `select_weekly_factor_metadata` |
| 元数据读取 | `read_factor_metadata_from_db` | `read_factor_metadata_from_db`（重复） |
| 周频长表读取 | `read_weekly_long_from_db` | `read_weekly_long_from_db`（重复） |
| 周频宽表 | `build_weekly_output_from_db` | `build_weekly_output_from_db`（另一实现） |
| 日期→week_id | 无 | `read_source_week_id_for_date` |
| wind_export 口径 | 无 | `build_wind_export_weekly_output_from_frames` + `build_daily_weekly_close_fallback_*` |

**问题 B — 跨方案耦合**（最严重）
`weekly_5y_direct_production/predict.py` 和 `weekly_7y_cross_d_overlay/predict.py` 都直接：

```python
from schemes.weekly_10y_d_overlay.core.weekly_data_service import read_source_week_id_for_date
```

即两个方案 **import 进了另一个方案的 core**。这违反方案隔离——`weekly_10y` 的任何改动会波及 `weekly_5y/7y`，且 StaticGate 的"core 零 DB / 强制公共输入入口"规则无法干净通过。

**问题 C — 交易日历查询散落**
`t5_daily/predict.py` 内联 `text("... FROM t_trade_calendar ...")` 直接查交易日历；周频方案走 core 里的 `read_source_week_id_for_date`。同一类"日历问题"有两套实现、两个位置。

**问题 D — InputArtifact 元数据不足**
当前 `InputArtifact` 仅含 `scheme_id/frequency/path/dataframe/source/generated_at/metadata`。InputGate 想机器校验"列覆盖/日期覆盖/空值率"时缺字段。

---

## 2. 目标设计

### 2.1 周频去重：单一权威 + 命名口径

原则：**`shared.data_service` 是唯一周频导出实现**；不同方案的历史口径差异通过**显式命名变体参数**表达，而非第二份服务。

- 把 `core/weekly_data_service.py` 中真正有用的 DB 逻辑（`read_source_week_id_for_date`、wind_export 口径、daily_weekly_close_fallback）**收编**进 `shared.data_service`，作为参数化口径：

```python
# shared/data_service.py（目标）
def build_weekly_output_from_db(
    *,
    schema_columns: list[str] | None = None,
    start_week: int | None = None,
    end_week: int | None = None,
    weekly_variant: str = "unified",     # "unified" | "wind_export_0529"
    engine=None,
) -> pd.DataFrame: ...
```

- `weekly_5y/7y` 依赖的旧 schema-driven 口径（`weekly_output_0529_columns.json` + daily fallback）→ `weekly_variant="wind_export_0529"`。
- 迁移后：`schemes/weekly_10y_d_overlay/core/weekly_data_service.py` **删除**；三个周频方案的 `predict.py` 只 import `shared.*`，不再跨方案 import。
- `core/` 只保留纯算法（`predictors.py`、`date_utils.py`、legacy 归档），零 DB。

### 2.2 统一交易日历服务（新模块）

新建 `shared/calendar_service.py`，把"日历/周历"查询收敛到一处，取代 `t5_daily` 内联 SQL 与周频 `read_source_week_id_for_date`：

```python
# shared/calendar_service.py（目标设计）
@dataclass(frozen=True)
class CalendarService:
    """只读交易日历/周历服务。core 不得直接查日历源表。"""
    def is_trading_day(self, d: str | date) -> bool: ...
    def next_trading_days(self, after: str, n: int) -> list[str]: ...
    def nth_trading_day_after(self, feature_date: str, horizon: int) -> str: ...   # 取代 t5_daily 内联 SQL
    def week_id_for_date(self, d: str) -> int | None: ...                          # 取代 read_source_week_id_for_date
    def week_id_to_last_trading_day(self, week_id: int) -> str: ...

def get_calendar(engine) -> CalendarService: ...
```

落点：
- `t5_daily/predict.py` 的 `_target_date_from_feature_date` 内联 SQL → `calendar.nth_trading_day_after(feature_date, 5)`。
- 三个周频 `predict.py` 的 `read_source_week_id_for_date(...)` → `calendar.week_id_for_date(...)`。
- 交易日判断只读 `t_trade_calendar.trade_flag`；缺失日期按非交易日处理，不回退本地节假日库或 weekday。
- `week_id` 映射只读 `api_wind_date.week_id`；周内最后交易日通过 `api_wind_date` 找同周日期，再 join `t_trade_calendar` 过滤交易日。

### 2.3 强化 InputArtifact

```python
# shared/input_artifacts.py（目标字段扩展）
@dataclass(frozen=True)
class Coverage:
    kind: str            # "date" | "week_id"
    min: str | int | None
    max: str | int | None
    count: int

@dataclass(frozen=True)
class InputArtifact:
    scheme_id: str
    frequency: str
    path: Path
    dataframe: pd.DataFrame
    source: str
    generated_at: str
    metadata: dict[str, Any]
    # 新增（让 InputGate 可机器校验）
    data_version: str               # 口径版本，如 "daily_v1" / "wind_export_0529"
    row_count: int
    column_count: int
    columns: list[str]
    date_coverage: Coverage         # daily: date min/max；weekly: week_id min/max
    quality_flags: dict[str, Any]   # {"null_ratio":.., "missing_required_cols":[...], "duplicate_keys":int}
```

兼容性：新增字段全部由 `build_*_input_artifact` 在生成时计算填充，不改变现有 `path/dataframe/source` 语义；现有调用方读 `.dataframe` 不受影响。

清理项（实现阶段）：`build_weekly_input_artifact` 仍保留 `end_date` / `include_daily_weekly_close_fallback` 两个"报错占位"参数——目标是改由 `weekly_variant` 表达口径后移除这两个死参数。

---

## 3. 对外稳定契约（重构后）

| 模块 | 对外接口 | 职责 | 禁止 |
|------|----------|------|------|
| `shared.data_service` | `build_{daily,weekly,monthly}_output_from_db`、`create_sqlalchemy_engine`、`save_*_output` | **唯一** DB 导出口径 | 普通方案接入时改其业务逻辑；新增第二份周频服务 |
| `shared.input_artifacts` | `build_daily_input_artifact` / `build_weekly_input_artifact`（返回强化后 `InputArtifact`） | **唯一**算法输入入口 | adapter/backtest runner 绕过它拼输入 |
| `shared.calendar_service` | `get_calendar(engine)` → `CalendarService` | **唯一**方案侧交易日历/周历查询 | core 直接查 `t_trade_calendar` / `api_wind_date`；方案各自实现日历 |
| `shared.models` | `PredictionRecord` / `ActualRecord` / `WeeklyActualRecord` | 统一数据模型 | 方案私自定义并行模型 |

---

## 4. 重构后的数据接入链路（目标）

```
DB (源表只读)
  → shared.data_service          唯一导出（weekly_variant 表达口径差异）
  → shared.calendar_service      唯一日历/周历查询
  → shared.input_artifacts       唯一输入工件（强化元数据）
  → schemes/{id}/predict.py      adapter：调 input_artifacts + calendar_service + core
  → schemes/{id}/core/           纯算法，零 DB、零跨方案 import
```

---

## 5. 建议实施排序（实现阶段，非本阶段）

1. 新建 `shared/calendar_service.py`，把 `read_source_week_id_for_date` + `t5_daily` 内联 SQL 收编。
2. `data_service.build_weekly_output_from_db` 增 `weekly_variant`，收编 wind_export 口径。
3. 三个周频 `predict.py` 改 import `shared.*`，删除 `core/weekly_data_service.py`，删跨方案 import。
4. 强化 `InputArtifact` 字段（生成时填充）。
5. 移除 `build_weekly_input_artifact` 的死参数 `end_date` / `include_daily_weekly_close_fallback`。
6. 每步配套更新 `tests/`，并用 `harness gate static/input`（见 [HARNESS_DESIGN.md](HARNESS_DESIGN.md)）验证违规清零。

---

## 6. 验收基线（重构完成的判定）

- `grep -rn "from schemes\." schemes/*/predict.py` 无跨方案 import。
- `schemes/*/core/*.py`（非 legacy）无 `sqlalchemy` / `text(` / `read_sql` —— StaticGate 规则 4 全绿。
- 方案侧交易日历/周历查询只在 `shared/calendar_service.py` 出现；scheduler 自身的交易日保护只在 `scheduler/calendar.py` 读取 `t_trade_calendar`。
- `InputArtifact` 含全部新增元数据字段，InputGate 可断言 `missing_required_cols == []`。

> 本文为目标设计。未创建或修改任何 `shared/` 代码。
