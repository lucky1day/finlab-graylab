# 新增预测方案 SOP

**更新日期**: 2026-06-14
**适用范围**: 在 `bond-factor-lab` 中新增一个可调度、可写库、可在前端方案矩阵中对比的预测方案。

> 强约束 harness 总纲见 [HARNESS_ARCHITECTURE.md](../HARNESS_ARCHITECTURE.md)。预测日期和实盘阶段语义见 [PREDICTION_SEMANTICS.md](../PREDICTION_SEMANTICS.md)。本 SOP 是执行入口；任何新增方案都必须按 harness gate 推进，不能临时绕过公共输入层、回测层或调度写库边界。
>
> 新增方案入口先读 [SCHEME_ONBOARDING_T0.md](SCHEME_ONBOARDING_T0.md)，再按本文执行。本文是「改造进系统」段的人类执行手册。
>
> **新增方案开工前必须先读 [SCHEME_ONBOARDING_T0.md](SCHEME_ONBOARDING_T0.md)**。T0 是 daily / weekly 通用的硬约束范式；本文负责展开具体步骤和命令。

## 1. 核心原则

新增方案时必须先区分两个概念:

- **任务格子**: `Y标的 + 预测长度`，例如 `10Y国债活跃 · T+1`。这是前端筛选和排行的格子。
- **方案实例**: 一个具体 `scheme_id`，例如 `t1_daily`、`t1_lgbm_spread_v2`。同一个任务格子下允许多个方案实例并存排行。

当前约定:

- 一个 `scheme_id` 只对应一个 `horizon`。同一算法如果同时做 T+1 和 T+5，应拆成两个方案目录。
- 当前已接入并 active 的方案包括日频 `t1_daily` / `t5_daily`，以及周频 `weekly_5y_direct_0529` / `weekly_7y_cross_d_overlay_0529` / `weekly_10y_d_overlay_0529`。新增周度方案进入 live 调度前，必须先确认周度目标日规则、actuals 对齐规则、最新特征周产出能力，以及调度时间与上游 weekly 首轮预测时间对齐。
- `scheme_id` 一旦写入数据库就视为稳定 ID，不要随意改名；展示名变更只改 `name`。
- 算法核心逻辑放在 `core/` 或独立模块里，`predict.py` 只做框架适配、输入准备和输出转换。
- 方案不能直接写 `t_scheme_predictions`；统一由 `scheduler.executor` 写库，保证运行日志和 UPSERT 口径一致。
- Y 标的展示名由数据库 `t_target_registry` 管理，`target_tenor` 只作为内部稳定 key。
- 新方案默认先用 `status: paused` 验证；通过 dry-run、手动写库和 API 检查后再改为 `active`。
- 当前 harness 设计已定，后续新增方案必须通过 Intake -> Normalize -> Input Gate -> Static Gate -> Unit Gate -> Dry-run Gate -> Backtest Gate -> Live Gate -> Activation -> Documentation；没有 gate 证据时不得宣称方案完成或 live ready。

### 1.0 关键数据口径约定（2026-06-10 修订）

> 以下约定是本次框架审查后强化的核心规则。所有代码层（后端查询、前端展示、回测计算、去重逻辑）和所有方案（现有及新增）必须统一遵守，不能出现口径不一致。

**规则一：三类日期字段只允许一种业务解释**
- `predict_date` = 信号发出日 / 调度运行日。
- `feature_date` = 数据截止日 / 预测站位日，是前端和业务统一使用的标准字段。
- `target_date` = 验证目标日，用于展示、去重、actual join 和月度统计归属。
- `anchor_date` 不作为业务字段使用；如果方案内部或审计 extra 保留，必须等于 `feature_date`。

**规则二：展示与分组只看 target_date，不用 predict_date**
- 前端的每日明细按 `target_date`（交易日）展示和分组。
- 月度指标按 `target_date` 的月份计算。
- 回测前端月度指标以 `t_backtest_predictions` 明细按 `target_date` 月份动态聚合为准；不得新增、读取或写入独立的回测月度指标汇总表。
- 后端 `_prediction_point_date` 去重键必须用 `target_date`。
- 后端 `_scheme_metric_month` 月份归属必须用 `target_date`。
- `predict_date` 只用于调度执行日志和 `extra` 中的记录字段，不参与任何展示/分组/去重。
- 历史回测输出样本的统一起点例外地由 `predict_date >= 2025-01-01` 控制；这是为了候选方案排行样本口径一致，不改变月度指标仍按 `target_date` 归属。

