# 方案入库统一入口

**文档状态**：`CURRENT`

**目标读者**：平台维护人员、算法工程师、代码评审人员

本文只负责选择入库路径，不记录方案数量、运行结果或生命周期现状。动态事实查看[当前状态](../CURRENT_STATUS.md)。

涉及 ECS/Mac3 时，先读[双机部署与访问入口](../operations/DEPLOYMENT_ACCESS.md)，直接使用已核验的连接与生产目录，再按本导航执行标准 CLI。已归档的批次 operator 脚本不是日常入口。

## 选择流程

| 场景 | 必须使用的流程 |
|---|---|
| 新算法、新方案 ID、新目标期限或新任务类型 | Blackbox V2 |
| Mac3 W4 九个 Native V1 存量方案的故障、数据口径或复现性修复 | Native V1 存量维护 |
| Native V1 的算法升级、替代实现或能力扩展 | 创建独立 Blackbox V2 trial |
| 已完成运行时升级的身份与保留边界 | [当前状态](../CURRENT_STATUS.md#保留范围与历史保护)与[共享契约](../architecture/SCHEME_CONTRACT.md)，不恢复临时迁移入口 |
| 查看当前方案状态或未关闭问题 | [当前状态](../CURRENT_STATUS.md)或[统一后续推进计划](../TODO.md) |
| 查看历史规则和旧草案 | 使用 Git 历史；不得用于当前验收 |

不得通过复用旧 ID、复制 `predict.py + core/` 或修改 Native 白名单，把新算法伪装成存量维护。

已完成的运行时迁移不属于日常入库流程；临时迁移入口已删除。不得把旧 backtest 改名成新版本重新执行，
也不因历史迁移例外放宽普通 activate。

运行时目录、版本、输入和共享业务身份由[共享方案契约](../architecture/SCHEME_CONTRACT.md)定义；本页不维护第二份合同。

## 操作入口

### Blackbox V2

- 上游算法：[交付 SOP](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)
- 平台人员：[平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)
- 生产准备：[生产准备清单](../blackbox_v2/PRODUCTION_READINESS.md)
- 架构边界：[代码架构](../architecture/CODE_ARCHITECTURE.md)

### Native V1

- 运行接口：[Native V1 存量契约](../native_v1/SCHEME_CONTRACT.md)
- 准入、改动分级、实施和验证：[存量维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)

### 共享规则

- [共享方案契约](../architecture/SCHEME_CONTRACT.md)
- [预测日期语义](../architecture/PREDICTION_SEMANTICS.md)
- [源算法保真](../architecture/SOURCE_ALGORITHM_FIDELITY.md)
- [Harness 架构](../architecture/HARNESS_ARCHITECTURE.md)

## 执行顺序

新 Blackbox ID 依次执行 `intake-blackbox`、完整 `gate backtest --persist` 和 `activate`。
同 ID 修订直接按[平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)复验 canonical 并重新回测、激活，不重复 Intake。
Native 存量使用 `onboard`，其完整 `all` 与后续 `native-maintenance` 的互斥准入条件只按
[Native 维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md#4-自动-gate)判断。

开始新方案回测前必须确定历史与灰度的 target 分界；已有事实不可重算或覆盖。
历史批次、live-safe 要求和区间拒绝规则见[预测日期语义 5.2](../architecture/PREDICTION_SEMANTICS.md#52-历史批次与灰度区间批次)。
`signal-gap-fill` 是独立授权的补缺操作：按[平台 SOP 第 6 节](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#6-灰度区间批量物化)
判断是否支持区间批量；不满足批量条件时使用既有单日入口，不从导航自行扩大支持范围。

`gate dashboard` 是激活后的可选只读检查，不是入库门禁；它不证明 exact version 或自然调度成功。
实际生产准备与观察分别按[生产准备清单](../blackbox_v2/PRODUCTION_READINESS.md)和
[生产调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)验收。

## 可复用测试矩阵

下列命令只列长期维护的系统合同测试。新方案不得复制一份以方案名、固定日期或固定 hash 命名的测试；方案特有但可复用的接口行为应加入现有参数化 conformance。

所有命令从仓库根目录运行，并固定服务环境：

```bash
conda activate bond_factor_lab_service
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
```

| 时机 | 验证目标 | 命令 | 通过标准 |
|---|---|---|---|
| 改动任意方案配置后 | active discovery、运行时身份和两文件入口 | `python -m pytest -q tests/test_active_scheme_contracts.py tests/test_config_schema.py tests/test_onboarding_policy.py` | 全部通过 |
| 新增或修订 Blackbox V2 平台合同后 | 通用 Contract、Intake 与 discovery | `python -m pytest -q tests/test_blackbox_v2_contracts.py tests/test_blackbox_v2_intake.py tests/test_blackbox_v2_discovery.py` | 全部通过。新 ID 通过 Intake 收包；同 ID 修订替换 canonical 两文件后，由持久化回测入口重新执行同一安全校验。具体交付不永久复制专项 pytest；截止隔离、跨批结果等价及显式增量方案的复用语义属上游义务 |
| 修改 Blackbox 可选状态能力后 | 状态身份、路径、独占、原子恢复及维护入口 | `python -m pytest -q tests/test_blackbox_state.py tests/test_blackbox_v2_harness_gates.py` | 全部通过；非法 Result/身份/路径/第二 Writer 不发布，私有回测不推进生产状态，显式重建不写业务事实 |
| 修改 Blackbox 平台适配后 | generation 读取、runner、完整回测与激活证据 | `python -m pytest -q tests/test_data_bridge_current.py tests/test_blackbox_v2_runner.py tests/test_blackbox_v2_harness_gates.py tests/test_blackbox_activation.py` | 全部通过 |
| 修改 Registry、API 或前端后 | active 方案可见性、actual join、Dashboard 基础状态与 Harness HTTP 验收 | `python -m pytest -q tests/test_repository_registry.py tests/test_backend_api.py tests/test_factor_lab_dashboard_api.py tests/test_dashboard_gate.py` | 全部通过 |
| 修改 Native 存量适配后 | 当前数据库输入、执行器和 source isolation | `python -m pytest -q tests/test_native_input_artifacts.py tests/test_native_executor.py tests/test_source_runner_database_isolation.py` | 全部通过；不得修改 Native core 算法口径 |

按实际改动选择对应行，不为单个方案入库重复执行无关的全仓测试。`tests/` 只保留跨方案复用的长期合同，
不保存单次事故、迁移实施或生产 rollout 的永久回归。Native 的 `harness onboard ... --stage all`
验收精确方案版本和真实输入证据；Blackbox 由持久化回测验收当前 exact version。pytest 只保护本次修改触及的平台代码边界，不与单次方案验收重复承担同一职责。
