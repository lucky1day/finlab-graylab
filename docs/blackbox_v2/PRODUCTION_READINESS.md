# Blackbox V2 生产准备清单

**文档状态**：`CURRENT`

本文是已入库 exact version 的生产接管核对清单，不是回测前置门禁，也不重复操作步骤。每项的命令、前置条件与
失败处理见[平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)对应小节；发布步骤见[部署手册](../../deploy/README.md)。

## 接管清单

| ✓ | 检查项 | 通过证据 | SOP |
|---|---|---|---|
| ☐ | 交付与 canonical | 两文件 Intake 通过；提交中的 canonical 即回测与激活使用的 exact | [3.1](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#31-intake)、[3.2](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#32-定稿-canonical-与部署清单) |
| ☐ | 部署清单 | 部署矩阵含该方案及正确目标；输入审计目录绑定当前 exact 与脚本 SHA；入库相关测试通过 | [3.2](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#32-定稿-canonical-与部署清单) |
| ☐ | release | 回测、激活、补缺均在同一已核准的不可变 release 中执行；current 已按授权切到包含该方案的 release | [3.3](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#33-候选-release-构建与安装) |
| ☐ | 输入 | 本机 producer-ready generation 与匹配输入视图；方案流程未代建输入 | [DataBridge 契约](data_bridge_v1/README.md) |
| ☐ | 回测证据 | 成功的 persisted run，exact、脚本校验策略、环境、generation 与 snapshot 与当前一致；覆盖到给定的最新数据日 | [3.4](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#34-持久化回测一次覆盖到最新数据日) |
| ☐ | 生命周期 | `activate` 读回 exact 与全部 composite Registry 为 active，owner 已经业务确认；**不以 config `status` 判断** | [1.1](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#11-config-中的-status-不代表是否上线)、[3.5](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#35-激活) |
| ☐ | 历史完整性 | 从 2025-01-01 到最新应发点，每个应有业务键恰好一条产品事实，符合[入库完整性](../architecture/PREDICTION_SEMANTICS.md#52-入库完整性) | [3.6](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#36-衔接补齐回测末尾--首次自然运行) |
| ☐ | 增量状态（仅 `incremental_state: true`） | 首个自然窗口前已完成显式预热/重建并验收 | [5.1](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#51-增量方案的显式预热重建) |
| ☐ | 产品读回 | `gate dashboard` 与 `gate data-consistency` 以有效会话通过；API 不携带 exact，版本另由数据库与 release 核验 | [3.8](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#38-接管验收) |
| ☐ | 目标环境 | Schema 处于 release 要求的基线与增量迁移版本，解释器/依赖、installed/loaded 控制面和恢复边界满足[部署手册](../../deploy/README.md#数据库迁移) | — |
| ☐ | 自然观察 | 明确首个自然窗口与观察责任；窗口后读回真实时钟产生的 `scheduled_live` run、预测与 Dashboard；未到窗口记为待观察 | [3.9](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#39-自然运行观察) |

运行边界：Runtime Profile、输入篡改检查与标准 Result 校验由平台验证；上游负责的确定性、分批等价与未来数据隔离
不能冒称为平台已独立验证。生产方案名单不是接管条件，仅在运维确认时维护（[SOP 3.7](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#37-生产方案名单仅运维确认时)）。

## 异常与权限

任一项不满足时停止并保留证据，按[SOP 失败处理](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#7-失败处理)恢复；不得使用另一方案、
旧版本或前端显示替代当前 exact 的证据，也不得覆盖既有事实。

清单通过不授予数据库写入、DDL、installed plist/unit 变更、服务重启或 Writer 切换权限；现场操作授权以[根规范](../../AGENTS.md)为准。
