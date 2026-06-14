# 指标 Fail-Closed 问题闭环记录（2026-06-14）

**状态**: 已闭环  
**闭环提交**: `56eb3eb fix: remove backtest monthly metrics fallback`  
**适用范围**: 前端候选排行、前端月度明细、`/api/backtests/factor-lab`、live metrics、backtest runner 指标口径

## 为什么这个文档一度不可见

本文件原本用于记录“预测为平的样本是否进入指标分母”和“回测月度汇总表是否还能作为 fallback”的开放问题。2026-06-14 清理废弃路径时，我把它误判为临时讨论文档，并按“废弃文档直接删除”的规则移除了。

这个判断不完整：如果文档承担问题闭环记录，它不应该删除，而应该改成已闭环状态，明确最终规则、实现位置和验证证据。因此本文件恢复为闭环记录。

## 最终规则

预测方向 `predicted_direction=0` 表示“平”或“无方向信号”。

- `samples` / `sample_count` 是样本总数，包含预测为“涨”“跌”“平”的全部可评价样本。
- `metric_samples` / `metric_sample_count` 是指标分母，只包含 `predicted_direction in {-1, 1}` 的有方向样本。
- `correct` / `correct_count` 只在 `metric_samples` 范围内统计。
- 整体准确率、上涨准确率、上涨召回率、下跌准确率、下跌召回率等所有指标都必须排除预测为“平”的样本。
- 前端每日/周度验证表中，预测为“平”的行结果列显示 `-`，不得显示 `×`。

示例：某月 8 条可评价样本，其中 1 条预测为平，3 条方向预测正确，4 条方向预测错误，则样本数展示为 `8`，整体准确率为 `3/7`，不是 `3/8`。

## 已关闭的问题

1. `/api/backtests/factor-lab` 不能先读旧月度汇总再用明细覆盖。

   已修正为只从 latest run 的 `t_backtest_predictions` 明细动态聚合 `monthly_metrics` 和 `summary`。如果 latest run 没有明细，接口 fail-closed，不使用任何旧汇总表兜底。

2. 前端不能在缺少方向分布或 `metric_samples` 时回退到 `samples`。

   已修正为没有可推导指标分母时直接报错。前端展示必须使用 `metric_samples` / `metric_*_dist`，不得使用 `samples` 作为指标分母 fallback。

3. 后端不能为老 monthly row 增加兼容 fallback。

   已删除旧月度汇总 API 入口和 repository writer。正常业务路径不再读取或写入独立的回测月度指标汇总。

4. runner 不能写独立月度指标汇总表。

   `persist_run_output()` 当前只写 `t_backtest_runs` 和 `t_backtest_predictions`，再更新 run summary；前端 canonical 月度指标由 `/api/backtests/factor-lab` 从明细动态聚合。

5. 文档不能继续暗示旧汇总表是 baseline 或 fallback。

   已更新 `PREDICTION_SEMANTICS.md`、`CURRENT_STATUS.md`、`SCHEME_ONBOARDING_SOP.md`、`SCHEME_POST_ONBOARDING_TEST_SOP.md`、`ARCHITECTURE.md` 等文档，统一写明 `t_backtest_predictions` 明细是回测前端指标唯一事实源。

## 保留边界

`t_backtest_monthly_metrics` 如果仍出现在测试 fixture、受控删除脚本或 table guard 中，只能用于处理历史数据库状态、旧 run 清理或防止未来误写；不得重新进入业务读取、runner 写入、前端展示或 API fallback 路径。

如需物理删除旧 DB 表，必须另起迁移计划并先完成真实库审计；这不属于本次闭环范围。

## 验证证据

2026-06-14 已完成以下验证：

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest discover tests -v
# 329 tests OK

git diff --check
# pass

/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m scripts.verify_frontend_db \
  --scheme-id daily_5y_2_v28 \
  --api-base-url http://127.0.0.1:8100
# run_id=107, total_cells_checked=17, mismatch_count=0
```

同时已重启 backend 8100，并强制刷新前端页面到 `http://127.0.0.1:8100/`。

## 后续准入规则

- 新增指标字段时，必须同时声明“样本总数”和“指标分母”的含义。
- 新增前端或后端统计逻辑时，必须有测试覆盖 `predicted_direction=0` 的样本。
- 任何读取侧不得增加“缺字段时回退到 samples”的兼容逻辑。
- 任何回测展示不得重新读取独立月度汇总表作为事实源。
