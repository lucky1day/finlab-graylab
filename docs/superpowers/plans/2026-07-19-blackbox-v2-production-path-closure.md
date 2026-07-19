# Blackbox V2 Production Path Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close `BBV2-01` through `BBV2-07` so one real Blackbox V2 delivery can pass the formal production path in an isolated database without changing the production trial from `shadow + paused`.

**Architecture:** Keep one Registry, lifecycle, result, API, and frontend model. Dispatch only runtime-specific version calculation, CLI execution, backtest construction, and lifecycle approval. Use canonical immutable Blackbox versions, MySQL-local transactions, atomic config writes, a durable operation journal, and fail-closed reconciliation.

**Tech Stack:** Python 3.12, unittest/pytest, SQLAlchemy, MySQL 8.0, APScheduler, FastAPI, macOS `sandbox-exec`, existing Harness and DataBridge snapshot modules.

---

## File Map

New modules:

- `shared/blackbox_v2/versioning.py`: canonical Blackbox platform-config hashing.
- `shared/blackbox_v2/history.py`: unique historical Request construction and actual-label matching.
- `shared/blackbox_v2/lifecycle.py`: lifecycle states, operation journal, atomic config update, and reconciliation.
- `backtests/blackbox_v2.py`: convert Results and labels to standard backtest output.
- `harness/blackbox_v2/activation.py`: signed Blackbox activation workflow.
- `tests/test_blackbox_v2_history.py`
- `tests/test_blackbox_v2_lifecycle.py`
- `tests/test_blackbox_v2_backtest_persistence.py`
- `tests/test_blackbox_v2_activation.py`

Existing modules:

- `scheduler/discovery.py`: canonical version source.
- `shared/blackbox_v2/contracts.py`: carrier-specific Result parsing.
- `scheduler/main.py`: structured result and exit code propagation.
- `scheduler/blackbox_v2_runner.py`: read allowlist and minimal environment.
- `deploy/blackbox_v2/runtime_profile_v1.json`: runtime security policy.
- `scheduler/repository.py`: connection-aware lifecycle and approval operations.
- `scheduler/executor.py`: exact approved-version execution hard-stop.
- `harness/authorization.py`: expose signing availability.
- `harness/blackbox_v2/gates.py`: delegate persistence and lifecycle work.
- `harness/gates/activate_gate.py`: runtime activation dispatch.
- `harness/cli.py`: Blackbox activation and reconciliation CLI wiring.
- `backtests/repository.py`: atomic backtest persistence.
- Existing focused tests under `tests/`.
- Production-readiness and dated audit records under `docs/blackbox_v2/`.

No migration is planned. Existing `approved_by` and `approved_at` columns and current Registry, Harness, run, and backtest tables are sufficient.

### Task 1: Canonical Blackbox Version Identity (BBV2-02)

**Files:**
- Create: `shared/blackbox_v2/versioning.py`
- Modify: `scheduler/discovery.py`
- Test: `tests/test_blackbox_v2_discovery.py`

- [ ] **Step 1: Write failing lifecycle-invariant tests**

Add tests that load one copied Blackbox scheme, change only `status` and `version_status`, and assert `scheme_version` and `config_hash` stay equal. Add separate tests proving a changed timeout, script byte, or Metadata byte changes the version. Keep a Native fixture assertion proving its current raw-config behavior is unchanged.

Core test:

```python
def test_blackbox_version_ignores_lifecycle_fields(self) -> None:
    first = load_scheme_config(config_path)
    text = config_path.read_text(encoding="utf-8")
    text = text.replace("status: paused", "status: active")
    text = text.replace("version_status: draft", "version_status: active")
    config_path.write_text(text, encoding="utf-8")
    second = load_scheme_config(config_path)

    self.assertEqual(first.config_hash, second.config_hash)
    self.assertEqual(first.scheme_version, second.scheme_version)
```

- [ ] **Step 2: Verify RED**

