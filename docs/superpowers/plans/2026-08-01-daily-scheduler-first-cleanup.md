# Daily Scheduler First Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除已退出生产调用图的两层 capacity admission 实现、专属测试和失真日度草案，同时以结构门禁防止旧模块回流。

**Architecture:** 保持 launchd、ledger runtime、APScheduler、数据库和方案执行行为不变；先写退役路径门禁并观察红灯，再删除孤立实现并同步 release 清单与 attestation 说明。文档清理单独提交，最后用容量、调度、文档和全套 `unittest` 验证行为边界。

**Tech Stack:** Python 3.12、`unittest`、Markdown、Git

---

### Task 1: 用失败测试锁定退役模块边界

**Files:**
- Modify: `tests/test_architecture_boundaries.py`
- Test: `tests/test_architecture_boundaries.py`

- [ ] **Step 1: 增加退役模块不存在门禁**

在 `test_current_repository_has_no_layer_inversions` 之前加入：

```python
def test_retired_daily_capacity_admission_modules_are_absent(self) -> None:
    project_root = Path(__file__).resolve().parents[1]
    retired = (
        project_root / "scheduler" / "capacity_admission.py",
        project_root / "scheduler" / "capacity_runtime_admission.py",
    )

    self.assertEqual(
        [],
        [
            path.relative_to(project_root).as_posix()
            for path in retired
            if path.exists()
        ],
        "retired daily capacity admission modules must not return",
    )
```

- [ ] **Step 2: 运行测试并确认红灯来自旧文件仍存在**

Run:

```bash
python -m unittest \
  tests.test_architecture_boundaries.RepositoryArchitectureBoundaryTests.test_retired_daily_capacity_admission_modules_are_absent
```

Expected: `FAIL`，失败列表精确包含
`scheduler/capacity_admission.py` 和
`scheduler/capacity_runtime_admission.py`，不是 import 或语法错误。

### Task 2: 删除孤立 admission 实现并恢复绿灯

**Files:**
- Delete: `scheduler/capacity_admission.py`
- Delete: `scheduler/capacity_runtime_admission.py`
- Delete: `tests/test_capacity_admission.py`
- Delete: `tests/test_capacity_runtime_admission.py`
- Modify: `scheduler/capacity_candidate_runtime.py:50-63`
- Modify: `scheduler/capacity_attestation.py:1-6`
- Test: `tests/test_architecture_boundaries.py`
- Test: `tests/test_capacity_candidate_runtime.py`
- Test: `tests/test_capacity_gate.py`

- [ ] **Step 1: 删除两个孤立模块和对应的退役接口测试**

使用 `apply_patch` 精确删除以下文件，不删除同目录的其他容量模块：

```text
*** Delete File: scheduler/capacity_admission.py
*** Delete File: scheduler/capacity_runtime_admission.py
*** Delete File: tests/test_capacity_admission.py
*** Delete File: tests/test_capacity_runtime_admission.py
```

- [ ] **Step 2: 从 scheduler release 必备文件清单删除旧模块名**

将 `scheduler/capacity_candidate_runtime.py` 中的集合收敛为：

```python
SCHEDULER_RELEASE_REQUIRED_FILES = frozenset(
    {
        "scheduler/capacity_attestation.py",
        "scheduler/capacity_candidate_runtime.py",
        "scheduler/daily_policy.py",
        "scheduler/discovery.py",
        "scheduler/daily_runtime.py",
        "scheduler/repository.py",
        "shared/input_artifacts.py",
        "backend/main.py",
    }
)
```

不修改 `_collect_scheduler_release_artifacts` 的目录扫描、缺失文件校验或 digest
计算。

- [ ] **Step 3: 修正 attestation 模块说明**

将 `scheduler/capacity_attestation.py` 顶部 docstring 改为：

```python
"""容量证据的精确候选与可信采集契约。

本模块只验证结构和候选绑定，不负责运行时调度准入。
"""
```

- [ ] **Step 4: 运行退役路径门禁并确认绿灯**

Run:

```bash
python -m unittest \
  tests.test_architecture_boundaries.RepositoryArchitectureBoundaryTests.test_retired_daily_capacity_admission_modules_are_absent
```

Expected: `Ran 1 test`，`OK`。

- [ ] **Step 5: 运行容量与架构定向回归**

Run:

```bash
python -m unittest \
  tests.test_architecture_boundaries \
  tests.test_capacity_candidate_runtime \
  tests.test_capacity_gate \
  tests.test_daily_policy_v2
```

Expected: 全部测试通过，退出码为 0；不得出现已删除模块的 import error。

- [ ] **Step 6: 提交代码清理**

提交前先执行：

```bash
git status --short
git branch --show-current
git branch -vv
```

