# Native Post-Admission Validation and G4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement mutually exclusive Native activation profiles: current exact version full `all` activates as `full_initial_onboarding_v1` without maintenance/prior-snapshot requirements; a revision with a matching persisted prior StaticGate business-identity snapshot may activate as `native_post_admission_revision_v1` without re-running changed historical source benchmarks. Under the 2026-08-04 explicit authorization, the one legacy 10Y prior run whose StaticGate lacks only the new snapshot field may instead receive one evidence-bound control-plane attestation; it still must pass the six maintenance Gates, activation, and the existing exact G4 repair chain.

**Architecture:** Keep the existing seven-Gate `all` sequence immutable for first technical admission. If the current exact version passes it, ActivationGate returns `full_initial_onboarding_v1` immediately and does not require `native-maintenance`, a prior version, or a prior snapshot. The Native-only `native-maintenance` alternative revalidates an older fully admitted Native version, a prior StaticGate identity source, and an exact expected Registry identity. The business identity contains only scheme/runtime/horizon/task/frequency/tenors/composite IDs, never current code/config/version hashes. For the single authorized G4 scope only, a dedicated `legacy-native-admission-attestation` stage/gate records an operator declaration in existing Harness tables, bound to prior version/run and canonical business identity. It is not in `all`, `native-maintenance`, or ordinary Gate dispatch; it is not a generic waiver, never modifies historical evidence, and never writes business tables. The current exact `t_scheme_versions` candidate must be `runtime_type='native_adapter'` and `status in {'draft','active'}`; its expected Registry is uniformly `paused` before activation or uniformly `active` after it, and `draft` plus `active` Registry fails closed. ActivationGate alone atomically establishes active state. The maintenance route does not invoke `compare` or `backtest`, but it persists the six current Gate results for activation audit. At activation, the resolver order is full admission → current six-Gate maintenance evidence → prior-admission verification; the prior verification happens immediately before the token-consumption path.

**Tech Stack:** Python 3.12, FastAPI harness modules, SQLAlchemy/MySQL control-plane tables, unittest/pytest, existing Native V1 and signal-gap contracts.

---

## File map

- Create: `harness/gates/native_maintenance_admission_gate.py` — read-only eligibility and Registry-identity evidence for the post-admission route.
- Modify: `harness/registry.py` — expose the Native-only stage and dispatch its admission Gate without changing `AUTO_SEQUENCE`.
- Modify: `harness/gates/activate_gate.py` — resolve the full-admission or Native-maintenance validation profile before authorization consumption and write profile evidence into the activation result.
- Modify: `harness/gates/static_gate.py` — persist the canonical Native business-identity snapshot in the prior full-admission StaticGate evidence.
- Modify: `tests/test_harness_static_gate.py` — pin canonical stage ordering and reject Blackbox use of the Native-only stage.
- Create: `tests/test_native_maintenance_admission.py` — SQLite-backed admission and Registry-drift regression coverage.
- Modify: `tests/test_activation_gate.py` — pin exact run/profile rules for both activation routes.
- Modify: `tests/test_harness_persistence.py` — pin that `--check-only` remains limited to the seven-Gate `all` sequence.
- Modify: `AGENTS.md` and `CLAUDE.md` identically — define benchmark scope as first technical admission only while retaining all non-benchmark safety gates.
- Modify: `docs/architecture/HARNESS_ARCHITECTURE.md`, `docs/architecture/SOURCE_ALGORITHM_FIDELITY.md`, `docs/architecture/SCHEME_CONTRACT.md` — define the two validation paths precisely.
- Modify: `docs/sop/NATIVE_V1_MAINTENANCE_SOP.md`, `docs/sop/NATIVE_V1_POST_CHANGE_TEST_SOP.md` — route already admitted Native revisions through `native-maintenance`.
- Modify: `docs/records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md`, `docs/CURRENT_STATUS.md`, `docs/TODO.md`, `docs/architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md`, `docs/records/SCHEME_ISSUE_LEDGER.md` — record the accepted post-admission policy and the remaining single G4 key.