Run:

```bash
python -m pytest tests/test_blackbox_v2_discovery.py -q
```

Expected: lifecycle-invariant assertions fail because raw `config.yaml` bytes currently participate in the hash.

- [ ] **Step 3: Implement canonical hashing**

Create a focused helper with this public API:

```python
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

FUNCTIONAL_FIELDS = (
    "runtime_type",
    "input_source",
    "runtime_profile",
    "data_schema_version",
    "schedule",
    "delivery",
)

def canonical_platform_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {field: raw[field] for field in FUNCTIONAL_FIELDS}

def compute_blackbox_config_hash(raw: Mapping[str, Any]) -> str:
    payload = json.dumps(
        canonical_platform_config(raw),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
```

Use `compute_blackbox_config_hash(raw)` only inside `_load_blackbox_config()`. Continue deriving the version from script hash, canonical config hash, and Metadata hash. Do not alter Native code.

- [ ] **Step 4: Verify GREEN**

```bash
python -m pytest tests/test_blackbox_v2_discovery.py tests/test_scheduler_discovery.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add shared/blackbox_v2/versioning.py scheduler/discovery.py tests/test_blackbox_v2_discovery.py
git commit -m "fix: stabilize blackbox scheme version identity"
```

### Task 2: Strict Result Parsing by Carrier (BBV2-05)

**Files:**
- Modify: `shared/blackbox_v2/contracts.py`
- Test: `tests/test_blackbox_v2_contracts.py`

- [ ] **Step 1: Write failing strict-type tests**

Add JSON negative cases for `"1"`, `1.0`, `true`, `null`, and `2`. Add CSV negative cases for `1.0`, `+1`, leading/trailing whitespace, empty value, and `2`. Positive cases remain JSON integer and exact CSV token `-1/0/1`.

```python
def test_prediction_json_rejects_string_direction(self) -> None:
    payload = dict(valid_result)
    payload["predicted_direction"] = "1"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with self.assertRaisesRegex(ValueError, "JSON integer"):
        load_prediction_result(path, request)
```

- [ ] **Step 2: Verify RED**

```bash
python -m pytest tests/test_blackbox_v2_contracts.py -q
```

Expected: at least the JSON string and float cases fail.

- [ ] **Step 3: Implement explicit parsers**

Add these exact helpers and route JSON/CSV loaders separately:

```python
def _json_direction(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in {-1, 0, 1}:
        raise ValueError("predicted_direction must be a JSON integer -1, 0, or 1")
    return value

def _csv_direction(value: Any) -> int:
    if not isinstance(value, str) or value not in {"-1", "0", "1"}:
        raise ValueError("predicted_direction CSV token must be -1, 0, or 1")
    return int(value)
```

Preserve all Request echo, field-set, duplicate, and ordering checks.

- [ ] **Step 4: Verify GREEN and commit**

```bash
python -m pytest tests/test_blackbox_v2_contracts.py tests/test_blackbox_v2_runner.py -q
git add shared/blackbox_v2/contracts.py tests/test_blackbox_v2_contracts.py
git commit -m "fix: enforce blackbox result carrier types"
```

### Task 3: Scheduler Failure Exit Semantics (BBV2-06)

**Files:**
- Modify: `scheduler/main.py`
- Test: `tests/test_scheduler_main.py`

- [ ] **Step 1: Write failing return/exit tests**

Use real `SchemeRunResult` instances. Cover single success, failed, partial, expected skipped, unknown scheme, and mixed batch.

```python
def test_main_returns_one_for_failed_run_once_prediction(self) -> None:
    failed = SchemeRunResult("demo", "failed", 0, 0.1, "stale generation")
    with patch.object(scheduler_main, "run_prediction_job", return_value=failed):
        code = scheduler_main.main(
            ["--run-once", "predictions", "--scheme-id", "demo", "--force"]
        )
    self.assertEqual(code, 1)
```

