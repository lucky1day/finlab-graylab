# Candidate Ranking Density and Remark Detail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compress four ranking columns and replace truncated remarks with a readable, accessible detail popover while preserving all ranking data and behavior.

**Architecture:** Keep the existing nine-column table, Dashboard payload, ranking metrics, sorting, and row selection unchanged. Add one shared non-modal popover to `index.html`; ranking rows only render a native trigger button, while small presentation-only functions in `aifin-shell.js` open, position, and close the popover using the existing scheme remark. CSS owns column density, alignment, responsive bounds, and readable popover styling.

**Tech Stack:** Static HTML, vanilla JavaScript, CSS, Python pytest contract tests, in-app browser verification.

---

### Task 1: Lock the display-only contract with failing tests

**Files:**
- Create: `tests/test_frontend_ranking_remark_detail_contract.py`
- Modify: none

- [ ] **Step 1: Write the failing structure and invariant tests**

Add tests that read the static frontend sources and assert:

```python
def test_ranking_keeps_current_overall_accuracy_markup() -> None:
    body = _ranking_row_renderer()
    assert "factor-score-cell" in body
    assert "metric.correct + '/' + metricSamples" in body
    assert "factor-score-bar" in body


def test_ranking_uses_detail_button_instead_of_truncated_remark() -> None:
    body = _ranking_row_renderer()
    assert 'data-factor-remark-open' in body
    assert 'aria-expanded="false"' in body
    assert "factor-remark-text" not in body


def test_remark_popover_is_single_accessible_dialog() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert html.count('id="factorRemarkPopover"') == 1
    assert 'role="dialog"' in html
    assert 'aria-labelledby="factorRemarkTitle"' in html
    assert 'data-factor-remark-close' in html
```

Also assert exact CSS width rules: third column remains `180px`, seventh remains `120px`, while columns four, five, six, and eight become `82px`, `112px`, `112px`, and `84px`. Assert JavaScript includes trigger-first click handling, `stopPropagation()`, outside-click closing, `Escape` closing, and focus restoration.

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest -q tests/test_frontend_ranking_remark_detail_contract.py
```

Expected: failures for missing `factorRemarkPopover`, `data-factor-remark-open`, and the old column widths.

- [ ] **Step 3: Commit the failing contract test**

```bash
git add tests/test_frontend_ranking_remark_detail_contract.py
git commit -m "test(frontend): specify compact ranking remark details"
```

### Task 2: Add the shared remark detail surface

**Files:**
- Modify: `frontend/index.html:101-125`
- Modify: `frontend/aifin-shell.js:2168-2185,2627-2761`
- Modify: `frontend/aifin-shell.css:565-750`
- Test: `tests/test_frontend_ranking_remark_detail_contract.py`

- [ ] **Step 1: Add one non-modal dialog after the ranking table wrapper**

Use this structure inside `.factor-ranking-panel`, after `.factor-ranking-wrap`:

```html
<aside class="factor-remark-popover" id="factorRemarkPopover" role="dialog"
  aria-modal="false" aria-hidden="true" aria-labelledby="factorRemarkTitle" hidden>
  <div class="factor-remark-popover-head">
    <strong id="factorRemarkTitle">方案备注</strong>
    <button type="button" data-factor-remark-close aria-label="关闭方案备注">×</button>
  </div>
  <div class="factor-remark-popover-body" id="factorRemarkBody"></div>
</aside>
```

- [ ] **Step 2: Render a safe native trigger without changing ranking metrics**

Keep the existing `factor-score-cell`, percentage/count expression, and `factor-score-bar` line byte-for-byte. Replace only the ninth cell with a button when the existing remark is non-empty, otherwise render `--`:

```javascript
var remarkControl = remark
  ? '<button type="button" class="factor-remark-detail" data-factor-remark-open="' +
      escapeHtml(scheme.id) + '" aria-controls="factorRemarkPopover" aria-expanded="false">' +
      '<span aria-hidden="true">ⓘ</span><span>详情</span></button>'
  : '<span class="factor-remark-empty">--</span>';
