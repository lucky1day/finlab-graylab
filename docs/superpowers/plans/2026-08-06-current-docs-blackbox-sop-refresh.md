# 当前文档与 Blackbox SOP 收敛 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除两份经确认的 superseded cutover 草案，并把 Blackbox V2 上游交付、平台精确 scheduler admission 和文档生命周期边界收敛为可测试的当前文档。

**Architecture:** 只变更文档和文档契约测试。上游 SOP 只定义两文件交付与算法责任；平台 SOP 单独定义按精确身份核验的仓库 admission，明确它既不授予现场控制面，也不把灰度补写伪称为自然调度。D0 记录保留为历史证据，状态入口同步标记已执行的两文件删除。

**Tech Stack:** Markdown、Python `unittest`、`pytest`、Git。

---

### Task 1: 先锁定两个 SOP 的职责边界测试

**Files:**
- Modify: `tests/test_onboarding_docs.py:1332-1360`

- [ ] **Step 1: 写入失败的文档契约测试**

在 `test_blackbox_production_boundary_is_explicit()` 后新增：

```python
    def test_blackbox_sops_separate_delivery_from_exact_scheduler_admission(self) -> None:
        upstream = UPSTREAM_SOP.read_text(encoding="utf-8")
        platform = PLATFORM_SOP.read_text(encoding="utf-8")

        for marker in (
            "交付不授予平台控制面权限",
            "不授予 activation、灰度写入、scheduler admission",
            "不得把 scheduler admission、`launchd_one_shot`",
        ):
            self.assertIn(marker, upstream)

        for marker in (
            "精确 scheduler admission 是独立的仓库策略",
            "scheduler/blackbox_scheduler_admission.py",
            "deploy/blackbox_scheduler_admission_v1.json",
            "`launchd_one_shot`",
            "`legacy_automatic`、`daily_ledger`、`direct_scheduled`",
            "不安装 plist、不运行 `launchctl`、不重启服务",
        ):
            self.assertIn(marker, platform)
```

- [ ] **Step 2: 运行测试，确认在文档更新前失败**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest -q tests/test_onboarding_docs.py::OnboardingDocumentationTests::test_blackbox_sops_separate_delivery_from_exact_scheduler_admission
```

Expected: FAIL，因为两个 SOP 尚未包含这些新边界句。

### Task 2: 收敛两个 Blackbox V2 SOP 和入口说明

**Files:**
- Modify: `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md:12-95,680-704`
- Modify: `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md:1-17,222-238,710-742`
- Modify: `docs/sop/README.md:7-27,43-58`

- [ ] **Step 1: 在上游 SOP 的第 1.3 节后新增交付权限边界**

新增 `### 1.4 交付不授予平台控制面权限`，明确以下文本语义：

```text
两文件交付、DataBridge 自验凭证和 Intake 成功只证明交付可被平台接收；
它们不授予 activation、灰度写入、scheduler admission、launchd_one_shot、installed plist 或服务操作。
上游不得在 Metadata 或交付目录中声明上述平台权限，也不得把两文件交付当作平台审批。
```

同时在最终检查增加两项：交付不包含平台控制面字段；交付方已将 scheduler admission、`launchd_one_shot`、installed plist 与服务操作保留给平台专项流程。

- [ ] **Step 2: 在平台 SOP 的第 2.4 节后新增精确 admission 小节**

新增 `### 2.5 精确 scheduler admission 是独立的仓库策略`，包含以下准确规则：

```text
Intake、Gate、shadow + paused、activation、持久化回测、gray_live 和 API/前端验收均不自动授予 scheduler admission。
变更必须令 scheduler/blackbox_scheduler_admission.py 与 deploy/blackbox_scheduler_admission_v1.json 的 frozen map 完全一致，
并按 scheme_id、scheme_version、runtime_type、frequency、task_type、horizon、target_tenor 的组合核验。
新的或变更的 Blackbox 准入 capability 只能为 {launchd_one_shot}；不得将 mode=formal 当作 legacy、ledger 或 direct 的许可，
也不得新增或扩大 legacy_automatic、daily_ledger、direct_scheduled。
仓库 admission 仍不安装 plist、不运行 launchctl、不重启服务，也不证明自然时钟已产生 scheduled_live；历史补缺只写 gray_live。
```

