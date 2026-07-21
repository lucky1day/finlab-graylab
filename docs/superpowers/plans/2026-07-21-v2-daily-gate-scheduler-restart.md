# Blackbox V2 Daily Gate and Scheduler Restart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an automated 06:00/06:30/06:35/07:00 DataBridge readiness workflow that restarts the shared scheduler only after a successful final check, blocks only Blackbox V2 scheduled runs on failure, and prevents per-job Registry metadata rewrites.

**Architecture:** A focused `scheduler.v2_daily_gate` module owns atomic day credentials and runtime validation, while `scheduler.v2_daily_preflight` owns the four clock phases and the fixed-label launchd restart. `scheduler.main` applies the credential only to `blackbox_v2 + data_bridge_current` scheduled execution and no longer syncs Registry from individual prediction jobs. A dedicated launchd agent triggers the preflight phases; Native V1 never reads the V2 credential.

**Tech Stack:** Python 3.12, pytest/unittest, FastAPI/SQLAlchemy existing services, APScheduler, macOS launchd, JSON runtime evidence.

---

## File map

- Create `scheduler/v2_daily_gate.py`: atomic daily credential persistence and DataBridge-bound V2 readiness validation.
- Create `scheduler/v2_daily_preflight.py`: phase resolution, primary refresh, check, conditional retry, final decision, fixed-label restart and JSON summaries.
- Create `tests/test_v2_daily_gate.py`: credential and generation/digest validation tests.
- Create `tests/test_v2_daily_preflight.py`: time-phase, retry, deadline and restart decision tests.
- Modify `scheduler/main.py`: V2-only scheduled execution Gate; remove per-job Registry sync and scheduler-owned daily refresh cron.
- Modify `tests/test_scheduler_main.py`: prove V1 isolation, V2 blocking, startup catchup behavior, Registry write boundary, and absence of the old refresh job.
- Create `deploy/launchd/com.bond-factor-lab.v2-preflight.plist`: four daily launchd triggers and dedicated logs.
- Modify `deploy/launchd/com.bond-factor-lab.scheduler.plist`: set 06:00/07:00 DataBridge environment values.
- Modify `tests/test_onboarding_docs.py`: machine-check launchd and SOP requirements.
- Modify `scripts/check_production_daily_health.py` and `tests/test_production_daily_health.py`: expose V2 gate health separately from V1 daily health.
- Modify `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`: authoritative time management, restart, alert and no-catchup procedure.
- Modify `docs/blackbox_v2/PRODUCTION_READINESS.md`: production readiness checklist for the daily credential and restart evidence.
- Modify `docs/CURRENT_STATUS.md`: replace the missed manual restart item with the automated control state.
- Create `reports/production-gray-20260720/v2-scheduler-control-20260721.json`: machine evidence from controlled deployment; keep under the existing ignored production evidence directory and do not commit it.

### Task 1: Daily credential store and validation

**Files:**
- Create: `tests/test_v2_daily_gate.py`
- Create: `scheduler/v2_daily_gate.py`

- [ ] **Step 1: Write failing credential tests**

Create a `TestV2DailyGate` fixture using a temporary `DataBridgeRefreshConfig` and a minimal current state. The public behavior must be explicit:

```python
def test_ready_gate_round_trips_atomically(self):
    record = write_gate_record(
        self.config,
        run_date="2026-07-22",
        status="ready",
        checked_at="2026-07-22T07:00:00+08:00",
        generation_id="full-20260722-a",
        refresh_date="2026-07-22",
        expected_daily_date="2026-07-21",
        business_digest="digest-a",
        checks=[{"name": "current_dataset", "status": "passed"}],
    )
    self.assertEqual(load_gate_record(self.config, "2026-07-22"), record)

def test_missing_gate_blocks_v2(self):
    with self.assertRaisesRegex(V2DailyGateBlocked, "missing"):
        require_v2_daily_ready(self.config, "2026-07-22", "2026-07-21")

def test_generation_mismatch_blocks_v2(self):
    self._publish_ready_gate(generation_id="full-20260722-old")
    self._write_current_state(generation_id="full-20260722-new")
    with self.assertRaisesRegex(V2DailyGateBlocked, "generation"):
        require_v2_daily_ready(self.config, "2026-07-22", "2026-07-21")
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest tests/test_v2_daily_gate.py -q
```

