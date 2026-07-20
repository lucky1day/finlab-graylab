# 1Y T+5 四方案生产灰度记录

**文档状态**：`IN_PROGRESS`

**执行日期**：2026-07-20，`Asia/Shanghai`

**当前阶段**：四方案已完成 `shadow + paused` 技术入库；尚未激活、持久化回测或写入 gray live。

**机器证据**：[PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.evidence.json](PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.evidence.json)

本文记录本批四个真实 Blackbox V2 日频方案的生产技术入库与后续专项灰度。通用规则仍以平台 SOP 为准；本批用户授权只适用于本文列出的四个方案，不构成面向其他交付的通用生产授权。

## 1. 范围

| 业务名称 | base scheme ID | Registry ID | 当前版本 |
|---|---|---|---|
| `LIQ_EXCESS_A` | `one_y_t5_liq_excess_a_v1` | `one_y_t5_liq_excess_a_v1__h5__1Y` | `8d583560c9f1` |
| `LIQ_EXCESS_A_W252_L7` | `one_y_t5_liq_excess_a_w252_l7_v1` | `one_y_t5_liq_excess_a_w252_l7_v1__h5__1Y` | `103c93bbc913` |
| `LIQ_EXCESS_A_W350_L7` | `one_y_t5_liq_excess_a_w350_l7_v1` | `one_y_t5_liq_excess_a_w350_l7_v1__h5__1Y` | `86b458c568a5` |
| `LIQ_EXCESS_B_W252_L7` | `one_y_t5_liq_excess_b_w252_l7_v1` | `one_y_t5_liq_excess_b_w252_l7_v1__h5__1Y` | `ba00891cd179` |

明确排除且未执行 Intake：

- `one_y_t5_liq_excess_b_v1`
- `one_y_t5_liq_excess_b_w160_l3_v1`
- `one_y_t5_liq_excess_b_w200_l7_v1`
- `one_y_t5_liq_excess_b_w350_l7_v1`

## 2. 收包与 Intake

原始 ZIP SHA256 为 `3b08ddc0bccc54e7b275d864748058f3ec9bfceb361969049ec34ff011d9de5f`，CRC 完整性通过。平台在仓库外拆成四个独立两文件目录后逐方案执行 Intake，没有修改上游脚本、Metadata、模型窗口或方向映射。

四个脚本字节相同，SHA256 均为 `689fe3734e3cdae5524a0e6cf8b22d83e96a40686baba77dd2bbd2eb6e08c6c0`。Metadata 摘要如下：

| base scheme ID | Metadata SHA256 |
|---|---|
| `one_y_t5_liq_excess_a_v1` | `e1e3bac8c54f8a192b02cc4a1433bad9ebf1552388d90f54cb5db25eb9c287d0` |
| `one_y_t5_liq_excess_a_w252_l7_v1` | `36c0da6a2b4edb621fff21c82de823be09174aaec813680bc98ce747b3e219b0` |
| `one_y_t5_liq_excess_a_w350_l7_v1` | `217d1d64761adbc1a7f1e25207a303b0de8b0b031397c9c9fffc61593c132799` |
| `one_y_t5_liq_excess_b_w252_l7_v1` | `a30b7a7cf326d70c5c822c6df94cf31d294d997c5d6d858339a6737814c76f25` |

Intake 后四个方案均为 `blackbox_v2 + data_bridge_current + paused + draft`，任务口径均为 `1Y + T+5 + horizon=5`，delivery 文件权限均为 `0444`。

## 3. 平台 Preflight

