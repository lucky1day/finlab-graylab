# 历史回测复现记录

**日期**: 2026-06-07  
**benchmark**: `model_muti_0529`  
**结论**: t1 / t5 canonical-csv baseline、framework-csv、framework-db 三组历史回测，以及 weekly 10Y D-overlay 的 DB 对齐回测，已在当前测试 Mac 复现并写入独立 backtest 表；不写入实盘预测表。

**当前验证口径**: 暂不纳入 `target_date` 落在 `2026-05-25` 至 `2026-05-29` 的 5 月最后目标周样本。算法仍先完整生成原始结果，再只在验证、统计和落库环节过滤这些样本。

## 基准材料

- canonical 输入: `benchmarks/model_muti_0529/daily_output.csv`
- canonical CSV 是真实 Git 文件，不再指向 `_original_source` symlink。
- 旧本机原始解压包和 `_original_source` 已移出运行路径；历史可通过 Git baseline/tag 追溯。

## 数据一致性

本轮验证已按上游同事的真实生产链路执行，当前代码入口也已和该链路对齐:

1. 从当前测试 Mac 的 `bond_db` 读取原始长表。
2. 通过公共输入文件层 `shared.input_artifacts` 生成输入 CSV。
3. 日频公共层内部调用 `shared.data_service`，周频公共层内部调用 `weekly_data_service`。
4. 算法只读取这张生成后的 CSV。

框架入口:

- live adapter: `schemes/t1_daily/predict.py`、`schemes/t5_daily/predict.py`、`schemes/weekly_10y_d_overlay/predict.py`
- 公共输入文件层: `shared/input_artifacts.py`
- 日频 data service: `shared/data_service.py`
- 只读审计脚本: `scripts/audit_daily_data_service.py`

上游 `data_service.py` 的交易日锚定列为 `TB1YWI0C/TB5YWI0C/TB0YWI0C`；当前框架默认锚定列为 `TB1YWI0C/TB3YWI0C/TB5YWI0C/TB7YWI0C/TB0YWI0C`。在本次日期范围内，两种生成口径的对齐版完全一致: 行数、列顺序、缺失值和数值误差均一致，`overall_max_abs_diff = 0`。

| 项 | 结果 |
|----|------|
| 原始 CSV | `3843 x 877` |
| 上游 data_service 生成 DB 完整输出 | `3843 x 880` |
| 上游 data_service 生成并按基准对齐后 | `3843 x 877` |
| 日期范围 | `2010-07-27` 到 `2026-05-28` |
| CSV 独有列 | 0 |
| DB 独有列 | `AUSHF00O`, `IFCFE00C`, `IFCFE00O` |
| Y 生成所依赖的收益率列最大误差 | `TB1YWI0C/TB3YWI0C/TB5YWI0C/TB7YWI0C/TB0YWI0C` 全部为 0 |
| 因子数值列 | 未过默认 `1e-8` 阈值，已记录差异样本和缺失差异 |
| 暂排除目标周后 | 排除 `2026-05-25` 至 `2026-05-29` 的 4 个 daily_output 日期后，最大数值误差从 `140.0` 降至约 `5e-7` |

2026-06-07 重新审计: 原始 data_service 生成版与 `shared.data_service` 的上游兼容版逐列对齐后完全一致，`overall_max_abs_diff=0.0`、`missing_diff_count=0`。当前运行路径已切到 `shared.data_service`，因此此前差异不是生成链路迁移导致。

说明: 数据对齐检查状态为 `failed`，原因不是 Y 生成所依赖的收益率列，也不是当前框架生成口径偏离上游生成口径，而是“当前 DB 重新生成的上游兼容 CSV”和“历史保存的 canonical CSV”之间存在因子列的缺失差异与小数精度差异。首个超阈值样本为 `SWR00001` 在 `2023-03-20`，历史 CSV `42.252313`，当前 DB 重新生成 `42.25231257158`。全量最大差异为 `S5470301` 在 `2026-05-28`: 历史 CSV `6140.0`，当前 DB 经 `shared.data_service` 生成 `6280.0`；15 个缺失差异集中在 `2026-05-26`，历史 CSV 为 `0.0` 而当前 DB 生成为空。