Expected: collection fails because `scheduler.v2_daily_gate` does not exist.

- [ ] **Step 3: Implement the minimal credential module**

Implement these public symbols:

```python
SCHEMA_VERSION = "v2-scheduler-gate-v1"

class V2DailyGateBlocked(RuntimeError):
    pass

def gate_record_path(config: DataBridgeRefreshConfig, run_date: str) -> Path:
    return config.runtime_root / "v2_scheduler_gate" / f"{run_date}.json"

def write_gate_record(
    config: DataBridgeRefreshConfig,
    *,
    run_date: str,
    status: Literal["checking", "ready", "blocked"],
    checked_at: str,
    generation_id: str | None,
    refresh_date: str | None,
    expected_daily_date: str,
    business_digest: str | None,
    checks: Sequence[Mapping[str, object]],
    restart: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if status not in {"checking", "ready", "blocked"}:
        raise ValueError(f"unsupported V2 daily gate status: {status}")
    record = {
        "schema_version": SCHEMA_VERSION,
        "run_date": date.fromisoformat(run_date).isoformat(),
        "status": status,
        "checked_at": checked_at,
        "generation_id": generation_id,
        "refresh_date": refresh_date,
        "expected_daily_date": date.fromisoformat(expected_daily_date).isoformat(),
        "business_digest": business_digest,
        "checks": [dict(item) for item in checks],
        "restart": dict(restart or {}),
    }
    path = gate_record_path(config, run_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".v2-gate-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return record

def load_gate_record(config: DataBridgeRefreshConfig, run_date: str) -> dict[str, object]:
    path = gate_record_path(config, run_date)
    if not path.is_file():
        raise V2DailyGateBlocked(f"V2 daily gate is missing for {run_date}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise V2DailyGateBlocked(f"V2 daily gate is invalid for {run_date}")
    return payload

def require_v2_daily_ready(
    config: DataBridgeRefreshConfig,
    run_date: str,
    expected_daily_date: str,
) -> dict[str, object]:
    record = load_gate_record(config, run_date)
    if record.get("schema_version") != SCHEMA_VERSION:
        raise V2DailyGateBlocked("V2 daily gate schema mismatch")
    if record.get("run_date") != run_date or record.get("status") != "ready":
        raise V2DailyGateBlocked(f"V2 daily gate is not ready for {run_date}")
    current = check_current_dataset(
        config,
        required_refresh_date=run_date,
        expected_daily_date=expected_daily_date,
    )
    for field in ("generation_id", "refresh_date", "business_digest"):
        if record.get(field) != current.state.get(field):
            raise V2DailyGateBlocked(f"V2 daily gate {field} mismatch")
    return record
```

Use `tempfile.mkstemp`, `json.dump`, `os.fsync`, and `os.replace`. `require_v2_daily_ready` must call `check_current_dataset` with `required_refresh_date=run_date` and the previous trading day, then compare `generation_id`, `refresh_date`, and `business_digest` between the ready credential and the current state.

- [ ] **Step 4: Run tests and verify GREEN**

Run the Task 1 pytest command. Expected: all tests pass.

- [ ] **Step 5: Commit Task 1**

Before staging, run `git status --short` and `git branch --list`. Stage only the two Task 1 files, then commit:

```bash
git commit -m "feat: add V2 daily readiness credential"
```

### Task 2: Four-phase preflight controller

**Files:**
- Create: `tests/test_v2_daily_preflight.py`
- Create: `scheduler/v2_daily_preflight.py`

- [ ] **Step 1: Write failing phase and orchestration tests**

Cover the exact clock boundaries and side effects:

