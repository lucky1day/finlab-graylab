# 第二阶段保守清理实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除一个已完成的一次性回填脚本和一个空内部计划索引，不影响任何当前生产或复现职责。

**Architecture:** 先以调用与生命周期证据限定删除范围，再做文件和导航闭环删除。最终清除本轮设计/计划记录，并以完整测试和限定目录 diff 证明生产实现未变。

**Tech Stack:** Bash、Python 3.12、pytest、Git、Markdown。

---

### Task 1: 删除一次性回填脚本

**Files:**
- Delete: `scripts/backfill_live_predictions.sh`

- [ ] **Step 1: 复核当前调用者**

Run:

```bash
rg -n 'backfill_live_predictions(.sh)?' . --glob '!outputs/**' --glob '!reports/**'
```

Expected: 除脚本自身外只命中 `docs/records/audits/OPS_AUDIT_2026-07-06.md` 的历史描述。

- [ ] **Step 2: 删除脚本**

使用 `apply_patch` 删除 `scripts/backfill_live_predictions.sh`，不修改历史审计快照。

- [ ] **Step 3: 检查脚本门禁**

Run:

```bash
conda run -n bond_factor_lab_service python -m compileall -q scripts tests
```

Expected: 退出码 0。

### Task 2: 删除空计划索引

**Files:**
- Delete: `docs/internal/plans/README.md`
- Modify: `docs/internal/README.md`

- [ ] **Step 1: 确认索引为空**

Run:

```bash
git ls-files docs/internal/plans
```

Expected: 最终工作记录删除前只有本计划和 `README.md`；计划完成后两者都删除。

- [ ] **Step 2: 删除空索引导航**

使用 `apply_patch` 删除 `docs/internal/README.md` 的“实施计划”导航项，并在最终收尾时删除整个 tracked plans 内容。

- [ ] **Step 3: 运行文档门禁**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q tests/test_onboarding_docs.py
```

Expected: 全部通过。

### Task 3: 清除中间记录并最终验证

**Files:**
- Delete: `docs/internal/specs/2026-08-01-conservative-cleanup-phase-2-design.md`
- Delete: `docs/internal/plans/2026-08-01-conservative-cleanup-phase-2.md`
- Modify: `docs/internal/specs/README.md`

- [ ] **Step 1: 删除本轮设计、计划及其索引行**

使用 `apply_patch` 删除两个中间记录，移除 specs 索引行；plans 索引随 Task 2 一并删除。

- [ ] **Step 2: 执行完整验证**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q
conda run -n bond_factor_lab_service python -m pytest --collect-only -q
conda run -n bond_factor_lab_service python -m compileall -q scripts docs tests
git diff --check
```

Expected: pytest 全通过，收集数与本批基线一致，编译和 diff 检查退出码 0。

- [ ] **Step 3: 核验生产边界并提交**

Run:

```bash
git diff --exit-code 8a7a6d0..HEAD -- scheduler harness backend shared backtests schemes deploy migrations
git status --short --branch
```

Expected: 限定目录无差异，最终工作树干净，当前分支仍为 `codex/audit-bugfixes-20260613`。
