# Blackbox Predict Timeout Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop scheduled Blackbox predictions from being implicitly capped at 600 seconds while preserving all 39 existing exact scheme versions and applying explicit operation deadlines only as narrowing constraints.

**Architecture:** Keep `config.yaml.schedule.timeout_sec` as the scheme request and `blackbox-v2-v1.predict_timeout_sec` as the platform ceiling. Make `execute_scheme()` default to no operation deadline, resolve the scheme request against an explicitly supplied deadline, and let the existing Blackbox runner cap that request against the Runtime Profile. Do not change Native or backtest behavior and do not perform any production lifecycle operation.

**Tech Stack:** Python 3.12, YAML/JSON configuration, dataclasses, unittest/pytest, existing Blackbox V2 executor and subprocess runner.

---

## File map

- `schemes/*/config.yaml`: restore and retain `schedule.timeout_sec: 3600` for all 39 Blackbox schemes.
- `shared/scheme_config_schema.py`: require a positive Blackbox schedule timeout instead of forbidding it.
- `shared/blackbox_v2/intake.py`: continue generating the 3600-second scheme request.
- `shared/blackbox_v2/versioning.py`: continue including the scheme request in the canonical config hash.
- `scheduler/discovery.py`: load the Blackbox scheme request into `SchemeSchedule.timeout_sec`.
- `deploy/blackbox_v2/runtime_profile_v1.json`: retain the 3600-second platform ceiling and 14400-second backtest ceiling.
- `scheduler/executor.py`: distinguish optional operation deadline from scheme request and stop rewriting Runtime Profile values.
- `scheduler/blackbox_v2_runner.py`: preserve the already-supported `min(profile ceiling, supplied request)` subprocess boundary.
- `tests/test_config_schema.py`, `tests/test_blackbox_v2_intake.py`, `tests/test_blackbox_v2_discovery.py`, `tests/test_blackbox_timeout_drift.py`, `tests/test_blackbox_v2_runner.py`: lock the three-layer contract and exact-version preservation.
- Five existing architecture/SOP documents: replace the superseded single-authority wording with the three-layer budget contract.

### Task 1: Restore the version-preserving configuration contract

**Files:**
- Modify: `schemes/*/config.yaml` for the 39 Blackbox schemes
- Modify: `shared/scheme_config_schema.py`
- Modify: `shared/blackbox_v2/intake.py`
- Modify: `shared/blackbox_v2/versioning.py`
- Modify: `scheduler/discovery.py`
- Modify: `deploy/blackbox_v2/runtime_profile_v1.json`
- Modify: `tests/test_config_schema.py`
- Modify: `tests/test_blackbox_v2_intake.py`
- Modify: `tests/test_blackbox_v2_discovery.py`

- [ ] **Step 1: Restore the target-branch contract files without copying user work**

Use `git show 9fe8b73:<path>` only as read-only reference and `apply_patch` to restore:

```yaml
schedule:
  cron: '<existing cron>'
  timezone: Asia/Shanghai
  timeout_sec: 3600
```

Restore Intake generation:

```python
"  timeout_sec: 3600\n"
```

Restore canonical versioning:

```python
timeout_sec = schedule.get("timeout_sec")
canonical["schedule"] = {
    "cron": str(_required_value(schedule, "cron", "schedule.cron")),
    "timezone": str(schedule.get("timezone", DEFAULT_TIMEZONE)),
    "timeout_sec": int(timeout_sec) if timeout_sec is not None else None,
}
```

Restore discovery:

```python
timeout_sec=(
    int(schedule_raw["timeout_sec"])
    if schedule_raw.get("timeout_sec") is not None
    else None
),
```

Restore Runtime Profile values:

```json
"predict_timeout_sec": 3600,
"backtest_timeout_sec": 14400
```

- [ ] **Step 2: Add a failing missing-timeout schema test**

In `tests/test_config_schema.py`, use the existing Blackbox fixture and add:

```python
def test_blackbox_schedule_timeout_sec_is_required(self) -> None:
    config = _base_blackbox_config()
    config["schedule"].pop("timeout_sec", None)

    errors = validate_config(config, dirname="demo_blackbox")

    self.assertIn(
        "Blackbox V2 schedule.timeout_sec must be a positive integer",
        errors,
    )
```

- [ ] **Step 3: Run the new test and verify RED**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q \
  tests/test_config_schema.py::ConfigSchemaScheduleTests::test_blackbox_schedule_timeout_sec_is_required
```

Expected: FAIL because the restored baseline accepts an omitted Blackbox timeout.

- [ ] **Step 4: Require the Blackbox scheme request**

Inside the Blackbox schedule validation branch in `shared/scheme_config_schema.py`, implement:

```python
timeout_sec = schedule.get("timeout_sec")
if type(timeout_sec) is not int or timeout_sec <= 0:
    errors.append(
        "Blackbox V2 schedule.timeout_sec must be a positive integer"
    )
