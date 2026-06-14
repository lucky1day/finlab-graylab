# 架构设计: Bond Factor Lab

**版本**: v1.1
**日期**: 2026-06-12

> 本文是**系统架构**（部署、DB schema、API 契约、数据流）。代码层面的分层、包依赖方向规则、运行时调用图与扩展模型见 [CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md)（代码架构主蓝图）。
> 预测日期与实盘阶段语义以 [PREDICTION_SEMANTICS.md](PREDICTION_SEMANTICS.md) 为准。

---

## 1. 系统架构图

```
┌─────────────────────────────────────────────────────────────┐
│                    Mac Studio (全部署)                        │
│                                                             │
│  ┌─────────────────┐     ┌────────────────────────────────┐ │
│  │   Scheduler     │     │   FastAPI Backend (:8100)      │ │
│  │  (APScheduler)  │     │                                │ │
│  │                 │     │  /api/schemes                  │ │
│  │  daily 07:03    │     │  /api/metrics/{scheme_id}      │ │
│  │  weekly 11:30   │     │  /api/predictions?scheme_id=...│ │
│  │  actuals jobs   │     │  /api/actuals                  │ │
│  │                 │     │                                │ │
│  │  ┌───────────┐  │     │                                │ │
│  │  │ discovery │  │     │  Static: native HTML/CSS/JS    │ │
│  │  │ executor  │  │     └──────────────┬─────────────────┘ │
│  │  └───────────┘  │                    │                   │
│  └────────┬────────┘                    │                   │
│           │                             │                   │
│           ▼                             ▼                   │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              MySQL bond_db                            │   │
│  │                                                      │   │
│  │  已有表:                                              │   │
│  │    api_wind_indicators_all (行情数据，自动更新)         │   │
│  │    api_wind_daily                                    │   │
│  │    t_pre_market_forecast (t1生产表)                   │   │
│  │                                                      │   │
│  │  新增表:                                              │   │
│  │    t_scheme_predictions (统一预测结果)                  │   │
│  │    t_scheme_actuals (实际方向)                         │   │
│  │    t_scheme_registry (方案注册)                        │   │
│  │    t_scheme_run_log (运行日志)                         │   │
│  │    t_target_registry (Y标的注册与展示名)                 │   │
│  │    t_backtest_* (历史复现结果，独立于实盘预测)             │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
         ▲
         │ iframe (http://mac-studio:8100/)
         │
┌────────┴────────────────────┐
│  panda_quantflow AIFin Lab  │
│  Shell (aifin-shell.js)     │
│  Nav: "实盘测试"             │
└─────────────────────────────┘
```

---

## 2. 数据流

### 2.0 预测日期与阶段

平台统一使用三类日期字段：

- `predict_date`: 信号发出日 / 调度运行日。
- `feature_date`: 数据截止日 / 预测站位日。
- `target_date`: 验证目标日，用于展示、去重、actual join 和月度统计归属。

`feature_date` 是前端和业务唯一标准数据截止字段；`anchor_date` 只允许作为方案内部变量或审计 extra。实盘预测分为 `gray_live` 和 `scheduled_live` 两个阶段，二者都属于实盘观察区；历史回测独立写入 `t_backtest_*`，不得从实盘预测表拼历史结果。

### 2.1 预测流程（日度07:03 / 周度11:30）

```
Scheduler启动
  → discovery.py 扫描 schemes/ 目录
  → 对每个active方案:
      → executor.py 检查是否交易日
      → 动态import scheme的predict.py
      → 调用 run(predict_date=today)
      → 返回 list[PredictionRecord]
      → 写入 t_scheme_predictions (UPSERT)
      → 写入 t_scheme_run_log
```

当前调度口径:

- 日度 `t1_daily` / `t5_daily`: 工作日 `07:03`（`3 7 * * 1-5`）。
- 周度 `weekly_5y_direct_0529` / `weekly_7y_cross_d_overlay_0529` / `weekly_10y_d_overlay_0529`: 周六 `11:30`（`30 11 * * 6`）。
- 旧周度方案 `weekly_10y_d_overlay` / `weekly_5y_direct_production` / `weekly_7y_cross_d_overlay` 已退役。

