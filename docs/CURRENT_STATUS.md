# 当前状态

**更新日期**: 2026-06-14

> 2026-06-14 文档已按当前 DB、代码目录、live 语义修复和 latest 回测重建状态刷新。新增方案入口统一为 [PREDICTION_SEMANTICS.md](PREDICTION_SEMANTICS.md) 与 [sop/SCHEME_ONBOARDING_T0.md](sop/SCHEME_ONBOARDING_T0.md)；平台统一使用 `predict_date`（信号发出日）、`feature_date`（数据截止日/预测站位日）、`target_date`（验证目标日）。日频与周频月度统计、明细日期均按 `target_date` 归属；灰度实盘与正式实盘需通过 `prediction_phase=gray_live/scheduled_live` 区分。回测起点 `2025-01-01` 是输出样本起点，不是训练历史裁剪点。
> 2026-06-14 追加 registry 单表 per-tenor 语义：`t_scheme_registry` 每一行就是前端/业务定义的一个方案，唯一身份为 composite `scheme_id = {base_scheme_id}__h{horizon}__{target_tenor}`。算法目录、scheduler、`PredictionRecord` 和 backtest 表继续使用 base `scheme_id`；前端、`/api/schemes`、`/api/metrics/{scheme_id}`、`/api/predictions?scheme_id=...` 和 `/api/backtests/factor-lab` 只使用 `status='active'` 的 registry composite `scheme_id`。`/api/metrics/{base_scheme_id}?tenor=...` 已废弃且不兼容；`paused` / `archived` registry 行只用于管理或审计，不进入当前前端/业务 API，也不允许 trigger 或 scheduler 新写入该 target。
> 2026-06-14 指标口径已统一：预测为“平”的样本计入 `samples` / `sample_count` 和方向分布，但不进入整体准确率、上涨/下跌准确率、上涨/下跌召回率等任何指标分母；平台新增 `metric_samples` / `metric_sample_count` 表达真实指标分母，前端准确率括号展示 `correct/metric_samples`，样本数列仍展示总样本数；每日/周度验证明细中预测为“平”的行结果列展示 `-`。
> 2026-06-14 回测前端指标事实源已收敛：`/api/backtests/factor-lab` 只从 latest run 的 `t_backtest_predictions` 明细动态聚合月度指标和 summary；正常 backtest runner 不再写独立月度指标汇总，后端也不再暴露旧月度汇总读入口。latest run 缺少回测明细时接口 fail-closed。
> 2026-06-14 benchmark 对齐口径已明确：原始算法 benchmark 中的 `T/date/predict_date` 表示 source T / 预测站位日，进入平台后必须对齐数据库明细的 `feature_date`，不是对齐实盘语义下的 `predict_date`。若 benchmark 样本的 `target_date` 已进入灰度/实盘观察区，则与 `t_scheme_predictions.feature_date` 对齐；仍在历史回测区间的样本与 `t_backtest_predictions.feature_date` 对齐。
> 2026-06-14 `t1_daily` / `t5_daily` 已重建严格逐方案 benchmark baseline：根目录 `benchmarks/model_muti_0529/` 保留为批次级 canonical 输入归档，真正供 CompareGate 使用的 original/current 文件位于 `schemes/{scheme_id}/benchmarks/`；字段固定为 `feature_date,target_date,target_tenor,horizon,direction,confidence,label,is_correct`。`t1_daily` 为 674 行、仅含 `5Y/10Y`；`t5_daily` 为 1332 行、含 `3Y/5Y/7Y/10Y`；两者已从旧的 `PASS_WITH_LEGACY_SAMPLE_LIMITATIONS` 升级为严格 `PASS`。0529 canonical CSV 截至 `2026-05-28`，逐方案 benchmark 通过 DB target completion 补齐 `2026-05-29` 目标验证日：T1 最后一条为 `feature_date=2026-05-28 -> target_date=2026-05-29`，T5 最后一周为 `feature_date=2026-05-18..2026-05-22 -> target_date=2026-05-25..2026-05-29`。
> 2026-06-14 `daily_5y_2_v28` 的 predict/backtest inference 已收敛到同一个共享入口。该方案属于 test-window 敏感算法，核心窗口固定为 `feature_date` 所在月月初到 `feature_date`；旧连续窗口口径写入的 `run_id=42` 灰度预测明细已删除并由 `run_id=58` 重跑修复，`feature_date=2026-05-28` 当前为 `predicted_direction=1`、`confidence=1.0`。新的 V28 回测 no-persist 对比已完成，但最新 backtest run 仍等待人工确认后再落库覆盖 canonical latest。

## 总览

当前代码侧保留六个 active 可调度方案：

| 方案 | 频率 | Horizon | 目标 | 状态 |
|------|------|---------|------|------|
| `t1_daily` | `daily` | 1 | `5Y/10Y` | `active` |
| `t5_daily` | `daily` | 5 | `3Y/5Y/7Y/10Y` | `active` |
| `weekly_5y_direct_0529` | `weekly` | 6 | `5Y` | `active` |
| `weekly_7y_cross_d_overlay_0529` | `weekly` | 6 | `7Y` | `active` |
| `weekly_10y_d_overlay_0529` | `weekly` | 6 | `10Y` | `active` |
| `daily_5y_2_v28` | `daily` | 5 | `5Y` | `active` |

