# 当前状态

**更新日期**: 2026-06-11

> 2026-06-10 新增周度方案 `weekly_5y_direct_0529`，按 SOP 全流程入库；同日修正前端/API/回测验证口径：日频与周频月度统计、明细日期均按 `target_date`（目标交易日）归属，`predict_date` 仅用于调度日志和运行记录。

## 总览

当前代码侧保留三个可发现、可调度的方案：

| 方案 | 频率 | Horizon | 目标 | 状态 |
|------|------|---------|------|------|
| `t1_daily` | `daily` | 1 | `5Y/10Y` | `active` |
| `t5_daily` | `daily` | 5 | `3Y/5Y/7Y/10Y` | `active` |
| `weekly_5y_direct_0529` | `weekly` | 6 | `5Y` | `active` |
| `weekly_7y_cross_d_overlay_0529` | `weekly` | 6 | `7Y` | `active` |

2026-06-10 新增周度方案 `weekly_5y_direct_0529`，按 [SCHEME_ONBOARDING_SOP](sop/SCHEME_ONBOARDING_SOP.md) 完整通过了 Intake → Normalize → Input/Static/Unit/Dry-run Gate → Live Gate → API 验证 → Activation 全流程。方案采用 3 规则加权投票算法（7Y-10Y 利差动量 + 5Y-10Y 利差反转 + 1Y 动量），所有 week_id↔日期 映射只读 `api_wind_date.week_id`，禁止日历公式计算。

2026-06-10 新增周度方案 `weekly_7y_cross_d_overlay_0529`，覆盖 `7Y`、horizon=6、周六 11:30 调度。原始脚本归档为 `schemes/weekly_7y_cross_d_overlay_0529/core/legacy_weekly_7y_cross_d_overlay_0529.py.txt`，活跃 core 为纯 DataFrame 算法；adapter 与回测 runner 均通过 `shared.input_artifacts.build_weekly_input_artifact()` 取数，`week_id` / `target_week_id` / `target_date` 均经 `shared.calendar_service` 读取 DB 日历。源文件原始回测（`/Users/macstudio0/Desktop/weekly_7y_cross_d_overlay_0529 (1).py` + `/Users/macstudio0/Desktop/weekly_output.csv`）与入库后回测在源文件窗口内 43 条样本逐 `feature_week_id` 对齐：方向差异 0、label 差异 0、confidence 最大差异 0、`target_date` 月度 accuracy 差异 0；四份 benchmark 文件已落在 `schemes/weekly_7y_cross_d_overlay_0529/benchmarks/`，且 `backtest.benchmark_required=true`。`python -m harness onboard weekly_7y_cross_d_overlay_0529 --predict-date 2026-06-06 --stage all` 已通过，CompareGate 为 `passed`；API gate 确认 `/api/backtests/factor-lab` 中 `7Y` 周度格存在，monthly_rows=124。ActivationGate 已用一次性 `activate` token 登记当前 active 版本，scheme_version=`27ece22d45f4`，并已重启 scheduler。

旧周度方案（`weekly_10y_d_overlay` / `weekly_5y_direct_production` / `weekly_7y_cross_d_overlay`）已于 2026-06-09 退役。

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

**三条不变量经独立核验全部守住**：core 纯净（`schemes/*/core` 未被污染）、写库单点（新增写库仅在 `scheduler.repository` / `backtests.repository` / `harness.persistence`）、GET 只读。**全套单测 165/165 通过**（服务环境 `bond_factor_lab_service`），并经多轮 scratch MySQL 验证迁移幂等、重跑不覆盖、pointer 指向新 run、append/latest view、留痕与 trace 字段。

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
- `weekly_5y_direct_0529` 已完成 adapter + harness 全流程入库，当前 active，预测目标 `5Y`，horizon=6（周度）。
- `weekly_7y_cross_d_overlay_0529` 已完成 adapter + harness 全流程入库，当前 active，预测目标 `7Y`，horizon=6（周度）；dry-run `predict_date=2026-06-06` 产出 `feature_date=2026-06-05`、`target_date=2026-06-12`，证明周六预测指向下一周最后交易日。
- 强约束 harness 已落地：[HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) 是方案入库总纲，[CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md) 是代码架构主蓝图，[SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) 是现行设计规范。
- artifact 命名已统一：`backtests/` 只放回测代码，`benchmarks/{benchmark_id}/` 只放 canonical 基准输入，运行期输入在 `backtest_artifacts/runtime_inputs/{scheme_id}/`，历史回测和数据差异报告在 `backtest_artifacts/backtests/{benchmark_id}/`。
- `t_target_registry` 当前展示 `3Y/5Y/7Y/10Y` 四个国债活跃目标；`1Y` 只保留为部分算法特征/审计输入，不作为当前前端目标。
- launchd 已安装并启动：
  - `com.bond-factor-lab.backend`
  - `com.bond-factor-lab.scheduler`