### Task 1: Define and prove the Native post-admission admission Gate

**Files:**
- Create: `tests/test_native_maintenance_admission.py`
- Modify: `tests/test_harness_static_gate.py`
- Modify: `tests/test_harness_persistence.py`
- Create: `harness/gates/native_maintenance_admission_gate.py`
- Modify: `harness/registry.py`

- [x] **Step 1: Write the failing stage and admission tests.**

Create an SQLite fixture with these four control-plane tables and exactly one active 10Y Registry row:

```python
conn.execute(text("CREATE TABLE t_scheme_versions (scheme_id TEXT, scheme_version TEXT, runtime_type TEXT, status TEXT, approved_at TEXT)"))
conn.execute(text("CREATE TABLE t_harness_runs (harness_run_id TEXT, scheme_id TEXT, scheme_version TEXT, stage TEXT, status TEXT, finished_at TEXT)"))
conn.execute(text("CREATE TABLE t_harness_gate_results (harness_run_id TEXT, gate_name TEXT, status TEXT, summary_json TEXT)"))
conn.execute(text("CREATE TABLE t_scheme_registry (scheme_id TEXT, base_scheme_id TEXT, name TEXT, description TEXT, horizon INTEGER, task_type TEXT, runtime_type TEXT, tenors TEXT, frequency TEXT, target_tenor TEXT, schedule_cron TEXT, schedule_timezone TEXT, status TEXT, deployed_at TEXT)"))
```

Write one passing-fixture expectation and one failure expectation per condition:

```python
result = NativeMaintenanceAdmissionGate().run(ctx)
assert result.passed is True
assert evidence_value(result, "prior_admitted_scheme_version") == "prior-native-version"
assert evidence_value(result, "registry_scheme_ids") == ["t5_daily__h5__10Y"]

assert NativeMaintenanceAdmissionGate().run(ctx_without_prior).status == GateStatus.BLOCKED
assert "prior" in " ".join(NativeMaintenanceAdmissionGate().run(ctx_without_prior).errors)
```

Cover: no different active Native version; a prior active version without an `all` run whose `compare` result is `passed`; missing, malformed, duplicate or non-matching prior StaticGate `native_business_identity` snapshot; candidate config status other than `active`; absent current exact `t_scheme_versions` row, or a row whose runtime is not `native_adapter` or whose status is neither `draft` nor `active`; Native ID absent from onboarding policy; a Registry row with wrong `task_type`, `frequency`, `horizon`, `target_tenor`, runtime, missing expected ID, extra ID, non-uniform lifecycle, or a `draft` candidate paired with active Registry. The snapshot is the `static.business_identity` evidence and contains only `scheme_id`、`runtime_type`、`horizon`、`task_type`、`frequency`、`tenors`、`registry_scheme_ids`; no code/config/version hash is legal. Also pin these sequence contracts:

```python
assert sequence_for_stage("all") == ["static", "input", "unit", "dry-run", "compare", "backtest", "api-readiness"]
assert sequence_for_stage("native-maintenance") == ["static", "native-maintenance-admission", "input", "unit", "dry-run", "api-readiness"]
with pytest.raises(ValueError, match="unsupported Blackbox V2 gate"):
    gates_for_stage("native-maintenance", ctx=blackbox_ctx)
with pytest.raises(ValueError, match="check-only"):
    onboard(ctx, stage="native-maintenance", check_only=True)
```

- [x] **Step 2: Run the new tests and verify the expected RED failure.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q tests/test_native_maintenance_admission.py tests/test_harness_static_gate.py tests/test_harness_persistence.py
```

Expected: the new imports/stage assertions fail because `NativeMaintenanceAdmissionGate` and `native-maintenance` do not exist; unrelated existing tests remain green.

- [x] **Step 3: Implement only the admission Gate and stage dispatch.**

In `harness/gates/native_maintenance_admission_gate.py`, provide these public values:

```python
NATIVE_MAINTENANCE_STAGE = "native-maintenance"
NATIVE_MAINTENANCE_SEQUENCE = (
    "static", "native-maintenance-admission", "input", "unit", "dry-run", "api-readiness",
)

