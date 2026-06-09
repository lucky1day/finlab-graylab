# 当前状态

**更新日期**: 2026-06-10

> 2026-06-10 文档清理：已实现功能的计划/设计文档均已删除。当前仅保留 8 个参考文档 + 2 个 SOP（见 [README.md](README.md)）。

## 总览

当前代码侧只保留两个可发现、可调度的日频方案：

| 方案 | 频率 | Horizon | 目标 | 状态 |
|------|------|---------|------|------|
| `t1_daily` | `daily` | 1 | `5Y/10Y` | `active` |
| `t5_daily` | `daily` | 5 | `3Y/5Y/7Y/10Y` | `active` |

当前注册方案只有 `t1_daily` 与 `t5_daily`；周度方案（`weekly_10y_d_overlay` / `weekly_5y_direct_production` / `weekly_7y_cross_d_overlay`）已退役 / 代码未实现，如需周度方案须按 [新增方案 SOP](sop/SCHEME_ONBOARDING_SOP.md) 重新接入。

2026-06-09 已删除全部旧周频预测方案代码。删除原因是旧周频方案在预测/回测路径中使用计算型周历公式推导 `week_id <-> 交易日`，而不是读取 `bond_db.api_wind_date.week_id` 的实际口径，可能从入库时起造成特征周/目标周错位。后续周频方案需要按 [新增方案 SOP](sop/SCHEME_ONBOARDING_SOP.md) 重新入库，并强制使用 `api_wind_date.week_id`。

2026-06-09 已清理旧周频写库记录：`t_scheme_weekly_actuals` 整表清空，`t_scheme_registry`、`t_scheme_predictions`、`t_scheme_run_log`、`t_backtest_runs`、`t_backtest_predictions`、`t_backtest_monthly_metrics` 中旧周频 `scheme_id` 记录已删除；源表未修改。同日已修复 `scheduler/weekly_actuals_updater.py`，新生成的周频 actuals 只读 `api_wind_date.week_id` 与 `t_trade_calendar.trade_flag`，不再使用计算型周历公式。

## 架构与 Harness

强约束 harness 工程已从"文档设计"推进到"代码落地并可运行"。架构演进路线见 [CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md) §10，执行阶段 S0-S8 已完成：

- `shared.calendar_service` 提供交易日历/周历查询单点入口，交易日判断只读 `t_trade_calendar.trade_flag`，`week_id_for_date` 只读 `api_wind_date.week_id`。
- `shared.input_artifacts` 是所有预测 adapter 的输入文件生成入口；日频、周频、月频底层统一由 `shared.data_service` 生成输出宽表，artifact 层负责写出输入 CSV 并读回给算法。
- `harness/` 包已实现 StaticGate、InputGate、UnitGate、DryRunGate、CompareGate、BacktestGate、ApiGate、LiveGate、ActivationGate，以及 contracts、table_guard、authorization、orchestrator、persistence、CLI。
- `python -m harness onboard {scheme_id} --stage all` 可串联 static -> input -> unit -> dry-run -> compare -> backtest -> api（compare 缺 benchmark 时 SKIP，不阻断）；live/activate 不在 `all` 内，必须显式授权。

周频共享基础设施保留：

- `shared/calendar_service.py`：`t_trade_calendar` 交易日查询与 `api_wind_date` 周编号查询。
- `shared/input_artifacts.py`：`build_weekly_input_artifact()`。
- `shared/data_service.py`：`build_weekly_output_from_db()`。
- `scheduler/weekly_actuals_updater.py`：周频 actuals 刷新基础设施，周编号只读 `api_wind_date`，周内最后交易日只读 `t_trade_calendar`。
- 前端周度列支持：按 `frequency=weekly` 映射，scheme-agnostic。

已删除的旧周频专属内容包括方案目录、回测 runner、方案专属测试/脚本、计算型周历模块和 5Y/7Y legacy 源。

## 平台改造里程碑（P0 / P1）

基于 [架构评审报告](bond_factor_lab_architecture_review.md) 的两阶段改造已全部落地并通过独立验证。

### P0 — 控制面加固与安全边界（已完成）