```python
def test_resolve_phase_uses_four_exact_windows(self):
    self.assertEqual(resolve_phase(self._at(6, 0)), "refresh-primary")
    self.assertEqual(resolve_phase(self._at(6, 30)), "check-primary")
    self.assertEqual(resolve_phase(self._at(6, 35)), "refresh-retry")
    self.assertEqual(resolve_phase(self._at(7, 0)), "finalize")

def test_retry_skips_when_current_is_valid(self):
    result = run_phase("refresh-retry", now=self._at(6, 35), dependencies=self.deps)
    self.assertEqual(result["status"], "skipped-current")
    self.refresh.assert_not_called()

def test_finalize_failure_writes_blocked_without_restart(self):
    self.check.side_effect = ValueError("stale generation")
    result = run_phase("finalize", now=self._at(7, 0), dependencies=self.deps)
    self.assertEqual(result["status"], "blocked")
    self.restart.assert_not_called()

def test_late_finalize_cannot_authorize_restart(self):
    with self.assertRaisesRegex(PreflightError, "safe window"):
        run_phase("finalize", now=self._at(7, 1), dependencies=self.deps)
    self.restart.assert_not_called()
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest tests/test_v2_daily_preflight.py -q
```

Expected: collection fails because `scheduler.v2_daily_preflight` does not exist.

- [ ] **Step 3: Implement phase resolution and dependency boundary**

Define a small dependency object so orchestration can be tested without launchctl or production refreshes:

```python
@dataclass(frozen=True)
class PreflightDependencies:
    config_factory: Callable[[], DataBridgeRefreshConfig]
    refresh: Callable[[str], Mapping[str, object]]
    check: Callable[[str], Mapping[str, object]]
    restart_scheduler: Callable[[datetime], Mapping[str, object]]

PHASE_CLOCKS = {
    (6, 0): "refresh-primary",
    (6, 30): "check-primary",
    (6, 35): "refresh-retry",
    (7, 0): "finalize",
}
```

`run_phase` must always emit a JSON-serializable summary. Primary refresh runs once; primary check is read-only; retry first checks current and refreshes only when stale; finalize must occur during 07:00:00–07:00:59, write `blocked` on validation failure, and call the restart dependency only after writing a generation-bound ready decision.

- [ ] **Step 4: Implement fixed-label restart safely**

The production restart function must hard-code `com.bond-factor-lab.scheduler`, resolve `gui/{os.getuid()}`, record the old launchd PID, call:

```text
launchctl kickstart -k gui/<uid>/com.bond-factor-lab.scheduler
```

and poll `launchctl print` for a changed running PID. No caller-supplied label, PID, shell expansion, or recursive retry is allowed. A failed verification must rewrite the day credential to `blocked`.

- [ ] **Step 5: Run tests and verify GREEN**

Run the Task 2 pytest command. Expected: all tests pass.

- [ ] **Step 6: Commit Task 2**

Stage only `scheduler/v2_daily_preflight.py` and `tests/test_v2_daily_preflight.py`, then commit:

```bash
git commit -m "feat: orchestrate V2 daily preflight"
```

### Task 3: V2-only scheduler Gate and Registry write boundary

**Files:**
- Modify: `tests/test_scheduler_main.py`
- Modify: `scheduler/main.py`

- [ ] **Step 1: Write failing isolation tests**

Add focused tests:

```python
def test_v2_scheduled_job_requires_daily_ready_gate(self):
    cfg = _cfg("blackbox_demo", runtime_type="blackbox_v2", input_source="data_bridge_current")
    with (
        patch.object(scheduler_main, "discover_schemes", return_value=[cfg]),
        patch.object(scheduler_main, "require_v2_daily_ready", side_effect=V2DailyGateBlocked("blocked")),
        patch.object(scheduler_main, "execute_scheme") as execute,
    ):
        result = scheduler_main.run_prediction_job("blackbox_demo", run_date="2026-07-22")
    self.assertEqual(result.status, "skipped")
    execute.assert_not_called()

def test_native_v1_does_not_read_v2_daily_gate(self):
    cfg = _cfg("native_demo", runtime_type="native_adapter")
    with patch.object(scheduler_main, "require_v2_daily_ready") as gate:
        scheduler_main.run_prediction_job("native_demo", run_date="2026-07-22")
    gate.assert_not_called()

def test_single_prediction_job_does_not_sync_registry(self):
    with patch.object(scheduler_main, "_sync_registry") as sync:
        scheduler_main.run_prediction_job("daily_demo", run_date="2026-07-22")
    sync.assert_not_called()
```

