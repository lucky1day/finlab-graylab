# Factor Lab Equivalent Frontend Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove only audited dead frontend code while proving the existing gray-lab page, shell, behavior, requests, and rendering remain equivalent.

**Architecture:** Keep the current single-file HTML/CSS/JS architecture and all runtime contracts. Add negative cleanup assertions plus positive preservation assertions, delete one audited category at a time, and compare the candidate against immutable baseline commit `448da2f` through tests, deterministic browser rendering, and direct/OOPIF probes.

**Tech Stack:** Native HTML/CSS/JavaScript, Python `unittest`/`pytest`, Node VM, Microsoft Edge CDP, existing Factor Lab browser benchmark.

---

## File map

| File | Responsibility | Planned action |
|---|---|---|
| `tests/test_frontend_factor_lab.py` | Frontend state-machine and DOM/CSS contracts | Add RED cleanup and preservation assertions |
| `frontend/aifin-shell.js` | Routing, loading, decoding, state, rendering, interactions | Delete only audited zero-call/unreachable code |
| `frontend/aifin-shell.css` | Shell and gray-lab presentation | Delete only audited zero-generation selectors |
| `frontend/index.html` | Existing page DOM | Must remain byte-identical |
| `frontend/assets/*.svg` | Existing AIFin branding | Must remain byte-identical |
| `/tmp/factor-lab-equivalent-cleanup/` | Non-committed acceptance evidence | Store DOM, screenshots, request and performance reports |

Implementation must not modify backend, Nginx, deployment files, API contracts, `master`, or production.

### Task 1: Lock the JavaScript boundary and remove zero-call code

**Files:**
- Modify: `tests/test_frontend_factor_lab.py`
- Modify: `frontend/aifin-shell.js`
- Verify unchanged: `frontend/index.html`
- Verify unchanged: `frontend/assets/aifin-lab-icon.svg`
- Verify unchanged: `frontend/assets/aifin-lab-logo.svg`

- [ ] **Step 1: Add imports and the failing JavaScript cleanup contract**

Add `hashlib` to the standard-library imports in `tests/test_frontend_factor_lab.py`:

```python
import copy
import hashlib
import json
```

Add the following test to `FactorLabRankingTests`:

```python
def test_equivalent_cleanup_removes_only_audited_dead_javascript(self) -> None:
    script = FRONTEND_SCRIPT.read_text(encoding="utf-8")
    index = FRONTEND_INDEX.read_bytes()
    icon = (PROJECT_ROOT / "frontend/assets/aifin-lab-icon.svg").read_bytes()
    logo = (PROJECT_ROOT / "frontend/assets/aifin-lab-logo.svg").read_bytes()

    dead_markers = (
        "var factorStatusLabels =",
        "var factorSchemeNamePool =",
        "function makeMonthRows(",
        "function makeMockDetailRowsByMonth(",
        "function createTaskSchemes(",
        "function fetchLiveFactorLabTasks(",
        "function loadBacktestFactorLabData(",
        "function loadBacktestFactorLabDataSilent(",
        'scanline.className = "route-scanline"',
    )
    for marker in dead_markers:
        self.assertNotIn(marker, script)

    protected_markers = (
        'var PUBLIC_BASE_PATH = "/bond-factor-lab"',
        "function apiUrl(",
        "function normalizeRoute(",
        "function setActiveRoute(",
        'data.type !== "aifin:navigate"',
        "var factorLabRuntimeState =",
        "function decodeDashboardPayload(",
        "function fetchLegacyFactorLabCandidate(",
        "window.__factorLabReady",
        "window.__factorLabTestHooks",
    )
    for marker in protected_markers:
        self.assertIn(marker, script)

    self.assertEqual(
        hashlib.sha256(index).hexdigest(),
        "bf72d27941b76153c6214c8f5f02e680919c40d9256f8f644aea4034fdbf625c",
    )
    self.assertEqual(
        hashlib.sha256(icon).hexdigest(),
        "e014fc86d69d61a32892b9799f83f8c784898d705c8df05313a04216281d2259",
    )
    self.assertEqual(
        hashlib.sha256(logo).hexdigest(),
        "fdb09795b77161900b6e48982a7678f9038786ba80c9838f2f80e22500fef5b5",
    )
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
PYTHONPATH=/Users/macstudio0/bond-factor-lab/.worktrees/factor-lab-subsecond-dashboard/.venv/testdeps:. \
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m pytest -q \
  tests/test_frontend_factor_lab.py::FactorLabRankingTests::test_equivalent_cleanup_removes_only_audited_dead_javascript
```

Expected: FAIL because `var factorStatusLabels =` and the other audited dead markers still exist. The three SHA assertions must not be the cause.

- [ ] **Step 3: Delete only the allowed JavaScript blocks**

In `frontend/aifin-shell.js`:

1. Delete `isRouting` and `reduceMotionQuery`.
2. Delete the complete `factorStatusLabels` object.
3. Delete the complete `factorSchemeNamePool` array.
4. Delete the complete definitions of:
   - `makeMonthRows`;
   - `makeMockDetailRowsByMonth`;
   - `createTaskSchemes`;
   - `fetchLiveFactorLabTasks`;
   - `loadBacktestFactorLabData`;
   - `loadBacktestFactorLabDataSilent`.
5. Delete `getViewForRoute`.
6. Replace only the body of `navigateWithTransition` with:

```javascript
function navigateWithTransition(route) {
  setActiveRoute(route, true);
}
```

Do not delete or change:

```javascript
factorDailyBaseRows
factorWeeklyBaseRows
factorDailyRows
factorWeeklyRows
factorLabDataMode === "mock"
publicBasePath
apiUrl
normalizeRoute
routeUrl
setActiveRoute
getActiveView
fetchLegacyFactorLabCandidate
window.__factorLabReady
window.__factorLabTestHooks
```

- [ ] **Step 4: Run syntax, focused, and full frontend tests**

Run:

```bash
node --check frontend/aifin-shell.js
PYTHONPATH=/Users/macstudio0/bond-factor-lab/.worktrees/factor-lab-subsecond-dashboard/.venv/testdeps:. \
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m pytest -q \
  tests/test_frontend_factor_lab.py::FactorLabRankingTests::test_equivalent_cleanup_removes_only_audited_dead_javascript
PYTHONPATH=/Users/macstudio0/bond-factor-lab/.worktrees/factor-lab-subsecond-dashboard/.venv/testdeps:. \
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m pytest -q tests/test_frontend_factor_lab.py
```

Expected:

- `node --check` exits 0;
- focused test passes;
- all frontend tests pass, with no new warnings;
- `frontend/index.html` and both SVGs remain unchanged.

- [ ] **Step 5: Self-review and commit Task 1**

Run:

```bash
git diff --check
git diff -- frontend/aifin-shell.js tests/test_frontend_factor_lab.py
git status --short
git add frontend/aifin-shell.js tests/test_frontend_factor_lab.py
git diff --cached --check
git commit -m "refactor: remove dead factor lab javascript"
```

The staged diff must contain no CSS, HTML, assets, backend, deployment, or output files.

### Task 2: Lock the CSS boundary and remove zero-generation styles

**Files:**
- Modify: `tests/test_frontend_factor_lab.py`
- Modify: `frontend/aifin-shell.css`
- Verify unchanged: `frontend/index.html`

- [ ] **Step 1: Add the failing CSS cleanup and shell-preservation contract**

Add this test to `FactorLabRankingTests`:

```python
def test_equivalent_cleanup_removes_only_audited_dead_css(self) -> None:
    css = FRONTEND_CSS.read_text(encoding="utf-8")

    dead_selectors = (
        ".eyebrow {",
        ".module-view .eyebrow {",
        ".factor-stack {",
        ".factor-stack span {",
        ".factor-matrix {",
        ".factor-filter-group.is-hidden {",
        ".factor-refresh-btn {",
        ".factor-accuracy-panel {",
        ".factor-scheme-badge {",
        ".factor-live-since-badge {",
        ".factor-accuracy-wrap {",
        ".factor-accuracy-table {",
        ".factor-accuracy-score {",
        ".factor-accuracy-value {",
        ".factor-accuracy-track {",
        ".factor-status-pill {",
        ".route-scanline {",
        "@keyframes scanline-sweep {",
        "@keyframes scanline-glow {",
    )
    for selector in dead_selectors:
        self.assertNotIn(selector, css)

    dead_variables = (
        "--bg-elevated:",
        "--surface-dark:",
        "--accent-soft:",
        "--gold-light:",
        "--positive:",
        "--shadow-md:",
        "--font-display:",
    )
    for variable in dead_variables:
        self.assertNotIn(variable, css)

    protected_selectors = (
        ".aifin-shell {",
        ".aifin-topbar {",
        ".brand-button {",
        ".main-nav {",
        ".status-strip {",
        ".shell-stage {",
        ".module-view {",
        ".factor-lab-hero {",
        ".factor-accuracy-meta {",
        ".factor-matrix-panel {",
        ".factor-live-divider td {",
        ".factor-trend-divider {",
        ".factor-calendar-drawer {",
        "@media (max-width: 980px) {",
        "@media (max-width: 620px) {",
        "@media (prefers-reduced-motion: reduce) {",
    )
    for selector in protected_selectors:
        self.assertIn(selector, css)
```

- [ ] **Step 2: Run the new CSS test and verify RED**

Run:

```bash
PYTHONPATH=/Users/macstudio0/bond-factor-lab/.worktrees/factor-lab-subsecond-dashboard/.venv/testdeps:. \
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m pytest -q \
  tests/test_frontend_factor_lab.py::FactorLabRankingTests::test_equivalent_cleanup_removes_only_audited_dead_css
```

Expected: FAIL because `.eyebrow {` and the other audited dead selectors still exist. Protected selector assertions must not be the cause.

- [ ] **Step 3: Delete complete dead CSS rules and fix shared selector lists**

Delete the complete rules for the dead selectors listed in Step 1, including:

- all `.factor-stack` variants;
- `.factor-matrix` only, never `.factor-matrix-panel`;
- both `.factor-live-since-badge` rules;
- all `.factor-accuracy-*` legacy table rules listed in the test, never `.factor-accuracy-meta`;
- all `.factor-status-pill` variants;
- `.route-scanline`, both pseudo-elements, and both scanline keyframes;
- the second duplicate `.factor-sample-badge` rule containing only `color`.

Update shared selector lists exactly:

```css
.factor-lab-hero,
.factor-filter-bar,
.factor-matrix-panel,
.factor-task-panel,
.factor-ranking-panel {
```

```css
.factor-filter-group button,
.factor-pagination button,
.factor-calendar-head button,
.factor-calendar-link {
```

```css
.factor-range-badge {
```

and make the same removals in the 980px/620px media queries. Delete the dead variables only after `rg` confirms their remaining occurrence count is exactly one.

Do not alter declarations belonging to any protected selector.

- [ ] **Step 4: Run focused and full frontend tests**

Run:

```bash
PYTHONPATH=/Users/macstudio0/bond-factor-lab/.worktrees/factor-lab-subsecond-dashboard/.venv/testdeps:. \
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m pytest -q \
  tests/test_frontend_factor_lab.py::FactorLabRankingTests::test_equivalent_cleanup_removes_only_audited_dead_css
PYTHONPATH=/Users/macstudio0/bond-factor-lab/.worktrees/factor-lab-subsecond-dashboard/.venv/testdeps:. \
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m pytest -q tests/test_frontend_factor_lab.py
```

Expected: focused CSS contract and all frontend tests pass.

- [ ] **Step 5: Prove no dead selector token remains and commit Task 2**

Run:

```bash
rg -n 'factor-refresh-btn|factor-accuracy-panel|factor-scheme-badge|factor-live-since-badge|factor-status-pill|route-scanline|scanline-(sweep|glow)' \
  frontend/aifin-shell.css
git diff --check
git diff -- frontend/aifin-shell.css tests/test_frontend_factor_lab.py
git status --short
```

Expected: `rg` exits 1 with no matches. Then:

```bash
git add frontend/aifin-shell.css tests/test_frontend_factor_lab.py
git diff --cached --check
git commit -m "refactor: remove dead factor lab styles"
```

The staged diff must not contain HTML, assets, backend, deployment, or output files.

### Task 3: Run independent equivalence and performance acceptance

**Files:**
- Read: `docs/superpowers/specs/2026-07-23-factor-lab-equivalent-frontend-cleanup-design.md`
- Read: `frontend/index.html`
- Read: `frontend/aifin-shell.js`
- Read: `frontend/aifin-shell.css`
- Output only: `/tmp/factor-lab-equivalent-cleanup/`

- [ ] **Step 1: Verify source scope and immutable assets**

Run:

```bash
mkdir -p /tmp/factor-lab-equivalent-cleanup
git diff --name-status 448da2fc9e3b9198909526953293aa5ab34d3031..HEAD
shasum -a 256 \
  frontend/index.html \
  frontend/assets/aifin-lab-icon.svg \
  frontend/assets/aifin-lab-logo.svg
git diff --check
```

Expected:

- runtime changes are limited to JS, CSS, and their tests;
- index hash is `bf72d27941b76153c6214c8f5f02e680919c40d9256f8f644aea4034fdbf625c`;
- icon hash is `e014fc86d69d61a32892b9799f83f8c784898d705c8df05313a04216281d2259`;
- logo hash is `fdb09795b77161900b6e48982a7678f9038786ba80c9838f2f80e22500fef5b5`.

- [ ] **Step 2: Run all related automated tests**

Run:

```bash
node --check frontend/aifin-shell.js
PYTHONPATH=/Users/macstudio0/bond-factor-lab/.worktrees/factor-lab-subsecond-dashboard/.venv/testdeps:. \
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m pytest -q \
  tests/test_frontend_factor_lab.py \
  tests/test_frontend_static_cache.py \
  tests/test_public_access_config.py \
  tests/test_factor_lab_dashboard_api.py \
  tests/test_factor_lab_performance_tools.py
```

Expected: zero failures. Existing FastAPI `on_event` deprecation warnings may be reported but no new warning class is allowed.

- [ ] **Step 3: Compare deterministic DOM, text, classes, controls, and requests**