- 2026-06-09 周频方案删除后已重启 scheduler；`launchctl print` 显示 `com.bond-factor-lab.scheduler` 为 `running`。
- 前端已从真实 API 读取目标注册表和回测数据；T+1/T+5/周度筛选入口保留。日频 T+N 与周度明细、月度指标均按 `target_date` 作为目标交易日归属；真实方向仍分别匹配 `t_scheme_actuals.trade_date = target_date` 与 `t_scheme_weekly_actuals.target_date = target_date`。`feature_date` 仅用于追溯算法实际消费的数据窗口，`predict_date` 仅用于调度日志和运行记录；同一 `target_date` 下同方案同标的同 horizon 由 UK + UPSERT 保持唯一。
- 周频 actuals 代码口径已修复并重刷入库；当前 `t_scheme_weekly_actuals` 2,927 条，按 `feature_date/target_date -> api_wind_date.week_id` 复核无错配。

## 当前数据库快照

核验时间：`2026-06-10`，数据库 `bond_db`，MySQL `8.0.45`。

| 项 | 当前值 |
|----|--------|
| `api_wind_date` | 6,017 行 |
| `api_wind_daily` | 2,237,475 行，日期 `2010-01-01` 到 `2026-06-10` |
| `api_wind_derivative_daily` | 938,265 行，日期 `2010-01-01` 到 `2026-06-09` |
| `api_wind_weekly` | 121,617 行，周 `200901` 到 `202621` |
| `api_wind_derivative_weekly` | 277,064 行，周 `200901` 到 `202621` |
| `api_wind_indicators_all` | 1,637 |
| `t_trade_calendar` | 6,209 |
| `t_pre_market_forecast` | 1,059 |
| `t_shap` | 8,845 |
| `t_scheme_predictions` | 83 |
| `t_scheme_actuals` | 13,919 |
| `t_scheme_weekly_actuals` | 2,927 |
| `t_scheme_registry` | 3 |
| `t_scheme_run_log` | 38 |
| `t_target_registry` | 4 |
| `t_backtest_runs` | 21 |
| `t_backtest_predictions` | 22,308 |
| `t_backtest_monthly_metrics` | 1,452 |
| `t_backtest_reproduction_checks` | 6 |
| `bfl_probe_*` 影子表 | 0 |

actuals 覆盖：

| 期限 | 记录数 | 日期范围 |
|------|--------|----------|
| `1Y` | 2,521 | `2016-01-18` 到 `2026-06-08` |
| `3Y` | 2,522 | `2016-01-18` 到 `2026-06-09` |
| `5Y` | 2,521 | `2016-01-18` 到 `2026-06-09` |
| `7Y` | 2,522 | `2016-01-18` 到 `2026-06-09` |
| `10Y` | 3,833 | `2010-07-27` 到 `2026-06-09` |

## 历史回测状态

历史复现结果写入独立 backtest 表，不混入 `t_scheme_predictions`。当前代码侧保留的历史回测方案如下：