2026-06-10 新增周度方案 `weekly_5y_direct_0529`，按 [SCHEME_ONBOARDING_SOP](sop/SCHEME_ONBOARDING_SOP.md) 完整通过了 Intake → Normalize → Input/Static/Unit/Dry-run Gate → Live Gate → API 验证 → Activation 全流程。方案采用 3 规则加权投票算法（7Y-10Y 利差动量 + 5Y-10Y 利差反转 + 1Y 动量），所有 week_id↔日期 映射只读 `api_wind_date.week_id`，禁止日历公式计算。2026-06-13 删除严格 PIT 旧 run_id=`104` 后，按 source-original batch reproduction 重建 latest run_id=`109`，历史回测仍按灰度实盘起点截断到 `target_date < 2026-06-01`，monthly_rows=17，original benchmark 覆盖区间 71/71 matched。实盘/灰度 adapter 仍严格使用 `feature_date`、`end_week=feature_week_id`、`as_of_date=feature_date`。

2026-06-10 新增周度方案 `weekly_7y_cross_d_overlay_0529`，覆盖 `7Y`、horizon=6、周六 11:30 调度。原始脚本归档为 `schemes/weekly_7y_cross_d_overlay_0529/core/legacy_weekly_7y_cross_d_overlay_0529.py.txt`，活跃 core 为纯 DataFrame 算法；adapter 与回测 runner 均通过 `shared.input_artifacts.build_weekly_input_artifact()` 取数，`week_id` / `target_week_id` / `target_date` 均经 `shared.calendar_service` 读取 DB 日历。源文件原始回测（`/Users/macstudio0/Desktop/weekly_7y_cross_d_overlay_0529 (1).py` + `/Users/macstudio0/Desktop/weekly_output.csv`）与入库后回测在历史 benchmark 覆盖窗口内 42 条样本逐 `feature_week_id` 对齐：方向差异 0、label 差异 0、confidence 最大差异 0、`target_date` 月度 accuracy 差异 0；这里的 confidence 为原始算法 `cross_d_prob_up` 概率输出映射到平台统一字段，不是平台另造指标。四份 benchmark 文件已落在 `schemes/weekly_7y_cross_d_overlay_0529/benchmarks/`，且 `backtest.benchmark_required=true`。2026-06-13 删除严格 PIT 旧 run_id=`105` 后，按 source-original batch reproduction 重建 latest run_id=`110`，历史回测仍按灰度实盘起点截断到 `target_date < 2026-06-01`，monthly_rows=17，original benchmark 覆盖区间 42/42 matched。实盘/灰度 adapter 仍严格使用 `feature_date`、`end_week=feature_week_id`、`as_of_date=feature_date`。ActivationGate 已用一次性 `activate` token 登记当前 active 版本，scheme_version=`27ece22d45f4`，并已重启 scheduler。

2026-06-11 新增周度方案 `weekly_10y_d_overlay_0529`，覆盖 `10Y`、horizon=6、周六 11:30 调度。原始脚本归档为 `schemes/weekly_10y_d_overlay_0529/core/legacy_weekly_10y_d_overlay_0529.py.txt`，活跃 core 为纯 DataFrame 算法；adapter 与回测 runner 均通过 `shared.input_artifacts.build_weekly_input_artifact()` 取数，`week_id` / `target_week_id` / `target_date` 均经 `shared.calendar_service` 读取 DB 日历。源文件原始回测（`/Users/macstudio0/Desktop/weekly_10y_d_overlay_0529 (1).py`）与入库后回测在已有 benchmark 覆盖窗口内 45 条样本逐 `feature_week_id` 对齐：方向差异 0、`target_date` 差异 0、`label/is_correct` 差异 0、confidence 最大差异在 `1e-12` 容差内；这里的 confidence 为原始算法 `d_prob_up`（score/model2 overlay 后概率）映射到平台统一字段。四份 benchmark 文件已落在 `schemes/weekly_10y_d_overlay_0529/benchmarks/`，且 `backtest.benchmark_required=true`。`python -m harness onboard weekly_10y_d_overlay_0529 --predict-date 2026-06-06 --stage all` 已通过，CompareGate 为 `passed`；API gate 确认 `/api/backtests/factor-lab` 中 `10Y` 周度格存在。2026-06-13 删除严格 PIT 旧 run_id=`106` 后，按 source-original batch reproduction 重建 latest run_id=`108`，历史回测仍按灰度实盘起点截断到 `target_date < 2026-06-01`，monthly_rows=17；2026-06 只由 gray/live metrics 展示。ActivationGate 已用一次性 `activate` token 登记当前 active 版本，已通过 registry 同步进入 `t_scheme_registry`，并已重启 scheduler。

2026-06-14 复查确认，三个周频方案 latest 回测的日期窗口已经对齐，但总样本数按各自算法 core 的有效输出自然不同：`weekly_5y_direct_0529` run_id=`109` 为 71 条，缺 `feature_week_id=202534`；`weekly_7y_cross_d_overlay_0529` run_id=`110` 为 68 条，缺 `202529/202534/202547/202608`；`weekly_10y_d_overlay_0529` run_id=`108` 为 72 条，无缺周。缺周来自源算法对规则信号 `0` / 无效信号的过滤和 inner join 组合逻辑，不是前端隐藏、latest view 错误或 DB 日历缺失。候选排行里的“样本总数”含义是有效预测行数，不是日历周数量；平台不得为了凑齐行数补写空预测。

