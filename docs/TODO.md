# 当前优先级与待办

**文档状态**：`CURRENT`

**目标读者**：项目负责人、平台开发、运维和审计人员

**最后核验日期**：2026-07-27

本文是当前未完成工作的唯一权威排序。它不替代[当前状态](CURRENT_STATUS.md)的已验证结论，也不替代专项记录的时点证据。

本批 10Y T+5 四方案的 `controlled activate`、`persistent backtest`、
`manual gray_live` 和前端验收已经完成，不再列为待办；终态见
[10Y T+5 四方案手工入库记录](blackbox_v2/records/GRAY_ONBOARDING_10Y_T5_4SCHEMES_20260726.md)。

## FengRL 五个月度方案手工灰度入库已完成

下列五个精确 Blackbox V2 月度方案已完成
`MANUAL_GRAY_ACCEPTED_5_OF_5`：

- `cgb_a4_fundseason_1y`
- `cgb_a4_fundseason_3y`
- `cgb_a4_fundseason_5y`
- `cgb_a4_fundseason_7y`
- `cgb_a4_fundseason_10y`

五方案 persisted all-stage 均为 7/7；每方案 16 条历史和 3 条手工
`gray_live` 已通过 DB/API/frontend 验收，每方案共 19 条，本批为
`80 + 15 = 95` 条。Registry/version 已 active，但完成手工灰度不授予自动调度；
本批没有 `scheduled_live`，也没有修改 rollout、admission 或 scheduler。时点证据见
[FengRL 五个月度方案记录](blackbox_v2/records/MONTHLY_ONBOARDING_FENGRL_5SCHEMES_20260726.md)。

## 已完成：Blackbox 自动调度防护 MVP

版本化 `blackbox_scheduler_admission_v1` 已冻结当前 5 个正式 Blackbox
身份为 `formal`、FengRL 五个月度与 10Y T+5 四个日频身份为 `gray`。`gray` 方案可以继续
保持 Registry active、手工运行和前端可见，但会在 legacy scheduler
注册、startup catch-up 和 scheduled wrapper 三个入口被拒绝；manual
single 不受影响。manual aggregate 和 legacy `--run-once predictions`
目前仍是绕过 admission 的既有运维入口，可能把 active gray 送入默认
`scheduled_live` phase；在 operator fence 完成前禁止对全量方案运行，
灰度手工写入只能使用受控 harness。

该防护同时拒绝未知 Blackbox、版本漂移和保留 Blackbox ID 的 runtime
重分类。策略缺失、非 UTF-8 或定义漂移时只关闭 Blackbox 自动调度，
Native、actuals、health 和 watchdog 继续。它没有实现 automatic gray
scheduling、没有写入 `scheduled_live`，也没有改变 rollout=`legacy`
或 admission=`BLOCKED`。

## P1：日频平台前置依赖（严格顺序）

本批自动调度只能在以下依赖全部通过后开始；每项均需有可审计证据，不能以 recorder、伪造 seal、生产库写入或其他替代物跳过。

1. `execute-only replay`：提供并核验 production audit-readonly 的 execute-only 入口。
2. `real 17 Native + 4 formal V2 21/25`：在获准独占维护窗口完成同轮真实联跑。
3. `scheduler resource/recovery/atomic commit/capacity`：通过资源、恢复、原子提交和容量门禁。
4. `three 0629 generation adapters`：三个 0629 方案逐个完成公共 generation adapter、CompareGate 与独立提交；触及 L2 即停止并改走 Blackbox V2 replacement。
5. `migrations018/019/020`：完成生产同构脱敏 clone 的迁移、断连与 `APPLYING` 恢复演练。
6. `archive/disk`：完成 generation 长期归档、去重、磁盘上限与回收策略。
7. `exact 20 forced-cold +20 revision/suffix`：完成精确 20 次 forced-cold、20 次 revision/suffix、故障注入、07:55 门禁和连续十个交易日观察。

前置事项的架构门禁见[日频信号 08:00 SLA 架构](architecture/DAILY_SIGNAL_SLA.md)；已验证进展只记录在[当前状态](CURRENT_STATUS.md)。

## P2：独立 gray/formal admission 与本批自动灰度

当前静态 admission 只解决“active 不等于自动调度权限”的安全同步问题。
仅在 P1 全部通过、调度设计验收通过且获得新的专项授权后，才扩展为
可执行的 `scheduler_admission=gray|formal` 和独立 gray 队列。它必须与
既有 21/25 occurrence 的 admission、账本和生产写入边界分离；gray
execution admission 通过后才可把以下已完成人工灰度的方案接入
`automatic gray scheduling`：

- `ten_y_t5_maj3_k3_ic_static_v1`
- `ten_y_t5_maj4_k3_ic_static_v1`
- `ten_y_t5_maj4_k3_ic_yearly_v1`
- `ten_y_t5_say_k5_sharpe_static_v1`
- `cgb_a4_fundseason_1y`
- `cgb_a4_fundseason_3y`
- `cgb_a4_fundseason_5y`
- `cgb_a4_fundseason_7y`
- `cgb_a4_fundseason_10y`

active 配置中的 `schedule_cron` 不构成调度授权。上述九个灰度方案均
已由当前静态 admission 以精确 `scheme_id + scheme_version` 冻结为
`gray`，代码已安全同步到开发分支，但不能产生自动任务。仓库 active
daily discovery 现在包含 25 item/29 target；daily policy、capacity
candidate、真实 replay 和 DailyRuntime 只选择其中 21 item/25 target
的正式身份。`active` 不等于 scheduler 授权，禁止旧 generation
fallback。`formal` 的准入必须另行授权，不能由 `gray`、description
豁免、代码同步或手工入库结论推导。

该阶段还必须给 `run_all_prediction_jobs` / legacy
`--run-once predictions` 增加 operator fence：聚合入口不得把
`gray` 身份写为 `scheduled_live`，受控 `gray_live` 继续只走 harness。
这是代码同步后的已知 P1，不影响当前 scheduler 自动入口，但在 ledger
切换或开放聚合运维命令前必须关闭。

## P3：正式晋级

只有本批自动灰度完成专项观察、actual/API/前端证据齐全并取得新的逐方案授权后，才可讨论 `formal promotion`；不得把手工入库、前端可见性或 gray admission 混称为正式生产。
