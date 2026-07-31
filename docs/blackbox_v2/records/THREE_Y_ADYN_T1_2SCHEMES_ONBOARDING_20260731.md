# 3Y ADYN T+1 双方案生产入库记录

**文档状态**：`CURRENT`

**目标读者**：平台入库、生产授权、日度运维和审计人员

**最终核验时间**：2026-07-31 13:14:03，`Asia/Shanghai`

**机器证据**：
[THREE_Y_ADYN_T1_2SCHEMES_ONBOARDING_20260731.evidence.json](THREE_Y_ADYN_T1_2SCHEMES_ONBOARDING_20260731.evidence.json)

## 当前结论

```text
PR_21_MERGED_IN_INTEGRATION: TRUE
TECHNICAL_GATES: PASS 14/14
CONTROL_PLANE_PERSISTED: TRUE
ALGORITHM_LOGIC_REVIEWED: FALSE
CONFIG_VERSION_REGISTRY: active
HISTORY_PER_SCHEME: 337 predictions + 17 monthly metrics
GRAY_LIVE_PER_SCHEME: 43 unique predictions
DAILY_GRAY_LAUNCHD: LOADED
DAILY_GRAY_CANARY: PASS 2/2
NATURAL_LAUNCHD_OCCURRENCE: NOT_OBSERVED
SCHEDULED_LIVE: 0
```

PR #21 的两个 Blackbox V2 日频方案已经完成七 Gate、生产激活、历史回测、
2026 年 6–7 月灰度入库、actual、API 和公网读回，并进入机器上已有的
`com.bond-factor-lab.daily-gray` 每日执行集合：

- `three_y_adyn_lb1_k3_v1@98233f0cb9ef`
- `three_y_adyn_lb2_k1_v1@47c7c1776db0`

平台没有评审、反编译或修改上游算法模型逻辑。效果数字只用于核对写库和展示
口径，不构成算法效果验收。

## PR 与交付身份

PR #21 head 为 `a8b9ec42fb336e8f5270fb341e3850915e5faa9d`。PR 分支基于较早
开发提交，平台使用三方合并保留当前分支已有五个周平均方案和双方 admission
追加，没有覆盖当前开发历史。

生产库合并前不存在这两个 base identity 的 Registry、版本、Harness、回测或
实盘记录，因此没有沿用 PR 描述中的测试机器状态。两个 config 先恢复为
`paused + draft`，再按 `draft → shadow → active` 完成首次生产生命周期。
delivery 两文件保持 PR 原字节。

| 方案 | Composite Registry | Python SHA256 | Metadata SHA256 |
|---|---|---|---|
| `three_y_adyn_lb1_k3_v1@98233f0cb9ef` | `three_y_adyn_lb1_k3_v1__h1__3Y` | `9e56d25732ca089f29937aff6a12d1736fccf7e61f72e48c49a621edf1a962e6` | `2668dbc9402e824a55e546625b6ca3e9fdc5bc50f2b5e5fd08787f949cd02fda` |
| `three_y_adyn_lb2_k1_v1@47c7c1776db0` | `three_y_adyn_lb2_k1_v1__h1__3Y` | `aa29e97b4134eceb301a8acc61ea06230c0b2d6c4179772282a0e6c6c4fc3ada` | `056e0aabaafdb3e39b28cf7ae0668982995a71d33f2a31e1bac0a0f92a9da3fd` |

两个 composite 均为 `3Y / T+1 / daily / horizon=1`，Registry、版本和 config
最终均为 `active`，`deployed_at=2026-07-31`，生产批准人为 `lucky1day`。

## 统一输入、Gate 与生命周期

| 项目 | 结果 |
|---|---|
| Environment fingerprint | `720ad40ab77cd6c7156ff35a80cf3604ac3a6153425ed235a4e3158b0631f8bd` |
| DataBridge generation | `full-20260730-081804-9794ce962c1a` |
| Data snapshot | `snapshot-a0dbf1774782db2e6d2a1ec5` |
| Daily / weekly / monthly cutoff | `2026-07-29 / 202629 / 202701` |
| `lb1_k3` persisted Harness | `hr_20260731T035836Z_6d0a86910541`，7/7 PASS |
| `lb2_k1` persisted Harness | `hr_20260731T035836Z_7e0416f37965`，7/7 PASS |

环境与 sandbox 探针通过，算法运行时网络访问和数据目录写入均被拒绝。两个
check-only all-stage 和两个 persisted all-stage 全部 7/7；持久化 Harness
只写控制面，不写业务预测或回测表。

