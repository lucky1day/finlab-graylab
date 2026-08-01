# Low-Risk Repository Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete four completed one-off repair scripts and three completed internal process records without changing any production behavior.

**Architecture:** Split the cleanup into two independently reviewable commits: scripts first, documentation second. Preserve current architecture, launchd, runtime, database, schemes and test behavior; finish by deleting this temporary plan and its temporary design record after full verification.

**Tech Stack:** Python 3.12, pytest 9.1.1, Markdown indexes, Git.

---

### Task 1: Delete completed one-off repair scripts

**Files:**
- Delete: `scripts/backfill_prediction_semantics.py`
- Delete: `scripts/repair_live_prediction_semantics.py`
- Delete: `scripts/delete_bad_live_predictions.py`
- Delete: `scripts/delete_retired_weekly_average_schemes.py`
- Test: `tests/test_prediction_semantics.py`
- Test: `tests/test_architecture_boundaries.py`

- [ ] **Step 1: Reconfirm the exact deletion set**

Run:

```bash
wc -l \
  scripts/backfill_prediction_semantics.py \
  scripts/repair_live_prediction_semantics.py \
  scripts/delete_bad_live_predictions.py \
  scripts/delete_retired_weekly_average_schemes.py
```

Expected: four files and 1,021 total lines.

For each basename, run `rg -n -F` outside its own file. Expected: no external references except `repair_live_prediction_semantics.py` referring internally to `delete_bad_live_predictions.py`; both are deleted together.

- [ ] **Step 2: Delete exactly the four scripts**

Use `apply_patch`. Do not modify other scripts, production modules, database state, launchd state or ignored runtime artifacts.

- [ ] **Step 3: Run the script-boundary guards**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q \
  tests/test_prediction_semantics.py \
  tests/test_architecture_boundaries.py
conda run -n bond_factor_lab_service python -m compileall -q \
  scripts tests
```

Expected: pytest exit code 0 and compileall exit code 0.

- [ ] **Step 4: Verify and commit only the script deletion**

Confirm `git diff --name-status` contains exactly four `D scripts/...` entries, then run:

```bash
git add -u scripts
git commit -m "chore: remove completed repair scripts"
```

### Task 2: Delete completed internal process records

**Files:**
- Delete: `docs/internal/plans/2026-07-01-daily-0629-sop-closure.md`
- Delete: `docs/internal/specs/2026-07-12-liwei-0616-incremental-phase-a-cache-design.md`
- Delete: `docs/internal/specs/2026-07-12-liwei-full-oos-gray-models-design.md`
- Modify: `docs/internal/plans/README.md`
- Modify: `docs/internal/specs/README.md`
- Test: `tests/test_onboarding_docs.py`

- [ ] **Step 1: Reconfirm that only internal indexes reference the records**

Run `rg -n -F` for each document basename from the repository root. Expected: each result appears only in its own internal `README.md` index.

- [ ] **Step 2: Delete the three records and their three index rows**

Use `apply_patch`. Preserve `docs/internal/specs/2026-07-21-t5-no-foreign-lgbm-ablation-design.md` and all current architecture, SOP, onboarding and production evidence documents.

- [ ] **Step 3: Run the documentation guard**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q \
  tests/test_onboarding_docs.py
```

Expected: exit code 0 with no index or relative-link failure.

- [ ] **Step 4: Verify and commit only the documentation cleanup**

Confirm the diff contains three document deletions and two internal index modifications, then run:

```bash
git add -u docs/internal
git commit -m "docs: remove completed internal process records"
```

### Task 3: Final verification and temporary record cleanup

**Files:**
- Delete: `docs/internal/specs/2026-08-01-low-risk-repository-hygiene-design.md`
- Delete: `docs/internal/plans/2026-08-01-low-risk-repository-hygiene.md`
- Modify: `docs/internal/specs/README.md`
- Modify: `docs/internal/plans/README.md`

- [ ] **Step 1: Run the canonical full verification**

Run:

```bash
/usr/bin/time -p conda run -n bond_factor_lab_service python -m pytest -q
conda run -n bond_factor_lab_service python -m pytest --collect-only -q
conda run -n bond_factor_lab_service python -m compileall -q \
  scripts docs tests
```

Expected: 1,708 tests collected, all retained tests pass or skip, and compileall exits 0.

- [ ] **Step 2: Run residual-reference and production-boundary checks**

Run exact-name `rg` scans for all seven deleted files. Expected: no results after temporary records are excluded.

Run:

```bash
git diff --check
git diff --exit-code 222cdb3..HEAD -- \
  scheduler harness backend shared backtests schemes deploy migrations
```

Expected: no whitespace errors and no production/runtime directory changes.

- [ ] **Step 3: Delete the temporary design, plan and index rows**

Use `apply_patch`, then run `tests/test_onboarding_docs.py` again.

- [ ] **Step 4: Commit temporary record cleanup**

```bash
git add -u docs/internal
git commit -m "docs: remove repository hygiene work records"
```

- [ ] **Step 5: Verify branch preservation**

Run:

```bash
git status --short --branch
git branch --show-current
git rev-parse master origin/master
git rev-list --left-right --count \
  origin/codex/audit-bugfixes-20260613...HEAD
```

Expected: clean `codex/audit-bugfixes-20260613`, local development commits ahead of its remote, unchanged local/remote master, and no push.
