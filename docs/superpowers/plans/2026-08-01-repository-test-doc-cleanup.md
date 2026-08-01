# Repository Test and Intermediate Documentation Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist pytest as the canonical test dependency and runner, remove provably redundant tests, and delete completed Superpowers plans/design artifacts from the working tree.

**Architecture:** Establish the correct pytest baseline before deleting tests, then remove only tests whose behavior is already guarded by generic admission/discovery/scheduler contracts. Finish by deleting the historical `docs/superpowers/` tree, including this temporary plan and its design, while preserving current architecture, SOP, status, audit, and production evidence documents.

**Tech Stack:** Python 3.12, pytest 9.1.1, unittest-compatible pytest collection, conda environment `bond_factor_lab_service`, Markdown, Git.

---

### Task 1: Persist pytest and establish the real baseline

**Files:**
- Modify: `pyproject.toml`
- Create: `requirements-test.txt`

- [ ] **Step 1: Add the test extra and canonical pytest configuration**

Add to `pyproject.toml`:

```toml
[project.optional-dependencies]
test = [
    "pytest==9.1.1",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Keep the existing `service` extra unchanged; append `test` beside it rather than replacing it.

- [ ] **Step 2: Add the persistent test-tool installation entrypoint**

Create `requirements-test.txt` with:

```text
# Test tooling layered onto the existing bond_factor_lab_service environment.
pytest==9.1.1
```

- [ ] **Step 3: Install the declared test extra once into the shared service environment**

Run:

```bash
conda run -n bond_factor_lab_service \
  python -m pip install -r requirements-test.txt
```

Expected: exit code 0; pytest 9.1.1 is installed without a temporary `PYTHONPATH`.

- [ ] **Step 4: Verify persistent import and full collection**

Run:

```bash
conda run -n bond_factor_lab_service python -c \
  'import pytest; print(pytest.__version__)'
conda run -n bond_factor_lab_service python -m pytest --collect-only -q
```

Expected: pytest prints `9.1.1`; collection includes all unittest tests and the previously skipped 242 top-level pytest functions, with at least 3161 tests collected.

- [ ] **Step 5: Run the true pre-cleanup baseline**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q
```

Expected: exit code 0. If pytest exposes a previously hidden failure, stop deletion work, diagnose the failure, and do not delete that test to obtain green status.

- [ ] **Step 6: Commit the test infrastructure**

Before staging, run `git status --short`, `git branch --show-current`, and `git diff --check`.
Then commit only the two dependency files:

```bash
git add pyproject.toml requirements-test.txt
git commit -m "test: make pytest the canonical test runner"
```

### Task 2: Remove redundant transition and onboarding tests

**Files:**
- Delete: `tests/test_docs_1y_target_visibility.py`
- Delete: `tests/test_cgb_causal_wk_1y_onboarding.py`
- Delete: `tests/test_cgb_causal_wk_3y_onboarding.py`
- Delete: `tests/test_wavg_gapflip_v5_onboarding.py`
- Test: `tests/test_onboarding_docs.py`
- Test: `tests/test_blackbox_scheduler_admission.py`
- Test: `tests/test_blackbox_v2_discovery.py`
- Test: `tests/test_scheduler_main.py`

- [ ] **Step 1: Record the exact redundant test count**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest --collect-only -q \
  tests/test_docs_1y_target_visibility.py \
  tests/test_cgb_causal_wk_1y_onboarding.py \
  tests/test_cgb_causal_wk_3y_onboarding.py \
  tests/test_wavg_gapflip_v5_onboarding.py
```

Expected: 7 tests collected.

- [ ] **Step 2: Confirm replacement guards before deletion**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q \
  tests/test_onboarding_docs.py \
  tests/test_blackbox_scheduler_admission.py \
  tests/test_blackbox_v2_discovery.py \
  tests/test_scheduler_main.py
```

Expected: exit code 0. These generic tests guard current docs, exact Blackbox admission identities, version hashing/platform input validation, and scheduler mounting.

