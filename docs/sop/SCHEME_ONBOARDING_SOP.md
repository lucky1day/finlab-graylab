# 新增预测方案 SOP

**更新日期**: 2026-06-09
**适用范围**: 在 `bond-factor-lab` 中新增一个可调度、可写库、可在前端方案矩阵中对比的预测方案。

> 强约束 harness 总纲见 [HARNESS_ARCHITECTURE.md](../HARNESS_ARCHITECTURE.md)。本 SOP 是执行入口；任何新增方案都必须按 harness gate 推进，不能临时绕过公共输入层、回测层或调度写库边界。
>
> **端到端主线见 [../SCHEME_INGESTION.md](../SCHEME_INGESTION.md)**（AI/新人第一入口，一图串联源码放哪/怎么拆/预测怎么放怎么验/写哪张表/回测入库）。本 SOP 是其「改造进系统」段的人类执行手册。

## 1. 核心原则

新增方案时必须先区分两个概念:

- **任务格子**: `Y标的 + 预测长度`，例如 `10Y国债活跃 · T+1`。这是前端筛选和排行的格子。
- **方案实例**: 一个具体 `scheme_id`，例如 `t1_daily`、`t1_lgbm_spread_v2`。同一个任务格子下允许多个方案实例并存排行。

当前约定:

- 一个 `scheme_id` 只对应一个 `horizon`。同一算法如果同时做 T+1 和 T+5，应拆成两个方案目录。
- 当前已接入并 active 的方案只有 `daily` 频率的 `t1_daily` / `t5_daily`。新增周度方案进入 live 调度前，必须先确认周度目标日规则、actuals 对齐规则、最新特征周产出能力，以及调度时间与上游 weekly 首轮预测时间对齐。
- `scheme_id` 一旦写入数据库就视为稳定 ID，不要随意改名；展示名变更只改 `name`。
- 算法核心逻辑放在 `core/` 或独立模块里，`predict.py` 只做框架适配、输入准备和输出转换。
- 方案不能直接写 `t_scheme_predictions`；统一由 `scheduler.executor` 写库，保证运行日志和 UPSERT 口径一致。
- Y 标的展示名由数据库 `t_target_registry` 管理，`target_tenor` 只作为内部稳定 key。
- 新方案默认先用 `status: paused` 验证；通过 dry-run、手动写库和 API 检查后再改为 `active`。
- 当前 harness 设计已定，后续新增方案必须通过 Intake -> Normalize -> Input Gate -> Static Gate -> Unit Gate -> Dry-run Gate -> Backtest Gate -> Live Gate -> Activation -> Documentation；没有 gate 证据时不得宣称方案完成或 live ready。

### 1.1 强约束模块边界

| 模块 | 允许职责 | 明确禁止 |
|------|----------|----------|
| `shared.data_service` | 唯一底层日/周/月 DB 导出标准 | 普通方案接入时修改其业务逻辑 |
| `shared.input_artifacts` | 唯一算法输入文件生成入口 | adapter 或 backtest runner 绕过它直接拼输入 |
| `schemes/{scheme_id}/core/` | 纯算法逻辑、legacy 原始脚本归档 | 写库、调 scheduler、直接生成运行期输入文件 |
| `schemes/{scheme_id}/predict.py` | 调公共输入层、调用 core、返回 `PredictionRecord` | 写 `t_scheme_predictions` / `t_scheme_run_log` |
| `scheduler.scheme_runner` | 只读 dry-run，输出 JSON | 写库或同步 registry |
| `scheduler.executor` / `scheduler.repository` | 正式预测统一写库边界 | 被普通 readiness 检查当作探针 |
| `backtests/` | 历史复现和 `t_backtest_*` 写入 | 写实盘预测表 |
| `scripts/` | 审计、对比、受控 admin 命令 | 作为普通方案运行入口绕过 SOP |

## 2. 命名规范

`scheme_id` 使用小写 snake_case，建议包含预测长度、模型或特征版本:

| 类型 | 示例 | 说明 |
|------|------|------|
| 基准复现 | `t1_daily` | 已接入的 T+1 0529 原始基准 |
| 新 T+1 方案 | `t1_lgbm_spread_v2` | T+1、LightGBM、利差增强、v2 |
| 新 T+5 方案 | `t5_lgbm_macro_v1` | T+5、LightGBM、宏观因子版本 |
| 新周度方案 | `weekly_db_sourced_v1` | 周六预测下周最后交易日 vs 本周最后交易日 |

不要把 `scheme_id` 命名成 `t1_5y`、`t5_10y` 这类只描述任务格子的名字。期限范围由 `tenors` 字段管理；方案身份由算法来源、特征集合和版本定义。

展示名 `name` 要比 `scheme_id` 更可读，建议包含来源、预测长度、模型类型和版本，例如:

```yaml
name: "T1-LGBM利差增强-v2"
```

文件命名规则:

- Python 模块、测试、脚本统一使用小写 `snake_case.py`，例如 `latest_prediction.py`、`check_weekly_readiness.py`。
- 方案目录必须等于 `scheme_id`，统一小写 snake_case，例如 `schemes/weekly_db_sourced_v1/`。
- `backtests/` 下的回测 runner 必须带范围，不使用 `reproduction.py` 这类泛名；日频批次用 `daily_0529_reproduction.py`，单方案周频用 `{scheme_id}_reproduction.py`，例如 `weekly_db_sourced_v1_reproduction.py`。
- `scripts/` 下的命令必须使用“动作 + 对象 + 目的”命名，例如 `run_framework_repro.py`、`verify_frontend_db.py`、`compare_refactor_outputs.py`。
- 调度器中涉及频率差异的刷新模块必须显式带频率，例如 `daily_actuals_updater.py` 和 `weekly_actuals_updater.py`；不要使用 `actuals_updater.py` 这类容易和周度逻辑混淆的泛名。
- 固定 schema 或 benchmark 文件可带版本日期，但日期前必须有分隔符，例如 `weekly_output_0529_columns.json`，不要使用 `weekly_output0529_columns.json`。
- 前端静态资源允许使用 kebab-case，例如 `aifin-shell.js`、`aifin-lab-logo.svg`。
- launchd plist 使用 macOS 约定的 reverse-DNS 命名，例如 `com.bond-factor-lab.backend.plist`。
- 文档入口文件保留常见大写约定，例如 `README.md`、`AGENTS.md`；正文引用必须使用真实路径。
- 不要把角色混在一个文件名里: `scheme_id`、`benchmark_id`、`data_source` 分别表达方案、基准批次、数据口径。

## 3. 目录模板

每个方案放在 `schemes/{scheme_id}/`:

```text
schemes/
└── t1_lgbm_spread_v2/
    ├── __init__.py
    ├── config.yaml
    ├── predict.py
    └── core/
        ├── __init__.py
        └── ...
```

`core/` 不是强制目录，但建议使用。它的职责是承载算法原逻辑；`predict.py` 的职责是把平台输入输出转换成统一接口。

## 4. config.yaml 模板

```yaml
scheme_id: t1_lgbm_spread_v2
name: "T1-LGBM利差增强-v2"
description: "T+1 方向预测，加入利差增强特征的 LightGBM 方案。"
horizon: 1
tenors: ["5Y", "10Y"]
frequency: daily
schedule:
  cron: "25 9 * * 1-5"
  timezone: "Asia/Shanghai"
entry_point: predict.run
status: paused
```

字段要求:

| 字段 | 要求 |
|------|------|
| `scheme_id` | 必须与目录名完全一致 |
| `horizon` | 日度使用 `1` / `5`；当前周度使用 `6` 表示周六发出、下周最后交易日为目标日 |
| `tenors` | 内部稳定 key，当前前端展示为 `3Y国债活跃/5Y国债活跃/7Y国债活跃/10Y国债活跃`；`1Y` 可作为因子输入，但不作为当前展示目标 |
| `schedule.cron` | 日度通常为 `25 9 * * 1-5`；当前周度 live 使用 `30 11 * * 6`，对齐旧实盘 weekly `multi` 任务首轮预测时间 |
| `status` | 新方案先用 `paused`；验证完成后再改 `active` |

