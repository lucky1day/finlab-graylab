# 当前状态

**文档状态**：`CURRENT`

**目标读者**：项目负责人、平台运维和审计人员

**最后核验日期**：2026-07-27

本文只保留当前已验证结论。较早的逐日状态、数据库快照和整改过程已冻结到[历史状态记录](records/status/README.md)；未完成工作的排序查看[TODO](TODO.md)。

## 当前政策

- Native V1 只维护版本化政策清单中的既有身份，不接受新方案或算法升级。
- 新算法、新方案 ID、新目标、新任务和替代版本一律通过 Blackbox V2 两文件交付。
- Blackbox V2 自动 Gate 通过不等于生产授权；每个方案仍需独立完成生产准备核验和专项授权，且授权不得外推。

## 平台状态

| 项目 | 当前结论 |
|---|---|
| Native V1 | 保持原 Registry、scheduler、数据库和历史结果，只做存量维护 |
| Blackbox V2 技术入库 | Intake、DataBridge 三文件父快照、显式平台输入、七个 Gate、零写库 check-only、预测和 no-persist 回测已形成稳定路径 |
| Blackbox V2 生产路径 | 一个真实周频方案、四个日频方案和本批五个月频方案已完成各自专项生产灰度；尚未形成面向任意新方案的通用生产授权 |
| 日频 08:00 保障 | `FUNCTIONAL_MVP_VERIFIED` 仅指受控 recorder 的 21/25 ledger 功能验证；真实 17+4 尚未联跑，08:00 SLA 与 07:55 容量仍无证据 |
| 平台总体评级 | `PRODUCTION_PATH_READY`，尚未取得覆盖所有任务和依赖的 `PRODUCTION_READY` |

## 周度历史与月度 Actual 修复候选

- 开发分支候选 `d81c512` 将三类 actual updater 的默认范围改为 active Registry；`91d3779` 增加周度正式窗口、coverage 诊断和 Blackbox persisted 起点门禁。
- 定向回归为 `99 tests + 16 subtests`、`134 tests + 13 subtests`；最终全量为 `2928 passed / 26 skipped / 1008 subtests`。生产只读 Registry 范围为 daily/monthly=`1Y/3Y/5Y/7Y/10Y`、weekly=`1Y/5Y/7Y/10Y`。
- 候选尚未部署且本轮未写生产库。现有 8 个月度方案仍为 `19 signal / 18 valid`，但旧 updater 的复发风险要到候选部署和幂等重建后才关闭。
- 候选 dashboard 只读投影保留 21 条 2024 审计行但不计排行：`weekly_10y_lgbm_point_v1` 从 `101/101` 变为 `80/80`，`weekly_10y_d_overlay_0529` 仍为 `77/77`，其余五个为 `81/80`。
- coverage 还识别出 `2025-01-24/02-07/04-25` 与 `2025-01-26/02-08/04-27` 的节假日 target_date 差异；必须用新的合规 persisted run 重建，不能裁剪共同交集或删除旧历史。
- `7 个周度候选全部 81/80` 尚未达成；部署、actual 重建、受控回补和 DB/API/前端验收见 [TODO](TODO.md)。本轮未修改算法、scheduler cron、BondProjectPro、rollout 或 admission。

## 本轮 10Y T+5 入库状态

- `ten_y_t5_maj3_k3_ic_static_v1`、`ten_y_t5_maj4_k3_ic_static_v1`、`ten_y_t5_maj4_k3_ic_yearly_v1` 和 `ten_y_t5_say_k5_sharpe_static_v1` 的 exact version 与 composite Registry 均为 `active`；每方案各有 1 个成功持久化回测 run、333 条历史 prediction、17 个月度指标和 39 条手工 `gray_live`。
- 四方案使用 DataBridge generation `full-20260724-062251-4977e502dadf` 与 runtime snapshot `snapshot-46ff3231de2c4a080c46ba56`，灰度日期范围为 `predict_date=2026-05-26..2026-07-20`、`feature_date=2026-05-25..2026-07-17`、`target_date=2026-06-01..2026-07-24`；本批共 156 条 `gray_live`，与历史无重叠。
- `/api/schemes` 已返回四方案，前端 10Y/T+5 格子共有 8 个候选；每个新方案由 333 条 backtest 与 39 条 `gray_live` 组成，合计 372 条前端展示记录。
- 本批 `scheduled_live=0`，三层 ledger 为 `0/0/0`；rollout=`legacy`、admission=`BLOCKED`，没有启动或修改 scheduler。手工灰度入库完成不授予自动调度或正式日批准入。
- 四方案代码已从授权提交按精确 bytes 重基到当前开发基线；四个
  `scheme_id + scheme_version` 均在版本化 scheduler admission 中冻结为
  `gray`。仓库 active daily discovery 为 25 item/29 target，但正式
  daily policy、capacity candidate、真实 replay 和 DailyRuntime 仍只接收
  21 item/25 target；四方案不进入正式 occurrence。