**违反后果示例**（2026-06-10 实际踩坑）：
1. 后端 `_scheme_metric_month` 用 predict_date → 月度指标显示 5月，明细在 6月，对不上。
2. 前端 `detailGroupMonth` 用 predict_date → 明细按预测日展示，用户需要往前推一天才能知道预测的是哪天。
3. 后端 `_prediction_point_date` 用 predict_date → 同一天发出的多笔预测（不同 target）被错误合并。
4. 回测 `_metric_month` 用 predict_date → 月度指标与前端明细口径不一致，样本数对不上。
5. 新增方案或修改方案时必须同时检查这四处是否统一使用 target_date。

**规则三：唯一键用 `(scheme_id, target_tenor, horizon, target_date)` + UPSERT**
- 禁止使用 `run_id` 作为唯一键组成部分。
- 禁止使用 `predict_date` 作为唯一键组成部分。
- 同一 `scheme_id + target_tenor + horizon + target_date` 只能有一条预测记录；新预测覆盖旧的。
- 不需要 serving pointer 表：UK 自身保证唯一性，后端查询直接读 `t_scheme_predictions`，无需再 JOIN `t_scheme_serving_pointer`。

**违反后果示例**：
1. 旧 UK 用 `(scheme_id, target_tenor, predict_date, run_id)` → 同一 predict_date 的多笔预测（不同 run_id）都可以存在，前端出现重复行。
2. Serving pointer 被覆盖 → 旧 prediction（不同 target_date）因指针指向新 run 而消失。

**规则四：周度实盘预测不依赖目标周源数据是否存在**
- 周六执行预测时，下一周的数据可能尚未进入 `api_wind_weekly`。算法必须能在特征周数据可用但目标周数据不可用的情况下生成预测。
- live/gray 的周频 artifact 必须传 `end_week=feature_week_id`、`as_of_date=feature_date`，确保输入只含 `feature_date` 及以前可见的周频原始行；不得为了让 target 周存在而把 `end_week` 放到未来周。
- `target_week_id/target_date` 必须由 DB 日历从 `feature_week_id` 推导到下一实际周及其最后交易日，不依赖目标周数据是否已进入 `api_wind_weekly`，也不得用公式 `feature_week_id + 1` 作为业务依据。

**规则五：CompareGate 的 `skipped` 只表示"对比没有发生"，不是新增方案的通过证据**
- 框架层允许 ActivationGate 兼容 `skipped`，是为了支持没有原始基准的纯框架内实验方案；这不是普通新增方案可以跳过源文件对比的许可。
- 只要方案来自原始脚本、原始输出文件或人工 benchmark，就必须设置 `backtest.benchmark_required: true`，四份 benchmark 文件齐全，并让 CompareGate 状态为 `passed`。
- 对新增 source-backed 方案，`skipped` 必须视为流程未完成，不能进入激活。

**规则六：灰度实盘起点和部署时间不是同一个概念**
- 灰度实盘也算实盘，但必须标识 `prediction_phase=gray_live`；正式 scheduler 自然发出的实盘标识 `prediction_phase=scheduled_live`。
- 灰度实盘观察起点按方案级 `target_date` 判定，当前 V28 批次为 `target_date >= 2026-06-01`。
- 历史回测只覆盖灰度起点之前的 target；实盘区间通过 `t_scheme_predictions` 和 `/api/metrics/{scheme_id}` 展示，并应能区分灰度与正式实盘。
- 前端“部署时间”来自 `t_scheme_registry.deployed_at`，语义是该业务方案挂载对应定时任务的日期；它只是展示字段，不参与回测截断、实盘回补范围、月份归属或唯一键计算。
- 前端不得再通过 hardcoded override、默认日期或 scheme_id 特判生成部署时间；如果 API/registry 缺 `deployed_at`，应作为注册数据问题处理，不能静默显示假日期。

**规则六补充：历史回测预测起点全平台统一为 2025-01-01**
- 参与历史排行的 daily/monthly 方案必须在 `config.yaml` 写 `backtest.start_date: "2025-01-01"`，并保证 runner 输出样本满足 `predict_date >= 2025-01-01`。
- 参与历史排行的 weekly 方案必须在 `config.yaml` 写 `backtest.predict_start_date: "2025-01-01"`；`start_week/end_week` 仍表示输入、训练和模型更新所需的历史周范围，可以早于 2025 年。
- 模型 warmup、训练样本、因子筛选、定期更新模型所需的数据可以早于 `2025-01-01`。禁止为了统一回测样本数而截断这些历史输入。
- 灰度实盘分界仍按方案级 `target_date` 起点；不要用 `predict_date` 或部署时间切 live/backtest 区间。

**规则七：灰度补齐只能使用 feature_date 及以前数据**
- 灰度补齐虽然是事后运行，但每条记录仍必须满足 `predict_date=T+1`、`feature_date=T`、`target_date=T+horizon`。
- 输入 artifact、辅助周/月映射、模型训练窗口都不得越过 `feature_date`。
- 当前 DB 可能已经拥有 `T+1` 或更晚数据；补齐逻辑必须显式以 `feature_date` 约束数据，不能只依赖当前 DB 最新状态。

