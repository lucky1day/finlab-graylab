# 已入库方案 Benchmark 与数据库明细对齐验证结论（历史归档）

**验证日期**: 2026-06-14
**验证分支**: `codex/audit-bugfixes-20260613`
**归档状态**: 历史一次性 DB 现场审计记录，不再作为当前常规 `unittest` 入口。

> 说明：原验证入口 `tests/test_onboarded_benchmark_feature_alignment.py` 依赖当时的 active 方案数量、benchmark 文件和数据库现场状态。随着 7Y 入库、weekly 手动补平、回测 run 清理和 `task_type` 契约升级，该测试已不适合作为长期可复用测试，已从 `tests/` 中移除。本文仅保留 2026-06-14 当天的验证口径和结论，后续当前状态校验应使用 harness gate、方案 benchmark 回归测试和 API/前端契约测试。

## 1. 验证口径

本次验证只读当前仓库、原始 benchmark CSV 和数据库明细，不修改业务数据。

核心规则：

- 原始算法 benchmark 中的 `T/date/predict_date` 表示 source T / 原始算法预测站位日。
- 平台数据库中与 source T 对齐的字段是 `feature_date`，不是实盘语义下的 `predict_date`。
- 历史回测区间样本与 `t_backtest_predictions.feature_date` 对齐。
- `target_date >= 2026-06-01` 的灰度/实盘观察区样本与 `t_scheme_predictions.feature_date` 对齐，并检查 `prediction_phase`。
- 完整逐行主键使用 `feature_date + target_date + target_tenor + horizon`。
- `benchmark_required=true` 的方案不得依赖旧列名或缺字段回退；缺少 `feature_date/target_date/target_tenor/horizon/direction/confidence/label/is_correct` 任一字段或值即 fail-closed。

结论等级：

| 等级 | 含义 |
|------|------|
| `PASS` | 原始 benchmark 覆盖行可按标准主键定位，方向、置信度、标签/正确性与数据库明细一致。 |
| `FAIL` | 至少一条可比较 benchmark 样本与数据库明细不一致。 |

## 2. 总体验证结果

