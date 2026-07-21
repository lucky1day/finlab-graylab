# Frontend Live Target Range Label Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every selected-scheme live divider description with one scheduled-live target-date start label.

**Architecture:** Keep the backend `phase_ranges` API and database semantics unchanged. Derive the display value in the shared frontend helper from the `scheduled_live.start_target_date`; render a pending label when that phase does not exist. Update the cache-busting asset version and platform-only onboarding documentation.

**Tech Stack:** Vanilla JavaScript, Python `unittest`, FastAPI static frontend, Markdown SOP.

---

### Task 1: Lock the shared frontend wording with failing tests

**Files:**
- Modify: `tests/test_frontend_factor_lab.py:1245-1254`
- Modify: `tests/test_frontend_factor_lab.py:1559`
- Modify: `tests/test_frontend_static_cache.py:39`

- [ ] **Step 1: Replace the scheduled-phase detail assertions**

In `test_daily_v28_backtest_and_live_start_are_visible_together`, require:

```python
self.assertIn("实盘预测目标区间：2026-06-18开始", result["detailMeta"])
for removed in (
    "实盘发出起点",
    "灰度实盘（目标期）",
    "信号发出",
    "正式调度发出起点",
):
    self.assertNotIn(removed, result["detailMeta"])
```

- [ ] **Step 2: Replace the no-scheduled-phase assertion**

In `test_weekly_live_uses_single_predict_date_start_semantics`, require:

```python
self.assertEqual(result["dividerText"], "实盘预测目标区间：待产生")
```

- [ ] **Step 3: Require a new frontend asset version**

Change `tests/test_frontend_static_cache.py` to require:

```python
self.assertIn('src="aifin-shell.js?v=20260721a"', html)
```

- [ ] **Step 4: Run the focused tests and verify RED**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
    tests.test_frontend_factor_lab.FactorLabRealtimeDataTests.test_daily_v28_backtest_and_live_start_are_visible_together \
    tests.test_frontend_factor_lab.FactorLabRealtimeDataTests.test_weekly_live_uses_single_predict_date_start_semantics \
    tests.test_frontend_static_cache -v
```

Expected: failures show the old combined live/gray/scheduled wording and old asset version.

### Task 2: Implement the single target-range label

**Files:**
- Modify: `frontend/aifin-shell.js:419-466`
- Modify: `frontend/index.html:213`
- Test: `tests/test_frontend_factor_lab.py`
- Test: `tests/test_frontend_static_cache.py`

- [ ] **Step 1: Replace the old helper chain with scheduled target lookup**

Use the shared helper for every frequency/runtime:

```javascript
function scheduledLiveTargetStart(phaseRanges) {
  var scheduled = (phaseRanges || []).find(function (range) {
    return String(range.prediction_phase || "") === "scheduled_live";
  });
  return normalizeIsoDate(scheduled && scheduled.start_target_date);
}

function liveDividerText(scheme) {
  var targetStart = scheduledLiveTargetStart(scheme && scheme.phaseRanges);
  return targetStart
    ? "实盘预测目标区间：" + targetStart + "开始"
    : "实盘预测目标区间：待产生";
}
```

Delete `liveDividerLabels` and `phaseRangeTexts`; they have no other consumers.

- [ ] **Step 2: Bump the JavaScript cache key**

In `frontend/index.html`, use:

```html
<script src="aifin-shell.js?v=20260721a"></script>
```

- [ ] **Step 3: Run the focused tests and verify GREEN**

Run the Task 1 command again.

Expected: all focused tests pass.

- [ ] **Step 4: Commit frontend behavior and tests**

```bash
git add frontend/aifin-shell.js frontend/index.html \
  tests/test_frontend_factor_lab.py tests/test_frontend_static_cache.py
git commit -m "fix: simplify live target range label"
```

### Task 3: Synchronize the platform documentation only

**Files:**
- Modify: `tests/test_onboarding_docs.py:91-129`
- Modify: `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md:468-475,536-538`
- Modify: `docs/CURRENT_STATUS.md:37-47`
- Modify: `docs/superpowers/specs/2026-07-21-live-target-range-label-design.md`

- [ ] **Step 1: Update the documentation test first**

Require the platform SOP to contain `实盘预测目标区间` and
`scheduled_live.start_target_date`, while the upstream delivery SOP remains free of
platform-only `phase_ranges` rules:

```python
self.assertIn("实盘预测目标区间", platform)
self.assertIn("scheduled_live.start_target_date", platform)
self.assertNotIn("实盘预测目标区间", upstream)
```

- [ ] **Step 2: Run the documentation test and verify RED**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
    tests.test_onboarding_docs.OnboardingDocumentationTests.test_blackbox_platform_sop_defines_live_boundary_and_frontend_acceptance -v
```

Expected: fail because the platform SOP still specifies the old combined wording.

- [ ] **Step 3: Update the platform SOP and current status**

Document this exact frontend rule:

```text
存在 scheduled_live：▼ 实盘预测目标区间：{scheduled_live.start_target_date}开始
不存在 scheduled_live：▼ 实盘预测目标区间：待产生
```

State that `predict_date`, `feature_date`, gray endpoints and `deployed_at` cannot substitute for the scheduled target start. Update the 1Y T+5 current status to 40 gray rows through 2026-07-21 and the simplified display contract. Do not modify `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`.

- [ ] **Step 4: Mark the approved design implemented**

Change the design status from `待实施` to `已实施`.

- [ ] **Step 5: Run the documentation tests and verify GREEN**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest tests.test_onboarding_docs -v
```

Expected: all onboarding documentation tests pass.

- [ ] **Step 6: Commit platform documentation**

```bash
git add tests/test_onboarding_docs.py \
  docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md \
  docs/CURRENT_STATUS.md \
  docs/superpowers/specs/2026-07-21-live-target-range-label-design.md
git commit -m "docs: standardize live target range wording"
```

### Task 4: Full verification and public static check

**Files:**
- Verify only; no new source files.

- [ ] **Step 1: Run frontend and documentation regression suites**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
    tests.test_frontend_factor_lab \
    tests.test_frontend_static_cache \
    tests.test_onboarding_docs -q
```

Expected: all tests pass.

- [ ] **Step 2: Run the full project suite**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest discover -s tests -p 'test_*.py' -q
```

Expected: zero failures.

- [ ] **Step 3: Verify static content locally and publicly**

```bash
curl -fsS http://127.0.0.1:8100/ | rg 'aifin-shell.js\?v=20260721a'
curl -fsS http://127.0.0.1:8100/aifin-shell.js | rg '实盘预测目标区间'
curl -fsS https://bond.finailab.cn/bond-factor-lab/ | rg 'aifin-shell.js\?v=20260721a'
curl -fsS https://bond.finailab.cn/bond-factor-lab/aifin-shell.js | rg '实盘预测目标区间'
```

Expected: local and public HTML/JS expose the new cache key and wording.

- [ ] **Step 4: Verify Git scope**

```bash
git diff --check
git status --short
```

Expected: no tracked uncommitted changes; pre-existing untracked plist and production report directory remain untouched.
