# 当前文档与 Blackbox SOP 收敛设计（2026-08-06）

**状态**：`APPROVED`

## 目标

在不改变算法、Registry、数据库、Harness、launchd 或 installed plist 的前提下，清理两份已被现行行为完整替代的前端 cutover 草案，并将 Blackbox V2 的“上游两文件交付”和“平台精确调度准入”边界写成可执行、可测试的当前 SOP。

## 已确认的范围

1. 只删除以下两份 `SUPERSEDED_DRAFT`：
   - `docs/superpowers/specs/2026-08-05-factor-lab-live-backtest-cutover-design.md`
   - `docs/superpowers/plans/2026-08-05-factor-lab-live-backtest-cutover.md`
2. 保留 G3.1、D0、D1、G7/G8 和所有仍有审计或当前规范价值的记录。
3. 更新上游交付 SOP、平台入库 SOP、SOP 索引、D0 审计、当前状态与统一 TODO；为关键边界补充文档契约测试。
4. 只提交并推送当前开发分支 `codex/audit-bugfixes-20260613`；不合并或推送 `master`。

## 设计决策

### 1. 上游交付与平台权限严格分离

上游的唯一交付物仍是同名 `{scheme_id}.py + {scheme_id}.json`。这两个文件、上游自验、DataBridge 下载凭证和 Intake 成功都不授予 activation、灰度写入、scheduler admission、`launchd_one_shot`、installed plist 或服务操作权限。上游不得把这些平台控制面字段或权限声明写入 Metadata，也不得以交付文件代替平台审批。

所有新增算法、新 ID、新目标、新 task type 与替代版本继续只走 Blackbox V2；Native V1 只用于既有政策清单身份的维护。

### 2. 精确 scheduler admission 是独立平台动作

平台 SOP 将 scheduler admission 定义为单独的版本化仓库策略，而不是 Intake、Gate、`shadow + paused`、activation、持久化回测、`gray_live` 或 API/前端验收的副作用。准入必须同时在 `scheduler/blackbox_scheduler_admission.py` 和 `deploy/blackbox_scheduler_admission_v1.json` 的 frozen map 中保持完全一致，并按以下精确身份字段核验：

```text
scheme_id + scheme_version + runtime_type + frequency
+ task_type + horizon + target_tenor
```

对新的或变更的 Blackbox 准入，目标 capability 只能是 `{launchd_one_shot}`。不得把 `mode=formal` 当作 legacy、ledger 或 direct 的隐式许可；不得新增或扩大 `legacy_automatic`、`daily_ledger`、`direct_scheduled`。历史条目仅作为 G8 的逐 identity 迁移/退役对象，不能成为新准入先例。

该仓库准入变更仍不安装 plist、不运行 `launchctl`、不重启服务，也不证明自然时钟已经现场产生 `scheduled_live`。历史补缺只能在独立授权下 insert-only 写 `gray_live`，不能倒签为 `scheduled_live`。

### 3. 文档生命周期收口

D0 保留为历史审计证据，但把两份候选更新为“已按确认删除”，同时记录删除前引用核验结论。`CURRENT_STATUS.md` 与 `TODO.md` 将 D0 状态推进为完成，并明确本次删除不涉及其它状态/治理记录。SOP 索引更新为 2026-08-06，并将两类职责和 scheduler admission 的独立性写入入口说明。

## 验收标准

- 删除后仓库中不存在两份已确认的 superseded 草案，且所有 Markdown 相对链接仍可解析。
- 上游 SOP 明确：两文件交付不授予平台控制面权限，且不包含 platform-only admission 信息。
- 平台 SOP 明确：scheduler admission 的精确身份、Python/JSON parity、仅 `launchd_one_shot` 的新变更能力、legacy/ledger/direct 禁扩张，以及它不等于现场安装或自然调度观测。
- 文档测试覆盖上述关键措辞；全量现有 onboarding-docs 测试、`compileall`、`git diff --check` 通过。
- 提交中不包含根工作区的既有用户修改、`reports/`、`.superpowers/` 或诊断脚本。

## 非目标

不修改 admission 代码或 JSON、不新增任何身份、不改生产权限、不写业务数据库、不执行 DataBridge refresh、launchd、plist、服务、DDL、迁移或 Git `master` 发布。
