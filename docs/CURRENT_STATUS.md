# 当前状态

**更新日期**: 2026-06-09

## 总览

当前代码侧只保留两个可发现、可调度的日频方案：

| 方案 | 频率 | Horizon | 目标 | 状态 |
|------|------|---------|------|------|
| `t1_daily` | `daily` | 1 | `5Y/10Y` | `active` |
| `t5_daily` | `daily` | 5 | `3Y/5Y/7Y/10Y` | `active` |

2026-06-09 已删除全部旧周频预测方案代码。删除原因是旧周频方案在预测/回测路径中使用计算型周历公式推导 `week_id <-> 交易日`，而不是读取 `bond_db.api_wind_daily` 的实际 `week_id` 口径，可能从入库时起造成特征周/目标周错位。后续周频方案需要按 [新增方案 SOP](sop/SCHEME_ONBOARDING_SOP.md) 重新入库，并强制使用 DB-sourced `week_id`。

本次只清理代码和文档，不清理数据库。旧周频方案在 `t_scheme_registry`、`t_scheme_predictions`、`t_scheme_run_log`、`t_scheme_weekly_actuals`、`t_backtest_*` 中的历史记录按决策延后处理。

## 架构与 Harness

强约束 harness 工程已从"文档设计"推进到"代码落地并可运行"。架构演进路线见 [CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md) §10，执行阶段 S0-S8 已完成：

- `shared.calendar_service` 提供交易日历/周历查询单点入口，`week_id_for_date` 读取 DB 口径。
- `shared.input_artifacts` 是所有预测 adapter 的输入文件生成入口；日频、周频、月频底层统一由 `shared.data_service` 生成输出宽表，artifact 层负责写出输入 CSV 并读回给算法。
- `harness/` 包已实现 StaticGate、InputGate、UnitGate、DryRunGate、BacktestGate、ApiGate、LiveGate，以及 contracts、table_guard、authorization、orchestrator、CLI。
- `python -m harness onboard {scheme_id} --stage all` 可串联 static -> input -> unit -> dry-run -> backtest -> api；live/activate 不在 `all` 内，必须显式授权。

周频共享基础设施保留：

- `shared/calendar_service.py`：DB-sourced 日历与 `week_id` 查询。
- `shared/input_artifacts.py`：`build_weekly_input_artifact()`。
- `shared/data_service.py`：`build_weekly_output_from_db()`。
- `scheduler/weekly_actuals_updater.py`：周频 actuals 刷新基础设施。
- 前端周度列支持：按 `frequency=weekly` 映射，scheme-agnostic。

已删除的旧周频专属内容包括方案目录、回测 runner、方案专属测试/脚本、计算型周历模块和 5Y/7Y legacy 源。

## 已完成

- 本机 `.env` 已生成并保持 Git 忽略；服务环境和算法环境均可读取。
- MySQL `bond_db` 已创建正式平台表：
  - `t_scheme_predictions`
  - `t_scheme_actuals`
  - `t_scheme_weekly_actuals`
  - `t_scheme_registry`
  - `t_scheme_run_log`
  - `t_target_registry`
- 历史回测表已创建：
  - `t_backtest_runs`
  - `t_backtest_predictions`
  - `t_backtest_monthly_metrics`
  - `t_backtest_reproduction_checks`
- 服务环境已分离：
  - 算法预测：conda `forecast_env`
  - 后端/API/调度器：conda `bond_factor_lab_service`
- `t1_daily` 已完成 adapter，当前 active，预测目标为 `5Y/10Y`。
- `t5_daily` 已完成 adapter，当前 active，预测目标为 `3Y/5Y/7Y/10Y`。
- 强约束 harness 已落地：[HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) 是方案入库总纲，[CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md) 是代码架构主蓝图，[HARNESS_DESIGN.md](HARNESS_DESIGN.md) / [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) / [DATA_LAYER_DESIGN.md](DATA_LAYER_DESIGN.md) 是现行设计规范。
- artifact 命名已统一：`backtests/` 只放回测代码，`benchmarks/{benchmark_id}/` 只放 canonical 基准输入，运行期输入在 `backtest_artifacts/runtime_inputs/{scheme_id}/`，历史回测和数据差异报告在 `backtest_artifacts/backtests/{benchmark_id}/`。
- `t_target_registry` 当前展示 `3Y/5Y/7Y/10Y` 四个国债活跃目标；`1Y` 只保留为部分算法特征/审计输入，不作为当前前端目标。
- launchd 已安装并启动：
  - `com.bond-factor-lab.backend`
  - `com.bond-factor-lab.scheduler`
- 2026-06-09 周频方案删除后已重启 scheduler；`launchctl print` 显示 `com.bond-factor-lab.scheduler` 为 `running`。
- 前端已从真实 API 读取目标注册表和回测数据；T+1/T+5/周度筛选入口保留。

## 当前数据库快照