在第 9 节最终检查增加 scheduler admission 的独立审查、Python/JSON parity、只含 `launchd_one_shot` 的新变更能力和现场操作分离检查。将两个 SOP 的最后核验日期更新为 2026-08-06。

- [ ] **Step 3: 更新 SOP 索引**

将 `docs/sop/README.md` 的最后核验日期更新为 2026-08-06；把平台 SOP 的用途写为“收包、Gate、精确 scheduler admission 的独立审查、专项生产灰度和前端验收”；在场景决策或维护规则中明确上游两文件交付不等于平台 activation、scheduler admission 或现场生产操作。

- [ ] **Step 4: 运行 Task 1 测试，确认通过**

Run the Task 1 command.

Expected: PASS。

### Task 3: 执行精确文档清理并更新当前状态入口

**Files:**
- Delete: `docs/superpowers/specs/2026-08-05-factor-lab-live-backtest-cutover-design.md`
- Delete: `docs/superpowers/plans/2026-08-05-factor-lab-live-backtest-cutover.md`
- Modify: `docs/records/status/D0_DOCUMENT_LIFECYCLE_AUDIT_20260806.md:40-51`
- Modify: `docs/records/status/README.md:9-16`
- Modify: `docs/TODO.md:132-148`
- Modify: `docs/CURRENT_STATUS.md:55-70`

- [ ] **Step 1: 删除唯一已确认的两份 superseded 草案**

只删除以下文件，不删除任何其它 `docs/superpowers`、`docs/records`、G3.1、G7、G8 或 D1 文档：

```text
docs/superpowers/specs/2026-08-05-factor-lab-live-backtest-cutover-design.md
docs/superpowers/plans/2026-08-05-factor-lab-live-backtest-cutover.md
```

- [ ] **Step 2: 将 D0 从待确认改为已执行的审计事实**

将 D0 的候选小节改为“已确认并执行的删除（2026-08-06）”，保留两个路径、删除前仅互相引用/审计列举的入站引用结论，以及“没有其它文档被删除”的界限。不得重写其分类矩阵或删除审计记录。

- [ ] **Step 3: 同步状态入口**

将状态记录索引改为“已确认并执行的两份 superseded draft 删除”；将 TODO 的 D0 改为 `COMPLETE`，把所有待确认/待删除 checkbox 改为已完成；将当前状态的 D0 摘要改为已按确认删除两份草案、保留 G3.1 和当前规范/证据，且不影响 G7/G8/D1 的独立状态。

- [ ] **Step 4: 核对删除后的入站链接**

Run:

```bash
git grep -n -F '2026-08-05-factor-lab-live-backtest-cutover-design.md' || true
git grep -n -F '2026-08-05-factor-lab-live-backtest-cutover.md' || true
```

Expected: 只剩 D0 审计中作为已删除证据的文字路径；没有可解析的 Markdown 链接或运行时引用。

### Task 4: 全量文档验证、提交与开发分支集成

**Files:**
- Verify: `tests/test_onboarding_docs.py`
- Verify: 所有上述改动文件

- [ ] **Step 1: 运行完整文档契约测试和编译检查**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest -q tests/test_onboarding_docs.py
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n bond_factor_lab_service \
  python -m compileall -q docs tests/test_onboarding_docs.py
git diff --check
```

Expected: 所有文档测试通过、`compileall` 退出码为 0、没有 whitespace error。

- [ ] **Step 2: 审查提交范围并创建实现提交**

Run:

```bash
git status --short
git diff --stat
git diff --check
```

暂存仅本计划列出的文档和 `tests/test_onboarding_docs.py`，创建提交：

```bash
git commit -m "docs: clarify blackbox intake and admission boundaries"
```

- [ ] **Step 3: 在根开发分支串行集成并推送**

确认根工作区仍只含预先存在的用户文件后，cherry-pick 本工作树的设计提交和实现提交到 `codex/audit-bugfixes-20260613`。随后只推送该开发分支：

```bash
git push -u origin codex/audit-bugfixes-20260613
```

不得 merge、push 或修改 `master`；若远程开发分支已分叉，停止并报告精确分叉状态。