```

Do not add a default or compatibility fallback.

- [ ] **Step 5: Run focused config, Intake, discovery, and version tests**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q \
  tests/test_config_schema.py \
  tests/test_blackbox_v2_intake.py \
  tests/test_blackbox_v2_discovery.py \
  tests/test_active_blackbox_conformance.py
```

Expected: PASS.

- [ ] **Step 6: Prove all scheme configs and exact versions match the target branch**

Run:

```bash
git diff --exit-code 9fe8b73 -- schemes
git diff --exit-code 9fe8b73 -- deploy/blackbox_v2/runtime_profile_v1.json
```

Expected: both commands exit 0 with no output.

- [ ] **Step 7: Commit the contract restoration**

```bash
git add deploy/blackbox_v2/runtime_profile_v1.json schemes \
  shared/scheme_config_schema.py shared/blackbox_v2/intake.py \
  shared/blackbox_v2/versioning.py scheduler/discovery.py \
  tests/test_config_schema.py tests/test_blackbox_v2_intake.py \
  tests/test_blackbox_v2_discovery.py
git commit -m "fix(blackbox): preserve timeout version contract"
```

### Task 2: Implement the three-layer predict budget with TDD

**Files:**
- Modify: `tests/test_blackbox_timeout_drift.py`
- Modify: `tests/test_blackbox_v2_runner.py`
- Modify: `scheduler/executor.py`
- Modify: `scheduler/blackbox_v2_runner.py`

- [ ] **Step 1: Write failing executor hierarchy tests**

Replace the superseded single-authority assertions in `tests/test_blackbox_timeout_drift.py` with:

```python
def test_runtime_profile_keeps_3600_second_predict_ceiling(self) -> None:
    from scheduler.blackbox_v2_runner import DEFAULT_RUNTIME_PROFILE

    self.assertEqual(DEFAULT_RUNTIME_PROFILE.predict_timeout_sec, 3600)
    self.assertEqual(DEFAULT_RUNTIME_PROFILE.backtest_timeout_sec, 14400)

def test_blackbox_without_operation_deadline_uses_scheme_request(self) -> None:
    self.assertEqual(self._effective(_cfg("blackbox_v2", 3600)), 3600)

def test_blackbox_explicit_deadline_can_only_narrow_scheme_request(self) -> None:
    cfg = _cfg("blackbox_v2", 3600)
    self.assertEqual(self._effective(cfg, 600), 600)
    self.assertEqual(self._effective(cfg, 7200), 3600)

def test_blackbox_missing_scheme_request_fails_closed(self) -> None:
    with self.assertRaisesRegex(ValueError, "timeout_sec must be configured"):
        self._effective(_cfg("blackbox_v2", None))
```

Keep the Native assertions:

```python
self.assertEqual(self._effective(_cfg("native_adapter", 3600)), 3600)
self.assertEqual(self._effective(_cfg("native_adapter", None)), 600)
```

- [ ] **Step 2: Run hierarchy tests and verify RED**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q tests/test_blackbox_timeout_drift.py
```

Expected: FAIL because current Blackbox `_effective_timeout_sec()` ignores the scheme request.

- [ ] **Step 3: Implement the minimal executor resolution**

Make `execute_scheme()` keep the optional operation deadline:

```python
timeout_sec: int | None = None,
```

Implement `_effective_timeout_sec()` as:

```python
def _effective_timeout_sec(
    cfg: SchemeConfig,
    operation_timeout_sec: int | None,
) -> int:
    runtime_type = getattr(cfg, "runtime_type", "native_adapter")
    schedule = getattr(cfg, "schedule", None)
    configured = getattr(schedule, "timeout_sec", None)
    if configured is None:
        configured = getattr(cfg, "execution_timeout_sec", None)

    if runtime_type == "blackbox_v2":
        if configured is None:
            raise ValueError(
                f"scheme {cfg.scheme_id} timeout_sec must be configured"
            )
        timeout = int(configured)
        if operation_timeout_sec is not None:
            operation_timeout = int(operation_timeout_sec)
            if operation_timeout <= 0:
                raise ValueError(
                    f"scheme {cfg.scheme_id} timeout_sec must be positive, "
                    f"got {operation_timeout}"
                )
            timeout = min(timeout, operation_timeout)
    else:
        fallback = 600 if operation_timeout_sec is None else int(operation_timeout_sec)
        timeout = int(configured) if configured is not None else fallback

    if timeout <= 0:
        raise ValueError(
            f"scheme {cfg.scheme_id} timeout_sec must be positive, got {timeout}"
        )
    return timeout
```

Keep `run_blackbox_scheme_subprocess()` passing `timeout_sec` separately to
`run_blackbox_predict()` and keep the Runtime Profile unchanged except for the existing conda-env
selection.

- [ ] **Step 4: Run hierarchy tests and verify GREEN**

Run the same command as Step 2.

Expected: all tests pass.

- [ ] **Step 5: Lock the runner ceiling independently**

In `tests/test_blackbox_v2_runner.py`, keep or add a table-driven test around
`execute_blackbox_cli()` that captures `_run_process(timeout=...)` and asserts:

```python
for requested, expected in ((None, 3600), (600, 600), (7200, 3600)):
    # execute_blackbox_cli(profile=predict_timeout_sec=3600,
    #                      timeout_sec=requested)
    self.assertEqual(run_process.call_args.kwargs["timeout"], expected)