| 检查 | 结果 |
|---|---|
| 服务回归 | 324/324 `unittest` 通过；冻结服务环境未安装 `pytest`，未临时安装依赖 |
| Runtime Profile | `blackbox-v2-v1` |
| 环境指纹 | `720ad40ab77cd6c7156ff35a80cf3604ac3a6153425ed235a4e3158b0631f8bd` |
| sandbox | 网络拒绝、DataBridge 目录写入拒绝 |
| generation | `full-20260720-055026-00e12e3803a8`，`refresh_date=2026-07-20` |
| snapshot | `snapshot-fd8a1f8736d3a4d057fbd98e` |
| business digest | `00e12e3803a89e3b06439881059c9f3d9093e8642b7c37f063f4ff6169c54f1b` |
| 日频 current | 3878×774，最大键 `2026-07-17`，SHA256 `03bbcc94c11acc58ac5647c2b530e2be3e45b9ed5fd74ff12a5677854e3a50e1` |
| 周频 current | 847×575，最大键 `202628`，SHA256 `9dfe8a8cfaae9fd4be1e70b872ff2d89d5839f4c266af3924eb0e0f5d4ebc4d5` |
| 月频 current | 201×123，最大键 `202701`，SHA256 `f29607a79c66860d4c43f369d99cb4fdfbba2cf665d3931f930f496bc21d86b5` |

### 3.1 CompareGate 探针修正

首轮真实 Gate 暴露平台未来行探针缺陷：硬编码 `2999-12-31` 超出冻结 pandas 的时间戳范围；首次改为动态日期后又因输出 `YYYY-MM-DD` 与 DataBridge 的 `YYYY/MM/DD HH:MM` 混用而被严格解析拒绝。两次均停在 CompareGate，未执行后续 Gate 或任何业务写入。

平台以测试先行方式将日频探针改为“快照末日加一天并保留原始日期格式”。目标回归和 Blackbox Gate 模块 20/20 通过，真实 current 探针末行变为 `2026/07/18 00:00` 且保持升序。交付脚本与 Metadata 字节未变化。

## 4. 10 轮稳定性认证

四方案共 40 个正式计数轮次全部通过，每轮固定覆盖 `static → input → unit → dry-run → compare → backtest → api-readiness`。每轮 no-persist 回测均返回 100 条，重复、predict/backtest、分批、Request 顺序和未来行隔离均通过。

| base scheme ID | 通过轮次 | P50 | P95 | 最终 Harness run |
|---|---:|---:|---:|---|
| `one_y_t5_liq_excess_a_v1` | 10/10 | 28 秒 | 28 秒 | `hr_20260720T085540Z_2068d5d0eea9` |
| `one_y_t5_liq_excess_a_w252_l7_v1` | 10/10 | 28 秒 | 29 秒 | `hr_20260720T090110Z_3de057d3955a` |
| `one_y_t5_liq_excess_a_w350_l7_v1` | 10/10 | 29 秒 | 29 秒 | `hr_20260720T090702Z_ea12669b5485` |
| `one_y_t5_liq_excess_b_w252_l7_v1` | 10/10 | 29 秒 | 29 秒 | `hr_20260720T091230Z_3852f6ea92e4` |

四个最终 run 均在 `t_harness_runs` 中精确命中 `stage=all + status=passed`，并各自拥有七条完整 passed Gate 记录。自动段结束后，四方案的正式 run、prediction、run log、backtest run 和 backtest prediction 均为 0。

## 5. Shadow 登记

新 Intake 目录尚未被常驻 backend/scheduler 重新发现，因此第一次 ShadowGate 在读取 draft 控制面身份时 fail-closed，配置和数据库均未变化。随后通过正式 `sync_scheme_registry()` 入口只为这四个方案建立 `draft` version 和 `paused` Registry；该入口不能提升版本或激活 Registry。

四个方案之后分别使用绑定 exact scheme/version/latest run 的全新 900 秒 HMAC token 完成 ShadowGate。所有 lifecycle journal 均为 `verified`，终态为：

- config：`status=paused + version_status=shadow`；
- exact version：`shadow`；
- composite Registry：`paused + blackbox_v2`；
- active `/api/schemes`：四方案均不可见；
- scheduler：未重启，未挂载四方案任务；
- 每方案业务表：正式 run、prediction、run log、backtest run、backtest prediction 均为 0。

## 6. 下一检查点

代表性 Canary 固定为 `one_y_t5_liq_excess_a_w252_l7_v1`。下一步必须使用当日 generation 重新执行 all-stage，再分别签发 `blackbox_activate`、`backtest_persist` 和 `live_write` token；本记录在完成 Canary 的 100 条持久化回测、单次 `gray_live`、API/前端验收及下一交易日自然 `scheduled_live` 前保持 `IN_PROGRESS`。
