# Task-Cell Red Accuracy Highlight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the task-cell green background highlight with a red accuracy number when the existing `>= 60%` rule applies.

**Architecture:** The existing JavaScript already decides whether a task cell has `is-accuracy-highlighted`; it remains the sole source of the threshold, metric, sorting, and finite-value behavior. CSS will target the existing `.factor-task-top` descendant, so selected and hover backgrounds remain independent. The existing frontend style-contract test will express the new visual contract.

**Tech Stack:** Native CSS, existing browser JavaScript rendering, Python `pytest` frontend contract tests.

---

### Task 1: Specify the red-number visual contract

**Files:**
- Modify: `tests/test_frontend_factor_lab.py:3155-3228`
- Test: `tests/test_frontend_factor_lab.py::FactorLabRankingTests::test_factor_lab_compact_layout_and_task_highlight_style_contract`

- [ ] **Step 1: Replace the old green-background expectation with a failing red-number expectation**

Change the selector lookup and assertions in the existing CSS contract from the parent task-cell rule to the number rule:

```python
highlight_rule = _css_rule(
    ".factor-task-cell.is-accuracy-highlighted .factor-task-top"
)

self.assertEqual(
    [line.strip() for line in highlight_rule.splitlines() if line.strip()],
    ["color: var(--negative);"],
)
self.assertNotIn(
    ".factor-task-cell.is-accuracy-highlighted {\n  background:",
    css,
)
self.assertNotIn(
    ".factor-task-cell.is-accuracy-highlighted {\n  box-shadow:",
    css,
)
```

Keep the existing `selected_rule` assertions unchanged so the test continues to prove hover and selected backgrounds remain available.

- [ ] **Step 2: Run the focused test to verify it fails for the intended reason**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q tests/test_frontend_factor_lab.py \
-k 'compact_layout_and_task_highlight_style_contract'
```

Expected: FAIL because the red descendant selector is absent while the current parent selector still supplies green `background` and `box-shadow` declarations.

### Task 2: Apply the minimal CSS-only visual change

**Files:**
- Modify: `frontend/aifin-shell.css:699-708`
- Test: `tests/test_frontend_factor_lab.py::FactorLabRankingTests::test_factor_lab_compact_layout_and_task_highlight_style_contract`

- [ ] **Step 1: Replace the green task-cell rule with the red-number rule**

Replace:

```css
.factor-task-cell.is-accuracy-highlighted {
  background: rgba(21, 92, 62, 0.05);
  box-shadow: inset 0 0 0 1px rgba(21, 92, 62, 0.15);
}
```

with:

```css
.factor-task-cell.is-accuracy-highlighted .factor-task-top {
  color: var(--negative);
}
```

Do not change `renderTaskOverview()`, the `is-accuracy-highlighted` predicate, `.factor-task-cell:hover`, `.factor-task-cell.is-selected`, or `.factor-task-top` base typography.

- [ ] **Step 2: Run the focused test to verify it passes**

Run the command from Task 1, Step 2.

Expected: `1 passed`.

- [ ] **Step 3: Run the full frontend regression suite**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q tests/test_frontend_factor_lab.py \
tests/test_frontend_static_cache.py
```

Expected: all tests pass; existing FastAPI `on_event` deprecation warnings may remain.

- [ ] **Step 4: Inspect the isolated diff and commit only the implementation files**

Run:

```bash
git diff --check
git status --short
git add frontend/aifin-shell.css tests/test_frontend_factor_lab.py
git commit -m "fix(frontend): use red task-cell accuracy highlight"
```

Expected: only the CSS and frontend test are included in this implementation commit. The already committed design and plan documents remain separate commits.