只读核验时间：`2026-06-08`，数据库 `bond_db`，MySQL `8.0.45`。以下为删除周频代码前的数据库快照；本次清理未触碰 DB，因此周频历史行数仍可能存在，后续另行清理。

| 项 | 当前值 |
|----|--------|
| `api_wind_daily` | 2,235,209 行，日期 `2010-01-01` 到 `2026-06-05` |
| `api_wind_derivative_daily` | 937,312 行，日期 `2010-01-01` 到 `2026-06-04` |
| `api_wind_weekly` | 121,553 行，周 `200901` 到 `202621` |
| `api_wind_derivative_weekly` | 277,064 行，周 `200901` 到 `202621` |
| `api_wind_indicators_all` | 1,637 |
| `t_trade_calendar` | 6,209 |
| `t_pre_market_forecast` | 1,059 |
| `t_shap` | 8,845 |
| `t_scheme_predictions` | 13 |
| `t_scheme_actuals` | 13,900 |
| `t_scheme_weekly_actuals` | 794 |
| `t_scheme_registry` | 3 |
| `t_scheme_run_log` | 6 |
| `t_target_registry` | 4 |
| `t_backtest_runs` | 12 |
| `t_backtest_predictions` | 8,111 |
| `t_backtest_monthly_metrics` | 649 |
| `t_backtest_reproduction_checks` | 1 |
| `bfl_probe_*` 影子表 | 0 |

actuals 覆盖：

| 期限 | 记录数 | 日期范围 |
|------|--------|----------|
| `1Y` | 2,518 | `2016-01-18` 到 `2026-06-03` |
| `3Y` | 2,518 | `2016-01-18` 到 `2026-06-03` |
| `5Y` | 2,517 | `2016-01-18` 到 `2026-06-03` |
| `7Y` | 2,518 | `2016-01-18` 到 `2026-06-03` |
| `10Y` | 3,829 | `2010-07-27` 到 `2026-06-03` |

## 历史回测状态

历史复现结果写入独立 backtest 表，不混入 `t_scheme_predictions`。当前代码侧保留的历史回测方案如下：

| 方案 | 数据源 | 状态 | 日期范围 |
|------|--------|------|----------|
| `t1_daily` | `baseline_original_csv` | `success` | `2025-01-02` 到 `2026-05-28` |
| `t1_daily` | `framework_original_csv` | `success` | `2025-01-02` 到 `2026-05-28` |
| `t1_daily` | `framework_db_aligned` | `success` | `2025-01-02` 到 `2026-05-28` |
| `t5_daily` | `baseline_original_csv` | `success` | `2025-01-01` 到 `2026-05-31` |
| `t5_daily` | `framework_original_csv` | `success` | `2025-01-01` 到 `2026-05-31` |
| `t5_daily` | `framework_db_aligned` | `success` | `2025-01-01` 到 `2026-05-31` |

DB 中旧周频回测 run 暂时保留，前端或 API 若直接读取 DB 历史记录，可能仍看到待清理的周频历史项；代码仓库中对应 runner 已删除。

## 正式预测状态

2026-06-05 09:25 launchd scheduler 已成功运行 active 日度方案：

| 方案 | 预测日期 | 记录数 | 状态 |
|------|----------|--------|------|
| `t1_daily` | `2026-06-05` | 2 | `success` |
| `t5_daily` | `2026-06-05` | 4 | `success` |

旧周频 live prediction/run_log 记录未在本次清理中删除。scheduler 重启后，当前代码配置只会注册日频方案。

## API 与安全边界

- 已只读验证：
  - `GET /api/health`
  - `GET /api/targets`
  - `GET /api/predictions?limit=1`
- `GET /api/backtests/factor-lab` 已改为只读获取 scheme metadata，不再触发 registry sync；`GET /api/schemes` 仍会同步 registry，属于正常服务行为，但不是纯只读接口；做数据库保护核验时不要把 `/api/schemes` 当作只读探针。
- 正式运行需要写库时，只应通过明确的调度器或运维命令写入 `t_scheme_predictions`、`t_scheme_run_log`、`t_scheme_actuals`、`t_scheme_weekly_actuals` 或 `t_backtest_*`，不要改动源数据表。

## 剩余观察项

1. 下一次日频 scheduler 运行后，确认日志只注册和执行 `t1_daily` / `t5_daily`。
2. 如需清理旧周频 DB 记录，单独制定 SQL 审计和备份方案后再执行。
3. 新周频方案进入时，必须按 [SCHEME_ONBOARDING_SOP.md](sop/SCHEME_ONBOARDING_SOP.md) 的 Intake -> Normalize -> Input Gate -> Static Gate -> Unit Gate -> Dry-run Gate -> Backtest Gate -> Live Gate -> Activation -> Documentation 流程，并证明 `week_id` 来自 DB。
4. 拿到 panda_quantflow 外层仓库路径后完成菜单/路由接入并验证 iframe。

历史复现详细记录见 [HISTORICAL_REPRODUCTION.md](HISTORICAL_REPRODUCTION.md)。