```

- [ ] **Step 6: Run executor/runner integration tests**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q \
  tests/test_blackbox_timeout_drift.py \
  tests/test_blackbox_v2_runner.py \
  tests/test_launchd_prediction_runner.py \
  tests/test_signal_gap_plan.py \
  tests/test_harness_static_gate.py
```

Expected: PASS, including scheduled calls with no operation deadline and explicit Harness/gap
deadlines.

- [ ] **Step 7: Commit the runtime fix**

```bash
git add scheduler/executor.py scheduler/blackbox_v2_runner.py \
  tests/test_blackbox_timeout_drift.py tests/test_blackbox_v2_runner.py
git commit -m "fix(runtime): honor blackbox timeout hierarchy"
```

### Task 3: Align documentation and verify the complete branch

**Files:**
- Modify: `docs/architecture/ARCHITECTURE.md`
- Modify: `docs/architecture/BLACKBOX_V2_PLATFORM.md`
- Modify: `docs/architecture/CODE_ARCHITECTURE.md`
- Modify: `docs/architecture/HARNESS_ARCHITECTURE.md`
- Modify: `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`
- Modify: `docs/superpowers/plans/2026-08-13-blackbox-predict-timeout-authority.md`

- [ ] **Step 1: Replace the superseded single-authority wording**

Document exactly:

```text
Blackbox predict final timeout = min(
  scheme schedule.timeout_sec,
  Runtime Profile predict_timeout_sec,
  explicit operation deadline when supplied
)
```

State that scheme configuration is the request, Runtime Profile is the ceiling, operation deadline
is optional and narrowing-only, and no exact version or lifecycle transition is required.

- [ ] **Step 2: Verify no stale heavy-design claims remain**

Run:

```bash
rg -n "only Blackbox prediction-timeout authority|sole Blackbox predict|new exact version|39.*Gate|predict_timeout_sec.*600" \
  docs deploy scheduler shared tests
```

Expected: no stale contract claim; test fixture occurrences are allowed only when explicitly testing
deadline narrowing.

- [ ] **Step 3: Compile changed Python directories**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m compileall -q scheduler shared tests
```

Expected: exit 0.

- [ ] **Step 4: Run complete pytest**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 5: Verify diff safety and exact-version preservation**

Run:

```bash
git diff --check 9fe8b73..HEAD
git diff --exit-code 9fe8b73 -- schemes deploy/blackbox_v2/runtime_profile_v1.json
git status --short
```

Expected: diff check exits 0; scheme/Profile diff is empty; only intended tracked branch changes
remain and the isolated worktree is clean after the final commit.

- [ ] **Step 6: Commit documentation**

```bash
git add docs/architecture/ARCHITECTURE.md \
  docs/architecture/BLACKBOX_V2_PLATFORM.md \
  docs/architecture/CODE_ARCHITECTURE.md \
  docs/architecture/HARNESS_ARCHITECTURE.md \
  docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md \
  docs/superpowers/plans/2026-08-13-blackbox-predict-timeout-authority.md
git commit -m "docs: align blackbox timeout hierarchy"
```

### Task 4: Integrate only into the authorized development branch

**Files:**
- No source edits
- Preserve all existing untracked files in `/Users/macstudio0/bond-factor-lab`

- [ ] **Step 1: Re-run shared-worktree safety checks**

```bash
git -C /Users/macstudio0/bond-factor-lab branch --show-current
git -C /Users/macstudio0/bond-factor-lab status --short
git -C /Users/macstudio0/bond-factor-lab rev-parse '@{upstream}'
git -C /Users/macstudio0/bond-factor-lab rev-parse HEAD
git ls-remote origin refs/heads/codex/audit-bugfixes-20260613
```

Expected: exact branch/upstream match; local HEAD equals remote target; no tracked edits; untracked user
paths do not overlap the changed files.

- [ ] **Step 2: Merge with a normal merge commit**

```bash
git -C /Users/macstudio0/bond-factor-lab merge --no-ff --no-edit \
  codex/issue-45-timeout-authority-20260813
```

Expected: clean merge without touching `master`.

- [ ] **Step 3: Re-run diff check, focused tests, and complete pytest on the development branch**

Use the exact commands from Tasks 2 and 3 in `/Users/macstudio0/bond-factor-lab`.

Expected: all checks pass.

- [ ] **Step 4: Push normally and verify remote readback**

```bash
git -C /Users/macstudio0/bond-factor-lab push origin codex/audit-bugfixes-20260613
git ls-remote origin refs/heads/codex/audit-bugfixes-20260613
```

Expected: remote SHA exactly equals local development-branch HEAD; never force push.
