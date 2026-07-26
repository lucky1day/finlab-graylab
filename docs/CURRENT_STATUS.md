# 当前状态

**文档状态**：`CURRENT`

**目标读者**：项目负责人、平台运维和审计人员

**最后核验日期**：2026-07-26

本文只保留当前已验证结论。较早的逐日状态、数据库快照和整改过程已冻结到[历史状态记录](records/status/README.md)；未完成工作的排序查看[TODO](TODO.md)。

## 当前政策

- Native V1 只维护版本化政策清单中的既有身份，不接受新方案或算法升级。
- 新算法、新方案 ID、新目标、新任务和替代版本一律通过 Blackbox V2 两文件交付。
- Blackbox V2 自动 Gate 通过不等于生产授权；每个方案仍需独立完成生产准备核验和专项授权，且授权不得外推。

## 平台状态

| 项目 | 当前结论 |
|---|---|
| Native V1 | 保持原 Registry、scheduler、数据库和历史结果，只做存量维护 |
| Blackbox V2 技术入库 | Intake、统一 DataBridge 输入、七个 Gate、预测和 no-persist 回测已形成稳定路径 |
| Blackbox V2 生产路径 | 一个真实周频方案已完成专项生产灰度；同一上游批次四个日频方案已完成 exact active 与历史入库验收，但实时灰度仍等待合法同日 generation；尚未形成面向任意新方案的通用生产授权 |
| 日频 08:00 保障 | `FUNCTIONAL_MVP_VERIFIED` 仅指受控 recorder 的 21/25 ledger 功能验证；真实 17+4 尚未联跑，08:00 SLA 与 07:55 容量仍无证据 |
| 平台总体评级 | `PRODUCTION_PATH_READY`，尚未取得覆盖所有任务和依赖的 `PRODUCTION_READY` |

## 本轮 10Y T+5 入库状态

- `ten_y_t5_maj3_k3_ic_static_v1`、`ten_y_t5_maj4_k3_ic_static_v1`、`ten_y_t5_maj4_k3_ic_yearly_v1` 和 `ten_y_t5_say_k5_sharpe_static_v1` 的 exact version 与 composite Registry 均为 `active`；每方案各有 1 个成功持久化回测 run、333 条历史 prediction 和 17 个月度指标。
- `/api/schemes` 已返回四方案，前端 10Y/T+5 格子共有 8 个候选，四方案的历史回测均已在 API 和前端可见；实时 `/api/metrics/{scheme_id}` 仍为空。
- production `t_input_generations=0`，四方案的 `gray_live`、`live_write`、`scheduled_live` 均为 0，统一状态为 `GRAY_LIVE_WAITING_FOR_SAME_DAY_GENERATION`；因此不能称为完整 gray 入库完成。
- 下一合法动作是在获准生产者发布合法同日 `SEALED` generation 后按 exact version 手工执行 `manual gray_live` 并验收实时 DB/API/frontend；调度设计和 gray admission 通过后才可自动调度。
- 四个 active 配置虽含 `schedule_cron`，在 gray admission 前不得把本 integration 合入或用于重启 legacy scheduler；自动 scheduler、`scheduled_live` 和旧 generation fallback 仍禁止。
- 本 integration 的 active daily discovery 为 25，而正式 policy 仍保持闭世界 21/25；policy、coordinator 和 replay 全量测试因此按设计 fail-closed。该结果是上线阻断证据，不是回归通过；gray admission 分离灰度 discovery 前必须继续隔离本分支。
- 四个缺少 `description` 的不可变既有交付均为 `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED`；exact version、摘要和来源证据见[10Y T+5 四方案手工入库记录](blackbox_v2/records/GRAY_ONBOARDING_10Y_T5_4SCHEMES_20260726.md)。

## 日频 08:00 整改状态

- 受控 recorder 已在隔离 MySQL 验证 21 item/25 target 的账本、双 lane、幂等、claim、原子提交和 watchdog；该证据没有执行真实 17+4 算法。
- 四个真实 V2 sealed delivery 的冻结输入、确定性、超时、generation fence、late 后继续执行和失败隔离已验证；隔离 replay 的 runtime/session/identity/process fence 和 `ProcessStartGuard` 已接线。
- `python -m harness daily-real-replay --check-only` 只读预检可运行，但已安装 backend LaunchAgent 缺少合法 coordinator mode，当前仍 fail-closed 为 `CONTROL_PLANE_BOUNDARY_UNAVAILABLE`。
- production 仍为 migration 017、rollout=`legacy`、admission=`BLOCKED`；ledger 三层账本和 generation 计数均为 0，未发生变化。
- 真实 21 算法同轮、07:55 容量、生产 clone migration、generation 长期归档、故障注入和连续 10 日均未通过；详细前置排序见[TODO](TODO.md)。

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
