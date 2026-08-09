# 架构设计: Bond Factor Lab

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：平台开发和架构审计人员
**最后核验日期**：2026-08-09
**版本**：v1.3

> 本文是**系统架构**（部署、DB schema、API 契约、数据流）。代码层面的分层、包依赖方向规则、运行时调用图与扩展模型见 [CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md)（代码架构主蓝图）。
> 预测日期与实盘阶段语义以 [PREDICTION_SEMANTICS.md](PREDICTION_SEMANTICS.md) 为准。
> Source-backed 方案的原始算法保真以 [SOURCE_ALGORITHM_FIDELITY.md](SOURCE_ALGORITHM_FIDELITY.md) 为准；系统架构只允许做输入、日期、落库和展示适配，不允许改变算法计算逻辑。

---

## 1. 系统架构图

下图是 G1/G2 完成后应形成的目标拓扑，不是 installed/loaded 现场快照。每个生产任务
都必须由独立授权的 installed plist、`launchctl`、日志、run 与 prediction 共同证明。

```
Mac Studio
└─ launchd + installed plist                         ← 真实生产调度控制面
   ├─ com.bond-factor-lab.backend
   │  └─ FastAPI :8100 + frontend 静态文件
   ├─ DataBridge refresh one-shot
   │  └─ 本机 MySQL → 原子发布日/周/月 artifact
   ├─ daily predictions one-shot
   │  └─ active discovery → scheduled_live
   ├─ weekly/monthly predictions one-shot
   │  └─ 各自自然时钟 → scheduled_live
   ├─ com.bond-factor-lab.actuals
   │  └─ 08:30/19:00/23:45 一次性 scheduler.actuals_runner
   └─ 每个 cadence 只有一个 writer

上述任务进程
├─ discovery / executor / repository
└─ MySQL bond_db
   ├─ 只读源表：api_wind_* / t_trade_calendar
   └─ 平台表：t_scheme_* / t_backtest_* / t_target_registry

panda_quantflow AIFin Lab Shell
└─ iframe → http://mac-studio:8100/
```

`launchd + installed plist` 是唯一生产调度控制面：launchd 决定任务是否挂载、何时触发、
使用什么环境、是否重启以及日志落点。常驻 `scheduler.main`/APScheduler 已从仓库删除；
Backend 不注册手动预测路由，也不形成第二套生产控制面。仓库 plist 也只有与 installed plist
和 `launchctl` loaded state 核对后，才能证明
现场配置；任何已安装 disabled legacy plist 的物理删除仍须单独授权。

目标拓扑中 Actuals 不挂载在常驻 APScheduler 中。独立
`com.bond-factor-lab.actuals` LaunchAgent 在 `08:30/19:00/23:45` 启动一次性
`scheduler.actuals_runner` 进程；三个时点和进程退出状态均由 launchd 管理。
actuals 不再经常驻 scheduler 兼容委托，仓库也不保留 `scheduler.main` 或
`--run-once actuals` CLI。disabled `com.bond-factor-lab.scheduler` 模板已移除；这不构成
任何 installed legacy plist 已物理删除的结论。

任何 frequency 的预测都不得同时挂载两条自动路径。已退役的 daily-gray/v2-preflight
writer 的仓库模板已移除，ledger/occurrence/epoch runtime 闭包也已从仓库移除；常驻 scheduler 与 per-scheme cron
不属于新的或过渡生产方案。历史 migration 和数据库对象的物理归档仍须在独立 DDL 授权下处理，不能以
另一套控制面替换它们。

当前 loaded 进程是否已经达到上述目标，统一以[当前状态](../CURRENT_STATUS.md)为准。
installed plist 替换、`bootstrap/bootout/kickstart` 和服务重启都属于独立生产操作；必须先
只读核对现场、取得明确授权，再以 `launchctl`、任务日志、run 与 prediction 确认收敛。

方案执行层有两个显式驱动：Native V1 仅运行政策清单中的存量 adapter；Blackbox V2 接收所有后续新增方案，通过 DataBridge 三频同代快照和隔离 CLI 执行。两者都转换为 `PredictionRecord`，之后共用 Registry、actual join、落库、API 和前端链路。

---

## 2. 数据流

### 2.0 预测日期与阶段

平台统一使用三类日期字段：

- `predict_date`: 信号发出日 / 调度运行日。
- `feature_date`: 数据截止日 / 预测站位日。
- `target_date`: 验证目标日，用于展示、去重、actual join 和月度统计归属。

