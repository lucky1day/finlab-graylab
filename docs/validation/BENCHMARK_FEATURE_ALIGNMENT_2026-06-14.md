# 已入库方案 Benchmark 与数据库明细对齐验证结论

**验证日期**: 2026-06-14
**验证分支**: `codex/audit-bugfixes-20260613`
**验证测试**: `tests/test_onboarded_benchmark_feature_alignment.py`

## 1. 验证口径

本次验证只读当前仓库、原始 benchmark CSV 和数据库明细，不修改任何业务代码或业务数据。

核心规则：

- 原始算法 benchmark 中的 `T/date/predict_date` 表示 source T / 原始算法预测站位日。
- 平台数据库中与 source T 对齐的字段是 `feature_date`，不是实盘语义下的 `predict_date`。
- 历史回测区间样本与 `t_backtest_predictions.feature_date` 对齐。
- `target_date >= 2026-06-01` 的灰度/实盘观察区样本与 `t_scheme_predictions.feature_date` 对齐，并检查 `prediction_phase`。
- 完整逐行主键优先使用 `feature_date + target_date + target_tenor + horizon`。
- 对于旧 benchmark 文件没有 `target_date` 的日频样本，只能按 `feature_date + target_tenor + horizon` 定位数据库唯一明细；这种情况只能证明“可比较字段一致”，不能证明严格完整逐行一致。

结论等级：

| 等级 | 含义 |
|------|------|
| `PASS` | 原始 benchmark 覆盖行可按标准主键定位，方向、置信度、标签/正确性与数据库明细一致。 |
| `PASS_WITH_LEGACY_SAMPLE_LIMITATIONS` | 当前可比较字段与数据库一致，但原始 benchmark sample 是旧格式或含非当前注册口径行，因此不能声称严格完整逐行一致。 |
| `FAIL` | 至少一条可比较 benchmark 样本与数据库明细不一致。 |

## 2. 总体验证结果

测试命令：

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest tests.test_onboarded_benchmark_feature_alignment -v
```

测试结果：`1 test OK`。

该测试的含义不是“所有方案都一致”，而是验证器成功跑完并产出当前逐方案结论。当前总览如下：

| 指标 | 数值 |
|------|-----:|
| 已检查 active source-backed 方案 | 6 |
| 原始 benchmark raw rows | 576 |
| 去重后可比较 rows | 286 |
| 与 backtest 明细比对 rows | 281 |
| 与 live 明细比对 rows | 4 |
| DB 缺失 rows | 0 |
| DB 多重命中 rows | 0 |
| benchmark 重复冲突 | 0 |
| 字段不一致 rows | 1 |
| 旧格式无 `target_date` rows | 110 |
| 被排除 rows | 72 |

## 3. 逐方案结论

| 方案 | 原始 rows | 可比较唯一 rows | 比对位置 | 结论 | 说明 |
|------|----------:|----------------:|----------|------|------|
| `daily_5y_2_v28` | 18 | 18 | backtest 13 + live 4 | `FAIL` | 有 1 条灰度实盘区间样本与原始 benchmark 不一致。 |
| `t1_daily` | 200 | 43 | backtest 43 | `PASS_WITH_LEGACY_SAMPLE_LIMITATIONS` | 当前注册 `5Y/10Y` 可比较字段一致；原始 sample 含 66 条非注册 `1Y` 行、6 条 `2025-01-01` 前站位行，且 43 条可比较唯一行均缺 `target_date`。 |
| `t5_daily` | 200 | 67 | backtest 67 | `PASS_WITH_LEGACY_SAMPLE_LIMITATIONS` | 当前注册 `3Y/5Y/7Y/10Y` 可比较字段一致；但 67 条可比较唯一行均缺 `target_date`，只能按 `feature_date + tenor + horizon` 定位 DB 明细。 |
| `weekly_5y_direct_0529` | 71 | 71 | backtest 71 | `PASS` | `feature_date/feature_week_id/target_date/tenor/horizon` 全部可定位，方向、置信度、标签一致。 |
| `weekly_7y_cross_d_overlay_0529` | 42 | 42 | backtest 42 | `PASS` | `framework_feature_date/framework_target_date` 对齐 DB 明细，方向、置信度、标签一致。 |
| `weekly_10y_d_overlay_0529` | 45 | 45 | backtest 45 | `PASS` | `feature_week_id` 映射到 DB `feature_date` 后逐行一致。 |

## 4. 不一致明细

### `daily_5y_2_v28`

原始 benchmark 第 18 行：

```text
source T / feature_date = 2026-05-28
target_date             = 2026-06-04
target_tenor            = 5Y
horizon                 = 5
benchmark direction     = 1
benchmark confidence    = 1.0
```

数据库实盘明细：

```text
table                   = t_scheme_predictions
run_id                  = 42
prediction_phase        = gray_live
predict_date            = 2026-05-29
feature_date            = 2026-05-28
target_date             = 2026-06-04
database direction      = 0
database confidence     = 0.0
```

结论：这条样本在最新规则下必须拿原始 benchmark 的 `2026-05-28` 对齐数据库 `feature_date=2026-05-28`。对齐后方向和置信度均不一致，因此 `daily_5y_2_v28` 不能判定为与原始 benchmark 完全一致。

## 5. 旧格式限制

### `t1_daily`

`schemes/t1_daily/benchmarks/original_predictions_sample.csv` 是旧格式 sample：

- 文件只有 `predict_date,tenor,direction,confidence`，没有 `target_date`。
- `predict_date` 按最新语义解释为 source T / 平台 `feature_date`。
- 文件含 `1Y` 行，但当前注册方案只包含 `5Y/10Y`。
- 文件含 `2024-12-31` 站位行，早于当前统一回测输出起点 `2025-01-01`。

本次只验证当前注册口径内、`feature_date >= 2025-01-01`、可在 DB 唯一定位的样本。43 条可比较唯一行方向和置信度均与 latest `framework_db_aligned` run_id=`99` 一致。

严格结论：可比较部分一致；由于 source sample 缺 `target_date` 且包含非当前注册口径行，不能声称“原始 benchmark 文件完整逐行严格一致”。

### `t5_daily`

`schemes/t5_daily/benchmarks/original_predictions_sample.csv` 同样是旧格式 sample：

- 文件只有 `predict_date,tenor,direction,confidence`，没有 `target_date`。
- `predict_date` 按最新语义解释为 source T / 平台 `feature_date`。
- 当前注册 `3Y/5Y/7Y/10Y` 均可比较。

67 条可比较唯一行方向和置信度均与 latest `framework_db_aligned` run_id=`96` 一致。

严格结论：可比较部分一致；由于 source sample 缺 `target_date`，不能声称“原始 benchmark 文件完整逐行严格一致”。

## 6. 后续建议

1. `daily_5y_2_v28` 需要单独排查 `feature_date=2026-05-28,target_date=2026-06-04` 的灰度实盘记录为什么与原始 benchmark 不一致。
2. `t1_daily` / `t5_daily` 如果后续要达到严格完整逐行一致证明，需要重新生成带 `feature_date,target_date,target_tenor,horizon,direction,confidence,label` 的 original benchmark 文件。
3. 后续所有新方案 benchmark 文件必须显式写 `feature_date` 或 `source_t`，并写 `target_date`；旧列名 `predict_date` 不应再作为新增 benchmark 的站位字段名。
