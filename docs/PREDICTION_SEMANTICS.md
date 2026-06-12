# 预测日期与实盘阶段语义

**更新日期**: 2026-06-12

本文是平台关于 `predict_date` / `feature_date` / `target_date` 与灰度实盘阶段的强制语义。前端、后端、回测、SOP、方案文档和测试用例必须使用同一套术语；如与旧文档冲突，以本文为准，并回写对应文档。

## 1. 三个标准日期字段

| 字段 | 平台含义 | 是否给前端/业务使用 |
|------|----------|:------------------:|
| `predict_date` | 信号发出日 / 调度运行日 | 是 |
| `feature_date` | 数据截止日 / 预测站位日，模型只能使用该日及以前允许可见的数据 | 是 |
| `target_date` | 验证目标日，用于展示、去重、actual join 和月度统计归属 | 是 |

`feature_date` 是平台、业务和前端唯一标准数据截止字段。`anchor_date` 只允许作为方案内部算法变量或历史审计 extra 保留；任何对外语义、前端展示、灰度规则、回测规则都不得依赖 `anchor_date`。如果 `extra` 同时保留 `anchor_date`，它必须等于 `feature_date`。

## 2. 三种运行口径

| 口径 | `prediction_phase` | `predict_date` | `feature_date` | `target_date` | 写库位置 |
|------|--------------------|----------------|----------------|---------------|----------|
| 历史回测 | 不写入实盘 phase | `T` | `T` | `T + horizon` | `t_backtest_*` |
| 灰度实盘 | `gray_live` | `T + 1` | `T` | `T + horizon` | `t_scheme_predictions` |
| 正式实盘 | `scheduled_live` | `T + 1` | `T` | `T + horizon` | `t_scheme_predictions` |

灰度实盘也属于实盘观察区。它与正式实盘的区别不是预测日期公式，而是来源阶段：灰度通常是方案部署前后的受控补齐或观察，正式实盘是 scheduler 在真实时钟自然触发。

## 3. 灰度实盘规则

灰度实盘用于补齐从灰度 target 起点到正式部署前的实盘观察序列。当前 V28 批次的灰度 target 起点是 `target_date >= 2026-06-01`；后续方案必须按方案生命周期登记自己的灰度起点和正式调度起点，不能把日期写成全局常量。

灰度补齐必须满足：

1. 灰度记录必须标识为 `prediction_phase = gray_live`。
2. 记录的 `predict_date` 仍是应当发出信号的调度日，日频通常为 `T + 1`。
3. `feature_date` 必须是 `T`，所有输入 artifact、辅助输入映射和模型训练窗口都不得越过 `feature_date`。
4. 当前数据库在事后补齐时可能已经拥有 `T+1` 或更晚的数据；补齐逻辑必须显式按 `feature_date` 约束输入，不能只依赖当前 DB 最新状态。
5. 如源表提供 `create_time` 或等价 as-of 字段，灰度补齐应进一步证明数据在预期发出时点可见；没有该证据时，不得把灰度样本宣称为 point-in-time 绩效，只能称为灰度实盘观察。

## 4. 正式实盘规则

正式实盘记录必须标识为 `prediction_phase = scheduled_live`。正式实盘起点来自方案激活并由 scheduler 自然成功发出的第一条记录，不能由灰度 target 起点反推。

正式实盘同样只能使用 `feature_date = T` 及以前可见的数据。日频工作日早盘预测的典型形态是：

```text
predict_date = T + 1
feature_date = T
target_date  = T + horizon
```

## 5. 回测规则

历史回测必须保持 T 语义：

```text
predict_date = T
feature_date = T
target_date  = T + horizon
```

回测结果只写 `t_backtest_*`，不得读取或复制 `t_scheme_predictions` 中的灰度/正式实盘记录来拼历史结果。参与前端历史排行的样本统一要求 `predict_date >= 2025-01-01`；训练、筛因子、模型 warmup 和定期更新可使用更早历史数据。

当方案已有灰度实盘观察区时，历史回测 runner 必须按 `target_date` 截断，避免同一 target 月同时由 backtest 和 live 区间重复解释。当前 V28 批次的历史回测只保留 `target_date < 2026-06-01`。

## 6. 前端展示规则

前端可以展示灰度实盘和正式实盘，但必须能区分 `prediction_phase`：

- `gray_live`：灰度实盘观察。
- `scheduled_live`：正式实盘。

前端与业务不读取 `anchor_date`。需要展示预测站位或数据截止时，统一显示 `feature_date`。月度行、明细归属、actual join 和去重仍统一按 `target_date`。

## 7. 当前 V28 判定

对 `daily_5y_2_v28` 当前已知记录：

| run_id | 日期范围 | 阶段 |
|--------|----------|------|
| `39`-`51` | `predict_date=2026-05-26` 至 `2026-06-11`，`target_date=2026-06-01` 至 `2026-06-17` | `gray_live` |
| `52` | `predict_date=2026-06-12`，`feature_date=2026-06-11`，`target_date=2026-06-18` | `scheduled_live` |

这些阶段标识已由迁移 `010_prediction_semantics.sql` 落到 `t_scheme_predictions.prediction_phase` 和 `t_scheme_runs.prediction_phase`，并由 `/api/metrics/{scheme_id}` 的 `daily_rows[].prediction_phase` 与 `phase_ranges` 对前端输出。`run_id=39`-`51` 是灰度实盘，`run_id=52` 是正式 scheduler 实盘，二者不得混称。
