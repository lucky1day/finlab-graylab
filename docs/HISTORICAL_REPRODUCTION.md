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
3. 日频、周频、月频公共层内部统一调用 `shared.data_service`；该文件来自用户提供的 `data_service (1).py`，仅做项目 `.env`/`shared.db_config` 连接适配。
4. 算法只读取这张生成后的 CSV。

框架入口:

- live adapter: `schemes/t1_daily/predict.py`、`schemes/t5_daily/predict.py`（早先的周度 adapter 已退役/代码未实现）
- 公共输入文件层: `shared/input_artifacts.py`
- 日频 data service: `shared/data_service.py`
- 只读审计脚本: `scripts/audit_daily_data_service.py`

统一 `shared.data_service` 的日频交易日锚定列为 `TB1YWI0C/TB5YWI0C/TB0YWI0C`，artifact 层不再额外传入 `target_columns`。2026-06-08 已用该统一数据层重跑日频 `--no-persist` 历史复现，t5/t1 framework-db 与 baseline 的 mismatch 仍为 0。

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

- `scheme_id` 永远表示具体方案，例如 `t1_daily`、`t5_daily`（早先示例中的周度 `weekly_10y_d_overlay` 已退役/代码未实现）。
- `benchmark_id` 表示历史基准批次，例如 `model_muti_0529`，只用于 `benchmarks/` 和 backtest 表。
- `data_source` 是数据库兼容枚举，当前保留 `baseline_original_csv`、`framework_original_csv`、`framework_db_aligned`；API 会映射为中文展示名。
- 运行期输入文件统一写入 `backtest_artifacts/runtime_inputs/{scheme_id}/`。
- 历史回测和数据差异报告统一写入 `backtest_artifacts/backtests/{benchmark_id}/`，不再使用 `model_muti_0529_daily` 这类伪方案名。

## 通用日频 runner

日频历史回测的共用骨架已抽到 `backtests/_base_runner.py`。新增日频方案若要参与历史排行，不应复制 `daily_0529_reproduction.py`，而是新建 `backtests/{scheme_id}_reproduction.py`，声明一个 `BacktestSpec`，再复用 `BaseDailyBacktestRunner` 或其中的函数式工具生成 `RunOutput`、月度指标、summary、行级比对和 backtest 表落库。

最小接入原则:

- 输入仍必须先经 `shared.input_artifacts.build_daily_input_artifact()` 生成并读回，数据口径要求见 [SCHEME_INGESTION.md §4](SCHEME_INGESTION.md#4-数据口径对齐)。
- 算法调用只进入 `schemes/{scheme_id}/core/` 或 adapter 暴露的纯预测逻辑，不在 runner 内直连源数据表拼输入。
- 落库只调用 `backtests.repository`，通常通过 `_base_runner.persist_run_output()` 或 `BaseDailyBacktestRunner.persist_run_output()` 完成。
- `benchmark_id` 表示历史基准批次，`scheme_id` 表示方案身份，`data_source` 表示输入口径，三者不要混用。

`daily_0529_reproduction.py` 是特殊的 0529 批次复现入口: 一个 runner 同时生成 `t1_daily` 和 `t5_daily` 多组数据源结果。普通新方案优先保持单方案单 runner，只在文件顶部放方案自己的日期范围、目标列、预期报告或排除区间，其他指标、比对和落库逻辑复用 `_base_runner`。

生成命令:

```bash
python -m scripts.run_baseline --scheme-id t1_daily
python -m scripts.run_framework_repro --scheme-id t1_daily --algo-env forecast_env
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

## weekly 周度方案复现（已退役）

周度方案 `weekly_10y_d_overlay` / `weekly_5y_direct_production` / `weekly_7y_cross_d_overlay` 均已退役/代码未实现：方案目录、adapter、`backtests/weekly_*_reproduction.py` runner 已不在代码库中，对应的 `t_backtest_*` 与 `t_scheme_*` 记录已于 2026-06-09 清理。本节早先记录的周度 10Y/5Y/7Y 历史回测 run、月度样本分布与前端周度格子数据均已失效，不再适用。如需周度历史复现，须先按 SOP 重新接入对应周度方案。

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
| `t_backtest_runs` | 12 |
| `t_backtest_predictions` | 8111 |
| `t_backtest_monthly_metrics` | 649 |
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

前端不新增历史验证结果页。历史复现用于后端验证、脚本验收和文档记录；前端继续按原有方案结果矩阵展示方案，后续新增方案后由方案自身结果进入现有展示链路。早先记录的前端周度明细归月口径与 `10Y/5Y/7Y 国债活跃 · 周度` 格子展示均对应已退役/代码未实现的周度方案，相关数据已失效；当前 factor-lab 矩阵只展示 `t1_daily` / `t5_daily` 的日度回测结果。如需周度展示，须先按 SOP 重新接入对应周度方案。

## 验证命令

```bash
conda run -n forecast_env python -m backtests.daily_0529_reproduction --n-jobs 4
python -m scripts.run_framework_repro --scheme-id t1_daily --algo-env forecast_env
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
