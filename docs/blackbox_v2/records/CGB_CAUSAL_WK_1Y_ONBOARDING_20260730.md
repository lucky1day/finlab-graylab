# cgb_causal_wk_1y 周度方案技术入库记录

**文档状态**：`CURRENT`

**目标读者**：平台入库、生产授权和审计人员

**最后核验日期**：2026-07-30

**机器证据**：[CGB_CAUSAL_WK_1Y_PRODUCTION_ACCEPTANCE_20260730.evidence.json](CGB_CAUSAL_WK_1Y_PRODUCTION_ACCEPTANCE_20260730.evidence.json)

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

`cgb_causal_wk_1y@cba824c27f0e` 已完成新版替换、生产重新认证、历史回测和
灰度数据替换。平台只验证 Contract、确定性、截止隔离、标准输出、日期身份和
写库结果，没有评审或修改算法内部模型逻辑。

旧版本 `05022a0eeec7` 已退役；其历史 run `192`、历史 prediction/metric、
灰度 prediction/run/log 均已精确删除，旧版本业务数据计数为零。版本注册行保留
为 `retired`，用于审计，不作物理删除。

## 当前身份与输入

| 项目 | 结果 |
|---|---|
| Registry ID | `cgb_causal_wk_1y__h1__1Y` |
| 任务 | `1Y / weekly_point / horizon=1` |
| Scheme version | `cba824c27f0e` |
| Python SHA256 | `cf79ab53433ba63cb05ab23d5aacf7cc33e698d37f31cbbba0e81688ab646172` |
| Metadata SHA256 | `efc8e03c5db98c33f0b830d62cc4465f7f890b5a783de68fee58e4ae19d4162b` |
| generation | `full-20260730-081804-9794ce962c1a` |
| combined snapshot | `snapshot-2c964086367c6a987f193bcd` |
| 平台周历 | `api-wind-date-v1`，6064 行，SHA256 `82b32635a1b94d414fdbdcb6391210729aadd7dbb86b9c44bc692ab77bd6da91` |
| 最新 Gate Request | `predict=2026-07-25 / feature=2026-07-24 / target=2026-07-31` |

本次上游更新不改变模型逻辑，只把 `api_wind_date.csv` 改为必需输入，并删除
脚本内嵌周历及尾部推算。平台部署继续只使用同一 DataBridge generation 内的
权威 `api-wind-date-v1`。

首轮 check-only all-stage
`hr_20260730T122348Z_88f7b93b73d4` 为 7/7 PASS，未持久化控制面；
随后可持久化 all-stage
`hr_20260730T122450Z_bd94b8af0876` 为 7/7 PASS，并作为新版生产授权依据。
技术回测为 100 requests / 100 records、分批与逆序一致。

## 生产数据终态

- Registry `cgb_causal_wk_1y__h1__1Y` 和新版本 `cba824c27f0e`
  均为 `active`。
- 新历史 run `193` 写入 72 条 prediction 和 17 条月度指标；站位日
  `2025-01-03..2026-05-22`，目标日 `2025-01-10..2026-05-29`。
- 新灰度 run `1674..1682` 写入 9 条 `gray_live`；目标日
  `2026-06-05..2026-07-31`。
- 新旧版本在共同历史 72 条和灰度 9 条上的日期、方向均完全一致，因此替换
  没有改变已经上线的信号。
- 前端当前读取 run `193`；17 个指标单元与数据库逐格一致，差异为零。
- scheduler admission 保持 `gray + capabilities=[]`，按用户要求暂不自动挂载；
  当前 `scheduled_live=0`。

历史回测从 `2025-01-01` 起构造，但只持久化
`target_date < 2026-06-01`；`target_date >= 2026-06-01` 的观察区进入
`gray_live`。首条未来自然周期仍定义为
`2026-08-01 / 2026-07-31 / 2026-08-07`，只有以后明确恢复 scheduler 且真实
时钟成功运行后，才可标记为 `scheduled_live`。