如果新增了新的 Y 标的 key，还需要先写入 `t_target_registry`:

```sql
INSERT INTO t_target_registry
    (target_code, display_name, asset_class, target_type, sort_order, status)
VALUES
    ('CGB_3Y_ACTIVE', '3Y国债活跃', 'bond', 'active_treasury', 30, 'active')
ON DUPLICATE KEY UPDATE
    display_name = VALUES(display_name),
    asset_class = VALUES(asset_class),
    target_type = VALUES(target_type),
    sort_order = VALUES(sort_order),
    status = VALUES(status),
    updated_at = CURRENT_TIMESTAMP;
```

当前历史基准方案仍使用 `3Y/5Y/7Y/10Y` 作为内部 key，因此数据库种子映射为 `3Y -> 3Y国债活跃`。

## 5. predict.py 接口模板

`predict.py` 必须暴露:

```python
from __future__ import annotations

from shared.models import PredictionRecord


SCHEME_ID = "t1_lgbm_spread_v2"
HORIZON = 1
TENORS = ["5Y", "10Y"]


def run(predict_date: str) -> list[PredictionRecord]:
    """执行单日预测。

    Args:
        predict_date: 预测发出日期，格式 YYYY-MM-DD。

    Returns:
        每个预测期限一条 PredictionRecord。
    """
    records: list[PredictionRecord] = []

    # 1. 通过 shared.input_artifacts 生成输入 CSV 并读回 DataFrame。
    # 2. 调用 core/ 中的算法逻辑。
    # 3. 把算法输出转换为 PredictionRecord。

    records.append(
        PredictionRecord(
            scheme_id=SCHEME_ID,
            target_tenor="10Y",
            horizon=HORIZON,
            predict_date=predict_date,
            target_date=predict_date,
            predicted_direction=1,
            confidence=0.62,
            model_version="v2",
            extra={
                "feature_date": "2026-05-29",
                "input_artifact_path": "backtest_artifacts/runtime_inputs/t1_lgbm_spread_v2/daily_output_2026-06-01.csv",
                "input_artifact_source": "shared_data_service_daily",
            },
        )
    )
    return records
```

输出约束:

- `scheme_id` 必须等于 `config.yaml` 和目录名。
- `target_tenor` 必须属于 `config.yaml.tenors`。
- `horizon` 必须等于 `config.yaml.horizon`。
- `predicted_direction` 只能是 `1`、`-1` 或 `0`。
- `target_date` 必须能与指标口径对齐；当前 live metrics 后端按 `horizon=1` 取 `direction_1d`，按 `horizon=5` 取 `direction_5d`，按周度 `horizon=6` 取 `t_scheme_weekly_actuals.direction_weekly`。新增周度方案 active 前仍需确认最新特征周数据完整并完成受控写库验收。
- `extra` 必含 `input_artifact_path` 和 `input_artifact_source`；日频另含 `feature_date`，周频另含 `feature_week_id/target_week_id/feature_date/target_date/target_rule`。完整字段契约（机器可校验）见 [SCHEME_CONTRACT.md](../SCHEME_CONTRACT.md) §3。

## 6. Harness 入库流程

后续所有新方案都按以下 gate 顺序推进。旧的手动命令仍可作为每个 gate 的实现方式，但不能跳过 gate。

> 可执行 harness 的统一入口设计（`python -m harness onboard {scheme_id} --stage all`、各 Gate 契约、授权机制）见 [HARNESS_DESIGN.md](../HARNESS_DESIGN.md)。下表每个 Gate 落地后对应一条 `python -m harness gate <name>` 命令；本节裸 conda 命令是该 Gate 的底层实现。