- `harness` 作为一等模块纳入 `pyproject.toml`（`python -m harness` 可用、可 `pip install -e .`）。
- **CompareGate**：比较算法原始输出 vs 平台归档输出（缺 benchmark 时 SKIP）；已接入 `--stage all`（dry-run 与 backtest 之间）。
- **ActivationGate**：由 fail-closed 桩升级为真实激活（凭 `activate` token 把 `status: paused -> active`）。
- **StaticGate 加固**：递归扫描 `core/**/*.py`；`legacy_*` 不再是逃逸口；禁网络/子进程/pickle/写文件/跨方案 import；收紧 `predict.py` 导入白名单。
- **BacktestGate**：首次 no-persist 运行自动落地 baseline。
- **授权 token 软默认**：配置 `HARNESS_AUTH_SECRET` 时附 HMAC + TTL；未配置时退化为明文一次性确认闸（单用户本机无需配置，写库/激活仍需显式 token）。
- **backend GET 只读**：`GET /api/schemes` 不再触发 registry 写库（同步移到启动时 + 受保护的 `POST /api/admin/registry/sync`）；trigger/admin 接口软默认（未配置 `BOND_ADMIN_TOKEN` 则放行，仅监听 `127.0.0.1`）；CORS 由 `BOND_CORS_ORIGINS` 白名单替代通配符。
- **clean export 脚本** `scripts/export_clean_repo.sh`：基于 `git archive` 并自检产物不含 `.env`/`.git`/密钥/artifacts/reports。
- 周度方案在文档/产物中彻底退役（代码不存在），对齐为仅 `t1_daily` / `t5_daily`。

### P1 — 灰度实验室数据模型（已完成，独立验证通过）

S1→S7 串行落地，新增迁移 `005_lifecycle.sql` / `006_predictions_runid_uk.sql` / `007_backtest_immutable.sql`：

- **S1 生命周期表**：新增 `t_scheme_versions`、`t_harness_runs`、`t_harness_gate_results`、`t_input_artifacts`、`t_scheme_runs`、`t_scheme_serving_pointer`；现有表加列（向后兼容）。
- **S2 输入产物指纹**：`InputArtifact` 增 `content_hash`/`schema_hash`/`artifact_id`/`source_watermark`，经 `scheduler.repository.upsert_input_artifact` 幂等落 `t_input_artifacts`。
- **S3 不可变预测**：每次运行生成 `run_id`，`executor` 走 `create_scheme_run -> insert_run_predictions(run_id) -> update_serving_pointer -> write_run_log`；UK 改为含 `run_id`，同方案同日多 run 共存不覆盖；前端经 serving pointer 读"latest approved"。
- **S4 方案版本**：`shared.versioning` 计算 `code_hash`/`config_hash`/`manifest_hash`，落 `t_scheme_versions`。
- **S5 harness 留痕**：`harness.persistence` 把每次 run 与每个 gate 结果落 `t_harness_runs`/`t_harness_gate_results`（仅写这两表，DB 不可用时降级本地 JSON）。
- **S6 回测不可变**：每次回测 append 新 `backtest_run_id`，新增 `v_latest_backtest_run` 视图把"最新"改为查询语义，不再覆盖。
- **S7 backend 读切换**：GET 预测读路径经 `t_scheme_serving_pointer`，响应透出 `run_id`/`scheme_version`/`input_artifact_hash` 可追溯字段，保持只读。

**三条不变量经独立核验全部守住**：core 纯净（`schemes/*/core` 未被污染）、写库单点（新增写库仅在 `scheduler.repository` / `backtests.repository` / `harness.persistence`）、GET 只读。**全套单测 123/123 通过**（不设环境变量验证软默认路径），并经多轮 scratch MySQL 验证迁移幂等、重跑不覆盖、pointer 指向新 run、append/latest view、留痕与 trace 字段。

### 仍未做（P2，前端/分析体验，依赖 P1 数据模型，待排期）

方案 ranking、按 tenor/horizon/frequency 横向对比、shadow vs active 对比、confidence calibration、rolling hit ratio、drawdown/连错、生命周期页、异常告警（未出预测 / actuals 未回填 / 输入 stale）。

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
- 强约束 harness 已落地：[HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) 是方案入库总纲，[CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md) 是代码架构主蓝图，[SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) 是现行设计规范。
- artifact 命名已统一：`backtests/` 只放回测代码，`benchmarks/{benchmark_id}/` 只放 canonical 基准输入，运行期输入在 `backtest_artifacts/runtime_inputs/{scheme_id}/`，历史回测和数据差异报告在 `backtest_artifacts/backtests/{benchmark_id}/`。
- `t_target_registry` 当前展示 `3Y/5Y/7Y/10Y` 四个国债活跃目标；`1Y` 只保留为部分算法特征/审计输入，不作为当前前端目标。
- launchd 已安装并启动：
  - `com.bond-factor-lab.backend`
  - `com.bond-factor-lab.scheduler`
- 2026-06-09 周频方案删除后已重启 scheduler；`launchctl print` 显示 `com.bond-factor-lab.scheduler` 为 `running`。
- 前端已从真实 API 读取目标注册表和回测数据；T+1/T+5/周度筛选入口保留。
- 周频 actuals 代码口径已修复；只读预览生成 811 条 10Y 周度 actuals，按 `feature_date/target_date -> api_wind_date.week_id` 复核无错配。旧正式 DB 表 `t_scheme_weekly_actuals` 已清空，等待新周频方案重新入库后按新口径重刷。