@dataclass(frozen=True)
class NativeMaintenanceAdmission:
    prior_admitted_scheme_version: str
    prior_harness_run_id: str
    registry_scheme_ids: tuple[str, ...]

def verify_native_maintenance_admission(ctx: GateContext) -> tuple[NativeMaintenanceAdmission | None, list[str]]:
    cfg = ctx.config
    if cfg is None or cfg.runtime_type != "native_adapter" or cfg.status != "active":
        return None, ["native-maintenance requires an active native_adapter config"]
    policy_errors = validate_onboarding_policy(ctx.project_root, ctx.scheme_id, cfg.runtime_type)
    if policy_errors:
        return None, policy_errors
    expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
    current_business_identity = native_business_identity_snapshot(
        scheme_id=cfg.scheme_id,
        runtime_type=cfg.runtime_type,
        horizon=cfg.horizon,
        task_type=cfg.task_type,
        frequency=cfg.frequency,
        tenors=cfg.tenors,
    )
    engine = ctx.engine_factory() if ctx.engine_factory is not None else create_engine_from_env()
    owns_engine = ctx.engine_factory is None
    try:
        with engine.begin() as conn:
            prior = conn.execute(text("""
                SELECT versions.scheme_version, runs.harness_run_id
                FROM t_scheme_versions AS versions
                JOIN t_harness_runs AS runs
                  ON runs.scheme_id = versions.scheme_id
                 AND runs.scheme_version = versions.scheme_version
                JOIN t_harness_gate_results AS compare_result
                  ON compare_result.harness_run_id = runs.harness_run_id
                WHERE versions.scheme_id = :scheme_id
                  AND versions.scheme_version <> :scheme_version
                  AND versions.runtime_type = 'native_adapter'
                  AND versions.status = 'active'
                  AND runs.stage = 'all'
                  AND runs.status = 'passed'
                  AND compare_result.gate_name = 'compare'
                  AND compare_result.status = 'passed'
                ORDER BY runs.finished_at DESC, runs.harness_run_id DESC
                LIMIT 1
            """), {"scheme_id": cfg.scheme_id, "scheme_version": cfg.scheme_version}).mappings().one_or_none()
            if prior is None:
                return None, ["no prior active Native version with passed all+compare evidence"]
            prior_identity, identity_error = _read_prior_native_business_identity_snapshot_conn(
                conn, harness_run_id=str(prior["harness_run_id"])
            )
            if identity_error is not None:
                return None, [identity_error]
            if prior_identity != current_business_identity:
                return None, [
                    "prior admitted Native static gate identity snapshot does not match "
                    "current Native business identity"
                ]
            registry_rows = _read_scheme_registry_rows_conn(
                conn, cfg, expected_registry_ids, for_update=False,
            )
            registry_error = _registry_identity_error(
                cfg, expected_tenors, expected_registry_ids, registry_rows,
                expected_runtime_type="native_adapter",
            )
            if registry_error is not None:
                return None, [registry_error]
            return NativeMaintenanceAdmission(
                prior_admitted_scheme_version=str(prior["scheme_version"]),
                prior_harness_run_id=str(prior["harness_run_id"]),
                registry_scheme_ids=expected_registry_ids,
            ), []
    finally:
        if owns_engine and hasattr(engine, "dispose"):
            engine.dispose()
