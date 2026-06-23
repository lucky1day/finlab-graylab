# 周度实盘 2026-06-18 补齐与前端展示修复

**验证日期**: 2026-06-23
**验证分支**: `codex/audit-bugfixes-20260613`

## 背景

2026-06 周度验证表中，三个周度方案只显示 `2026-06-05` 和 `2026-06-12` 两个目标周样本。用户侧期望看到 6 月 19 日这一周的预测和实盘结果。

DB 复核结果：

- `2026-06-19` 在 `t_trade_calendar` 中 `trade_flag='0'`，不是交易日。
- 平台周度目标点是下一周周五；若周五非交易日，则使用该周最后交易日。因此本周前端应显示 `target_date=2026-06-18`。
- `t_scheme_weekly_actuals` 中 `5Y/7Y/10Y` 的 `target_date=2026-06-18` actual 均已存在，`direction_weekly=-1`。
- 补齐前，三个周度方案在 `t_scheme_predictions` 中 `target_date=2026-06-18` 均为 0 行，因此前端/API 无 prediction row 可展示。

## 修复

### 1. 补齐周度实盘 prediction

使用标准写库入口 `scheduler.executor.execute_scheme` 补跑 `predict_date=2026-06-13`，没有手写 SQL。该调度日对应：

- `feature_date=2026-06-12`
- `feature_week_id=202622`
- `target_week_id=202623`
- `target_date=2026-06-18`
- `prediction_phase=scheduled_live`

补齐结果：

| base_scheme_id | target_tenor | run_id | predict_date | feature_date | target_date | predicted_direction | confidence |
|---|---|---:|---|---|---|---:|---:|
| `weekly_5y_direct_0529` | `5Y` | 218 | `2026-06-13` | `2026-06-12` | `2026-06-18` | -1 | 0.483333 |
| `weekly_7y_cross_d_overlay_0529` | `7Y` | 219 | `2026-06-13` | `2026-06-12` | `2026-06-18` | -1 | 0.483333 |
| `weekly_10y_d_overlay_0529` | `10Y` | 220 | `2026-06-13` | `2026-06-12` | `2026-06-18` | -1 | 0.320000 |

对应 `t_scheme_run_log` 为 success：

- run_id `218`: `weekly_5y_direct_0529`
- run_id `219`: `weekly_7y_cross_d_overlay_0529`
- run_id `220`: `weekly_10y_d_overlay_0529`

补齐后 `/api/metrics` 的 2026-06 周度样本：

| registry_scheme_id | samples | target_date 集合 |
|---|---:|---|
| `weekly_5y_direct_0529__h6__5Y` | 3 | `2026-06-05`, `2026-06-12`, `2026-06-18` |
| `weekly_7y_cross_d_overlay_0529__h6__7Y` | 3 | `2026-06-05`, `2026-06-12`, `2026-06-18` |
| `weekly_10y_d_overlay_0529__h6__10Y` | 3 | `2026-06-05`, `2026-06-12`, `2026-06-18` |

### 2. 前端周度展示文案

前端周度验证表头从 `预测周` 改为 `目标周五`。说明文案补充：周五非交易日时显示该周最后交易日。

原因：周度方案预测的是下一实际周的周五目标点；例如 2026-06-19 为非交易日时，验证目标显示为 `06/18`。

同时将周度候选排行的低样本提示阈值从日频的 30 条改为少于 3 条才提示，避免 2026-06 当前完整三周样本仍显示 `样本不足`。

### 3. actuals 调度链路

`scheduler.main.run_actuals_job` 已同时刷新日频 actuals 和周频 actuals：

- `update_actuals(end_date=target_date)`
- `update_weekly_actuals(end_date=target_date)`

这保证每日 actuals job 运行后，`t_scheme_weekly_actuals` 也会跟随更新，不再需要单独手工刷新周度 actuals。

## 验证

已运行：

```bash
conda run -n bond_factor_lab_service python -m unittest \
  tests.test_frontend_factor_lab \
  tests.test_backend_serving \
  tests.test_weekly_metrics \
  tests.test_prediction_context \
  tests.test_weekly_actuals \
  tests.test_scheduler_main
```

结果：

```text
Ran 55 tests
OK
```

DB/API 复核：

- `t_scheme_predictions` 中 `target_date=2026-06-18` 的周度 prediction 行数为 3。
- `t_scheme_weekly_actuals` 中 `target_date=2026-06-18` 的 `5Y/7Y/10Y` actual 行数为 3。
- 三个周度 registry metrics 的 2026-06 样本数均为 3，target 集合均为 `2026-06-05/2026-06-12/2026-06-18`。

## 后续观察

下一次周六调度后，需要继续确认：

- `predict_date=2026-06-20` 不应因目标周日历不完整写入错误 `target_date`。
- 当目标周完整后，周度 prediction 与 weekly actuals 继续按 `target_date` 对齐。
- 前端周度验证表展示 `目标周五`，非交易周五显示该周最后交易日。
