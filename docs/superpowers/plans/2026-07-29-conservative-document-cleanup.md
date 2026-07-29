# Conservative Documentation Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 12 份已退出当前阅读路径的兼容/archive 文档，并让全部现行入口、索引和文档门禁只指向当前权威文档。

**Architecture:** 保留现行架构、SOP、records、internal 和 active Superpowers 文档不动；先用测试锁定待删除路径，再删除文件并同步修正引用。Git 历史承担旧正文追溯，工作树只移除已经被当前入口完整替代的副本。

**Tech Stack:** Markdown、Python `unittest`、Git、现有文档链接门禁

---

### Task 1: 单独收口现有 handoff 修改

**Files:**
- Modify: `docs/records/status/DAILY_SIGNAL_RECOVERY_HANDOFF_20260729.md`

- [ ] **Step 1: 核对 handoff 只包含已确认的性能诊断**

Run:

```bash
git diff -- docs/records/status/DAILY_SIGNAL_RECOVERY_HANDOFF_20260729.md
```

Expected: 只包含 Liwei full rebuild、cache schema/fingerprint、中断重试、
耗时证据和接手顺序，不包含第一批删除。

- [ ] **Step 2: 验证 handoff**

Run:

```bash
conda run -n bond_factor_lab_service \
  python -m unittest tests.test_onboarding_docs
git diff --check
```

Expected: 40 tests passed；`git diff --check` 退出码为 0。

- [ ] **Step 3: 独立提交 handoff**

```bash
git add docs/records/status/DAILY_SIGNAL_RECOVERY_HANDOFF_20260729.md
git commit -m "docs: record daily signal performance diagnosis"
```

Expected: handoff 形成独立提交，工作树不再混有该修改。

### Task 2: 先修改文档门禁并确认红灯

**Files:**
- Modify: `tests/test_onboarding_docs.py`

- [ ] **Step 1: 将历史跳转页测试改为删除路径测试**

把 `test_old_native_entry_paths_are_redirect_only` 替换为：

```python
def test_retired_document_paths_are_absent(self) -> None:
    retired = (
        DOCS_ROOT / "archive" / "README.md",
        DOCS_ROOT / "archive" / "SCHEME_PARADIGM.md",
        DOCS_ROOT / "blackbox_v2" / "archive" / "README.md",
        DOCS_ROOT
        / "blackbox_v2"
        / "archive"
        / "UPSTREAM_DELIVERY_SOP_EXCEL_DRAFT.md",
        DOCS_ROOT / "native_v1" / "archive" / "README.md",
        DOCS_ROOT
        / "native_v1"
        / "archive"
        / "SCHEME_ONBOARDING_SOP_PRE_FREEZE.md",
        DOCS_ROOT
        / "native_v1"
        / "archive"
        / "SCHEME_ONBOARDING_T0_PRE_FREEZE.md",
        DOCS_ROOT
        / "native_v1"
        / "archive"
        / "SCHEME_PARADIGM_DRAFT.md",
        DOCS_ROOT
        / "native_v1"
        / "archive"
        / "SCHEME_POST_ONBOARDING_TEST_SOP_PRE_FREEZE.md",
        DOCS_ROOT / "sop" / "SCHEME_ONBOARDING_SOP.md",
        DOCS_ROOT / "sop" / "SCHEME_ONBOARDING_T0.md",
        DOCS_ROOT / "sop" / "SCHEME_POST_ONBOARDING_TEST_SOP.md",
    )
    self.assertTrue(all(not path.exists() for path in retired))
```

- [ ] **Step 2: 收紧 SOP 索引状态断言**

在 `test_sop_index_covers_the_entire_directory` 中保留完整目录覆盖断言，并将
状态断言改为：

```python
for status in ("CURRENT", "LEGACY_MAINTENANCE"):
    self.assertIn(f"`{status}`", text)
self.assertNotIn("`HISTORICAL`", text)
```

- [ ] **Step 3: 运行测试确认先失败**

Run:

```bash
conda run -n bond_factor_lab_service \
  python -m unittest \
  tests.test_onboarding_docs.OnboardingDocumentationTests.test_retired_document_paths_are_absent \
  tests.test_onboarding_docs.OnboardingDocumentationTests.test_sop_index_covers_the_entire_directory
```

Expected: FAIL，因为 12 份待删除文档仍存在，SOP 索引仍包含
`HISTORICAL`。

### Task 3: 删除第一批文档并修正权威入口

**Files:**
- Delete: `docs/archive/README.md`
- Delete: `docs/archive/SCHEME_PARADIGM.md`
- Delete: `docs/blackbox_v2/archive/README.md`
- Delete: `docs/blackbox_v2/archive/UPSTREAM_DELIVERY_SOP_EXCEL_DRAFT.md`
- Delete: `docs/native_v1/archive/README.md`
- Delete: `docs/native_v1/archive/SCHEME_ONBOARDING_SOP_PRE_FREEZE.md`
- Delete: `docs/native_v1/archive/SCHEME_ONBOARDING_T0_PRE_FREEZE.md`
- Delete: `docs/native_v1/archive/SCHEME_PARADIGM_DRAFT.md`
- Delete: `docs/native_v1/archive/SCHEME_POST_ONBOARDING_TEST_SOP_PRE_FREEZE.md`
- Delete: `docs/sop/SCHEME_ONBOARDING_SOP.md`
- Delete: `docs/sop/SCHEME_ONBOARDING_T0.md`
- Delete: `docs/sop/SCHEME_POST_ONBOARDING_TEST_SOP.md`
- Modify: `docs/README.md`
- Modify: `docs/onboarding/README.md`
- Modify: `docs/sop/README.md`
- Modify: `docs/native_v1/README.md`
- Modify: `docs/blackbox_v2/README.md`
- Modify: `docs/architecture/BLACKBOX_V2_PLATFORM.md`
- Modify: `docs/records/status/STATUS_HISTORY_THROUGH_20260720.md`