- [ ] **Step 2: Verify RED**

```bash
python -m pytest tests/test_scheduler_main.py -q
```

Expected: `run_prediction_job()` returns `None` and `main()` has no exit-code contract.

- [ ] **Step 3: Implement structured propagation**

Apply these signatures:

```python
def run_prediction_job(
    scheme_id: str,
    run_date: str | date | None = None,
    algo_env: str = DEFAULT_ALGO_ENV,
    force: bool = False,
) -> SchemeRunResult:
    predict_date = _normalize_run_date(run_date)
    schemes = discover_schemes()
    _sync_registry(schemes)
    cfg = next((item for item in schemes if item.scheme_id == scheme_id), None)
    if cfg is None:
        return SchemeRunResult(scheme_id, "failed", 0, 0.0, "scheme not found")
    if not force and _skips_non_trading_day(cfg) and not _is_trading_day(predict_date):
        return SchemeRunResult(scheme_id, "skipped", 0, 0.0, "non-trading day")
    with _prediction_slot():
        return execute_scheme(cfg, predict_date, algo_env=algo_env)

def run_all_prediction_jobs(
    run_date: str | date | None = None,
    algo_env: str = DEFAULT_ALGO_ENV,
    force: bool = False,
) -> list[SchemeRunResult]:
    configs = [cfg for cfg in discover_schemes() if cfg.status == "active"]
    return [
        run_prediction_job(cfg.scheme_id, run_date=run_date, algo_env=algo_env, force=force)
        for cfg in configs
    ]

def _prediction_exit_code(results: Sequence[SchemeRunResult]) -> int:
    return 1 if any(item.status in {"failed", "partial"} for item in results) else 0
```

Keep discovery and execution on these existing paths; do not introduce a second scheme loader. At the CLI boundary, inspect the failed result reason so an unknown explicit scheme maps to exit code 2. Success and expected calendar/status skips map to 0.

Add an APScheduler wrapper that raises when the returned status is failed or partial, and register that wrapper in `build_scheduler()`. Change `main(argv=None) -> int` and module entry to `raise SystemExit(main())`.

- [ ] **Step 4: Verify GREEN and commit**

```bash
python -m pytest tests/test_scheduler_main.py tests/test_executor_run_id.py -q
git add scheduler/main.py tests/test_scheduler_main.py
git commit -m "fix: propagate scheduler prediction failures"
```

### Task 4: Sandbox Read and Environment Allowlist (BBV2-04)

**Files:**
- Modify: `scheduler/blackbox_v2_runner.py`
- Modify: `deploy/blackbox_v2/runtime_profile_v1.json`
- Test: `tests/test_blackbox_v2_runner.py`
- Test: `tests/test_blackbox_v2_runtime_profile.py`

- [ ] **Step 1: Write failing policy and environment tests**

Assert the generated policy does not contain an unrestricted read clause and the environment excludes secrets:

```python
def test_runtime_environment_does_not_inherit_parent_secrets(self) -> None:
    with patch.dict(
        os.environ,
        {
            "BOND_DB_PASSWORD": "db-secret",
            "DATABRIDGE_API_PASSWORD": "bridge-secret",
            "HARNESS_AUTH_SECRET": "auth-secret",
        },
    ):
        env = _runtime_environment(RuntimeProfile.for_tests(), Path("/tmp/blackbox-run"))
    self.assertNotIn("BOND_DB_PASSWORD", env)
    self.assertNotIn("DATABRIDGE_API_PASSWORD", env)
    self.assertNotIn("HARNESS_AUTH_SECRET", env)
```

On macOS, add real process probes for allowed CSV read, `/etc/hosts`, an external temporary secret, inherited `BLACKBOX_TEST_SECRET`, network, and data-dir write.

- [ ] **Step 2: Verify RED**

```bash
python -m pytest tests/test_blackbox_v2_runner.py tests/test_blackbox_v2_runtime_profile.py -q
```