2026-06-12 完成日频 V28 方案 `daily_5y_2_v28`（对应外部 `5y_2`，因 scheme_id 规范采用平台名）入库。adapter 实盘语义为 `predict_date=signal_date=T+1`、`feature_date=T`（上一交易日），`target_date=T+5`；`anchor_date` 仅作为方案内部/审计字段保留，业务和前端统一使用 `feature_date`。回测 runner 语义为 `predict_date=feature_date=T`，并排除 `target_date >= 2026-06-01` 的灰度/实盘 target 区间。方案声明 `input_spec.auxiliary_inputs`，InputGate 会构建 daily/weekly/monthly 三类 artifact；active core 只消费原 V28 代码白名单中的 `WEEKLY_COLS` / `MONTHLY_COLS`，避免把 artifact 全量列误引入特征空间。模型更新语义保留原 V28 设计：每个预测锚点滚动重训 LGBM，训练 mask 使用 `all_idx < idx - horizon` 防泄漏；IC screening 固定在 `2024-07-01` 前；LGBM top-K 按月用此前 test-window 表现更新；信号组合按该方案 `rebal=monthly` 更新。2026-06-14 修复确认，V28 是 test-window 敏感算法，Phase C 的 monthly ensemble / signal selection 会因 test window 改变而改变输出；`predict.py`、benchmark current 生成和 backtest runner 现统一调用 `schemes.daily_5y_2_v28.inference`，核心窗口为 `feature_date` 所在月月初到 `feature_date`，不再使用连续窗口。CompareGate 基准采用外部 May 2026 patched auxiliary source，original/current 各 18 条锚点 `2026-05-06` 至 `2026-05-29`，这些锚点语义是 source T / 平台 `feature_date`，不是实盘 `predict_date`；missing/extra=0、direction_match_rate=`1.0`、max_confidence_abs_diff=`0.0`。初始 BacktestGate 曾落库 run_id=`92`；2026-06-13 按最终预测语义重建 latest 基线 run_id=`107`，`t_backtest_predictions` 333 条；2026-06-14 新口径 no-persist 已完成，候选输出仍为 333 条、17 个月度格，是否落库为新的 canonical latest 等待人工确认。ActivationGate 已用 `activate` token 将 `config.yaml.status` 从 `paused` 翻为 `active`，当前 active scheme_version=`c3d222781648`，并已重启 scheduler。

旧周度方案（`weekly_10y_d_overlay` / `weekly_5y_direct_production` / `weekly_7y_cross_d_overlay`）已于 2026-06-09 退役。

2026-06-09 已清理旧周频写库记录：`t_scheme_weekly_actuals` 整表清空，`t_scheme_registry`、`t_scheme_predictions`、`t_scheme_run_log`、`t_backtest_runs`、`t_backtest_predictions` 中旧周频 `scheme_id` 记录已删除；源表未修改。同日已修复 `scheduler/weekly_actuals_updater.py`，新生成的周频 actuals 只读 `api_wind_date.week_id` 与 `t_trade_calendar.trade_flag`，不再使用计算型周历公式。

## 架构与 Harness

强约束 harness 工程已从"文档设计"推进到"代码落地并可运行"。架构演进路线见 [CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md) §10，执行阶段 S0-S8 已完成：

- `shared.calendar_service` 提供交易日历/周历查询单点入口，交易日判断只读 `t_trade_calendar.trade_flag`，`week_id_for_date` 只读 `api_wind_date.week_id`。
- `shared.input_artifacts` 是所有预测 adapter 的输入文件生成入口；日频、周频、月频底层统一由 `shared.data_service` 生成输出宽表，artifact 层负责写出输入 CSV 并读回给算法。
- `harness/` 包已实现 StaticGate、InputGate、UnitGate、DryRunGate、CompareGate、BacktestGate、ApiGate、LiveGate、ActivationGate，以及 contracts、table_guard、authorization、orchestrator、persistence、CLI。
- `python -m harness onboard {scheme_id} --stage all` 可串联 static -> input -> unit -> dry-run -> compare -> backtest -> api；新增方案必须准备 benchmark 并让 CompareGate `passed`，不能把 `skipped` 当作完成证据。live/activate 不在 `all` 内，必须显式授权。

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
- 旧周度方案在文档/产物中退役；当前 active 周度方案为 `weekly_5y_direct_0529` / `weekly_7y_cross_d_overlay_0529` / `weekly_10y_d_overlay_0529`。

### P1 — 灰度实验室数据模型（已完成，独立验证通过）

S1→S7 串行落地，新增迁移 `005_lifecycle.sql` / `006_predictions_runid_uk.sql` / `007_backtest_immutable.sql`；2026-06-12 追加 `009_backtest_latest_view.sql` 和 `010_prediction_semantics.sql`，分别统一历史回测 latest 语义与实盘预测日期/阶段字段。