## 独立数据差异报告

已新增离线验证报告，不接入前端、不影响实盘监控页:

- HTML 报告: `backtest_artifacts/backtests/model_muti_0529/data_checks/historical_data_diff_report.html`
- 缺失差异按因子汇总: `backtest_artifacts/backtests/model_muti_0529/data_checks/missing_diff_by_factor.csv`
- 缺失差异按月份汇总: `backtest_artifacts/backtests/model_muti_0529/data_checks/missing_diff_by_month.csv`
- 数值差异按因子汇总: `backtest_artifacts/backtests/model_muti_0529/data_checks/numeric_diff_by_factor.csv`
- 日频 data service 审计摘要: `backtest_artifacts/backtests/model_muti_0529/data_checks/daily_data_service_audit_summary.json`
- 日频 data service DB 生成输出: `backtest_artifacts/backtests/model_muti_0529/data_checks/shared_daily_data_service_generated_daily_output.csv`
- 日频 data service DB 对齐输出: `backtest_artifacts/backtests/model_muti_0529/data_checks/shared_daily_data_service_generated_daily_output_aligned.csv`

## 命名规范

- `scheme_id` 永远表示具体方案，例如 `t1_daily`、`t5_daily`、`weekly_10y_d_overlay`。
- `benchmark_id` 表示历史基准批次，例如 `model_muti_0529`，只用于 `benchmarks/` 和 backtest 表。
- `data_source` 是数据库兼容枚举，当前保留 `baseline_original_csv`、`framework_original_csv`、`framework_db_aligned`；API 会映射为中文展示名。
- 运行期输入文件统一写入 `backtest_artifacts/runtime_inputs/{scheme_id}/`。
- 历史回测和数据差异报告统一写入 `backtest_artifacts/backtests/{benchmark_id}/`，不再使用 `model_muti_0529_daily` 这类伪方案名。

生成命令:

```bash
conda run -n forecast_env python scripts/generate_daily_data_diff_report.py
conda run -n forecast_env python scripts/audit_daily_data_service.py
```

报告包含以下可视化:

- 首屏验收看板，直接展示 Y 生成列、生成链路、DB 多列、四舍五入剔除口径。
- 年度缺失差异柱状图，用于判断差异是否集中在少数时间段。
- 主要差异因子贡献图（累计占比），用于识别主要贡献因子。
- 主要差异因子月度热力图，用于观察长期缺失区间。
- 主要差异因子缺失时间轴，用于查看每个因子的首末差异日期。
- 实质数值差异直方图，已剔除按历史 CSV 单元格小数位常规四舍五入后可对齐的差异。

当前报告结论: 缺失差异覆盖 `31` 个因子列，方向全部为“历史 CSV 有值、当前 DB 重新生成缺失”；前 `16` 个商品/现货价格类因子解释 `99.97%` 的缺失差异。前端验证报告不把纯四舍五入差异作为统计项，只展示剔除后的 `11` 个实质数值差异单元格；全量最大误差 `140.0` 来自 `2026-05-28` 的个别商品价格因子。

## t5 复现结果

| 期限 | 全量准确率 | 样本 | 当前口径一致 | framework-csv mismatch | framework-db mismatch |
|------|------------|------|--------------|------------------------|-----------------------|
| 3Y | 69.5% | 228/328 | 是 | 0 | 0 |
| 5Y | 64.6% | 212/328 | 是 | 0 | 0 |
| 7Y | 66.5% | 218/328 | 是 | 0 | 0 |
| 10Y | 63.4% | 208/328 | 是 | 0 | 0 |

原始行数为 `1328`，暂排除目标周后有效行数为 `1312`。sim / real / May 样本数按当前验证口径复现为 `117 / 203 / 8`。

## t1 复现结果

t1 无上游报告，因此以原始 `run_backtest(..., dry_run=True)` 生成的 CSV 为行级 baseline。

| 期限 | baseline 准确率 | 样本 | framework-csv mismatch | framework-db mismatch |
|------|-----------------|------|------------------------|-----------------------|
| 1Y | 39.9% | 133/333 | 0 | 0 |
| 5Y | 56.2% | 187/333 | 0 | 0 |
| 10Y | 60.7% | 202/333 | 0 | 0 |