**规则八：`confidence` 是算法输出数值的统一承接字段**
- `confidence` 用来承接原始算法已有的置信度、概率或分数；不是平台为模型重新生成的新信号。
- CompareGate 的 `max_confidence_abs_diff` 是 original/current benchmark 两侧 `confidence` 的最大绝对差。`1e-16` 量级属于浮点舍入误差，视为 0。
- 如果原始算法没有天然 `confidence`，必须在 source/current 两侧使用同一确定性映射，并在 `CURRENT_STATUS.md` 说明。

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

`config.yaml` 的 `scheme_id` 是算法执行身份，也就是 registry 中的 `base_scheme_id`。不要把它命名成 `t1_5y`、`t5_10y` 这类只描述任务格子的名字；期限范围由 `tenors` 字段管理，算法身份由来源、特征集合和版本定义。

平台同步 registry 时会按 `tenors` 拆成业务方案行，每行 `scheme_id = {base_scheme_id}__h{horizon}__{target_tenor}`。单标的算法也必须使用这个 composite registry ID，例如 `weekly_5y_direct_0529__h6__5Y`。前端、业务 API 和候选排行只认 `status='active'` 的 registry composite `scheme_id`；`paused` / `archived` 行只用于验证期管理或审计保留，不进入当前前端矩阵，不允许 trigger，也不允许 scheduler 新写入该 target。scheduler、harness、`PredictionRecord` 和 backtest 存储仍使用 base `scheme_id`。

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
  cron: "3 7 * * 1-5"
  timezone: "Asia/Shanghai"
entry_point: predict.run
status: paused
backtest:
  runner: backtests.t1_lgbm_spread_v2_reproduction
  start_date: "2025-01-01"  # daily/monthly 历史回测输出样本 predict_date 起点；weekly 改用 predict_start_date
