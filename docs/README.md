# Bond Factor Lab 文档中心

**文档状态**：`CURRENT`

[项目概览](../README.md)介绍用途，[项目根规范](../AGENTS.md)维护全局约束与权限。已读根规范后，按下表选择当前任务；无需依次加载所有专题。本文维护导航、权威归属与文档维护方法，不定义业务合同或操作流程。

## 按任务查找

| 要完成的任务 | 阅读顺序 |
|---|---|
| 了解部署与剩余工作 | [当前状态](CURRENT_STATUS.md) → [待办](TODO.md)；现场可能已变化，操作前重新读回 |
| 接收或修订算法 | [入库导航](onboarding/README.md) → Blackbox 上游/平台 SOP → [接管清单](blackbox_v2/PRODUCTION_READINESS.md) |
| 维护 W4 固定版本运行 | [W4 运行维护](sop/NATIVE_V1_MAINTENANCE_SOP.md) → 调度治理；不进入 Native 版本修订、准入或再激活 |
| 修改平台实现 | [代码架构](architecture/CODE_ARCHITECTURE.md) → [共享契约](architecture/SCHEME_CONTRACT.md) → 对应运行时合同 |
| 判断算法改动或迁移边界 | [源算法保真](architecture/SOURCE_ALGORITHM_FIDELITY.md) → 入库导航中的适用流程 |
| 检查日期、历史缺口或指标 | [预测语义](architecture/PREDICTION_SEMANTICS.md) → [平台入库 SOP](sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)的补缺步骤 |
| 发布、晋级或恢复 | [当前状态](CURRENT_STATUS.md) → [部署访问](operations/DEPLOYMENT_ACCESS.md) → [部署手册](../deploy/README.md) → [调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md) |
| 核验自然运行或处理漏跑 | [待办](TODO.md) → [部署访问](operations/DEPLOYMENT_ACCESS.md) → [调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)；模拟、补缺与自然证据分别核验 |
| 排查 Dashboard | [Dashboard 合同与运行验收](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md) → 部署访问中的探针位置；指标疑问查预测语义 |
| 账户初始化或恢复 | [认证合同](architecture/AUTHENTICATION_AND_ACCOUNT_MANAGEMENT.md) → 部署访问中的目标环境 → 合同链接的受控 CLI |
| 使用页面或解释结果 | [产品手册](product/GRAY_LAB_USER_MANUAL.md) |

## 权威来源与职责

| 权威文档 | 维护内容 |
|---|---|
| [AGENTS.md](../AGENTS.md) | 全局约束、权限和不可破坏的不变量；[CLAUDE.md](../CLAUDE.md)只转向此文件 |
| 本文 | 任务导航、文档职责、维护与验收方法；[项目概览](../README.md)只介绍项目与入口 |
| [CURRENT_STATUS.md](CURRENT_STATUS.md) | 带核验日期的 release、部署/数据事实、已知限制与必要证据索引 |
| [TODO.md](TODO.md) | 尚未完成事项、下一步及完成标准 |
| [代码架构](architecture/CODE_ARCHITECTURE.md) | 模块职责、依赖图、运行时调用关系；物理 schema 以 [migrations](../migrations/)为准 |
| [共享方案契约](architecture/SCHEME_CONTRACT.md) | 身份、版本、任务、标准平台记录与生命周期边界 |
| [Native 接口](native_v1/SCHEME_CONTRACT.md) | W4 固定版本的 adapter/core/config 运行接口与依赖 |
| [上游交付 SOP](sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md) | 可外发的完整两文件交付、输入、Metadata、Request/Result、自测合同及随包资源要求 |
| [DataBridge](blackbox_v2/data_bridge_v1/README.md) | schema/样例入口、因子版本封版、legacy 输入保护；[数据目录说明](../data/data_bridge/README.md)仅说明运行资产位置 |
| [预测语义](architecture/PREDICTION_SEMANTICS.md) | 三日期、日历、事实键、Actual、分区和统计定义 |
| [源算法保真](architecture/SOURCE_ALGORITHM_FIDELITY.md) | L0/L1/L2、历史口径、迁移等价与旧依赖退役边界 |
| [Harness 架构](architecture/HARNESS_ARCHITECTURE.md) | Blackbox 验证、命令作用域与事务边界 |
| [入库导航](onboarding/README.md) | 场景选择、阅读顺序及公共验证入口 |
| [平台入库 SOP](sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md) | canonical、回测、激活、补缺及状态重建的操作步骤 |
| [W4 运行维护 SOP](sop/NATIVE_V1_MAINTENANCE_SOP.md) | 固定版本运行检查、故障定位、调度恢复与依赖保护 |
| [Blackbox 生产准备](blackbox_v2/PRODUCTION_READINESS.md) | 生产接管前的证据清单，不代替操作步骤 |
| [部署访问](operations/DEPLOYMENT_ACCESS.md) | 主机角色、固定地址/Origin、SSH/转发、生产路径及只读连接方法 |
| [部署手册](../deploy/README.md) | 环境、release 工具、安装/晋级/恢复、迁移操作及期望时钟与模板映射 |
| [调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md) | 唯一 Writer、installed/loaded 证明、自然运行证据与手工恢复边界 |
| [Dashboard 合同](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md) | HTTP 表示、刷新行为、性能和故障定位/验收 |
| [认证合同](architecture/AUTHENTICATION_AND_ACCOUNT_MANAGEMENT.md) | 账户、会话、权限、认证 API 与恢复范围 |
| [产品手册](product/GRAY_LAB_USER_MANUAL.md) | 面向使用者的页面说明和阅读示例，技术规则引用上述合同 |

