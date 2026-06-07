# 测试机环境基线

**日期**: 2026-06-06  
**机器**: 当前 Mac Studio 测试机  
**项目路径**: `/Users/macstudio0/bond-factor-lab`  
**状态**: 新模拟生产机器已完成服务环境、正式平台表、actuals、历史回测、周度 10Y 回测接入和 launchd 常驻验证；2026-06-05 09:25 已由常驻 scheduler 写入 t1/t5 正式预测记录，2026-06-06 已完成周度 10Y 受控 live 写库并启用周六 11:30 自动调度。

---

## 1. 仓库与目录状态

- 当前目录未携带 `.git` 元数据，`git status --short` 返回“not a git repository”。如后续需要版本管理，应先确认是否要重新初始化 Git 或从远端仓库重新拉取。
- 当前目录已包含业务运行所需代码和文档:
  - `shared/`: 环境变量化数据库配置、数据服务、统一模型。
  - `migrations/`: 正式平台表、backtest 表和 target registry 迁移脚本。
  - `schemes/t1_daily/`: T+1 adapter 与原始 core 副本，当前 active，目标 `5Y/10Y`。
  - `schemes/t5_daily/`: T+5 adapter 与原始 core 副本，当前 active，目标 `3Y/5Y/7Y/10Y`。
  - `schemes/weekly_10y_d_overlay/`: 周度 10Y D-overlay adapter 与原始 0529 core 副本，当前 active，目标 `10Y`，live cron 为周六 `11:30`。
  - `scheduler/`: 方案发现、子进程执行、统一写库、APScheduler 入口、actuals 刷新。
  - `backend/`: FastAPI API、指标计算、手动触发端点、静态前端 serve。
  - `frontend/`: AIFin Lab Shell 提取页面，已接入真实 API；T+1/T+5/周度入口均可展示回测结果。
  - `deploy/launchd/`: 后端和调度器 launchd plist，路径已改为当前新机器路径。
  - `backtests/`、`benchmarks/`、`backtest_artifacts/`: 历史复现 runner、基准输入和离线报告。
- `.env` 已存在，权限为 `600`，不应提交或外传。

## 2. MySQL 实例识别

项目目标数据库为本机 MySQL:

| 项 | 当前值 |
|----|--------|
| Host | `localhost` / `127.0.0.1` |
| Port | `3306` |
| Version | `8.0.45` |
| Database | `bond_db` |

确认结果:

- `bond_db` 已存在。
- 服务环境可通过项目 `.env` 连接 `bond_db`。
- 本轮文档复核只执行 `SELECT`、`SHOW COLUMNS`、`information_schema` 和只读 GET API，没有修改数据库。

## 3. bond_db 当前核心表状态

| 表 | 记录数 | 日期范围/备注 |
|----|--------|----------------|
| `api_wind_daily` | 2,235,209 | `2010-01-01` 到 `2026-06-05` |
| `api_wind_derivative_daily` | 937,312 | `2010-01-01` 到 `2026-06-04` |
| `api_wind_weekly` | 121,553 | `200901` 到 `202621` |
| `api_wind_derivative_weekly` | 277,064 | `200901` 到 `202621` |
| `api_wind_indicators_all` | 1,637 | 元数据表 |
| `t_trade_calendar` | 6,209 | 交易日历 |
| `t_pre_market_forecast` | 1,059 | 盘前预测相关源表 |
| `t_shap` | 8,845 | SHAP 相关源表 |
| `t_scheme_predictions` | 7 | `t1_daily` 2 条、`t5_daily` 4 条，预测日 `2026-06-05`；`weekly_10y_d_overlay` 1 条，预测日 `2026-06-06` |
| `t_scheme_actuals` | 13,900 | actuals 已到 `2026-06-03` |
| `t_scheme_weekly_actuals` | 794 | 10Y 周度 actuals，最新完整预测周六 `2026-05-23` |
| `t_scheme_registry` | 3 | `t1_daily` / `t5_daily` / `weekly_10y_d_overlay` |
| `t_scheme_run_log` | 4 | 2026-06-05 09:25 `t1_daily` / `t5_daily` 均 success；2026-06-06 周度受控 live 写库和单方案 scheduler 手动补跑均 success |
| `t_target_registry` | 4 | `3Y/5Y/7Y/10Y` |
| `t_backtest_runs` | 8 | t1/t5 三组数据源 + weekly 10Y |
| `t_backtest_predictions` | 7,022 | 历史回测预测明细 |
| `t_backtest_monthly_metrics` | 379 | 历史回测月度指标 |
| `t_backtest_reproduction_checks` | 1 | 最新数据一致性检查 |

