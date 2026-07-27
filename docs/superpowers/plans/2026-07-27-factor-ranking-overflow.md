# Factor Ranking Overflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep long factor scheme names and remarks within their ranking-table columns.

**Architecture:** Preserve the existing table and horizontal-scroll container, but add explicit column contracts and two-line clamped content elements. The renderer supplies escaped full-text tooltips without changing data semantics.

**Tech Stack:** Native HTML/CSS/JavaScript, Python frontend contract tests, in-app browser visual verification.

---

### Task 1: Reproduce and contract the overflow

**Files:**
- Modify: `tests/test_frontend_factor_lab.py`

- [ ] **Step 1: Add a failing render contract**

Render a scheme with:

```javascript
name: "MACRO_DIFFUSION_FUNDSEASON_TRENDKERNEL",
description: "以日周月三频宏观指标构建的超长客户展示备注"
```

Assert the ranking row contains `.factor-scheme-name` and
`.factor-remark-text`, both with escaped full `title` values.

- [ ] **Step 2: Add a failing CSS contract**

Assert the stylesheet contains explicit scheme/accuracy/remark column widths,
`overflow-wrap: anywhere`, two-line clamping, and hidden overflow.

- [ ] **Step 3: Run tests and verify RED**

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q \
  tests/test_frontend_factor_lab.py -k "ranking and overflow"
```

Expected: missing content classes and clamp rules.

### Task 2: Implement bounded ranking cells

**Files:**
- Modify: `frontend/aifin-shell.js`
- Modify: `frontend/aifin-shell.css`
- Test: `tests/test_frontend_factor_lab.py`

- [ ] **Step 1: Render bounded elements**

Use the existing `escapeHtml()` for both visible text and `title`:

```javascript
'<strong class="factor-scheme-name" title="' + escapeHtml(scheme.name) + '">' +
  escapeHtml(scheme.name) +
'</strong>'
```

Apply the same structure to the remark text.

- [ ] **Step 2: Add minimal column and clamp CSS**

```css
.factor-ranking-table th:nth-child(2),
.factor-ranking-table td:nth-child(2) { width: 260px; }
.factor-ranking-table th:nth-child(3),
.factor-ranking-table td:nth-child(3) { width: 180px; }
.factor-ranking-table th:nth-child(8),
.factor-ranking-table td:nth-child(8) { width: 300px; }

.factor-scheme-name,
.factor-remark-text {
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.factor-scheme-name { overflow-wrap: anywhere; }
```

- [ ] **Step 3: Verify GREEN and frontend regressions**

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q \
  tests/test_frontend_factor_lab.py \
  tests/test_factor_lab_dashboard.py \
  tests/test_factor_lab_dashboard_api.py
```

Expected: zero failures.

- [ ] **Step 4: Verify visually**

At the customer viewport, select `1Y国债活跃 · 月度` and verify:

- the long FengRL scheme name remains inside the scheme column;
- the accuracy value and bar remain unobstructed;
- the remark is limited to two lines;
- the table can still scroll horizontally;
- no browser console error appears.

- [ ] **Step 5: Commit**

```bash
git add frontend/aifin-shell.js frontend/aifin-shell.css tests/test_frontend_factor_lab.py
git commit -m "fix: contain factor ranking text overflow"
```
