# Bond Factor Lab 文档中心

**文档状态**：`CURRENT`

**目标读者**：所有项目参与者

本文是仓库文档的唯一总入口。这里不复制运行 ID、单次实验或生产时点状态；当前稳定事实查看
[当前状态](CURRENT_STATUS.md)，未批准工作的顺序和边界查看[统一后续推进计划](TODO.md)，生产调度当前规则查看
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。
所有新方案的平台入库只有三步：Blackbox 两文件 Intake、一次完整持久化回测和独立授权 activate。

## 按角色进入

| 读者 | 唯一入口 |
|---|---|
| 外部客户、业务负责人 | [灰度实验室说明手册](product/GRAY_LAB_USER_MANUAL.md) |
| 上游算法工程师 | [Blackbox V2 上游交付 SOP](sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md) |
| 平台入库和审计人员 | [方案入库统一入口](onboarding/README.md) |
| 平台开发人员 | [代码架构](architecture/CODE_ARCHITECTURE.md)与[共享契约](architecture/SCHEME_CONTRACT.md) |
| 平台运维人员 | [部署运行手册](../deploy/README.md)与[生产调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md) |
| 项目负责人 | [当前状态](CURRENT_STATUS.md)和[统一后续推进计划](TODO.md) |

## 文档域

| 文档域 | 内容 | 是否定义当前规则 |
|---|---|---|
| [入库导航](onboarding/README.md) | 判断使用 Blackbox V2 新增还是 Native V1 存量维护 | 是 |
| [SOP](sop/README.md) | 上游交付、平台入库和 Native 存量维护步骤 | 是 |
| [代码架构](architecture/CODE_ARCHITECTURE.md) | 分层、依赖、输入与写库边界 | 是 |
| [登录与账户管理](architecture/AUTHENTICATION_AND_ACCOUNT_MANAGEMENT.md) | 登录、会话、账户和管理员安全合同 | 是 |
| [产品手册](product/GRAY_LAB_USER_MANUAL.md) | 当前用户手册 | 是 |
| [公网性能验收](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md) | Dashboard 性能和故障处理边界 | 是 |
| [公网刷新与链路可靠性](operations/PUBLIC_FACTOR_LAB_REFRESH_RELIABILITY.md) | Dashboard 刷新、降级和三点探针 | 是 |
| [Blackbox V2](blackbox_v2/README.md) | V2 专属数据、生产准备和证据边界 | 是 |
| [Native V1](native_v1/README.md) | 存量方案维护 | 仅存量维护 |
| [统一后续推进计划](TODO.md) | 当前未批准工作的顺序与停止条件 | 是 |

## 状态规则

| 状态 | 含义 |
|---|---|
| `CURRENT` | 当前有效规范、索引或操作手册 |
| `LEGACY_MAINTENANCE` | 只适用于 Native V1 存量维护 |
| `BLOCKED_DRAFT` | 未批准草案，不得被 CURRENT 文档作为操作入口 |
| `HISTORICAL` | 历史证据，不得被 CURRENT 文档作为操作入口 |

## 维护规则

1. 只保留提供独有决策或操作边界的目录入口；纯链接目录不另建 README。实施计划完成后从工作树删除，通过 Git 历史追溯。
2. 当前稳定事实只写入 `CURRENT_STATUS.md`；所有未闭环工作和具体方案问题只写入 `TODO.md`，解决并验收后删除。
3. 通用 SOP 不记录具体方案、generation、snapshot 或 Harness run。
4. 历史记录不反向定义当前规则；已被现行入口完整替代的过期文档从工作树删除，通过 Git 历史追溯。
5. 文档移动必须同步更新相对链接，并通过文档门禁测试。
6. `AGENTS.md` 是根规范唯一来源；`CLAUDE.md` 只引导读取，不复制规范正文。
7. CURRENT 文档不得依赖 `docs/superpowers/`、历史 status/handoff 或 `BLOCKED_DRAFT/HISTORICAL` 文档定义当前规则。
