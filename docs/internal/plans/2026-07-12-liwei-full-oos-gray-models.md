# Liwei Full-OOS Gray Models Implementation Plan

> **文档状态：HISTORICAL。** 本文是内部实施记录，不是当前操作 SOP；当前入口见 [文档中心](../../README.md)。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add independent 10Y01 and 5Y01 continuous full-OOS models to Bond Factor Lab, run them in the weekday 07:03 business schedule, and expose their historical and gray/live metrics in the existing Factor Lab UI without changing the old models.

**Architecture:** Each new scheme gets a small platform adapter that reuses the existing source core but owns its scheme identity, full-OOS window, model version, config, and audit metadata. Scheme-specific reproduction runners follow the validated 10Y02 target-month full-OOS batching pattern. Existing registry, backtest, metrics, scheduler, and frontend merge paths remain unchanged.

**Tech Stack:** Python 3.12/3.13, pandas, SQLAlchemy, unittest/pytest, YAML scheme discovery, APScheduler, MySQL-backed harness and Factor Lab API.

---

### Task 1: Define New Scheme Contracts

**Files:**
- Create: `schemes/liwei_0616_10y01_full_oos_k3_div_k10/__init__.py`
- Create: `schemes/liwei_0616_10y01_full_oos_k3_div_k10/config.yaml`
- Create: `schemes/liwei_0616_5y01_full_oos_k3_div_k10/__init__.py`
- Create: `schemes/liwei_0616_5y01_full_oos_k3_div_k10/config.yaml`
- Create: `tests/test_liwei_0616_full_oos_gray_models.py`

- [ ] **Step 1: Write failing config tests**

Assert both new scheme IDs, names, T+5 tenor, `status: paused` before activation, `cron: "3 7 * * 1-5"`, `timezone: Asia/Shanghai`, `timeout_sec: 3600`, benchmark runner, benchmark ID, required input columns, and absence of a pinned `--input-end` argument.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m unittest tests.test_liwei_0616_full_oos_gray_models.FullOosConfigTests -v`

Expected: FAIL because the new scheme configs do not exist.

- [ ] **Step 3: Add minimal configs**

Use these identities:

```yaml
scheme_id: liwei_0616_10y01_full_oos_k3_div_k10
name: "liwei_0616 10Y_01 原脚本Full-OOS"
schedule:
  cron: "3 7 * * 1-5"
  timezone: "Asia/Shanghai"
  timeout_sec: 3600
status: paused
```

```yaml
scheme_id: liwei_0616_5y01_full_oos_k3_div_k10
name: "liwei_0616 5Y_01 原脚本Full-OOS"
schedule:
  cron: "3 7 * * 1-5"
  timezone: "Asia/Shanghai"
  timeout_sec: 3600
status: paused
```

- [ ] **Step 4: Run focused config tests and strict discovery**

Run: `python -m unittest tests.test_liwei_0616_full_oos_gray_models.FullOosConfigTests -v`

Run: `python -c "from scheduler.discovery import discover_schemes; print([x.scheme_id for x in discover_schemes(strict=True) if 'full_oos' in x.scheme_id])"`

Expected: PASS and exactly the two new scheme IDs.

### Task 2: Implement Full-OOS Inference and Live Date Conversion

**Files:**
- Create: `schemes/liwei_0616_10y01_full_oos_k3_div_k10/inference.py`
- Create: `schemes/liwei_0616_10y01_full_oos_k3_div_k10/predict.py`
- Create: `schemes/liwei_0616_5y01_full_oos_k3_div_k10/inference.py`
- Create: `schemes/liwei_0616_5y01_full_oos_k3_div_k10/predict.py`
- Modify: `tests/test_liwei_0616_full_oos_gray_models.py`

- [ ] **Step 1: Write failing window and adapter tests**

Cover these behaviors:

```python
window = liwei_0616_full_oos_window("2026-07-10")
assert window.test_ranges == (("2024-01-01", "2026-07-10"),)
assert window.current_start == "2026-07-10"
assert window.current_end == "2026-07-10"
```

Mock the 7Y-style calendar/artifact boundary and assert live records preserve:

```text
predict_date=2026-07-13
feature_date=2026-07-10
target_date=nth_trading_day_after(2026-07-10, 5)
```

Also assert the output scheme ID, independent model version, `model_scope=source_original_full_oos`, and one-element `model_test_ranges`.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_liwei_0616_full_oos_gray_models.FullOosWindowTests tests.test_liwei_0616_full_oos_gray_models.FullOosLiveAdapterTests -v`