- **S1 生命周期表**：新增 `t_scheme_versions`、`t_harness_runs`、`t_harness_gate_results`、`t_input_artifacts`、`t_scheme_runs`；历史迁移中存在的 serving pointer 不再作为读取依赖。
- **S2 输入产物指纹**：`InputArtifact` 增 `content_hash`/`schema_hash`/`artifact_id`/`source_watermark`，经 `scheduler.repository.upsert_input_artifact` 幂等落 `t_input_artifacts`。
- **S3 实盘预测唯一口径**：每次运行生成 `run_id` 留痕，但业务唯一键按 `(scheme_id, target_tenor, horizon, target_date)` + UPSERT 保持同一目标点唯一；`predict_date` 只记录调度发出日，`feature_date` 记录输入截止日，`prediction_phase` 区分 `gray_live/scheduled_live`。
- **S4 方案版本**：`shared.versioning` 计算 `code_hash`/`config_hash`/`manifest_hash`，落 `t_scheme_versions`。
- **S5 harness 留痕**：`harness.persistence` 把每次 run 与每个 gate 结果落 `t_harness_runs`/`t_harness_gate_results`（仅写这两表，DB 不可用时降级本地 JSON）。
- **S6 回测不可变**：每次回测 append 新 `backtest_run_id`，`v_latest_backtest_run` 只返回同一 `benchmark_id + scheme_id + data_source` 下最新的 `status='success'` run（按 `updated_at DESC, id DESC`），`start_date/end_date` 仅作为 run 属性，不参与 latest 分组。
- **S7 backend 读切换**：GET 预测直接读 `t_scheme_predictions` 并按 `target_date` 去重/分组；响应透出 `run_id`/`scheme_version`/`input_artifact_hash`/`feature_date`/`prediction_phase`/`phase_ranges` 可追溯字段，保持只读。

**三条不变量经独立核验全部守住**：core 纯净（`schemes/*/core` 未被污染）、写库单点（新增写库仅在 `scheduler.repository` / `backtests.repository` / `harness.persistence`）、GET 只读。**全套单测 249/249 通过**（服务环境 `bond_factor_lab_service`），并经真实 MySQL 验证迁移幂等、实盘语义回填、同一业务键 UPSERT、回测 append/latest view、留痕与 trace 字段。

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
  - `t_backtest_reproduction_checks`
- 服务环境已分离：
  - 算法预测：conda `forecast_env`
  - 后端/API/调度器：conda `bond_factor_lab_service`
- `t1_daily` 已完成 adapter，当前 active，预测目标为 `5Y/10Y`。
- `t5_daily` 已完成 adapter，当前 active，预测目标为 `3Y/5Y/7Y/10Y`。
- `weekly_5y_direct_0529` 已完成 adapter + harness 全流程入库，当前 active，预测目标 `5Y`，horizon=6（周度）。
- `weekly_7y_cross_d_overlay_0529` 已完成 adapter + harness 全流程入库，当前 active，预测目标 `7Y`，horizon=6（周度）；dry-run `predict_date=2026-06-06` 产出 `feature_date=2026-06-05`、`target_date=2026-06-12`，证明周六预测指向下一周最后交易日。
- `weekly_10y_d_overlay_0529` 已完成 adapter + harness 全流程入库，当前 active，预测目标 `10Y`，horizon=6（周度）；dry-run `predict_date=2026-06-06` 产出 `feature_date=2026-06-05`、`target_date=2026-06-12`，证明周六预测指向下一周最后交易日。
- 强约束 harness 已落地：[HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) 是方案入库总纲，[CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md) 是代码架构主蓝图，[SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) 是现行设计规范。
- artifact 命名已统一：`backtests/` 只放回测代码，`benchmarks/{benchmark_id}/` 只放 canonical 基准输入，运行期输入在 `backtest_artifacts/runtime_inputs/{scheme_id}/`，历史回测和数据差异报告在 `backtest_artifacts/backtests/{benchmark_id}/`。
- `t_target_registry` 当前展示 `3Y/5Y/7Y/10Y` 四个国债活跃目标；`1Y` 只保留为部分算法特征/审计输入，不作为当前前端目标。
- launchd 已安装并启动：
  - `com.bond-factor-lab.backend`
  - `com.bond-factor-lab.scheduler`
- 2026-06-09 周频方案删除后已重启 scheduler；`launchctl print` 显示 `com.bond-factor-lab.scheduler` 为 `running`。2026-06-11 起日频 actuals/真实方向刷新在每日 `08:30` 和 `19:00` 各触发一次；非交易日由交易日检查跳过，旧的“每日16:00”文案已废弃。
- 前端已从真实 API 读取目标注册表和回测数据；T+1/T+5/周度筛选入口保留。日频 T+N 与周度明细、月度指标均按 `target_date` 作为目标交易日归属；真实方向仍分别匹配 `t_scheme_actuals.trade_date = target_date` 与 `t_scheme_weekly_actuals.target_date = target_date`。`feature_date` 是前端和业务统一的数据截止字段，`predict_date` 是信号发出/调度运行日；同一 `target_date` 下同方案同标的同 horizon 由 UK + UPSERT 保持唯一。
- 周频 actuals 代码口径已修复并重刷入库；当前 `t_scheme_weekly_actuals` 2,927 条，按 `feature_date/target_date -> api_wind_date.week_id` 复核无错配。

## 当前数据库快照

核验时间：`2026-06-13`，数据库 `bond_db`，MySQL `8.0.45`。

