# Weekly Live Gap Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the read-only signal-gap report use Saturday, the actual launchd weekly execution date, so a holiday Friday does not create a false weekly signal gap.

**Architecture:** `shared.signal_gap_report._SnapshotCalendar.predict_dates()` remains the single cadence-to-date adapter. Only its weekly branch changes: daily and monthly remain unchanged, while weekly dates are generated as calendar Saturdays in the requested interval; `build_weekly_live_context()` still derives feature and target dates from the shared DB calendar.

**Tech Stack:** Python 3.12, SQLAlchemy read-only report, unittest/pytest.

---

### Task 1: Lock the Saturday live-cadence contract

**Files:**
- Modify: `tests/test_signal_gap_report.py`

- [x] **Step 1: Write the failing holiday-week test**

```python
def test_weekly_live_gap_report_uses_saturday_when_friday_is_holiday(self) -> None:
    self._calendar("2026-06-12", "2026-06-26")
    with self.engine.begin() as connection:
        connection.execute(
            text("UPDATE t_trade_calendar SET trade_flag = '0' WHERE rdate = '2026-06-19'")
        )
    self._registry(
        "weekly__h6__10Y", "weekly", "native_adapter", "weekly",
        "weekly_point", "10Y", 6, "2026-06-11",
    )
    self._prediction("weekly", "current", "10Y", 6, "2026-06-13", "2026-06-12", "2026-06-18", "scheduled_live")
    self._prediction("weekly", "current", "10Y", 6, "2026-06-20", "2026-06-18", "2026-06-26", "scheduled_live")

    report = self._report("2026-06-12", "2026-06-20")

    self.assertEqual([item.predict_date for item in report.expected], ["2026-06-13", "2026-06-20"])
    self.assertEqual(report.missing, ())
```

- [x] **Step 2: Verify it fails under the Friday-derived implementation**

Run:

```bash
python -m pytest -q tests/test_signal_gap_report.py -k saturday_when_friday_is_holiday
```

Expected: the report expects `2026-06-19` and reports it missing.

### Task 2: Generate only actual weekly launchd dates

**Files:**
- Modify: `shared/signal_gap_report.py:557-573`
- Test: `tests/test_signal_gap_report.py`

- [x] **Step 1: Replace the weekly date source with Saturdays**

```python
def _weekly_live_dates(start_date: str, end_date: str) -> tuple[str, ...]:
    current = date.fromisoformat(start_date)
    current += timedelta(days=(5 - current.weekday()) % 7)
    result: list[str] = []
    while current.isoformat() <= end_date:
        result.append(current.isoformat())
        current += timedelta(days=7)
    return tuple(result)
```

Use `_weekly_live_dates(start_date, end_date)` in the `frequency == "weekly"` branch of `_SnapshotCalendar.predict_dates()`. Keep `build_weekly_live_context()` unchanged.

- [x] **Step 2: Verify focused report tests**

Run:

```bash
python -m pytest -q tests/test_signal_gap_report.py
```

Expected: all tests pass, including the holiday-week regression.

### Task 3: Verify production-facing semantics and publish

**Files:**
- Modify: `shared/signal_gap_report.py`
- Modify: `tests/test_signal_gap_report.py`

- [x] **Step 1: Run dashboard/report focused regression and full suite**

```bash
python -m pytest -q tests/test_signal_gap_report.py tests/test_factor_lab_dashboard.py tests/test_factor_lab_dashboard_api.py tests/test_dashboard_direct_db.py tests/test_cgb_causal_wk_1y_v128_delivery.py
```

Expected: all pass; CGB cutoff identity remains untouched.

- [ ] **Step 2: Commit the code and plan**

```bash
git add docs/superpowers/plans/2026-08-07-weekly-live-gap-report.md shared/signal_gap_report.py tests/test_signal_gap_report.py
git commit -m "fix(report): use Saturday for weekly live gaps"
```

- [ ] **Step 3: Merge, push, deploy, and rerun the read-only report**

After explicit production-release authorization already granted for this closure, fast-forward `master`, push the focused branch and `master`, deploy the new release, then run:

```bash
python scripts/report_signal_gaps.py --as-of 2026-08-07
```

Expected: no false `2026-06-19` weekly gap; only the 21 known daily T+5 gaps remain before controlled gap fill.

### Task 4: Keep direct-read production acceptance executable

**Files:**
- Modify: `scripts/check_public_access.sh`
- Modify: `scripts/benchmark_factor_lab_dashboard.py`
- Modify: `shared/signal_gap_report.py`
- Test: `tests/test_public_access_config.py`
- Test: `tests/test_benchmark_factor_lab_dashboard.py`
- Test: `tests/test_signal_gap_report.py`

- [x] Replace the obsolete hard-coded page resource version with a strict extractor
  for the CSS/JS URL actually advertised by `index.html`.
- [x] Update the public benchmark's exact V1 scheme fields and validate the new
  signal-state pair.
- [x] Replace repeated full-calendar scans in the direct-read signal status path
  with equivalent indexed lookups; no cache or secondary data source is added.
- [ ] Rerun focused and full regression, then validate the deployed public route.