```

Use the current `ctx.config` only after confirming it is Native, `status == "active"`, and allowed by `validate_onboarding_policy`; then require its exact `t_scheme_versions` row to be `runtime_type='native_adapter'` with status `draft` or `active`. Validate the expected Registry IDs as uniformly `paused` before activation or uniformly `active` afterwards, rejecting a draft candidate paired with active Registry. Obtain an Engine from `ctx.engine_factory` when supplied, otherwise from the repository environment factory; dispose an owned Engine in `finally`. Read, but never write, `t_scheme_versions`, `t_harness_runs`, `t_harness_gate_results`, and `t_scheme_registry`.

The prior-admission SQL shown above deliberately requires one **different** `scheme_version` that is `runtime_type='native_adapter'`, `status='active'`, has a successful `stage='all'` run, and has `gate_name='compare' AND status='passed'` in that same run. After selecting that prior run, read its CompareGate evidence separately: it is valid only when there is exactly one `gate_name='compare'` result for that selected `harness_run_id` and its status is `passed`; zero, duplicate, skipped, or failed results block the maintenance route. The selected run's passed StaticGate must also carry exactly one canonical `native_business_identity` evidence value in `summary_json`; this is the persisted `static.business_identity` snapshot, not a value reconstructed from today's Registry or config. Reuse the repository’s exact composite Registry helpers with `for_update=False` and `expected_runtime_type="native_adapter"`; validate lifecycle as uniformly paused pre-activation or uniformly active post-activation, rather than hard-coding active Registry. Reject any mismatch rather than synchronizing it. Legacy runs with no usable snapshot are fail-closed; the current, only implemented recovery is a full `all`. `legacy admission identity attestation` is future-only: it would require separate design, implementation, and explicit authorization, and is not executable in this plan.

`NativeMaintenanceAdmissionGate.run()` must translate this verification into a `GateResult`: `PASSED` includes `validation_profile="native_post_admission_revision_v1"`, prior version/run, and a JSON-safe ordered Registry ID list; `BLOCKED` includes all prerequisite errors, including missing/malformed/mismatched legacy snapshots. Do not call a benchmark, a backtest runner, scheduler, repository write, or config mutation.

In `harness/registry.py`, leave `AUTO_SEQUENCE` byte-for-byte semantically unchanged. Map `stage == NATIVE_MAINTENANCE_STAGE` to the six-item sequence, and map `native-maintenance-admission` only for `runtime_type == "native_adapter"`; Blackbox dispatch must keep rejecting it.

- [x] **Step 4: Run the focused suite and verify GREEN.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q tests/test_native_maintenance_admission.py tests/test_harness_static_gate.py tests/test_harness_persistence.py
```

Expected: all focused tests pass, including the existing assertion that `all` remains seven Gates and `--check-only` cannot select the new stage.

### Task 2: Bind activation to either full admission or the new persisted profile

**Files:**
- Modify: `tests/test_activation_gate.py`
- Modify: `harness/gates/activate_gate.py`

- [x] **Step 1: Write failing activation-profile tests.**

Add SQLite-backed tests that exercise the real history resolver through a patched `_db_engine`:

```python
validation, errors = _resolve_native_activation_validation(ctx, "candidate-version")
assert errors == []
assert validation.validation_profile == "native_post_admission_revision_v1"
assert validation.validation_stage == "native-maintenance"
assert validation.validation_harness_run_id == "hr-maintenance"
assert validation.prior_admitted_scheme_version == "prior-native-version"
assert validation.benchmark_validation == "not_run_post_admission"
```

The fixture must contain all six `native-maintenance` Gate results for `candidate-version`. Add one rejection each for a missing maintenance Gate, an absent/rejected current admission check, a selected prior `all` run whose CompareGate evidence is zero, duplicate, skipped, or failed rather than exactly one passed result, and a missing/malformed/mismatched prior StaticGate business snapshot. Retain and run the existing full-`all` test; assert it yields `full_initial_onboarding_v1`, `validation_stage == "all"`, and `benchmark_validation == "passed_initial_admission"`.

Add an end-to-end `ActivationGate.run()` test with a valid token and patched `_sync_registry_after_activation` that asserts the successful `GateResult` carries the profile evidence. Assert the token is not consumed and registry sync is not called when the maintenance profile is incomplete.

