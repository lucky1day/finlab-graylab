# 当前状态

**更新日期**: 2026-06-08

**结论**: 新模拟生产机器已完成服务环境、正式平台表、目标注册表、历史回测表、actuals 刷新、历史回测复现、launchd 常驻服务验证，以及周度 10Y D-overlay 方案回测接入。2026-06-05 09:25 常驻 scheduler 已成功写入 `t1_daily` / `t5_daily` 当日正式预测记录；2026-06-06 已完成 `weekly_10y_d_overlay` 受控 live 写库验收，并在 15:24 通过单方案 scheduler 手动补跑成功。2026-06-06 15:05 已将周度方案切换为 `active` 并重启 scheduler，日志确认注册 `Scheduled scheme weekly_10y_d_overlay at 30 11 * * 6`；第一次自动 cron 运行待下一次周六 11:30 观察。2026-06-07 已补齐公共输入文件层护栏并完成 Git baseline/cleanup 分支管理: t1/t5/weekly live adapter 和历史复现 upstream 分支均通过 `shared.input_artifacts` 调用对应 data service 先生成输入 CSV，再读回给算法；日频使用 `shared.data_service`，周频使用 `weekly_data_service` 的 `wind_export(1)` 口径。2026-06-08 已按新增方案 SOP 接入 `weekly_5y_direct_production`: 原始脚本归档在 scheme core，运行 adapter 通过公共周频输入层生成 CSV 后读回，标准 dry-run 返回 1 条 `5Y` 预测；no-persist DB 回测成功但未写入 `t_backtest_*`，方案保持 `paused`，尚未进入前端矩阵。旧 `_original_source` 运行依赖已移除，历史 benchmark CSV 已固化为 `benchmarks/model_muti_0529/daily_output.csv` 的真实文件。

**最新前端/回测口径核验**: 2026-06-07 已重新核验 `GET /api/backtests/factor-lab`，`10Y国债活跃 · 周度` 使用当前 DB 版本最新成功回测 run_id=`13`，显示 `68.9% (31/45)`。回测输入由公共周频导出层生成，宽表为 `840 x 575`，周范围 `201001` 到 `202621`；重点月样本数为 `2025-07=4`、`2025-10=5`、`2026-01=6`。

## 已完成

- 本机 `.env` 已生成并保持 Git 忽略；服务环境和算法环境均可读取。
- MySQL `bond_db` 已创建正式平台表:
  - `t_scheme_predictions`
  - `t_scheme_actuals`
  - `t_scheme_weekly_actuals`
  - `t_scheme_registry`
  - `t_scheme_run_log`
  - `t_target_registry`
- 历史回测表已创建:
  - `t_backtest_runs`
  - `t_backtest_predictions`
  - `t_backtest_monthly_metrics`
  - `t_backtest_reproduction_checks`
- 服务环境已分离:
  - 算法预测: conda `forecast_env`
  - 后端/API/调度器: conda `bond_factor_lab_service`