- 四个 active 配置中的 `schedule_cron` 只是交付元数据，不构成 scheduler
  授权，也不授予 legacy scheduler 执行权限。
- legacy `run_all_prediction_jobs` / `--run-once predictions` 仍是绕过
  admission 的手工聚合入口，在 operator fence 完成前禁止用于全量运行；
  本次同步未调用该入口，受控手工灰度继续只走 harness。
- 自动 scheduler、`scheduled_live` 和旧 generation fallback 仍禁止。
- 四个缺少 `description` 的不可变既有交付均为 `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED`；exact version、摘要和来源证据见[10Y T+5 四方案手工入库记录](blackbox_v2/records/GRAY_ONBOARDING_10Y_T5_4SCHEMES_20260726.md)。

## 本轮五个月度方案手工灰度状态

- `cgb_a4_fundseason_1y`、`cgb_a4_fundseason_3y`、
  `cgb_a4_fundseason_5y`、`cgb_a4_fundseason_7y` 和
  `cgb_a4_fundseason_10y` 的 exact version 与 composite Registry 均为
  `active`；版本依次为 `04e7af163fb0`、`89d31f8cb95`、
  `7d47e0328532`、`ddba87ece7ae` 和 `85a65700499b`。
- 五个方案的 persisted all-stage 均为 7/7 passed；每方案持久化 1 个
  backtest run、16 条历史 prediction 和 16 条月度指标，历史
  `target_date < 2026-06-01`。本批历史合计 80 条。
- 每方案手工写入 3 条 `gray_live`，日期严格为
  `2026-05-15 → 2026-05-15 → 2026-06-15`、
  `2026-06-15 → 2026-06-15 → 2026-07-15` 和
  `2026-07-15 → 2026-07-15 → 2026-08-14`
  （依次为 predict/feature/target）；本批灰度合计 15 条。
- DB、API 和前端已验收：每方案显示 `16 + 3 = 19` 条信号，不是 18
  条；五方案合计 `80 + 15 = 95` 条。所有结果绑定同一 DataBridge
  generation `full-20260724-062251-4977e502dadf`。
- 本批每方案 `scheduled_live=0`，全局既有 scheduled 基线仍为
  490 runs / 513 predictions，三层 ledger 为 `0/0/0`；
  rollout=`legacy`、admission=`BLOCKED`。本次手工灰度不授予定时调度、
  正式日批、推送或部署。
- Blackbox 自动调度防护 MVP 已通过：精确 5 个既有正式身份为
  `formal`、本批五个月度与 10Y T+5 四个日频身份为 `gray`。九个灰度方案不会注册 legacy
  scheduler job、不会进入 startup catch-up，也不能通过 scheduled wrapper
  执行；手工运行与前端可见性保持不变。未知身份、版本/runtime 漂移和
  非 UTF-8/损坏策略均 fail-closed 于 Blackbox 自动调度域，不影响 Native、
  actuals、health 或 watchdog。该结论不等于 automatic gray scheduling。
- 历史预检的 `INTEGRATION_PREFLIGHT_READY_NO_WRITE` 与
  [FENGRL_MONTHLY_GRAY_PREFLIGHT_20260727.evidence.json](blackbox_v2/records/FENGRL_MONTHLY_GRAY_PREFLIGHT_20260727.evidence.json)
  仍保留为操作前时点证据；其中私有 publication
  不是数据库 `t_input_generations` 的 `SEALED` 记录。终验详见
  [FengRL 五个月度方案记录](blackbox_v2/records/MONTHLY_ONBOARDING_FENGRL_5SCHEMES_20260726.md)
  和
  [机器可读终验证据](blackbox_v2/records/FENGRL_MONTHLY_GRAY_ACCEPTANCE_20260727.evidence.json)。

