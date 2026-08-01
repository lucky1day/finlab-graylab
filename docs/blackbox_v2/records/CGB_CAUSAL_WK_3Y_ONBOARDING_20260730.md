# cgb_causal_wk_3y 周度方案生产入库记录

**文档状态**：`HISTORICAL`

**目标读者**：平台入库、生产授权和审计人员

**最后核验日期**：2026-07-30

**机器证据**：
[技术 Gate](CGB_CAUSAL_WK_3Y_ONBOARDING_20260730.evidence.json) /
[生产终验](CGB_CAUSAL_WK_3Y_PRODUCTION_ACCEPTANCE_20260730.evidence.json)

**统一问题记录**：
[ISSUE-20260730-003](../../records/SCHEME_ISSUE_LEDGER.md#issue-20260730-003cgb_causal_wk_3y-上游与生产复现差异)

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

```text
RECONCILIATION_STATUS: PARTIAL_SNAPSHOT_ALIGNMENT
UPSTREAM_SAME_SNAPSHOT: 376/376
HISTORY_DATE_ALIGNMENT: 72/72
HISTORY_ACTUAL_ALIGNMENT: 72/72
HISTORY_PREDICTION_ALIGNMENT: 69/72
GRAY_STRICT_PREDICTION_ALIGNMENT: 6/7
ACTUAL_ZERO_POLICY: ALIGNED
```

上游包在其自带 0725 sample snapshot 上通过 13/13 selfcheck，并与研究参考
方向 376/376 一致。该结论只属于上游 snapshot。

本次生产使用 0730 DataBridge generation。上游 sample 三频摘要与生产三频
摘要均不同：

| 输入 | 上游 0725 sample SHA256 | 平台 0730 generation SHA256 |
|---|---|---|
| daily | `0ffbf694d15bcdb585cfb46a51835bca2e50b5b13292430cf20ec8890402cec3` | `e9969d55626aa31ef6c2e069dce04c586044612f3ab33eb2c98169ea03e94bdf` |
| weekly | `b38e731c3d295a46d54b8228bdefc68d577b1bd7639d3e047bb0f2d36689c324` | `396e44008f45d0aed286d60ad90b1534e13ce3ea9759130e7e0145d17d727d8c` |
| monthly | `9d8d754d7dcde97e531d9d4b2f4dafc4b0ead7b2aba0bb49d57334e025c98592` | `9d028233894e764aeb871bc34229a3159033c7c90e83272c605547f3c3d61aa1` |
| calendar | `8db357fa38ed7195c63896efdfc222e8cafea8458c074e1ff40b62097ba1848f` | `82b32635a1b94d414fdbdcb6391210729aadd7dbb86b9c44bc692ab77bd6da91` |

因此记录为 `data_vintage_mismatch`，不得把 376/376 直接解释为生产
generation 上逐值一致。随包日历还有以下问题：

- 6057 行，原始 SHA256
  `8db357fa38ed7195c63896efdfc222e8cafea8458c074e1ff40b62097ba1848f`；
- 存在 4 处非严格升序；
- 缺少平台权威周 `202625`。

平台没有把该日历送入 provider，而是统一使用 6064 行的
`api-wind-date-v1`。因此端午周目标日为 `2026-06-18`，六月末和七月尾部
日期也以平台权威周历为准。

### 历史结果逐条核对

以 `feature_date + target_date` 对齐上游
`accuracy_cgb_causal_wk_3y.csv` 与生产历史 run `194`：

- 平台 72 条历史记录全部找到同窗口上游记录，实际方向 72/72 一致；
- 预测方向 69/72 一致，3 条差异如下；
- 上游预测在平台相同 72 条边界上为 36/72、50.0%，平台为
  37/72、51.4%。净增 1 条正确来自下表三处方向变化；
- 这 3 条差异与输入 snapshot 已确认不同相吻合，也符合上游报告所述模型对
  滚动窗口、衰减和低置信度翻转敏感的特征；平台标准输出未保存内部概率，
  因此不能进一步断言某一条一定由低置信度门触发。

| `week_id` | `feature_date` | `target_date` | 上游方向 | 平台方向 | 实际方向 | 正确性变化 |
|---|---|---|---:|---:|---:|---|
| `202512` | 2025-03-21 | 2025-03-28 | -1 | +1 | -1 | 正确 → 错误 |
| `202514` | 2025-04-03 | 2025-04-11 | +1 | -1 | -1 | 错误 → 正确 |
| `202527` | 2025-07-04 | 2025-07-11 | -1 | +1 | +1 | 错误 → 正确 |

上游报告的 `2025-01..2026-06` 汇总为 40/76、52.63%，其中月份字段按
`feature_date` 归属。平台按 `target_date < 2026-06-01` 切历史，所以上游
这 76 条中的以下 4 条进入平台灰度而不进入历史：

| `feature_date` | `target_date` |
|---|---|
| 2026-05-29 | 2026-06-05 |
| 2026-06-05 | 2026-06-12 |
| 2026-06-12 | 2026-06-18 |
| 2026-06-18 | 2026-06-26 |

这 4 条在上游均预测正确。因此先统一平台边界，上游口径自然从 40/76 变为
36/72；再叠加上述 3 条 snapshot 方向变化，平台结果为 37/72。该差异不是
漏数或实际方向过滤造成的。

平台月度统计按 `target_date` 归属。把上游 72 条也改按 `target_date` 分月
后，只有受上述 3 条方向变化影响的月份不同：

| 目标月 | 上游同边界 | 平台 |
|---|---:|---:|
| 2025-03 | 2/4，50.0% | 1/4，25.0% |
| 2025-04 | 3/5，60.0% | 4/5，80.0% |
| 2025-07 | 2/4，50.0% | 3/4，75.0% |

其余目标月逐月一致。若直接拿上游报告的 `feature_date` 月份与前端
`target_date` 月份比较，会看到更多表面差异，那属于归属口径未对齐。
历史回测的 `predict_date` 也有展示语义差异：上游 CSV 写下一周首个交易日，
平台历史按规范持久化为 `predict_date=feature_date`；两边实际采用的
`feature_date/target_date` 预测窗口仍然一致。

### 灰度结果核对

上游 0725 sample 与平台灰度中，严格相同 `feature_date + target_date` 的
记录共有 7 条，方向 6/7 一致；双方都有实际标签的 6 条，实际方向 6/6
一致。唯一方向差异为：

| `week_id` | `feature_date` | `target_date` | 上游方向 | 平台方向 | 实际方向 |
|---|---|---|---:|---:|---:|
| `202626` | 2026-07-03 | 2026-07-10 | -1 | +1 | +1 |

尾部三处必须单独解释：

- `202624` 的方向双方均为 +1。上游随包日历缺 `202625`，所以报告中实际
  标签为空；平台权威日历可完整计算，实际方向为 0；
- `202628` 上游 snapshot 只到 2026-07-20，目标日为 2026-07-20；平台使用
  完整周目标日 2026-07-24。两条不是同一目标窗口，不能纳入严格逐值比较；
- 平台 `feature_date=2026-07-24 / target_date=2026-07-31` 的灰度记录在
  上游 0725 snapshot 中没有可比输出，且截至 2026-07-30 实际值待生成。

平台截至 2026-07-24 的灰度已有 8 条实际，其中 7 条正确，准确率 87.5%；
分母包含 `202624` 的实际方向 0。双方规则一致：仅剔除
`predicted_direction=0`，不得剔除 `actual_direction=0`。本方案当前预测只
产生 ±1，因此没有预测为 0 的待剔除样本。

对外只能分别陈述：

1. 上游算法在 0725 sample snapshot 内复现一致；
2. 平台在 0730 production generation 内执行、隔离和落库一致。

当前核对结论是“接口和同 snapshot 复现一致，生产 snapshot 部分一致”，
不是“生产逐值完全一致”。若要宣称双方逐值完全一致，必须重新使用相同
DataBridge generation、相同 `api-wind-date-v1` 摘要，并统一按
`target_date` 分月和切分历史/灰度。