### 2.2 实际方向更新（每日08:30与19:00）

```
Scheduler在每日08:30和19:00触发日频actuals更新任务；非交易日由交易日检查跳过
  → 从 api_wind_indicators_all 读取最新收盘收益率
  → 计算各tenor的T+1和T+5方向
  → 写入 t_scheme_actuals (UPSERT)
```

周度 actuals 独立维护:

```
手动或调度触发 weekly_actuals 更新任务
  → 从 api_wind_daily 读取日频收益率
  → 从 api_wind_date 读取 rdate→week_id 和实际周序
  → 按 t_trade_calendar.trade_flag 取周内最后交易日
  → 目标周完整后计算下一周最后交易日 vs 本周最后交易日
  → 写入 t_scheme_weekly_actuals (UPSERT)
```

### 2.3 前端查询流程

```
用户选择: 任务格子(Y标的 + 预测长度) + 候选方案 + 月份范围
  → 前端通过 t_target_registry/API 获取 Y 标的展示名
  → 前端按任务格子筛选候选方案排行
  → 选中方案后调用 GET /api/metrics/{registry_scheme_id}?start_month=2025-01&end_month=2025-05
  → 后端 metric_service:
      → T+1/T+5 JOIN t_scheme_actuals
      → 周度 horizon=6 JOIN t_scheme_weekly_actuals
      → 按月分组计算准确率指标
      → 返回结构化JSON
  → 前端渲染表格和图表
```

### 2.4 历史复现流程

```
手动执行 backtests.daily_0529_reproduction
  → 读取 benchmarks/model_muti_0529/daily_output.csv
  → 通过 shared.input_artifacts 生成 DB 版 daily_output CSV 并读回
  → 日频内部调用 shared.data_service 生成 daily_output CSV
  → 按 canonical CSV 对齐列和日期
  → 运行原始 t5 run_all.py / 原始 t1 run_backtest(dry_run=True) 生成 baseline
  → 运行框架内 t1_daily / t5_daily 批量回测逻辑
  → 对比 baseline、framework-csv、framework-db
  → 写入 t_backtest_runs / t_backtest_predictions / t_backtest_monthly_metrics / t_backtest_reproduction_checks
  → 验证结果保留在后端 API、脚本和文档中，不新增前端验证结果页

手动执行 backtests.weekly_*_reproduction
  → 通过 shared.input_artifacts 生成 historical_backtest 周频 weekly_output CSV 并读回
  → 周频内部调用统一 shared.data_service
  → 运行 scheme core 中的周频算法逻辑
      → 按 target_date 所在月份生成月度指标
  → 写入对应 scheme_id 的 t_backtest_runs / t_backtest_predictions / t_backtest_monthly_metrics
  → 前端通过 /api/backtests/factor-lab 读取 canonical latest success run
```

---

## 3. 数据库Schema

### 3.1 t_scheme_predictions