## 最新复现对齐核查

```text
SCHEME_ID: cgb_causal_wk_1y
TARGET_TENOR: 1Y
FREQUENCY: weekly
TASK_TYPE: weekly_point（周度单点/周收）
HORIZON: 1 个后续业务周
UPSTREAM_SAMPLE_SNAPSHOT: 0725 sample_data
PLATFORM_SNAPSHOT: full-20260730-081804-9794ce962c1a
RECONCILIATION_STATUS: PARTIAL_SNAPSHOT
```

`PARTIAL_SNAPSHOT` 只表示上游报告附带的静态 sample snapshot 与生产
DataBridge generation 不是同一个 vintage，不表示交付脚本、算法复现或生产
运行失败。

最新版上游报告已经明确：交付脚本在其自身 0725 sample snapshot 上与研究基准
逐周方向 `378/378` 一致。平台另外用相同新版交付脚本和生产 0730 generation
生成 run `193`。这两项验收不能混写成同一个 snapshot 下的逐值完全一致。

| 核查项 | 最新结果 | 结论 |
|---|---|---|
| 日期与实际标签 | 72/72 对齐 | `feature_date`、`target_date` 和实际方向逐条一致 |
| 预测方向 | 70/72 对齐 | 仍只有 `202538`、`202603` 两周不同；上游 0725 sample 均为 `-1`，生产 0730 generation 均为 `+1` |
| 新版脚本 I/O | 已对齐 | 双方均由外部 `api_wind_date.csv` 提供周历，不再使用内嵌周历或尾部推算 |
| 输入 vintage | 未对齐 | 上游报告固定 0725 sample；生产固定 0730 generation。此前逐格核查发现生产代在相关日频 `M0000271`、`DR007IBC` 上有 125 个值/空值修订 |
| 月份归属 | 仍未对齐 | 上游准确率工具按当前 `week_id` 的周末、等价于 `feature_date` 月份统计；平台按 `target_date` 月份统计 |
| 历史/灰度分区 | 仍未对齐 | 平台按 `target_date >= 2026-06-01` 进入 `gray_live`；例如 `feature_date=2026-05-29`、`target_date=2026-06-05` 在平台属于 6 月灰度 |
| 持平样本 | **已对齐** | 最新报告和平台都保留 `actual_direction=0`，只剔除预测为 0；历史 72 条中实际持平 6 条、预测为 0 有 0 条、正确 37 条，统一为 `37/72` |
| 历史 `predict_date` | 字段语义不同 | 上游 Request 取下一周首个交易日；平台历史回测按规范保存 `predict_date=feature_date`。算法 cutoff 相同，因此不导致翻号 |
| 尾部业务周历 | 生产周历优先 | 上游 sample 周历为 6057 行且缺少 `202625`；生产权威周历为 6064 行并包含该周，六月末和七月尾部必须按生产周历 |

两条预测差异的逐行证据如下：

| `week_id` | `feature_date` | `target_date` | 上游 0725 | 生产 0730 | 实际方向 |
|---|---|---|---:|---:|---:|
| `202538` | `2025-09-19` | `2025-09-26` | `-1` | `+1` | `-1` |
| `202603` | `2026-01-23` | `2026-01-30` | `-1` | `+1` | `+1` |

因此，当前“不一致”已经收敛为三类可解释差异：

1. 上游 sample 与生产 generation 的历史数据修订；
2. 上游报告按 `feature_date`、平台按 `target_date` 的月度归属；
3. 历史 `predict_date` 的保存语义和尾部周历覆盖差异。

实际方向为 0 的分母口径不再是不一致项。对外报告若要宣称与生产逐值完全一致，
必须使用 DataBridge 下载并固定与平台相同的 generation、同一
`api-wind-date-v1` 摘要，并按平台的 `target_date` 口径重新分月及切分
历史/灰度；否则只能分别宣称“上游 snapshot 内算法复现一致”和“生产
generation 内平台执行一致”。
