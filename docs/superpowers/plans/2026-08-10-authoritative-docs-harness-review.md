# 权威文档收敛与 Harness 评审实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除已实施/陈旧过程文档，统一当前文档权威口径，快进同步并推送开发分支与 `master`，再完成 Harness 只读深度评审。

**Architecture:** 当前规则只存在于根规范、CURRENT 架构/SOP、CURRENT_STATUS、TODO 和问题台账。过程计划完成后从工作树删除，历史由 Git 追溯；Harness 本轮只评审不修改。

**Tech Stack:** Markdown、Python 3.12、pytest、Git。

---

### Task 1: 建立文档权威门禁

**Files:**
- Modify: `tests/test_onboarding_docs.py`

- [ ] 增加 CURRENT 文档不得依赖 `docs/superpowers` 或 `docs/records/status` 的通用断言。
- [ ] 运行新用例，确认因现有 CURRENT 链接而 RED。
- [ ] 不为单个旧文件名建立永久事故测试。

### Task 2: 删除过程文档并统一当前文档

**Files:**
- Delete: `docs/superpowers/**/*.md`
- Delete: `docs/records/status/*.md`
- Modify: `docs/README.md`, `docs/CURRENT_STATUS.md`, `docs/TODO.md`
- Modify: current architecture/onboarding/SOP/index documents as required by link readback
- Modify: `AGENTS.md`, `CLAUDE.md` only if current authority links change
- Modify: `schemes/weekly_10y_d_overlay_0529/core/d_overlay.py`, `tests/test_weekly_10y_d_overlay_stable_order.py` comments only

- [ ] 删除已批准范围内的过程文件。
- [ ] 将仍有效约束收敛到 CURRENT 文档，删除历史 SHA、数量和完成清单。
- [ ] 将 `PRODUCTION_READINESS.md` 精简为 CURRENT 检查清单。
- [ ] 更新所有相对链接和索引，使新门禁 GREEN。

### Task 3: 验证、提交与双分支发布

**Files:**
- Verify only: repository-wide docs and tests

- [ ] 运行文档/架构测试、静态冲突搜索、`git diff --check` 和全量 pytest。
- [ ] 提交开发分支并推送。
- [ ] 只在 `master` 可 fast-forward 时同步，重新运行关键门禁后推送 `master`。
- [ ] 精确读回本地与远程两个分支 SHA 相同；不修改 plist、launchd、服务或数据库。

### Task 4: Harness 只读深度评审

**Files:**
- Read only: `harness/**/*.py`, callers, tests and current Harness architecture

- [ ] 建立文件职责、入口、调用者和测试矩阵。
- [ ] 识别无调用者、重复校验、重复 hash/receipt、过度 fallback、过大模块和历史兼容路径。
- [ ] 对每个候选检查业务必要性与失败暴露价值，排除仅因文件大而拆分的伪问题。
- [ ] 按“现象/原因/第一性原理最小方案”输出，经评审建议不在本次实施。
