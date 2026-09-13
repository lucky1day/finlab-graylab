# 方案入库统一入口

**文档状态**：`CURRENT`

**目标读者**：平台维护人员、算法工程师、代码评审人员

本文负责选择入库路径及相关验证入口，不记录方案数量、运行结果或生命周期现状。动态事实查看[当前状态](../CURRENT_STATUS.md)。

涉及 ECS/Mac3 时，先读[双机部署与访问入口](../operations/DEPLOYMENT_ACCESS.md)，直接使用已核验的连接与生产目录，再按本导航执行标准 CLI。已归档的批次 operator 脚本不是日常入口。

## 选择流程

| 场景 | 必须使用的流程 |
|---|---|
| 新算法、新方案 ID、新目标期限或新任务类型 | Blackbox V2 |
| Mac3 W4 九个 Native 固定版本的运行检查、故障定位或漏跑恢复 | [W4 运行维护](../sop/NATIVE_V1_MAINTENANCE_SOP.md)，不做版本修订或重新准入 |
| W4 的算法升级、替代实现或能力扩展 | 另行确认独立 Blackbox V2 方案，不修改既有 W4 版本 |
| 已完成运行时升级的身份与保留边界 | [当前状态](../CURRENT_STATUS.md#保留身份与恢复证据)与[共享契约](../architecture/SCHEME_CONTRACT.md)，不恢复临时迁移入口 |
| 查看当前方案状态或未关闭问题 | [当前状态](../CURRENT_STATUS.md)或[统一后续推进计划](../TODO.md) |
| 查看历史规则和旧草案 | 使用 Git 历史；不得用于当前验收 |

不得通过复用旧 ID、复制 `predict.py + core/` 或修改 Native 白名单，把新算法伪装成存量维护。

历史运行时迁移只按[源算法保真](../architecture/SOURCE_ALGORITHM_FIDELITY.md#7-同算法-native--blackbox-迁移)追溯，不恢复临时入口或套用旧例外。

## 操作入口

### Blackbox V2

- 上游算法：[交付 SOP](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)
- 平台人员：[平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)
- 生产准备：[生产准备清单](../blackbox_v2/PRODUCTION_READINESS.md)
- 架构边界：[代码架构](../architecture/CODE_ARCHITECTURE.md)

### W4 固定版本运行

- 运行接口：[Native V1 存量契约](../native_v1/SCHEME_CONTRACT.md)
- 运行检查、故障与恢复边界：[W4 运行维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)

## 执行顺序

| 任务 | 操作与验收去向 |
|---|---|
| 新 Blackbox ID | [平台 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)：Intake → 定稿 canonical 与候选 release → 完整持久化回测 → 授权 activate |
| 同 ID Blackbox 修订 | 同一 SOP 的修订路径，不重复 Intake；已有事实不得重算或覆盖 |
| W4 固定版本运行 | [运行维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)核对现有版本、输入与日/周/月调度，不执行 Native 入库、回测准入或再激活 |
| 历史与灰度补齐 | 先按[预测语义](../architecture/PREDICTION_SEMANTICS.md#52-历史批次与灰度区间批次)确定分界及 live-safe 条件，再按[平台 SOP 第 5—6 节](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#5-可选后续动作)选择已支持的区间或单日入口并取得对应授权 |
| 接管与观察 | [生产准备](../blackbox_v2/PRODUCTION_READINESS.md)核验接管证据；[调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)核验现场与真实时钟；Dashboard 可见性不证明 exact 或自然运行 |

## 可复用测试矩阵

下列命令按实际改动选用，测试保留边界见[根规范](../../AGENTS.md)。

所有命令从仓库根目录运行，并固定服务环境：

```bash
conda activate bond_factor_lab_service
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
```

| 时机 | 验证目标 | 命令 | 通过标准 |
|---|---|---|---|
| 改动生产包依赖后 | 静态依赖方向及扫描器范围 | `python -B -m pytest -q tests/test_architecture_boundaries.py` | 无逆向依赖；不代替运行期权限与输入检查 |
| 改动任意方案配置后 | active discovery、运行时身份和两文件入口 | `python -m pytest -q tests/test_active_scheme_contracts.py tests/test_config_schema.py` | 全部通过 |
| 新增或修订 Blackbox V2 平台合同后 | 通用 Contract、Intake 与 discovery | `python -m pytest -q tests/test_blackbox_v2_contracts.py tests/test_blackbox_v2_intake.py tests/test_blackbox_v2_discovery.py` | 全部通过；真实方案仍按平台 SOP 完成 exact-version 回测，不以 pytest 替代 |
| 修改 Blackbox 可选状态能力后 | 状态身份、路径、独占、原子恢复及维护入口 | `python -m pytest -q tests/test_blackbox_state.py tests/test_blackbox_v2_harness_gates.py` | 全部通过；非法 Result/身份/路径/第二 Writer 不发布，私有回测不推进生产状态，显式重建不写业务事实 |
| 修改 Blackbox 平台适配后 | generation 读取、runner、完整回测与激活证据 | `python -m pytest -q tests/test_data_bridge_current.py tests/test_blackbox_v2_runner.py tests/test_blackbox_v2_harness_gates.py tests/test_blackbox_activation.py` | 全部通过 |
| 修改 Registry、API 或前端后 | active 方案可见性、actual join、Dashboard 基础状态与 Harness HTTP 验收 | `python -m pytest -q tests/test_repository_registry.py tests/test_backend_api.py tests/test_factor_lab_dashboard_api.py tests/test_dashboard_gate.py` | 全部通过 |
| 修改公共输入、执行器或清理旧依赖时保护 W4 | 当前数据库输入、执行器和 source isolation | `python -m pytest -q tests/test_native_input_artifacts.py tests/test_native_executor.py tests/test_source_runner_database_isolation.py` | 全部通过；保留 W4 固定版本的 adapter、source、输入与 runner 依赖，不据此开放 Native 改版 |
| 修改灰度规划或事务后 | 全部业务键预检、重复拒绝与提交边界 | `python -B -m pytest -q tests/test_signal_gap_plan.py tests/test_signal_gap_fill.py tests/test_repository_gray_gap_atomic.py` | 既有公共用例通过，失败不产生部分预测；不以隔离测试代替本机数据验收 |
| 修改 release 工具或启动器后 | 确定性包、摘要、路径隔离、current 切换与环境加载 | `python -B -m pytest -q tests/test_source_release_tools.py tests/test_launchd_release_launcher.py` | 临时测试环境的构建/安装/拒绝/恢复用例通过；不操作生产 current |
| 修改宿主调度入口或漂移检查后 | 部署目标、到期选择、生命周期、互斥与漂移识别 | `python -B -m pytest -q tests/test_systemd_control_plane.py tests/test_launchd_prediction_runner.py tests/test_close_prediction_runner.py tests/test_launchd_config_drift_audit.py` | 合同通过；installed/loaded、自然日志和事实另按调度治理读回 |

pytest 保护公共代码合同，不替代目标环境的精确版本、输入、持久化与接管验收。unit/plist/Nginx 模板不维护正文快照测试；模板变更按[部署手册](../../deploy/README.md)核验配置，并按[调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)读回现场。

纯文档修改只做[文档维护与验收](../README.md#维护与验收)规定的路径、作用域和任务检查；若解释某项现行行为存在疑点，可选用对应公共合同测试，不因整理文档运行全套算法或生产命令。