- `t1_daily` 已完成 adapter，当前 active，预测目标为 `5Y/10Y`。
- `t5_daily` 已完成 adapter，当前 active，预测目标为 `3Y/5Y/7Y/10Y`。
- 公共输入文件层已落地: `shared.input_artifacts` 是所有预测 adapter 的输入文件生成入口；日频通过 `shared.data_service` 生成 `daily_output_*.csv`，周频通过 `weekly_data_service` 内的 `wind_export(1)` 口径生成 `weekly_output_*.csv`，运行期文件统一位于 `backtest_artifacts/runtime_inputs/{scheme_id}/`。每条 live `PredictionRecord.extra` 会记录 `input_artifact_path` 和 `input_artifact_source`。
- artifact 命名已统一: `backtests/` 只放回测代码，`benchmarks/{benchmark_id}/` 只放 canonical 基准输入，运行期输入在 `backtest_artifacts/runtime_inputs/{scheme_id}/`，历史回测和数据差异报告在 `backtest_artifacts/backtests/{benchmark_id}/`。DB 中 `benchmark_id` / `data_source` 保留兼容枚举，API 额外提供中文展示名。
- `weekly_10y_d_overlay` 已完成 adapter、DB 周频输入生成、历史回测落库、前端周度格子展示和 2026-06-06 受控 live 写库验收；当前已切换为 `active`，目标为 `10Y`。
- `weekly_5y_direct_production` 已完成 adapter、DB 周频输入生成、原始脚本归档、no-persist 历史回测 runner 和标准 dry-run；当前保持 `paused`，目标为 `5Y`，尚未同步 registry、未写入 `t_backtest_*`、未进入前端矩阵。
- `weekly_10y_d_overlay` 的 live 调度 cron 为周六 `11:30`（`30 11 * * 6`），对齐旧实盘 weekly cron 的首轮预测时间；旧脚本 16:00 / 22:00 为检查和必要补跑节点。2026-06-06 15:05 重启 scheduler 后已确认该 job 注册成功。
- 周度 scheduler job 已显式使用 `force=True` 绕过通用“非交易日跳过”保护；日度方案仍保留交易日判断。
- registry 同步已加安全护栏: 未发生元数据变化的方案不会无意义刷新 `t_scheme_registry.updated_at`；本次 scheduler 重启后 `t1_daily` / `t5_daily` registry 时间保持不变，仅 `weekly_10y_d_overlay` 更新为 `active` / `30 11 * * 6`。
- 周度 live 前置工具已补齐: `scripts/check_weekly_10y_readiness.py` 只读检查关键周频指标覆盖，`scripts/write_weekly_10y_live_prediction.py` 只允许受控写入 `weekly_10y_d_overlay` 自己的 prediction/run_log。
- `t_target_registry` 当前展示 `3Y/5Y/7Y/10Y` 四个国债活跃目标；`1Y` 只保留为部分算法特征/审计输入，不作为当前前端目标。
- `t_scheme_actuals` 已刷新到 `2026-06-03`，覆盖 `1Y/3Y/5Y/7Y/10Y`。
- `t_scheme_weekly_actuals` 已按实盘周频口径接入，当前 10Y 周度 actuals 794 条；内部方向为收益率方向 `1/-1/0`，展示语义为 `1=空`、`-1=多`、`0=平`。
- 历史复现 runner 已完成，t1/t5 原始 baseline、framework-csv、framework-db 三组结果以及 weekly 10Y `framework_db_aligned` 结果已写入独立 backtest 表。
- FastAPI 后端已启动在 `127.0.0.1:8100`，`GET /api/health` 返回 `{"status":"ok"}`。
- launchd 已安装并启动:
  - `com.bond-factor-lab.backend`
  - `com.bond-factor-lab.scheduler`
- 前端已从真实 API 读取目标注册表和回测数据；T+1/T+5/周度筛选入口保留。

## 当前数据库快照

只读核验时间: `2026-06-06`，数据库 `bond_db`，MySQL `8.0.45`。

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
| `t_scheme_predictions` | 7 |
| `t_scheme_actuals` | 13,900 |
| `t_scheme_weekly_actuals` | 794 |
| `t_scheme_registry` | 3 |
| `t_scheme_run_log` | 4 |
| `t_target_registry` | 4 |
| `t_backtest_runs` | 8 |
| `t_backtest_predictions` | 7,022 |
| `t_backtest_monthly_metrics` | 379 |
| `t_backtest_reproduction_checks` | 1 |
| `bfl_probe_*` 影子表 | 0 |

当前可直接查到的 Y 生成所依赖的收益率代码:

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

## 方案与周度入口

当前代码配置可发现四个方案，其中三个 active、一个 paused。数据库 `t_scheme_registry` 仍为 3 条，因为本轮未调用会同步 registry 的接口或写库命令。