| 方案 | 数据源 | 状态 | 日期范围 |
|------|--------|------|----------|
| `t1_daily` | `baseline_original_csv` | `success` | `2025-01-02` 到 `2026-05-28` |
| `t1_daily` | `framework_original_csv` | `success` | `2025-01-02` 到 `2026-05-28` |
| `t1_daily` | `framework_db_aligned` | `success`，run_id=`79` | `2025-01-02` 到 `2026-05-28` |
| `t5_daily` | `baseline_original_csv` | `success` | `2025-01-01` 到 `2026-05-31` |
| `t5_daily` | `framework_original_csv` | `success` | `2025-01-01` 到 `2026-05-31` |
| `t5_daily` | `framework_db_aligned` | `success`，run_id=`76` | `2025-01-01` 到 `2026-05-31` |
| `weekly_5y_direct_0529` | `framework_db_aligned` | `success`，run_id=`80` | `2016-02-27` 到 `2026-05-30` |
| `weekly_7y_cross_d_overlay_0529` | `framework_db_aligned` | `success`，run_id=`89` | `2016-03-12` 到 `2026-05-30` |

DB 中旧周频回测 run 已清理；当前新接入的周频方案 `weekly_5y_direct_0529` 已按 DB 周历完成历史回测落库，最新 `framework_db_aligned` run_id=`80`。

`weekly_7y_cross_d_overlay_0529` 已按 DB 周历完成历史回测落库，最新 `framework_db_aligned` run_id=`89`，`t_backtest_predictions` 472 条、`t_backtest_monthly_metrics` 124 条；整体样本 472、正确 294、accuracy=62.3%，`evaluation_filter.date_field=target_date`。`/api/backtests/factor-lab` 返回该方案 `frequency=weekly`、`horizon=6`、`tenor=7Y`、overall=62.3。该周频方案已补齐源文件原始回测 benchmark：`original_predictions_sample.csv` / `current_predictions_sample.csv` 各 43 条，`original_backtest_summary.json` / `current_backtest_summary.json` 按 `target_date` 覆盖 12 个目标月；CompareGate 最新证据为 direction_match_rate=1.0、max_confidence_abs_diff=0、missing/extra=0。

2026-06-10 已按 `target_date` 月度口径重跑日频和周频回测，最新 run_id：`t5_daily` baseline/framework-csv/framework-db 分别为 `74/75/76`，`t1_daily` baseline/framework-csv/framework-db 分别为 `77/78/79`，`weekly_5y_direct_0529` framework-db 为 `80`。`canonical_csv_vs_upstream_db_generated` 最新数据检查的整体状态仍为 `failed`，失败来自非目标字段/静态 CSV 与源 DB 的缺失及微小数值差异；目标列 `TB0YWI0C/TB1YWI0C/TB3YWI0C/TB5YWI0C/TB7YWI0C` 最大差异均为 0，且 `framework_db_comparison` 为 0 mismatch。

## 正式预测状态

launchd scheduler 已成功运行 active 方案：

| 方案 | 预测日期 | 记录数 | 状态 |
|------|----------|--------|------|
| `t5_daily` | `2026-05-26` 到 `2026-05-29` | 4/日 | `success`，run_id=`18..21` |
| `t1_daily` | `2026-05-29` 到 `2026-06-10` | 2/日 | `success`，run_id=`22..30` |
| `t5_daily` | `2026-06-05`、`2026-06-08`、`2026-06-09` | 4/日 | `success`，run_id=`15..17` |
| `weekly_5y_direct_0529` | `2026-06-11` | 1 | `success`，run_id=`9` |
| `weekly_7y_cross_d_overlay_0529` | `2026-06-06`（回补） | 1 | `success`，run_id=`35` |

2026-06-10 周度方案 `weekly_5y_direct_0529` 首次入库，`run_id=9`，predict_date=2026-06-11，feature_week_id=202620 → target_week_id=202621，feature_date=2026-05-29，target_date=2026-06-05，预测方向=1（上行），与 actuals 对比准确。API metrics 按 `target_date` 归入 2026-06，返回 accuracy=100%（1/1）；2026-05 无该 live 样本。