| Gate | 目标 | 通过证据 |
|------|------|----------|
| Intake | 明确方案身份、频率、horizon、tenors、预测语义、调度时间、原始文件和样本数据 | 接入记录中写清楚 scheme_id、frequency、预测口径和是否需要历史回测 |
| Normalize | 将原始算法归档并改造成框架 core | `schemes/{scheme_id}/core/` 存在，实盘路径不直接 import 外部绝对路径脚本 |
| Input Gate | 所有算法输入由公共层生成 | adapter/backtest runner 调用 `shared.input_artifacts`，extra/summary 记录 `input_artifact_source` |
| Static Gate | 阻断危险结构和绕路调用 | 目录、命名、接口、危险导入、直接写库检查通过 |
| Unit Gate | 锁定 core 和 adapter 行为 | 单测覆盖 core 输出、adapter 输出、公共输入层调用、`PredictionRecord` 字段 |
| Dry-run Gate | 只读运行方案 | `scheduler.scheme_runner` 返回 JSON，正式 prediction/run_log 行数不变 |
| Backtest Gate | 历史回测可复现 | `--no-persist` summary 通过；授权后只写 `t_backtest_*` |
| Live Gate | 受控写入单方案实盘预测 | 只写该 `scheme_id` 的 prediction/run_log，actuals 和源表不变 |
| Activation | 启用自动调度 | 全部 gate 通过后才把 `status` 改为 `active` 并重启 scheduler |
| Documentation | 留下审计证据 | 更新状态、测试、历史回测或上线观察文档 |

### Step 1: Intake - 确认方案身份

先写清楚:

| 项 | 示例 |
|----|------|
| 方案 ID | `t1_lgbm_spread_v2` |
| 预测长度 | `T+1` |
| 覆盖 Y 标的 | `5Y国债活跃/10Y国债活跃` |
| 算法来源 | 上游新模型、内部改造、参数实验等 |
| 数据来源 | `bond_db` 直接取数、DB 生成 CSV、人工补充文件等 |
| 是否需要历史回测 | 是 / 否 |

如果只是新增同一个任务格子的候选方案，不要复用旧 `scheme_id`，要新增独立目录。

### Step 2: Normalize - 新建方案目录

```bash
mkdir -p schemes/t1_lgbm_spread_v2/core
touch schemes/t1_lgbm_spread_v2/__init__.py
touch schemes/t1_lgbm_spread_v2/core/__init__.py
```

创建 `config.yaml` 和 `predict.py`。如果算法来自上游原始代码，把原始核心逻辑归档到 `core/`，实盘路径必须改造成 DataFrame 输入的 core 函数；adapter 只负责输入输出转换。

### Step 3: Input Gate - 公共输入层

预测 adapter 不应自行从 DB 拼输入 DataFrame，也不应自行决定输入文件路径。所有方案必须先通过 `shared.input_artifacts` 生成输入 CSV，再读取该 CSV 给算法；日频使用 `build_daily_input_artifact()`，周频使用 `build_weekly_input_artifact()`，月频后续补 `build_monthly_input_artifact()`。底层数据导出统一由 `shared.data_service` 负责，运行期 CSV 统一写入 `backtest_artifacts/runtime_inputs/{scheme_id}/`；公共数据层逻辑不得在新增方案时临时改动。

周频 artifact 只接受 `start_week/end_week` 作为周范围过滤。旧的 `end_date` 和 `include_daily_weekly_close_fallback` 参数不属于统一数据层口径，当前会显式报错，不能在新增方案中使用。

历史回测 runner 也必须遵守同一条输入链路: runner 先调用 `shared.input_artifacts` 生成 `historical_backtest` 输入文件，再把读回后的 DataFrame 交给算法。只有 `scripts/audit_*`、`scripts/compare_*` 这类数据服务审计脚本可以直接调用底层 `shared.data_service`；普通方案、live dry-run 和 backtest runner 不允许绕过公共输入 artifact。

#### Step 3a（强制）: 周频 week_id 必须读 DB，禁止用日历公式算

> **背景**：曾出现周频方案用日历公式（"每年第一个周一"或 ISO 周）把 `week_id` 算成日期，与数据库实际口径不一致，导致特征周/目标周对错行、取错收益率、算错方向。该错误在入库时即被引入且不易察觉。

强制规则（适用于所有周频/月频方案的 adapter、core 与 backtest runner）：