## 当前文档与历史证据

- `CURRENT` 表示现行合同、导航或手册；适用范围由各文档正文限定。
- `BLOCKED_DRAFT` 是未批准草案，`HISTORICAL` 是历史记录；两者不能定义当前操作流程或授予权限。
- [source_evidence](../source_evidence/) 下的上游原件与 README 是来源证据，其旧路径、取数和运行指令不适用于平台操作。保留原字节，不按现行文档风格改写。
- 已完成批次的原件、截图、JSON、回执和恢复材料由当前状态链接外置证据；旧实现和已退役计划通过 Git 与 immutable release 追溯，不回到当前操作入口。

## 维护与验收

文档的完成标准是让执行者找到依据、理解适用范围、完成任务并验证结果。逐段判断：**删除后是否降低准确性、增加查找成本、丢失决策依据，或削弱执行、验收与恢复能力？会则保留或迁移；不会且无独立价值则删除。** 不以字数、文件数或形式统一作为目标。

1. **归属与上下文**：完整规则只在职责表对应位置维护，其他位置保留足以选择路径的摘要与链接。业务依据、容易误判的例外和关键依赖不能只因“代码里有”而删除；字段、参数和枚举已有机器契约时链接它，并解释用途。上游外发合同保持独立自洽。
2. **作用域与完成标准**：强制规则写明适用条件、动作或禁止事项、检查入口和通过条件；建议、示例与待验证观点明确标识，不混写为强制要求。文档与实现冲突时先查业务决定、机器合同及现场证据；涉及业务或操作语义的未决调整先确认，不能自动把当前代码当成正确合同。
3. **事实与证据**：主机地址归部署访问，带核验时间的运行事实归当前状态，未完成工作归 TODO。通用合同不记录批次 run/generation 或临时授权。任务完成后删除无价值流水；仍解释业务选择或用于审计、恢复、来源追溯的决定与证据，保留适用范围及可定位索引，不退回当前操作入口。
4. **维护责任与触发**：变更实施者同步更新权威正文及受影响引用，评审者按本节验收。实际变更、重复误判或验证反馈触发修正、替换或退役；不为假设问题追加禁令，不把本轮授权写成永久权限。
5. **工具与限制**：关键不变量优先复用[现有测试与校验器](onboarding/README.md#可复用测试矩阵)，文档解释边界和证据含义；没有对应检查时如实记录缺口，不宣称已自动保障，也不为每句话新增框架。凭据、全量输入和临时报告不进入 Git。
6. **实际验收**：先检查文件/锚点/反链、根规范与局部指令的作用域；再从根入口沿受影响任务实际阅读，确认能找到前置条件、操作入口、通过条件、失败恢复及证据。链接存在不等于可读取或可执行；外置证据需区分本机可读、目标机只读路径与当前不可访问，不能将路径存在标为内容已核验。上游原件另核验字节未改，运行状态不能因文档整理标为完成。

工具的指令加载机制以 [AGENTS.md 配置指南](https://learn.chatgpt.com/docs/agent-configuration/agents-md)和 [CLAUDE.md 组织指南](https://code.claude.com/docs/en/memory#write-effective-instructions)为准；不要把链接的专题正文当成已加载指令，也不要把旧 worktree 的根规范套到当前 checkout。

维护方法参考 [Codex 最佳实践](https://learn.chatgpt.com/guides/best-practices#make-guidance-reusable-with-agentsmd)、[Astra 按需读取与任务完成建议](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra)及 [CLAUDE.md 最佳实践](https://code.claude.com/docs/en/best-practices#write-an-effective-claudemd)。这些资料解释维护方法，不定义项目业务合同或授予操作权限。