| 项 | 当前值 |
|----|--------|
| `api_wind_date` | 6,017 行 |
| `api_wind_daily` | 2,237,901 行，日期 `2010-01-01` 到 `2026-06-11` |
| `api_wind_derivative_daily` | 938,450 行，日期 `2010-01-01` 到 `2026-06-10` |
| `api_wind_weekly` | 121,630 行，周 `200901` 到 `202622` |
| `api_wind_derivative_weekly` | 277,064 行，周 `200901` 到 `202621` |
| `api_wind_indicators_all` | 1,637 |
| `t_trade_calendar` | 6,209 |
| `t_pre_market_forecast` | 930 |
| `t_shap` | 28,147 |
| `t_scheme_predictions` | 94 |
| `t_scheme_actuals` | 13,927 |
| `t_scheme_weekly_actuals` | 2,927 |
| `t_scheme_registry` | 10（011 迁移后按 target_tenor 拆分；当前快照执行迁移前为 6） |
| `t_scheme_run_log` | 69 |
| `t_target_registry` | 4 |
| `t_backtest_runs` | 48 |
| `t_backtest_predictions` | 38,512 |
| `t_backtest_reproduction_checks` | 8 |
| `bfl_probe_*` 影子表 | 0 |

actuals 覆盖：

| 期限 | 记录数 | 日期范围 |
|------|--------|----------|
| `1Y` | 2,521 | `2016-01-18` 到 `2026-06-08` |
| `3Y` | 2,522 | `2016-01-18` 到 `2026-06-09` |
| `5Y` | 2,521 | `2016-01-18` 到 `2026-06-09` |
| `7Y` | 2,522 | `2016-01-18` 到 `2026-06-09` |
| `10Y` | 3,833 | `2010-07-27` 到 `2026-06-09` |

weekly actuals 覆盖：

| 期限 | 记录数 | target_date 范围 |
|------|--------|------------------|
| `1Y` | 529 | `2016-01-29` 到 `2026-06-05` |
| `3Y` | 529 | `2016-01-29` 到 `2026-06-05` |
| `5Y` | 529 | `2016-01-29` 到 `2026-06-05` |
| `7Y` | 529 | `2016-01-29` 到 `2026-06-05` |
| `10Y` | 811 | `2010-08-06` 到 `2026-06-05` |

## 历史回测状态

历史复现结果写入独立 backtest 表，不混入 `t_scheme_predictions`。当前代码侧保留的历史回测方案如下：

| 方案 | 数据源 | 状态 | 日期范围 |
|------|--------|------|----------|
| `t1_daily` | `baseline_original_csv` | `success`，run_id=`114` | `2025-01-01` 到 `2026-05-29` |
| `t1_daily` | `framework_original_csv` | `success`，run_id=`115` | `2025-01-01` 到 `2026-05-29` |
| `t1_daily` | `framework_db_aligned` | `success`，run_id=`116` | `2025-01-01` 到 `2026-05-29` |
| `t5_daily` | `baseline_original_csv` | `success`，run_id=`111` | `2025-01-01` 到 `2026-05-31` |
| `t5_daily` | `framework_original_csv` | `success`，run_id=`112` | `2025-01-01` 到 `2026-05-31` |
| `t5_daily` | `framework_db_aligned` | `success`，run_id=`113` | `2025-01-01` 到 `2026-05-31` |
| `weekly_5y_direct_0529` | `framework_db_aligned` | `success`，run_id=`109` | `2025-01-03` 到 `2026-05-22` |
| `weekly_7y_cross_d_overlay_0529` | `framework_db_aligned` | `success`，run_id=`110` | `2025-01-03` 到 `2026-05-22` |
| `weekly_10y_d_overlay_0529` | `framework_db_aligned` | `success`，run_id=`108` | `2025-01-03` 到 `2026-05-22` |
| `daily_5y_2_v28` | `framework_db_aligned` | `success`，run_id=`107` | `2025-01-02` 到 `2026-05-22` |

已确认错误口径并受控删除的周频回测 run 包括 `weekly_5y_direct_0529` run_id=`104`、`weekly_7y_cross_d_overlay_0529` run_id=`105`、`weekly_10y_d_overlay_0529` run_id=`106`；旧的正确审计 run 继续保留，`v_latest_backtest_run` 只选当前 latest success。当前新接入的周频方案 `weekly_5y_direct_0529` 已按 source-original batch reproduction 完成历史回测落库，最新 `framework_db_aligned` run_id=`109`，`t_backtest_predictions` 71 条；整体样本 71、正确 41、accuracy=57.7%，`evaluation_filter.date_field=target_date`。该方案历史回测是批准的 source-original batch reproduction 例外：旧严格 PIT run_id=`104` 已删除；新 run summary 标记 `backtest_mode=original_batch_reproduction`、`backtest_point_in_time=false`、`historical_backtest_exception=true`，且 `original_benchmark_validation` 为 71/71 matched。实盘/灰度 adapter 仍严格使用 `feature_date`、`end_week=feature_week_id`、`as_of_date=feature_date`。