1. **week_id 的权威来源唯一**：`bond_db.api_wind_date.week_id`（该表含 `rdate` + `week_id` 两列）。任何 `week_id ↔ 日期` 的映射都必须**从 DB 读取**，不得用本地日历公式计算。
   - 日期 → week_id：读 `api_wind_date.week_id`（经 `shared.calendar_service.week_id_for_date`）。
   - week_id → 周内最后交易日：先从 `api_wind_date` 找同周日期，再按 `t_trade_calendar.trade_flag = '1'` 过滤并取 `MAX(rdate)`，不得用 `week_id_to_friday/monday` 这类公式。
2. **禁止**新增方案在预测/回测路径中 import 任何 `*_to_friday` / `*_to_monday` / `get_week_id_for_date` 这类**计算型**周历函数来决定特征周/目标周日期。这些仅允许作为展示用近似或历史归档，不得参与数据对齐。
3. **target_week_id** 同样以 DB 口径推导：本周 week_id 的下一周，应以 `api_wind_date` 中实际存在的下一个 week_id 为准，而非 `feature+1` 直接递增。
4. 验收证据：adapter/runner 日志或 `extra` 中能证明 `feature_week_id`、`target_week_id`、`feature_date`、`target_date` 均来自 `api_wind_date` / `t_trade_calendar` 读取，而非公式计算。

### Step 4: Static Gate - 静态边界检查

静态检查必须覆盖:

- `scheme_id` 与目录名一致。
- `config.yaml` 可被 `scheduler.discovery` 读取。
- `predict.py` 暴露 `run(predict_date: str) -> list[PredictionRecord]`。
- `core/` 不直接 import `scheduler.repository`、`scheduler.executor` 或 SQL 写库函数。
- `predict.py` 不直接执行 `INSERT/UPDATE/DELETE/ALTER/DROP`。
- 普通方案和 backtest runner 不绕过 `shared.input_artifacts` 生成输入。
- **周频/月频方案的预测与回测路径不得 import 计算型周历函数（例如 `*_to_friday/*_to_monday/get_week_id_for_date`）来决定 week_id↔日期；week_id 必须读 `api_wind_date`（见 Step 3a）。**
- 运行路径不依赖 `/Users/.../Downloads`、`Desktop` 等外部绝对路径。

### Step 5: Unit Gate - 单元验证

至少覆盖:

- core 函数接收 DataFrame 后能返回算法原生结果。
- adapter 调用正确的 `build_daily_input_artifact()` 或 `build_weekly_input_artifact()`。
- `PredictionRecord.scheme_id/horizon/target_tenor/predict_date/target_date/predicted_direction` 与 `config.yaml` 一致。
- `PredictionRecord.extra` 包含 `input_artifact_path` 和 `input_artifact_source`。
- 周频方案额外校验 `feature_week_id/target_week_id/feature_date/target_date`。

### Step 6: Dry-run Gate - 本地 dry-run，不写库

先用算法环境直接跑入口:

```bash
conda run -n forecast_env python -m scheduler.scheme_runner \
  --scheme-id t1_lgbm_spread_v2 \
  --predict-date 2026-06-01
```

验收点:

- 命令退出码为 0。
- stdout 是 JSON list。
- 返回条数等于本次有效 `tenors` 数量。
- 每条记录的 `scheme_id/horizon/target_tenor/target_date/predicted_direction` 都符合配置。
- dry-run 前后 `t_scheme_predictions` 和 `t_scheme_run_log` 行数不变。

### Step 7: Backtest Gate - 历史回测接入

如果新方案需要参与当前前端方案矩阵的历史排行，必须产出并写入独立 backtest 表。当前前端优先展示 `/api/backtests/factor-lab` 的最新 `framework_db_aligned` 回测结果，并统一显示为“当前DB对齐回测”；只写 `t_scheme_predictions` 的实盘结果，不会自动混入已有历史排行。

命名约束: `scheme_id` 只能表示真实方案；`benchmark_id` 只能表示历史基准批次；`data_source` 只能表示数据口径。文件系统中运行期输入用 `backtest_artifacts/runtime_inputs/{scheme_id}/`，历史回测 artifact 用 `backtest_artifacts/backtests/{benchmark_id}/`，不得再新增 `model_muti_0529_daily` 这类混合命名。

历史回测至少记录三件事:

- 使用的数据口径: 原始 CSV、DB 生成 CSV、或 DB 直接取数。
- baseline 来源: 上游原始脚本、首次框架输出、或人工确认基准。
- 复现结论: 总样本数、准确率、上涨/下跌准确率、mismatch 数。

如果方案来源于上游脚本，原则上按历史复现链路处理:

```bash
python -m scripts.run_baseline --scheme-id <scheme_id>
python -m scripts.run_framework_repro --scheme-id <scheme_id> --algo-env forecast_env
```

新增方案如果还没有通用 backtest runner，需要先补 runner，再写入:

- `t_backtest_runs`
- `t_backtest_predictions`
- `t_backtest_monthly_metrics`

不要把历史回测结果写入 `t_scheme_predictions`。

周度方案示例:

```bash
PYTHONNOUSERSITE=1 conda run -n forecast_env python -m backtests.{scheme_id}_reproduction --no-persist
PYTHONNOUSERSITE=1 conda run -n forecast_env python -m backtests.{scheme_id}_reproduction
```

验收点:

- 回测写入只作用于新 `scheme_id` 对应 run。
- `/api/backtests/factor-lab` 返回 `frequency=weekly`，前端落到“周度”列。
- 周度明细行按 `feature_date` 所在月份归组，显示日仍可使用周六 `predict_date`；月度样本数必须与后端 `t_backtest_monthly_metrics` 一致。
- 方案保持 `paused`，直到最新特征周产出能力和 weekly live 写库验收完成。

### Step 8: API/前端只读验证

```bash
curl -s http://127.0.0.1:8100/api/targets
curl -s http://127.0.0.1:8100/api/backtests/factor-lab
curl -s "http://127.0.0.1:8100/api/metrics/t1_lgbm_spread_v2?tenor=10Y"
```

验收点:

- `/api/targets` 能看到新 Y 标的的 `target_code/display_name/status`。
- `/api/backtests/factor-lab` 能返回参与历史排行的新方案；如果只是 live 方案，`/api/metrics/{scheme_id}` 能返回月度指标、汇总指标和逐日样本。
- 还没有 actuals 的未来目标日可以暂时无准确率；这不是接入失败。
- API 只读验收优先使用不会同步 registry 的接口；保护性核验时不要把 `/api/schemes` 当作纯只读探针。

打开:

```text
http://127.0.0.1:8100/
```

验收点:

- 新方案出现在对应任务格子下，例如 `10Y国债活跃 · T+1`。
- 同一个任务格子下可以同时看到多个候选方案。
- 方案排行的整体准确率、上涨准确率、下跌准确率按样本级聚合。
- 切换排行指标时，任务格子最优指标同步变化。

如果前端没有出现，优先检查:

1. `config.yaml.status` 是否为 `active`，或是否已有 `t_backtest_*` 历史回测结果。
2. `/api/schemes` 是否能看到方案。
3. 是否已经写入实盘预测或历史回测结果。
4. 若已有历史回测结果，当前矩阵优先读取 backtest API，新方案需要写入 backtest 表才会参与历史排行。
5. 周度方案是否返回 `frequency=weekly` 或 `horizon=6`；前端据此映射到“周度”列。

### Step 9: Live Gate - 手动写库验证

dry-run 和回测 gate 通过后，才能在明确授权下执行单方案写库。不要用 broad scheduler run-once 或 `--include-paused` 作为 live 验证入口。需要通过调度器写库时，先把该方案 `status` 改为 `active`，再执行一次单方案调度器写库:

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -m scheduler.executor \
  2026-06-01 \
  --scheme-id t1_lgbm_spread_v2 \
  --algo-env forecast_env
```

验收 SQL:

```sql
SELECT scheme_id, target_tenor, horizon, predict_date, target_date,
       predicted_direction, confidence, model_version
FROM t_scheme_predictions
WHERE scheme_id = 't1_lgbm_spread_v2'
ORDER BY predict_date DESC, target_tenor;