原始行数为 `1011`，暂排除目标周后有效行数为 `999`。

注: `1Y` 保留在历史复现审计结果中，但当前前端结果矩阵和实盘预测目标已移除 `1Y`。

## weekly 10Y 复现结果

周度 10Y 来源于 `/Users/macstudio0/Downloads/weekly_10y_d_overlay_0529.py`，已并入:

- 方案目录: `schemes/weekly_10y_d_overlay/`
- DB 周频输入生成: `schemes/weekly_10y_d_overlay/core/weekly_data_service.py`
- 回测入口: `backtests/weekly_10y_d_overlay_reproduction.py`

预测语义: 本周六预测下一周最后一个交易日 10Y 收益率相对本周最后一个交易日是上行还是下行。live 预测按 `feature_date` 从源表反查实际 `week_id`；历史回测为复现上游 0529 周频算法，日期转换优先使用算法输出的 `month_date/week_date` 作为 legacy 特征周日期，再把次日作为 `predict_date`，并按特征日期排序后的下一条算法输出作为 `target_date`。跨年处保留上游算法真实输出的 `week_id=202553`，因此 2026-01 当前有 6 个历史样本；`future_return/label` 按重排后的 target 重新计算，避免 `target_date` 早于 `feature_date`。

| 方案 | 数据源 | run_id | 日期范围 | 样本 | 准确率 | 上涨 precision | 下跌 precision |
|------|--------|--------|----------|------|--------|----------------|----------------|
| `weekly_10y_d_overlay` | `framework_db_aligned` | 13 | `2025-07-05` 到 `2026-05-02` | 45 | `68.9% (31/45)` | `75.0%` | `66.7%` |

2026-06-06 修正记录: 最新前端展示 run 已刷新为 run_id=`13`。`2025-07` 月度样本数为 4，对应预测日 `2025-07-05/12/19/26`；`2025-10` 月度样本数为 5，对应特征周日期 `2025-10-03/10/17/24/31`；`2026-01` 月度样本数为 6，保留上游算法输出的跨年周 `week_id=202553, month_date=2026-01-04`。跨年 target 顺序已按特征日期重排为 `202552 -> 202601 -> 202553 -> 202602`，其中 `202553` 的 target 为 `2026-01-09`，该样本 label 从上行修正为下行。此前 run_id=`15` 因历史回测转换误用 live week_id 公式，把 `week_id=202527` 的算法 `month_date=2025-07-04` 偏移到 `2025-07-11`；同时通用月度统计按 `predict_date` 归月，导致 `2025-10-31` 特征周被错归到 2025-11。

2026-06-08 复核: 周度公共输入层已切换为 `/Users/macstudio0/Desktop/wind_export(1).py` 口径，生成 `840 x 575` 的 `weekly_output_2026-06-06.csv`。该输入与 `/Users/macstudio0/Desktop/weekly_output.csv` 在行数、列数、列顺序和周范围结构上对齐，关键最新周收益率列和最新模型输出一致；但共同周/共同因子输入仍有少量实质数值差异、缺失差异和大量小数精度差异，完整明细已保存在:

- `reports/weekly_output_csv_validation_summary.json`
- `reports/weekly_output_csv_vs_current_db_material_diff.csv`
- `reports/weekly_output_csv_vs_current_db_until_202604_prediction_diff.csv`
- `reports/weekly_output_csv_prediction_diff_weeks_input_changes.csv`
- `reports/weekly_input_artifact_wind_export1_vs_desktop_summary.json`
- `reports/weekly_input_artifact_wind_export1_vs_desktop_diff.csv`

当前前端展示以最新 DB 公共层回测 run_id=`13` 为准，整体样本数 45、正确数 31、准确率 `68.9%`。代码复核确认 `backtests.weekly_10y_d_overlay_reproduction` 不再直接调用底层周频 data service，而是通过 `shared.input_artifacts.build_weekly_input_artifact(scheme_id="weekly_10y_d_overlay", predict_date="historical_backtest")` 生成并读回输入 CSV；`--no-persist` 真实库验证仍返回 45 条、`68.9% (31/45)`，summary 中记录输入路径 `backtest_artifacts/runtime_inputs/weekly_10y_d_overlay/weekly_output_historical_backtest.csv`。

