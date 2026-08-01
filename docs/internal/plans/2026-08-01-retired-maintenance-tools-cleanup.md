# 退役维护工具清理实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除七个已经被现行平台契约替代或会生成过期结果的维护脚本，并修正相关文档索引。

**Architecture:** 先确认七个脚本没有当前消费者，再按一个删除闭包移除。文档只删除失效入口并补齐既有证据索引；最终删除本轮设计和计划，以完整 pytest 和限定目录 diff 证明生产实现不变。

**Tech Stack:** Python 3.12、pytest、Git、Markdown。

---

### Task 1: 删除退役工具闭包

**Files:**
- Delete: `scripts/audit_daily_data_service.py`
- Delete: `scripts/compare_refactor_outputs.py`
- Delete: `scripts/delete_backtest_runs.py`
- Delete: `scripts/generate_benchmark_samples.py`
- Delete: `scripts/normalize_weekly_scheme_benchmarks.py`
- Delete: `scripts/run_baseline.py`
- Delete: `scripts/run_framework_repro.py`

- [ ] **Step 1: 复核消费者和生产入口**

Run:

```bash
git grep -n -E 'audit_daily_data_service|compare_refactor_outputs|delete_backtest_runs|generate_benchmark_samples|normalize_weekly_scheme_benchmarks|run_baseline|run_framework_repro' -- ':!docs/internal/**' ':!docs/records/**' ':!reports/README.md'
git grep -n -E 'audit_daily_data_service|compare_refactor_outputs|delete_backtest_runs|generate_benchmark_samples|normalize_weekly_scheme_benchmarks|run_baseline|run_framework_repro' -- deploy scheduler harness backend shared backtests schemes tests
```

Expected: 没有当前代码、测试或生产入口消费者。

- [ ] **Step 2: 删除七个脚本**

使用 `apply_patch` 删除上述七个文件，不修改其替代实现。

- [ ] **Step 3: 编译剩余脚本与测试**

Run:

```bash
conda run -n bond_factor_lab_service python -m compileall -q scripts tests
```

Expected: 退出码 0。

### Task 2: 修正文档入口

**Files:**
- Modify: `reports/README.md`
- Modify: `docs/blackbox_v2/records/README.md`

- [ ] **Step 1: 清除失效报告入口**

从 `reports/README.md` 删除 `audit_daily_data_service.py`、
`compare_refactor_outputs.py`、`run_framework_repro.py` 三个入口及空的“常见生成入口”段落。

- [ ] **Step 2: 补齐四份复认证证据索引**

在 Blackbox V2 records 索引增加以下四个现存文件的链接：

```text
RECERTIFICATION_10Y_T5_MAJ3_K3_IC_STATIC_20260726.evidence.json
RECERTIFICATION_10Y_T5_MAJ4_K3_IC_STATIC_20260726.evidence.json
RECERTIFICATION_10Y_T5_MAJ4_K3_IC_YEARLY_20260726.evidence.json
RECERTIFICATION_10Y_T5_SAY_K5_SHARPE_STATIC_20260726.evidence.json
```

- [ ] **Step 3: 运行文档门禁**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q tests/test_onboarding_docs.py
```

Expected: 全部通过。

### Task 3: 回收过程记录并完成验证

**Files:**
- Delete: `docs/internal/specs/2026-08-01-retired-maintenance-tools-cleanup-design.md`
- Delete: `docs/internal/plans/2026-08-01-retired-maintenance-tools-cleanup.md`
- Delete: `docs/internal/plans/README.md`
- Modify: `docs/internal/specs/README.md`
- Modify: `docs/internal/README.md`

- [ ] **Step 1: 删除本轮设计、计划和临时 plans 导航**

使用 `apply_patch` 删除上述过程记录及索引行，使最终树恢复为只有长期文档的结构。

- [ ] **Step 2: 执行完整验证**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q
conda run -n bond_factor_lab_service python -m pytest --collect-only -q
conda run -n bond_factor_lab_service python -m compileall -q scripts docs tests
git diff --check
```

Expected: pytest 全通过，仍收集 1708 项，编译和 diff 检查退出码 0。

- [ ] **Step 3: 核验最终净变更与分支边界**

Run:

```bash
git diff --exit-code b71ec6f..HEAD -- scheduler harness backend shared backtests schemes deploy migrations
git status --short --branch
```

Expected: 生产目录零差异，工作树干净，当前分支保持 `codex/audit-bugfixes-20260613`，不推送远程。