- [x] **Step 2: Run the activation tests and verify the expected RED failure.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q tests/test_activation_gate.py tests/test_cli_activate.py
```

Expected: tests fail because the resolver/profile evidence and `native-maintenance` history path do not exist; existing `all` behavior remains the reference.

- [x] **Step 3: Implement the smallest profile resolver.**

Add an immutable `NativeActivationValidation` record and preserve the public compatibility of `_verify_gate_history(ctx, scheme_version) -> list[str]`. Implement a new resolver with this order:

```python
def _resolve_native_activation_validation(
    ctx: GateContext, scheme_version: str,
) -> tuple[NativeActivationValidation | None, list[str]]:
    full = _passed_full_all_validation(ctx, scheme_version)
    if full is not None:
        return full, []
    maintenance = _passed_maintenance_validation(ctx, scheme_version)
    if maintenance is None:
        return None, ["no passed native-maintenance harness run with all required gates"]
    admission, admission_errors = verify_native_maintenance_admission(ctx)
    if admission_errors:
        return None, admission_errors
    return NativeActivationValidation(
        validation_profile="native_post_admission_revision_v1",
        validation_harness_run_id=maintenance.harness_run_id,
        validation_stage="native-maintenance",
        prior_admitted_scheme_version=admission.prior_admitted_scheme_version,
        registry_scheme_ids=admission.registry_scheme_ids,
        benchmark_validation="not_run_post_admission",
    ), []
```

`_passed_full_all_validation` must retain the existing exact `stage='all'`, `status='passed'`, required seven-Gate behavior, including the benchmark-required rule that a skipped compare result is rejected. When it succeeds, the resolver must return `full_initial_onboarding_v1` immediately and must not call or require maintenance admission/snapshot validation. After the full path is absent, `_passed_maintenance_validation` must first require exactly the current `scheme_version`, `stage='native-maintenance'`, `status='passed'`, and all six names in `NATIVE_MAINTENANCE_SEQUENCE`; it must never treat `compare` or `backtest` as skipped success for that profile. Only then does `verify_native_maintenance_admission` run immediately before the token-consumption path. It must reject any selected prior run without a matching persisted `static.business_identity` snapshot or without exactly one passed CompareGate result; it may never reconstruct identity from current Registry/config.

Call the resolver after strict preflight and immediately before `mark_token_used`. Keep strict discovery, exact token/version binding, config-byte checks, config status behavior, atomic repository activation, and all existing error paths unchanged. Add these `Evidence` keys to both success and relevant history-failure results:

```python
Evidence("validation_profile", validation.validation_profile)
Evidence("validation_harness_run_id", validation.validation_harness_run_id)
Evidence("validation_stage", validation.validation_stage)
Evidence("prior_admitted_scheme_version", validation.prior_admitted_scheme_version)
Evidence("admission_registry_scheme_ids", list(validation.registry_scheme_ids))
Evidence("benchmark_validation", validation.benchmark_validation)
```

The full-admission profile must not claim post-admission behavior; the maintenance profile must say `benchmark_validation="not_run_post_admission"`.

- [x] **Step 4: Run focused verification and inspect the diff.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q tests/test_activation_gate.py tests/test_cli_activate.py tests/test_native_maintenance_admission.py
git diff --check
```

Expected: all three suites pass and the diff checker prints no diagnostics.

### Task 3: Update only current policy and G4 status documents

**Files:**
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `docs/architecture/HARNESS_ARCHITECTURE.md`
- Modify: `docs/architecture/SOURCE_ALGORITHM_FIDELITY.md`
- Modify: `docs/architecture/SCHEME_CONTRACT.md`
- Modify: `docs/sop/NATIVE_V1_MAINTENANCE_SOP.md`
- Modify: `docs/sop/NATIVE_V1_POST_CHANGE_TEST_SOP.md`
- Modify: `docs/records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md`
- Modify: `docs/CURRENT_STATUS.md`
- Modify: `docs/TODO.md`
- Modify: `docs/architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md`
- Modify: `docs/records/SCHEME_ISSUE_LEDGER.md`