Serve the baseline checkout `/Users/macstudio0/bond-factor-lab` and candidate worktree with the same fixed `_dashboard_payload()` from `tests/test_factor_lab_performance_tools.py`. In Edge, freeze `Date.now`, disable animation, and capture for both versions:

```javascript
({
  html: document.documentElement.outerHTML,
  status: document.getElementById("factorDataStatus").className,
  statusText: document.getElementById("factorDataStatusText").textContent,
  startMonth: document.getElementById("factorStartMonth").value,
  endMonth: document.getElementById("factorEndMonth").value,
  source: document.getElementById("factorDataSource").value,
  taskHtml: document.getElementById("factorTaskMatrixBody").innerHTML,
  rankingHtml: document.getElementById("factorSchemeRankingBody").innerHTML,
  monthlyHtml: document.getElementById("factorMonthlyTableBody").innerHTML,
  ready: window.__factorLabReady
})
```

Write normalized results to:

```text
/tmp/factor-lab-equivalent-cleanup/baseline-dom.json
/tmp/factor-lab-equivalent-cleanup/candidate-dom.json
```

Expected: exact equality after removing only browser-internal serialization noise. Each browser attempt must make exactly one dashboard request and zero legacy requests.

- [ ] **Step 4: Capture and compare deterministic screenshots**

For both baseline and candidate, use the same Edge version, DPR 1, fixed dashboard payload, reduced motion, and these viewports:

```text
1440x1000
980x900
620x900
375x812
```

Capture fresh, stale/LKG, error empty-state, selected ranking, changed trend toggles, and open drawer states under:

```text
/tmp/factor-lab-equivalent-cleanup/screenshots/baseline/
/tmp/factor-lab-equivalent-cleanup/screenshots/candidate/
```

Expected: pixel-identical images. If a diff appears, repeat baseline-to-baseline first; any candidate-only business-region difference fails acceptance.

- [ ] **Step 5: Run direct and cross-origin OOPIF canaries**

Run the existing real Edge smoke tests:

```bash
FACTOR_LAB_RUN_EDGE_SMOKE=1 \
PYTHONPATH=/Users/macstudio0/bond-factor-lab/.worktrees/factor-lab-subsecond-dashboard/.venv/testdeps:. \
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m pytest -q \
  tests/test_factor_lab_performance_tools.py::test_real_edge_smoke_and_short_soak_use_actual_frontend \
  tests/test_factor_lab_performance_tools.py::test_real_edge_cross_origin_iframe_uses_child_ready_and_requests
```

Then run 20 direct and 20 synthetic cross-origin iframe attempts with `scripts/benchmark_factor_lab_browser.py`, the same fixed payload, Edge approval metadata, and reports:

```text
/tmp/factor-lab-equivalent-cleanup/direct-20.json
/tmp/factor-lab-equivalent-cleanup/iframe-20.json
```

Expected for both reports:

- 20/20 success;
- P95 below 1000ms;
- zero legacy, stale, cache, redirect, console, and page errors;
- one dashboard request per attempt;
- scheme/live/backtest counts equal the fixed fixture.

- [ ] **Step 6: Run final scope and repository checks**

Run:

```bash
git diff --check
git status --short
git diff --stat 448da2fc9e3b9198909526953293aa5ab34d3031..HEAD
git diff --name-status 448da2fc9e3b9198909526953293aa5ab34d3031..HEAD
```

Expected:

- no uncommitted task changes;
- no `outputs/` files;
- no HTML, SVG, backend, deployment, Nginx, database, scheduler, or scheme changes;
- frontend JS and CSS raw byte counts do not increase.

Do not merge, push, deploy, restart services, or change `master`.

### Task 4: Final independent review

**Files:**
- Review: all changes from `448da2f..HEAD`
- Review: `/tmp/factor-lab-equivalent-cleanup/` evidence

- [ ] **Step 1: Dispatch spec-compliance review**

Give the reviewer the complete design and this plan. Require explicit answers for:

- every deletion belongs to the allowlist;
- every protected runtime and page contract remains;
- HTML and SVG hashes match;
- automated, DOM, screenshot, request and performance gates passed;
- no production state changed.

Expected: no open Critical or Important issue.

- [ ] **Step 2: Dispatch code-quality review**

Review the same commit range for:

- tests that could pass without detecting an accidental deletion;
- partial CSS block deletion or dangling selectors;
- hidden references to deleted JavaScript;
- formatting-only churn;
- untracked or unrelated files.

Expected: no open Critical or Important issue.

- [ ] **Step 3: Report branch-ready status without publishing**

Report exact commits, deleted line/byte counts, test counts, browser evidence, known baseline-only exceptions, and the explicit fact that production remains unchanged.

Do not claim production completion until a separately authorized rollout and formal 200-attempt public acceptance have passed.