```

字段要求:

| 字段 | 要求 |
|------|------|
| `scheme_id` | 必须与目录名完全一致 |
| `horizon` | 日度使用 `1` / `5`；当前周度使用 `6` 表示周六发出、下周最后交易日为目标日 |
| `tenors` | 内部稳定 key，当前前端展示为 `3Y国债活跃/5Y国债活跃/7Y国债活跃/10Y国债活跃`；`1Y` 可作为因子输入，但不作为当前展示目标 |
| `schedule.cron` | 当前日度 live 使用 `3 7 * * 1-5`；当前周度 live 使用 `30 11 * * 6` |
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
    feature_date = "2026-05-29"
    target_date = "2026-06-01"  # 必须由平台交易日历从 feature_date + horizon 推导。

    records.append(
        PredictionRecord(
            scheme_id=SCHEME_ID,
            target_tenor="10Y",
            horizon=HORIZON,
            predict_date=predict_date,
            feature_date=feature_date,
            target_date=target_date,
            predicted_direction=1,
            confidence=0.62,
            model_version="v2",
            extra={
                "feature_date": feature_date,
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
- `feature_date` 必须是一等字段，表示输入数据硬截止；不得只放在 `extra` 里让平台猜。
- `target_date` 必须由 `feature_date + horizon` 的平台日历规则推导，不能直接写成 `predict_date`；当前 live metrics 后端按 `horizon=1` 取 `direction_1d`，按 `horizon=5` 取 `direction_5d`，按周度 `horizon=6` 取 `t_scheme_weekly_actuals.direction_weekly`。新增周度方案 active 前仍需确认最新特征周数据完整并完成受控写库验收。
- `extra` 必含 `input_artifact_path` 和 `input_artifact_source`；`feature_date` 可作为审计副本保留但必须等于一等字段，周频另含 `feature_week_id/target_week_id/target_rule`。完整字段契约（机器可校验）见 [SCHEME_CONTRACT.md](../SCHEME_CONTRACT.md) §3。

## 6. Harness 入库流程

后续所有新方案都按以下 gate 顺序推进。旧的手动命令仍可作为每个 gate 的实现方式，但不能跳过 gate。

> 可执行 harness 的统一入口为 `python -m harness onboard {scheme_id} --stage all`；边界总纲见 [HARNESS_ARCHITECTURE.md](../HARNESS_ARCHITECTURE.md)。下表每个 Gate 落地后对应一条 `python -m harness gate <name>` 命令；本节裸 conda 命令是该 Gate 的底层实现。

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

预测 adapter 不应自行从 DB 拼输入 DataFrame，也不应自行决定输入文件路径。所有方案必须先通过 `shared.input_artifacts` 生成输入 CSV，再读取该 CSV 给算法；日频使用 `build_daily_input_artifact()`，周频使用 `build_weekly_input_artifact()`，月频使用 `build_monthly_input_artifact()`。底层数据导出统一由 `shared.data_service` 负责，运行期 CSV 统一写入 `backtest_artifacts/runtime_inputs/{scheme_id}/`；公共数据层逻辑不得在新增方案时临时改动。

周频 artifact 只接受 `start_week/end_week` 作为周范围过滤，并支持 `as_of_date` 作为 point-in-time 原始行截止；实盘和灰度补齐必须传 `end_week=feature_week_id`、`as_of_date=feature_date`。旧的 `end_date` 和 `include_daily_weekly_close_fallback` 参数不属于统一数据层口径，当前会显式报错，不能在新增方案中使用。

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
- `PredictionRecord.scheme_id/horizon/target_tenor/predict_date/feature_date/target_date/prediction_phase/predicted_direction` 与 `config.yaml` 和预测语义一致。
- `PredictionRecord.extra` 包含 `input_artifact_path` 和 `input_artifact_source`。
- 周频方案额外校验 `feature_week_id/target_week_id/feature_date/target_date`。

### Step 5a: Benchmark Sample 准备（为 CompareGate 提供对比基准）

CompareGate 需要四份逐方案 benchmark 文件来验证平台改造后的输出与原始算法是否一致。它们必须放在 `schemes/{scheme_id}/benchmarks/` 目录下；根目录 `benchmarks/{benchmark_id}/` 只用于保存批次级 canonical 输入归档，例如 `benchmarks/model_muti_0529/daily_output.csv`，不能把它当作逐方案 CompareGate baseline。

| 文件 | 内容 |
|------|------|
| `original_predictions_sample.csv` | 原始算法的预测样本。`benchmark_required=true` 时必须使用严格字段：`feature_date,target_date,target_tenor,horizon,direction,confidence,label,is_correct`；周度还必须能保留或映射 `feature_week_id`。文件名虽保留 `sample`，内容应覆盖原始 benchmark 全量可比较行，不再只放 200 行抽样。 |
| `original_backtest_summary.json` | 原始算法的月度指标摘要 |
| `current_predictions_sample.csv` | 当前平台输出的预测样本（字段与 original 同口径，内容应一致） |
| `current_backtest_summary.json` | 当前平台的月度指标摘要（与 original 同口径，内容应一致） |

`confidence` 字段含义必须与原始算法一致：原始脚本如果输出概率/score，应映射到同一个数值；原始脚本没有置信度时，original/current 必须使用同一确定性代理值。benchmark 对齐的第一主语义是 source T 对齐平台 `feature_date`，不是对齐实盘 `predict_date`；月度指标、前端展示、回测/live 分区仍一律按 `target_date`。

`benchmark_required=true` 的方案采用严格主键 `feature_date + target_date + target_tenor + horizon`。缺少 `feature_date`、`target_date`、`target_tenor`、`horizon`、`direction`、`confidence`、`label`、`is_correct` 任一字段或值时，CompareGate 必须 fail-closed。旧列名 `predict_date/date/tenor` 只允许在历史说明中解释，不允许作为新增 benchmark 的静默回退逻辑。

日频 0529 批次的 `t1_daily` / `t5_daily` 已使用受控脚本从原始算法回测口径重建严格 baseline：

```bash
conda run -n bond_factor_lab_service python scripts/rebuild_daily0529_scheme_benchmarks.py \
  --scheme-id t1_daily \
  --scheme-id t5_daily
```

注意：根目录 `benchmarks/model_muti_0529/daily_output.csv` 是上游批次输入归档，当前截到 `2026-05-28`；逐方案 benchmark 为了覆盖完整 2026-05 目标月，会通过 `shared.input_artifacts` 从 DB 补齐 `2026-05-29` 目标验证日。该补齐行只用于计算 `target_date=2026-05-29` 的 label/actual，不能把 source T / `feature_date` 推到未来，也不能作为 live 预测输入截止日。

V28 `daily_5y_2_v28` 属于 test-window 敏感方案，benchmark current 侧必须由平台共享 inference helper 生成，不能从 source 文件复制：

```bash
conda run -n bond_factor_lab_service python scripts/rebuild_v28_scheme_benchmark.py --n-workers 10
```

该脚本把 source `date/T` 对齐为平台 `feature_date`，并用 `schemes.daily_5y_2_v28.inference` 生成 current rows；任一 strict key 缺失、方向不一致、`target_date` 不一致或 `confidence` 超容差都会 fail-closed。

如果方案已有历史回测数据写入 `t_backtest_*` 表，可以用以下脚本从数据库提取样本。必须显式指定 `--run-id`，或同时指定 `--scheme-id --benchmark-id --data-source`，避免把多个 benchmark 或旧 run 混成一份 CompareGate 基准：

```bash
conda run -n bond_factor_lab_service python scripts/generate_benchmark_samples.py \
  --scheme-id daily_5y_2_v28 \
  --benchmark-id v28_daily_5y_2 \
  --data-source framework_db_aligned

