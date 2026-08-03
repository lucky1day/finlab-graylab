# P-1 Daily-Gray Policy-External Identity Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the frozen 28-execution daily-gray batch when policy-external active daily identities appear, while making those identities visible as isolated and never scheduling them.

**Architecture:** Keep the default policy loader exact and fail-closed. Add an explicit opt-in mode used only by `scheduler.daily_gray_runner`: it validates every frozen policy member exactly as today, separates policy-external active daily identities into an immutable evidence list, executes only frozen members, and makes the process exit non-zero after emitting the isolation evidence. This does not alter the frozen 28/32 JSON, grant scheduler admission, or write new business records.

**Tech Stack:** Python 3.12, dataclasses, unittest/pytest, existing launchd daily-gray runner.

---

## File map

- Modify: `scheduler/daily_gray_launchd_policy.py` — opt-in classification of policy-external active daily identities while retaining default exact validation.
- Modify: `scheduler/daily_gray_runner.py` — use the opt-in mode, log isolation evidence, expose it in the summary, and return non-zero after the frozen batch.
- Modify: `tests/test_daily_gray_launchd_policy.py` — pin default fail-closed behavior and the opt-in isolation classification.
- Modify: `tests/test_daily_gray_runner.py` — prove policy-external identities are never sent to `execute_scheme`, while frozen identities still are.

## Task 1: Make policy-external classification explicit and opt-in

**Files:**

- Modify: `tests/test_daily_gray_launchd_policy.py`
- Modify: `scheduler/daily_gray_launchd_policy.py`

- [ ] **Step 1: Write the failing tests**

Add a test which appends an active daily `SchemeConfig` named `seven_y_current55_lgbm_001_v1` to the real frozen discovery and asserts both modes:

```python
added = self.active_daily + (
    replace(self.active_daily[0], scheme_id="seven_y_current55_lgbm_001_v1"),
)

with self.assertRaisesRegex(
    self.module.DailyGrayLaunchdPolicyError,
    "missing active daily identities.*seven_y_current55_lgbm_001_v1",
):
    self.module.load_daily_gray_launchd_policy(discovered=added)

policy = self.module.load_daily_gray_launchd_policy(
    discovered=added,
    allow_policy_external_active_daily=True,
)
self.assertEqual(
    policy.isolated_active_daily_scheme_ids,
    ("seven_y_current55_lgbm_001_v1",),
)
self.assertEqual(len(policy.schemes), 28)
```

Add a companion assertion that removing a frozen policy identity still raises `unknown policy identities` even with `allow_policy_external_active_daily=True`.

- [ ] **Step 2: Run the policy test and verify the expected RED failure**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  pytest tests/test_daily_gray_launchd_policy.py -q
```

Expected: failure because the loader does not accept `allow_policy_external_active_daily` and the policy object has no isolation field.

- [ ] **Step 3: Implement the minimal opt-in classification**

In `DailyGrayLaunchdPolicy`, add:

```python
isolated_active_daily_scheme_ids: tuple[str, ...] = ()
```

Extend `load_daily_gray_launchd_policy` with a keyword-only default:

```python
allow_policy_external_active_daily: bool = False
```

Keep `_validate_identity_sets` exact by default. When the flag is true, still reject `policy_ids - discovered_ids`, still validate all policy rows, their 28/32 cardinality, dependencies, and frozen semantics, but classify sorted `discovered_ids - policy_ids` as `isolated_active_daily_scheme_ids` rather than raising. Do not change `deploy/daily_gray_launchd_policy_v1.json` or its expected counts.

- [ ] **Step 4: Run the policy tests and verify GREEN**

Run the same command from Step 2.

Expected: all policy tests pass; default callers retain their exact-set failure behavior.

## Task 2: Execute only frozen identities and surface isolation as failure evidence

**Files:**

- Modify: `tests/test_daily_gray_runner.py`
- Modify: `scheduler/daily_gray_runner.py`

- [ ] **Step 1: Write the failing runner tests**

Extend the test policy helper to provide `isolated_active_daily_scheme_ids`. Add a test with discovered `alpha` and `seven_y_current55_lgbm_001_v1`, policy containing only `alpha`, and isolation tuple containing the latter. Assert:

```python
result = self._run_with_fakes(
    [_config("alpha"), _config("seven_y_current55_lgbm_001_v1")],
    _policy([("alpha", "light", None)], isolated=("seven_y_current55_lgbm_001_v1",)),
)

result.execute.assert_called_once()
self.assertEqual(result.execute.call_args.args[0].scheme_id, "alpha")
self.assertEqual(
    result.summary.isolated_active_daily_scheme_ids,
    ("seven_y_current55_lgbm_001_v1",),
)
```

Add a CLI-level test that a trading-day summary with zero execution failures but a non-empty isolation tuple returns `1`.

- [ ] **Step 2: Run the runner test and verify the expected RED failure**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  pytest tests/test_daily_gray_runner.py -q
```

Expected: failure because `RunnerSummary` has no isolation evidence and `run()` does not request the opt-in policy mode.

- [ ] **Step 3: Implement the minimal runner behavior**

Call the loader only from `run()` as:

```python
policy = load_daily_gray_launchd_policy(
    discovered=discovered,
    allow_policy_external_active_daily=True,
)
```

Copy the policy's immutable isolation tuple into `RunnerSummary`. Before creating the engine, emit one `logger.error` with the exact sorted isolated IDs and the fact that they were not scheduled. Construct `schemes` exclusively from `policy.schemes`; do not add isolated configs to heavy/light pools, `--only`, dependency resolution, or `execute_scheme`. Extend `_print_summary` to show the isolated IDs. Return `1` from `main()` for a trading-day run with non-empty isolation even if all frozen executions succeeded; retain `2` for policy/preflight errors and retain the non-trading-day exit `0` behavior.

- [ ] **Step 4: Run runner tests and verify GREEN**

Run the command from Step 2.

Expected: all runner tests pass, including proof that the policy-external identity cannot write through `execute_scheme`.

## Task 3: Regression verification and scoped review

**Files:**

- Verify only: `scheduler/daily_gray_launchd_policy.py`, `scheduler/daily_gray_runner.py`, and their two test modules.

- [ ] **Step 1: Run the focused regression suite**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  pytest \
    tests/test_daily_gray_launchd_policy.py \
    tests/test_daily_gray_runner.py \
    tests/test_actuals_launchd.py \
    -q
```

Expected: exit code 0.

- [ ] **Step 2: Verify the live-current invariant without invoking it**

Run a no-write policy load against current discovery and confirm it reports exactly zero isolated identities until a valid 7Y delivery is activated. Do not run `scheduler.daily_gray_runner`, write the database, alter installed plists, or call `launchctl`.

- [ ] **Step 3: Perform two-stage review**

First have a reviewer compare the change against this plan: frozen 28/32 validation must remain exact; policy-external identities must never reach an executor; no policy JSON or scheduler admission changes are allowed. Then have a separate reviewer inspect error handling, logging, test quality, and backward compatibility. Address all important findings before progressing.

- [ ] **Step 4: Record the production boundary**

Document that source changes are verified on the development branch only. Do not activate the 7Y schemes, persist a backtest, write gray/live data, reload the backend, replace an installed plist, or use `launchctl` without a new, explicit authorization.
