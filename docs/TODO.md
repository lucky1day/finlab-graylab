# 当前优先级与待办

**文档状态**：`CURRENT`

**目标读者**：项目负责人、平台开发、运维和审计人员

**最后核验日期**：2026-07-26

本文是当前未完成工作的唯一权威排序。它不替代[当前状态](CURRENT_STATUS.md)的已验证结论，也不替代专项记录的时点证据。

## P0：10Y T+5 四方案同日手工灰度补全

本批四个日频 Blackbox V2 方案已完成 revalidation → `controlled activate` → `persistent backtest` → 历史 `DB/API/frontend acceptance`：四个 exact version 和 composite Registry 均为 `active`，每方案各有 1 个成功持久化回测 run、333 条历史 prediction 和 17 个月度指标；`/api/schemes` 与前端均已可见，10Y/T+5 格子共有 8 个候选。该结论只是历史入库完成，不是完整 `gray_live` 入库完成。

| base scheme ID | description 专项状态 | 当前入库状态 |
|---|---|---|
| `ten_y_t5_maj3_k3_ic_static_v1` | `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED` | `EXACT_ACTIVE / GRAY_LIVE_WAITING_FOR_SAME_DAY_GENERATION` |
| `ten_y_t5_maj4_k3_ic_static_v1` | `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED` | `EXACT_ACTIVE / GRAY_LIVE_WAITING_FOR_SAME_DAY_GENERATION` |
| `ten_y_t5_maj4_k3_ic_yearly_v1` | `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED` | `EXACT_ACTIVE / GRAY_LIVE_WAITING_FOR_SAME_DAY_GENERATION` |
| `ten_y_t5_say_k5_sharpe_static_v1` | `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED` | `EXACT_ACTIVE / GRAY_LIVE_WAITING_FOR_SAME_DAY_GENERATION` |

- production `t_input_generations=0`，四方案的 `gray_live`、`live_write` 和 `scheduled_live` 均为 0；实时 metrics 为空，统一状态为 `GRAY_LIVE_WAITING_FOR_SAME_DAY_GENERATION`。
- 下一合法动作是在获准生产者发布合法同日 `SEALED` generation 后，按 exact version 专项授权手工执行 `manual gray_live`，再验收实时 DB/API/frontend；禁止 `旧 generation fallback`，也不得提前执行 `scheduled_live`。
- 四个 active 配置均含 `schedule_cron`；在 gray admission 的调度设计通过并获授权前，不得把本 integration 合入或用于重启 legacy scheduler。`active` 不等于 scheduler 授权。
- 本 integration 的四个 active config 会使 active daily discovery 从 21 变为 25，而正式 policy 仍是闭世界 21/25；因此 policy、coordinator 和 replay 全量测试按设计 fail-closed。这是上线阻断证据，不是回归通过；在 gray admission 将灰度 discovery 与正式 occurrence 分离前，分支必须保持隔离。
- 四个不可变既有 Metadata 均缺少 `description`；专项豁免不改写 Metadata、不代写算法逻辑，也不外推到其他交付。
- 后续新交付必须提供合规的 `description`；当前机器 Intake 对缺失仍只返回兼容 warning，平台人工收包必须 fail-closed。把该要求升级为机器门禁尚待独立 TDD 实施，不能由本批豁免推定已完成。
- 本批 exact version、Metadata/Delivery SHA、运行异常和来源提交只由[10Y T+5 四方案手工入库记录](blackbox_v2/records/GRAY_ONBOARDING_10Y_T5_4SCHEMES_20260726.md)定义。

## P1：日频平台前置依赖（严格顺序）

本批自动调度只能在以下依赖全部通过后开始；每项均需有可审计证据，不能以 recorder、伪造 seal、生产库写入或其他替代物跳过。

1. `migration017 namespace digest`：完成 namespace digest 的生产前置闭合。
2. `migration017 real MySQL recovery`：完成真实 MySQL 恢复演练。
3. `canonical migration runner`：完成 canonical runner 的 apply、重复执行与恢复边界。
4. `execute-only replay`：提供并核验 production audit-readonly 的 execute-only 入口。
5. `real 17 Native + 4 formal V2 21/25`：在获准独占维护窗口完成同轮真实联跑。
6. `scheduler resource/recovery/atomic commit/capacity`：通过资源、恢复、原子提交和容量门禁。
7. `three 0629 generation adapters`：三个 0629 方案逐个完成公共 generation adapter、CompareGate 与独立提交；触及 L2 即停止并改走 Blackbox V2 replacement。
8. `migrations018/019/020`：完成生产同构脱敏 clone 的迁移、断连与 `APPLYING` 恢复演练。
9. `archive/disk`：完成 generation 长期归档、去重、磁盘上限与回收策略。
10. `exact 20 forced-cold +20 revision/suffix`：完成精确 20 次 forced-cold、20 次 revision/suffix、故障注入、07:55 门禁和连续十个交易日观察。

前置事项的架构门禁见[日频信号 08:00 SLA 架构](architecture/DAILY_SIGNAL_SLA.md)；已验证进展只记录在[当前状态](CURRENT_STATUS.md)。

## P2：独立 gray/formal admission 与本批自动灰度

仅在 P1 全部通过、调度设计验收通过且获得新的专项授权后，才建设独立的 `scheduler_admission=gray|formal`（即 `gray/formal admission`）。它必须与既有 21/25 occurrence 的 admission、账本和生产写入边界分离；gray admission 通过后才可把本批四方案接入 `automatic gray scheduling`。`formal` 的准入必须另行授权，不能由 `gray`、description 豁免或本批手工入库结论推导。

## P3：正式晋级

只有本批自动灰度完成专项观察、actual/API/前端证据齐全并取得新的逐方案授权后，才可讨论 `formal promotion`；不得把手工入库、前端可见性或 gray admission 混称为正式生产。