Expected: 当前分支为 `codex/audit-bugfixes-20260613`，变更只包含本任务列出的
六个删除/修改文件和架构门禁测试。

然后执行：

```bash
git add \
  scheduler/capacity_admission.py \
  scheduler/capacity_runtime_admission.py \
  scheduler/capacity_candidate_runtime.py \
  scheduler/capacity_attestation.py \
  tests/test_capacity_admission.py \
  tests/test_capacity_runtime_admission.py \
  tests/test_architecture_boundaries.py
git commit -m "refactor: remove retired capacity admission runtime"
```

Expected: 形成一个只包含退役 capacity admission 闭环的提交。

### Task 3: 删除失真日度草案并修正 SOP 索引

**Files:**
- Delete: `docs/sop/DAILY_GRAY_RUNNER_IMPLEMENTATION_PLAN_20260730.md`
- Modify: `docs/sop/README.md:31`
- Test: `tests/test_onboarding_docs.py`

- [ ] **Step 1: 删除失真草案**

使用 `apply_patch` 删除：

```text
*** Delete File: docs/sop/DAILY_GRAY_RUNNER_IMPLEMENTATION_PLAN_20260730.md
```

- [ ] **Step 2: 从 SOP 索引移除草案行**

从 `docs/sop/README.md` 删除包含
`DAILY_GRAY_RUNNER_IMPLEMENTATION_PLAN_20260730.md` 的唯一完整表格行；该行当前
状态为 `DRAFT`，用途为“每日 gray_live 自动信号实施计划”。

不要创建 redirect、archive 或同内容替代文档。

- [ ] **Step 3: 运行文档索引和链接门禁**

Run:

```bash
python -m unittest tests.test_onboarding_docs
```

Expected: 全部测试通过，SOP 目录索引与 Markdown 相对链接均无缺口。

- [ ] **Step 4: 确认现行 SOP 不再引用旧草案**

Run:

```bash
rg -n "DAILY_GRAY_RUNNER_IMPLEMENTATION_PLAN_20260730" docs/sop tests
```

Expected: 退出码为 1 且无输出。

- [ ] **Step 5: 提交文档清理**

提交前先执行：

```bash
git status --short
git branch --show-current
git branch -vv
```

Expected: 当前分支不变；未提交内容只包含旧草案删除和 SOP 索引修改。

然后执行：

```bash
git add \
  docs/sop/DAILY_GRAY_RUNNER_IMPLEMENTATION_PLAN_20260730.md \
  docs/sop/README.md
git commit -m "docs: remove stale daily gray runner plan"
```

Expected: 文档清理形成独立提交。

### Task 4: 验证完整边界并收尾

**Files:**
- Verify only: repository-wide Python and Markdown files

- [ ] **Step 1: 检查退役引用只留在历史设计/实施计划和禁止回流门禁**

Run:

```bash
rg -n \
  "scheduler[./]capacity_(runtime_)?admission|DAILY_GRAY_RUNNER_IMPLEMENTATION_PLAN_20260730" \
  . \
  --glob '!docs/superpowers/specs/2026-08-01-daily-scheduler-first-cleanup-design.md' \
  --glob '!docs/superpowers/plans/2026-08-01-daily-scheduler-first-cleanup.md' \
  --glob '!tests/test_architecture_boundaries.py' \
  --glob '!.git/**'
```

Expected: 退出码为 1 且无输出；不存在可执行 import、release 必备项或现行 SOP 链接。

- [ ] **Step 2: 运行完整 unittest 套件**

Run:

```bash
python -m unittest discover -s tests
```

Expected: 全部测试通过，退出码为 0。

- [ ] **Step 3: 运行编译和 diff 门禁**

Run:

```bash
python -m compileall -q backend backtests harness migrations scheduler scripts shared tests
git diff --check
```

Expected: 两个命令都无错误并返回 0。

- [ ] **Step 4: 核对最终提交和工作树**

Run:

```bash
git status --short --branch
git log -3 --oneline --decorate
```

Expected: 工作树干净；最近四个提交由旧到新依次为设计记录、实施计划、capacity
admission 代码清理和失真日度草案清理，当前分支只领先远程且未推送。

- [ ] **Step 5: 对照设计逐项确认未越界**

Run:

```bash
git diff 93e986e^..HEAD -- \
  deploy \
  scheduler/main.py \
  scheduler/capacity_candidate_runtime.py \
  scheduler/capacity_attestation.py \
  migrations \
  schemes \
  shared
```

Expected: `deploy/`、`scheduler/main.py`、`migrations/`、`schemes/` 和 `shared/`
均没有行为变更；输出只允许出现
`scheduler/capacity_candidate_runtime.py` 与
`scheduler/capacity_attestation.py` 的两处已批准同步修改。
