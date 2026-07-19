# Bond Factor Lab 文档索引

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：所有项目参与者
**最后核验日期**：2026-07-19

本文是仓库文档总入口；所有方案入库或维护必须从[统一入库导航](onboarding/README.md)开始。

## 1. 当前政策

- Native V1：只维护 `deploy/onboarding_policy_v1.json` 中的 29 个存量方案。
- Blackbox V2：所有后续新算法、新方案 ID、新目标、新任务和替代版本的唯一入库方式。
- Blackbox V2 当前最多进入 `shadow + paused`；生产晋级条件尚处于阻塞草案。
- 具体方案、generation、snapshot 和 Harness run 只写入状态文档或追加式试验台账，不写入通用 SOP。

## 2. 文档状态

每份当前治理文档使用以下状态之一：

| 状态 | 含义 |
|---|---|
| `CURRENT` | 当前有效规范或操作手册 |
| `LEGACY_MAINTENANCE` | 只适用于 Native V1 存量维护 |
| `BLOCKED_DRAFT` | 尚有实现阻塞，不可作为可执行 SOP |
| `HISTORICAL` | 仅供审计，不得用于当前验收 |

历史检查报告、审计报告和实施计划保持原文；日期结论不自动升级为当前规则。

## 3. 入库与维护

| 文档 | 状态 | 用途 |
|---|---|---|
| [统一入库导航](onboarding/README.md) | CURRENT | 判断使用 Native 维护还是 Blackbox 新增流程 |
| [Blackbox 上游交付 SOP](sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md) | CURRENT | Contract 1.0 两文件交付、CLI、输入输出和自验 |
| [Blackbox 平台入库 SOP](sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md) | CURRENT | Contract 1.0 Intake 至 shadow 的平台操作 |
| [Native V1 文档域](native_v1/README.md) | LEGACY_MAINTENANCE | 29 个存量方案的修复与验证入口 |
| [Blackbox 生产晋级条件](blackbox_v2/PRODUCTION_READINESS.md) | BLOCKED_DRAFT | 从 shadow 到 active/live 的实现阻塞项，不可执行 |

旧 `SCHEME_ONBOARDING_*.md` 和 `SCHEME_POST_ONBOARDING_TEST_SOP.md` 仅保留历史跳转，不是当前正文。

## 4. 共享规范

| 文档 | 内容 |
|---|---|
| [双运行时共享方案契约](SCHEME_CONTRACT.md) | 身份、任务、日期、结果、生命周期和运行时分派 |
| [系统架构](ARCHITECTURE.md) | 部署、数据、Registry、API 和双运行时执行流 |
| [代码架构](CODE_ARCHITECTURE.md) | 分层、依赖方向、输入/写库单点和扩展边界 |
| [Harness 架构](HARNESS_ARCHITECTURE.md) | Gate、授权、证据和副作用边界 |
| [预测语义](PREDICTION_SEMANTICS.md) | 三日期、任务组合、灰度和实盘语义 |
| [源算法保真](SOURCE_ALGORITHM_FIDELITY.md) | Native 与 Blackbox 的不同保真责任 |
| [Blackbox 平台架构](BLACKBOX_V2_PLATFORM.md) | 两文件执行器、DataBridge 快照和结果转换 |

## 5. 运行时专属文档

### Native V1

- [存量维护 T0](sop/NATIVE_V1_MAINTENANCE_T0.md)
- [存量维护 SOP](sop/NATIVE_V1_MAINTENANCE_SOP.md)
- [修改后验证 SOP](sop/NATIVE_V1_POST_CHANGE_TEST_SOP.md)
- [Native 专属契约](native_v1/SCHEME_CONTRACT.md)
- [历史档案](native_v1/archive/README.md)

### Blackbox V2

- [文档管理](blackbox_v2/README.md)
- [DataBridge V1 契约与样例](blackbox_v2/data_bridge_v1/README.md)
- [追加式试验台账](blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md)
- [生产晋级条件](blackbox_v2/PRODUCTION_READINESS.md)

版本名称必须分开理解：Blackbox V2 是运行时代际；`schema_version=1.0` 是接口合同；`data-bridge-v1` 是数据 Schema；`blackbox-v2-v1` 是 Runtime Profile。

## 6. 当前状态与历史证据

| 文档 | 用途 |
|---|---|
| [当前状态](CURRENT_STATUS.md) | 方案数量、状态和生产运行结论的当前事实源 |
| [Blackbox 试验台账](blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md) | generation、snapshot、run 和整改项的追加记录 |
| [2026-07-06 运维审计](OPS_AUDIT_2026-07-06.md) | 带日期的历史审计证据 |
| [历史系统检查](check/bond_factor_lab_all_schemes_system_check_20260628.md) | 2026-06-28 历史快照 |
| [旧 runbook 对照检查](check/bond_factor_lab_system_check_against_old_runbook_20260628.md) | 2026-06-28 历史对照 |

`SCHEME_PARADIGM.md` 已归档为 Native 目标态历史草案，不在当前阅读路径中。

## 7. 阅读路径

- 新算法工程师：只读 [Blackbox 上游交付 SOP](sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)。
- 平台接收新方案：先读[统一入库导航](onboarding/README.md)，再执行 [Blackbox 平台入库 SOP](sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)。
- 维护现有 Native：先读 [Native 存量维护 T0](sop/NATIVE_V1_MAINTENANCE_T0.md)，确认在白名单且不是算法升级。
- 修改 Harness：依次读[共享方案契约](SCHEME_CONTRACT.md)、[Harness 架构](HARNESS_ARCHITECTURE.md)和对应测试。
- 查看方案状态：读[当前状态](CURRENT_STATUS.md)；Blackbox 单次实验细节读[试验台账](blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md)。

## 8. 文档维护规则

1. 通用规则只写在 CURRENT 契约、架构或 SOP 中。
2. 运行时专属细节不得复制到共享文档形成第二份权威正文。
3. 具体运行记录只追加，不覆盖历史时点。
4. 相对链接必须可解析，旧入口只能跳转到当前文档或历史归档。
5. `AGENTS.md` 与 `CLAUDE.md` 必须保持字节一致。
6. 机器契约与文档冲突时先记录实现缺口，不得用文字宣称尚未具备的能力。
7. 发布到 `master` 仍需用户明确确认；文档整理不改变分支发布政策。
