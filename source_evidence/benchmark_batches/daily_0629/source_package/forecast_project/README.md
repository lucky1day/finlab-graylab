# forecast_project_0616

本目录是最终筛选 9 个模型的本地生产格式迁移包，用于上线改造前验证。目录结构按 `/Users/fengrl/Documents/factor_processing/db_data/迁移模版.md` 收敛：根目录只保留三频共享能力、统一入口和说明；频率私有代码放在各自 `*_project/src` 下；运行产物放在各自 `check_data`、`output`、`logs`、`hyper_params` 下。

本包不保存训练后的模型权重。所有模型按固定配置在运行时重新训练、重建策略信号或重建月频候选结果。生产入口默认 `DRY_RUN=0` 写数据库；本地验证请显式设置 `DRY_RUN=1` 或使用 `run_backtest_test.sh`。

## 目录

| 路径 | 作用 |
|---|---|
| `daily_project/` | 日频 `D1Y/D5Y/D10Y` 生产式本地运行包 |
| `weekly_project/` | 周频 `W1Y/W5Y/W10Y` 生产式本地运行包 |
| `monthly_project/` | 月频 `M1Y/M5Y/M10Y` 生产式本地运行包 |
| `data_service.py` | 三频数据库长表到宽表的公共服务 |
| `db_config.py` | 数据库连接配置 |
| `shap_policy.py` | 三频 SHAP 方向归一化策略 |
| `scripts/forecast_env.sh` | 统一 Python、PYTHONPATH、DRY_RUN 环境 |
| `run_backtest_test.sh` | 本地测试回测总入口 |
| `run_backtest.sh` | 正式口径回测总入口，默认写数据库 |

## 最终模型

| 频率 | 期限 | 最终编号 | 生产入口 |
|---|---|---|---|
| 日频 | 1Y | `1Y13` | `daily_project/src/daily/selected_models/1y13/run.py` |
| 日频 | 5Y | `5Y10` | `daily_project/src/daily/selected_models/5y10/run.py` |
| 日频 | 10Y | `10Y04` | `daily_project/src/daily/selected_models/10y04/run.py` |
| 周频 | 1Y | `WEEKLY-1Y-LGBM-01` | `weekly_project/src/weekly/run_weekly.py` |
| 周频 | 5Y | `WEEKLY-5Y-LGBM-01` | `weekly_project/src/weekly/run_weekly.py` |
| 周频 | 10Y | `WEEKLY-10Y-LGBM-01` | `weekly_project/src/weekly/run_weekly.py` |
| 月频 | 1Y | `1Y-06` | `monthly_project/src/monthly/selected_models/1y06/run.py` |
| 月频 | 5Y | `5Y-06` | `monthly_project/src/monthly/selected_models/5y06/run.py` |
| 月频 | 10Y | `10Y-01` | `monthly_project/src/monthly/selected_models/10y01/run.py` |

## 数据来源

生产入口默认只从数据库构建输入，不再读取包内预置数据，也不再支持 `FORECAST_DATA_ROOT` 或 `WEEKLY_INPUT_CSV` 之类的本地数据兜底。三频入口会在每次运行时把数据库快照写入标准产物目录，作为审查证据和候选模型子进程的临时输入：

- 日频：`daily_project/check_data/YYYY-MM-DD/model_input/candidate_data_root/`
- 周频：`weekly_project/check_data/YYYY-MM-DD/model_input/`
- 月频：`monthly_project/output/YYYY-MM-DD/standardized_data/input_snapshot/`

这些 CSV 是当次数据库输入的落盘审计，不是部署输入源；生产代码不得依赖任何包内历史数据文件。

## 运行

```bash
cd /Users/fengrl/Documents/factor_processing/final_select/forecast_project_0616

DRY_RUN=1 PYTHON_BIN=/Users/fengrl/miniconda3/envs/factor-mac/bin/python \
  bash daily_project/src/run_daily.sh run 2026-06-10

DRY_RUN=1 PYTHON_BIN=/Users/fengrl/miniconda3/envs/factor-mac/bin/python \
  bash weekly_project/run_weekly_pipeline.sh run 2026-06-10

DRY_RUN=1 PYTHON_BIN=/Users/fengrl/miniconda3/envs/factor-mac/bin/python \
  bash monthly_project/run_monthly_pipeline.sh run 2026-04-15
```

总入口：

```bash
DRY_RUN=1 PYTHON_BIN=/Users/fengrl/miniconda3/envs/factor-mac/bin/python \
  bash run_backtest_test.sh 2026-04-15 2026-04-15 all
```

## 标准产物

日频、周频项目按运行日生成；月频项目按 DB `rdate` 生成，DB `rdate` 为 `feature_month` 的 15 日，真实触发日记录在 metadata 中：

- `check_data/YYYY-MM-DD/{raw,model_input,metadata}`
- `output/YYYY-MM-DD/{standardized_data,train_data,predict_data,prediction,factors,shap,db_payload,backtest}`
- `logs/YYYY-MM-DD`
- `hyper_params/YYYY-MM-DD`

## 验证

```bash
/Users/fengrl/miniconda3/envs/factor-mac/bin/python -m py_compile \
  data_service.py db_config.py runtime_common.py shap_policy.py \
  daily_project/src/daily/*.py weekly_project/src/weekly/*.py monthly_project/src/monthly/*.py

/Users/fengrl/miniconda3/envs/factor-mac/bin/python -m pytest \
  daily_project/tests weekly_project/tests monthly_project/tests -q
```

迁移报告保存在 `docs/production_migration_report.md`。
9 个模型的 SHAP 方案保存在 `docs/shap方案_9模型.md`。
