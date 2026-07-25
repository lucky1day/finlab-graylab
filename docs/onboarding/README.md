# 方案入库统一入口

**文档状态**：`CURRENT`

**目标读者**：平台维护人员、算法工程师、代码评审人员

**最后核验日期**：2026-07-26

本文只负责选择入库路径，不记录方案数量、运行结果或生命周期现状。动态事实查看[当前状态](../CURRENT_STATUS.md)。

## 选择流程

| 场景 | 必须使用的流程 |
|---|---|
| 新算法、新方案 ID、新目标期限或新任务类型 | Blackbox V2 |
| 现有 Native V1 的故障、数据口径或复现性修复 | Native V1 存量维护 |
| Native V1 的算法升级、替代实现或能力扩展 | 创建独立 Blackbox V2 trial |
| 查看具体方案状态或运行证据 | 当前状态或对应试验记录 |
| 查看历史规则和旧草案 | 历史目录，只读，不得用于验收 |

不得通过复用旧 ID、复制 `predict.py + core/` 或修改 Native 白名单，把新算法伪装成存量维护。

## 两种运行时

| 维度 | Native V1 | Blackbox V2 |
|---|---|---|
| 机器标识 | `runtime_type: native_adapter` | `runtime_type: blackbox_v2` |
| 管理定位 | 既有身份的存量维护 | 后续新增方案唯一入口 |
| 上游形态 | 仓库内 `config + predict + core` | 一个 `.py` 和一个 `.json` |
| 输入 | 配置/通用执行契约仍为 `legacy_db` 经统一输入层；正式 scheduled daily 按版本化 policy 使用 `generation_v1`，只有 policy 精确 allowlist 的存量兼容 ID 可使用 `live_source_0629` | 交付配置契约为 `data_bridge_current` 三频 Snapshot；正式 scheduled daily 必须绑定当天 `SEALED` generation，禁止旧代 fallback |
| 执行 | import adapter 子进程 | sandbox CLI 子进程 |
| 生产权限 | 保持既有方案的独立状态 | 每个方案必须单独完成生产准备和专项授权 |

两种运行时共用 Registry、版本、Harness 编排、日期语义、`PredictionRecord`、业务表、API 和前端。运行时只决定交付检查、输入准备和算法执行驱动。

## 操作入口

### Blackbox V2

- 上游算法：[交付 SOP](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)
- 平台人员：[平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)
- 生产准备：[生产准备清单](../blackbox_v2/PRODUCTION_READINESS.md)
- 架构边界：[Blackbox V2 平台架构](../architecture/BLACKBOX_V2_PLATFORM.md)
- 实验记录：[Blackbox V2 试验记录](../blackbox_v2/records/README.md)

### Native V1

- 文档入口：[Native V1 存量维护](../native_v1/README.md)
- 维护判断：[存量维护 T0](../sop/NATIVE_V1_MAINTENANCE_T0.md)
- 维护实施：[存量维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)
- 修改验证：[修改后验证 SOP](../sop/NATIVE_V1_POST_CHANGE_TEST_SOP.md)

### 共享规则

- [共享方案契约](../architecture/SCHEME_CONTRACT.md)
- [预测日期语义](../architecture/PREDICTION_SEMANTICS.md)
- [源算法保真](../architecture/SOURCE_ALGORITHM_FIDELITY.md)
- [Harness 架构](../architecture/HARNESS_ARCHITECTURE.md)

## 版本与政策

- `Blackbox V2`：运行时代际。
- `schema_version=1.0`：上游接口合同。
- `data-bridge-v1`：数据 Schema。
- `blackbox-v2-v1`：运行 Profile。
- `deploy/onboarding_policy_v1.json`：Native V1 存量身份的机器白名单。

机器白名单只决定身份能否进入 Native 维护 Gate，不能把算法升级变成存量修复。