- [x] **Step 1: Add one consistent policy statement to the root rules.**

In the Native source-fidelity paragraph, state that source benchmark/Native Compare evidence is mandatory for first technical admission. State that historical source-vintage drift for an already admitted, same-identity Native revision is an archive diagnostic only, not a standalone blocker for activation, gap repair, `gray_live`, `scheduled_live`, or API. Define "same identity" as a prior `all` StaticGate's persisted matching `static.business_identity` snapshot containing only scheme/runtime/horizon/task/frequency/tenors/composite IDs, never a code/config/version hash. Document the mutually exclusive profiles: current full `all` uses `full_initial_onboarding_v1` without snapshot/maintenance; maintenance requires the snapshot and six Gates. Legacy admission without it is fail-closed and currently needs a new full `all`; `legacy admission identity attestation` is future-only, requiring separate design, implementation, and explicit authorization before it could exist. No Gate may create or infer it. Explicitly retain static, input, calendar, cutoff, version, Registry, authorization, and live-safe-oracle requirements; state Blackbox Compare remains in force. Apply the exact same bytes to `AGENTS.md` and `CLAUDE.md`.

- [x] **Step 2: Describe the two routes without weakening initial admission.**

In architecture and SOP documents, define:

```text
first technical admission: static -> input -> unit -> dry-run -> compare -> backtest -> api-readiness
eligible Native post-admission revision: static -> native-maintenance-admission -> input -> unit -> dry-run -> api-readiness
```

Document the mutually exclusive full-`all` and maintenance profiles. For maintenance, document prior `all+compare=passed`, matching prior `static.business_identity`, current exact `t_scheme_versions` as native `draft|active`, the same expected Registry identity uniformly paused pre-activation or active post-activation, exact current version/run, persisted evidence, and fail-closed behavior; a draft candidate with active Registry must fail. Explicitly say a legacy snapshot cannot be retroactively inferred from current Registry/config and currently needs full `all`; attestation is future-only pending separate design, implementation, and explicit authorization. Do not describe the second route as a generic Compare waiver and do not alter Blackbox instructions.

- [x] **Step 3: Update G4 to the single authorized record.**

Remove the obsolete benchmark waiver/rebuild decision from current status documents. Record that 14/45 historical differences and three direction changes are archived diagnostics, that the legacy prior `static.business_identity` snapshot is absent, and that the G4 current exact candidate is `t_scheme_versions=native_adapter/draft` with uniformly paused expected Registry—a normal pre-activation state, not an extra blocker. Therefore the potential business write below is not ready: the only implemented recovery is current exact version full `all` (including current Compare) followed by `full_initial_onboarding_v1`; it has not passed. Attestation is future-only, not an available alternative:

```text
weekly_10y_d_overlay_0529 / 10Y / h6
predict_date=2026-08-01
feature_date=2026-07-31
target_date=2026-08-07
prediction_phase=gray_live
```

Set `ISSUE-20260731-007` to `ACCEPTED_RISK`; retain its observed facts and prohibit algorithm retuning, benchmark rewriting, or old-input fallback.

- [x] **Step 4: Verify documentation scope.**

Run:

```bash
cmp -s AGENTS.md CLAUDE.md
rg -n "scoped waiver|同源重建|用户选择" docs/CURRENT_STATUS.md docs/TODO.md docs/architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md docs/records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md || true
rg -n "T(O)DO|T(B)D|implement later|fill in details" docs/superpowers/specs/2026-08-04-post-admission-native-validation-design.md docs/superpowers/plans/2026-08-04-native-maintenance-g4.md || true
git diff --check
```

