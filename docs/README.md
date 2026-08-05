# Bond Factor Lab 文档中心

**文档状态**：`CURRENT`

**目标读者**：所有项目参与者

**最后核验日期**：2026-08-06

本文是仓库文档的唯一总入口。这里不复制运行 ID、单次实验或生产时点状态；已验证
的动态事实统一查看[当前状态](CURRENT_STATUS.md)，未完成工作的优先级统一查看
[TODO](TODO.md)，生产调度当前规则统一查看
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。

## 按角色进入

| 读者 | 唯一入口 |
|---|---|
| 外部客户、业务负责人 | [灰度实验室说明手册](product/GRAY_LAB_USER_MANUAL.md) |
| 上游算法工程师 | [Blackbox V2 上游交付 SOP](sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md) |
| 平台入库和审计人员 | [方案入库统一入口](onboarding/README.md) |
| 平台开发人员 | [架构与契约](architecture/README.md) |
| 平台运维人员 | [运维文档](operations/README.md) |
| 项目负责人 | [当前状态](CURRENT_STATUS.md)、[TODO](TODO.md)和[全方案问题台账](records/SCHEME_ISSUE_LEDGER.md) |

## 文档域

| 文档域 | 内容 | 是否定义当前规则 |
|---|---|---|
| [入库导航](onboarding/README.md) | 判断使用 Blackbox V2 新增还是 Native V1 存量维护 | 是 |
| [SOP](sop/README.md) | 上游交付、平台入库和 Native 存量维护步骤 | 是 |
| [架构与契约](architecture/README.md) | 系统架构、launchd-only 调度治理、代码边界、日期语义和共享契约 | 是 |
| [产品文档](product/README.md) | 用户手册和产品需求记录 | 以文档状态为准 |
| [运维文档](operations/README.md) | 环境和部署资料 | 以文档状态为准 |
| [Blackbox V2](blackbox_v2/README.md) | V2 专属数据、生产准备和试验记录 | 规范与记录分开 |
| [Native V1](native_v1/README.md) | 存量方案维护 | 仅存量维护 |
| [当前优先级与待办](TODO.md) | 未完成工作、前置依赖与排序 | 是 |
| [审计与状态记录](records/README.md) | 全方案问题台账、带日期的状态、审计和系统检查；不定义当前生产控制面 | 否 |
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
2. 已验证的动态状态只写入 `CURRENT_STATUS.md`；未完成工作的排序只写入 `TODO.md`；带日期的详情进入 `records/`；具体方案问题统一写入 `records/SCHEME_ISSUE_LEDGER.md`，解决并验收后从当前台账删除。
3. 通用 SOP 不记录具体方案、generation、snapshot 或 Harness run。
4. 历史记录不反向定义当前规则；已被现行入口完整替代的过期文档从工作树删除，通过 Git 历史追溯。
5. 文档移动必须同步更新相对链接，并通过文档门禁测试。
6. `AGENTS.md` 与 `CLAUDE.md` 必须保持字节一致。
7. 带日期的 status/handoff 记录只能保存证据；当前生产调度规则以
   [生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)为准。