### 周度 5Y direct-production 落库复核

2026-06-08 已按 SOP 接入 `weekly_5y_direct_production`，先只读运行 `python -m backtests.weekly_5y_direct_production_reproduction --no-persist`，再在明确授权后受控执行落库。该 runner 使用当前公共周频输入层从 `bond_db` 生成 `weekly_output`，再按原始 0529 5Y 三规则等权投票生成历史预测。代码复核确认 runner 通过 `shared.input_artifacts.build_weekly_input_artifact(scheme_id="weekly_5y_direct_production", predict_date="historical_backtest")` 生成并读回输入 CSV；`--no-persist` 真实库验证仍返回 501 条、`58.3% (292/501)`，summary 中记录输入路径 `backtest_artifacts/runtime_inputs/weekly_5y_direct_production/weekly_output_historical_backtest.csv`。落库只写 `weekly_5y_direct_production` 对应的 `t_backtest_*` 回测记录，不写实盘预测表、不写 actuals、不改源数据表。

| 方案 | 数据源 | run_id | 日期范围 | 样本 | 准确率 |
|------|--------|--------|----------|------|--------|
| `weekly_5y_direct_production` | `framework_db_aligned` | 28 | `2016-02-20` 到 `2026-05-23` | 501 | `58.3% (292/501)` |

实盘窗口复核: `2025-07-05` 到 `2026-04-25` 共 42 个样本，正确 26 个，准确率 `61.9%`。落库前后受保护表保持不变: `t_scheme_predictions=7`、`t_scheme_run_log=4`、`t_scheme_actuals=13900`、`t_scheme_weekly_actuals=794`。回测目标表变化符合预期: `t_backtest_runs` 8 -> 9，`t_backtest_predictions` 7022 -> 7523，`t_backtest_monthly_metrics` 379 -> 503，`t_backtest_reproduction_checks=1` 不变。backend factor-lab 数据函数、HTTP API 和浏览器 UI 已可返回 `weekly_5y_direct_production:5Y:framework_db_aligned`；浏览器默认区间显示 `58.6% (41/70)`，全量 run 摘要为 `58.3% (292/501)`。

### 周度 7Y cross-D-overlay 落库复核

2026-06-08 已按 SOP 接入 `weekly_7y_cross_d_overlay`。原始脚本 `/Users/macstudio0/Downloads/weekly_7y_cross_d_overlay_0529.py` 已归档到 scheme core，运行路径不直接 import legacy 文件，而是在 `core/predictors.py` 中按 DataFrame 方式复现 7Y 主规则、低利率反弹 overlay、5Y 辅助 down overlay 和 cross-D final signal。live adapter 与 backtest runner 均通过 `shared.input_artifacts.build_weekly_input_artifact()` 生成周频输入 CSV 后读回。标准 dry-run `2026-06-06` 返回 `7Y` 一条预测: `feature_week_id=202621`、`target_week_id=202622`、`target_date=2026-06-12`、`predicted_direction=1`、`confidence=0.55`。

| 方案 | 数据源 | run_id | 日期范围 | 样本 | 准确率 |
|------|--------|--------|----------|------|--------|
| `weekly_7y_cross_d_overlay` | `framework_db_aligned` | 30 | `2025-07-05` 到 `2026-05-23` | 42 | `66.7% (28/42)` |

实盘窗口复核: `2025-07-05` 到 `2026-04-25` 共 39 个样本，正确 26 个，准确率 `66.7%`。落库前后受保护表保持不变: `t_scheme_predictions=13`、`t_scheme_run_log=6`、`t_scheme_actuals=13900`、`t_scheme_weekly_actuals=794`。回测目标表变化符合预期: `t_backtest_runs` 9 -> 10，`t_backtest_predictions` 7523 -> 7565，`t_backtest_monthly_metrics` 503 -> 514，`t_backtest_reproduction_checks=1` 不变。backend factor-lab 数据函数已可返回 `weekly_7y_cross_d_overlay:7Y:framework_db_aligned`；HTTP/browser 验收待本地 8100 服务恢复后复核。

