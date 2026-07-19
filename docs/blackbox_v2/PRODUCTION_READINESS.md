# Blackbox V2 生产晋级条件

**文档状态**：`BLOCKED_DRAFT`
**适用运行时**：`blackbox_v2`
**目标读者**：平台开发、运维、风险控制和授权人员
**最后核验日期**：2026-07-19

本文不是可执行的生产 SOP。它只列出 Blackbox V2 从 `shadow + paused` 晋级为 `active/live` 前必须完成并验证的阻塞项。

> 在全部条件完成、代码验证通过并另行批准为 `CURRENT` 生产 SOP 前，任何 Blackbox V2 方案最多进入 `shadow + paused`，不得激活、不得进入正式调度、不得写业务预测或回测表。

## 1. 当前边界

当前已具备：

- 两文件 Intake、Metadata 和目录身份校验。
- DataBridge 三频同代快照及 Request 生成。
- 七个自动 Gate 和无业务落库技术验收。
- 经授权登记 `shadow + paused` 的控制面流程。

当前未具备可宣称生产就绪的闭环：

- 自动 Gate 证据尚未完整绑定数据 generation freshness。
- Shadow 登记存在多步状态，失败后依赖人工 reconciliation。
- activate/live 仍缺少 Blackbox 专用代码 hard-stop 和生产探针。

## 2. 必须完成的阻塞项

| 编号 | 前置条件 | 完成标准 | 必需证据 |
|---|---|---|---|
| PR-01 | Gate 绑定 generation/freshness | 每个 Input、DryRun、Compare、Backtest 报告自包含 `generation_id`、`refresh_date`、`refreshed_at`、business digest 和三文件 SHA256，并区分 onboarding 最新可用代与 scheduled-live 当日代 | 自动测试、真实 dry-run 报告、stale generation 失败样例 |
| PR-02 | Runtime Profile 成为唯一配置源 | Python、依赖、资源、超时、文件/环境权限全部只从版本化 Runtime Profile 解析，SOP 与代码不另设隐式默认值 | Profile schema、环境指纹、自检报告和漂移失败测试 |
| PR-03 | Harness 审计持久化 fail-closed | shadow 授权前必须确认最新 Harness run、scheme version、Gate 证据和审计 DB 记录一致；审计不可用时禁止继续 | DB 故障测试、授权拒绝测试、审计查询结果 |
| PR-04 | Shadow 原子化或自动 reconciliation | 版本、Registry 和配置状态要么单事务成功，要么失败后由受测工具自动恢复到一致状态 | 故障注入测试、reconciliation 报告、幂等重试测试 |
| PR-05 | Blackbox 专用 activate/live 门禁 | 只有 `runtime_type=blackbox_v2` 且满足生产批准版本的方案可激活；未批准 trial 必须在 ActivationGate、scheduler 和人工触发三处 fail-closed | 未批准激活失败、绕过尝试失败、批准路径测试 |
| PR-06 | 真实 Registry/API/scheduler 探针 | ApiReadiness 不仅检查结构，还只读验证 Registry 可见性、API composite ID、scheduler 发现与目标完整性；探针必须区分 shadow 和 active 预期 | 真实探针报告、错误状态失败测试 |
| PR-07 | JSON Result 严格整数 | `prediction.json` 的 `predicted_direction` 只接受 JSON integer `-1/0/1`；字符串、布尔值、浮点数全部拒绝。CSV 仍按解析后的文本枚举验收 | 契约单测和恶意输入样例 |
| PR-08 | Sandbox 读取与环境收紧 | 文件读取 allowlist 仅包含交付脚本、Request、三频快照、必要解释器/依赖；清除非必要继承环境变量，禁止网络、数据库、凭据、仓库其他文件和任意写路径 | sandbox 集成测试、网络/路径/环境逃逸失败样例 |

## 3. 生产 SOP 形成条件

上述八项全部完成后，仍需单独完成：

1. 在隔离测试环境运行完整 Intake、Gate、shadow、activate、scheduled-live 和失败恢复演练。
2. 验证 active Registry、scheduler、PredictionRecord、actual join、API 和前端的同一目标闭环。
3. 验证 DataBridge 当日刷新失败、算法超时、Result 非法和写库失败均不产生部分业务结果。
4. 验证回退只暂停 Blackbox 版本，不修改 Native 存量方案和历史记录。
5. 形成新的 Blackbox V2 生产晋级 SOP，经过业务、平台和运维批准后将其标记为 `CURRENT`。

本文件在此之前保持 `BLOCKED_DRAFT`，不得通过修改文字状态替代代码、测试和授权证据。

## 4. 每次复核记录

复核只在本表追加，不覆盖历史结论：

| 日期（Asia/Shanghai） | 复核范围 | 完成项 | 未完成项 | 结论 |
|---|---|---|---|---|
| 2026-07-19 | 初始治理基线 | Intake 至 shadow 文档边界 | PR-01 至 PR-08 | 阻塞，维持 shadow + paused |