`feature_date` 是前端和业务唯一标准数据截止字段；`anchor_date` 只允许作为方案内部变量或审计 extra。实盘预测分为 `gray_live` 和 `scheduled_live` 两个阶段，二者都属于实盘观察区；历史回测独立写入 `t_backtest_*`，不得从实盘预测表拼历史结果。

原始算法 benchmark 中的 `T/date/predict_date` 表达的是 source T / 预测站位日。进入平台后必须对齐 `feature_date`，不能对齐实盘语义下的 `predict_date`。若 benchmark 样本 target 仍在历史回测区间，则与 `t_backtest_predictions.feature_date` 对齐；若 target 已进入灰度/实盘观察区，必须先确认 benchmark 与 live 记录是否同一执行口径，同口径时才与 `t_scheme_predictions.feature_date` 对齐并校验 `prediction_phase`。若 source-original batch 固定了晚于样本 `feature_date` 的 `source_end` 或 test window，该 batch 只能作为 source-original backtest 证据，live 必须用 `feature_date` 硬截止的 live-safe oracle 验收。

Source-backed 方案必须先声明 source 执行口径：`source_original_reproduction`、`source_strict_pit` 或经批准的 `platform_live_pit_variant`。无论采用哪类口径，算法内部的时间窗口、特征、周/月频对齐、模型参数、投票和内部 score 映射都不得被平台重写。跨灰度边界的 `original_predictions_sample.csv` 必须按 row role 拆分；`TOTAL_BAD=0` 只能说明 live 行结构、版本和 scope 正确，不能替代同口径数值 diff。

### 2.1 预测流程

```
launchd 按 installed plist 在一个明确 cadence 启动一次性任务
  → discovery.py 按显式 runtime_type 构建 active SchemeConfig
  → DataBridge artifact 先验证 refresh 与 feature cutoff
  → 一个 writer 调用 scheduler.executor：
  → 读取 schedule.timeout_sec（如有）作为子进程等待预算
  → native_adapter: 隔离调用 schemes.{id}.predict.run(predict_date)
  → blackbox_v2: 构造只读快照和 Request，隔离调用交付脚本 CLI
  → 返回 list[PredictionRecord]
  → scheduler.repository 写 t_scheme_runs / predictions / run_log
```

具体 active 方案、cron 和运行状态属于时点信息，只在 [CURRENT_STATUS.md](../CURRENT_STATUS.md) 维护。Blackbox 方案只有完成生产准备核验并取得专项授权后，才会进入正式 Registry 或 scheduler。

并发、timeout 与方案依赖属于执行器实现细节，不能改变一 cadence 一 writer、日期语义、
feature cutoff 或 source 算法逻辑。生产上修改调度配置前必须核对仓库与 installed plist
差异；取得独立生产授权后再按对应 LaunchAgent 生效方式操作。仅看到
`Scheduled scheme ...` 注册记录不能单独证明任务已由真实生产控制面接管。

### 2.2 实际方向更新（每日08:30、19:00与23:45）

```
launchd 在每日08:30、19:00和23:45启动一次性 Actuals 任务；交易日 daily/weekly 刷新到当日，非交易日 daily/weekly 刷新到上一交易日，monthly 仍刷新到自然 run date
  → 从 api_wind_daily 读取最新收盘收益率
  → 计算各tenor的T+1和T+5方向
  → 写入 t_scheme_actuals (UPSERT)
```

其中 `23:45` 夜间刷新用于承接上游 BondPrediction `23:25` 左右的 Wind 日频导入，避免源表夜间补齐后前端仍等到次日 `08:30` 才显示验证结果。
若周五源 actual 晚于 `23:45` 才进入 `api_wind_daily`，周六的 actuals job 会以周五为 daily/weekly end date 自动补刷，避免 T+1 最新验证卡在上一交易日。

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
用户选择: 任务格子(Y标的 + task_type) + 候选方案 + 月份范围
  → 前端调用 GET /api/factor-lab/dashboard 获取一致性只读快照
  → dashboard 按 active Registry 的 task_type 分列，并附带 t_target_registry 展示名
  → 用户选择候选方案和月份时只在该快照内筛选
  → 后端 dashboard 聚合层:
      → T+1/T+5 JOIN t_scheme_actuals
      → Native V1 存量周度 horizon=6 JOIN t_scheme_weekly_actuals
      → 按月分组计算准确率指标
      → 返回结构化JSON
  → 前端渲染表格和图表
