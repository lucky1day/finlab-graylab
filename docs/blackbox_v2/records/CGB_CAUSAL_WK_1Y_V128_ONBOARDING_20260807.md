# cgb_causal_wk_1y_v128 入库与单快照批量回补记录（2026-08-07）

**文档状态**：`HISTORICAL`

**适用运行时**：`blackbox_v2`

**目标读者**：平台入库、审计和生产灰度人员

**最后核验日期**：2026-08-07

本记录固定本次当前精确版本的入库与回补证据，不定义通用接口或默认授权。此前
[`CGB_CAUSAL_WK_1Y_V128_ONBOARDING_20260806.md`](CGB_CAUSAL_WK_1Y_V128_ONBOARDING_20260806.md)
所记 `ee921f65476c` 是已保留的历史前身；其 Gate、回测和灰度证据不复用于本记录的
精确版本。

## 1. 当前身份

| 项 | 值 |
|---|---|
| `scheme_id` | `cgb_causal_wk_1y_v128` |
| composite Registry ID | `cgb_causal_wk_1y_v128__h1__1Y` |
| `scheme_version` | `59415aa789c5` |
| `runtime_type` | `blackbox_v2` |
| `task_type` / `frequency` / `horizon` / `target_tenor` | `weekly_point` / `weekly` / `1` / `1Y` |
| Registry / config 生命周期 | `active` / `active` |
| DataBridge generation | `full-20260806-063108-e08812802aff` |
| DataBridge `refresh_date` | `2026-08-06` |

## 2. 准入、激活与历史回测

| 项 | 结果 |
|---|---|
| 零写七段 Gate | `hr_20260806T170243Z_351367853dc3`，全部通过 |
| 持久化七段 Gate | `hr_20260806T172335Z_31865d2ed2f3`，全部通过；仅 Harness 控制面持久化 |
| 生命周期 | 在独立、按顺序授权下完成 `draft → shadow → active`；未触发 scheduler 或 launchd |
| 历史回测 | run `206`，一次 Contract `backtest` batch：72 requests / 72 predictions / 17 monthly metrics |
| 历史预测区间 | `predict_date=feature_date` 为 `2025-01-03` 至 `2026-05-22` |
| 历史目标区间 | `target_date` 为 `2025-01-10` 至 `2026-05-29`；`target_date >= 2026-06-01` 为 0 |
| 历史输入与语义 | `snapshot-4a26408c4aa671641aca361c`；`current_snapshot_as_of_not_historical_vintage` |

该历史 run 的汇总为 51/72 正确（70.8%）。这是当前快照 as-of 的复现统计，不能表述为
逐历史时点的 data vintage 回放。

## 3. 单快照灰度信号补齐

本次写入只覆盖方案范围已冻结的 CGB 周度缺口，而不是将全局 `weekly_point` 计划中的
无关方案 blocker 人为删去。冻结计划为 `active-signal-gap-plan-v4`，选择范围是：

```json
{
  "target_date_start": "2026-06-01",
  "target_date_end": "2026-08-07",
  "task_types": ["weekly_point"],
  "base_scheme_ids": ["cgb_causal_wk_1y_v128"]
}
```

初始计划 SHA-256 为
`02f74adaea902029931c71f3a9bf00fd0180c739b0835dfcfc099d20d6b5c5e0`：9 个
`GRAY_LIVE_GAP`、0 个 blocker、0 个跨方案 action。每个 predict-date group 的一次性授权
均绑定该 SHA、精确版本、对应 target key 与 source authority；随后通过正常
`signal-gap-fill` Gate 一次性执行，未使用直接 SQL、逐周重新读取 DataBridge、手工 runner、
scheduler 或 launchd。

| 项 | 结果 |
|---|---|
| 写入 run | `2193`–`2201`，九个均为 `success`，每个 `expected=returned=written=1` |
| 灰度记录 | 9 条，全部 `prediction_phase=gray_live`、版本 `59415aa789c5` |
| 日期范围 | `predict_date` `2026-06-06`–`2026-08-01`；`feature_date` `2026-06-05`–`2026-07-31`；`target_date` `2026-06-12`–`2026-08-07` |
| 8 月 1 日语义 | 非交易日 `predict_date=2026-08-01` 映射为 `feature_date=2026-07-31`，目标为 `2026-08-07` |
| 共享写入 snapshot | `snapshot-0a19f001bbc2fe2f4ce85e99` |
| 共享回放 session | `2b518a7a840d88380e822c5463866eb21caf8585b3354e4e15e5af625f1e0072` |
| session manifest SHA-256 | `86d156969c144881e509dca4d0ad0e1d1e0d604b6c803be347d5c879a5cd178e` |
| session 父快照 | `snapshot-817c9e04c1c521888d171bfd` |
| 物理最高截止 | 日频 `2026-07-31`、周频 `202629`、月频 `202608` |

这是一份冻结的 DataBridge base snapshot、一个 immutable session manifest 和一次 CGB
批量 delivery。每个请求仍在 session 内按自身日/周/月 cutoff 截断；共享的是源快照和
批量计算，不是把所有周点错误地套用同一周末数据。

写后重新生成同一 CGB-only 范围计划，SHA-256 为
`199b7a04338d25dcd11f7f4b40e8a394999fa21f2e49417fd116a1781d2e335d`，结果为 9 个
`SKIP_PRESENT`、0 个 open gray gap、0 个 blocker 和 0 个 anomaly。

## 4. 对外读回与回归

- 本地已服务的 `/api/factor-lab/dashboard` 返回 HTTP 200、`stale=false`；active composite
  方案返回上述 9 条 `live_rows` 和 72 条历史 backtest rows，最新灰度行的 actual 仍为 pending。
- 服务环境的共享快照、执行器、信号补齐、CGB 与 Contract 聚焦套件通过
  `184 passed, 94 subtests passed`；CGB delivery 在实际 Blackbox Runtime Profile 中亦通过
  6 个测试。
- 计划 v4 将 `base_scheme_ids` 纳入 canonical scope 与 SHA，并在生成/回放阶段对 unknown 或
  非 active 身份 fail-closed；它隔离无关 blocker，但不改变全局 Registry digest 或任何其它方案。

## 5. 本记录不授予什么

- 不授予 scheduler admission、自动 `scheduled_live` 或新的自然时钟运行权限；本记录中的
  九条全部是受控 `gray_live`，没有 `scheduled_live`。
- 不安装、修改或重启 launchd/plist，也不改变现有调度控制面。
- 不外推为其他方案、其他版本、其他时间范围或未来 DataBridge generation 的授权。
- 不改变 Native V1、Registry 生命周期规则、历史回测灰度边界或 Blackbox 通用生产晋级条件。
