# Blackbox V2 Full-Range Batched Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist every eligible Blackbox V2 historical case from a default `2025-01-01` start date through an exclusive live cutoff, while keeping each delivery invocation at no more than 100 Requests and preserving same-month backtest rows in the frontend.

**Architecture:** Resolve the persisted date range in the Harness, bind it to the HMAC authorization, build all HistoricalCases, and pass one request sequence through the existing runner's bounded batch loop. Convert all records into one RunOutput and persist that output in one transaction; preserve prior immutable runs and let the existing canonical latest-success selection expose the new run. Merge frontend daily rows by source instead of replacing a whole month.

**Tech Stack:** Python 3.12, argparse, HMAC authorization, SQLAlchemy, MySQL 8.0, unittest, native JavaScript, Blackbox V2 Harness and Runtime Profile.

---

### Task 1: Bind the persisted backtest range in CLI and authorization

**Files:**
- Modify: `tests/test_authorization.py`
- Modify: `tests/test_blackbox_v2_harness_dispatch.py`
- Modify: `harness/authorization.py`
- Modify: `harness/context.py`
- Modify: `harness/cli.py`

- [ ] **Step 1: Write failing authorization tests**

Add tests proving that only `backtest_persist` tokens carry an exact normalized range start and that verification rejects a mismatch:

```python
def test_backtest_persist_token_binds_start_date(self) -> None:
    token = issue_token(
        "trial", "backtest_persist", predict_date="2026-07-20",
        backtest_start_date="2025-02-03",
    )
    auth = parse_token(token)
    self.assertEqual(auth.backtest_start_date, "2025-02-03")
    _, errors = verify_authorization(
        token,
        scheme_id="trial",
        action="backtest_persist",
        predict_date="2026-07-20",
        backtest_start_date="2025-01-01",
        used_store_path=self._used_path(),
    )
    self.assertIn("backtest_start_date mismatch", "\n".join(errors))

def test_non_backtest_token_schema_is_unchanged(self) -> None:
    envelope = self._decode_token(issue_token("trial", "blackbox_activate"))
    self.assertNotIn("backtest_start_date", envelope["payload"])
```

- [ ] **Step 2: Write failing parser tests**

Use `_build_parser()` to prove `gate backtest --persist` and `auth issue --action backtest_persist` both default to `2025-01-01`, an explicit `--backtest-start-date` is retained, and the gate parser leaves `sample_size=None` when the user did not supply it.

- [ ] **Step 3: Run the focused tests and verify red**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
    tests.test_authorization \
    tests.test_blackbox_v2_harness_dispatch -v
```

Expected: failures report the missing `backtest_start_date` argument/field and the old parser defaults.

- [ ] **Step 4: Implement the action-specific token field**

Add `DEFAULT_BACKTEST_START_DATE = "2025-01-01"`, add `Authorization.backtest_start_date`, and make the token schema conditional:

```python
payload = {**base_payload}
if action == "backtest_persist":
    payload["backtest_start_date"] = normalize_backtest_start_date(backtest_start_date)
```

`verify_authorization(..., backtest_start_date=...)` must compare the signed value when the expected action is `backtest_persist`; other actions retain the existing exact payload schema.

- [ ] **Step 5: Implement CLI and GateContext semantics**

Add:

```python
GateContext.backtest_start_date: str = DEFAULT_BACKTEST_START_DATE
GateContext.backtest_sample_size: int | None = None
```

The backtest gate parser gets `--backtest-start-date` with the default constant and `--sample-size` with `default=None`. The auth issue parser gets the same range option and passes it into `issue_token` only through the common function signature.

- [ ] **Step 6: Run authorization and CLI tests green**

Run the Step 3 command. Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add harness/authorization.py harness/context.py harness/cli.py \
  tests/test_authorization.py tests/test_blackbox_v2_harness_dispatch.py
git commit -m "feat: bind blackbox backtest ranges"
```

### Task 2: Build the full historical interval and carry one total batch budget

**Files:**
- Modify: `tests/test_blackbox_v2_history.py`
- Modify: `tests/test_blackbox_v2_backtest_persistence.py`
- Modify: `shared/blackbox_v2/history.py`
- Modify: `backtests/blackbox_v2.py`