```

Do not put the full remark in an HTML attribute. Resolve the selected task scheme by ID when opening, then assign the remark with `textContent`.

- [ ] **Step 3: Add focused open, place, and close functions**

Implement small presentation-only functions:

```javascript
function placeFactorRemarkPopover(trigger) {
  var popover = document.getElementById("factorRemarkPopover");
  if (!popover || !trigger) return;
  var margin = 12;
  var gap = 8;
  var triggerRect = trigger.getBoundingClientRect();
  var width = popover.offsetWidth;
  var height = popover.offsetHeight;
  var left = Math.min(Math.max(margin, triggerRect.right - width), window.innerWidth - width - margin);
  var top = triggerRect.bottom + gap;
  if (top + height > window.innerHeight - margin) {
    top = Math.max(margin, triggerRect.top - height - gap);
  }
  popover.style.left = Math.round(left) + "px";
  popover.style.top = Math.round(top) + "px";
}
```

`openFactorRemark()` must set `textContent`, `hidden=false`, `aria-hidden=false`, and trigger `aria-expanded=true`, position the popover, and focus the close button. `closeFactorRemark(restoreFocus)` must hide it, clear position, reset `aria-expanded`, and optionally focus the connected trigger.

- [ ] **Step 4: Bind trigger-first interaction and dismissal**

In the existing factor page click handler, process `[data-factor-remark-open]` before `[data-factor-scheme-id]`, call `preventDefault()` and `stopPropagation()`, then open the popover and return. Handle `[data-factor-remark-close]` similarly. Add a document click handler that closes an open popover only when the click is outside the popover and outside a remark trigger. Extend the existing `Escape` handler to close the popover before closing the calendar.

- [ ] **Step 5: Add compact widths and readable responsive CSS**

Keep third and seventh widths unchanged at `180px` and `120px`. Set fourth through sixth to `82px`, `112px`, `112px`; set eighth to `84px`; give the ninth a compact `96px` trigger area. Center columns four through nine without changing the overall accuracy cell structure. Style the popover as `position: fixed`, `width: min(440px, calc(100vw - 24px))`, with a `190px` scrollable body, `14px` text, `1.75` line height, visible focus, and no backdrop.

- [ ] **Step 6: Run the contract tests and verify GREEN**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest -q \
  tests/test_frontend_ranking_remark_detail_contract.py \
  tests/test_frontend_ranking_contract.py \
  tests/test_frontend_owner_column_contract.py
```

Expected: all tests pass.

- [ ] **Step 7: Commit the implementation**

```bash
git add frontend/index.html frontend/aifin-shell.js frontend/aifin-shell.css
git commit -m "feat(frontend): add compact ranking remark details"
```

### Task 3: Refresh asset versions and run regression verification

**Files:**
- Modify: `frontend/index.html`
- Test: `tests/test_frontend_asset_versions.py`
- Test: `tests/test_dashboard_contract_no_drift.py`
- Test: `tests/test_dashboard_gate.py`

- [ ] **Step 1: Run the asset-version test and verify RED**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest -q tests/test_frontend_asset_versions.py
```

Expected: failure because the JavaScript and CSS content hashes in `index.html` are stale.

- [ ] **Step 2: Update both frontend content hashes**

Calculate SHA-256 for `frontend/aifin-shell.js` and `frontend/aifin-shell.css`, then replace only their corresponding `?v=` values in `frontend/index.html`.

- [ ] **Step 3: Run focused frontend and Dashboard regressions**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest -q \
  tests/test_frontend_asset_versions.py \
  tests/test_frontend_ranking_contract.py \
  tests/test_frontend_ranking_remark_detail_contract.py \
  tests/test_frontend_owner_column_contract.py \
  tests/test_dashboard_contract_no_drift.py \
  tests/test_factor_lab_dashboard_api.py \
  tests/test_dashboard_gate.py
```

Expected: all tests pass.

- [ ] **Step 4: Run the complete suite**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest -q
```

Expected: exit code 0 with no failed tests.

- [ ] **Step 5: Commit the asset hash update**

```bash
git add frontend/index.html
git commit -m "chore(frontend): refresh ranking asset versions"
```

### Task 4: Verify the real page at desktop and narrow widths

**Files:**
- Modify: none

- [ ] **Step 1: Verify desktop layout and unchanged accuracy presentation**

Open the current app in the in-app browser at desktop width. Confirm the page reaches “数据已就绪”; the ranking has nine headers; “区间准确率” remains a sort button with `DESC`; the percentage/count and green progress bar remain visible; the four requested columns are visibly narrower; clicking “详情” does not select another scheme.

- [ ] **Step 2: Verify popover interaction and accessibility**

Confirm one 440px-bounded popover opens near the trigger, shows the complete remark without two-line truncation, does not add a backdrop or change row height, closes by close button/outside click/`Escape`, and returns focus to the trigger.

- [ ] **Step 3: Verify narrow layout**

At a 390px viewport, confirm the table remains horizontally scrollable, the popover fits within 12px viewport margins, its body scrolls for long content, and no control or text overlaps.

- [ ] **Step 4: Record clean repository evidence**

Run `git diff --check`, show the exact changed files, and verify unrelated `.superpowers/` content and the existing untracked plan are absent from all commits.
