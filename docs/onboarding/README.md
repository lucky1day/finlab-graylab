# 方案入库统一入口

**文档状态**：`CURRENT`

**目标读者**：平台维护人员、算法工程师、代码评审人员

本文负责选择入库路径及入库相关验证，不记录方案数量、运行结果或生命周期现状。动态事实查看[当前状态](../CURRENT_STATUS.md)。

涉及 ECS/Mac3 时，先读[双机部署与访问入口](../operations/DEPLOYMENT_ACCESS.md)，直接使用已核验的连接与生产目录，再按本导航执行标准 CLI。已归档的批次 operator 脚本不是日常入口。

## 按场景进入

Blackbox 上游算法人员按[交付 SOP](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)完成自测并交付两文件；平台人员按下表选择操作和验收入口。

| 场景 | 操作与验收去向 |
|---|---|
| 新算法、新 ID、新目标期限或新任务 | [平台 SOP 第 3 节](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#3-新方案逐步操作)：Intake → 定稿 canonical 与部署清单 → 候选 release → 一次回测到最新日 → 激活 → 衔接补齐 → 接管验收 → 自然运行观察 |
| 同 ID Blackbox 修订 | [平台 SOP 第 4 节](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#4-同-id-修订)，不重复 Intake；已有事实不得重算或覆盖 |
| W4 九套固定 Native 版本运行、故障或漏跑 | [W4 运行维护](../sop/NATIVE_V1_MAINTENANCE_SOP.md)，接口见[存量契约](../native_v1/SCHEME_CONTRACT.md)；不做 Native 改版或重新准入 |
| W4 算法升级、替代实现或能力扩展 | 另行确认独立 Blackbox V2 方案，不修改既有 W4 版本 |
| 漏跑补跑与入库衔接 | 按[平台 SOP 3.6 节](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#36-衔接补齐回测末尾--首次自然运行)选择已支持的区间或单日补跑入口，结果按[入库完整性](../architecture/PREDICTION_SEMANTICS.md#52-入库完整性)核对；在对应授权范围内执行 |
| 判断方案是否已上线 | 只看本机数据库 exact 与 Registry，不看 `config.yaml` 的 `status`，见[平台 SOP 1.1](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#11-config-中的-status-不代表是否上线) |
| 接管与观察 | [生产准备](../blackbox_v2/PRODUCTION_READINESS.md)核验接管证据；[调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)核验现场与真实时钟；Dashboard 可见性不证明 exact 或自然运行 |
| 查迁移来源、保留身份或旧例外 | [当前状态](../CURRENT_STATUS.md#保留身份与恢复证据) → [保真与迁移边界](../architecture/SOURCE_ALGORITHM_FIDELITY.md#7-同算法-native--blackbox-迁移)及对应 Git/release；不恢复临时入口 |

不得通过复用旧 ID、复制 `predict.py + core/` 或修改 Native 白名单，把新算法伪装成存量维护。


## 入库相关验证

仓库侧入库改动（新增 `schemes/{id}/` 与部署矩阵条目）提交前，在服务环境中从仓库根目录运行：

| 验证目标 | 命令 | 通过标准 |
|---|---|---|
| canonical discovery、配置 Schema 与 exact 计算 | `python -B -m pytest -q tests/test_blackbox_v2_discovery.py tests/test_config_schema.py tests/test_active_scheme_contracts.py` | 全部通过 |
| 部署矩阵覆盖全部方案、目标过滤 fail-closed | `python -B -m pytest -q tests/test_deployment_scope.py` | 全部通过 |
| 修改 Intake、Contract、回测或激活实现时 | `python -B -m pytest -q tests/test_blackbox_v2_intake.py tests/test_blackbox_v2_contracts.py tests/test_blackbox_v2_harness_gates.py tests/test_blackbox_activation.py` | 全部通过 |

pytest 只保护公共合同，不替代目标环境的 exact 回测、激活、补缺与接管验收。修改平台其它代码时按
[平台开发测试矩阵](../development/TEST_MATRIX.md)选择检查。纯文档修改只做[文档维护与验收](../README.md#维护与验收)规定的检查。
