# Factor Lab Live/Backtest Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent a Factor Lab scheme from displaying the same `target_date` from both backtest and live sources.

**Architecture:** Keep historical and live database records immutable. A shared frontend helper derives the earliest live `target_date`; Dashboard and legacy fallback retain only backtest details strictly before it, then derive their monthly rows from the retained details.

**Tech Stack:** Native JavaScript in `frontend/aifin-shell.js`; Python `unittest` running the Node VM frontend harness.

---

### Task 1: Lock the target-date cutover behavior with a failing regression test

**Files:**
- Modify: `tests/test_frontend_factor_lab.py:729-780, 966-1560`

- [x] **Step 1: Add the pre-cutoff daily backtest detail and change the daily assertions**

  In `test_dashboard_view_model_groups_sources_and_applies_target_date_cutoff`, retain one
  backtest row with `target_date="2026-05-27"`, while keeping two rows at/after the first
  live target date (`2026-05-28`). Assert that only the former remains as `backtest`,
  while the two live rows remain:

  ```python
  self.assertEqual(
      [(row["source"], row["targetDate"]) for row in may_daily],
      [
          ("backtest", "2026-05-27"),
          ("live", "2026-05-28"),
          ("live", "2026-05-29"),
      ],
  )
  ```

- [x] **Step 2: Update the helper-contract assertion to require a target-date helper**

  Replace the old monthly-only marker assertion with:

  ```python
  self.assertIn("liveBacktestCutoffTargetDate(", builder)
  ```

- [x] **Step 3: Run the focused test and verify RED**

  Run:

  ```bash
  conda run -n bond_factor_lab_service --no-capture-output python -m pytest \
    tests/test_frontend_factor_lab.py::FactorLabRankingTests::test_dashboard_view_model_groups_sources_and_applies_target_date_cutoff \
    tests/test_frontend_factor_lab.py::FactorLabRankingTests::test_dashboard_builder_reuses_shared_target_date_cutoff_helper -q
  ```

  Expected: failure because current daily/`T+1` Dashboard code does not apply a live cutoff.

### Task 2: Implement the shared frontend target-date cutoff

**Files:**
- Modify: `frontend/aifin-shell.js:320-383`
- Modify: `frontend/aifin-shell.js:1068-1089`
- Modify: `frontend/aifin-shell.js:1776-1815`

- [x] **Step 1: Replace the monthly-only helper with a target-date helper**

  Add a helper that collects normalized `targetDate` / `target_date` values from
  `dailyRowsByMonth` and `phaseRanges.start_target_date`, sorts them, and returns the
  earliest date or `""`:

  ```javascript
  function liveBacktestCutoffTargetDate(liveScheme) {
    var targetDates = [];
    Object.keys((liveScheme && liveScheme.dailyRowsByMonth) || {}).forEach(function (month) {
      (liveScheme.dailyRowsByMonth[month] || []).forEach(function (row) {
        var targetDate = normalizeIsoDate(row && (row.targetDate || row.target_date || ""));
        if (targetDate) targetDates.push(targetDate);
      });
    });
    (liveScheme && liveScheme.phaseRanges || []).forEach(function (range) {
      var targetDate = normalizeIsoDate(range && range.start_target_date);
      if (targetDate) targetDates.push(targetDate);
    });
    targetDates.sort();
    return targetDates.length ? targetDates[0] : "";
  }
  ```

- [x] **Step 2: Use it in the Dashboard builder**

  Replace the monthly cutoff branch with exact target-date filtering:

  ```javascript
  var cutoffTargetDate = liveBacktestCutoffTargetDate({
    dailyRowsByMonth: liveGrouped,
    phaseRanges: phaseRanges
  });
  if (cutoffTargetDate) {
    backtestRows = backtestRows.filter(function (row) {
      return row.targetDate < cutoffTargetDate;
    });
  }
  ```

- [x] **Step 3: Apply the same cutover before legacy source merge**

  In `trimBacktestAtLiveStart`, filter `scheme.dailyRowsByMonth` by
  `row.targetDate < cutoffTargetDate`, rebuild the backtest monthly metrics from the
  retained detail rows with `monthlyRowsFromGroupedDetails`, tag them `_source="backtest"`,
  then refresh `backtestStartMonth` / `backtestEndMonth`. Rename the function to
  `trimBacktestAtLiveStart` and update its sole call site.

- [x] **Step 4: Run focused frontend tests and verify GREEN**

  Run:

  ```bash
  conda run -n bond_factor_lab_service --no-capture-output python -m pytest \
    tests/test_frontend_factor_lab.py::FactorLabRankingTests::test_dashboard_view_model_groups_sources_and_applies_target_date_cutoff \
    tests/test_frontend_factor_lab.py::FactorLabRankingTests::test_dashboard_builder_reuses_shared_target_date_cutoff_helper \
    tests/test_frontend_factor_lab.py::FactorLabRankingTests::test_dashboard_and_legacy_fixture_builders_are_view_model_equivalent -q
  ```

  Expected: all selected tests pass.

### Task 3: Verify the served result and commit the stage

**Files:**
- Modify: `frontend/aifin-shell.js`
- Modify: `tests/test_frontend_factor_lab.py`
- Modify: `docs/superpowers/specs/2026-08-05-factor-lab-live-backtest-cutover-design.md`
- Create: `docs/superpowers/plans/2026-08-05-factor-lab-live-backtest-cutover.md`

- [x] **Step 1: Run the frontend test module and static checks**

  ```bash
  conda run -n bond_factor_lab_service --no-capture-output python -m pytest \
    tests/test_frontend_factor_lab.py -q
  python -m compileall -q backend scheduler shared harness
  git diff --check
  ```

- [x] **Step 2: Read back the live Dashboard without modifying it**

  ```bash
  curl -fsS http://127.0.0.1:8100/api/factor-lab/dashboard
  ```

  Confirm the API remains healthy. The installed backend is not restarted in this task;
  browser verification follows the normal asset refresh path after code deployment.

- [x] **Step 3: Commit only task-owned files**

  ```bash
  git add -- frontend/aifin-shell.js tests/test_frontend_factor_lab.py \
    docs/superpowers/specs/2026-08-05-factor-lab-live-backtest-cutover-design.md \
    docs/superpowers/plans/2026-08-05-factor-lab-live-backtest-cutover.md
  git commit -m "fix(frontend): cut backtests at live target date"
  ```