首次并行 `draft-register` 时，`lb2_k1` 在两个缺失 identity 同时插入时遭遇
MySQL 1213 deadlock；该事务已回滚，业务表增量为 0，也没有 reconciliation
要求。只读核对确认 `lb1_k3` 已提交、`lb2_k1` 仍不存在后，为 `lb2_k1` 签发
新的 exact token 串行重试成功。此后所有生命周期写入均串行执行。该事件没有
修改算法、没有直接 SQL 补写，也没有留下未完成的生产状态。

## 历史回测

| 方案 | Run | Feature / predict | Target | Prediction | 月度指标 | 指标 |
|---|---:|---|---|---:|---:|---|
| `lb1_k3` | `201` | `2025-01-02..2026-05-28` | `2025-01-03..2026-05-29` | 337 | 17 | 129/240，53.8% |
| `lb2_k1` | `202` | `2025-01-02..2026-05-28` | `2025-01-03..2026-05-29` | 337 | 17 | 122/220，55.5% |

两项历史构造起点均为 `2025-01-01`，有效首日由平台交易日历决定。所有历史
`target_date` 严格小于灰度起点 `2026-06-01`；replay 语义为
`current_snapshot_as_of_not_historical_vintage`。实际方向为 0 的记录保留，
指标聚合只剔除预测方向为 0 的记录。

## 灰度实盘

两个方案均覆盖平台交易日历中的 43 个目标日：

| 日期字段 | 范围 |
|---|---|
| `predict_date` | `2026-06-01..2026-07-30` |
| `feature_date` | `2026-05-29..2026-07-29` |
| `target_date` | `2026-06-01..2026-07-30` |

每项均为 43 个成功 run、43 条 prediction 和 43 条 run log，三层一一对应；
交易日历应有 43 天、实际覆盖 43 天、缺口为 0。历史与灰度 target 重叠为 0，
数据库中本批 `scheduled_live=0`。

| 方案 | 方向分布（下/平/上） | 实际分布（下/平/上） | 非零预测计分 | 当前灰度指标 |
|---|---|---|---:|---|
| `lb1_k3` | `18 / 15 / 10` | `19 / 4 / 20` | 28 | 16/28，57.1% |
| `lb2_k1` | `16 / 17 / 10` | `19 / 4 / 20` | 26 | 15/26，57.7% |

`daily_actuals_updater` 对 `2026-06-01..2026-07-30 / 3Y` 幂等刷新 43 条。
实际方向为 0 的 4 个目标仍保留；仅预测为 0 的样本不进入准确率分母。

## API、前端数据与公网

- 本机 `/api/schemes` 和公网同一路由均返回两个 active composite；
- 两个 metrics endpoint 各返回 43 条 `gray_live` 和完整 phase range；
- `/api/backtests/factor-lab` 各返回 latest run 的 337 条历史和 17 个月度指标；
- dashboard 各返回 `337 backtest + 43 gray_live`，3Y/T+1 格子现有 2 个候选；
- 公网 `https://bond.finailab.cn/bond-factor-lab/api/health` 返回 HTTP 200，
  dashboard snapshot 为 `ready`。

公网 health 中 `daily_schedule.mode=legacy` 且 ledger 为 `not_enabled`，这是
29/29 正式调度控制面的预期状态，不代表独立 daily-gray 未加载。

## launchd 与 29/29 边界

仓库模板和已安装
`/Users/macstudio0/Library/LaunchAgents/com.bond-factor-lab.daily-gray.plist`
SHA256 均为
`95717e18c341e643e12ae00c8e603369ca84e2d54e6308f1e01633cef398dcf4`。
launchctl 已加载唯一任务：

```text
label: com.bond-factor-lab.daily-gray
command: python -m scheduler.daily_gray_runner
calendar: every day 07:00
phase: gray_live
discovery: status=active + frequency=daily
RunAtLoad: false
```

受控执行
`--only three_y_adyn_lb2_k1_v1,three_y_adyn_lb1_k3_v1 --predict-date 2026-07-30`
得到 `total=2 / success=2 / failed=0 / records_written=2`，证明两个方案可被
LaunchAgent 的同一路径发现和执行。

最终核验时 LaunchAgent 为 `loaded / state=not running / runs=0`。这是日历型任务
等待下次 07:00 的正常状态，记录为 `MOUNTED_NOT_OBSERVED`。当前 2026-07-31
DataBridge 尚无满足当日运行的完整 current，故没有用 `kickstart` 立即触发全部
active daily，避免制造预期失败或把人工触发误写成自然首跑。

两个方案 exact admission 均为 `mode=gray + capabilities=[]`，不进入
`legacy_automatic`、`daily_ledger` 或 `direct_scheduled`。本次没有修改
`deploy/daily_scheduler_policy_v2.json`、29/29 ledger、occurrence 或 receipt；
未来每日 07:00 仍由独立 daily-gray 写 `gray_live`。