Also change the scheduler registration test to assert `data-bridge-refresh` is absent; DataBridge refresh is now owned by preflight launchd.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest tests/test_scheduler_main.py -q
```

Expected: new assertions fail because all schemes currently share the same path, `run_prediction_job` syncs Registry, and the scheduler still registers the 05:30 refresh job.

- [ ] **Step 3: Add the minimal V2-only Gate**

In `_run_prediction_config`, before opening the prediction slot:

```python
if (
    getattr(cfg, "runtime_type", "native_adapter") == "blackbox_v2"
    and getattr(cfg, "input_source", None) == "data_bridge_current"
):
    try:
        require_v2_daily_ready(
            DataBridgeRefreshConfig.from_env(),
            predict_date,
            _previous_trading_day(predict_date),
        )
    except V2DailyGateBlocked as exc:
        logger.error("V2 scheduled prediction blocked: scheme=%s date=%s error=%s", cfg.scheme_id, predict_date, exc)
        return SchemeRunResult(cfg.scheme_id, "skipped", 0, 0.0, str(exc))
```

This path is used by normal cron and startup catchup. It must not be placed in `execute_scheme`, because authorized `gray_live` and backtest flows are outside this daily scheduler Gate.

- [ ] **Step 4: Remove per-job Registry sync and scheduler refresh ownership**

Delete `_sync_registry(schemes)` from `run_prediction_job` and `run_all_prediction_jobs`. Keep sync in `build_scheduler` and explicit lifecycle/admin paths. Remove the `data-bridge-refresh` APScheduler job from `build_scheduler`; retain the callable for the preflight controller and explicit CLI refresh.

- [ ] **Step 5: Run scheduler tests and verify GREEN**

Run the Task 3 pytest command. Expected: all scheduler tests pass.

- [ ] **Step 6: Run adjacent regression tests**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest \
    tests/test_scheduler_main.py \
    tests/test_scheduler_discovery.py \
    tests/test_executor_run_id.py \
    tests/test_backend_api.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 3**

Stage only `scheduler/main.py` and `tests/test_scheduler_main.py`, then commit:

```bash
git commit -m "fix: isolate V2 scheduler readiness"
```

### Task 4: launchd time management

**Files:**
- Create: `deploy/launchd/com.bond-factor-lab.v2-preflight.plist`
- Modify: `deploy/launchd/com.bond-factor-lab.scheduler.plist`
- Modify: `tests/test_onboarding_docs.py`

- [ ] **Step 1: Write failing plist contract tests**

Parse the plist with `plistlib` and assert:

```python
self.assertEqual(plist["Label"], "com.bond-factor-lab.v2-preflight")
self.assertNotIn("KeepAlive", plist)
self.assertEqual(
    {(item["Hour"], item["Minute"]) for item in plist["StartCalendarInterval"]},
    {(6, 0), (6, 30), (6, 35), (7, 0)},
)
self.assertIn("scheduler.v2_daily_preflight", plist["ProgramArguments"])
self.assertEqual(scheduler_env["DATABRIDGE_REFRESH_START"], "06:00")
self.assertEqual(scheduler_env["DATABRIDGE_REFRESH_DEADLINE"], "07:00")
```

- [ ] **Step 2: Run docs tests and verify RED**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest tests/test_onboarding_docs.py -q
```

Expected: failure because the preflight plist does not exist and scheduler env values are absent.

- [ ] **Step 3: Add the preflight plist and scheduler environment**

The preflight plist uses the service conda environment, module `scheduler.v2_daily_preflight`, project working directory, four `StartCalendarInterval` dictionaries, `ProcessType=Background`, and dedicated stdout/stderr paths. It must not set `RunAtLoad` or `KeepAlive`.

Set these environment values on both scheduler and preflight where applicable:

```xml
<key>DATABRIDGE_REFRESH_START</key>
<string>06:00</string>
<key>DATABRIDGE_REFRESH_DEADLINE</key>
<string>07:00</string>
```

- [ ] **Step 4: Validate plists and tests**

Run:

```bash
plutil -lint deploy/launchd/com.bond-factor-lab.scheduler.plist
plutil -lint deploy/launchd/com.bond-factor-lab.v2-preflight.plist
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest tests/test_onboarding_docs.py -q
```

Expected: both plists are OK and all tests pass.

- [ ] **Step 5: Commit Task 4**

Stage only the two tracked plists and `tests/test_onboarding_docs.py`; explicitly exclude the unrelated untracked `deploy/launchd/com.bondprojectpro.backend.plist`. Commit:

```bash
git commit -m "feat: schedule V2 daily preflight"
```

### Task 5: Health reporting and platform SOP

**Files:**
- Modify: `scripts/check_production_daily_health.py`
- Modify: `tests/test_production_daily_health.py`
- Modify: `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`
- Modify: `docs/blackbox_v2/PRODUCTION_READINESS.md`
- Modify: `docs/CURRENT_STATUS.md`
- Modify: `tests/test_onboarding_docs.py`

- [ ] **Step 1: Write failing health and documentation tests**

Create the following focused health and documentation assertions:

```python
def test_blocked_v2_gate_is_reported_separately(self):
    snapshot = V2SchedulerGateSnapshot(
        run_date="2026-07-22",
        status="blocked",
        generation_id="full-old",
        current_generation_id="full-current",
        restart_verified=False,
        error="current dataset is stale",
    )
    findings = evaluate_v2_scheduler_gate(snapshot)
    self.assertEqual([item.code for item in findings], ["v2_daily_gate_blocked"])
    self.assertEqual(status_from_findings(evaluate_daily_health(self._snapshot())), "ok")

def test_platform_sop_defines_v2_preflight_timeline(self):
    platform = PLATFORM_SOP.read_text(encoding="utf-8")
    for marker in (
        "06:00", "06:30", "06:35", "07:00",
        "V1 不读取", "校验成功后重启", "不重启、不补跑",
        "单个预测任务不得回写 Registry",
    ):
        self.assertIn(marker, platform)
    upstream = UPSTREAM_SOP.read_text(encoding="utf-8")
    self.assertNotIn("06:35", upstream)
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest tests/test_production_daily_health.py tests/test_onboarding_docs.py -q
```

Expected: failures for the missing V2 snapshot and missing authoritative SOP language.

- [ ] **Step 3: Implement separate V2 health reporting**

Define and populate the separate snapshot:

```python
@dataclass(frozen=True)
class V2SchedulerGateSnapshot:
    run_date: str
    status: str | None
    generation_id: str | None
    current_generation_id: str | None
    restart_verified: bool
    error: str | None

def evaluate_v2_scheduler_gate(snapshot: V2SchedulerGateSnapshot) -> list[HealthFinding]:
    if snapshot.status is None:
        code = "v2_daily_gate_missing"
    elif snapshot.status == "blocked":
        code = "v2_daily_gate_blocked"
    elif snapshot.generation_id != snapshot.current_generation_id:
        code = "v2_daily_gate_generation_mismatch"
    elif not snapshot.restart_verified:
        code = "v2_scheduler_restart_unverified"
    else:
        return []
    return [
        HealthFinding(
            level="error",
            code=code,
            message=f"Blackbox V2 scheduler gate failed for {snapshot.run_date}",
            detail=asdict(snapshot),
        )
    ]
```

Load the credential for the requested production date and use only these finding codes:

```text
v2_daily_gate_missing
v2_daily_gate_blocked
v2_daily_gate_generation_mismatch
v2_scheduler_restart_unverified
```

Do not add V2 gate findings to the Native V1 `missing_runs` calculation. JSON output must contain separate `daily_health` and `v2_scheduler_gate` sections.

- [ ] **Step 4: Update only platform-owned documentation**

Document the four timestamps and operational boundary in the Blackbox V2 platform SOP and readiness checklist. Do not change `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`; DataBridge time management is a platform responsibility, not an upstream algorithm delivery requirement.