- [ ] **Step 3: Delete the four redundant test modules**

Use `apply_patch` to delete exactly the four files listed above. Do not delete coordinator, Actuals, migration, source-fidelity, destructive-script, or repository tests.

- [ ] **Step 4: Re-run replacement guards and full pytest**

Run the replacement-guard command from Step 2, then:

```bash
conda run -n bond_factor_lab_service python -m pytest -q
```

Expected: both commands exit 0; the full collected test count decreases by exactly 7 from Task 1.

- [ ] **Step 5: Commit the test cleanup**

Before staging, inspect status, branch, diff, and `git diff --check`. Then:

```bash
git add \
  tests/test_docs_1y_target_visibility.py \
  tests/test_cgb_causal_wk_1y_onboarding.py \
  tests/test_cgb_causal_wk_3y_onboarding.py \
  tests/test_wavg_gapflip_v5_onboarding.py
git commit -m "test: remove completed onboarding regressions"
```

### Task 3: Delete completed plans and intermediate design artifacts

**Files:**
- Delete: `docs/superpowers/README.md`
- Delete: `docs/superpowers/plans/*.md`
- Delete: `docs/superpowers/specs/*.md`
- Verify: `docs/README.md`
- Verify: `docs/architecture/**`
- Verify: `docs/sop/**`
- Verify: `docs/CURRENT_STATUS.md`

- [ ] **Step 1: Prove current documents do not depend on Superpowers records**

Run:

```bash
rg -n 'docs/superpowers|superpowers/(plans|specs)|2026-[0-9]{2}-[0-9]{2}-.*-(design|onboarding|cleanup)\.md' \
  . \
  --glob '!docs/superpowers/**' \
  --glob '!outputs/**' \
  --glob '!reports/**'
```

Expected: no current-document references. If references exist, inspect each one; migrate only a missing current rule before removing its process document.

- [ ] **Step 2: Delete the complete Superpowers documentation tree**

Use `apply_patch` to delete every tracked file returned by:

```bash
rg --files docs/superpowers | sort
```

This intentionally deletes this implementation plan and the approved design after their steps have been loaded for execution.

- [ ] **Step 3: Verify current documentation remains complete**

Run:

```bash
test ! -e docs/superpowers
conda run -n bond_factor_lab_service python -m pytest -q tests/test_onboarding_docs.py
if rg -n 'docs/superpowers|superpowers/(plans|specs)' \
  docs README.md AGENTS.md CLAUDE.md; then
  exit 1
fi
```

Expected: the directory is absent; onboarding documentation tests pass; the final `rg` returns no matches.

- [ ] **Step 4: Commit the documentation cleanup**

Before staging, inspect status, branch, deletion list, and `git diff --check`. Then:

```bash
git add -u docs/superpowers
git commit -m "docs: remove completed implementation records"
```

### Task 4: Final verification and branch preservation

**Files:**
- Verify only; no expected source changes.

- [ ] **Step 1: Run the complete canonical suite**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q
```

Expected: exit code 0 and the count equals Task 1 baseline minus 7.

- [ ] **Step 2: Run compile and stale-reference checks**

Run:

```bash
conda run -n bond_factor_lab_service python -m compileall -q \
  backend scheduler shared harness backtests schemes tests scripts
test ! -e docs/superpowers
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 3: Prove production behavior boundaries were untouched**

Run:

```bash
git diff --exit-code 307a6c3..HEAD -- \
  scheduler backend shared harness backtests scripts migrations schemes \
  deploy/launchd
```

Expected: no output. This batch changes only dependency declarations, tests, and intermediate docs.

- [ ] **Step 4: Verify final Git state**

Run:

```bash
git status --short --branch
git branch --show-current
git rev-parse master origin/master
git rev-list --left-right --count \
  origin/codex/audit-bugfixes-20260613...HEAD
```

Expected: clean current branch `codex/audit-bugfixes-20260613`; local and remote master remain identical; no push has occurred.