`weekly_7y_cross_d_overlay_0529` 已按 source-original batch reproduction 完成历史回测落库，最新 `framework_db_aligned` run_id=`110`，`t_backtest_predictions` 68 条；整体样本 68、正确 51、accuracy=75.0%，`evaluation_filter.date_field=target_date`。`/api/backtests/factor-lab` 返回该业务方案 composite `scheme_id=weekly_7y_cross_d_overlay_0529__h6__7Y`、`base_scheme_id=weekly_7y_cross_d_overlay_0529`、`frequency=weekly`、`horizon=6`、`target_tenor=7Y`。该周频方案已补齐源文件原始回测 benchmark：`original_predictions_sample.csv` / `current_predictions_sample.csv` 各 42 条，CompareGate 最新证据为 direction_match_rate=1.0、max_confidence_abs_diff=0、missing/extra=0，confidence 来源为 `cross_d_prob_up`。该方案历史回测是批准的 source-original batch reproduction 例外：旧严格 PIT run_id=`105` 已删除；新 run summary 标记 `backtest_mode=original_batch_reproduction`、`backtest_point_in_time=false`、`historical_backtest_exception=true`，且 `original_benchmark_validation` 为 42/42 matched。实盘/灰度 adapter 仍严格使用 `feature_date`、`end_week=feature_week_id`、`as_of_date=feature_date`。

`weekly_10y_d_overlay_0529` 已按 DB 周历完成历史回测落库，最新 `framework_db_aligned` run_id=`108`，`t_backtest_predictions` 72 条；整体样本 72、正确 47、accuracy=65.3%，`evaluation_filter.date_field=target_date`。`/api/backtests/factor-lab` 返回该业务方案 composite `scheme_id=weekly_10y_d_overlay_0529__h6__10Y`、`base_scheme_id=weekly_10y_d_overlay_0529`、`frequency=weekly`、`horizon=6`、`target_tenor=10Y`，历史回测月份为 2025-01 到 2026-05，2026-06 不再出现在 backtest monthly rows。该方案历史回测是批准的 source-original batch reproduction 例外：旧严格 PIT run_id=`106` 已删除；原因是 10Y D-overlay 的 Model2 固定未来分段与逐周 PIT 切片冲突，会导致 2025H1 无有效当前周信号。新 run summary 标记 `backtest_mode=original_batch_reproduction`、`backtest_point_in_time=false`，且 `original_benchmark_validation` 为 45/45 matched。实盘/灰度 adapter 仍严格使用 `feature_date`、`end_week=feature_week_id`、`as_of_date=feature_date`。

`daily_5y_2_v28` 当前 canonical latest 仍为 `framework_db_aligned` run_id=`107`，`t_backtest_predictions` 333 条；整体样本 333、正确 163、accuracy=48.9%，`evaluation_filter.date_field=target_date`。历史回测已截断到 `target_date < 2026-06-01`，predict_date 范围为 `2025-01-02` 到 `2026-05-22`，target_date 范围为 `2025-01-09` 到 `2026-05-29`；`/api/backtests/factor-lab?benchmark_id=v28_daily_5y_2&data_source=framework_db_aligned` 返回 active registry 业务方案 `scheme_id=daily_5y_2_v28__h5__5Y`、`base_scheme_id=daily_5y_2_v28`、run_id=`107`，DB 明细动态聚合↔API 月度格 17/17 一致。其中 2026-05 目标月 13 个样本、正确 10 个、accuracy=76.9%。旧 run_id=`92/93/103` 仍保留为审计历史，但不再被 `v_latest_backtest_run` 或前端 latest 查询选中。2026-06-14 用共享 inference helper 运行的 no-persist 候选回测为 333 条、17 个月度格，metric_samples=261、correct=164、accuracy=62.8%；该候选尚未落库，避免未经人工确认直接改变前端 canonical latest。

2026-06-14 已移除 `t1_daily` / `t5_daily` 的 2026-05 最后一周 target 临时排除，并重跑落库 latest 回测。最新 run_id：`t5_daily` baseline/framework-csv/framework-db 分别为 `111/112/113`，`t1_daily` baseline/framework-csv/framework-db 分别为 `114/115/116`，`weekly_5y_direct_0529` framework-db 为 `109`，`weekly_7y_cross_d_overlay_0529` framework-db 为 `110`，`weekly_10y_d_overlay_0529` framework-db 为 `108`，`daily_5y_2_v28` framework-db 为 `107`。SQL 复核所有 latest rows 均满足 `predict_date = feature_date`、`predict_date >= 2025-01-01`、`target_date < 2026-06-01`，且同一 `benchmark_id + scheme_id + data_source` 不存在多行 latest。`/api/backtests/factor-lab?data_source=framework_db_aligned` 中 `t1_daily__h1__5Y/10Y` 的 2026-05 目标月已包含 `target_date=2026-05-29`，对应 `feature_date=2026-05-28`；`t5_daily__h5__5Y` 的 2026-05 目标月已有 18 条样本，并包含 `target_date=2026-05-25..2026-05-29` 五条每日明细。

## 实盘预测状态

active 方案的 live 预测事实表当前状态：