Expected: `cmp` exits zero; no current G4 document claims a legacy identity can be inferred from current state or that G4 is ready to write; no diff diagnostics occur. Do not edit `docs/records/status/KNOWN_ISSUES_HANDOFF_20260802.md` or either user diagnostic script.

### Task 4: Verify, commit the implementation governance, then keep G4 blocked on its current full-`all` path

**Files:**
- Modify after successful DB/API readback only: `docs/records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md`, `docs/CURRENT_STATUS.md`, `docs/TODO.md`, `docs/records/SCHEME_ISSUE_LEDGER.md`

- [x] **Step 1: Run complete pre-commit verification.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q tests/test_native_maintenance_admission.py tests/test_activation_gate.py tests/test_cli_activate.py tests/test_harness_static_gate.py tests/test_harness_persistence.py
PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q tests/test_signal_gap_plan.py
git diff --check
git status --short
```

Expected: all named suites pass, the diff checker is silent, and the only staged candidates are the file-map paths plus the approved design/plan. Preserve the existing modified handoff document and untracked `_diag_*` scripts.

- [ ] **Step 2: Commit the code and policy with a strict whitelist.**

After checking `git status --short` and both branch names, stage only the changed harness/tests/current-policy documents plus the G4 design and plan. Commit with:

```bash
git commit -m "feat(harness): add native post-admission validation"
```

Do not merge or push. Do not stage `outputs/`, `docs/records/status/KNOWN_ISSUES_HANDOFF_20260802.md`, or `scripts/_diag_weekly_10y_0801.py` / `scripts/_diag_weekly_10y_bench_drift.py`.

- [ ] **Step 3: Do not run the G4 recovery stage until the current exact-version full-`all` path is authorized and passes.**

The G4 legacy prior snapshot is absent, so `native-maintenance` is not available. The only implemented recovery is the current Native algorithm and shared platform calendar through full `all`:

```bash
python -m harness onboard weekly_10y_d_overlay_0529 --predict-date 2026-08-01 --stage all --project-root .
```

Before granting activation, inspect the persisted run: all seven Gates must pass, including current Compare; the dry-run output must preserve the exact 2026-08-01/2026-07-31/2026-08-07 date contract. Activation must use `full_initial_onboarding_v1` and must not require prior admission/snapshot or maintenance evidence. Until this full path has passed, do not execute activation or a write. `legacy admission identity attestation` is not implemented and is not an alternate command.

- [ ] **Step 4: After the passed full-`all` path, activate the exact validated version under the existing authorization protocol.**

Issue an `activate` token bound to the exact `scheme_version` returned by the stage, run `python -m harness activate` with that token, and inspect its result. It must report `validation_profile=full_initial_onboarding_v1`, the current full-`all` run ID, `benchmark_validation=passed_initial_admission`, and a successful exact Registry/version readback. Do not run launchd, alter installed plists, restart backend, or write predictions during this step.

- [ ] **Step 5: Only after activation, freeze, authorize, and fill the G4 key.**

Create and revalidate a signal-gap plan that contains only the authorized `weekly_10y_d_overlay_0529` 10Y/h6 2026-08-01 target. Prepare/register a Native input artifact only if the existing signal-gap workflow says it is needed; bind its source authority, plan SHA, exact current scheme version, and only target key into the normal HMAC authorization. Execute the existing `signal-gap-fill` command once. Do not use a direct SQL client, manual scheduler call, broad backfill, or benchmark input.

- [ ] **Step 6: Read back and record closure evidence.**

Verify the sole inserted prediction through the repository/DB read path and formal API/front-end read path. Confirm its base scheme, tenor, horizon, phase, predict date, feature date, target date, direction, confidence, version, and artifact provenance exactly match the gate evidence. Update only the four listed G4 status documents with run IDs and readback result, re-run the focused tests and `git diff --check`, then commit:

```bash
git commit -m "docs(g4): record weekly 10y gap closure"
```

Do not merge to `master` or push either branch until all later plan phases and specialized acceptance are complete and the user explicitly confirms production release.

### Task 5: Implement the authorized, single-scope legacy identity attestation

**Files:**
- Create: `harness/legacy_native_admission_attestation.py`
- Modify: `harness/persistence.py`
- Modify: `harness/cli.py`
- Modify: `harness/gates/native_maintenance_admission_gate.py`
- Create: `tests/test_native_legacy_admission_attestation.py`
- Modify: `tests/test_native_maintenance_admission.py`
- Modify: `tests/test_harness_persistence.py`
- Modify: `tests/test_cli_activate.py` or a focused CLI test

- [x] **Step 1: Freeze the scope and historical evidence without a write.**

Only this scope is allowed:

```text
scheme_id=weekly_10y_d_overlay_0529
prior_scheme_version=63ffb52105ee
prior_harness_run_id=hr_20260611T055610Z_8742d5bc99c9
business identity=(native_adapter, weekly_point, weekly, h6, 10Y)
```

The command must re-read rather than hard-code those values: it accepts only the current
maintenance-selected active prior `all` run, exactly one passed CompareGate and exactly one
passed StaticGate whose `native_business_identity` evidence is absent. A malformed, duplicate
or mismatched old snapshot is not eligible for attestation.

- [ ] **Step 2: Write RED tests for the receipt and its maintenance fallback.**

Cover a successful canonical receipt; wrong scheme, missing/non-expiring or replayed token,
wrong prior version/run, non-passed or ambiguous Compare, non-missing StaticGate snapshot,
duplicate/non-canonical receipt, and receipt/current business-identity drift. Prove that only
`t_harness_runs` and `t_harness_gate_results` are written, and that a receipt alone does not
activate or satisfy the six maintenance Gate history. Add the one positive maintenance case in
which an otherwise missing old snapshot is supplied by the single receipt, and retain all
existing malformed/mismatch fail-closed cases.

- [ ] **Step 3: Implement the smallest dedicated command and persistence path.**

Add an explicit `native-legacy-admission-attest` command, not a generic Gate or waiver. It must
require a one-time `native_legacy_admission_identity_attest` token with non-empty issuer,
non-empty prior version and prior run ID, and an expiry no more than 900 seconds away. Reuse
the platform's existing Native authorization signing mode (do not change environment/plist
configuration). Its deterministic run ID is based only on the prior run identifier; the
dedicated persistence function writes a passed `legacy-native-admission-attestation` run plus
one same-named gate result in one transaction. The canonical evidence stores scope, prior
version/run, current canonical business identity, issuer, issued-at and token SHA-256. It stores
no current code/config/version hash and does not overwrite historical results. Existing valid
receipt returns a read-only skipped result; any conflicting receipt fails closed.

Only when the old StaticGate field is explicitly missing may
`NativeMaintenanceAdmissionGate` read this receipt. It must require exactly one passed,
canonical receipt for the maintenance-selected prior run and must record
`admission_identity_source=legacy_operator_attestation_v1`; otherwise existing static snapshot
logic stays unchanged. The receipt must not change `AUTO_SEQUENCE`, `native-maintenance`,
ActivationGate authority, Registry lifecycle, benchmark behavior, or any business table.

- [ ] **Step 4: Verify and execute the authorized control-plane receipt.**

After focused tests and code review pass, issue the short-lived scoped token, execute the
dedicated command once, and read it back through the maintenance verifier. Confirm that only
the two Harness control-plane tables changed and that the verifier identifies the receipt source.
Do not activate, run launchd, restart backend, write a prediction, or run signal-gap fill in
this task.

- [ ] **Step 5: Update current policy documents and remove closed implementation documents.**

Replace every "attestation is future-only" statement in current architecture/SOP/status documents
with the precise fixed 10Y scope and its remaining gates. Keep only documents still needed for
unresolved G4 activation/backfill; remove this plan/spec only after G4 itself is closed. Commit
the attestation governance with a strict whitelist and leave `master` and remotes untouched.