Update `CURRENT_STATUS.md` to state that the old manual 07:03 restart requirement is superseded by the new automated preflight, while natural `scheduled_live` remains pending until observed.

- [ ] **Step 5: Run tests and verify GREEN**

Run the Task 5 pytest command. Expected: all tests pass.

- [ ] **Step 6: Commit Task 5**

Stage only the health script/tests and three platform documents, then commit:

```bash
git commit -m "docs: govern V2 scheduler preflight"
```

### Task 6: Full verification and controlled production deployment

**Files:**
- Runtime evidence only: `reports/production-gray-20260720/v2-scheduler-control-20260721.json`
- No source changes unless a verification exposes a defect; defects restart at a failing test.

- [ ] **Step 1: Run the complete relevant regression suite**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest \
    tests/test_v2_daily_gate.py \
    tests/test_v2_daily_preflight.py \
    tests/test_scheduler_main.py \
    tests/test_blackbox_v2_*.py \
    tests/test_backend_api.py \
    tests/test_frontend_factor_lab.py \
    tests/test_production_daily_health.py \
    tests/test_onboarding_docs.py -q
```

Expected: zero failures.

- [ ] **Step 2: Verify the four active V2 configurations without writes**

Confirm all four configs are `active`, their exact versions are active, Registry composite rows are active, local `display_name` values are concise, and no excluded scheme became active.

- [ ] **Step 3: Install the preflight agent**

After checking `git status --short` and branch lists, copy only the tracked preflight plist to `~/Library/LaunchAgents/`, bootstrap it in `gui/$UID`, and verify `launchctl print` reports all four calendar intervals. Do not touch the unrelated `com.bondprojectpro.backend.plist`.

- [ ] **Step 4: Perform the one-time controlled scheduler restart**

Only after the day's normal scheduled predictions have completed, write/retain a `blocked` day credential for 2026-07-21 so startup catchup cannot run V2. Install the tracked scheduler plist, then use the fixed-label restart path. Verify:

- scheduler PID and start time changed;
- all existing V1 jobs remain registered;
- the four V2 jobs are registered once each;
- startup catchup skipped all four V2 and wrote no `scheduled_live` rows for 2026-07-21;
- Registry names are the four concise `LIQ_EXCESS_*` values.

- [ ] **Step 5: Restart backend and verify API/frontend metadata**

Restart only `com.bond-factor-lab.backend`, verify `/api/schemes` returns four short names, and confirm the 1Y × T+5 cell shows four candidates without the redundant long prefix. This restart does not authorize any prediction write.

- [ ] **Step 6: Save machine evidence**

Write a JSON evidence file containing commit IDs, test command/results, launchd schedule, old/new scheduler PID, mounted V1/V2 counts, Registry names, API names, current DataBridge generation, and explicit `scheduled_live` deltas. Keep the ignored reports directory out of Git.

- [ ] **Step 7: Next-trading-day observation**

At 06:00/06:30/06:35/07:00 observe the launchd logs and daily credential. After 07:03, confirm V1 run continuity and four V2 natural `scheduled_live` rows at their effective staggered times. Only then change production status from `Onboarding Complete` to `Production Observed` in a later evidence commit.

### Task 7: Final branch verification and handoff

**Files:**
- No additional files expected.

- [ ] **Step 1: Inspect scope**

Run `git status --short`, `git diff --check`, and `git log --oneline -10`. Confirm the untracked plist and ignored report artifacts were not committed accidentally.

- [ ] **Step 2: Re-run the full relevant regression command**

Use the Task 6 command and require a fresh zero-failure result.

- [ ] **Step 3: Report actual production state**

Report separately: implementation complete, launchd installed, scheduler/backend restarted, V1 unaffected, V2 gate status, current API names, and whether next-day `scheduled_live` observation is still pending. Do not claim `Production Observed` before a natural run is present.

- [ ] **Step 4: Preserve branch policy**

Keep all commits on `codex/audit-bugfixes-20260613`. Do not merge or push `master` without a new explicit user authorization.
