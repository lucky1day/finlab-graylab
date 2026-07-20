# Hide Frontend Scheme Version Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide scheme-version fingerprints from every visible frontend surface while preserving the version field in API data and database audit records.

**Architecture:** Remove the only visible version renderer from the factor-lab ranking row and delete its now-unused helper and CSS selector. Keep the normalized `schemeVersion` value in frontend state and leave backend contracts untouched, with a Node VM rendering regression proving that a supplied version fingerprint never enters generated HTML.

**Tech Stack:** Vanilla JavaScript, CSS, Python `unittest`, Node `vm`

---

## File map

- `tests/test_frontend_factor_lab.py`: add a real rendering regression through the existing Node VM hook.
- `frontend/aifin-shell.js`: stop deriving and appending a version badge to ranking rows.
- `frontend/aifin-shell.css`: remove the unused version-badge selector while preserving sample-badge styling.

### Task 1: Remove the visible version badge with TDD

**Files:**
- Modify: `tests/test_frontend_factor_lab.py`
- Modify: `frontend/aifin-shell.js:1273`
- Modify: `frontend/aifin-shell.js:1324`
- Modify: `frontend/aifin-shell.css:832`

- [ ] **Step 1: Write the failing rendering test**

Add this test to `FactorLabRankingTests` in `tests/test_frontend_factor_lab.py`:

```python
def test_scheme_ranking_hides_scheme_version_fingerprint(self) -> None:
    result = _run_factor_lab_hook(
        """
        const metric = {
          overall: 80,
          correct: 8,
          samples: 10,
          metricSamples: 10,
          upPrecision: 75,
          downPrecision: 70
        };
        const rowHtml = hooks.renderSchemeRankingRowForTest(
          {
            id: "full-oos-10y",
            name: "liwei_0616 10Y_01 原脚本Full-OOS · 10Y国债活跃",
            deploymentDate: "2026/07/13",
            dailyRowsByMonth: {
              "2026-07": [{ schemeVersion: "35e461e60705" }]
            }
          },
          0,
          metric
        );
        return { rowHtml };
        """
    )

    self.assertIn("liwei_0616 10Y_01 原脚本Full-OOS", result["rowHtml"])
    self.assertNotIn("35e461e60705", result["rowHtml"])
    self.assertNotIn("factor-scheme-version", result["rowHtml"])
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest -v \
  tests.test_frontend_factor_lab.FactorLabRankingTests.test_scheme_ranking_hides_scheme_version_fingerprint
```

Expected: `FAIL`; the rendered HTML contains `35e461e60705` inside a `factor-scheme-version` span.

- [ ] **Step 3: Implement the minimal renderer change**

Delete the complete `latestSchemeVersion` function from `frontend/aifin-shell.js`:

```javascript
function latestSchemeVersion(scheme) {
  if (!scheme || !scheme.dailyRowsByMonth) return "";
  var latest = "";
  Object.keys(scheme.dailyRowsByMonth).forEach(function (month) {
    (scheme.dailyRowsByMonth[month] || []).forEach(function (row) {
      if (row.schemeVersion) latest = row.schemeVersion;
    });
  });
  return latest;
}
```

In `renderSchemeRankingRow`, remove:

```javascript
var version = latestSchemeVersion(scheme);
var versionHtml = version ? '<span class="factor-scheme-version">' + escapeHtml(version) + '</span>' : "";
```

Change the name cell from:

```javascript
'<td><strong>' + escapeHtml(scheme.name) + '</strong>' + versionHtml + '</td>' +
```

to:

```javascript
'<td><strong>' + escapeHtml(scheme.name) + '</strong></td>' +
```

In `frontend/aifin-shell.css`, change:

```css
.factor-scheme-version,
.factor-sample-badge {
```

to:

```css
.factor-sample-badge {
```

Do not remove `schemeVersion: row.scheme_version || ""` from frontend data normalization.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the same command from Step 2.

Expected: `OK`, one test passing.

### Task 2: Verify scope, audit retention, and regressions

**Files:**
- Verify: `frontend/aifin-shell.js`
- Verify: `frontend/aifin-shell.css`
- Test: `tests/test_frontend_factor_lab.py`

- [ ] **Step 1: Run the complete frontend factor-lab test module**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest -v tests.test_frontend_factor_lab
```

Expected: all tests pass with `OK` and no warning or error output.

- [ ] **Step 2: Verify no visible version renderer remains**

Run:

```bash
rg -n 'factor-scheme-version|latestSchemeVersion' frontend
```

Expected: no matches and exit code `1`.

Run:

```bash
rg -n 'schemeVersion: row\.scheme_version' frontend/aifin-shell.js
```

Expected: one match, proving the frontend still retains the audit field in normalized state.

- [ ] **Step 3: Verify the backend still exposes the audit version**

Run:

```bash
curl -sS \
  'http://127.0.0.1:8100/api/metrics/liwei_0616_10y01_full_oos_k3_div_k10__h5__10Y' \
  | jq -e '.daily_rows[] | select(.scheme_version == "35e461e60705")'
```

Expected: at least one matching prediction row and exit code `0`.

- [ ] **Step 4: Review the exact diff and whitespace**

Run:

```bash
git diff --check
git diff -- frontend/aifin-shell.js frontend/aifin-shell.css tests/test_frontend_factor_lab.py
```

Expected: no whitespace errors; diff contains only the test, renderer removal, and CSS selector cleanup.

- [ ] **Step 5: Commit the implementation**

Run:

```bash
git add frontend/aifin-shell.js frontend/aifin-shell.css tests/test_frontend_factor_lab.py
git commit -m "fix: hide scheme versions in frontend"
```

Expected: one implementation commit containing exactly the three listed files.
