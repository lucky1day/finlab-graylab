# Native Gap Cache Prewarm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permit one authorized Native publisher to build an artifact-bound Phase-A cache before an existing `hit_only` gray-gap fill.

**Architecture:** The harness rechecks the exact database `SEALED` fence, derives a private cache root from the registered current-snapshot artifact, and then issues a short-lived one-time permit only after HMAC verification. The exact 10Y publisher consumes that permit at the cache write point through a new explicit Native prewarm mode. The cache manifest retains the existing Native generation binding. The existing fill Gate resolves the same root and remains read-only, so the consumer cannot mutate or borrow realtime state.

**Trust boundary:** The permit is a capability handoff inside the existing trusted harness/executor/Native chain. It prevents ordinary scheduler/env-path bypasses and binds the supported operator flow; it is not a defense against arbitrary same-UID local code, because that code can already write the cache root. A hostile-local-code boundary would require a separately authorized privileged writer/broker or fixed public-key deployment.

**Tech Stack:** Python 3.12, existing harness HMAC authorization, Native input generations, Phase-A cache contract, unittest.

---

### Task 1: Define the explicit prewarm execution contract

**Files:**
- Modify: `shared/liwei_0616_cache_contract.py`
- Modify: `shared/liwei_0616_phase_a_cache.py`
- Modify: `scheduler/executor.py`
- Test: `tests/test_native_generation_executor.py`

- [x] **Step 1: Write failing executor tests**

Add tests which call `run_scheme_subprocess()` with a current-snapshot Native context and
`native_execution_mode="signal_gap_cache_prewarm"`. Assert that the child environment contains the exact
Native artifact fields, the explicit private cache root, and `BOND_LIWEI_0616_CACHE_MUTATION_POLICY=prewarm`.
Assert a normal `signal_gap_current_snapshot` call still gets `hit_only`.

- [x] **Step 2: Run the new tests and observe RED**

Run: `conda run --no-capture-output -n bond_factor_lab_service python -m unittest tests.test_native_generation_executor`

Expected: the prewarm-mode test fails because the mode and cache policy do not exist.

- [x] **Step 3: Add the minimal contract**

Add `CACHE_MUTATION_POLICY_PREWARM = "prewarm"`; accept it only in the cache module and only to allow a
signal-gap exporter binding during publisher construction. Add
`NATIVE_EXECUTION_MODE_SIGNAL_GAP_CACHE_PREWARM`; require the same later-capture/exact-feature checks as the
current-snapshot gap mode, exact 10Y publisher identity, an artifact-derived absolute `phase_a_cache_root`, and
a short-lived one-time permit. The cache write sink must verify and consume the permit before it creates a family
directory or publishes `current.json`. Do not alter scheduled execution.

- [x] **Step 4: Run GREEN**

Run: `conda run --no-capture-output -n bond_factor_lab_service python -m unittest tests.test_native_generation_executor`

Expected: PASS.

### Task 2: Add an authorized artifact-bound prewarm command

**Files:**
- Modify: `harness/authorization.py`
- Modify: `harness/signal_gap_native_artifact.py`
- Modify: `harness/cli.py`
- Test: `tests/test_signal_gap_native_cache_prewarm.py`

- [x] **Step 1: Write failing harness tests**

Create tests that use a fake registered signal-gap artifact and fake publisher runner. The public API must:

```python
prewarm_signal_gap_native_cache(
    manifest=manifest,
    historical_predict_date="2026-08-05",
    publisher_scheme_id="liwei_0616_10y01_full_oos_k3_div_k10",
    authorize=token,
    storage_root=storage_root,
)
```

Assert it rejects a non-publisher, derives the cache root under
`storage_root / ".phase-a-cache" / generation_id`, calls the runner once with the sealed generation and
prewarm mode, consumes only the dedicated prewarm token, and returns no prediction-write action.

- [x] **Step 2: Run the new tests and observe RED**

Run: `conda run --no-capture-output -n bond_factor_lab_service python -m unittest tests.test_signal_gap_native_cache_prewarm`

Expected: FAIL because the function and authorization action do not exist.

- [x] **Step 3: Implement the narrow command**

Add one `signal_gap_native_cache_prewarm` HMAC action with TTL `<=900` seconds. Reuse the existing artifact
authority normalization, derive the cache root internally, recheck the exact `SEALED` DB row, load the exact 10Y
publisher config, and call `run_configured_scheme()` only to publish/cache. After HMAC consumption, issue the
private one-time permit; do not pass the HMAC or its secret to the child. Validate that one returned record contains
a published Phase-A cache audit bound to the same Native artifact. Persist only the usual authorization audit
under `reports/harness`; do not write business tables.

- [x] **Step 4: Run GREEN**

Run: `conda run --no-capture-output -n bond_factor_lab_service python -m unittest tests.test_signal_gap_native_cache_prewarm`

Expected: PASS.

### Task 3: Make current-snapshot fill consume only the derived cache root

**Files:**
- Modify: `harness/gates/signal_gap_fill_gate.py`
- Test: `tests/test_signal_gap_fill_gate.py`

- [x] **Step 1: Write a failing fill-routing test**

Build a current-snapshot Native `_GapGroup`, invoke `_run_algorithm()` with a fake runner, and assert that its
arguments include the deterministic artifact-derived `phase_a_cache_root` while execution mode remains
`signal_gap_current_snapshot`.

- [x] **Step 2: Run the test and observe RED**

Run: `conda run --no-capture-output -n bond_factor_lab_service python -m unittest tests.test_signal_gap_fill_gate`

Expected: FAIL because fill does not pass an artifact-specific cache root.

- [x] **Step 3: Add only cache-root routing**

For `SIGNAL_GAP_NATIVE_EXPORTER_VERSION`, derive the same private root through the artifact helper and pass it
to `run_configured_scheme()`. Keep `signal_gap_fill` token scope, frozen plan, preflight/postflight, and
`hit_only` behavior unchanged.

- [x] **Step 4: Run GREEN and the focused regression set**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest \
  tests.test_native_generation_executor \
  tests.test_signal_gap_native_cache_prewarm \
  tests.test_signal_gap_fill_gate \
  tests.test_native_generation_liwei
```

Expected: PASS.

### Task 4: Verify and prepare the controlled operational handoff

**Files:**
- Test only; no new source files.

- [x] **Step 1: Run syntax and broader regression checks**

Run `git diff --check`, the focused suite above, and the full test suite appropriate to the release branch.

- [ ] **Step 2: Inspect the staged diff**

Run `git status --short` and `git diff --check`; stage only the files listed above plus these design/plan records.

- [ ] **Step 3: Commit the implementation**

Create one commit after all tests pass. Do not merge to `master`, deploy code, issue any HMAC token, or run the
prewarm/fill until the verified commit is reviewed and the release operation is explicitly confirmed.