```

旧 `/api/schemes`、`/api/metrics/{registry_scheme_id}` 和
`/api/backtests/factor-lab` 仅保留给本机 Harness、回滚和公网 rollout 兼容；final 公网
配置拒绝这三个接口，不能再把它们作为前端主合同。

### 2.4 历史复现流程

```
手动执行 backtests.daily_0529_reproduction
  → 默认通过 shared.input_artifacts 从 DB 生成 DB-first daily_output 并读回
  → 运行框架内 t1_daily / t5_daily 批量回测逻辑
  → 只输出 framework_db_aligned（--no-persist 时不写库）
  → 显式 --include-source-evidence 时才读取 source_evidence/benchmark_batches/model_muti_0529/daily_output.csv
  → source-evidence 模式对比外部归档、framework-csv、framework-db
  → 写入 t_backtest_runs / t_backtest_predictions / t_backtest_reproduction_checks
  → 前端回测指标只以 t_backtest_predictions 明细动态聚合为准
  → 验证结果保留在后端 API、脚本和文档中，不新增前端验证结果页

手动执行 backtests.weekly_*_reproduction
  → 通过 shared.input_artifacts 生成 historical_backtest 周频 weekly_output CSV 并读回
  → 周频内部调用统一 shared.data_service
  → 运行 scheme core 中的周频算法逻辑
      → 按 target_date 所在月份生成月度指标
  → 写入对应 scheme_id 的 t_backtest_runs / t_backtest_predictions
  → dashboard 聚合层读取 canonical latest success run，并由 t_backtest_predictions 动态聚合月度指标
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

