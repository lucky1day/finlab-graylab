# 历史回测复现记录

**日期**: 2026-06-07  
**benchmark**: `model_muti_0529`  
**结论**: t1 / t5 原始脚本 baseline、framework-csv、framework-db 三组历史回测，以及 weekly 10Y D-overlay 的 DB 对齐回测，已在当前测试 Mac 复现并写入独立 backtest 表；不写入实盘预测表。

**当前验证口径**: 暂不纳入 `target_date` 落在 `2026-05-25` 至 `2026-05-29` 的 5 月最后目标周样本。算法仍先完整生成原始结果，再只在验证、统计和落库环节过滤这些样本。

## 基准材料

- canonical 输入: `benchmarks/model_muti_0529/daily_output.csv`
- canonical CSV 实际指向: `schemes/_original_source/t5/data/daily_output.csv`
- 原始 `model-mutitest-0529/t1/daily_output.csv` 与 `model-mutitest-0529/t5/data/daily_output.csv` 已字节级一致。
- 当前测试 Mac 上解压的 `model-mutitest-0529/` 含本地 DB 密码配置，已被 `.gitignore` 忽略，不进入业务运行路径。

## 数据一致性

本轮验证已按上游同事的真实生产链路执行，当前代码入口也已和该链路对齐:

1. 从当前测试 Mac 的 `bond_db` 读取原始长表。
2. 通过公共输入文件层 `shared.input_artifacts` 生成输入 CSV。
3. 日频公共层内部动态加载原始 `schemes/_original_source/data_service.py`，原文件只读不改。
4. 算法只读取这张生成后的 CSV。

框架入口:

- live adapter: `schemes/t1_daily/predict.py`、`schemes/t5_daily/predict.py`、`schemes/weekly_10y_d_overlay/predict.py`
- 公共输入文件层: `shared/input_artifacts.py`
- 原始日频文件桥接: `shared/original_daily_data_service.py`
- 只读审计脚本: `scripts/audit_original_daily_data_service.py`

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

2026-06-07 重新审计: 用原始 `schemes/_original_source/data_service.py` 生成的 DB daily_output，与 `shared.data_service` 的上游兼容版逐列对齐后完全一致，`overall_max_abs_diff=0.0`、`missing_diff_count=0`。因此此前差异不是 `shared.data_service` 计算逻辑导致。

说明: 数据对齐检查状态为 `failed`，原因不是 Y 生成所依赖的收益率列，也不是当前框架生成口径偏离上游生成口径，而是“当前 DB 重新生成的上游 CSV”和“上游历史保存的原始 CSV”之间存在因子列的缺失差异与小数精度差异。首个超阈值样本为 `SWR00001` 在 `2023-03-20`，历史 CSV `42.252313`，当前 DB 重新生成 `42.25231257158`。全量最大差异为 `S5470301` 在 `2026-05-28`: 历史 CSV `6140.0`，当前 DB 经原始 data_service 生成 `6280.0`；15 个缺失差异集中在 `2026-05-26`，历史 CSV 为 `0.0` 而当前 DB 生成为空。

## 独立数据差异报告

已新增离线验证报告，不接入前端、不影响实盘监控页:

- HTML 报告: `backtest_artifacts/model_muti_0529/historical_data_diff_report.html`
- 缺失差异按因子汇总: `backtest_artifacts/model_muti_0529/missing_diff_by_factor.csv`
- 缺失差异按月份汇总: `backtest_artifacts/model_muti_0529/missing_diff_by_month.csv`
- 数值差异按因子汇总: `backtest_artifacts/model_muti_0529/numeric_diff_by_factor.csv`
- 原始 data_service 审计摘要: `backtest_artifacts/model_muti_0529/original_daily_data_service_audit_summary.json`
- 原始 data_service DB 生成输出: `backtest_artifacts/model_muti_0529/original_data_service_generated_daily_output.csv`
- 原始 data_service DB 对齐输出: `backtest_artifacts/model_muti_0529/original_data_service_generated_daily_output_aligned.csv`

生成命令:

```bash
conda run -n forecast_env python scripts/generate_data_diff_report.py
conda run -n forecast_env python scripts/audit_original_daily_data_service.py
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
- 回测入口: `backtests/weekly_10y_reproduction.py`

预测语义: 本周六预测下一周最后一个交易日 10Y 收益率相对本周最后一个交易日是上行还是下行。live 预测按 `feature_date` 从源表反查实际 `week_id`；历史回测为复现上游 0529 周频算法，日期转换优先使用算法输出的 `month_date/week_date` 作为 legacy 特征周日期，再把次日作为 `predict_date`，并按特征日期排序后的下一条算法输出作为 `target_date`。跨年处保留上游算法真实输出的 `week_id=202553`，因此 2026-01 当前有 6 个历史样本；`future_return/label` 按重排后的 target 重新计算，避免 `target_date` 早于 `feature_date`。

| 方案 | 数据源 | run_id | 日期范围 | 样本 | 准确率 | 上涨 precision | 下跌 precision |
|------|--------|--------|----------|------|--------|----------------|----------------|
| `weekly_10y_d_overlay` | `framework_db_aligned` | 13 | `2025-07-05` 到 `2026-05-02` | 45 | `68.9% (31/45)` | `75.0%` | `66.7%` |

2026-06-06 修正记录: 最新前端展示 run 已刷新为 run_id=`13`。`2025-07` 月度样本数为 4，对应预测日 `2025-07-05/12/19/26`；`2025-10` 月度样本数为 5，对应特征周日期 `2025-10-03/10/17/24/31`；`2026-01` 月度样本数为 6，保留上游算法输出的跨年周 `week_id=202553, month_date=2026-01-04`。跨年 target 顺序已按特征日期重排为 `202552 -> 202601 -> 202553 -> 202602`，其中 `202553` 的 target 为 `2026-01-09`，该样本 label 从上行修正为下行。此前 run_id=`15` 因历史回测转换误用 live week_id 公式，把 `week_id=202527` 的算法 `month_date=2025-07-04` 偏移到 `2025-07-11`；同时通用月度统计按 `predict_date` 归月，导致 `2025-10-31` 特征周被错归到 2025-11。

2026-06-07 复核: 周度公共输入层已切换为 `/Users/macstudio0/Desktop/wind_export(1).py` 口径，生成 `840 x 575` 的 `weekly_output_2026-06-06.csv`。该输入与 `/Users/macstudio0/Desktop/weekly_output.csv` 在行数、列数、列顺序和周范围结构上对齐，关键最新周收益率列和最新模型输出一致；但共同周/共同因子输入仍有少量实质数值差异、缺失差异和大量小数精度差异，完整明细已保存在:

- `reports/weekly_output_csv_validation_summary.json`
- `reports/weekly_output_csv_vs_current_db_material_diff.csv`
- `reports/weekly_output_csv_vs_current_db_until_202604_prediction_diff.csv`
- `reports/weekly_output_csv_prediction_diff_weeks_input_changes.csv`
- `reports/weekly_input_artifact_wind_export1_vs_desktop_summary.json`
- `reports/weekly_input_artifact_wind_export1_vs_desktop_diff.csv`

当前前端展示以最新 DB 公共层回测 run_id=`13` 为准，整体样本数 45、正确数 31、准确率 `68.9%`。

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
| `t_backtest_runs` | 8 |
| `t_backtest_predictions` | 7022 |
| `t_backtest_monthly_metrics` | 379 |
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

前端不新增历史验证结果页。历史复现用于后端验证、脚本验收和文档记录；前端继续按原有方案结果矩阵展示方案，后续新增方案后由方案自身结果进入现有展示链路。2026-06-07 已通过 `GET /api/backtests/factor-lab` 核验，`10Y国债活跃 · 周度` 格子展示当前 DB 版本最新 run_id=`13` 的 `68.9% / 1 个方案`，排行显示 `68.9%（31/45）`。

## 验证命令

```bash
conda run -n forecast_env python -m backtests.reproduction --n-jobs 4
conda run -n forecast_env python scripts/verify_reproduction.py
```

最终验证脚本输出:

```text
ok check_original_csvs
ok check_data_alignment
ok check_t5_reproduction
ok check_t1_reproduction
ok check_api_endpoints
ok check_frontend_entry
```
