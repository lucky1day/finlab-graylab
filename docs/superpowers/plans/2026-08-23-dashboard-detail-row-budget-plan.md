# Dashboard Detail Row Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让包含五个新 M0 方案的完整 Dashboard 在现有 raw/gzip 预算内恢复可用，同时保留明确的详情行 fail-closed 上限。

**Architecture:** 只调整 `backend.factor_lab_dashboard.MAX_DETAIL_ROWS`，用精确边界测试保护 25,000/25,001 行行为。API schema、查询和前端均不改变；发布仍使用一份确定性 archive 先 ECS、后 Mac3。

**Tech Stack:** Python 3.12、pytest、FastAPI Dashboard、immutable source release。

---

### Task 1: 以测试驱动调整详情行预算

**Files:**
- Modify: `tests/test_factor_lab_dashboard_api.py`
- Modify: `backend/factor_lab_dashboard.py`

- [ ] **Step 1: 写失败边界测试**

  调用 `_validate_canonical_snapshot_budgets({"schemes": [], "snapshot_id": "budget-boundary"}, detail_rows=25_000)` 并断言成功；调用同一函数传 `25_001` 并断言错误包含 `dashboard detail rows exceed budget`。`snapshot_id` 只满足成功路径的既有日志字段，不改变测试目标。

- [ ] **Step 2: 验证 RED**

  Run: `python -m pytest tests/test_factor_lab_dashboard_api.py -q`

  Expected: 25,000 行用例因当前上限 20,000 失败。

- [ ] **Step 3: 最小实现**

  将 `MAX_DETAIL_ROWS = 20_000` 改为 `MAX_DETAIL_ROWS = 25_000`，不修改 raw/gzip 上限或其他代码。

- [ ] **Step 4: 验证 GREEN**

  Run: `python -m pytest tests/test_factor_lab_dashboard_api.py tests/test_dashboard_gate.py -q`

  Expected: PASS。

- [ ] **Step 5: 独立规格和质量复审后提交**

  Commit message: `fix(dashboard): raise detail row resource budget`

### Task 2: 发布并恢复 DashboardGate

**Files:**
- Execute: `scripts/build_source_release.py`
- Execute: `scripts/install_source_release.py`

- [ ] **Step 1: 完整回归和双构建**

  Run: `python -m pytest -q`，随后从 clean HEAD 构建两份 archive 并断言 SHA-256 相同。

- [ ] **Step 2: ECS 先晋级**

  预安装、expected-current CAS、只重启 Backend；验证 Dashboard 200、五个 M0 active 状态和五个 timer 不变。

- [ ] **Step 3: Mac3 后晋级**

  使用同一 archive 做预安装和 expected-current CAS，只 kickstart Backend，不替换 plist。

- [ ] **Step 4: 最终验收**

  验证 Mac3 Dashboard 200、五个 M0 DashboardGate passed、数据库/Registry/overlay/回测/live 精确状态、launchd drift 和浏览器控制台。
