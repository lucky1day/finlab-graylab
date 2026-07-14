# 文档索引（Bond Factor Lab Docs）

**更新日期**: 2026-07-14

> 当前文档入口以本文为准。历史检查报告只作为带日期的审计证据保留，不是当前状态或现行规则来源；新增或修复方案只读现行规范，不从历史报告推导规则。

> 发布分支规则：`master` 是生产分支和远程默认分支。验证完成的开发分支只有在用户明确确认后，才能合并或覆盖到 `master` 并推送远程；agent 不得自行决定发布到 `master`。项目不再维护第二生产分支。

## 架构

| 文档 | 内容 |
|------|------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 系统架构：部署拓扑、DB schema、API 契约、数据流 |
| [CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md) | 代码架构主蓝图：分层模型、包依赖方向、运行时调用图 |
| [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) | 强约束 harness 边界总纲：分层边界、DB 安全边界 |
| [SOURCE_ALGORITHM_FIDELITY.md](SOURCE_ALGORITHM_FIDELITY.md) | 源算法保真强约束：source-backed 方案不得改变原始算法逻辑 |
| [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) | 方案契约：config.yaml schema、predict.py 接口、core 约束 |
| [PREDICTION_SEMANTICS.md](PREDICTION_SEMANTICS.md) | 预测日期与实盘阶段强制语义：`predict_date` / `feature_date` / `target_date` / `prediction_phase` |
| [SCHEME_PARADIGM.md](SCHEME_PARADIGM.md) | 方案生命周期目标态提案（DRAFT）：尚未落地的接口、目录和 gate 不构成当前强制契约 |

> Registry 方案身份已收敛为单表 per-tenor 语义：前端和业务使用 `t_scheme_registry.scheme_id = {base_scheme_id}__h{horizon}__{target_tenor}`；算法目录、scheduler 和预测记录仍使用 base `scheme_id`。前端任务格子由 registry `target_tenor + task_type` 定义，`task_type ∈ {T+1, T+5, weekly_point, weekly_average, monthly}`，不再由 `frequency/horizon` 隐式推断。细节见 [ARCHITECTURE.md](ARCHITECTURE.md) §3.4 / §5 与 [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md)。

> 当前入库 `stage all` 固定为 `static -> input -> unit -> dry-run -> compare -> backtest -> api-readiness`。`api-readiness` 是激活前 paused registry row 验收；active-only `api` gate、`live` 写库、backtest persist 和 activate 都是显式授权或激活后的步骤。

> Source-backed 方案必须先遵守 [SOURCE_ALGORITHM_FIDELITY.md](SOURCE_ALGORITHM_FIDELITY.md)：不得修改原始算法逻辑，时间起点、窗口、特征、对齐、模型参数、投票/fallback 和内部 score 映射都必须按原始脚本复现。外部 `latest_oos` / batch 结果是否作为 source-original reproduction、strict PIT 或平台 live-like PIT 变体，必须先分类并留证；所有改动先分级为 L0/L1/L2，L2 算法内部改动默认禁止。平台变体不得冒充原始 source 输出。跨灰度边界的 benchmark 必须按 row role 拆分，固定 future `source_end` 的 source batch 不能直接当作 live-safe 逐日数值真值。

> 月度方案有独立调度语义：每个自然月 15 号预测一次，无论 15 号是否交易日；`predict_date` 保留自然 15 号，`feature_date` / `target_date` 分别取当前月/目标月 15 号及以前最近交易日。灰度/实盘边界按方案级 `target_date` 判定；当前 0629 月度三方案中 `target_date >= 2026-06-01` 均为 `gray_live`，历史回测 latest 截止到 `target_date=2026-05-15`。

> 日度 0629 三方案（`daily_1y_xgb_1y13_0629`、`daily_5y_lgbm_5y10_0629`、`daily_10y_lgbm_10y04_0629`）已完成 SOP 收口。历史回测统一按 `feature_date >= 2025-01-01` 且 `target_date < 2026-06-01` 输出，latest backtest 每方案 337 行；`target_date=2026-06-01..2026-07-01` 的 22 个交易日已作为 `gray_live` 补齐。live 顶层 `model_version` 必须适配 DB `VARCHAR(64)`，完整 source model id 应放入 `extra` 审计字段。

