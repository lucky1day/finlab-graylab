# Factor Lab 日频实盘与回测切换设计

## 目标

修正 Factor Lab Dashboard 中日频方案在灰度实盘起点后仍展示同一 `target_date`
回测记录的问题，避免同一业务样本被计入两次。

## 已确认事实

`one_y_t1_quote_state_hv_v1__h1__1Y` 当前 Dashboard 同时返回 382 条回测和
45 条实盘记录，两个来源内部的 `target_date` 均唯一。45 个实盘 `target_date`
与回测完全重叠：2026-06 为 21 条、2026-07 为 23 条、2026-08 为 1 条。
因此页面的 427 样本是跨来源重叠，不是数据库重复行。

## 方案比较

1. **复用前端切换规则（采用）**：以最早 live `target_date` 为边界，排除该日
   及之后的 backtest rows。Dashboard 与 legacy fallback 共用这条规则，保持两条
   前端读取路径的结果等价；不改写历史回测或实盘记录。
2. 在 API 层删除或筛掉回测记录：会改变审计可见的不可变回测数据，并扩大 API
   语义变更范围，不采用。
3. 在数据库删除重叠历史记录：会破坏回测审计，不采用。

## 设计

在 `frontend/aifin-shell.js` 中把既有的月份级 live/backtest cutoff 收敛为纯函数
的 `target_date` cutoff。它从 live 明细或 phase range 取最早 target date；若存在
cutoff，则只保留严格早于该日期的 backtest rows。Dashboard view-model 与 legacy
fallback 都调用该函数，不再依 task type 放宽日频或周频的切断规则。

数据库、Dashboard API 契约、算法和调度均不在本次范围内。保留 legacy fallback
本身的兼容接口，只让它使用与 Dashboard 相同的前端展示规则。

## 验收

- 针对 daily / `T+1` Dashboard fixture：live 起点当天的回测行必须被移除；
  同日 live 行保留。
- 聚合后不再有同一 `target_date` 的 backtest 与 live 双计数。
- 该 fixture 的页面月度结果在 live 起点为月初时仅显示 live 月度行。
- Dashboard 与 legacy fallback 对相同的 source rows 产出相同的 cutover 结果。
- 现有前端测试继续通过；不修改业务库、API、算法或 launchd 配置。

## 批准

用户于 2026-08-05 指示“修正”，批准上述最小范围。