当时测试命令（归档留痕；对应测试文件已移除，不再作为当前命令使用）：

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest tests.test_onboarded_benchmark_feature_alignment -v
```

当前总览：

| 指标 | 数值 |
|------|-----:|
| 已检查 active source-backed 方案 | 6 |
| 原始 benchmark raw rows | 2182 |
| 去重后可比较 rows | 2182 |
| 与 backtest 明细比对 rows | 2177 |
| 与 live 明细比对 rows | 5 |
| DB 缺失 rows | 0 |
| DB 多重命中 rows | 0 |
| benchmark 重复冲突 | 0 |
| 字段不一致 rows | 0 |
| 旧格式无 `target_date` rows | 0 |
| 被排除 rows | 0 |

说明：`t1_daily` / `t5_daily` 已用 `scripts/rebuild_daily0529_scheme_benchmarks.py` 重建为严格新格式完整 baseline，旧的 `PASS_WITH_LEGACY_SAMPLE_LIMITATIONS` 结论已经闭环关闭。2026-06-14 进一步移除了 `target_date=2026-05-25..2026-05-29` 的历史临时排除，并用 DB target completion 补齐 2026-05-29 目标验证日；`daily_5y_2_v28` 已用 `scripts/rebuild_v28_scheme_benchmark.py` 重建严格 May 2026 benchmark，并修复旧连续 test window 写入的灰度明细；此前失败结论已经闭环关闭。

## 3. 逐方案结论

| 方案 | 原始 rows | 可比较唯一 rows | 比对位置 | 结论 | 说明 |
|------|----------:|----------------:|----------|------|------|
| `daily_5y_2_v28` | 18 | 18 | backtest 13 + live 5 | `PASS` | V28 current 侧由共享 inference helper 生成；旧 `run_id=42` 错误灰度明细已删除，新 `run_id=58` 与原始 benchmark 对齐。 |
| `t1_daily` | 674 | 674 | backtest 674 | `PASS` | 当前注册 `5Y/10Y` 全量严格 benchmark 已重建，字段完整且逐行一致；不再包含 `1Y` 预测 target rows；2026-05 目标月已覆盖到 `target_date=2026-05-29`。 |
| `t5_daily` | 1332 | 1332 | backtest 1332 | `PASS` | 当前注册 `3Y/5Y/7Y/10Y` 全量严格 benchmark 已重建，字段完整且逐行一致；2026-05 目标月已覆盖到 `target_date=2026-05-29`。 |
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

- `t1_daily`: original/current 各 674 行，`target_tenor` 仅包含 `5Y/10Y`，不再包含旧 sample 中的 `1Y` 预测 target rows。
- `t5_daily`: original/current 各 1332 行，`target_tenor` 包含 `3Y/5Y/7Y/10Y`。
- 根目录 canonical `benchmarks/model_muti_0529/daily_output.csv` 截至 `2026-05-28`；逐方案 benchmark 为覆盖完整 2026-05 目标月，会通过 `shared.input_artifacts` 从 DB 追加 `2026-05-29` 目标验证日。
- `t1_daily` 的旧 core 参数现在明确命名为 `target_date`：最后一条 5 月目标日是 `target_date=2026-05-29`，模型站位为最后一个 `< target_date` 的交易日，即 `feature_date=2026-05-28`。
- `t5_daily` 的最后一组 5 月目标日为 `target_date=2026-05-25..2026-05-29`，对应 source T / `feature_date=2026-05-18..2026-05-22`。追加 `2026-05-29` 只用于 label/actual，不把 T+5 的模型输入截止推到 target 日。
- 两个方案的 CompareGate 均使用严格主键 `feature_date + target_date + target_tenor + horizon`，missing/extra=0，direction mismatch=0，confidence max abs diff=0。

## 5. V28 不一致闭环记录

### `daily_5y_2_v28`

旧问题样本：

```text
source T / feature_date = 2026-05-28
target_date             = 2026-06-04
target_tenor            = 5Y
horizon                 = 5
benchmark direction     = 1
benchmark confidence    = 1.0
```

旧数据库实盘明细曾为：

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

根因：V28 源算法是 test-window 敏感算法，Phase C 的 monthly ensemble / signal selection 会因 test window 改变输出。旧平台实盘 wrapper 使用连续窗口 `2024-07-01..feature_date`，而 source May 2026 benchmark 使用月度窗口 `2026-05-01..feature_date`。

闭环修复：

1. `predict.py`、benchmark current 生成和 backtest runner 已统一调用 `schemes.daily_5y_2_v28.inference`。
2. 核心窗口固定为 `feature_date` 所在月月初到 `feature_date`，不得超过 `feature_date`。
3. 旧 `run_id=42` 的错误灰度预测明细已受控删除，`t_scheme_runs/t_scheme_run_log` 保留审计。
4. 新 `run_id=58` 已写入：

```text
table                   = t_scheme_predictions
run_id                  = 58
prediction_phase        = gray_live
predict_date            = 2026-05-29
feature_date            = 2026-05-28
target_date             = 2026-06-04
database direction      = 1
database confidence     = 1.0
```

当前结论：`daily_5y_2_v28` 的 18 条 benchmark rows 全部可按 `feature_date + target_date + target_tenor + horizon` 定位，方向、置信度、标签/正确性一致，结论为 `PASS`。

## 6. 后续要求

1. 后续所有新方案 benchmark 文件必须显式写 `feature_date,target_date,target_tenor,horizon,direction,confidence,label,is_correct`；旧列名 `predict_date/date/tenor` 不得作为 `benchmark_required=true` 的静默回退路径。
2. 根目录 `benchmarks/{benchmark_id}/` 只保留批次级 canonical 输入归档；逐方案 CompareGate baseline 只能放在 `schemes/{scheme_id}/benchmarks/`。
3. 若源算法存在 test-window-sensitive 的 selector、ensemble、rolling top-K、分月校准或信号组合逻辑，必须抽共享 inference helper，并同时服务 adapter、benchmark current 和 backtest runner。