- `target_tenor + task_type` 定义任务格子，例如 `5Y + T+1` 或 `5Y + weekly_average`；前端展示名通过 target label 和 `task_type` 映射为 `5Y国债活跃 · T+1`、`5Y国债活跃 · 周平均` 等。
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
    UNIQUE KEY uk_weekly_actual_predict_rule (tenor, predict_date, target_rule),
    INDEX idx_weekly_actual_target (tenor, target_date),
    INDEX idx_weekly_actual_week (feature_week_id, target_week_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

周频 actuals 写入前由 `scheduler.weekly_actuals_updater` 读取 `api_wind_date + t_trade_calendar` 构造周历；预测侧 `shared.calendar_service` 与 actuals updater 共享 `shared.week_calendar_normalizer`，只对源周历中“单个交易日提前跳到下一周、随后非交易日回落上一周”的孤立不连续行做只读归一化。归一化不修改源表，也不能替代 source core 的信号水位检查。

### 3.4 t_scheme_registry

```sql
CREATE TABLE t_scheme_registry (
    id INT AUTO_INCREMENT PRIMARY KEY,
    scheme_id VARCHAR(64) NOT NULL UNIQUE,
    base_scheme_id VARCHAR(64) NOT NULL,
    name VARCHAR(128) NOT NULL,
    description TEXT,
    horizon INT NOT NULL,
    task_type VARCHAR(32) NULL,
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

`t_scheme_registry` 是唯一方案注册表，一行就是前端/业务定义的一个方案。`scheme_id` 是唯一业务身份，统一格式为 `{base_scheme_id}__h{horizon}__{target_tenor}`，例如 `t5_daily__h5__10Y`；不再存在第二套 `(base_scheme_id, frequency, horizon, target_tenor)` 唯一键。`base_scheme_id` 是算法目录 / config / scheduler / backtest 存储使用的执行身份，例如 `t5_daily`；同一个 base 算法预测多个 Y 标的时，registry 拆成多行，但 scheduler 仍只按 `base_scheme_id` 挂载一个执行任务。`task_type` 是前端任务格子分列的唯一语义字段，固定取值为 `T+1`、`T+5`、`weekly_point`、`weekly_average`、`monthly`；字段缺失或非法时 API 必须 fail-closed，不得回退到 `frequency/horizon` 猜列。

`active` 是唯一前端/业务可见和自动调度资格状态。`GET /api/schemes`、`GET /api/metrics/{scheme_id}`、`GET /api/backtests/factor-lab` 只返回 `status='active'` 的 registry composite `scheme_id`。`paused` 用于验证期管理，`archived` 用于保留审计历史；二者不进入当前前端矩阵，也不允许 scheduler 新写入对应 target。Backend 不提供手动预测接口。

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
| `1Y` | `1Y国债活跃` |
| `3Y` | `3Y国债活跃` |
| `5Y` | `5Y国债活跃` |
| `7Y` | `7Y国债活跃` |
| `10Y` | `10Y国债活跃` |

`target_code` 用于关联预测表、actuals 表和历史回测表；`display_name` 用于前端展示。后续新增其他品种 Y 时，优先新增或更新 `t_target_registry`，不要直接把中文展示名写入 `target_tenor`。

---

## 4. 双运行时接口边界

共享身份、日期、结果和生命周期以[双运行时共享方案契约](SCHEME_CONTRACT.md)为准。以下 `config + predict + core` 只描述 Native V1 存量兼容；所有后续新增方案使用 Blackbox V2 Contract 1.0。

### 4.1 Native V1 config.yaml

```yaml
scheme_id: t5_daily              # 方案实例唯一标识，与目录名一致
runtime_type: native_adapter      # 仅存量维护
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

`config.scheme_id` 是 base 执行身份，不是 `T+1/5Y` 这样的任务格子名称；任务格子由 `task_type + target_tenor` 决定，`horizon` 保留为目标日计算和 actual join 语义。前端/业务方案身份由 registry composite `scheme_id` 决定。`target_tenor` 是内部稳定 key，前端展示应使用 `t_target_registry.display_name` 或 API 返回的 `target_label`，当前数据库映射为 `1Y -> 1Y国债活跃`、`3Y -> 3Y国债活跃` 等。

历史回测命名边界:

- `scheme_id`: 真实方案实例，只能使用当前在库的方案目录名（例如 `t1_daily`、`t5_daily`、`weekly_5y_direct_0529`、`weekly_avg_5y_lgbm_0529`）。早先示例中的周度方案（如 `weekly_10y_d_overlay`）已退役；旧 point-backed 周平均方案已从运行代码和 Registry 删除。
- `benchmark_id`: 历史基准批次，例如 `model_muti_0529`；外部来源证据归档位于 `source_evidence/benchmark_batches/{benchmark_id}/`，平台 active runner 的默认输入真源必须来自 `shared.input_artifacts`。
- `data_source`: 数据口径枚举，例如 `framework_db_aligned`；API 负责映射成中文展示名，例如“当前DB对齐回测”。
- 运行期输入 artifact: `backtest_artifacts/runtime_inputs/{scheme_id}/`。
- 历史回测 artifact: `backtest_artifacts/backtests/{benchmark_id}/`。

### 4.2 Native V1 predict.py 接口

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

### 4.3 Blackbox V2 上游接口

上游只交付 `{scheme_id}.py + {scheme_id}.json`，实现 `predict` 和 `backtest` CLI；平台传入七字段 Request 与三频只读快照，脚本输出五字段 Result。完整合同见[上游交付 SOP](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)。平台校验 Result 后补充 Metadata 和运行上下文，转换为统一 `PredictionRecord`。

### 4.4 PredictionRecord

```python
@dataclass
class PredictionRecord:
    scheme_id: str
    target_tenor: str        # "1Y", "3Y", "5Y", "7Y", "10Y"
    horizon: int             # 由运行时合同/存量配置定义；业务分列只认 task_type
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
    "task_type": "T+5",
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
      "target_code": "1Y",
      "display_name": "1Y国债活跃",
      "asset_class": "bond",
      "target_type": "active_treasury",
      "status": "active"
    }
  ],
  "target_labels": {
    "1Y": "1Y国债活跃",
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

**前端数据语义**: 前端以 `GET /api/factor-lab/dashboard` 的一致性快照作为当前和 final 公网主合同。dashboard 只把 canonical latest-success 历史回测和 live 明细映射到 `active` Registry target；Registry 缺行、`paused` 或 `archived` target 不进入业务矩阵。旧 `/api/schemes`、`/api/metrics/{registry_scheme_id}` 和 `/api/backtests/factor-lab` 只用于本机 Harness、回滚与公网 rollout 兼容，不属于 final 公网合同。回测与 live 统一按 `target_date` 归属月份，按 `feature_date` 表达数据截止，并区分 `gray_live` 与 `scheduled_live`。动态方案清单、资源版本、运行数量和最新验证结果不在架构文档维护，统一查看[当前状态](../CURRENT_STATUS.md)和带日期的记录。

**前端指标口径**: 因子实验室页面必须同时展示“样本总数”和“指标分母”两种语义。月度“样本数”列使用 `samples`，包含预测为“平”的交易日或预测周；所有准确率类指标使用 `metric_samples` / `metric_*_dist`，排除预测为“平”的样本。每日/周度验证表中预测为“平”的行结果列显示 `-`，不显示 `×`，也不显示 `✓`。
**iframe 准备**: 当前服务未设置阻止嵌入的响应头；外层 panda_quantflow 接入仍是剩余观察项，最新进展见 [CURRENT_STATUS.md](../CURRENT_STATUS.md)。

由FastAPI后端直接serve这个目录作为静态文件。

---

## 7. 部署架构

### 7.1 进程管理（launchd）

生产目标的 launchd plist 必须分别承担 DataBridge refresh、daily prediction、weekly
prediction、monthly prediction、actuals 和 backend 的单一职责。仓库已移除
`com.bond-factor-lab.scheduler` 这一 disabled legacy/过渡模板，以消除该 legacy 调度表达；
这项 repo-only 变更不说明或改变任何 installed plist 状态。已退役的 daily-gray 和
v2-preflight 仓库模板也已移除。

这些仓库文件描述期望配置，不自动代表 `~/Library/LaunchAgents` 中的 installed plist。
任何安装、替换、`bootstrap/bootout/kickstart` 或重启都属于独立生产操作。当前规则与
阶段顺序见[生产信号与调度治理](PRODUCTION_SCHEDULING_GOVERNANCE.md)。

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

## 8. 方案入库与维护

唯一入口见[方案入库导航](../onboarding/README.md)。

- 新算法、新方案 ID、新 target、新 task type 和替代版本一律使用 Blackbox V2。
- Native V1 只能维护 `deploy/onboarding_policy_v1.json` 登记的既有身份；StaticGate 和 ActivationGate 同时阻断清单外 Native ID。
- Blackbox Intake 原样保存两文件，生成 `paused/draft` 平台配置，再执行七个自动 Gate。
- 自动 Gate 只授予技术验收权限；生产准备通过的具体方案仍须取得专项授权，才能执行 activate、持久化回测或 live。
- 两种运行时都遵守三日期、composite Registry 身份、失败不生成业务信号和写库授权边界。

---

## 9. 强约束 Harness 工程架构

Bond Factor Lab 后续按“强约束 harness”管理方案入库。Harness 的职责是检查、编排和留下证据；它不定义新数据口径，不承载业务算法，也不替代 scheduler 做正式调度。

### 9.1 分层边界

| 层 | 目录/模块 | 职责 | 禁止事项 |
|----|-----------|------|----------|
| 统一公共层 | `shared.data_service` | 唯一底层日/周/月 DB 导出标准 | 普通方案接入时不得修改其业务逻辑 |
| 输入 artifact 层 | `shared.input_artifacts` | 唯一算法输入文件生成入口，负责导出 CSV、读回 DataFrame 和 metadata | adapter/backtest runner 不得绕过它直接拼输入 |
| Native 算法层 | `schemes/{scheme_id}/core/` | 存量纯算法逻辑、legacy 原始脚本归档、DataFrame 输入函数 | 禁止新增 Native 身份、写库、调 scheduler 或生成运行期输入文件 |
| Native Adapter 层 | `schemes/{scheme_id}/predict.py` | 存量方案解析上下文、调用公共输入层和 core、返回 `PredictionRecord` | 禁止直接写 `t_scheme_predictions` / `t_scheme_run_log` |
| Blackbox Delivery 层 | `schemes/{scheme_id}/delivery/` | 保存上游原始 `.py + .json`，以隔离 CLI 执行 | 禁止平台改写脚本、算法访问网络/DB 或写入数据目录 |
| 预测任务层 | `scheduler.scheme_runner` / `scheduler.executor` | dry-run JSON 输出、正式单方案执行、统一写库 | readiness 检查不得用 broad run-once 代替 |
| 回测层 | `backtests/{scheme_id}_reproduction.py` | 历史复现、`--no-persist` 验证、受控写 `t_backtest_*` | 禁止把回测结果写入实盘预测表 |
| 工具脚本层 | `scripts/` | 审计、对比、人工 admin、受控写库 | 禁止新增一次性绕路脚本作为方案运行入口 |
| 产物层 | `backtest_artifacts/` / `reports/` / `source_evidence/` | 运行期输入、历史回测产物、审计报告、外部来源证据归档 | 禁止放可复用业务代码；`source_evidence/` 不得作为 active runner 默认输入 |

### 9.2 Harness Gate 顺序

首次技术入库的 Native 与 Blackbox 都固定使用七段 `all`：`static -> input -> unit -> dry-run -> compare -> backtest -> api-readiness`。Native 的 source benchmark/CompareGate 是这条首次路径的硬证据；Blackbox Compare 仍验证确定性、分批/顺序与截止隔离，二者都没有全局关闭 CompareGate 的例外。

ActivationGate 的两条 Native 路径互斥：当前 exact version 通过完整七段 `all` 时，按 `full_initial_onboarding_v1` 激活，只核验该 current `all` 的七个 Gate（含当前 Compare），不要求 prior snapshot 或 `native-maintenance`。只有未使用 full-`all` 的已入库 Native 修订才可能使用六段 `native-maintenance`：`static -> native-maintenance-admission -> input -> unit -> dry-run -> api-readiness`。后者必须只读复核不同 prior Native version 的 passed `all + compare`，以及该 prior `all` 的持久化 `static.business_identity` 与当前身份精确匹配。该快照只含 `scheme_id`、`runtime_type`、`horizon`、`task_type`、`frequency`、target tenors 和 composite Registry IDs，不含代码、config 或 version hash。唯一保留的补证是 `weekly_10y_d_overlay_0529` 已持久化的 canonical receipt；平台只读校验其 prior、StaticGate 与固定业务身份，writer 与 token action 已退役。receipt 不改历史或当前 hash、不自动生成或推断、不激活、不补数，只让 maintenance 标记 `legacy_operator_attestation_v1` 后继续六段 Gate。maintenance 的 current exact `t_scheme_versions` 行必须为 `runtime_type='native_adapter'` 且 status 为 `draft|active`；expected Registry identity 可在预激活时统一为 `paused`，或在激活后统一为 `active`，但 draft version 配 active Registry 必须 fail-closed。只有 ActivationGate 可在严格 discovery、精确版本与一次性授权核验后原子建立 active 状态。

1. Blackbox onboarding 继续验证两文件、Metadata、三频快照、CLI、确定性、分批/顺序一致性、截止隔离和 Result 转换。
2. 自动段只生成 Harness 报告和控制面审计，不得写预测、回测等业务表。
3. Native 的既有受控副作用继续沿用授权边界；Blackbox 通用入库只登记 `shadow + paused`，生产副作用必须逐方案通过专用 Gate 和专项授权。
4. `api-readiness` 只声明其实际探测范围，不得把结构兼容证据写成真实 Registry/API/scheduler 探针。

### 9.3 CLI 入口

当前 `harness/` 包的 CLI 入口:

```bash
python -m harness onboard t1_daily \
  --predict-date 2026-06-06 \
  --stage all

python -m harness onboard {existing_native_scheme_id} \
  --predict-date YYYY-MM-DD \
  --stage native-maintenance

python -m harness gate input \
  --scheme-id t1_daily \
  --predict-date 2026-06-06

python -m harness gate live \
  --scheme-id t1_daily \
  --predict-date 2026-06-06 \
  --prediction-phase scheduled_live \
  --authorize "$TOKEN"
```

`--stage all` 固定执行七段 `static -> input -> unit -> dry-run -> compare -> backtest-no-persist -> api-readiness`；任一步失败即停止，并且是首次 Native 技术入库唯一保留 source benchmark/CompareGate 的路径。当前 exact version 的 `all` 通过时，ActivationGate 采用 `full_initial_onboarding_v1`，不再附加 maintenance 或 prior-snapshot 条件。`--stage native-maintenance` 仅对有匹配 prior `static.business_identity` 快照的既有 Native 身份执行六段 `static -> native-maintenance-admission -> input -> unit -> dry-run -> api-readiness`，不运行当前 historical `compare/backtest`，并采用独立的 `native_post_admission_revision_v1` profile。若 prior StaticGate 已通过但仅 identity 字段缺失，只有 `weekly_10y_d_overlay_0529` 可先使用专用 receipt；receipt 不是 stage、不能替代六段 Gate 或 activation，也不能应用于其它身份。写库动作和 active-only `api` gate 不属于任何默认自动段，必须由受控 backtest/live/activate 命令或激活后验收单独执行。
