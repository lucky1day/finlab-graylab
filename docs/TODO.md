# 当前优先级与待办

**文档状态**：`CURRENT`

**目标读者**：项目负责人、平台开发、运维和审计人员

**最后核验日期**：2026-07-27

本文是当前未完成工作的唯一权威排序。它不替代[当前状态](CURRENT_STATUS.md)的已验证结论，也不替代专项记录的时点证据。

本批 10Y T+5 四方案的 `controlled activate`、`persistent backtest`、
`manual gray_live` 和前端验收已经完成，不再列为待办；终态见
[10Y T+5 四方案手工入库记录](blackbox_v2/records/GRAY_ONBOARDING_10Y_T5_4SCHEMES_20260726.md)。

## P0：日频平台前置依赖（严格顺序）

本批自动调度只能在以下依赖全部通过后开始；每项均需有可审计证据，不能以 recorder、伪造 seal、生产库写入或其他替代物跳过。

1. `execute-only replay`：提供并核验 production audit-readonly 的 execute-only 入口。
2. `real 17 Native + 4 formal V2 21/25`：在获准独占维护窗口完成同轮真实联跑。
3. `scheduler resource/recovery/atomic commit/capacity`：通过资源、恢复、原子提交和容量门禁。
4. `three 0629 generation adapters`：三个 0629 方案逐个完成公共 generation adapter、CompareGate 与独立提交；触及 L2 即停止并改走 Blackbox V2 replacement。
5. `migrations018/019/020`：完成生产同构脱敏 clone 的迁移、断连与 `APPLYING` 恢复演练。
6. `archive/disk`：完成 generation 长期归档、去重、磁盘上限与回收策略。
7. `exact 20 forced-cold +20 revision/suffix`：完成精确 20 次 forced-cold、20 次 revision/suffix、故障注入、07:55 门禁和连续十个交易日观察。

前置事项的架构门禁见[日频信号 08:00 SLA 架构](architecture/DAILY_SIGNAL_SLA.md)；已验证进展只记录在[当前状态](CURRENT_STATUS.md)。

## P1：独立 gray/formal admission 与本批自动灰度

仅在 P0 全部通过、调度设计验收通过且获得新的专项授权后，才建设独立的 `scheduler_admission=gray|formal`（即 `gray/formal admission`）。它必须与既有 21/25 occurrence 的 admission、账本和生产写入边界分离；gray admission 通过后才可把以下四个方案接入 `automatic gray scheduling`：

- `ten_y_t5_maj3_k3_ic_static_v1`
- `ten_y_t5_maj4_k3_ic_static_v1`
- `ten_y_t5_maj4_k3_ic_yearly_v1`
- `ten_y_t5_say_k5_sharpe_static_v1`

四个 active 配置均含 `schedule_cron`；在 gray admission 通过前，不得把本
integration 合入或用于重启 legacy scheduler。该 integration 的 active daily
discovery 为 25 item/29 target，而正式 policy 仍为闭世界 21 item/25 target；
policy、coordinator 和 replay 全量测试继续按设计 fail-closed。`active` 不等于
scheduler 授权，禁止旧
generation fallback。`formal` 的准入必须另行授权，不能由 `gray`、description
豁免或本批手工入库结论推导。

## P2：正式晋级

只有本批自动灰度完成专项观察、actual/API/前端证据齐全并取得新的逐方案授权后，才可讨论 `formal promotion`；不得把手工入库、前端可见性或 gray admission 混称为正式生产。