# 或者显式锁定某一次 run：
conda run -n bond_factor_lab_service python scripts/generate_benchmark_samples.py --run-id <confirmed_run_id>
```

该脚本根据 `v_latest_backtest_run` 的 canonical latest success 语义提取单一 run，生成四份 benchmark 文件；若无法唯一定位 run，会 fail-closed 并拒绝写文件。

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
- dry-run 前后所有保护表（`t_scheme_predictions`、`t_scheme_run_log` 等）行数不变。
- **周度方案额外检查**：`target_date` 必须是 DB 日历中 `feature_week_id` 下一实际周的最后一个交易日，不是当前周。live/gray artifact 必须传 `end_week=feature_week_id`、`as_of_date=feature_date`；目标周数据尚未入库时仍要能预测，但不能读取 feature 周之后的周频原始行（参见 PITFALLS 坑 4）。

### Step 7: Backtest Gate - 历史回测接入

如果新方案需要参与当前前端方案矩阵的历史排行，必须产出并写入独立 backtest 表。当前前端优先展示 `/api/backtests/factor-lab` 的最新 `framework_db_aligned` 回测结果，并统一显示为“当前DB对齐回测”；只写 `t_scheme_predictions` 的实盘结果，不会自动混入已有历史排行。

全平台历史回测输出样本必须从 `predict_date >= 2025-01-01` 开始。daily/monthly runner 用 `backtest.start_date: "2025-01-01"` 表达这个预测发出起点；weekly runner 用 `backtest.predict_start_date: "2025-01-01"` 表达，且必须在生成 row 时按 `predict_date` 过滤。weekly 的 `start_week/end_week`、daily 的输入起点、模型 warmup 和训练/筛选窗口可以更早，只要最终输出样本起点对齐即可。

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

新增方案如果还没有通用 backtest runner，需要先补 runner。runner 放在 `backtests/` 下，命名规则为 `{scheme_id}_reproduction.py`。runner 继承 `backtests._base_runner.BaseDailyBacktestRunner`（日频）或参照 `backtests.weekly_5y_direct_0529_reproduction` 的格式（周频）。

**日频 runner 最小模板**：

```python
"""{scheme_id} 历史回测复现。"""
from pathlib import Path
from backtests._base_runner import BacktestSpec, BaseDailyBacktestRunner

SPEC = BacktestSpec(
    benchmark_id="{benchmark_id}",
    scheme_id="{scheme_id}",
    canonical_csv=Path("benchmarks/{benchmark_id}/daily_output.csv"),
    target_columns=("TB0YWI0C",),  # 方案关注的收益率列
    start_date="2025-01-01",
    end_date="YYYY-MM-DD",
)

class MyRunner(BaseDailyBacktestRunner):
    def predict_rows(self, daily_df, *, n_jobs=4):
        # 调用 schemes/{scheme_id}/core 下的算法，返回预测行列表
        ...

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args()
    runner = MyRunner(SPEC)
    output = runner.run_framework_db_aligned(persist=not args.no_persist)
    print(output.summary)