## 当前数据库快照

核验时间：`2026-06-09`，数据库 `bond_db`，MySQL `8.0.45`。以下为清理旧周频写库记录后的数据库快照；源表保持只读未修改。

| 项 | 当前值 |
|----|--------|
| `api_wind_date` | 6,017 行 |
| `api_wind_daily` | 2,236,855 行，日期 `2010-01-01` 到 `2026-06-05` |
| `api_wind_derivative_daily` | 938,076 行，日期 `2010-01-01` 到 `2026-06-04` |
| `api_wind_weekly` | 121,617 行，周 `200901` 到 `202621` |
| `api_wind_derivative_weekly` | 277,064 行，周 `200901` 到 `202621` |
| `api_wind_indicators_all` | 1,637 |
| `t_trade_calendar` | 6,209 |
| `t_pre_market_forecast` | 1,059 |
| `t_shap` | 8,845 |
| `t_scheme_predictions` | 18 |
| `t_scheme_actuals` | 13,910 |
| `t_scheme_weekly_actuals` | 0 |
| `t_scheme_registry` | 2 |
| `t_scheme_run_log` | 6 |
| `t_target_registry` | 4 |
| `t_backtest_runs` | 6 |
| `t_backtest_predictions` | 6,933 |
| `t_backtest_monthly_metrics` | 357 |
| `t_backtest_reproduction_checks` | 3 |
| `bfl_probe_*` 影子表 | 0 |

actuals 覆盖：

| 期限 | 记录数 | 日期范围 |
|------|--------|----------|
| `1Y` | 2,520 | `2016-01-18` 到 `2026-06-05` |
| `3Y` | 2,520 | `2016-01-18` 到 `2026-06-05` |
| `5Y` | 2,519 | `2016-01-18` 到 `2026-06-05` |
| `7Y` | 2,520 | `2016-01-18` 到 `2026-06-05` |
| `10Y` | 3,831 | `2010-07-27` 到 `2026-06-05` |

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

DB 中旧周频回测 run 已清理；代码仓库中对应 runner 已删除。前端/API 当前只应看到保留的日频历史回测项。

## 正式预测状态

launchd scheduler 已成功运行 active 日度方案：

| 方案 | 预测日期 | 记录数 | 状态 |
|------|----------|--------|------|
| `t1_daily` | `2026-06-05` | 2 | `success` |
| `t5_daily` | `2026-06-05` | 4 | `success` |
| `t1_daily` | `2026-06-08` | 2 | `success` |
| `t5_daily` | `2026-06-08` | 4 | `success` |
| `t1_daily` | `2026-06-09` | 2 | `success` |
| `t5_daily` | `2026-06-09` | 4 | `success` |

旧周频 live prediction/run_log 记录已清理。scheduler 重启后，当前代码配置只会注册日频方案。

## API 与安全边界

- 已只读验证：
  - `GET /api/health`
  - `GET /api/targets`
  - `GET /api/predictions?limit=1`
- `GET /api/backtests/factor-lab` 已改为只读获取 scheme metadata，不再触发 registry sync。P0 后 `GET /api/schemes` 也已去除 registry 写副作用：registry 同步改为后端启动时执行一次，外加受保护的 `POST /api/admin/registry/sync`（未配置 `BOND_ADMIN_TOKEN` 时放行，配置后需 `X-Admin-Token`）。当前所有 GET 接口均为只读。
- 正式运行需要写库时，只应通过明确的调度器或运维命令写入 `t_scheme_predictions`、`t_scheme_run_log`、`t_scheme_actuals`、`t_scheme_weekly_actuals` 或 `t_backtest_*`，不要改动源数据表。

## 剩余观察项

1. 下一次日频 scheduler 运行后，确认日志只注册和执行 `t1_daily` / `t5_daily`。
2. 旧周频 DB 记录已清理；后端启动 registry 同步或调用 `POST /api/admin/registry/sync` 后，确认 registry 仍只保留 `t1_daily` / `t5_daily`（`GET /api/schemes` 已为只读，不再触发同步）。
3. 新周频方案进入时，必须按 [SCHEME_ONBOARDING_SOP.md](sop/SCHEME_ONBOARDING_SOP.md) 的 Intake -> Normalize -> Input Gate -> Static Gate -> Unit Gate -> Dry-run Gate -> Backtest Gate -> Live Gate -> Activation -> Documentation 流程，并证明 `week_id` 来自 DB。
4. 拿到 panda_quantflow 外层仓库路径后完成菜单/路由接入并验证 iframe。
