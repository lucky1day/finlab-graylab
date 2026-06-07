# 新增预测方案 SOP

**更新日期**: 2026-06-06  
**适用范围**: 在 `bond-factor-lab` 中新增一个可调度、可写库、可在前端方案矩阵中对比的预测方案。

## 1. 核心原则

新增方案时必须先区分两个概念:

- **任务格子**: `Y标的 + 预测长度`，例如 `10Y国债活跃 · T+1`。这是前端筛选和排行的格子。
- **方案实例**: 一个具体 `scheme_id`，例如 `t1_daily`、`t1_lgbm_spread_v2`。同一个任务格子下允许多个方案实例并存排行。

当前约定:

- 一个 `scheme_id` 只对应一个 `horizon`。同一算法如果同时做 T+1 和 T+5，应拆成两个方案目录。
- 当前已接入 `daily` 频率的 `t1_daily` / `t5_daily`，以及 `weekly` 频率的 `weekly_10y_d_overlay` active live 方案。新增周度方案进入 live 调度前，必须先确认周度目标日规则、actuals 对齐规则、最新特征周产出能力，以及调度时间与上游 weekly 首轮预测时间对齐。
- `scheme_id` 一旦写入数据库就视为稳定 ID，不要随意改名；展示名变更只改 `name`。
- 算法核心逻辑放在 `core/` 或独立模块里，`predict.py` 只做框架适配、输入准备和输出转换。
- 方案不能直接写 `t_scheme_predictions`；统一由 `scheduler.executor` 写库，保证运行日志和 UPSERT 口径一致。
- Y 标的展示名由数据库 `t_target_registry` 管理，`target_tenor` 只作为内部稳定 key。
- 新方案默认先用 `status: paused` 验证；通过 dry-run、手动写库和 API 检查后再改为 `active`。

## 2. 命名规范

`scheme_id` 使用小写 snake_case，建议包含预测长度、模型或特征版本:

| 类型 | 示例 | 说明 |
|------|------|------|
| 基准复现 | `t1_daily` | 已接入的 T+1 0529 原始基准 |
| 新 T+1 方案 | `t1_lgbm_spread_v2` | T+1、LightGBM、利差增强、v2 |
| 新 T+5 方案 | `t5_lgbm_macro_v1` | T+5、LightGBM、宏观因子版本 |
| 新周度方案 | `weekly_10y_d_overlay` | 周六预测下周最后交易日 vs 本周最后交易日 |

不要把 `scheme_id` 命名成 `t1_5y`、`t5_10y` 这类只描述任务格子的名字。期限范围由 `tenors` 字段管理；方案身份由算法来源、特征集合和版本定义。

展示名 `name` 要比 `scheme_id` 更可读，建议包含来源、预测长度、模型类型和版本，例如:

```yaml
name: "T1-LGBM利差增强-v2"
```

文件命名规则:

- Python 模块、测试、脚本统一使用小写 `snake_case.py`，例如 `latest_prediction.py`、`check_weekly_10y_readiness.py`。
- 方案目录必须等于 `scheme_id`，统一小写 snake_case，例如 `schemes/weekly_10y_d_overlay/`。
- `backtests/` 下的回测 runner 必须带范围，不使用 `reproduction.py` 这类泛名；日频批次用 `daily_0529_reproduction.py`，单方案周频用 `{scheme_id}_reproduction.py`，例如 `weekly_10y_d_overlay_reproduction.py`。
- `scripts/` 下的命令必须使用“动作 + 对象 + 目的”命名，例如 `verify_backtest_reproduction.py`、`generate_daily_data_diff_report.py`、`compare_weekly_wind_export.py`。
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

    # 1. 准备算法输入: 从 bond_db 取数，或生成算法需要的临时 CSV。
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
                "data_source": "bond_db",
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
- `extra` 建议保留 `feature_date`、数据来源、核心模型版本、输入行数等排查字段。