Expected: unrestricted read and inherited-secret tests fail.

- [ ] **Step 3: Extend RuntimeProfile**

Add immutable fields:

```python
read_roots: Sequence[str] = (
    "/System/Library",
    "/usr/lib",
    "/opt/homebrew/opt/libomp",
)
environment_allowlist: Sequence[str] = ("LANG", "LC_ALL", "TZ")
environment_defaults: Sequence[tuple[str, str]] = (
    ("LANG", "C.UTF-8"),
    ("TZ", "Asia/Shanghai"),
)
```

Mirror these fields in `runtime_profile_v1.json` and assert exact equality in the profile test.

- [ ] **Step 4: Generate a path-specific policy and minimal environment**

Resolve and allow only:

- selected Python executable prefix;
- profile system read roots;
- current delivery script;
- current Request file;
- current three-file data directory;
- current writable run/output directory.

Delete `(allow file-read*)`. Build the subprocess environment from an empty dictionary, profile defaults, explicitly allowlisted parent variables, Python isolation variables, thread controls, and run-local `HOME/TMPDIR/cache` paths. Reject symlink escape for delivery, Request, data, and output paths.

- [ ] **Step 5: Verify real runtime and commit**

```bash
python -m pytest tests/test_blackbox_v2_runner.py tests/test_blackbox_v2_runtime_profile.py -q
```

Run the real trial predict and two-row backtest in `forecast_env_blackbox_v1`. Expected: LightGBM succeeds; external reads, inherited secret, network, and data-dir writes fail.

```bash
git add scheduler/blackbox_v2_runner.py deploy/blackbox_v2/runtime_profile_v1.json tests/test_blackbox_v2_runner.py tests/test_blackbox_v2_runtime_profile.py
git commit -m "fix: isolate blackbox filesystem and environment"
```

### Task 5: Approved Version and Fail-Closed Registry Sync (BBV2-03 foundation)

**Files:**
- Modify: `scheduler/repository.py`
- Modify: `scheduler/executor.py`
- Test: `tests/test_repository_registry.py`
- Test: `tests/test_executor_run_id.py`

- [ ] **Step 1: Write failing Registry and executor tests**

Prove that generic discovery/sync cannot activate an unknown Blackbox version even when config says active, while Native active sync remains unchanged. Vary exact Blackbox version status and approval fields; only `active + approved_by + approved_at` may reach the runner.

```python
def test_blackbox_shadow_version_cannot_execute(self) -> None:
    approval = BlackboxExecutionApproval(
        executable=False,
        reason="version status is shadow",
        version_status="shadow",
        approved_by=None,
        approved_at=None,
    )
    with patch("scheduler.executor.read_blackbox_execution_approval", return_value=approval):
        result = execute_scheme(active_blackbox_cfg, "2026-07-20", algo_env="test")
    self.assertEqual(result.status, "failed")
    self.assertEqual(result.records_written, 0)
```

- [ ] **Step 2: Verify RED**

```bash
python -m pytest tests/test_repository_registry.py tests/test_executor_run_id.py -q
```

Expected: generic sync trusts config lifecycle state and executor accepts shadow status.

- [ ] **Step 3: Add connection-aware repository primitives**

Introduce private connection-based functions `_upsert_scheme_version_conn(conn, cfg, *, trusted_status, approved_by, approved_at) -> str` and `_sync_scheme_registry_conn(conn, schemes, *, effective_statuses) -> None`, then make the existing public functions open a transaction and delegate to them.

Implement the bodies by moving the current SQL and parameter construction unchanged into the connection-scoped functions. Public generic sync applies these Blackbox rules:

- unknown exact version is inserted as draft;
- existing approved version is never downgraded;
- Registry is active only when the exact DB version is active and approved;
- Native behavior remains unchanged.

Add `BlackboxExecutionApproval` and `read_blackbox_execution_approval(engine, cfg)`, validating exact version, runtime type, Registry identity/status, version status, and non-null approval fields.