```sql
CREATE TABLE t_scheme_predictions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id BIGINT DEFAULT NULL,
    scheme_version VARCHAR(64) DEFAULT NULL,
    scheme_id VARCHAR(64) NOT NULL,
    target_tenor VARCHAR(64) NOT NULL,
    horizon INT NOT NULL,
    predict_date DATE NOT NULL,
    target_date DATE NOT NULL,
    feature_date DATE DEFAULT NULL,
    prediction_phase ENUM('gray_live','scheduled_live') DEFAULT NULL,
    predicted_direction TINYINT NOT NULL COMMENT '1=涨, -1=跌, 0=平',
    confidence FLOAT DEFAULT NULL,
    model_version VARCHAR(64) DEFAULT NULL,
    extra JSON DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_scheme_tenor_target (scheme_id, target_tenor, horizon, target_date),
    INDEX idx_scheme_predict_date (scheme_id, predict_date),
    INDEX idx_target_date (target_date),
    INDEX idx_tenor_target_date (target_tenor, target_date),
    INDEX idx_scheme_predictions_phase (scheme_id, prediction_phase, predict_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

隔离口径:

- `target_tenor + horizon` 定义任务格子，例如 `5Y + T+1`；前端展示名通过 target label 映射为 `5Y国债活跃 · T+1`。
- `scheme_id` 定义具体方案实例，同一个任务格子下允许多个 `scheme_id` 并存排行。
- 业务唯一键按 `(scheme_id, target_tenor, horizon, target_date)` 保证同一目标点只有一条当前实盘预测；`run_id` / `scheme_version` 负责追溯每次运行来源。
- `feature_date` 是对外数据截止字段，`prediction_phase` 区分 `gray_live` 与 `scheduled_live`；旧 `extra.anchor_date` 只能作为审计副本，且必须等于 `feature_date`。

### 3.2 t_scheme_actuals

```sql
CREATE TABLE t_scheme_actuals (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenor VARCHAR(64) NOT NULL,
    trade_date DATE NOT NULL,
    close_yield DOUBLE NOT NULL,
    direction_1d TINYINT DEFAULT NULL COMMENT '1=涨, -1=跌, 0=平',
    direction_5d TINYINT DEFAULT NULL COMMENT '1=涨, -1=跌, 0=平',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_tenor_date (tenor, trade_date),
    INDEX idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3.3 t_scheme_weekly_actuals

```sql
CREATE TABLE t_scheme_weekly_actuals (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenor VARCHAR(64) NOT NULL,
    feature_week_id INT NOT NULL,
    target_week_id INT NOT NULL,
    predict_date DATE NOT NULL,
    feature_date DATE NOT NULL,
    target_date DATE NOT NULL,
    feature_yield DOUBLE NOT NULL,
    target_yield DOUBLE NOT NULL,
    direction_weekly TINYINT NOT NULL COMMENT '收益率方向: 1=上行/价格空, -1=下行/价格多, 0=平',
    price_signal VARCHAR(8) NOT NULL COMMENT '价格视角: 空/多/平',
    target_rule VARCHAR(128) NOT NULL,
    extra JSON DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_weekly_actual_predict (tenor, predict_date),
    INDEX idx_weekly_actual_target (tenor, target_date),
    INDEX idx_weekly_actual_week (feature_week_id, target_week_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3.4 t_scheme_registry

```sql
CREATE TABLE t_scheme_registry (
    id INT AUTO_INCREMENT PRIMARY KEY,
    scheme_id VARCHAR(64) NOT NULL UNIQUE,
    base_scheme_id VARCHAR(64) NOT NULL,
    name VARCHAR(128) NOT NULL,
    description TEXT,
    horizon INT NOT NULL,
    tenors JSON NOT NULL,
    frequency VARCHAR(32) NOT NULL DEFAULT 'daily',
    target_tenor VARCHAR(16) NOT NULL,
    schedule_cron VARCHAR(64) NOT NULL,
    schedule_timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
    status ENUM('active','paused','archived') NOT NULL DEFAULT 'active',
    deployed_at DATE DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_status (status),
    INDEX idx_scheme_registry_base (base_scheme_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`t_scheme_registry` 是唯一方案注册表，一行就是前端/业务定义的一个方案。`scheme_id` 是唯一业务身份，统一格式为 `{base_scheme_id}__h{horizon}__{target_tenor}`，例如 `t5_daily__h5__10Y`；不再存在第二套 `(base_scheme_id, frequency, horizon, target_tenor)` 唯一键。`base_scheme_id` 是算法目录 / config / scheduler / backtest 存储使用的执行身份，例如 `t5_daily`；同一个 base 算法预测多个 Y 标的时，registry 拆成多行，但 scheduler 仍只按 `base_scheme_id` 挂载一个执行任务。

`active` 是唯一前端/业务可见状态。`GET /api/schemes`、`GET /api/metrics/{scheme_id}`、`GET /api/backtests/factor-lab` 和手动 trigger 只接受 / 返回 `status='active'` 的 registry composite `scheme_id`。`paused` 用于验证期管理，`archived` 用于保留审计历史；二者不进入当前前端矩阵，不允许 trigger，也不允许 scheduler 新写入对应 target。

### 3.5 t_scheme_runs

```sql
CREATE TABLE t_scheme_runs (
    run_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    scheme_id VARCHAR(64) NOT NULL,
    scheme_version VARCHAR(64) DEFAULT NULL,
    run_type ENUM('dry_run','shadow','active','manual') NOT NULL DEFAULT 'active',
    prediction_phase ENUM('gray_live','scheduled_live') DEFAULT NULL,
    predict_date DATE NOT NULL,
    status ENUM('running','success','failed','partial','skipped') NOT NULL DEFAULT 'running',
    input_artifact_id VARCHAR(128) DEFAULT NULL,
    harness_run_id VARCHAR(64) DEFAULT NULL,
    records_expected INT DEFAULT NULL,
    records_returned INT DEFAULT NULL,
    records_written INT DEFAULT NULL,
    error_message TEXT DEFAULT NULL,
    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at DATETIME DEFAULT NULL,
    INDEX idx_scheme_date (scheme_id, predict_date),
    INDEX idx_status (status),
    INDEX idx_scheme_runs_phase (scheme_id, prediction_phase, predict_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`t_scheme_runs` 是一次执行的主审计表；`t_scheme_predictions.run_id` 指回这里。`prediction_phase` 在 run 和 prediction 两层同时落地，用于区分灰度实盘补齐与正式 scheduler 自然发出。物理列允许 `NULL` 只为兼容历史 dry-run/旧记录；当前 live 写库必须显式写入 `gray_live` 或 `scheduled_live`。

### 3.6 t_scheme_run_log

```sql
CREATE TABLE t_scheme_run_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    scheme_id VARCHAR(64) NOT NULL,
    run_date DATE NOT NULL,
    status ENUM('success','failed','skipped','partial') NOT NULL,
    duration_sec FLOAT DEFAULT NULL,
    error_msg TEXT DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_scheme_run (scheme_id, run_date),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`t_scheme_run_log` 保留轻量运行日志；正式追溯以 `t_scheme_runs` + `t_scheme_predictions.run_id` 为主。

### 3.7 t_target_registry

```sql
CREATE TABLE t_target_registry (
    target_code VARCHAR(64) PRIMARY KEY,
    display_name VARCHAR(128) NOT NULL,
    asset_class VARCHAR(64) NOT NULL DEFAULT 'bond',
    target_type VARCHAR(64) NOT NULL DEFAULT 'active_treasury',
    sort_order INT NOT NULL DEFAULT 0,
    status ENUM('active','paused','archived') NOT NULL DEFAULT 'active',
    extra JSON DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
```

当前种子映射:

| 内部 key | 展示名 |
|----------|--------|
| `3Y` | `3Y国债活跃` |
| `5Y` | `5Y国债活跃` |
| `7Y` | `7Y国债活跃` |
| `10Y` | `10Y国债活跃` |

`target_code` 用于关联预测表、actuals 表和历史回测表；`display_name` 用于前端展示。后续新增其他品种 Y 时，优先新增或更新 `t_target_registry`，不要直接把中文展示名写入 `target_tenor`。

---

## 4. 方案接口规范

### 4.1 config.yaml

```yaml
scheme_id: t5_daily              # 方案实例唯一标识，与目录名一致
name: "0529原始T5-LGBM投票基准"    # 显示名称
description: "..."               # 描述
horizon: 5                       # 预测跨度
tenors: ["3Y", "5Y", "7Y", "10Y"]  # 覆盖期限
frequency: daily                 # 频率
schedule:
  cron: "3 7 * * 1-5"           # cron表达式
  timezone: "Asia/Shanghai"
entry_point: predict.run         # 入口函数
```

周度方案示例使用 `cron: "30 11 * * 6"`，对齐旧实盘 weekly 首轮预测时间。

`config.scheme_id` 是 base 执行身份，不是 `T+1/5Y` 这样的任务格子名称；任务格子由 `horizon + target_tenor` 决定。前端/业务方案身份由 registry composite `scheme_id` 决定。`target_tenor` 是内部稳定 key，前端展示应使用 `t_target_registry.display_name` 或 API 返回的 `target_label`，当前数据库映射为 `3Y -> 3Y国债活跃` 等。

历史回测命名边界:

- `scheme_id`: 真实方案实例，只能使用当前在库的方案目录名（`t1_daily`、`t5_daily`、`weekly_5y_direct_0529`、`weekly_7y_cross_d_overlay_0529`）。早先示例中的周度方案（如 `weekly_10y_d_overlay`）已退役。
- `benchmark_id`: 历史基准批次，例如 `model_muti_0529`；canonical 输入位于 `benchmarks/{benchmark_id}/`。
- `data_source`: 数据口径枚举，例如 `framework_db_aligned`；API 负责映射成中文展示名，例如“当前DB对齐回测”。
- 运行期输入 artifact: `backtest_artifacts/runtime_inputs/{scheme_id}/`。
- 历史回测 artifact: `backtest_artifacts/backtests/{benchmark_id}/`。

### 4.2 predict.py接口

```python
from shared.models import PredictionRecord

def run(predict_date: str) -> list[PredictionRecord]:
    """
    执行预测。
    
    Args:
        predict_date: 预测发出日期 (YYYY-MM-DD)
    
    Returns:
        预测记录列表，每个tenor一条
    """
    ...
```

### 4.3 PredictionRecord

```python
@dataclass
class PredictionRecord:
    scheme_id: str
    target_tenor: str        # "1Y", "3Y", "5Y", "7Y", "10Y"
    horizon: int             # daily 1/5, weekly 6
    predict_date: str        # 信号发出日 / 调度运行日
    target_date: str         # 验证目标日，用于 actual join 和月度归属
    predicted_direction: int # 1=涨, -1=跌, 0=平
    feature_date: str | None = None       # 数据截止日 / 预测站位日
    prediction_phase: str | None = None   # gray_live / scheduled_live
    confidence: float | None = None
    model_version: str | None = None
    extra: dict | None = None
    run_id: int | None = None
    scheme_version: str | None = None
```

---

## 5. 后端API设计

### 5.1 GET /api/schemes

只返回 `status='active'` 的 registry rows；每行就是一个前端/业务方案。

```json
[
  {
    "scheme_id": "t5_daily__h5__10Y",
    "base_scheme_id": "t5_daily",
    "name": "0529原始T5-LGBM投票基准",
    "description": "...",
    "horizon": 5,
    "frequency": "daily",
    "target_tenor": "10Y",
    "schedule_cron": "3 7 * * 1-5",
    "schedule_timezone": "Asia/Shanghai",
    "status": "active",
    "deployed_at": "2026-06-10",
    "created_at": "2026-06-10T00:00:00",
    "updated_at": "2026-06-10T00:00:00"
  }
]
```

### 5.2 GET /api/targets

```json
{
  "targets": [
    {
      "target_code": "3Y",
      "display_name": "3Y国债活跃",
      "asset_class": "bond",
      "target_type": "active_treasury",
      "status": "active"
    }
  ],
  "target_labels": {
    "3Y": "3Y国债活跃"
  }
}
```

### 5.3 GET /api/metrics/{scheme_id}

参数: `start_month`, `end_month`

`scheme_id` 必须是 registry composite ID，例如 `t5_daily__h5__10Y`。接口不再接受 `?tenor=...`；传入 base scheme id（如 `t5_daily`）应返回 404，传入 `tenor` query 应返回 400。

该接口只接受 `status='active'` 的 registry row。`paused` / `archived` 或 registry 缺行的 `scheme_id` 一律返回 404。

返回中的 `daily_rows` 必须包含平台业务字段 `feature_date` 与 `prediction_phase`（`gray_live` / `scheduled_live`），并提供 `phase_ranges` 汇总。前端不得依赖 `anchor_date`。

指标字段中 `total` / `samples` 表示已验证样本总数，包含 `predicted_direction=0` 的“平”样本；`metric_samples` 表示准确率类指标分母，只包含有方向信号的预测样本。`accuracy` / `overall` 必须按 `correct / metric_samples` 计算；样本数列仍展示 `samples`。上涨准确率、上涨召回率、下跌准确率、下跌召回率同样只使用 `metric_*_dist` 口径，预测为“平”的样本不进入任何指标分母。

前端每日/周度验证明细只把预测为“平”的行作为中性样本展示：结果列显示 `-`，不显示 `✓` 或 `×`。这不改变样本总数，月度样本数仍包含该行；只是指标计算和正确/错误计数排除该行。

```json
{
  "scheme_id": "t5_daily__h5__10Y",
  "base_scheme_id": "t5_daily",
  "target_tenor": "10Y",
  "target_label": "10Y国债活跃",
  "monthly_metrics": [
    {
      "month": "2025-01",
      "total": 18,
      "samples": 18,
      "metric_samples": 17,
      "correct": 7,
      "accuracy": 41.2,
      "up_precision": 46.2,
      "up_recall": 60.0,
      "down_precision": 20.0,
      "down_recall": 16.7
    }
  ],
  "summary": {
    "total": 97,
    "samples": 97,
    "metric_samples": 95,
    "accuracy": 64.9,
    "up_precision": 65.1,
    "down_precision": 64.5
  }
}
```

---

### 5.4 GET /api/predictions

参数: `scheme_id`, `start_date`, `end_date`, `limit`, `offset`

`scheme_id` 必须是 `status='active'` 的 registry composite ID，例如 `t5_daily__h5__5Y`。接口会解析 registry 行得到 `base_scheme_id + target_tenor + horizon`，再查询底层 `t_scheme_predictions`；返回 item 中的 `scheme_id` 仍是 registry composite ID，同时用 `base_scheme_id` 留下底层存储身份。

该接口不接受 base scheme id、无 `scheme_id` 或 `?tenor=...`。base scheme id 和 `paused/archived` registry ID 返回 404，`tenor` query 返回 400，缺少 `scheme_id` 由 FastAPI 返回 422。

---

### 5.5 历史复现 API

历史回测结果不混入 `t_scheme_predictions`，统一通过独立接口读取:

- `GET /api/backtests/runs`
- `GET /api/backtests/runs/{run_id}`
- `GET /api/backtests/runs/{run_id}/metrics`
- `GET /api/backtests/runs/{run_id}/diffs`
- `GET /api/backtests/data-checks`

前端不新增历史验证结果页；方案展示仍走原有方案结果矩阵。

迁移阅读顺序：`migrations/007_backtest_immutable.sql` 引入 append-only 回测表和旧版 latest view，`migrations/009_backtest_latest_view.sql` 在保留 007 表结构的基础上替换 `v_latest_backtest_run` 为当前 canonical latest-success 语义。排查 latest 查询时必须先读 007 再读 009。

---

## 6. 前端结构

当前采用原生HTML/CSS/JS方案（从panda_quantflow提取，精简为仅因子实验室页面）:

```
frontend/
├── index.html              # 因子实验室单页面（仅保留factor-lab视图）
├── aifin-shell.css         # 完整样式（含factor-lab相关class）
├── aifin-shell.js          # 完整逻辑（优先读取API，mock仅作备用）
└── assets/
    ├── aifin-lab-icon.svg  # favicon
    └── aifin-lab-logo.svg  # 顶栏logo
```

**当前状态**: 前端优先读取 `GET /api/backtests/factor-lab` 展示最新 `framework_db_aligned` 历史回测矩阵；该 API 与 `v_latest_backtest_run` 使用同一套 canonical latest success 语义：同一 `benchmark_id + scheme_id + data_source` 下只取最新 `status='success'` run（`updated_at DESC, id DESC`），`start_date/end_date` 仅作为 run 属性。未传 `benchmark_id` 时返回所有 benchmark 下各方案最新成功 run，显式传 `benchmark_id` 时收窄到指定历史基准批次。该 API 只把 latest run 映射到 active registry rows；registry 缺行、`paused` 或 `archived` 的 target 不会进入前端候选排行，也不会从 config 临时拼业务 `scheme_id`。前端使用 API 返回的 `display_name` 统一显示为“方案名｜Y标的｜数据口径”；回测数据不可用时再回退到 `GET /api/schemes` 和 `GET /api/metrics/...` 的实盘预测接口。前端和业务统一使用 `feature_date` 表示数据截止日，不使用 `anchor_date`；实盘展示应能区分 `gray_live` 与 `scheduled_live`。当前回测/实盘方案包含日频 `t1_daily`、`t5_daily`、`daily_5y_2_v28` 与周频 `weekly_5y_direct_0529`、`weekly_7y_cross_d_overlay_0529`、`weekly_10y_d_overlay_0529`。

**前端指标口径**: 因子实验室页面必须同时展示“样本总数”和“指标分母”两种语义。月度“样本数”列使用 `samples`，包含预测为“平”的交易日或预测周；所有准确率类指标使用 `metric_samples` / `metric_*_dist`，排除预测为“平”的样本。每日/周度验证表中预测为“平”的行结果列显示 `-`，不显示 `×`，也不显示 `✓`。
**iframe 准备**: 当前服务未设置阻止嵌入的响应头；外层 panda_quantflow 接入仍是剩余观察项，最新进展见 [CURRENT_STATUS.md](CURRENT_STATUS.md)。

由FastAPI后端直接serve这个目录作为静态文件。

---

## 7. 部署架构

### 7.1 进程管理（launchd）

两个launchd plist:
- `com.bond-factor-lab.scheduler.plist` — 调度器进程
- `com.bond-factor-lab.backend.plist` — FastAPI后端

### 7.2 端口分配

| 服务 | 端口 |
|------|------|
| FastAPI后端 + 前端静态文件 | 8100 |
| MySQL | 3306 (已有) |

当前测试 Mac 的目标库为本机原生 MySQL `localhost:3306/bond_db`。机器上另有 Docker MySQL `127.0.0.1:13306/monitor_db`，不是本项目目标库。

### 7.3 环境变量

```bash
BOND_DB_HOST=localhost
BOND_DB_PORT=3306
BOND_DB_USER=root
BOND_DB_PASSWORD=***
BOND_DB_NAME=bond_db
```

---

## 8. 新增方案流程

标准流程见 [SCHEME_ONBOARDING_SOP.md](sop/SCHEME_ONBOARDING_SOP.md)。强约束 harness 总纲见 [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md)。

核心约定:

1. `scheme_id` 表示具体方案实例，不表示 `Y标的 + 预测长度` 的任务格子。
2. 一个 `scheme_id` 固定一个 `horizon`；同一算法若同时覆盖 T+1 和 T+5，应拆成两个方案目录。
3. 新方案先 `status: paused` dry-run，再改为 `active` 手动写库验证。
4. `predict.py` 只返回 `PredictionRecord`，不直接写 `t_scheme_predictions`。
5. 普通新增方案无需修改 scheduler、backend 或 frontend；若要参与当前历史排行，需要同步写入独立 backtest 表。
6. 新增方案必须遵守 [PREDICTION_SEMANTICS.md](PREDICTION_SEMANTICS.md)：回测 `predict_date=feature_date=T`，实盘 `predict_date=T+1/feature_date=T`，灰度实盘与正式实盘通过 `prediction_phase` 区分。

---

## 9. 强约束 Harness 工程架构

Bond Factor Lab 后续按“强约束 harness”管理方案入库。Harness 的职责是检查、编排和留下证据；它不定义新数据口径，不承载业务算法，也不替代 scheduler 做正式调度。

### 9.1 分层边界

| 层 | 目录/模块 | 职责 | 禁止事项 |
|----|-----------|------|----------|
| 统一公共层 | `shared.data_service` | 唯一底层日/周/月 DB 导出标准 | 普通方案接入时不得修改其业务逻辑 |
| 输入 artifact 层 | `shared.input_artifacts` | 唯一算法输入文件生成入口，负责导出 CSV、读回 DataFrame 和 metadata | adapter/backtest runner 不得绕过它直接拼输入 |
| 算法层 | `schemes/{scheme_id}/core/` | 纯算法逻辑、legacy 原始脚本归档、DataFrame 输入函数 | 禁止写库、禁止调 scheduler、禁止生成运行期输入文件 |
| Adapter 层 | `schemes/{scheme_id}/predict.py` | 解析预测上下文、调用公共输入层、调用 core、返回 `PredictionRecord` | 禁止直接写 `t_scheme_predictions` / `t_scheme_run_log` |
| 预测任务层 | `scheduler.scheme_runner` / `scheduler.executor` | dry-run JSON 输出、正式单方案执行、统一写库 | readiness 检查不得用 broad run-once 代替 |
| 回测层 | `backtests/{scheme_id}_reproduction.py` | 历史复现、`--no-persist` 验证、受控写 `t_backtest_*` | 禁止把回测结果写入实盘预测表 |
| 工具脚本层 | `scripts/` | 审计、对比、人工 admin、受控写库 | 禁止新增一次性绕路脚本作为方案运行入口 |
| 产物层 | `backtest_artifacts/` / `reports/` / `benchmarks/` | 运行期输入、历史回测产物、审计报告、canonical benchmark | 禁止放可复用业务代码 |

### 9.2 Harness Gate 顺序

未来新增方案统一按以下 gate 推进:

1. Intake: 明确方案身份、频率、预测语义、输入文件和回测目标。
2. Normalize: 将原始算法改造成 `schemes/{scheme_id}/core` 中的 DataFrame 输入逻辑。
3. Input Gate: 确认 live adapter 和 backtest runner 都通过 `shared.input_artifacts` 生成输入。
4. Static Gate: 静态扫描目录、命名、接口和危险导入。
5. Unit Gate: 覆盖 core、adapter、公共输入层调用和 `PredictionRecord` 字段。
6. Dry-run Gate: 通过 `scheduler.scheme_runner` 返回 JSON，且正式 prediction/run_log 行数不变，并校验 live 日期语义。
7. Backtest Gate: 先 `--no-persist`，授权后才写 `t_backtest_*`，并用 protected table snapshot 阻断越界写库。
8. Live Gate: 授权并显式传入 `prediction_phase` 后，只写该 `scheme_id` 的 prediction/run_log。
9. Activation: 全部通过后才允许从 `paused` 改为 `active`。
10. Documentation: 更新状态、测试、回测和 harness 报告路径。

### 9.3 CLI 入口

当前 `harness/` 包的 CLI 入口:

```bash
python -m harness onboard t1_daily \
  --predict-date 2026-06-06 \
  --stage all

python -m harness gate input \
  --scheme-id t1_daily \
  --predict-date 2026-06-06

python -m harness gate live \
  --scheme-id t1_daily \
  --predict-date 2026-06-06 \
  --prediction-phase scheduled_live \
  --authorize "$TOKEN"
```

`--stage all` 固定执行 static -> input -> unit -> dry-run -> compare -> backtest-no-persist -> api-readonly；任一步失败即停止。写库动作不属于默认 `all`，必须由受控 backtest/live 命令单独执行。