Expected: FAIL because inference and predict modules do not exist.

- [ ] **Step 3: Implement the minimal adapters**

Reuse the existing source cores via absolute imports:

```python
from schemes.liwei_0616_10y01_cons_say_k3_div_k10.core.v31_common import ...
from schemes.liwei_0616_cons_sda_k3_div_k10.core.v31_common import ...
```

Both window builders must validate dates and return only:

```python
test_ranges=((SOURCE_OOS_START, source_end_str),)
```

The 10Y adapter uses cache family `liwei_0616_10y_v61`; the 5Y adapter uses `liwei_0616_5y_v31`. Preserve source baseline configuration and use independent top-level model versions:

```text
liwei_0616_10y01_full_oos_v1
liwei_0616_5y01_full_oos_v1
```

- [ ] **Step 4: Run focused inference/live tests**

Run: `python -m unittest tests.test_liwei_0616_full_oos_gray_models.FullOosWindowTests tests.test_liwei_0616_full_oos_gray_models.FullOosLiveAdapterTests -v`

Expected: PASS.

### Task 3: Add Target-Month Full-OOS Backtest Runners

**Files:**
- Create: `backtests/liwei_0616_10y01_full_oos_k3_div_k10_reproduction.py`
- Create: `backtests/liwei_0616_5y01_full_oos_k3_div_k10_reproduction.py`
- Modify: `tests/test_liwei_0616_full_oos_gray_models.py`

- [ ] **Step 1: Write failing backtest grouping tests**

Assert that feature dates are grouped by `target_date[:7]`, each group has `source_end=max(target_date)`, and every source call receives one test range beginning at `2024-01-01`. Assert historical rows keep `feature_date` and use `target_date` for the live cutoff.

- [ ] **Step 2: Run the backtest tests and verify RED**

Run: `python -m unittest tests.test_liwei_0616_full_oos_gray_models.FullOosBacktestTests -v`

Expected: FAIL because both runners are absent.

- [ ] **Step 3: Implement runner adapters**

Follow `backtests/liwei_0616_10y02_cons_say_k3_div_k5_reproduction.py` for target-month grouping, cache keys, artifact provenance, persistence validation, and CLI. Adapt the 5Y source return shape without changing its core. Use independent constants:

```text
BENCHMARK_ID=liwei_0616_10y01_full_oos
BENCHMARK_ID=liwei_0616_5y01_full_oos
DATA_SOURCE=framework_db_aligned
LIVE_TARGET_CUTOFF=2026-06-01
```

- [ ] **Step 4: Run focused backtest tests**

Run: `python -m unittest tests.test_liwei_0616_full_oos_gray_models.FullOosBacktestTests -v`

Expected: PASS.

### Task 4: Build Benchmark Evidence

**Files:**
- Create: `schemes/liwei_0616_10y01_full_oos_k3_div_k10/benchmarks/README.md`
- Create: `schemes/liwei_0616_10y01_full_oos_k3_div_k10/benchmarks/original_predictions_sample.csv`
- Create: `schemes/liwei_0616_10y01_full_oos_k3_div_k10/benchmarks/current_predictions_sample.csv`
- Create: `schemes/liwei_0616_10y01_full_oos_k3_div_k10/benchmarks/original_backtest_summary.json`
- Create: `schemes/liwei_0616_10y01_full_oos_k3_div_k10/benchmarks/current_backtest_summary.json`
- Create: `schemes/liwei_0616_5y01_full_oos_k3_div_k10/benchmarks/README.md`
- Create: `schemes/liwei_0616_5y01_full_oos_k3_div_k10/benchmarks/original_predictions_sample.csv`
- Create: `schemes/liwei_0616_5y01_full_oos_k3_div_k10/benchmarks/current_predictions_sample.csv`
- Create: `schemes/liwei_0616_5y01_full_oos_k3_div_k10/benchmarks/original_backtest_summary.json`
- Create: `schemes/liwei_0616_5y01_full_oos_k3_div_k10/benchmarks/current_backtest_summary.json`

