# 已入库方案 Benchmark 与数据库明细对齐验证结论

**验证日期**: 2026-06-14
**验证分支**: `codex/audit-bugfixes-20260613`
**验证测试**: `tests/test_onboarded_benchmark_feature_alignment.py`

## 1. 验证口径

本次验证只读当前仓库、原始 benchmark CSV 和数据库明细，不修改业务数据。

核心规则：

- 原始算法 benchmark 中的 `T/date/predict_date` 表示 source T / 原始算法预测站位日。
- 平台数据库中与 source T 对齐的字段是 `feature_date`，不是实盘语义下的 `predict_date`。
- 历史回测区间样本与 `t_backtest_predictions.feature_date` 对齐。
- `target_date >= 2026-06-01` 的灰度/实盘观察区样本与 `t_scheme_predictions.feature_date` 对齐，并检查 `prediction_phase`。
- 完整逐行主键使用 `feature_date + target_date + target_tenor + horizon`。
- `benchmark_required=true` 的方案不得依赖旧列名或缺字段回退；缺少 `feature_date/target_date/target_tenor/horizon/direction/confidence` 任一字段或值即 fail-closed。

结论等级：

| 等级 | 含义 |
|------|------|
| `PASS` | 原始 benchmark 覆盖行可按标准主键定位，方向、置信度、标签/正确性与数据库明细一致。 |
| `FAIL` | 至少一条可比较 benchmark 样本与数据库明细不一致。 |

## 2. 总体验证结果

测试命令：

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest tests.test_onboarded_benchmark_feature_alignment -v
```

当前总览：

| 指标 | 数值 |
|------|-----:|
| 已检查 active source-backed 方案 | 6 |
| 原始 benchmark raw rows | 2152 |
| 去重后可比较 rows | 2152 |
| 与 backtest 明细比对 rows | 2147 |
| 与 live 明细比对 rows | 4 |
| DB 缺失 rows | 0 |
| DB 多重命中 rows | 0 |
| benchmark 重复冲突 | 0 |
| 字段不一致 rows | 1 |
| 旧格式无 `target_date` rows | 0 |
| 被排除 rows | 0 |

说明：`t1_daily` / `t5_daily` 已用 `scripts/rebuild_daily0529_scheme_benchmarks.py` 重建为严格新格式完整 baseline，旧的 `PASS_WITH_LEGACY_SAMPLE_LIMITATIONS` 结论已经闭环关闭。

## 3. 逐方案结论

| 方案 | 原始 rows | 可比较唯一 rows | 比对位置 | 结论 | 说明 |
|------|----------:|----------------:|----------|------|------|
| `daily_5y_2_v28` | 18 | 18 | backtest 13 + live 4 | `FAIL` | 有 1 条灰度实盘区间样本与原始 benchmark 不一致。 |
| `t1_daily` | 664 | 664 | backtest 664 | `PASS` | 当前注册 `5Y/10Y` 全量严格 benchmark 已重建，字段完整且逐行一致；不再包含 `1Y` 预测 target rows。 |
| `t5_daily` | 1312 | 1312 | backtest 1312 | `PASS` | 当前注册 `3Y/5Y/7Y/10Y` 全量严格 benchmark 已重建，字段完整且逐行一致。 |
| `weekly_5y_direct_0529` | 71 | 71 | backtest 71 | `PASS` | `feature_date/feature_week_id/target_date/target_tenor/horizon` 全部可定位，方向、置信度、标签一致。 |
| `weekly_7y_cross_d_overlay_0529` | 42 | 42 | backtest 42 | `PASS` | `framework_feature_date/framework_target_date` 对齐 DB 明细，方向、置信度、标签一致。 |
| `weekly_10y_d_overlay_0529` | 45 | 45 | backtest 45 | `PASS` | `feature_week_id` 映射到 DB `feature_date` 后逐行一致。 |

## 4. T1/T5 严格 Baseline 重建

重建命令：

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python scripts/rebuild_daily0529_scheme_benchmarks.py \
  --scheme-id t1_daily \
  --scheme-id t5_daily
```

重建后的逐方案文件：

- `schemes/t1_daily/benchmarks/original_predictions_sample.csv`
- `schemes/t1_daily/benchmarks/current_predictions_sample.csv`
- `schemes/t5_daily/benchmarks/original_predictions_sample.csv`
- `schemes/t5_daily/benchmarks/current_predictions_sample.csv`

固定字段顺序：

```text
feature_date,target_date,target_tenor,horizon,direction,confidence,label,is_correct
```

验收结论：

- `t1_daily`: original/current 各 664 行，`target_tenor` 仅包含 `5Y/10Y`，不再包含旧 sample 中的 `1Y` 预测 target rows。
- `t5_daily`: original/current 各 1312 行，`target_tenor` 包含 `3Y/5Y/7Y/10Y`。
- 两个方案的 CompareGate 均使用严格主键 `feature_date + target_date + target_tenor + horizon`，missing/extra=0，direction mismatch=0，confidence max abs diff=0。

## 5. 仍未闭环的不一致

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

结论：这条样本必须拿原始 benchmark 的 `2026-05-28` 对齐数据库 `feature_date=2026-05-28`。对齐后方向和置信度均不一致，因此 `daily_5y_2_v28` 不能判定为与原始 benchmark 完全一致。

## 6. 后续要求

1. `daily_5y_2_v28` 需要单独排查 `feature_date=2026-05-28,target_date=2026-06-04` 的灰度实盘记录为什么与原始 benchmark 不一致。
2. 后续所有新方案 benchmark 文件必须显式写 `feature_date,target_date,target_tenor,horizon,direction,confidence`；旧列名 `predict_date/date/tenor` 不得作为 `benchmark_required=true` 的静默回退路径。
3. 根目录 `benchmarks/{benchmark_id}/` 只保留批次级 canonical 输入归档；逐方案 CompareGate baseline 只能放在 `schemes/{scheme_id}/benchmarks/`。