- [ ] **Step 4: Enforce exact approval in executor**

For Blackbox only:

```python
approval = read_blackbox_execution_approval(engine, cfg)
if not approval.executable:
    raise RuntimeError(
        f"Blackbox V2 version is not production-approved: {approval.reason}"
    )
```

Keep Native verification unchanged.

- [ ] **Step 5: Verify GREEN and commit**

```bash
python -m pytest tests/test_repository_registry.py tests/test_executor_run_id.py tests/test_scheduler_discovery.py -q
git add scheduler/repository.py scheduler/executor.py tests/test_repository_registry.py tests/test_executor_run_id.py
git commit -m "fix: require approved blackbox versions for execution"
```

### Task 6: Recoverable Shadow and Formal Activation (BBV2-03, BBV2-07)

**Files:**
- Create: `shared/blackbox_v2/lifecycle.py`
- Create: `harness/blackbox_v2/activation.py`
- Create: `tests/test_blackbox_v2_lifecycle.py`
- Create: `tests/test_blackbox_v2_activation.py`
- Modify: `scheduler/repository.py`
- Modify: `harness/authorization.py`
- Modify: `harness/blackbox_v2/gates.py`
- Modify: `harness/gates/activate_gate.py`
- Modify: `harness/cli.py`
- Modify: `tests/test_blackbox_v2_harness_gates.py`
- Modify: `tests/test_cli_activate.py`

- [ ] **Step 1: Write failing lifecycle journal tests**

Define the required data types in the tests:

```python
previous = LifecycleState(
    config_status="paused",
    version_status="shadow",
    registry_status="paused",
)
target = LifecycleState(
    config_status="active",
    version_status="active",
    registry_status="active",
)
journal = LifecycleJournal.prepare(
    action="activate",
    scheme_id="trial",
    scheme_version="abc123",
    harness_run_id="hr_1",
    previous=previous,
    target=target,
    token_hash="token-sha256",
)
```

Assert atomic round-trip, allowed phase transitions, token hash persistence, absence of raw token/secret, and rejection of invalid transitions.

- [ ] **Step 2: Write failing failure-injection tests**

Inject failures after config write, DB commit, and independent verification. Shadow failure must restore its exact prior paused draft/validated state. Activation failure must restore `paused + shadow + paused`. An unresolved journal must block shadow, activation, live, and scheduler execution.

- [ ] **Step 3: Verify RED**

```bash
python -m pytest tests/test_blackbox_v2_lifecycle.py tests/test_blackbox_v2_activation.py tests/test_blackbox_v2_harness_gates.py tests/test_cli_activate.py -q
```

Expected: lifecycle service and dedicated activation APIs do not exist.

- [ ] **Step 4: Implement journal and reconciliation**

Create these immutable structures:

```python
@dataclass(frozen=True)
class LifecycleState:
    config_status: str
    version_status: str
    registry_status: str

@dataclass(frozen=True)
class LifecycleJournal:
    operation_id: str
    action: str
    scheme_id: str
    scheme_version: str
    harness_run_id: str
    previous: LifecycleState
    target: LifecycleState
    phase: str
    token_hash: str
    error: str | None
```

Journal phases are `prepared -> config_written -> db_committed -> verified`, with `compensated` and `unresolved` terminal failure phases. Persist under `backtest_artifacts/blackbox_v2_lifecycle/{scheme_id}/` using temp file, flush, fsync, and `os.replace()`. Atomic config writes use the same durability sequence.

Reconciliation reads actual config/version/Registry state and converges only to the recorded previous safe state. It never promotes an incomplete operation to active.

- [ ] **Step 5: Implement one-transaction lifecycle database update**

Add:

```python
def apply_blackbox_lifecycle_state(
    engine: Engine,
    cfg: SchemeConfig,
    *,
    version_status: str,
    registry_status: str,
    approved_by: str | None = None,
    approved_at: datetime | None = None,
) -> None:
    with engine.begin() as conn:
        _upsert_scheme_version_conn(
            conn,
            cfg,
            trusted_status=version_status,
            approved_by=approved_by,
            approved_at=approved_at,
        )
        _sync_scheme_registry_conn(
            conn,
            [cfg],
            effective_statuses={
                registry_scheme_id(cfg.scheme_id, cfg.horizon, cfg.tenors[0]): registry_status
            },
        )
```

Before returning, query both rows through the same connection and raise on mismatch so the transaction rolls back.

- [ ] **Step 6: Implement signed Blackbox activation**

Expose `authorization_signing_enabled()` from `harness.authorization`. Blackbox activation requires it to be true and verifies action `blackbox_activate`, exact version, exact latest passed all-stage run, expiry, and issuer. It writes a prepared journal, writes/consumes authorization audit, atomically updates config, commits DB lifecycle state, independently verifies all three stores, then marks the journal verified.

Activation target:

```python
LifecycleState(
    config_status="active",
    version_status="active",
    registry_status="active",
)
```

Set `approved_by=auth.issued_by` and `approved_at` to current UTC.

- [ ] **Step 7: Delegate Shadow and Activation**

Replace direct Blackbox shadow writes in `gates.py` with the lifecycle service. Make `ActivationGate` load the runtime and dispatch Blackbox to `harness.blackbox_v2.activation` while preserving the Native path. Add `harness gate lifecycle-reconcile`; it only reconciles to the safe previous state and prints before/after evidence.

- [ ] **Step 8: Verify GREEN and commit**

```bash
python -m pytest tests/test_blackbox_v2_lifecycle.py tests/test_blackbox_v2_activation.py tests/test_blackbox_v2_harness_gates.py tests/test_cli_activate.py tests/test_authorization.py -q
git add shared/blackbox_v2/lifecycle.py harness/blackbox_v2/activation.py scheduler/repository.py harness/authorization.py harness/blackbox_v2/gates.py harness/gates/activate_gate.py harness/cli.py tests/test_blackbox_v2_lifecycle.py tests/test_blackbox_v2_activation.py tests/test_blackbox_v2_harness_gates.py tests/test_cli_activate.py
git commit -m "fix: make blackbox lifecycle recoverable and approved"
```

### Task 7: Historical Requests and Atomic Backtest Persistence (BBV2-01)

**Files:**
- Create: `shared/blackbox_v2/history.py`
- Create: `backtests/blackbox_v2.py`
- Create: `tests/test_blackbox_v2_history.py`
- Create: `tests/test_blackbox_v2_backtest_persistence.py`
- Modify: `backtests/repository.py`
- Modify: `harness/blackbox_v2/gates.py`
- Modify: `tests/test_blackbox_v2_harness_gates.py`

- [ ] **Step 1: Write failing historical Request tests**

Use small synthetic calendars, snapshot keys, and actual rows for all five tasks. Assert unique Request ids/predict dates, exact target date semantics, three cutoff keys present in the same snapshot, and labels matching the platform actual builders. Missing actual, duplicate actual, missing cutoff, and fewer-than-requested cases fail instead of duplicating dates.

Public type:

```python
@dataclass(frozen=True)
class HistoricalCase:
    request: BlackboxRequest
    label: int
    actual_extra: dict[str, Any]
```

- [ ] **Step 2: Write failing atomic persistence tests**

Use an isolated SQLAlchemy test database. Positive case asserts one successful run, 100 predictions, and expected monthly metrics. Inject a duplicate-key failure after run creation and assert all related table counts are identical to before.

- [ ] **Step 3: Verify RED**

```bash
python -m pytest tests/test_blackbox_v2_history.py tests/test_blackbox_v2_backtest_persistence.py tests/test_blackbox_v2_harness_gates.py -q
```

Expected: history and atomic persistence APIs do not exist; current Gate always reports `persist=false`.

