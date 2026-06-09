# 测试机环境基线

**日期**: 2026-06-06  
**机器**: 当前 Mac Studio 测试机  
**项目路径**: `/Users/macstudio0/bond-factor-lab`  
**状态**: 新模拟生产机器已完成服务环境、正式平台表、actuals、历史回测和 launchd 常驻验证；2026-06-05 09:25 已由常驻 scheduler 写入 t1/t5 正式预测记录。早先文档曾描述的周度方案（`weekly_10y_d_overlay` / `weekly_5y_direct_production` / `weekly_7y_cross_d_overlay`）现已退役/代码未实现，相关 DB 记录已清理；当前在库方案只有 `t1_daily` / `t5_daily`。

---

## 1. 仓库与目录状态

- 当前目录已本地初始化 Git；baseline commit 已打 tag `baseline/new-machine-2026-06-07`，规整工作在 `cleanup/scheme-framework-20260607` 分支推进。
- 当前目录已包含业务运行所需代码和文档:
  - `shared/`: 环境变量化数据库配置、数据服务、统一模型。
  - `migrations/`: 正式平台表、backtest 表和 target registry 迁移脚本。
  - `schemes/t1_daily/`: T+1 adapter 与原始 core 副本，当前 active，目标 `5Y/10Y`。
  - `schemes/t5_daily/`: T+5 adapter 与原始 core 副本，当前 active，目标 `3Y/5Y/7Y/10Y`。
  - 周度方案（`weekly_10y_d_overlay` 等）已退役/代码未实现，`schemes/` 下当前只有 `t1_daily` / `t5_daily` 两个方案目录。
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
| `t_scheme_predictions` | 7 | `t1_daily` 2 条、`t5_daily` 4 条，预测日 `2026-06-05`；另 1 条为早先周度方案的历史 live 记录，该周度方案已退役、记录已于 2026-06-09 清理 |
| `t_scheme_actuals` | 13,900 | actuals 已到 `2026-06-03` |
| `t_scheme_weekly_actuals` | 794 | 早先 10Y 周度 actuals；周度方案已退役，该表已于 2026-06-09 清空，当前无周度方案写入 (no weekly scheme currently writes here) |
| `t_scheme_registry` | 3 | 当时含 `t1_daily` / `t5_daily` 及一个周度方案；周度方案已退役，registry 当前只保留 `t1_daily` / `t5_daily` |
| `t_scheme_run_log` | 4 | 2026-06-05 09:25 `t1_daily` / `t5_daily` 均 success；另 2 条来自早先周度方案的受控写库/手动补跑，该周度方案已退役、记录已于 2026-06-09 清理 |
| `t_target_registry` | 4 | `3Y/5Y/7Y/10Y` |
| `t_backtest_runs` | 8 | t1/t5 三组数据源 + 早先周度回测；周度方案已退役，其 `t_backtest_*` 记录已于 2026-06-09 清理 |
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

（早先文档曾记录的周度 10Y dry-run 已随周度方案退役/代码未实现而失效，不再适用。）

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
- 早先针对周度方案的 `GET /api/metrics/...` 与 `GET /api/backtests/factor-lab` 周度 10Y 矩阵已随周度方案退役/代码未实现而下线；当前 factor-lab 只返回 `t1_daily` / `t5_daily` 的日度回测数据。

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
| `com.bond-factor-lab.scheduler` | 由 launchd 管理；已观察 2026-06-05 09:25 日度调度成功。早先注册的周度 job（`30 11 * * 6`）已随周度方案退役/代码未实现而移除 |

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

> 周度方案（`weekly_10y_d_overlay` / `weekly_5y_direct_production` / `weekly_7y_cross_d_overlay`）已退役/代码未实现，不在当前方案表内；如需周度方案须按 SOP 重新接入。

前端 `10Y国债活跃 · 周度` 格子当前无对应方案回测数据（周度方案已退役）；前端只展示 `t1_daily` / `t5_daily` 的日度回测结果。

周度 live 时间节点曾参考旧实盘 cron `30 11 * * 6 ... multi`；该周度方案已退役，BFL 当前不注册任何周度 cron。

## 9. 后续检查项

- 继续观察 16:00 actuals 刷新窗口，确认 `t_scheme_actuals` 按数据源节奏更新。
- 拿到 panda_quantflow 外层仓库路径后完成 iframe 菜单/路由接入。