| 方案 | 预测日期 | 记录数 | 状态 |
|------|----------|--------|------|
| `t1_daily` | `2026-05-29` 到 `2026-06-12` | 20 条（2/日） | `gray_live` run_id=`22` 到 `29`；`scheduled_live` run_id=`34/53` |
| `t5_daily` | `2026-05-26` 到 `2026-06-12` | 52 条（4/日） | `gray_live` run_id=`2/4/6/8/11/16` 到 `21`；`scheduled_live` run_id=`33/54` |
| `weekly_5y_direct_0529` | `2026-06-06` | 2 条 | `gray_live` run_id=`31/32`；旧错误 `scheduled_live` run_id=`57` 的预测明细已删除 |
| `weekly_7y_cross_d_overlay_0529` | `2026-05-30` 到 `2026-06-06` | 2 条 | `gray_live` run_id=`36/35`；旧错误 `scheduled_live` run_id=`56` 的预测明细已删除 |
| `weekly_10y_d_overlay_0529` | `2026-05-30`、`2026-06-06`（回补） | 1/次 | `gray_live`，run_id=`37/38` |
| `daily_5y_2_v28` | `2026-05-26` 到 `2026-06-12` | 14 条（1/日） | `gray_live` run_id=`39`-`41`、`43`-`51`、`58`；旧 `run_id=42` 预测明细已删除，仅保留 run/log 审计；`scheduled_live` run_id=`52` |

2026-06-13 审计确认 `weekly_5y_direct_0529` 旧 live run_id=`31` 曾出现 predict_date=2026-06-06 → target_date=2026-06-05 的反向时点异常；该记录保留为历史审计，不作为合规语义范例。当前修复分支已去掉 5Y 周频 adapter 的 stale signal fallback，后续重整 live 回补时必须按 Step 10b 以 `target_date` 覆盖为准，并满足 `predict_date=T+1`、`feature_date=T`、`target_date=T+horizon`。

2026-06-10 周度方案 `weekly_5y_direct_0529` post-onboarding SOP 已完成：原始基准按 DB 周历归一化后 503 条，改造后 framework 复现 503 条，S5 方向差异 0；当时按 `target_date` 月度口径落库的 run_id=`81` 保留为审计历史。2026-06-13 周频历史回测统一按 source-original batch reproduction 例外重建后，当前 latest framework_db_aligned run_id=`109`，`t_backtest_predictions` 71 条；旧严格 PIT run_id=`104` 已受控删除。scheduler 已确认注册周六 11:30 任务（`30 11 * * 6`）。

2026-06-11 按 SOP Step 10b 回补 `weekly_7y_cross_d_overlay_0529` 灰度实盘预测：先补 `execute_scheme(cfg, '2026-06-06')`，run_id=`35`，predict_date=2026-06-06 → target_date=2026-06-12，方向=1；随后发现 2026-06 周度表还缺第一条目标周，又补 `execute_scheme(cfg, '2026-05-30')`，run_id=`36`，predict_date=2026-05-30 → target_date=2026-06-05，方向=1。该经验已写入 SOP：周度回补必须按 `target_date >= 2026-06-01` 枚举目标周并反推 predict_date，不能只按 `predict_date >= 2026-06-01` 枚举。API 已返回两条 live row：06/05 已验证（actual=-1，correct=False），06/12 尚无 actual（待验证）；`t_scheme_run_log` id=`49/48` 留痕。迁移 010 回填后，run_id=`35/36` 均标识为 `prediction_phase=gray_live`。

2026-06-11 按 SOP Step 10b 回补 `weekly_10y_d_overlay_0529` 灰度实盘预测：registry 同步前两次 live gate 因 `scheme ... not found in t_scheme_registry` 跳过并留痕（run_log id=`50/51`），随后通过 `sync_scheme_registry` 同步 active 配置，再补 `2026-05-30` 与 `2026-06-06` 两个周六，run_id=`37/38`，分别写入 predict_date=2026-05-30 → target_date=2026-06-05、predict_date=2026-06-06 → target_date=2026-06-12，方向均为 -1。API 已返回两条 live row：06/05 已验证（actual=1，correct=False），06/12 尚无 actual（待验证）；011 后 `/api/backtests/factor-lab` 返回业务方案 `weekly_10y_d_overlay_0529__h6__10Y`，对应 base `weekly_10y_d_overlay_0529`、run_id=`108`。本次还修复了 10Y 周度详情出现两个 2026-06 的问题：历史回测截断为 `target_date < 2026-06-01`，灰度实盘月份只由 live metrics 展示。迁移 010 回填后，run_id=`37/38` 均标识为 `prediction_phase=gray_live`。

2026-06-11 已重刷日频源数据至 `api_wind_daily.rdate=2026-06-11`，实盘预测表中 `t1_daily` 覆盖 target_date=2026-06-01 到 2026-06-11，`t5_daily` 覆盖 target_date=2026-06-01 到 2026-06-17。当前 6 月日频明细按目标日展示；对应目标日有 actuals 时直接计结果，目标日尚无 actuals 时保持 `待验证`，不会显示为“平”。

2026-06-12 按 SOP Step 10b 回补 `daily_5y_2_v28` 灰度实盘预测：按 `target_date >= 2026-06-01` 反推 signal `predict_date`，补齐 `2026-05-26` 到 `2026-06-11` 共 13 条 gray_live row，target_date 覆盖 `2026-06-01` 到 `2026-06-17`，原始 run_id=`39` 到 `51`。2026-06-14 复查发现 `run_id=42` 的 `feature_date=2026-05-28` 明细使用了错误的连续 test window，已受控删除该预测明细并保留 run/log 审计；随后用共享月度窗口 inference 重跑为 `run_id=58`，`predict_date=2026-05-29`、`feature_date=2026-05-28`、`target_date=2026-06-04`、`prediction_phase=gray_live`、`predicted_direction=1`、`confidence=1.0`。`predict_date=2026-06-12`、run_id=`52` 是当前第一条 scheduler 自然触发的正式实盘记录，`prediction_phase=scheduled_live`，feature_date=`2026-06-11`、target_date=`2026-06-18`。011 后 `/api/metrics/daily_5y_2_v28__h5__5Y` 返回 live row；旧 `/api/metrics/daily_5y_2_v28?tenor=5Y` 不再是合法调用。