## 日频 08:00 整改状态

- `migration017 namespace digest` 已由开发提交 `f93b154` 闭合：migration preflight 和 `APPLYING` inspect 会读取同 schema 的 FK/CHECK 保留名占用，非法占用在业务 DDL 前 fail-closed，状态占用同时进入 recovery digest；该结论绑定当前 Mac 的 MySQL 8.0.45、`lower_case_table_names=2`。
- `migration017 real MySQL recovery` 已由开发提交 `66e7a6b` 闭合：显式 opt-in 测试在本机隔离 MySQL 8.0.45、`lower_case_table_names=2` 上覆盖正常 public apply、首个 DDL 前中断、前两个 DDL 已 implicit commit 的中段恢复、DDL 完成但 history 未标记、定义漂移拒绝、FK/CHECK 大小写命名冲突 preflight 与 digest fence，以及 accent、跨约束类型和跨 schema 命名语义；8 个场景全部通过，临时进程和 datadir 均已回收。该结论没有应用生产迁移，不代表下一项 canonical migration runner 已完成。
- `canonical migration runner` 已由 `f3a5720`、`1f1019b`、`8ee916f` 与 `3c96f58` 闭合：唯一行为实现是 caller-supplied `Engine` 的 `migrations.runner`，唯一受控 operator wrapper 是 `scripts/apply_migrations.py`。隔离 MySQL CLI 已证明 normal apply/no-op、017 中段 recovery 和 018 两类 recovery；所有 CLI 写路径在建 Engine 前要求 expected database/server UUID，并在首个写动作前精确核验连接身份。inspect 保持只读且无需 identity 参数。隔离测试未应用生产 migration，且不等于 production-shaped sanitized clone 演练；后者仍是 `migrations018/019/020` 的待办。当前 CLI apply/no-op 尚无 durable signed operator report。
- 本轮日频生产化主线停在 canonical migration runner closure；下一项仍是 `execute-only replay`，其余真实 21/25、scheduler、0629、clone、归档与容量门禁均未启动。
- 受控 recorder 已在隔离 MySQL 验证 21 item/25 target 的账本、双 lane、幂等、claim、原子提交和 watchdog；该证据没有执行真实 17+4 算法。
- 四个真实 V2 sealed delivery 的冻结输入、确定性、超时、generation fence、late 后继续执行和失败隔离已验证；隔离 replay 的 runtime/session/identity/process fence 和 `ProcessStartGuard` 已接线。
- `python -m harness daily-real-replay --check-only` 只读预检可运行，但已安装 backend LaunchAgent 缺少合法 coordinator mode，当前仍 fail-closed 为 `CONTROL_PLANE_BOUNDARY_UNAVAILABLE`。
- production 仍为 migration 017、rollout=`legacy`、admission=`BLOCKED`；ledger 三层账本和 generation 计数均为 0，未发生变化。
- execute-only replay、真实 21 算法同轮、07:55 容量、生产同构 clone migration、generation 长期归档、故障注入和连续 10 日均未通过；详细前置排序见[TODO](TODO.md)。

## Native V1 当前摘要

- 版本化政策清单保留 29 个 Native V1 仓库身份；新增身份继续由机器门禁拒绝。
- 当前业务展示目标覆盖 `1Y/3Y/5Y/7Y/10Y`，具体 active 范围以 Registry 和 API 为准。
- 10Y 周点值方案的历史周历和输入冲突尚未通过数据治理修复，不能用补平或修改算法绕过。

## 权威入口

- 未完成工作：[TODO](TODO.md)
- 方案入库：[统一入库导航](onboarding/README.md)
- 上游交付：[Blackbox V2 上游交付 SOP](sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)
- 平台操作：[Blackbox V2 平台入库 SOP](sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)
- 生产准备：[Blackbox V2 生产准备清单](blackbox_v2/PRODUCTION_READINESS.md)
- 历史状态：[状态记录索引](records/status/README.md)