- [ ] **Step 4: Implement historical case construction**

Implement `build_historical_cases(metadata, snapshot, engine, *, limit) -> list[HistoricalCase]`. Dispatch over exactly the five fixed task types and reject any other value. Use existing platform calendar and actual builders for T+1, T+5, weekly point, weekly average, and monthly. Match actual by target tenor, target rule, predict date, feature date, and target date. Sort by target date, take the latest limit, and require exact limit, unique ids/dates, and existing daily/weekly/monthly cutoff keys.

- [ ] **Step 5: Convert to standard backtest output**

`backtests/blackbox_v2.py` runs the unchanged delivery and builds rows with benchmark id, base scheme id, tenor, horizon, all three dates, label, direction, Request id, runtime type, target rule, scheme version, generation, and snapshot id. Use existing shared metric helpers to build summary and monthly metrics.

- [ ] **Step 6: Add atomic repository API**

Move current run/prediction/metric SQL into connection-scoped helpers and add:

```python
def persist_backtest_output_atomic(
    engine: Engine,
    output: RunOutput,
    *,
    benchmark_id: str,
) -> int:
    with engine.begin() as conn:
        run_id = _create_backtest_run_conn(conn, output, benchmark_id)
        _replace_backtest_predictions_conn(conn, run_id, output.rows)
        _replace_monthly_metrics_conn(conn, run_id, output.monthly_metrics)
        _update_backtest_run_summary_conn(conn, run_id, "success", output.summary)
        return run_id
```

Keep existing Native repository functions as wrappers around their prior transaction boundaries so their behavior does not change.

- [ ] **Step 7: Wire persist behavior into BlackboxBacktestGate**

No-persist keeps the current 100-row contract stress test. Persist mode must:

1. verify a `backtest_persist` token bound to scheme version and latest passed all-stage run;
2. build 100 unique historical cases;
3. run Blackbox backtest;
4. atomically persist;
5. independently query run/prediction/metric deltas;
6. fail unless run delta is 1 and prediction delta is 100.

Set evidence `persist` from `ctx.persist_backtest`; a zero-write persist cannot pass.

- [ ] **Step 8: Verify GREEN and commit**

```bash
python -m pytest tests/test_blackbox_v2_history.py tests/test_blackbox_v2_backtest_persistence.py tests/test_blackbox_v2_harness_gates.py tests/test_backtest_gate.py tests/test_base_runner.py tests/test_backtest_factor_lab_readonly.py -q
git add shared/blackbox_v2/history.py backtests/blackbox_v2.py backtests/repository.py harness/blackbox_v2/gates.py tests/test_blackbox_v2_history.py tests/test_blackbox_v2_backtest_persistence.py tests/test_blackbox_v2_harness_gates.py
git commit -m "feat: persist blackbox backtests atomically"
```

### Task 8: Regression, Isolated Certification, and Evidence

**Files:**
- Modify: `docs/blackbox_v2/PRODUCTION_READINESS.md`
- Modify: `docs/blackbox_v2/records/FULL_PIPELINE_STABILITY_AUDIT_20260719.md`
- Modify: `docs/blackbox_v2/records/FULL_PIPELINE_STABILITY_AUDIT_20260719.evidence.json`
- Test: all relevant `tests/` modules

- [ ] **Step 1: Run targeted regression**

```bash
python -m pytest \
  tests/test_blackbox_v2_contracts.py \
  tests/test_blackbox_v2_discovery.py \
  tests/test_blackbox_v2_requests.py \
  tests/test_blackbox_v2_runner.py \
  tests/test_blackbox_v2_runtime_profile.py \
  tests/test_blackbox_v2_harness_dispatch.py \
  tests/test_blackbox_v2_harness_gates.py \
  tests/test_blackbox_v2_lifecycle.py \
  tests/test_blackbox_v2_activation.py \
  tests/test_blackbox_v2_history.py \
  tests/test_blackbox_v2_backtest_persistence.py \
  tests/test_repository_registry.py \
  tests/test_executor_run_id.py \
  tests/test_scheduler_main.py \
  tests/test_backend_api.py \
  tests/test_backtest_factor_lab_readonly.py -q
```