## 6. 接入步骤

### Step 1: 确认方案身份

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

### Step 2: 新建方案目录

```bash
mkdir -p schemes/t1_lgbm_spread_v2/core
touch schemes/t1_lgbm_spread_v2/__init__.py
touch schemes/t1_lgbm_spread_v2/core/__init__.py
```

创建 `config.yaml` 和 `predict.py`。如果算法来自上游原始代码，把原始核心逻辑放入 `core/`，adapter 只负责输入输出。

输入文件特殊要求: 预测 adapter 不应自行从 DB 拼输入 DataFrame，也不应自行决定输入文件路径。所有方案必须先通过 `shared.input_artifacts` 生成输入 CSV，再读取该 CSV 给算法；日频使用 `build_daily_input_artifact()`，周频使用 `build_weekly_input_artifact()`。运行期 CSV 统一写入 `backtest_artifacts/runtime_inputs/{scheme_id}/`；原始 `data_service.py` 只读不改。

### Step 3: 本地 dry-run，不写库

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

### Step 4: 手动写库验证

dry-run 通过后，把 `status` 改为 `active`，再执行一次调度器写库:

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

### Step 5: API 验证

```bash
curl -s http://127.0.0.1:8100/api/schemes
curl -s "http://127.0.0.1:8100/api/metrics/t1_lgbm_spread_v2?tenor=10Y"
```

验收点:

- `/api/schemes` 能看到新方案的 `name/horizon/tenors/status`。
- `/api/targets` 能看到新 Y 标的的 `target_code/display_name/status`。
- `/api/metrics/{scheme_id}` 能返回月度指标、汇总指标和逐日样本。
- 还没有 actuals 的未来目标日可以暂时无准确率；这不是接入失败。

### Step 6: 历史回测接入

如果新方案需要参与当前前端方案矩阵的历史排行，必须产出并写入独立 backtest 表。当前前端优先展示 `/api/backtests/factor-lab` 的最新 `framework_db_aligned` 回测结果，并统一显示为“当前DB对齐回测”；只写 `t_scheme_predictions` 的实盘结果，不会自动混入已有历史排行。

命名约束: `scheme_id` 只能表示真实方案；`benchmark_id` 只能表示历史基准批次；`data_source` 只能表示数据口径。文件系统中运行期输入用 `backtest_artifacts/runtime_inputs/{scheme_id}/`，历史回测 artifact 用 `backtest_artifacts/backtests/{benchmark_id}/`，不得再新增 `model_muti_0529_daily` 这类混合命名。

历史回测至少记录三件事:

- 使用的数据口径: 原始 CSV、DB 生成 CSV、或 DB 直接取数。
- baseline 来源: 上游原始脚本、首次框架输出、或人工确认基准。
- 复现结论: 总样本数、准确率、上涨/下跌准确率、mismatch 数。

如果方案来源于上游脚本，原则上按历史复现链路处理:

```bash
conda run -n forecast_env python scripts/verify_backtest_reproduction.py
```

新增方案如果还没有通用 backtest runner，需要先补 runner，再写入:

- `t_backtest_runs`
- `t_backtest_predictions`
- `t_backtest_monthly_metrics`

不要把历史回测结果写入 `t_scheme_predictions`。

周度方案示例:

```bash
PYTHONNOUSERSITE=1 conda run -n forecast_env python -m backtests.weekly_10y_d_overlay_reproduction --no-persist
PYTHONNOUSERSITE=1 conda run -n forecast_env python -m backtests.weekly_10y_d_overlay_reproduction
```

验收点:

- 回测写入只作用于新 `scheme_id` 对应 run。
- `/api/backtests/factor-lab` 返回 `frequency=weekly`，前端落到“周度”列。
- 方案保持 `paused`，直到最新特征周产出能力和 weekly live 写库验收完成。

### Step 7: 前端确认

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

## 7. 上线和调度

验证完成后:

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
