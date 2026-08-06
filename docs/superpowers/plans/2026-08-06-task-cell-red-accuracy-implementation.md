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

### Task 3: Deliver the CSS revision through a content-addressed immutable URL

**Files:**
- Modify: `frontend/index.html:13`
- Modify: `tests/test_frontend_static_cache.py:154-211`
- Modify: `tests/test_frontend_factor_lab.py:507-518, 3129-3134`
- Modify: `docs/superpowers/specs/2026-08-06-task-cell-red-accuracy-design.md`
- Modify: `docs/superpowers/plans/2026-08-06-task-cell-red-accuracy-implementation.md`

- [ ] **Step 1: Set the digest-based CSS cache contract before changing HTML**

Update `test_index_uses_current_asset_cache_buster()` to calculate the complete
SHA-256 of `frontend/aifin-shell.css` bytes and require the parsed real index's
CSS `v=` token set to equal that digest exactly. The test must also instantiate
`NoCacheFrontendStaticFiles` against the real `frontend/` root, require the
current CSS URL to be immutable, and require a stale CSS URL to be
no-cache/revalidate. Retain the exact `aifin-shell.js?v=20260806a` contract.
Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q tests/test_frontend_static_cache.py \
-k 'index_uses_current_asset_cache_buster'
```

Expected: FAIL while `index.html` still references the prior calendar-style
token, proving that CSS content changes cannot reuse an immutable URL.

- [ ] **Step 2: Advance only the CSS content token and synchronize index contracts**

Set the stylesheet URL in `frontend/index.html` to
`aifin-shell.css?v=<complete-css-sha256>`. Update the HTML parser assertion in
`tests/test_frontend_factor_lab.py` to derive the same digest dynamically, then
synchronize its exact index SHA-256 guard. Do not change the JavaScript URL or
its `20260806a` assertions.

- [ ] **Step 3: Record the immutable CSS delivery requirement**

Document that the backend serves only the exact current CSS content-hash token
with `public, max-age=31536000, immutable`; every delivered CSS revision
therefore needs the full SHA-256 of its bytes as a new token, while an unchanged
JS asset retains its token.

- [ ] **Step 4: Verify cache delivery and preserve separate commit boundaries**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q tests/test_frontend_factor_lab.py \
tests/test_frontend_static_cache.py
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q tests/test_onboarding_docs.py
git diff --check
```

Commit the two documentation files separately from the index and test files.