- [ ] **Step 1: Write a failing unlimited-history test**

Add a test calling:

```python
cases = build_historical_cases(
    _metadata("T+5"), snapshot, engine,
    limit=None,
    target_date_before="2026-03-01",
    predict_date_from="2025-01-01",
)
self.assertGreater(len(cases), 100)
self.assertGreaterEqual(cases[0].request.predict_date, "2025-01-01")
self.assertTrue(all(case.request.target_date < "2026-03-01" for case in cases))
```

Also assert an empty unlimited interval fails with an explicit “at least one” error.

- [ ] **Step 2: Run the history tests and verify red**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest tests.test_blackbox_v2_history -v
```

Expected: `limit=None` is rejected by the current positive-integer validation.

- [ ] **Step 3: Implement optional history limit**

Change the signature to `limit: int | None`. For `None`, select the complete sorted eligible list; for an integer, preserve the current exact-count/latest-N behavior. Validate the selected result with `expected_count=len(selected)` and fail if the complete interval is empty.

- [ ] **Step 4: Write a failing budget propagation test**

In the converter test, pass 205 cases and a sentinel budget to `run_blackbox_historical_backtest`, capture `kwargs["budget"]` in `run_delivery`, and assert the same object is received. Assert summary fields describe `batch_count=3`, `batch_sizes=[100, 100, 5]`, and `max_batch_requests=100`.

- [ ] **Step 5: Run the converter test and verify red**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest tests.test_blackbox_v2_backtest_persistence.BlackboxV2BacktestConversionTests -v
```

Expected: the converter does not accept/forward `budget` and lacks batch evidence.

- [ ] **Step 6: Forward budget without introducing a scheduler import**

Add an untyped/`Any` `budget` keyword to `run_blackbox_historical_backtest`, forward it to `run_delivery`, and calculate batch evidence from `len(cases)` plus `profile.max_batch_requests`. Keep the AST test that forbids `backtests.blackbox_v2 -> scheduler` dependencies green.

- [ ] **Step 7: Run history, converter and runner tests green**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
    tests.test_blackbox_v2_history \
    tests.test_blackbox_v2_backtest_persistence \
    tests.test_blackbox_v2_runner -v
```

- [ ] **Step 8: Commit**

```bash
git add shared/blackbox_v2/history.py backtests/blackbox_v2.py \
  tests/test_blackbox_v2_history.py tests/test_blackbox_v2_backtest_persistence.py
git commit -m "feat: build full blackbox history"
```

### Task 3: Upgrade the persisted Backtest Gate to dynamic full-range writes

**Files:**
- Modify: `tests/test_blackbox_v2_harness_gates.py`
- Modify: `tests/test_blackbox_v2_persisted_gate_provenance.py`
- Modify: `harness/blackbox_v2/gates.py`

- [ ] **Step 1: Write failing persisted Gate tests**

Cover four behaviors:

```python
# 1. Explicit sample size is invalid only in persist mode.
ctx = replace(base_ctx, persist_backtest=True, backtest_sample_size=100)
self.assertIn("--sample-size", "\n".join(BlackboxBacktestGate().run(ctx).errors))

# 2. The signed start date must match GateContext.
ctx = replace(base_ctx, backtest_start_date="2025-02-01")
self.assertIn("backtest_start_date mismatch", "\n".join(result.errors))

# 3. 205 cases produce dynamic deltas.
cases = _cases(205)
output = _output(205)
self.assertEqual(evidence["records"], 205)

# 4. The total budget has ceil(205 / 100) == 3 subprocesses.
self.assertEqual(captured_budget.max_subprocesses, 3)
```

Adjust the provenance helper's mocked counts to use `len(cases)` rather than fixed 100 and assert `build_historical_cases` receives `limit=None` and the context start date.

- [ ] **Step 2: Run the focused Gate tests and verify red**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
    tests.test_blackbox_v2_harness_gates \
    tests.test_blackbox_v2_persisted_gate_provenance -v
```

Expected: the current Gate blocks non-100 totals, requests `limit=100`, does not bind the start date, and expects a fixed `+100` delta.

- [ ] **Step 3: Implement the full-range Gate**

