# cgb_causal_wk_1y_v128 入库记录（2026-08-06）

**文档状态**：`HISTORICAL`

**适用运行时**：`blackbox_v2`

**目标读者**：平台入库、审计和生产灰度人员

**最后核验日期**：2026-08-06

本记录提供时点证据，不定义通用接口或默认授权。

## 1. 身份

| 项 | 值 |
|---|---|
| `scheme_id` | `cgb_causal_wk_1y_v128` |
| composite Registry ID | `cgb_causal_wk_1y_v128__h1__1Y` |
| `scheme_version` | `ee921f65476c` |
| `task_type` / `horizon` / `target_tenor` | `weekly_point` / `1` / `1Y` |
| `target_rule` | `target_week_end_yield_vs_feature_week_end_yield` |
| `algorithm_version` | `1.28.0-predict-only-1` |
| `platform_inputs` | `api-wind-date-v1` |

同族已有 `cgb_causal_wk_1y` 与 `cgb_causal_wk_3y`；本方案是同族新算法版本，按
[入库导航](../../onboarding/README.md)作为独立 Blackbox V2 trial 入库，未覆盖既有身份。

## 2. 交付适配

上游只提供一个研究 runner 脚本（SHA256
`9302d502b029329b00825589b93401657b1f28d591a175655821a6d706d9f1d7`），不含 Contract 1.0
CLI。平台按[上游交付 SOP](../../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md) 补齐交付件，
共三处适配，均不改算法逻辑：

**（1）去除 `exec`/`compile` 组件 bundler。** 上游脚本把 20 个组件源码存成字符串再
`compile`+`exec` 进模块命名空间，命中 StaticGate `FORBIDDEN_CALLS`。改为每个组件一个
闭包命名空间函数。1239 行组件源码只改动 34 行——20 行 `from __future__` 与 14 处跨组件
`import` 改写为命名空间引用，其余逐字节不变。等价性验证：同一输入下原版与改造版的
逐周预测表 381 行 × 10 列逐格完全相同（含 NaN 位置），`latest_prediction` 字节一致。
不能扁平内联：20 个组件间有 29 个重名顶层符号，其中 `rolling_z`（4 种）、`make_labels`、
`primary_score`、`walk_forward_model` 实现互不相同。

**（2）去除 `api_wind_indicators_all.csv` 依赖。** 该文件在冻结组件内仅被两处
`validate_raw_inputs` 各引用一次，且只读 `.columns` 做列存在性校验，不消费任何值。
平台 `--data-dir` 不提供该制品，故以等值空表满足冻结签名，对算法数值零影响；未新增
platform input。

**（3）批量回测使用等价的一次性计算。** 按 §6.3 允许的模式实现：批内 ≥6 条时在批内
最大截止键上计算一次，按 `week_id` 取每条结果；同时对批内首条、中间条、末条各做一次
独立截断复算，逐字段比对 `week_id / pred_label / prob_up / center_pred_label /
center_prob_up`；任一不一致整批回退到逐条独立截断并写 `stderr`。抽样确定性选取。
地面真值验证：10 条 Request 的批量结果与 10 次单点 `predict`（始终独立截断）逐条一致。

交付件 SHA256：`.py` `9e8925cc7358c946f3b054f0701f36a09d5e983cb9543e09c98e0052476f5197`，
`.json` `e7df23572bb2d2d1275a9187368a33c6b4687c3abc0c99737a356f0163addda5`。

## 3. 输入身份

| 项 | 值 |
|---|---|
| `generation_id` | `full-20260728-152206-d52ecec59422` |
| `refresh_date` | `2026-07-28` |
| 组合 `snapshot_id` | `snapshot-7c47efbc0c37657948f61c17` |
| 父 `snapshot_id` | `snapshot-d4217c30b726df4331f2fc3e` |
| `api_wind_date.csv` 规范化 SHA256 | `82b32635a1b94d414fdbdcb6391210729aadd7dbb86b9c44bc692ab77bd6da91` |

算法消费的 80 个指标码在该 generation 的日频与周频文件中全部存在。

## 4. Gate 与登记

| 阶段 | 结果 |
|---|---|
| check-only 七 Gate | `overall_passed=true`，`hr_20260806T102442Z_d86ee5b70ece`，四个零写字段正确 |
| 持久化 all-stage | `overall_passed=true`，`hr_20260806T105354Z_4a560c36ca99`，`control_plane_persisted=true`、`business_tables_written=false` |
| 审计 DB 核验 | `t_harness_runs` 恰好一行 `all/passed`；`t_harness_gate_results` 恰好七个 Gate 全 `passed` |
| draft-register → shadow-register | 通过；config `paused/shadow`，composite Registry `paused`，业务表零新增 |

Backtest Gate 证据含 `requests=100 / records=100`，并以 `primary_batch_size=100` 与
`alternate_batch_size=80` 两种切分各跑一遍要求结果一致，共 3 个子进程。

## 5. 生产灰度

| 项 | 值 |
|---|---|
| Activation | config `active/active`，Registry `active`，`deployed_at=2026-08-06` |
| 持久化回测 | run `195`，72 条明细，17 条月度指标；`predict_date` `2025-01-03`~`2026-05-22`，`target_date` `2025-01-10`~`2026-05-29` |
| `target_date >= 2026-06-01` 的回测行 | `0` |
| gray_live 补齐 | 8 个目标周末（`2026-06-05` ~ `2026-07-24`），run / prediction / run_log 各 8 条一一对应 |
| 应有目标周末缺口 | `0` |
| backtest 与 live target 重叠 | `0` |

`gray_target_start=2026-06-01`；灰度上界取最后一个严格早于 `refresh_date` 的交易日
`2026-07-27`，因此周 `202629`（周末 `2026-07-31`）不在本次补齐范围内。

## 6. 效果（如实记录）

1Y 平台口径准确率 `48/72 = 66.7%`（`correct / metric_samples`）。分段：
`2025-01-03`~`2025-06-27` 为 `18/26 = 69.2%`；`2025-07-04`~`2026-04-30` 为
`28/43 = 65.1%`；`2026-05` 为 `2/3 = 66.7%`。

两点必须同时记录：预测方向分布为 down 50 / up 22，而实际为 down 39 / up 27，存在明显
偏空倾向；另有 6 个目标周实际为「平」，二分类预测器无法命中且计入分母，因此该样本上的
上限为 `66/72 = 91.7%`。72 个周样本的区间较宽，不足以支撑强结论。

## 7. 本记录不授予什么

- 不授予 scheduler admission。仓库 admission 已按精确身份 `cgb_causal_wk_1y_v128 +
  ee921f65476c` 登记为 `mode=gray` 且 `capabilities=[]`，`allows()` 恒为假，方案不进入
  `policy_v2`；该登记不安装 plist、不运行 `launchctl`、不重启服务。
- 不构成 `scheduled_live` 的自然时钟证据。本次全部实盘行均为 `gray_live`。
- 不外推为其它方案的授权，也不改变[生产晋级条件](../PRODUCTION_READINESS.md)的
  `BLOCKED_DRAFT` 状态。
