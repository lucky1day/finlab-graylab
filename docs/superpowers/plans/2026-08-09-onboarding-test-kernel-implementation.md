# Onboarding Test Kernel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将测试目录收敛为 33 个未来方案入库可复用的合同测试，并删除一次性测试的独占 fixture。

**Architecture:** 以批准设计中的显式 allowlist 为唯一保留边界。先删除 allowlist 外测试和三个孤立 fixture，再修正文档入口与死引用，最后只运行保留集合并检查受保护证据计数。

**Tech Stack:** Python 3.12、pytest、Git、ripgrep

---

### Task 1: 删除 allowlist 外测试

**Files:**
- Delete: `tests/test_*.py` 中不属于设计保留集合的 63 个文件
- Delete: `tests/factor_lab_dashboard_conformance.py`
- Delete: `tests/isolated_mysql.py`
- Delete: `tests/mysql_fixtures.py`

- [ ] **Step 1: 生成实际删除集合并核对数量**

Run: 使用设计中的 33 文件 allowlist 遍历 `tests/test_*.py`，输出不在 allowlist 的路径。

Expected: 删除集合为 63 个测试文件；`tests/blackbox_backtest_fixtures.py` 不在删除集合。

- [ ] **Step 2: 删除测试与独占 fixture**

Run: 对核对后的精确路径执行 `git rm`。

Expected: `find tests -maxdepth 1 -name 'test_*.py' | wc -l` 返回 `33`。

- [ ] **Step 3: 复查测试间 import**

Run: `rg -n '^from tests\.|^import tests\.' tests`

Expected: 只引用保留文件或 `tests.blackbox_backtest_fixtures`。

### Task 2: 收敛入库文档入口

**Files:**
- Modify: `docs/onboarding/README.md`
- Modify/Delete: 仍引用已删除测试的受版本控制文档；不修改用户工作区已改动的 `docs/records/status/KNOWN_ISSUES_HANDOFF_20260802.md`

- [ ] **Step 1: 更新常用 pytest 表**

删除 `test_frontend_factor_lab.py` 等已退役入口，只列设计保留集合中的按场景测试命令；将完整回归定义为“运行入库核心集”，不再暗示历史测试永久保留。

- [ ] **Step 2: 检查死引用**

Run: `rg -n 'tests/test_[A-Za-z0-9_]+' docs README.md AGENTS.md CLAUDE.md scripts deploy`

Expected: 受版本控制的当前文档不引用已删除测试；用户工作区冲突文件保持未提交。

### Task 3: 删除测试独占死代码闭包

**Files:**
- Delete/Modify: 仅在被删除测试和自身定义中出现、且不被文档、plist、CLI 或生产 import 引用的 helper

- [ ] **Step 1: 扫描候选符号与文件**

Run: 对删除前后引用差异执行 `rg`，并检查 `deploy/launchd/*.plist`、文档入口与模块 import。

Expected: 只删除证明为测试独占的代码；migration SQL、生产入口、`source_evidence/` 与方案 `benchmarks/` 不变。

- [ ] **Step 2: 编译保留模块**

Run: `python -m compileall -q backend backtests harness scheduler shared schemes scripts tests`

Expected: 退出码 `0`。

### Task 4: 验证并提交

**Files:**
- Verify: `tests/`、`docs/onboarding/README.md`

- [ ] **Step 1: 运行保留测试**

Run: `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q`

Expected: 收集的全部核心测试通过，无 import error。

- [ ] **Step 2: 验证规模和证据边界**

Run: 统计 `test_*.py` 数量、`tests/*.py` 行数，并比较清理前后 `source_evidence/` 与 `schemes/*/benchmarks/` 的 Git 文件数。

Expected: 33 个测试文件、少于 30,000 行；受保护证据分别保持 202 和 114 个文件。

- [ ] **Step 3: 提交**

Run: `git add -u docs tests backend backtests harness scheduler shared schemes scripts && git commit -m 'test: retain onboarding contract kernel'`

Expected: 提交只包含本设计授权的测试、文档和测试独占死代码清理。