Expected: zero failures.

- [ ] **Step 2: Capture production invariants**

Record production trial config SHA256 and read-only counts/status for Registry, versions, runs, predictions, and backtests. Save only non-secret summaries. Expected baseline: `shadow + paused` and zero Blackbox business rows.

- [ ] **Step 3: Prepare isolated certification environment**

Create a dedicated MySQL Schema, run migrations 001-016, expose production source tables as read-only views, and keep all Registry/Harness/business tables local. Use independent credentials that cannot write production business tables.

- [ ] **Step 4: Execute formal Intake through Shadow**

Run unchanged two-file Intake, then ten all-stage runs and signed shadow registration. Expected: 10/10 passed, one canonical version, no business writes, no temporary snapshot residue.

- [ ] **Step 5: Execute formal ActivationGate**

Set an ephemeral `HARNESS_AUTH_SECRET`, issue short-lived `blackbox_activate` authorization bound to exact scheme version and all-stage run, and execute the public Activation CLI. Direct SQL status changes and `FORCED_ACTIVE_TEST_ONLY` evidence are forbidden.

Expected: isolated config, version, and Registry all active; approval fields populated.

- [ ] **Step 6: Certify backtest persistence**

Run 100/500/1000 no-persist invariance tests, then signed 100-row persist. Verify one run, 100 unique predictions, metrics, API response, and frontend backtest display.

- [ ] **Step 7: Certify live through frontend**

Run one authorized `gray_live` and one scheduler `scheduled_live` date with known actuals. Require exactly two runs and two predictions, run actual updater, compare SQL with API field by field, and capture a browser screenshot without console errors.

- [ ] **Step 8: Execute failure/recovery matrix**

Inject stale generation, missing CSV, timeout, illegal Result, network access, data-dir write, `/etc/hosts` read, external secret read, inherited environment secret, config-write failure, DB-commit failure, and verification failure. Require zero partial business rows and safe lifecycle reconciliation. Resolve any injected journal and verify no runtime residue.

- [ ] **Step 9: Re-check production invariants**

Repeat production read-only checks. Config SHA256, Registry/version state, and business counts must match Step 2 exactly. Any mismatch blocks certification.

- [ ] **Step 10: Update dated evidence**

Append a post-fix section without rewriting the original audit. Mark only evidenced items as PASS. Keep `BBV2-08` open and report:

```text
SHADOW_READY: PASS
PRODUCTION_PATH_READY: PASS
PRODUCTION_READY: NOT_CERTIFIED (BBV2-08 open)
```

- [ ] **Step 11: Run final verification**

```bash
python -m pytest tests -q
git diff --check
git status --short
```

Expected: all tests pass; no generated data, reports, secrets, `outputs/`, or unrelated launchd file is staged.

- [ ] **Step 12: Commit certification evidence**

```bash
git add docs/blackbox_v2/PRODUCTION_READINESS.md docs/blackbox_v2/records/FULL_PIPELINE_STABILITY_AUDIT_20260719.md docs/blackbox_v2/records/FULL_PIPELINE_STABILITY_AUDIT_20260719.evidence.json
git commit -m "docs: certify blackbox v2 production path"
```

## Self-Review

- Tasks 1-7 map directly to `BBV2-01` through `BBV2-07`.
- Task 8 performs the isolated certification and keeps `BBV2-08` open.
- Every production-path write is directed at the isolated Schema.
- Production trial invariants are checked before and after.
- Native V1 version, activation, scheduler, and persistence behavior receive regression coverage.
- Existing upstream `.py` and `.json` remain unchanged.
- No database migration or second Registry is introduced.