```

**周频 runner** 参照 `backtests/weekly_5y_direct_0529_reproduction.py`。周频 runner 不继承 `BaseDailyBacktestRunner`，而是直接导入方案的 core 算法循环逐周预测。Runner 必须：
- 从 `shared.input_artifacts.build_weekly_input_artifact()` 获取输入。
- 使用 `shared.calendar_service` 查询 `week_id`（禁止日历公式）。
- 声明 `backtest.predict_start_date: "2025-01-01"`，并按 `predict_date >= 2025-01-01` 过滤输出样本；不要把早期 `start_week` 误删，因为那通常是训练和模型更新窗口。
- 调用 `backtests.repository.create_backtest_run` / `replace_backtest_predictions` 写库（`--no-persist` 时跳过写库）。前端 canonical 月度指标由 `/api/backtests/factor-lab` 从 `t_backtest_predictions` 动态聚合；runner 不得写入独立的月度指标汇总表。
- 默认优先使用 point-in-time 回测；如果源方案只能按 source-original batch reproduction 复现，必须在 `PREDICTION_SEMANTICS.md` 和 `PITFALLS_2026-06-10.md` 记录原因，并在 persist 前校验已有 original benchmark 覆盖区间逐行一致。该例外只允许用于历史回测，不得改变 gray/live/scheduled live adapter 的 `feature_date/as_of_date` 截止规则。
- 已批准的 `weekly_5y_direct_0529` / `weekly_7y_cross_d_overlay_0529` / `weekly_10y_d_overlay_0529` 历史回测是 source-original batch reproduction 例外：runner 一次性调用 core 生成完整历史预测，再按 DB 日历构造平台 rows；summary 必须写 `backtest_mode=original_batch_reproduction`、`backtest_point_in_time=false`、`historical_backtest_exception=true`，并写入 `original_benchmark_validation`。
- 周频候选方案之间的样本总数不要求强行一致；runner 只能写入 core 真实产出的有效预测行。若某个日历周因为规则信号为 0、NaN、无效标签或 source core 的 inner join 被排除，不能补写空预测来凑齐样本数；必须在状态文档中记录缺失的 `feature_week_id` 和 core 过滤原因。
- 新增方案不得直接套用上述例外。只有当源 benchmark 明确是 batch reproduction，且逐点 PIT 会改变原始评价对象时，才可以申请同类例外；批准后必须提供 benchmark 覆盖区间逐行一致证明，至少覆盖 `feature_date/source_t`、`target_date`、`direction/predicted_direction`、`confidence`、`label/is_correct`，其中 source T 必须对齐平台 `feature_date`，`confidence` 只允许浮点舍入误差。
- 如果源算法对 test window 敏感（例如 `daily_5y_2_v28` 的月度 test window 会参与 ensemble / signal selection），必须把窗口计算和 core 调用抽成方案内共享 inference helper。adapter、dry-run、gray/live 补齐、benchmark current 生成和 backtest runner 都必须调用同一 helper；禁止 live 使用月度窗口、backtest 使用连续窗口，或反过来。
- 对这类方案，历史回测依然必须满足 `predict_date=feature_date`、`target_date` 由平台日历计算、`target_date < 灰度实盘起点`。窗口敏感只说明“如何调用算法 core”，不改变平台日期语义。

回测写库后入库:

周度方案示例:

```bash
PYTHONNOUSERSITE=1 conda run -n forecast_env python -m backtests.{scheme_id}_reproduction --no-persist
PYTHONNOUSERSITE=1 conda run -n forecast_env python -m backtests.{scheme_id}_reproduction
```

验收点:

- 回测写入只作用于新 `scheme_id` 对应 run。
- 输出样本的最早 `predict_date` 不早于 `2025-01-01`；训练、warmup、筛因子和模型更新历史窗口可以早于该日期。
- `/api/backtests/factor-lab` 返回 `frequency=weekly`，前端落到“周度”列。
- 周度明细行、月度指标、去重和展示月份一律按 `target_date` 归组；`feature_date` 只用于追溯输入窗口，`predict_date` 只用于调度日志和运行记录。
- 如果方案已有灰度实盘起点（当前为 `target_date >= 2026-06-01`），历史回测 runner 必须排除该实盘区间（即回测 `target_date < 2026-06-01`），避免前端同一个 target 月同时出现 backtest 与 live 两行；不要用部署时间或 `predict_date` 截断历史回测。
- 如果删除错误口径的旧回测 run，必须使用受控脚本显式指定 `scheme_id + run_id`，先 dry-run 打印命中行数，再 apply；不得手写散落 SQL 删除。
- 方案保持 `paused`，直到最新特征周产出能力和 weekly live 写库验收完成。

### Step 8: API/前端只读验证

```bash
curl -s http://127.0.0.1:8100/api/targets
curl -s http://127.0.0.1:8100/api/schemes
curl -s http://127.0.0.1:8100/api/backtests/factor-lab
curl -s "http://127.0.0.1:8100/api/metrics/t1_lgbm_spread_v2__h1__10Y"
curl -s "http://127.0.0.1:8100/api/predictions?scheme_id=t1_lgbm_spread_v2__h1__10Y&limit=5"
```

验收点:

- `/api/targets` 能看到新 Y 标的的 `target_code/display_name/status`。
- `/api/schemes` 是纯读接口，只返回 `status='active'` 的 `t_scheme_registry` rows；每行只有一个 `target_tenor`，没有 `tenor/tenors`。
- `/api/backtests/factor-lab` 只把 latest run 映射到 active registry rows；返回的 `scheme_id` 必须为 active registry composite ID，registry 缺行、`paused` 或 `archived` 不得展示。
- 如果只是 live 方案，`/api/metrics/{registry_scheme_id}` 能返回月度指标、汇总指标和逐日样本；`/api/metrics/{base_scheme_id}`、`paused/archived` registry ID 或 `?tenor=...` 都不是合法入口。
- `/api/predictions?scheme_id={registry_scheme_id}` 能返回该业务方案的底层预测明细；`/api/predictions?scheme_id={base_scheme_id}`、无 `scheme_id` 或 `?tenor=...` 都不是合法入口。
- 还没有 actuals 的未来目标日可以暂时无准确率；这不是接入失败。
- registry 同步只在后端启动或受保护的 `POST /api/admin/registry/sync` 中发生；普通 GET 验收不得产生写库副作用。
- 月度指标必须区分 `samples` 与 `metric_samples`：`samples` 是样本总数，包含预测为“平”的交易日或预测周；`metric_samples` 是所有准确率、precision、recall 指标的分母，只包含预测为“涨/跌”的有方向样本。
- 若月内存在 `predicted_direction=0`，前端准确率括号必须展示 `correct/metric_samples`，不得展示 `correct/samples`；上涨/下跌准确率和召回率也必须排除这些“平”样本。

打开:

```text
http://127.0.0.1:8100/
```

验收点:

- 新方案出现在对应任务格子下，例如 `10Y国债活跃 · T+1`。
- 同一个任务格子下可以同时看到多个候选方案。
- 方案排行的整体准确率、上涨准确率、下跌准确率按样本级聚合，但预测为“平”的样本不进入任何指标分母。
- 月度表“样本数”列仍包含预测为“平”的样本；整体准确率括号显示 `correct/metric_samples`。
- 每日/周度验证表中，预测为“平”的行结果列必须显示 `-`，不得显示 `×` 或 `✓`。
- 切换排行指标时，任务格子最优指标同步变化。
- 部署时间必须来自 `/api/schemes` 或 `/api/backtests/factor-lab` 返回的 `deployed_at`，并在真实候选排行 row 上验证；不得只直测 helper，也不得接受前端默认日期、override 或 mock 值混入真实展示。
- 如果刚改过 `frontend/aifin-shell.js` / `frontend/index.html` 后页面仍显示旧内容，第一时间提醒用户做浏览器强制刷新（macOS `Cmd+Shift+R`）或打开 DevTools 勾选 `Disable Cache` 后刷新，再继续排查 API/代码。

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

**Step 10a（强制，不允许遗漏）：激活前必须完成"源文件原始回测 vs 入库后回测"逐样本对比**

> **背景**（2026-06-10 实际遗漏案例）：`weekly_7y_cross_d_overlay_0529` 首次入库激活时跳过了该对比（CompareGate 因缺 benchmark 文件 SKIP，未阻断激活），事后才补做。该对比是验证"平台改造未改变算法行为"的唯一手段，跳过等于上线了未经验证的方案。

激活前必须满足以下全部条件，**任何一条不满足都不允许激活**：

1. **完成 [SCHEME_POST_ONBOARDING_TEST_SOP.md](SCHEME_POST_ONBOARDING_TEST_SOP.md) S3–S5**：
	   - S3：用入库前原始脚本（或静态基准文件）跑出基准预测序列。
	   - S4：用入库后的框架代码（同一数据接入层）跑出复现序列。
	   - S5：逐样本对比，原始算法 source T 必须对齐平台 `feature_date`，**`predicted_direction` 方向零容差**（差一个样本即不一致），浮点 `1e-9` 容差。
2. **benchmark 文件已落到 `schemes/{scheme_id}/benchmarks/`**（四份，见 Step 5a），且 `config.yaml` 中 `backtest.benchmark_required: true`。
3. **CompareGate 状态必须是 `passed`，不能是 `skipped`**。`skipped` 意味着对比没有发生——对于新增方案这是不可接受的（`skipped` 仅对无原始基准的纯框架内实验方案可接受，且需在 CURRENT_STATUS 中显式说明原因）。
4. 对比证据（matched 数、source T/feature_date 对齐口径、direction diff、confidence diff）写入 `docs/CURRENT_STATUS.md`；`confidence diff` 指 benchmark 两侧同一字段的浮点差异，不是新的模型指标。

执行顺序建议：Step 7（回测落库）→ Step 10a（源 vs 入库对比 + benchmark 文件）→ 重跑 `harness onboard --stage all` 确认 CompareGate passed → Step 10（激活）。

**Step 10b（强制，不允许遗漏）：激活后必须回补灰度实盘预测，覆盖 target_date 从灰度起点（2026-06-01）到当前**

> **背景**（2026-06-10 实际遗漏案例）：`weekly_7y_cross_d_overlay_0529` 激活后没有回补实盘预测，导致前端没有"实盘发出起点"分隔线，也缺少 6月6日预测的 06/12"待验证"行。所有方案在前端必须有连续的实盘观察序列，从灰度起点开始。灰度也算实盘，但必须标识为 `gray_live`。

回补规则：

1. **回补范围**：所有 `target_date >= 2026-06-01`（当前 V28 批次灰度观察起点）至今应当存在的实盘预测。后续方案使用方案级灰度起点，不写死全局日期。
   - 日频方案：每个交易日一条（从 6月1日 或激活日中较早者开始反推 predict_date）。
   - 周频方案：以 `target_date` 为准枚举应有目标周，再反推对应调度日；不要只从灰度起点之后的 `predict_date` 开始枚举。
   - 例：灰度起点为 2026-06-01 时，周度 2026-06 的第一条目标周是 `target_date=2026-06-05`，其预测发出日是上一轮周六 `predict_date=2026-05-30`；下一条才是 `predict_date=2026-06-06 -> target_date=2026-06-12`。
2. **predict_date 取调度日历上应当发出的日期**，允许早于灰度起点（只要其 `target_date` 落在灰度起点之后），不允许全部填当前日期。
3. **feature_date 是硬截止**：灰度补齐时必须证明 `feature_date=T`，且所有输入 artifact、辅助周/月映射和模型训练窗口均不越过 `feature_date`；禁止因为当前 DB 已有 `T+1` 或更晚数据而读入未来信息。
4. **阶段标识**：补齐记录必须标识为 `prediction_phase=gray_live`；正式 scheduler 自然发出的记录标识为 `prediction_phase=scheduled_live`。
5. **执行方式**：用 `scheduler.executor.execute_scheme(cfg, '<predict_date>', prediction_phase='gray_live')` 按时间顺序逐个补跑；命令行补跑则必须传 `--prediction-phase gray_live`。例如周度方案补 2026-06 首两周：

```bash
conda run -n bond_factor_lab_service python -c "
from scheduler.discovery import discover_schemes
from scheduler.executor import execute_scheme
schemes = list(discover_schemes())
cfg = [s for s in schemes if s.scheme_id == '<scheme_id>'][0]
result = execute_scheme(cfg, '2026-05-30', algo_env='forecast_env', prediction_phase='gray_live')
print(result)
result = execute_scheme(cfg, '2026-06-06', algo_env='forecast_env', prediction_phase='gray_live')
print(result)
"
```

6. **验收点**：
   - [ ] DB 中该方案的实盘预测 target_date 连续覆盖 2026-06-01 至今的全部应有周期。
   - [ ] 周频方案首个 6 月 target（如 2026-06-05）没有因为 `predict_date` 在 5 月（如 2026-05-30）而被漏掉。
   - [ ] 每条补齐记录有 `feature_date`，且前端/业务不依赖 `anchor_date`。
   - [ ] 灰度补齐记录可判定为 `prediction_phase=gray_live`，不与 `scheduled_live` 混淆。
   - [ ] 前端出现"实盘发出起点"分隔线；前端统一取该方案 live rows 的最小 `predict_date`，不再对周度方案按 target 月份反推。灰度区间与正式调度起点由 `phase_ranges` 展示。
   - [ ] 尚无 actuals 的 target 显示"待验证"（参考 5Y 周度方案的 06/12 行）。
   - [ ] 回补的预测在 `t_scheme_run_log` 有对应运行记录。

### Step 11: Documentation - 文档留痕

每次新方案合入前，必须更新:

- `docs/CURRENT_STATUS.md`: 当前状态、run_id、样本数、是否 active。
- `docs/CURRENT_STATUS.md`: 已通过的 gate、run_id、回测口径、API 验证、实盘回补和剩余观察项。
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
- [ ] `/api/schemes` 和 `/api/metrics/{registry_scheme_id}` 返回正常；registry ID 必须来自 `t_scheme_registry.scheme_id`。
- [ ] 如需参与历史排行，backtest 表已写入并在前端对应任务格子可见。
- [ ] 如为周度方案，live adapter 与历史 backtest runner 都通过 `build_weekly_input_artifact()` 生成算法输入。
- [ ] 文档更新: 当前状态、方案说明、历史回测结论或测试记录。
- [ ] Git 提交包含代码、配置和文档。

## 10. 常见问题

### 同一个 T+1、5Y 任务能有多个方案吗？

可以。前端/业务层的候选方案由 registry composite `scheme_id` 区分，例如 `t5_daily__h5__5Y` 和 `new_model__h5__5Y` 可以同时位于 `5Y国债活跃 · T+5`。实盘预测表内部仍按 base `(scheme_id, target_tenor, horizon, target_date)` 做唯一业务口径；`predict_date` 只用于调度日志和运行记录。一个 base `scheme_id` 仍要求固定一个 `horizon`，这样执行层、回测和 live 写库不会把不同预测长度混在一起。

### 新方案只改 `name` 可以吗？

如果只是展示名称更清楚，可以只改 `name`。如果算法、特征集合、训练窗口或预测口径变了，应新建 `scheme_id`，避免历史结果混在一起。

### 算法能不能自己读写数据库？

读数据库可以，但建议由 adapter 层集中处理。写预测结果不可以，必须返回 `PredictionRecord`，由调度器统一写库。

### 什么时候需要改前端？

普通新增方案不需要改前端。只有新增预测长度、展示新的 Y 期限、或改变矩阵交互逻辑时，才需要改前端。
