# Bond Factor Lab 文档中心

**文档状态**：`CURRENT`

**目标读者**：所有项目参与者

**最后核验日期**：2026-07-21

本文是仓库文档的唯一总入口。这里不复制方案数量、运行 ID 或生命周期现状；动态事实统一查看[当前状态](CURRENT_STATUS.md)。

## 按角色进入

| 读者 | 唯一入口 |
|---|---|
| 外部客户、业务负责人 | [灰度实验室说明手册](product/GRAY_LAB_USER_MANUAL.md) |
| 上游算法工程师 | [Blackbox V2 上游交付 SOP](sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md) |
| 平台入库和审计人员 | [方案入库统一入口](onboarding/README.md) |
| 平台开发人员 | [架构与契约](architecture/README.md) |
| 平台运维人员 | [运维文档](operations/README.md) |
| 项目负责人 | [当前状态](CURRENT_STATUS.md) |

## 文档域

| 文档域 | 内容 | 是否定义当前规则 |
|---|---|---|
| [入库导航](onboarding/README.md) | 判断使用 Blackbox V2 新增还是 Native V1 存量维护 | 是 |
| [SOP](sop/README.md) | 上游交付、平台入库和 Native 存量维护步骤 | 是 |
| [架构与契约](architecture/README.md) | 系统架构、代码边界、日期语义和共享契约 | 是 |
| [产品文档](product/README.md) | 用户手册和产品需求记录 | 以文档状态为准 |
| [运维文档](operations/README.md) | 环境和部署资料 | 以文档状态为准 |
| [Blackbox V2](blackbox_v2/README.md) | V2 专属数据、生产准备和试验记录 | 规范与记录分开 |
| [Native V1](native_v1/README.md) | 存量方案维护及冻结前历史 | 仅存量维护 |
| [审计与状态记录](records/README.md) | 带日期的状态、审计和系统检查 | 否 |
| [历史入口](archive/README.md) | 已退出当前阅读路径的兼容页 | 否 |
| [内部设计记录](internal/README.md) | 实施计划与设计过程 | 否 |

## 状态规则

| 状态 | 含义 |
|---|---|
| `CURRENT` | 当前有效规范、索引或操作手册 |
| `LEGACY_MAINTENANCE` | 只适用于 Native V1 存量维护 |
| `BLOCKED_DRAFT` | 前置能力未完成，不可作为操作 SOP |
| `HISTORICAL` | 历史证据、计划或废弃规则，不得用于当前验收 |

## 维护规则

1. 每个包含 Markdown 的目录必须有 `README.md`，并登记本层文档和子目录。
2. 动态状态只写入 `CURRENT_STATUS.md`；带日期的详情进入 `records/`。
3. 通用 SOP 不记录具体方案、generation、snapshot 或 Harness run。
4. 历史记录不反向定义当前规则；过期文档必须标明替代入口。
5. 文档移动必须同步更新相对链接，并通过文档门禁测试。
6. `AGENTS.md` 与 `CLAUDE.md` 必须保持字节一致。