当前不存在 `bfl_probe_*` 影子表。

## 4. 目标期限数据覆盖

当前 `api_wind_daily` 中可直接查到以下收益率代码:

| 指标代码 | 记录数 | 非空数 | 日期范围 |
|----------|--------|--------|----------|
| `TB0YWI0C` | 3,984 | 3,829 | `2010-01-04` 到 `2026-06-03` |
| `TB1YWI0C` | 3,984 | 2,518 | `2010-01-04` 到 `2026-06-03` |
| `TB3YWI0C` | 3,984 | 2,518 | `2010-01-04` 到 `2026-06-03` |
| `TB5YWI0C` | 3,984 | 2,517 | `2010-01-04` 到 `2026-06-03` |
| `TB7YWI0C` | 3,984 | 2,518 | `2010-01-04` 到 `2026-06-03` |

actuals 覆盖:

| 期限 | 记录数 | 日期范围 |
|------|--------|----------|
| `1Y` | 2,518 | `2016-01-18` 到 `2026-06-03` |
| `3Y` | 2,518 | `2016-01-18` 到 `2026-06-03` |
| `5Y` | 2,517 | `2016-01-18` 到 `2026-06-03` |
| `7Y` | 2,518 | `2016-01-18` 到 `2026-06-03` |
| `10Y` | 3,829 | `2010-07-27` 到 `2026-06-03` |

## 5. Python/conda 环境

### 5.1 算法预测环境

| 项 | 当前值 |
|----|--------|
| conda env | `forecast_env` |
| 路径 | `/Users/macstudio0/miniconda3/envs/forecast_env` |
| 用途 | 运行 t1/t5 算法核心和历史回测 |

算法预测 dry-run 已验证:

- `t1_daily` 返回 `5Y/10Y` 两条 `PredictionRecord`。
- `t5_daily` 返回 `3Y/5Y/7Y/10Y` 四条 `PredictionRecord`。
- `weekly_10y_d_overlay` 当前按 `feature_date` 从源表反查实际 `week_id` 运行；`2026-06-06` readiness 为 `ready=true`，使用源表 `feature_week_id=202621`，dry-run 返回 `10Y` 一条 `PredictionRecord`，`target_date=2026-06-12`，`predicted_direction=-1`。若 weekly close 尚未物化，adapter 会只读 `api_wind_daily` 在内存中生成 `TB0YWI3C/TB1YWI3C/TB5YWI3C`，不写回源表。

### 5.2 后端/API/调度器环境

| 项 | 当前值 |
|----|--------|
| conda env | `bond_factor_lab_service` |
| 路径 | `/Users/macstudio0/miniconda3/envs/bond_factor_lab_service` |
| Python | `3.12.13` |
| 依赖清单 | `requirements-service.txt` |

已验证服务依赖可 import: FastAPI、uvicorn、SQLAlchemy、PyMySQL、APScheduler、pydantic-settings、python-dotenv、PyYAML、chinese-calendar。

## 6. 服务与 API

FastAPI 后端当前由 launchd 运行在:

```text
http://127.0.0.1:8100/
```

只读验证结果:

- `GET /api/health` 返回 `{"status":"ok"}`。
- `GET /api/targets` 返回 4 个 active 目标: `3Y/5Y/7Y/10Y` 国债活跃。
- `GET /api/metrics/weekly_10y_d_overlay?tenor=10Y` 返回 1 条 live prediction row；因 `target_date=2026-06-12` 的 future actual 尚未产生，summary samples 暂为 0。
- `GET /api/backtests/factor-lab` 返回周度 10Y `framework_db_aligned` 矩阵数据，run_id=`13`，样本 45，正确 31，准确率 `68.9%`；`2025-07` 月度样本为 4，`2025-10` 月度样本为 5，`2026-01` 月度样本为 6。周频输入已切换为 `/Users/macstudio0/Desktop/wind_export(1).py` 口径公共导出层。

注意: `GET /api/backtests/factor-lab` 已改为只读获取 scheme metadata，不再触发 registry sync；`GET /api/schemes` 当前仍会间接同步 scheme registry，是正常服务接口，但不适合作为“只读数据库保护核验”的探针。

## 7. launchd 状态

模板路径:

- `deploy/launchd/com.bond-factor-lab.backend.plist`
- `deploy/launchd/com.bond-factor-lab.scheduler.plist`

当前模板均使用:

- `WorkingDirectory`: `/Users/macstudio0/bond-factor-lab`
- conda: `/Users/macstudio0/miniconda3/bin/conda`
- service env: `bond_factor_lab_service`
- algo env: `forecast_env`

当前 LaunchAgents 核验:

| Label | 状态 |
|-------|------|
| `com.bond-factor-lab.backend` | 由 launchd 管理；已通过 `GET /api/health` 验证 |
| `com.bond-factor-lab.scheduler` | 由 launchd 管理；已观察 2026-06-05 09:25 日度调度成功，2026-06-06 15:05 已注册周度 job `30 11 * * 6` |

PID 会随 `launchctl kickstart` 或 KeepAlive 拉起而变化，以实时 `launchctl list` 为准。

日志路径:

- `/tmp/bond-factor-lab-backend.log`
- `/tmp/bond-factor-lab-backend.err`
- `/tmp/bond-factor-lab-scheduler.log`
- `/tmp/bond-factor-lab-scheduler.err`

## 8. 当前方案与前端口径

| 方案 | 频率 | Horizon | 目标 | 状态 |
|------|------|---------|------|------|
| `t1_daily` | `daily` | 1 | `5Y/10Y` | `active` |
| `t5_daily` | `daily` | 5 | `3Y/5Y/7Y/10Y` | `active` |
| `weekly_10y_d_overlay` | `weekly` | 6 | `10Y` | `active` |

前端会在 `10Y国债活跃 · 周度` 格子展示 `weekly_10y_d_overlay` 的最新 `framework_db_aligned` 回测结果: run_id=`13`，45 个样本，31 个正确，整体准确率 `68.9%`；点击该格子后排行显示 `68.9%（31/45）`。`2025-07` 有 4 个样本，`2025-10` 有 5 个样本，`2026-01` 有 6 个样本。2026-06-06 已完成一次受控 live 写库和一次单方案 scheduler 手动补跑，写入 `predict_date=2026-06-06`、`target_date=2026-06-12`、`predicted_direction=-1`、`confidence=0.28` 的正式预测；prediction 通过 UPSERT 保持 1 条，weekly run_log 累计 2 条 success。2026-06-06 15:05 已切换为 active 并重启 scheduler，首次自动运行待下一次周六 11:30 观察。

周度 live 时间节点参考旧实盘 cron: `30 11 * * 6 bash /Users/macstudio0/bondprojectpro/forecast_project/weekly_project/run_weekly_pipeline.sh multi`。该脚本会在周六 11:30 首轮运行，再在 16:00 和 22:00 检查/补跑；BFL 周度方案使用同一 cron `30 11 * * 6`。

## 9. 后续检查项

- 继续观察 16:00 actuals 刷新窗口，确认 `t_scheme_actuals` 按数据源节奏更新。
- 观察下一次周六 11:30 周度自动 live 写库，确认只影响 `weekly_10y_d_overlay` 的 prediction/run_log。
- 拿到 panda_quantflow 外层仓库路径后完成 iframe 菜单/路由接入。