- [ ] **Step 1: 删除精确 12 份文档**

使用补丁逐文件删除设计中列出的 12 份 Markdown。不得使用递归删除命令，
不得删除父目录中的其他文件。

- [ ] **Step 2: 修正顶层导航**

在 `docs/README.md` 中：

- 删除“历史入口”文档域；
- 将 Native V1 描述改为只包含存量维护；
- 将“过期文档必须标明替代入口”改为“过期文档从工作树删除，历史通过 Git
  追溯”。

在 `docs/onboarding/README.md` 中将“历史规则和旧草案”入口改为：

```markdown
| 查看历史规则和旧草案 | 使用 Git 历史；不得用于当前验收 |
```

- [ ] **Step 3: 修正 SOP 与 Native 索引**

在 `docs/sop/README.md` 中：

- 将开头改为“本目录只保存可执行 SOP”；
- 删除旧链接读者行、三条 `SCHEME_*` 文档记录和 `HISTORICAL` 状态；
- 删除“跟随历史入口”和“历史兼容页”维护规则。

在 `docs/native_v1/README.md` 中：

- 删除历史档案行；
- 将旧路径说明改为“冻结前新增流程已从工作树删除，可通过 Git 历史追溯”。

- [ ] **Step 4: 修正 Blackbox 文档**

在 `docs/blackbox_v2/README.md` 中：

- 删除 archive 文档层、入口和冲突优先级；
- 将废弃资料规则改为“从工作树删除，由 Git 历史追溯”；
- 将桌面旧 SOP 映射改为“不保留工作树副本”；
- 删除“历史档案文件名”要求；
- 更新提交前检查，不再要求 archive banner。

在 `docs/architecture/BLACKBOX_V2_PLATFORM.md` 中将：

```markdown
| 废弃规范和决策演进 | `docs/blackbox_v2/archive/` |
```

替换为：

```markdown
| 废弃规范和决策演进 | Git 历史，不进入当前工作树 |
```

- [ ] **Step 5: 修正冻结状态页中的失效链接**

在 `STATUS_HISTORY_THROUGH_20260720.md` 中只修正三个 Markdown 链接：

- 旧 T0/入库入口改指 `../../onboarding/README.md`；
- 文本明确这是链接维护，不改变历史事实。

- [ ] **Step 6: 验证定点测试转绿**

Run:

```bash
conda run -n bond_factor_lab_service \
  python -m unittest tests.test_onboarding_docs
```

Expected: 40 tests passed。

### Task 4: 全面验证并提交清理

**Files:**
- Test: `tests/test_onboarding_docs.py`
- Verify: all changed/deleted documentation

- [ ] **Step 1: 检查删除和引用**

Run:

```bash
git status --short
rg -n \
  "archive/README|SCHEME_PARADIGM|SCHEME_ONBOARDING_T0|SCHEME_ONBOARDING_SOP|SCHEME_POST_ONBOARDING_TEST|UPSTREAM_DELIVERY_SOP_EXCEL_DRAFT|native_v1/archive|blackbox_v2/archive" \
  AGENTS.md CLAUDE.md README.md docs tests deploy data \
  -g '*.md' -g '*.py'
```

Expected: 只允许设计/计划中的反引号删除清单和明确的 Git 历史说明；
不存在可点击失效链接或现行入口。

- [ ] **Step 2: 运行文档和同步门禁**

Run:

```bash
conda run -n bond_factor_lab_service \
  python -m unittest tests.test_onboarding_docs
cmp -s AGENTS.md CLAUDE.md
git diff --check
```

Expected: 40 tests passed；同步检查和 diff 检查退出码均为 0。

- [ ] **Step 3: 运行完整回归**

Run:

```bash
/Users/macstudio0/miniconda3/envs/get_factor/bin/python -m pytest -q
/Users/macstudio0/miniconda3/envs/get_factor/bin/python -m compileall -q \
  backend backtests harness scheduler shared schemes tests
```

Expected: pytest 零失败，compileall 退出码为 0。

- [ ] **Step 4: 提交第一批清理**

提交前重新核对 `git status --short` 和分支列表，只暂存本计划列出的文件：

```bash
git commit -m "docs: remove retired compatibility archives"
```

Expected: 第一批删除、引用修正和门禁修改形成独立提交。

### Task 5: 推送开发分支

**Files:**
- No file changes

- [ ] **Step 1: 核对提交和远程差异**

Run:

```bash
git status --short
git log --oneline origin/codex/audit-bugfixes-20260613..HEAD
git diff --stat origin/codex/audit-bugfixes-20260613..HEAD
```

Expected: 无未提交治理修改；差异只包含 handoff、设计、计划和第一批清理。

- [ ] **Step 2: 推送当前开发分支**

Run:

```bash
git push origin codex/audit-bugfixes-20260613
```

Expected: 远程分支前进到本批最终提交；不修改 `master`。