| 方案 | 频率 | Horizon | 目标 | 状态 |
|------|------|---------|------|------|
| `t1_daily` | `daily` | 1 | `5Y/10Y` | `active` |
| `t5_daily` | `daily` | 5 | `3Y/5Y/7Y/10Y` | `active` |
| `weekly_10y_d_overlay` | `weekly` | 6 | `10Y` | `active` |
| `weekly_5y_direct_production` | `weekly` | 6 | `5Y` | `paused` |

周度 10Y/5Y 语义均为: 周六发出预测，判断下一周最后一个交易日收益率相对本周最后一个交易日是上行还是下行。live 预测现在按 `feature_date` 在源表中反查实际 `week_id`，避免公式 week_id 与 DB 周编号偏移；例如 `2026-06-06` 的 `feature_date=2026-06-05`，源表实际 `feature_week_id=202621`，`target_week_id=202622`，`target_date=2026-06-12`。旧实盘 weekly cron 为 `30 11 * * 6 ... run_weekly_pipeline.sh multi`，首轮预测在周六 11:30 启动，16:00 和 22:00 为检查/补跑，因此 BFL 周度 live cron 对齐为 `30 11 * * 6`。周度 actuals 已独立接入 `t_scheme_weekly_actuals`，方向内部为收益率口径，价格视角映射为 `1=空`、`-1=多`、`0=平`。当前周频宽表按 `/Users/macstudio0/Desktop/wind_export(1).py` 口径生成: 只取 `status=1`、`pre_forecast_flag=1` 的周频因子，保留周末更新参与同周折叠，并按 `lag_length` 平移；源表缺关键周频值时 readiness/dry-run 失败，不写回源表、不临时改源数据。`weekly_10y_d_overlay` 的 `2026-06-06` 预测结果为 `predicted_direction=-1`、`confidence=0.28`；受控 writer 和单方案 scheduler 手动补跑均成功，prediction 通过 UPSERT 保持 1 条，run_log 累计 2 条 weekly success。`weekly_5y_direct_production` 的 `2026-06-06` 标准 dry-run 返回 `predicted_direction=-1`、`confidence=0.48333333333333334`，输入文件为 `backtest_artifacts/runtime_inputs/weekly_5y_direct_production/weekly_output_2026-06-06.csv`；dry-run 前后 `t_scheme_predictions/t_scheme_run_log/t_scheme_actuals/t_scheme_weekly_actuals` 行数不变。

## 历史回测状态

历史复现结果写入独立 backtest 表，不混入 `t_scheme_predictions`。

| 方案 | 数据源 | 状态 | 日期范围 |
|------|--------|------|----------|
| `t1_daily` | `baseline_original_csv` | `success` | `2025-01-02` 到 `2026-05-28` |
| `t1_daily` | `framework_original_csv` | `success` | `2025-01-02` 到 `2026-05-28` |
| `t1_daily` | `framework_db_aligned` | `success` | `2025-01-02` 到 `2026-05-28` |
| `t5_daily` | `baseline_original_csv` | `success` | `2025-01-01` 到 `2026-05-31` |
| `t5_daily` | `framework_original_csv` | `success` | `2025-01-01` 到 `2026-05-31` |
| `t5_daily` | `framework_db_aligned` | `success` | `2025-01-01` 到 `2026-05-31` |
| `weekly_10y_d_overlay` | `framework_db_aligned` | `success` | `2025-07-05` 到 `2026-05-02` |

`weekly_5y_direct_production` 当前仅完成 no-persist DB 回测，尚未写入上述 backtest 表: 样本 501 条，正确 292 条，整体准确率 `58.3%`；实盘窗口样本 42 条，正确 26 条，准确率 `61.9%`；当前 DB 行数在 no-persist 回测前后保持 `t_backtest_runs=8`、`t_backtest_predictions=7022`、`t_backtest_monthly_metrics=379`、`t_backtest_reproduction_checks=1`。