For persist mode:

```python
if ctx.backtest_sample_size is not None:
    return _blocked(..., ["--sample-size is no-persist only"])

cases = build_historical_cases(
    metadata, state.snapshot, engine,
    limit=None,
    target_date_before=ctx.predict_date,
    predict_date_from=ctx.backtest_start_date,
)
profile = _profile(ctx)
budget = BacktestExecutionBudget(
    deadline_monotonic=time.monotonic() + ctx.timeout_sec,
    max_subprocesses=math.ceil(len(cases) / profile.max_batch_requests),
)
```

Pass `budget` into the converter. Replace all fixed 100 validation/evidence/delta expectations with `len(cases)` and `len(output.rows)`, and include range/batch/budget evidence.

For no-persist mode, resolve `sample_size = 100` when the context value is `None`, preserving the existing 1..1000 contract.

- [ ] **Step 4: Run all Blackbox persisted Gate tests green**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
    tests.test_blackbox_v2_harness_gates \
    tests.test_blackbox_v2_persisted_gate_provenance \
    tests.test_blackbox_v2_backtest_persistence \
    tests.test_authorization -v
```

- [ ] **Step 5: Commit**

```bash
git add harness/blackbox_v2/gates.py \
  tests/test_blackbox_v2_harness_gates.py \
  tests/test_blackbox_v2_persisted_gate_provenance.py
git commit -m "feat: persist full-range blackbox backtests"
```

### Task 4: Preserve same-month backtest details in the frontend

**Files:**
- Modify: `tests/test_frontend_factor_lab.py`
- Modify: `frontend/aifin-shell.js`

- [ ] **Step 1: Extend the existing same-month test to expose the 87-row bug**

Return and assert source-specific daily counts:

```javascript
var daily = scheme.dailyRowsByMonth["2026-05"] || [];
return {
  backtestDaily: daily.filter(function (r) { return r._source === "backtest"; }).length,
  liveDaily: daily.filter(function (r) { return r._source === "live"; }).length
};
```

Expected assertions are `backtestDaily == 3` and `liveDaily == 1`.

- [ ] **Step 2: Run the exact test and verify red**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
    tests.test_frontend_factor_lab.FactorLabRankingTests.test_same_month_backtest_and_live_split_into_two_rows -v
```

Expected: `backtestDaily` is zero because the live month replaced the backtest array.

- [ ] **Step 3: Append source-tagged live rows**

Replace the monthly assignment with concatenation:

```javascript
var existingRows = mScheme.dailyRowsByMonth[m] || [];
var liveRows = (liveScheme.dailyRowsByMonth[m] || []).map(function (dr) {
  var d = {}; Object.keys(dr).forEach(function (k) { d[k] = dr[k]; });
  d._source = "live";
  return d;
});
mScheme.dailyRowsByMonth[m] = existingRows.concat(liveRows);
```

- [ ] **Step 4: Run all frontend factor-lab tests green**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest tests.test_frontend_factor_lab -v
```

- [ ] **Step 5: Commit**

```bash
git add frontend/aifin-shell.js tests/test_frontend_factor_lab.py
git commit -m "fix: preserve same-month backtest details"
```

### Task 5: Make both SOPs explicit about full-range persistence

**Files:**
- Modify: `tests/test_onboarding_docs.py`
- Modify: `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`
- Modify: `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`

- [ ] **Step 1: Write a failing documentation contract test**

Require both documents to contain the operative boundary:

```python
self.assertIn("单批上限不是完整回测总量上限", upstream)
self.assertIn("--backtest-start-date", platform)
self.assertIn("默认 `2025-01-01`", platform)
self.assertIn("全部批次成功后", platform)
self.assertIn("单一事务", platform)
self.assertIn("current snapshot as-of replay", platform)
```

- [ ] **Step 2: Run the exact docs test and verify red**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
    tests.test_onboarding_docs.OnboardingDocumentationTests.test_blackbox_full_range_backtest_contract -v
```

- [ ] **Step 3: Update the upstream SOP**

State that a delivery handles only the current batch, the 100-row ceiling is not the complete-history ceiling, the platform may invoke multiple batches under one version/snapshot/generation, and partition/order invariance is mandatory.

