# one_y_t1_quote_state_hv_v1 生产入库记录

**文档状态**：`CURRENT`

**目标读者**：平台入库、生产授权、日度运维和审计人员

**最终核验时间**：2026-07-31 00:10:24，`Asia/Shanghai`

**机器证据**：
[ONE_Y_T1_QUOTE_STATE_HV_ONBOARDING_20260730.evidence.json](ONE_Y_T1_QUOTE_STATE_HV_ONBOARDING_20260730.evidence.json)

**效果问题诊断**：
[全方案问题台账 ISSUE-20260731-001](../../records/SCHEME_ISSUE_LEDGER.md#issue-20260731-001one_y_t1_quote_state_hv_v1-效果偏弱)

## 当前结论

```text
PR_19_MERGED: TRUE
TECHNICAL_GATES: PASS 7/7
CONTROL_PLANE_PERSISTED: TRUE
ALGORITHM_LOGIC_REVIEWED: FALSE
CONFIG_VERSION_REGISTRY: active
HISTORY: 337 predictions + 17 monthly metrics
GRAY_LIVE: 43 unique predictions
DAILY_GRAY_LAUNCHD: LOADED
DAILY_GRAY_CANARY: PASS
NATURAL_LAUNCHD_OCCURRENCE: NOT_OBSERVED
SCHEDULED_LIVE: 0
```

`one_y_t1_quote_state_hv_v1@d6d0cb43aacd` 已完成 PR 修复合入、Blackbox V2
七 Gate、生产激活、历史回测、2026 年 6–7 月灰度入库、actual/API/前端读回，
并进入机器上已有的 `com.bond-factor-lab.daily-gray` 每日执行集合。

平台没有评审、反编译或修改上游算法模型逻辑。所有效果数字只用于核对写库和
展示口径，不构成算法效果验收。

## PR 修复与交付身份

PR #19 包含一项平台修复和一组两文件算法交付：

- CompareGate 构造 prior request 时，将 `feature_date` 同步为 prior
  `daily_cutoff_key`；
- 交付文件保持原始字节：
  `one_y_t1_quote_state_hv_v1.py + one_y_t1_quote_state_hv_v1.json`。

修复按测试先行验证：未合入时针对性断言得到
`2026-07-15 != 2026-07-14` 的预期失败；合入后同一测试和完整 Gate 测试文件
通过。修复只令 prior comparison request 内部自洽，不改变当前 request、
权威截止键或算法逻辑。

| 项目 | 结果 |
|---|---|
| Registry ID | `one_y_t1_quote_state_hv_v1__h1__1Y` |
| 任务 | `1Y / T+1 / daily / horizon=1` |
| Scheme version | `d6d0cb43aacd` |
| Algorithm version | `1.0.0` |
| Python SHA256 | `a16899b1be1cacb867529485637f86c55c8f7a6b3d7565b04b1baf42775d2394` |
| Metadata / manifest SHA256 | `d34b6c6864bdfda7f2b50b39d1f4ce0f6659a8c237569e4cef2afae5d9f25318` |
| Registry / version / config | `active / active / active` |
| Deployed at | `2026-07-30` |
| Approved by | `codex-one-y-t1-production-20260730` |

## 统一输入与 Gate

| 项目 | 结果 |
|---|---|
| Environment fingerprint | `720ad40ab77cd6c7156ff35a80cf3604ac3a6153425ed235a4e3158b0631f8bd` |
| DataBridge generation | `full-20260730-081804-9794ce962c1a` |
| Data snapshot | `snapshot-a0dbf1774782db2e6d2a1ec5` |
| Daily SHA256 | `e9969d55626aa31ef6c2e069dce04c586044612f3ab33eb2c98169ea03e94bdf` |
| Weekly SHA256 | `396e44008f45d0aed286d60ad90b1534e13ce3ea9759130e7e0145d17d727d8c` |
| Monthly SHA256 | `9d028233894e764aeb871bc34229a3159033c7c90e83272c605547f3c3d61aa1` |
| Gate request | `predict=2026-07-30 / feature=2026-07-29 / target=2026-07-30` |
| Check-only Harness | `hr_20260730T153357Z_b4b42137022c`，7/7 PASS |
| Persisted Harness | `hr_20260730T153459Z_14bc241e6e40`，7/7 PASS |
| No-persist backtest | 100 requests / 100 records |

环境与 sandbox 探针通过：算法运行时网络访问和数据目录写入均被拒绝。Persisted
Harness run 及七个 Gate 均可从控制面审计表读回；生命周期按
`draft-register → shadow-register → activate` 完成。

## 历史回测

历史回测 run `195`：

- benchmark：
  `bbv2-one_y_t1_quote_state_hv_v1-hr_20260730T153459Z_14bc241e6e40`；
- 以 `2025-01-01` 为构造起点，实际 feature/predict 范围为
  `2025-01-02..2026-05-28`；
- target 范围为 `2025-01-03..2026-05-29`，全部严格小于灰度起点
  `2026-06-01`；
- 写入 337 条 prediction、17 条月度指标；
- 方向分布为下行 103、持平 146、上行 88；
- actual 方向为 0 的记录有 64 条，保留在样本中；
- 指标只排除预测为 0 的记录，`metric_samples=191`、`correct=78`、
  accuracy `40.8%`；
- replay 语义为 `current_snapshot_as_of_not_historical_vintage`。

## 灰度实盘

灰度写入覆盖 43 个唯一交易目标：

| 日期字段 | 范围 |
|---|---|
| `predict_date` | `2026-06-01..2026-07-30` |
| `feature_date` | `2026-05-29..2026-07-29` |
| `target_date` | `2026-06-01..2026-07-30` |

43 条预测方向分布为下行 10、持平 21、上行 12。当前 42 条已有 actual，
其中 actual=0 为 6 条；actual=0 不剔除。只对预测非 0 的 22 条计分，
11 条正确，accuracy 为 50.0%。`2026-07-30` 目标当前 actual 待源数据生成。

历史和灰度 target 重叠为 0。43 条业务 prediction 对应 44 条成功 run 和
44 条 run log：多出的 1 条是对同一 `2026-07-30` 目标执行 launchd 同路径
canary 时产生的幂等运行审计，prediction 仍由唯一键保持 1 条，并非重复业务
信号。

## API 与前端

- `/api/schemes` 返回 active composite；
- `/api/metrics/one_y_t1_quote_state_hv_v1__h1__1Y` 返回 43 条
  `gray_live` 和完整 phase range；
- `/api/backtests/factor-lab` 选择 run `195`，337 条历史和 17 个月度格；
- dashboard 返回 337 条历史 + 43 条 gray，其中 42 条已有 actual；
- 前端 1Y/T+1 格子从 1 个候选变为 2 个候选，新方案显示
  379 个已评估样本、89/213、41.8%，部署日期为 2026/07/30；
- 浏览器选中新方案后可见 19 个历史加灰度月份，控制台 error/warn 为 0。

前端明细里的“实盘预测目标区间”特指 `scheduled_live`，所以本方案当前显示
“待产生”；这不表示灰度未入库。`gray_live` 已进入排行、月度矩阵和 metrics
phase range。

## launchd 与 29/29 边界

机器已有且已加载：

```text
label: com.bond-factor-lab.daily-gray
command: python -m scheduler.daily_gray_runner
calendar: every day 07:00
phase: gray_live
discovery: status=active + frequency=daily
```

当前发现集为 26 个方案：17 Native + 9 Blackbox V2，包含本方案。受控执行
`--only one_y_t1_quote_state_hv_v1 --predict-date 2026-07-30` 成功，证明与
launchd 完全同路径可发现和执行。

LaunchAgent 安装时已错过 2026-07-30 07:00，因此最终核验时
`runs=0`、`last exit=(never exited)`。当前状态必须写成
`MOUNTED_NOT_OBSERVED`；下一个自然触发是 2026-07-31 07:00，只有真实发生后
才能追加自然运行证据。

本方案的 exact admission 为 `mode=gray + capabilities=[]`，明确不进入
`legacy_automatic`、`daily_ledger` 或 `direct_scheduled`。现有 daily-gray
LaunchAgent 不读取 29/29 ledger，所以可以每日写 `gray_live`；29/29 正式容量
账本、policy 和 occurrence 均未修改，数据库 `scheduled_live=0`。