Weekly 10Y 当前最新回测 run_id=`13`: 45 个样本，正确 31 个，整体准确率 `68.9%`；前端会按最新成功 run 显示在 `10Y国债活跃 · 周度` 格子，并在排行中显示 `0529周度10Y-D-overlay基准 · 10Y国债活跃回测`、`68.9%（31/45）`。历史回测转换和月度指标已改为优先使用算法输出的 `month_date/week_date` 作为 legacy 特征周日期，并保留跨年周 `202553`；target 映射按这些特征日期重新排序后取下一条样本，避免跨年处 `target_date` 早于 `feature_date`，并按新 target 重新计算 `future_return/label`。`2025-07` 当前有 4 个样本，`2025-10` 当前有 5 个样本，`2026-01` 当前有 6 个样本。当前月度样本分布为 `2025-07:4/2, 2025-08:5/5, 2025-09:4/3, 2025-10:5/4, 2025-11:4/2, 2025-12:4/2, 2026-01:6/5, 2026-02:4/1, 2026-03:4/3, 2026-04:4/3, 2026-05:1/1`。

同窗口 CSV 对齐复核: `/Users/macstudio0/Desktop/weekly_output.csv` 覆盖 `week_id=201001` 到 `202604`；当前公共层产出的 `weekly_output_2026-06-06.csv` 覆盖 `201001` 到 `202621`。两者的行数、列数、列顺序和周范围结构可对齐，但共同周/共同列仍非逐单元格完全一致，详见 `reports/weekly_input_artifact_wind_export1_vs_desktop_diff.csv`。当前前端展示以最新 DB 公共层回测 run_id=`13` 为准。

周频输入导出状态（2026-06-07）: 已按 `/Users/macstudio0/Desktop/wind_export(1).py` 口径切换周度 live/backtest 的公共输入链路。该版本在网页导出周频宽表时新增 `status=1`、`pre_forecast_flag=1` 因子过滤，并允许周末更新参与同周折叠。公共层从当前 DB 只读生成的 `backtest_artifacts/runtime_inputs/weekly_10y_d_overlay/weekly_output_2026-06-06.csv` 与 `/Users/macstudio0/Desktop/weekly_output.csv` 在行数、列数、列顺序和周范围结构上已对齐（`840 x 575`，`201001` 到 `202621`），关键最新周收益率列 `TB0YWI3C/TB1YWI3C/TB5YWI3C/TB0YWI10/WC000005` 对齐，最新周模型输出也一致（`pred_label=-1`、`prob_up=0.28`）。剩余问题是仍未逐格完全一致: `diff_cell_count=71852`，其中绝大多数为 `1e-7` 量级小数精度差异；实质数值差异（`abs_diff > 1e-4`）为 12 个单元格，另有 101 个单次缺失差异。明细见:

- `reports/weekly_input_artifact_wind_export1_vs_desktop_summary.json`
- `reports/weekly_input_artifact_wind_export1_vs_desktop_diff.csv`

## 正式预测状态

2026-06-05 09:25 launchd scheduler 已成功运行 active 日度方案:

| 方案 | 预测日期 | 记录数 | 状态 |
|------|----------|--------|------|
| `t1_daily` | `2026-06-05` | 2 | `success` |
| `t5_daily` | `2026-06-05` | 4 | `success` |

2026-06-06 已写入 `weekly_10y_d_overlay` 1 条 live 预测记录；14:15 的受控 writer 和 15:24 的单方案 scheduler 手动补跑均为 success，prediction 通过 UPSERT 仍保持 1 条，run_log 累计 2 条 weekly success。周度方案现已 active，下一条自动 live 预测应由下一个周六 11:30 scheduler 触发。

| 方案 | 预测日期 | 目标日期 | 记录数 | 状态 |
|------|----------|----------|--------|------|
| `weekly_10y_d_overlay` | `2026-06-06` | `2026-06-12` | 1 | `success` |