> 2026-07-06/11 运维复审已完成：07/06 scheduler miss 已受控补跑可产出方案；stale actual tail 已清理；07/07 进一步发现 `t1_daily/t5_daily` 在源水位不足时复用旧 `feature_date=2026-07-03`，其中 `t5_daily` 因业务唯一键覆盖 07/06 明细。已在 executor 增加 daily live 日期语义 fail-closed，在 `scripts/check_production_daily_health.py` 增加 run/prediction 明细一致性与 stale live 检查，并完成生产数据修复。07/09 补齐 scheduler launchd 运行态基线：`RunAtLoad + KeepAlive`、`BOND_SCHEDULER_STARTUP_CATCHUP=1`，启动时会补跑当天已过 cron 且无终态 run 的 active 任务；actual 刷新扩展为 `08:30/19:00/23:45`。07/11 修复周末 actual 补刷缺口：非交易日 daily/weekly actuals 刷新到上一交易日，避免周五源数据晚于 `23:45` 入库后 T+1 最新验证卡住。当前 07/10 T+1 预测与 actual 均已齐。详见 [CURRENT_STATUS.md](CURRENT_STATUS.md) 与 [OPS_AUDIT_2026-07-06.md](OPS_AUDIT_2026-07-06.md)。

## 操作

| 文档 | 内容 |
|------|------|
| [sop/SCHEME_ONBOARDING_T0.md](sop/SCHEME_ONBOARDING_T0.md) | 新增方案前必读 T0 强约束范式（daily / weekly / monthly 通用） |
| [sop/SCHEME_ONBOARDING_SOP.md](sop/SCHEME_ONBOARDING_SOP.md) | 新增方案入库 SOP（含 2026-06-10 修订的数据口径规则） |
| [sop/SCHEME_POST_ONBOARDING_TEST_SOP.md](sop/SCHEME_POST_ONBOARDING_TEST_SOP.md) | 入库后测试验证 SOP |
| [CLOUD_ENVIRONMENT.md](CLOUD_ENVIRONMENT.md) | 云服务器第一步：Conda 双环境复刻、依赖快照和只读烟测 |

日常生产巡检：`scripts/check_production_daily_health.py` 是只读健康检查入口，用于发现交易日日频预测整体缺失、active 日频方案缺成功 run、run log 成功数与 prediction 明细不一致、active 日频 prediction 复用旧 feature/target、actual 晚于源表水位等问题；用法见 [deploy/README.md](../deploy/README.md) §监控。

## 状态与参考

| 文档 | 内容 |
|------|------|
| [CURRENT_STATUS.md](CURRENT_STATUS.md) | 项目当前状态（单一来源） |
| [OPS_AUDIT_2026-07-06.md](OPS_AUDIT_2026-07-06.md) | 2026-07-06 运维深度审计报告：发现、验证、修复、残余风险和后续处置 |
| [check/bond_factor_lab_all_schemes_system_check_20260628.md](check/bond_factor_lab_all_schemes_system_check_20260628.md) | 2026-06-28 全方案历史检查快照（非当前状态） |
| [check/bond_factor_lab_system_check_against_old_runbook_20260628.md](check/bond_factor_lab_system_check_against_old_runbook_20260628.md) | 2026-06-28 旧 runbook 对照检查快照（非当前规范） |

## 阅读路径

- **新人入门** → CODE_ARCHITECTURE → ARCHITECTURE → PREDICTION_SEMANTICS → SOURCE_ALGORITHM_FIDELITY → SCHEME_CONTRACT → CURRENT_STATUS
- **新增方案** → 先读 [sop/SCHEME_ONBOARDING_T0.md](sop/SCHEME_ONBOARDING_T0.md) → 再读 [PREDICTION_SEMANTICS.md](PREDICTION_SEMANTICS.md) + [SOURCE_ALGORITHM_FIDELITY.md](SOURCE_ALGORITHM_FIDELITY.md) + [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) + [sop/SCHEME_ONBOARDING_SOP.md](sop/SCHEME_ONBOARDING_SOP.md)，用 `python -m harness onboard {scheme_id} --stage all` 驱动 pre-activation gates
- **改 harness** → HARNESS_ARCHITECTURE → CODE_ARCHITECTURE → 对应 tests
- **讨论平台目标态** → SCHEME_PARADIGM；其中未落地条款必须先走独立平台改造评审，不得直接用于拒绝或改写现有方案
- **了解当前状态** → CURRENT_STATUS