- [ ] **Step 4: Update the platform SOP**

Add the signed auth and persist commands with `--backtest-start-date`, define the default and exclusive cutoff, document atomic all-or-nothing persistence, dynamic row-count acceptance, immutable prior runs, latest-success selection, and current-snapshot replay limitations.

- [ ] **Step 5: Run docs and relevant platform regression**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
    tests.test_onboarding_docs \
    tests.test_blackbox_v2_history \
    tests.test_blackbox_v2_runner \
    tests.test_blackbox_v2_backtest_persistence \
    tests.test_blackbox_v2_harness_gates \
    tests.test_blackbox_v2_persisted_gate_provenance \
    tests.test_backend_api \
    tests.test_frontend_factor_lab -v
```

- [ ] **Step 6: Commit**

```bash
git add docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md \
  docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md tests/test_onboarding_docs.py
git commit -m "docs: define full-range blackbox persistence"
```

### Task 6: Verify code and refresh the four production backtests

**Files:**
- Modify: `docs/blackbox_v2/records/PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md`
- Modify: `docs/blackbox_v2/records/PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.evidence.json`
- Modify: `docs/blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md`
- Modify: `docs/CURRENT_STATUS.md`
- Modify: `docs/blackbox_v2/PRODUCTION_READINESS.md`

- [ ] **Step 1: Run fresh complete verification**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest discover -s tests -p 'test_*.py'
```

Expected: all tests pass with zero failures.

- [ ] **Step 2: Verify production prerequisites read-only**

Confirm branch/status, four exact active configs and Registry rows, HMAC availability without printing the secret, DataBridge refresh/generation/fingerprint, current backtest counts, and unchanged delivery file hashes. Stop before mutation if any prerequisite differs from the approved design.

- [ ] **Step 3: Run fresh all-stage certification for each scheme**

For each of the four base IDs, execute:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness onboard <scheme_id> \
    --predict-date 2026-07-20 \
    --stage all \
    --algo-env forecast_env_blackbox_v1 \
    --timeout-sec 1800
```

Require all seven Gates to pass and capture the exact new `harness_run_id`.

- [ ] **Step 4: Issue and consume one range-bound token per scheme**

For each exact version/run pair:

```bash
TOKEN=$(conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness auth issue \
    --scheme-id <scheme_id> \
    --action backtest_persist \
    --predict-date 2026-07-20 \
    --backtest-start-date 2025-01-01 \
    --scheme-version <scheme_version> \
    --harness-run-id <harness_run_id> \
    --expires-in 900 \
    --issued-by codex-production-gray)

conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness gate backtest \
    --scheme-id <scheme_id> \
    --predict-date 2026-07-20 \
    --persist \
    --backtest-start-date 2025-01-01 \
    --timeout-sec 1800 \
    --algo-env forecast_env_blackbox_v1 \
    --authorize "$TOKEN"
```

- [ ] **Step 5: Verify database and API boundaries**

For each scheme require one new success run, a dynamic prediction count greater than 100, non-empty monthly metrics, earliest eligible predict date at/after `2025-01-01`, and every `target_date < 2026-07-20`. Verify API selects the new run, old 100-row runs remain queryable, live row counts are unchanged, and all four Registry rows remain active.

- [ ] **Step 6: Restart only the backend and verify the frontend**

Restart the backend through the existing launchd service without editing or staging the untracked plist. Do not restart scheduler. Verify the `1Y国债活跃 × T+5` grid contains four concise names, complete backtest samples are retained when July live rows exist, and the browser console has zero errors. Save a screenshot in the existing ignored production report directory.

- [ ] **Step 7: Update durable evidence and status documents**

Append the new run IDs, benchmark IDs, actual counts, dates, batch sizes, generation/snapshot/version/run bindings, API checks and frontend result. State explicitly that old 100-row runs remain immutable history and current live rows were not rewritten.

- [ ] **Step 8: Run final verification and commit evidence**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest discover -s tests -p 'test_*.py'
git diff --check
```

Then stage only the intended tracked documentation/evidence files and commit:

```bash
git commit -m "docs: record full-range 1y t5 backtests"
```

Do not merge or push `master`.