SELECT scheme_id, run_date, status, duration_sec, error_msg
FROM t_scheme_run_log
WHERE scheme_id = 't1_lgbm_spread_v2'
ORDER BY id DESC
LIMIT 5;
```

如果结果不正确，先把 `status` 改回 `paused`，修复后重新 dry-run。

### Step 10: Activation - 启用调度

只有 Intake、Normalize、Input Gate、Static Gate、Unit Gate、Dry-run Gate、Backtest Gate、API/前端只读验证和 Live Gate 全部通过后，才允许进入 activation。周度方案还要确认 `schedule.cron` 与上游 weekly 首轮预测时间对齐。

### Step 11: Documentation - 文档留痕

每次新方案合入前，必须更新:

- `docs/CURRENT_STATUS.md`: 当前状态、run_id、样本数、是否 active。
- `docs/TEST_PLAN.md`: 已通过的 gate 和剩余观察项。
- `docs/HISTORICAL_REPRODUCTION.md`: 需要参与历史排行的方案，记录回测口径和结果。
- `docs/SCHEME_ONBOARDING_SOP.md`: 仅当 SOP 本身变化时更新；普通方案接入不应临时修改规则。

## 7. 上线和调度

Activation Gate 完成后:

1. 保持 `config.yaml.status: active`；周度方案还要确认 `schedule.cron` 与上游 weekly 首轮预测时间对齐。
2. 重启 scheduler，让 launchd 进程读取最新方案文件:

```bash
launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.scheduler
```

3. 如后端 Python 代码有变更，再重启 backend:

```bash
launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.backend
```

4. 观察下一次调度后的运行日志:

```sql
SELECT scheme_id, run_date, status, error_msg, created_at
FROM t_scheme_run_log
WHERE scheme_id = 't1_lgbm_spread_v2'
ORDER BY id DESC
LIMIT 10;
```

## 8. 回滚策略

如果新方案上线后异常:

1. 先把 `config.yaml` 改为 `status: paused`。
2. 重启 scheduler。
3. 保留已经写入的预测和日志，不直接删除，便于追溯。
4. 如误写入明显错误的预测数据，先导出待删除记录并确认范围，再执行 SQL 清理。

禁止使用 `git reset --hard` 或直接回滚整库数据来处理单个方案问题。

## 9. 验收清单

新增方案合入前必须确认:

- [ ] `scheme_id` 与目录名一致，且没有复用旧方案 ID。
- [ ] `name` 能表达算法来源、预测长度、模型类型和版本。
- [ ] `config.yaml` 可被 `scheduler.discovery` 发现。
- [ ] `predict.py` 暴露 `run(predict_date: str) -> list[PredictionRecord]`。
- [ ] dry-run 成功，输出 JSON list。
- [ ] 手动写库成功，`t_scheme_predictions` 条数符合预期。
- [ ] `t_scheme_run_log` 有成功记录。
- [ ] `/api/schemes` 和 `/api/metrics/{scheme_id}` 返回正常。
- [ ] 如需参与历史排行，backtest 表已写入并在前端对应任务格子可见。
- [ ] 如为周度方案，live adapter 与历史 backtest runner 都通过 `build_weekly_input_artifact()` 生成算法输入。
- [ ] 文档更新: 当前状态、方案说明、历史回测结论或测试记录。
- [ ] Git 提交包含代码、配置和文档。

## 10. 常见问题

### 同一个 T+1、5Y 任务能有多个方案吗？

可以。隔离键是 `scheme_id + target_tenor + horizon` 的业务组合。当前数据库实盘表的唯一键是 `(scheme_id, target_tenor, predict_date)`，因此要求一个 `scheme_id` 固定一个 `horizon`。

### 新方案只改 `name` 可以吗？

如果只是展示名称更清楚，可以只改 `name`。如果算法、特征集合、训练窗口或预测口径变了，应新建 `scheme_id`，避免历史结果混在一起。

### 算法能不能自己读写数据库？

读数据库可以，但建议由 adapter 层集中处理。写预测结果不可以，必须返回 `PredictionRecord`，由调度器统一写库。

### 什么时候需要改前端？

普通新增方案不需要改前端。只有新增预测长度、展示新的 Y 期限、或改变矩阵交互逻辑时，才需要改前端。
