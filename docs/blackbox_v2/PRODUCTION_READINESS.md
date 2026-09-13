# Blackbox V2 生产准备清单

**文档状态**：`CURRENT`

本文定义已入库 exact version 的生产接管验收，不是回测前置门禁。各副作用的授权和操作前提按
[平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)执行；发布步骤按[部署运行手册](../../deploy/README.md)执行。

## 接管条件

| 检查 | 所需证据与权威规则 |
|---|---|
| 交付与版本 | [SOP 第 2—4 节](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#2-step-1intake新-id)的两文件校验、最终 canonical、完整不可变回测和激活证据；exact、脚本校验策略、环境与输入绑定一致 |
| 本机输入 | [DataBridge 契约](data_bridge_v1/README.md)定义的 ready generation 与匹配输入视图；方案流程不代建 producer 输入 |
| 运行边界 | 当前 Runtime Profile、输入篡改检查和标准 Result 校验通过；上游负责的确定性、分批等价和未来数据隔离不能冒称为平台已独立验证 |
| 生命周期 | 本机数据库 exact version 与 composite Registry 为权威；最终 config、exact 与全部 target active，且 cadence 与部署目标匹配 |
| 历史与灰度 | 按 [SOP 第 6 节](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#6-灰度区间批量物化)选择批量或单日入口；按[预测语义](../architecture/PREDICTION_SEMANTICS.md#52-历史批次与灰度区间批次)核验完整键集合、边界、来源与 insert-only |
| 产品读回 | 按[Dashboard 合同](../operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#认证响应与合同验收)使用有效会话验证 HTTP 与业务可见性；API 不携带 exact，版本另由数据库和 release 核验 |
| 目标环境 | release、Schema、实际解释器/依赖、installed/loaded 控制面和可恢复边界满足部署手册；周期均值任务还须满足下述 Schema 条件 |
| 自然观察计划 | 接管时明确首次自然窗口与观察责任，接管后按[调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)核对真实时钟、run、prediction 和 Dashboard；未到窗口可保持待观察，不能将模拟或补缺标为自然运行通过 |

`monthly_average`、`quarterly_average`、`annual_average` 在部署读取周期 Actual 的 Backend 前，须确认 migration 020 已经受控应用且 closed-world schema 校验通过。它们复用既有 close-period 控制面，不增加任务专属 timer。

## 异常与权限

输入、版本、算法或生命周期不匹配时停止并保留证据，按[SOP 失败处理](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#7-失败处理)恢复；不得使用另一方案、旧版本或前端显示替代当前 exact 的证据，也不得覆盖既有事实。

清单通过不授予数据库写入、DDL、installed plist/unit 变更、服务重启或 Writer 切换权限。现场操作授权与禁止恢复的调度控制面以[根规范](../../AGENTS.md)为准。
