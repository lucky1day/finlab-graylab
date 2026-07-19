# 方案入库统一入口

**文档状态**：`CURRENT`
**适用运行时**：`common`
**目标读者**：平台维护人员、算法工程师、代码评审人员
**最后核验日期**：2026-07-19

本文是 Bond Factor Lab 方案入库的唯一导航。所有后续新增方案一律使用 Blackbox V2；Native V1 只保留现有方案的维护能力。

## 1. 先选择流程

| 场景 | 必须使用的流程 |
|---|---|
| 新算法、新方案 ID、新目标期限或新任务类型 | Blackbox V2 |
| 现有 Native V1 的故障、数据口径或复现性修复 | Native V1 存量维护 |
| Native V1 的算法升级、替代实现或能力扩展 | 创建独立 Blackbox V2 trial |
| 查看具体方案状态 | `CURRENT_STATUS` 或 Blackbox 试验台账 |
| 查看历史规则和旧草案 | archive，只读，不得用于验收 |

不得通过复用旧 ID、复制 `predict.py + core/` 或修改 Native 白名单的方式把新算法伪装成存量维护。

## 2. 两个运行版本

| 维度 | Native V1 | Blackbox V2 |
|---|---|---|
| 机器标识 | `runtime_type: native_adapter` | `runtime_type: blackbox_v2` |
| 管理定位 | 存量维护 | 新增方案唯一入口 |
| 当前数量 | 29 | 1 个 shadow trial |
| 上游形态 | 平台仓库内 `config + predict + core` | 一个 `.py` 和一个 `.json` |
| 输入 | `legacy_db` 经 `shared.input_artifacts` | `data_bridge_current` 三频 Snapshot |
| 执行 | import adapter 子进程 | sandbox CLI 子进程 |
| 当前生命周期上限 | 保持既有状态 | `shadow + paused` |

两种运行时共用 Registry、版本、Harness 编排、日期语义、`PredictionRecord`、业务表、API 和前端。运行时差异只存在于交付检查、输入准备和执行驱动。

## 3. 版本术语

以下四个版本维度不得混称：

| 名称 | 含义 | 当前值示例 |
|---|---|---|
| 运行时代际 | 平台接入形态 | `Native V1`、`Blackbox V2` |
| 接口合同 | 上游 Metadata/Request/Result 契约 | `schema_version=1.0` |
| 数据 Schema | DataBridge 三频列契约 | `data-bridge-v1` |
| 运行 Profile | 环境和资源基准 | `blackbox-v2-v1` |

文件名中的 `V1` 若用于 Blackbox 文档，只表示合同修订 1，不表示 Native V1。

## 4. 当前文档

### Blackbox V2

- [上游交付 SOP](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)
- [平台 Intake 至 Shadow SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)
- [生产晋级准备清单](../blackbox_v2/PRODUCTION_READINESS.md)
- [架构与实现边界](../BLACKBOX_V2_PLATFORM.md)
- [试验台账](../blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md)

### Native V1

- [Native V1 文档入口](../native_v1/README.md)
- [存量维护 T0](../sop/NATIVE_V1_MAINTENANCE_T0.md)
- [存量维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)
- [修改后验证 SOP](../sop/NATIVE_V1_POST_CHANGE_TEST_SOP.md)
- [Native V1 机器契约说明](../native_v1/SCHEME_CONTRACT.md)

### 共享规范

- [共享方案契约](../SCHEME_CONTRACT.md)
- [预测日期语义](../PREDICTION_SEMANTICS.md)
- [源算法保真](../SOURCE_ALGORITHM_FIDELITY.md)
- [Harness 架构](../HARNESS_ARCHITECTURE.md)

## 5. 政策门禁

版本化政策位于 `deploy/onboarding_policy_v1.json`：

- `legacy_native_scheme_ids` 是唯一允许进入 Native V1 维护 Gate 的身份集合；
- Native StaticGate 和 ActivationGate 均拒绝清单外的 `native_adapter`；
- Blackbox V2 不使用 Native 白名单；
- 修改白名单属于平台治理变更，不属于普通方案入库。

政策文件只决定“身份能否进入运行时”，不能替代人工算法改动分级。既有 ID 的算法升级仍必须在评审中识别并转为 Blackbox V2。

## 6. 文档状态

| 状态 | 含义 |
|---|---|
| `CURRENT` | 当前规范或统一入口 |
| `LEGACY_MAINTENANCE` | 仅适用于存量 Native V1 维护 |
| `BLOCKED_DRAFT` | 目标规则已定义，但前置能力未完成，不得执行 |
| `HISTORICAL` | 历史证据或废弃规则，不得用于当前验收 |

具体方案、运行 ID、generation、snapshot 和数据库计数只能进入状态文档或试验台账，不得写进通用 SOP。