当前周度 10Y 月度样本分布:

| 月份 | 样本 | 正确 | 准确率 |
|------|------|------|--------|
| `2025-07` | 4 | 2 | `50.0%` |
| `2025-08` | 5 | 5 | `100.0%` |
| `2025-09` | 4 | 3 | `75.0%` |
| `2025-10` | 5 | 4 | `80.0%` |
| `2025-11` | 4 | 2 | `50.0%` |
| `2025-12` | 4 | 2 | `50.0%` |
| `2026-01` | 6 | 5 | `83.3%` |
| `2026-02` | 4 | 1 | `25.0%` |
| `2026-03` | 4 | 3 | `75.0%` |
| `2026-04` | 4 | 3 | `75.0%` |
| `2026-05` | 1 | 1 | `100.0%` |

当前周频源表覆盖:

| 表 | 行数 | 周范围 |
|----|------|--------|
| `api_wind_weekly` | 121,553 | `200901` 到 `202621` |
| `api_wind_derivative_weekly` | 277,064 | `200901` 到 `202621` |

标准 dry-run 边界:

- live dry-run 已改为按 `feature_date` 从源表反查实际 `week_id`；`2026-06-06` 可返回一条 `PredictionRecord`: `feature_week_id=202621`，`target_week_id=202622`，`target_date=2026-06-12`。
- 周度 live/backtest 统一使用 `wind_export(1)` 口径的周频公共导出层；若关键周频值未覆盖到目标 feature week，readiness/dry-run 应失败并等待上游补齐，不写回源表。
- live 周度 actuals 已独立接入 `t_scheme_weekly_actuals`；当前 `config.yaml` 为 `status: active`，已完成 2026-06-06 readiness、dry-run、受控 live 写库验收和单方案 scheduler 手动补跑。前端同时展示历史回测排行，live metrics 等 `2026-06-12` target actual 产生后纳入统计。

## 数据库落库

历史复现结果写入独立 backtest 表:

| 表 | 当前记录数 |
|----|------------|
| `t_backtest_runs` | 10 |
| `t_backtest_predictions` | 7565 |
| `t_backtest_monthly_metrics` | 514 |
| `t_backtest_reproduction_checks` | 1 |

注: 新机器当前只保留最新一次数据一致性检查记录。该检查状态为 `failed`，原因是少数因子列的缺失/精度差异；Y 生成所依赖的收益率列最大误差均为 0。

## API

新增接口已验证:

- `GET /api/backtests/runs`
- `GET /api/backtests/runs/{run_id}`
- `GET /api/backtests/runs/{run_id}/metrics`
- `GET /api/backtests/runs/{run_id}/diffs`
- `GET /api/backtests/data-checks`

## 前端口径

前端不新增历史验证结果页。历史复现用于后端验证、脚本验收和文档记录；前端继续按原有方案结果矩阵展示方案，后续新增方案后由方案自身结果进入现有展示链路。2026-06-08 已复核前端周度明细归月: 周度 detail rows 使用 `feature_date` 决定所属月份，显示日仍使用周六 `predict_date`；因此 `feature_date=2025-10-31 / predict_date=2025-11-01` 的样本在明细层也归入 2025-10，与后端月度 metrics 保持一致。此前 2026-06-07 已通过 `GET /api/backtests/factor-lab` 核验，`10Y国债活跃 · 周度` 格子展示当前 DB 版本最新 run_id=`13` 的 `68.9% / 1 个方案`，排行显示 `68.9%（31/45）`。2026-06-08 7Y 接入后，backend factor-lab service 函数已返回 `weekly_7y_cross_d_overlay:7Y:framework_db_aligned`，全量 run 摘要为 `66.7% (28/42)`。

## 验证命令

```bash
conda run -n forecast_env python -m backtests.daily_0529_reproduction --n-jobs 4
conda run -n forecast_env python scripts/verify_backtest_reproduction.py
```

最终验证脚本输出:

```text
ok check_canonical_csv
ok check_data_alignment
ok check_t5_reproduction
ok check_t1_reproduction
ok check_api_endpoints
ok check_frontend_entry
```
