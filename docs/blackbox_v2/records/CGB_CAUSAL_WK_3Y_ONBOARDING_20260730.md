# cgb_causal_wk_3y 周度方案生产入库记录

**文档状态**：`CURRENT`

**目标读者**：平台入库、生产授权和审计人员

**最后核验日期**：2026-07-30

**机器证据**：
[技术 Gate](CGB_CAUSAL_WK_3Y_ONBOARDING_20260730.evidence.json) /
[生产终验](CGB_CAUSAL_WK_3Y_PRODUCTION_ACCEPTANCE_20260730.evidence.json)

## 当前结论

```text
TECHNICAL_GATES: PASS 7/7
CONTROL_PLANE_PERSISTED: TRUE
ALGORITHM_LOGIC_REVIEWED: FALSE
CONFIG: active + active
HISTORY: 72 predictions + 17 monthly metrics
GRAY_LIVE: 9 predictions
PRODUCTION_ACTIVATED: TRUE
SCHEDULER_MOUNTED: FALSE
```

`cgb_causal_wk_3y@4b8db29b2f74` 已按 Blackbox V2 两文件契约完成生产
激活、历史回测、灰度写入和前端读回。平台保留上游算法脚本原始字节，只验证
输入、Contract、确定性、截止隔离、标准输出、日期身份和写库结果；没有评审、
反编译或修改算法内部模型逻辑。

## 身份与输入

| 项目 | 结果 |
|---|---|
| Registry ID | `cgb_causal_wk_3y__h1__3Y` |
| 任务 | `3Y / weekly_point / horizon=1` |
| Scheme version | `4b8db29b2f74` |
| Algorithm version | `2.0.0-rc1` |
| Python SHA256 | `849c2fe6e24343230ddcc17212979f981144cb03f44d0ff9b1457f96fa7d0383` |
| Metadata SHA256 | `84b09972a583314c401228957a83d9195821ed0fbab93040cfb76e2e3c4bbb38` |
| Config hash | `56fb607b68780989fc26f41b8d27bad67171394bc9a5a6bf5844788ea345902b` |
| DataBridge generation | `full-20260730-081804-9794ce962c1a` |
| Combined snapshot | `snapshot-2c964086367c6a987f193bcd` |
| 平台周历 | `api-wind-date-v1`，6064 行，SHA256 `82b32635a1b94d414fdbdcb6391210729aadd7dbb86b9c44bc692ab77bd6da91` |
| Gate Request | `predict=2026-07-25 / feature=2026-07-24 / target=2026-07-31` |

DataBridge check-only 已验证当前日频截至 `2026-07-29`，三频文件属于同一
generation。算法依赖的 `week_id` 和交易日映射由平台
`api-wind-date-v1` 注入，未使用随包 sample 日历。

## Gate 与生产授权

无副作用 all-stage
`hr_20260730T142045Z_9f6dbfc6db9d` 为 7/7 PASS，明确
`control_plane_persisted=false`、`business_tables_written=false`。

持久化 all-stage
`hr_20260730T142202Z_2a70ae7b8e8e` 同样为 7/7 PASS，并作为本方案
`draft-register → shadow-register → activate`、历史回测和每条灰度写入的
exact 授权依据。生产批准人为 `codex-cgb3y-production-20260730`。

技术回测为 100 requests / 100 records；重复执行、predict/backtest、
分批、逆序和未来行隔离均一致。Sandbox 验证网络访问和输入目录写入均被拒绝。

## 历史与灰度终态

历史回测 run `194`：

- 从 `2025-01-01` 起构造，实际站位日为
  `2025-01-03..2026-05-22`；
- 目标日为 `2025-01-10..2026-05-29`，严格满足
  `target_date < 2026-06-01`；
- 写入 72 条 prediction 和 17 条月度指标；
- 当前口径为 37/72，准确率 51.4%；实际方向分布为
  上行 28、下行 41、持平 3，预测方向为 0 的样本为 0；
- `replay_semantics=current_snapshot_as_of_not_historical_vintage`。

灰度 run `1683..1691` 写入 9 条 `gray_live`：

| `predict_date` | `feature_date` | `target_date` | 方向 |
|---|---|---|---:|
| 2026-05-30 | 2026-05-29 | 2026-06-05 | +1 |
| 2026-06-06 | 2026-06-05 | 2026-06-12 | +1 |
| 2026-06-13 | 2026-06-12 | 2026-06-18 | -1 |
| 2026-06-20 | 2026-06-18 | 2026-06-26 | +1 |
| 2026-06-27 | 2026-06-26 | 2026-07-03 | +1 |
| 2026-07-04 | 2026-07-03 | 2026-07-10 | +1 |
| 2026-07-11 | 2026-07-10 | 2026-07-17 | -1 |
| 2026-07-18 | 2026-07-17 | 2026-07-24 | -1 |
| 2026-07-25 | 2026-07-24 | 2026-07-31 | -1 |

每条授权写入只增加 1 条本方案 prediction、1 条 run 和 1 条 run log，
其他受保护表增量均为 0。历史与灰度 target 零重叠。

## API、前端与调度边界

- `/api/schemes` 已返回 active composite
  `cgb_causal_wk_3y__h1__3Y`。
- `/api/metrics/cgb_causal_wk_3y__h1__3Y` 已返回 9 条
  `gray_live` 和完整 phase range。
- `/api/backtests/factor-lab` 已选择 run `194`；17 个月度单元与数据库
  逐格比对差异为 0。
- scheduler admission 固定为 `gray + capabilities=[]`；launchd scheduler
  未加载，数据库 `scheduled_live=0`。

配置中保留 cron 仅用于声明方案自然周期，不代表已挂载。首条未来自然周期仍为
`predict_date=2026-08-01 / feature_date=2026-07-31 /
target_date=2026-08-07`；只有以后明确恢复 scheduler 并观察到真实运行后，
才能写成 `scheduled_live`。

## 上游复现与 snapshot 边界

上游包在其自带 0725 sample snapshot 上通过 13/13 selfcheck，并与研究参考
方向 376/376 一致。该结论只属于上游 snapshot。

本次生产使用 0730 DataBridge generation。上游 sample 三频摘要与生产三频
摘要均不同，因此记录为 `data_vintage_mismatch`，不得把 376/376 直接解释为
生产 generation 上逐值一致。随包日历还有以下问题：

- 6057 行，原始 SHA256
  `8db357fa38ed7195c63896efdfc222e8cafea8458c074e1ff40b62097ba1848f`；
- 存在 4 处非严格升序；
- 缺少平台权威周 `202625`。

平台没有把该日历送入 provider，而是统一使用 6064 行的
`api-wind-date-v1`。因此端午周目标日为 `2026-06-18`，六月末和七月尾部
日期也以平台权威周历为准。

对外只能分别陈述：

1. 上游算法在 0725 sample snapshot 内复现一致；
2. 平台在 0730 production generation 内执行、隔离和落库一致。

若要宣称双方逐值完全一致，必须重新使用相同 DataBridge generation、相同
`api-wind-date-v1` 摘要，并统一按 `target_date` 分月和切分历史/灰度。