2026-06-10 已验证前端/DB 一致性：`python -m scripts.verify_frontend_db --scheme-id t5_daily --run-id 76` 检查 68 格、0 mismatch；`t1_daily --run-id 79` 检查 36 格、0 mismatch（DB 中 `1Y` 回测格被前端目标注册表隐藏）；`weekly_5y_direct_0529 --run-id 80` 检查 124 格、0 mismatch。2026-06-13 对 `daily_5y_2_v28` 使用显式 `benchmark_id=v28_daily_5y_2` 验证 `/api/backtests/factor-lab` 与 DB 月度格：最终 latest run_id=`107` 为 17/17 一致、0 mismatch。同日修复默认 `/api/backtests/factor-lab` 只读取 `model_muti_0529` 的问题：未传 `benchmark_id` 时现在返回所有 benchmark 下各方案最新成功回测，因此前端可同时合并 `daily_5y_2_v28` 的 17 条回测月度行与实盘 2026-06 行；前端展示 `实盘发出起点 2026-05-26`，并通过 `phase_ranges` 显示灰度实盘区间与正式调度起点 `2026-06-12`。2026-06-12 scheduler 配置复核：`daily_5y_2_v28` / `t1_daily` / `t5_daily` 注册工作日 07:03，`weekly_5y_direct_0529` / `weekly_7y_cross_d_overlay_0529` / `weekly_10y_d_overlay_0529` 注册周六 11:30，日频 actuals 注册每日 08:30 与 19:00；已重启 `com.bond-factor-lab.scheduler`，日志确认 `Scheduled scheme daily_5y_2_v28 at 3 7 * * 1-5` 与 `Scheduled actuals refresh at 08:30 and 19:00 Asia/Shanghai`。2026-06-13 修复分支已移除前端部署日期 override，候选方案部署/灰度/正式调度展示统一依赖后端 live rows 与 `phase_ranges`。用户侧强制刷新后确认页面正确，后续遇到“静态前端已改但页面仍旧”需先提醒强制刷新/禁用缓存。

旧周频 live prediction/run_log 记录已清理。scheduler 重启后，当前代码配置会注册 `daily_5y_2_v28`、`t1_daily`、`t5_daily` 日频方案与 `weekly_5y_direct_0529`、`weekly_7y_cross_d_overlay_0529`、`weekly_10y_d_overlay_0529` 周频方案。

## API 与安全边界

- 已只读验证：
  - `GET /api/health`
  - `GET /api/targets`
  - `GET /api/predictions?scheme_id=t5_daily__h5__5Y&limit=1`
- `GET /api/backtests/factor-lab` 已改为只读获取 active registry metadata，不再触发 registry sync，也不再从 config fallback 临时拼业务 `scheme_id`。P0 后 `GET /api/schemes` 也已去除 registry 写副作用：registry 同步改为后端启动时执行一次，外加受保护的 `POST /api/admin/registry/sync`（未配置 `BOND_ADMIN_TOKEN` 时放行，配置后需 `X-Admin-Token`）。当前所有 GET 接口均为只读。
- `GET /api/predictions` 已收敛为 active registry composite `scheme_id` 入口；base scheme id、无 `scheme_id` 和 `?tenor=...` 都不是合法业务查询。
- 正式运行需要写库时，只应通过明确的调度器或运维命令写入 `t_scheme_predictions`、`t_scheme_run_log`、`t_scheme_actuals`、`t_scheme_weekly_actuals` 或 `t_backtest_*`，不要改动源数据表。

## 剩余观察项

1. 下一次 scheduler 运行后，继续确认日志注册和执行 `daily_5y_2_v28` / `t1_daily` / `t5_daily` / `weekly_5y_direct_0529` / `weekly_7y_cross_d_overlay_0529` / `weekly_10y_d_overlay_0529`，并确认 actuals jobs 为 `actuals:0830` / `actuals:1900`。
2. 后续新周频方案进入时，继续按 [SCHEME_ONBOARDING_SOP.md](sop/SCHEME_ONBOARDING_SOP.md) 流程，并证明 `week_id` 来自 `api_wind_date` 而非公式计算。
3. 2026-06-10 修复了 `backend/services.py` 中周度 metrics 的 JOIN 条件：去掉 `wa.predict_date = p.predict_date`（周度 predict_date 语义在 prediction 和 actuals 间不一致），仅按 `tenor + target_date` 匹配。
4. 2026-06-12 固化日频 T+N 与周频前端/API/回测口径：月度指标与明细均按 `target_date` 归属，真实方向按 `target_date` join；`feature_date` 是前端和业务统一的数据截止字段，`predict_date` 是信号发出/调度日；灰度实盘和正式实盘需通过 `prediction_phase=gray_live/scheduled_live` 区分；未来目标日可先显示为 `待验证`。
5. 拿到 panda_quantflow 外层仓库路径后完成菜单/路由接入并验证 iframe。