2026-06-10 周度方案 `weekly_5y_direct_0529` post-onboarding SOP 已完成：原始基准按 DB 周历归一化后 503 条，改造后 framework 复现 503 条，S5 方向差异 0；按 `target_date` 月度口径重新正式回测落库 run_id=`80`，`t_backtest_predictions` 503 条、`t_backtest_monthly_metrics` 124 条；其中 2026-05 为 5 条、2026-06 为 0 条；`/api/backtests/factor-lab` 与 DB 逐 `tenor × month` 比对 124 格，差异 0；scheduler 已确认注册周六 11:30 任务（`30 11 * * 6`）。

2026-06-11 按 SOP Step 10b 回补 `weekly_7y_cross_d_overlay_0529` 实盘预测：`execute_scheme(cfg, '2026-06-06')` 成功，run_id=`35`，predict_date=2026-06-06 → target_date=2026-06-12，方向=1；`t_scheme_run_log` id=48 留痕；API 已返回该 live row（actual=None，前端显示 06/12 待验证），实盘发出起点分隔线 2026-05-30 由前端按 target 月份自动反推。坑 12 检查清单 4 项全部通过。

2026-06-10 已重刷日频 actuals 至源表可用水位，并补跑 `t5_daily` 的 2026-05-26 到 2026-05-29（run_id=18..21）以及 `t1_daily` 的 2026-05-29 到 2026-06-10（run_id=22..30）。当前 6 月日频明细按目标日展示；对应目标日有 actuals 时直接计结果，目标日尚无 actuals 时保持 `待验证`，不会显示为“平”。

2026-06-10 已验证前端/DB 一致性：`python -m scripts.verify_frontend_db --scheme-id t5_daily --run-id 76` 检查 68 格、0 mismatch；`t1_daily --run-id 79` 检查 36 格、0 mismatch（DB 中 `1Y` 回测格被前端目标注册表隐藏）；`weekly_5y_direct_0529 --run-id 80` 检查 124 格、0 mismatch。scheduler 挂载也已复核通过：`t1_daily` / `t5_daily` 注册工作日 09:25，`weekly_5y_direct_0529` 注册周六 11:30，launchd scheduler 为 running。

旧周频 live prediction/run_log 记录已清理。scheduler 重启后，当前代码配置会注册日频方案与 `weekly_5y_direct_0529`、`weekly_7y_cross_d_overlay_0529` 周频方案。

## API 与安全边界

- 已只读验证：
  - `GET /api/health`
  - `GET /api/targets`
  - `GET /api/predictions?limit=1`
- `GET /api/backtests/factor-lab` 已改为只读获取 scheme metadata，不再触发 registry sync。P0 后 `GET /api/schemes` 也已去除 registry 写副作用：registry 同步改为后端启动时执行一次，外加受保护的 `POST /api/admin/registry/sync`（未配置 `BOND_ADMIN_TOKEN` 时放行，配置后需 `X-Admin-Token`）。当前所有 GET 接口均为只读。
- 正式运行需要写库时，只应通过明确的调度器或运维命令写入 `t_scheme_predictions`、`t_scheme_run_log`、`t_scheme_actuals`、`t_scheme_weekly_actuals` 或 `t_backtest_*`，不要改动源数据表。

## 剩余观察项

1. 下一次 scheduler 运行后，继续确认日志只注册和执行 `t1_daily` / `t5_daily` / `weekly_5y_direct_0529` / `weekly_7y_cross_d_overlay_0529`。
2. 后续新周频方案进入时，继续按 [SCHEME_ONBOARDING_SOP.md](sop/SCHEME_ONBOARDING_SOP.md) 流程，并证明 `week_id` 来自 `api_wind_date` 而非公式计算。
3. 2026-06-10 修复了 `backend/services.py` 中周度 metrics 的 JOIN 条件：去掉 `wa.predict_date = p.predict_date`（周度 predict_date 语义在 prediction 和 actuals 间不一致），仅按 `tenor + target_date` 匹配。
4. 2026-06-10 修复日频 T+N 与周频前端/API/回测口径：月度指标与明细均按 `target_date` 归属，真实方向按 `target_date` join；`feature_date` 只作为输入窗口追溯字段，`predict_date` 只作为调度日志和运行记录；未来目标日可先显示为 `待验证`。
5. 拿到 panda_quantflow 外层仓库路径后完成菜单/路由接入并验证 iframe。
