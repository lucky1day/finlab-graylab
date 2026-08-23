# Bond Factor Lab 文档中心

**文档状态**：`CURRENT`

**目标读者**：所有项目参与者

**最后核验日期**：2026-08-23

本文是仓库文档的唯一总入口。这里不复制运行 ID、单次实验或生产时点状态；当前稳定事实查看
[当前状态](CURRENT_STATUS.md)，未批准工作的顺序和边界查看[统一后续推进计划](TODO.md)，生产调度当前规则查看
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。

## 按角色进入

| 读者 | 唯一入口 |
|---|---|
| 外部客户、业务负责人 | [灰度实验室说明手册](product/GRAY_LAB_USER_MANUAL.md) |
| 上游算法工程师 | [Blackbox V2 上游交付 SOP](sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md) |
| 平台入库和审计人员 | [方案入库统一入口](onboarding/README.md) |
| 平台开发人员 | [架构与契约](architecture/README.md) |
| 平台运维人员 | [运维文档](operations/README.md) |
| 项目负责人 | [当前状态](CURRENT_STATUS.md)、[统一后续推进计划](TODO.md)和[未关闭问题台账](records/SCHEME_ISSUE_LEDGER.md) |

## 文档域

| 文档域 | 内容 | 是否定义当前规则 |
|---|---|---|
| [入库导航](onboarding/README.md) | 判断使用 Blackbox V2 新增还是 Native V1 存量维护 | 是 |
| [SOP](sop/README.md) | 上游交付、平台入库和 Native 存量维护步骤 | 是 |
| [架构与契约](architecture/README.md) | 系统架构、Mac3 launchd / ECS systemd 调度治理、代码边界、日期语义和共享契约 | 是 |
| [产品文档](product/README.md) | 当前用户手册 | 是 |
| [运维文档](operations/README.md) | 当前运行与验收资料 | 是 |
| [Blackbox V2](blackbox_v2/README.md) | V2 专属数据、生产准备和证据边界 | 是 |
| [Native V1](native_v1/README.md) | 存量方案维护 | 仅存量维护 |
| [统一后续推进计划](TODO.md) | 当前未批准工作的顺序与停止条件 | 是 |
| [问题记录](records/README.md) | 当前未关闭问题；历史证据由 Git 和控制面保存 | 否 |

## 状态规则

| 状态 | 含义 |
|---|---|
| `CURRENT` | 当前有效规范、索引或操作手册 |
| `LEGACY_MAINTENANCE` | 只适用于 Native V1 存量维护 |
| `BLOCKED_DRAFT` | 未批准草案，不得被 CURRENT 文档作为操作入口 |
| `HISTORICAL` | 历史证据，不得被 CURRENT 文档作为操作入口 |

## 维护规则

1. 每个包含 Markdown 的目录必须有 `README.md`，并登记本层文档和子目录。实施计划完成后从工作树删除，通过 Git 历史追溯。
2. 当前稳定事实只写入 `CURRENT_STATUS.md`；未批准工作的排序只写入 `TODO.md`；具体方案问题统一写入 `records/SCHEME_ISSUE_LEDGER.md`，解决并验收后从当前台账删除。
3. 通用 SOP 不记录具体方案、generation、snapshot 或 Harness run。
4. 历史记录不反向定义当前规则；已被现行入口完整替代的过期文档从工作树删除，通过 Git 历史追溯。
5. 文档移动必须同步更新相对链接，并通过文档门禁测试。
6. `AGENTS.md` 与 `CLAUDE.md` 必须保持字节一致。
7. CURRENT 文档不得依赖 `docs/superpowers/`、历史 status/handoff 或 `BLOCKED_DRAFT/HISTORICAL` 文档定义当前规则。