最新数据一致性检查 `canonical_csv_vs_upstream_db_generated` 状态为 `failed`。这不是目标收益率列失败: `TB0YWI0C/TB1YWI0C/TB3YWI0C/TB5YWI0C/TB7YWI0C` 最大误差均为 0；失败原因是历史 CSV 与当前 DB 重新生成 CSV 在少数因子列上存在缺失或精度差异，`overall_max_abs_diff=140.0`，`missing_diff_count=15`。

2026-06-07 复核: `scripts/audit_daily_data_service.py` 使用 `shared.data_service` 生成 DB daily_output，并通过 `shared.input_artifacts` 固化为算法输入 CSV 再读回。此前已确认原始 data_service 生成版与 shared 上游兼容版完全一致，当前运行路径不再依赖 `_original_source`。因此 current DB daily_output 差异归因为“历史 CSV vs 当前 DB 数据版本差异”，不是生成链路差异。最大差异为 `2026-05-28 / S5470301`: 历史 CSV `6140.0`，当前 DB 生成 `6280.0`；另有 15 个 `2026-05-26` 商品/现货相关因子在历史 CSV 为 `0.0`、当前 DB 为空。

## API 与安全边界

- 已只读验证:
  - `GET /api/health`
  - `GET /api/targets`
  - `GET /api/predictions?limit=1`
- `GET /api/backtests/factor-lab` 已改为只读获取 scheme metadata，不再触发 registry sync；`GET /api/schemes` 仍会同步 registry，属于正常服务行为，但不是纯只读接口；做数据库保护核验时不要把 `/api/schemes` 当作只读探针。
- 正式运行需要写库时，只应通过明确的调度器或运维命令写入 `t_scheme_predictions`、`t_scheme_run_log`、`t_scheme_actuals`、`t_scheme_weekly_actuals` 或 `t_backtest_*`，不要改动源数据表。新增周度方案启用前、或周度方案需要受控手动写库前，应先执行 `python -m scripts.check_weekly_10y_readiness --predict-date <target-saturday>`；受控写库只使用 `python -m scripts.write_weekly_10y_live_prediction --predict-date <target-saturday>`。

## 剩余观察项

当前没有阻塞 `weekly_10y_d_overlay` 上线的 TODO；周度 10Y 已 active、已手动补跑、已注册自动调度。周频输入导出逐格对齐属于后续专项，不影响当前先观察现有版本。后续事项:

1. 观察下一次周六 11:30 周度自动调度，确认 `weekly_10y_d_overlay` 只写入本方案 prediction/run_log，且 actuals/source 表不变。
2. 等待 `2026-06-12` target actual 产生后，确认 `2026-06-06 -> 2026-06-12` 这条 live 样本进入 weekly metrics 统计。
3. 观察 16:00 actuals 刷新窗口，确认 `t_scheme_actuals` 继续随源数据更新。
4. 拿到 panda_quantflow 外层仓库路径后完成菜单/路由接入并验证 iframe。
5. 后续专项解决公共周频导出与 `/Users/macstudio0/Desktop/weekly_output.csv` 的剩余 12 个实质数值差异、101 个缺失差异和小数精度差异；这属于桌面 CSV/当前 DB 逐格对齐问题，不再阻塞公共周频输入层使用。
6. 如需新增更多周度或月度方案，继续按 [SCHEME_ONBOARDING_SOP.md](SCHEME_ONBOARDING_SOP.md) 的 paused -> dry-run -> backtest -> frontend -> active 流程。
7. `weekly_5y_direct_production` 下一步如需展示到前端矩阵，应先确认 5Y 周度 actuals 覆盖和回测窗口口径，再受控执行 `backtests.weekly_5y_direct_production_reproduction` 落库，最后通过只读 API 验证前端矩阵。

历史复现详细记录见 [HISTORICAL_REPRODUCTION.md](HISTORICAL_REPRODUCTION.md)。