- [ ] **Step 1: Generate source-original sample evidence**

Use the already completed continuous full-OOS contexts through `2026-07-10` and the aligned target-date mapping. Select a compact sample that covers multiple target months and includes non-zero and zero predictions.

- [ ] **Step 2: Run the new runners in no-persist sample mode**

Run each runner with the same feature dates, `--batch-mode monthly`, Phase A cache enabled, and no persistence.

- [ ] **Step 3: Compare direction and internal fields**

Require zero mismatches for predicted direction, true label, vote score, and every retained baseline score. Write the current sample and summary only after this comparison passes.

- [ ] **Step 4: Run benchmark contract tests**

Run: `python -m unittest tests.test_liwei_0616_full_oos_gray_models.FullOosBenchmarkTests -v`

Expected: PASS with `benchmark_scope=source_original_full_oos_targeted_sample` and zero internal mismatches.

### Task 5: Persist Historical Backtests and Activate Registry Rows

**Files:**
- Modify: `schemes/liwei_0616_10y01_full_oos_k3_div_k10/config.yaml`
- Modify: `schemes/liwei_0616_5y01_full_oos_k3_div_k10/config.yaml`
- Update: `docs/CURRENT_STATUS.md`

- [ ] **Step 1: Run full no-persist gates**

Run static, unit, dry-run, compare, backtest no-persist, and API readiness gates for each paused scheme. Confirm protected table deltas are zero.

- [ ] **Step 2: Persist authorized `framework_db_aligned` backtests**

Issue one-time backtest-write authorizations for the two exact scheme versions, execute BacktestGate persistence, and record run IDs and row counts.

- [ ] **Step 3: Activate both versions**

Issue one-time activation authorizations after required gate history is green. Activation must update config status, scheme version state, and one composite registry row per scheme.

- [ ] **Step 4: Sync and verify API visibility**

Verify `/api/backtests/factor-lab` includes both base scheme IDs and `/api/schemes` includes:

```text
liwei_0616_10y01_full_oos_k3_div_k10__h5__10Y
liwei_0616_5y01_full_oos_k3_div_k10__h5__5Y
```

### Task 6: Add Gray Evidence and Mount 07:03 Scheduling

**Files:**
- Update: `docs/CURRENT_STATUS.md`

- [ ] **Step 1: Run one no-write live validation per scheme**

Use an authorized trading date and explicit `prediction_phase=gray_live`; verify platform dates match the 7Y daily convention.

- [ ] **Step 2: Persist one authorized gray row per scheme**

Use LiveGate with one-time `live_write` authorization. Confirm each operation changes only one run, one prediction, and one run-log row.

- [ ] **Step 3: Restart scheduler**

Run: `launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.scheduler`

Verify both configs remain at business cron `3 7 * * 1-5`, effective physical times are staggered, timeout is 3600, and global prediction concurrency remains 1.

- [ ] **Step 4: Verify Factor Lab UI/API**

Check both metrics endpoints expose the gray row and phase range. Open `http://127.0.0.1:8100`, verify both names appear in the corresponding 10Y T+5 and 5Y T+5 candidate lists, and inspect detail metadata for historical plus gray/live sections.

### Task 7: Regression and Final Verification

**Files:**
- Modify only files already listed if a verified failure requires a correction.

- [ ] **Step 1: Run focused and shared regression tests**

Run the new test module, config schema, discovery, repository registry, backend Factor Lab, frontend Factor Lab, Phase A cache, and existing 10Y01/10Y02/5Y01/7Y tests.

- [ ] **Step 2: Run static validation**

Run `python -m compileall` for the new scheme and runner modules, `git diff --check`, and strict scheme discovery.

- [ ] **Step 3: Verify old schemes are unchanged in DB/API**

Confirm existing old scheme composite IDs remain active and their historical/live row counts were not replaced.

- [ ] **Step 4: Record final evidence**

Document new scheme versions, historical run IDs, gray run IDs, effective scheduler times, observed durations, API URLs, and remaining future target labels in `docs/CURRENT_STATUS.md`.
