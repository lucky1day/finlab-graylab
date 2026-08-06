# Blackbox Gray Replay Shared Snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one gray-live signal-gap replay job read and freeze DataBridge once, then reuse that immutable parent snapshot for compatible daily, weekly, and monthly Blackbox requests without changing any request's point-in-time cutoff semantics.

**Architecture:** `shared.input_artifacts` creates a durable, content-addressed, physically clipped base snapshot plus a run-scoped immutable session manifest. In a `signal-gap-fill` invocation that session is the sole current-DataBridge read; `harness.signal_gap_plan` replays frozen DataBridge authority for preflight and postflight rather than reopening current data. `scheduler.executor` consumes the session per Blackbox scheme through Contract 1.0 `backtest` batches; the gate preserves existing per-`predict_date` authorization, run, validation, and atomic persistence boundaries. The CGB delivery wrapper separately fixes its `week_id`-only cache defect by using the complete cutoff signature; Native source-backed paths remain unchanged.

**Tech Stack:** Python 3.12, Pandas, SQLAlchemy read-only transactions, Blackbox V2 Contract 1.0, `pytest`/`unittest`, existing `forecast_env` subprocess runner.

---

## Scope and invariants

- The work applies to `runtime_type=blackbox_v2` gray-gap replay only. Native daily/monthly source-backed replay continues to use its frozen Native generation path and is not routed through DataBridge.
- A job-wide parent snapshot is derived from one validated DataBridge current read. It is physically clipped to the maximum frozen daily, weekly, and monthly cutoff keys present in that job's Blackbox actions.
- In one `signal-gap-fill` invocation, no path other than session creation may call `check_current_dataset()` or `resolve_stable_databridge_current_authority()` for Blackbox replay. Preflight and postflight replan business/Registry state with the session-validated frozen authority.
- Every Blackbox Request keeps its original `predict_date`, `feature_date`, `target_date`, and complete cutoff signature. Input reuse does not make different as-of requests equal.
- The session is an ignored runtime artifact. It creates no daily ledger record, input-generation registry row, Registry mutation, launchd change, or business-table write.
- A scheme may need more than one Contract batch because of the profile request cap or a non-equivalent platform-input signature. Those batches reuse the same parent DataBridge snapshot.
- `complete_gray_gap_run()` remains the only business persistence path and keeps exact per-`predict_date` authorization checks.

## File map

| File | Responsibility |
|---|---|
| `shared/input_artifacts.py` | Define, create, verify, and persist the job-scoped Blackbox gray-replay snapshot session. |
| `scheduler/executor.py` | Add a read-only Blackbox gray-replay batch runner that consumes the session and enriches records with provenance. |
| `harness/gates/signal_gap_fill_gate.py` | Batch compatible Blackbox gray-gap groups by source identity, create one session per identity, and fan records back into existing per-date executions. |
| `harness/signal_gap_plan.py` | Accept a fully validated frozen DataBridge authority override so fill preflight/postflight can inspect business state without reopening current DataBridge. |
| `schemes/cgb_causal_wk_1y_v128/delivery/cgb_causal_wk_1y_v128.py` | Replace the CGB fast path's week-only identity with the complete cutoff signature. |
| `tests/test_blackbox_gray_replay_session.py` | Test physical clipping, immutable manifest, and one-current-read behavior. |
| `tests/test_blackbox_v2_runner.py` | Test executor batching, runtime-view reuse, provenance, and profile chunking. |
| `tests/test_signal_gap_fill_gate.py` | Test mixed daily/weekly/monthly session reuse and strict source-identity separation. |
| `tests/test_signal_gap_plan.py` | Test the authority-override grammar and prove the override path does not resolve mutable DataBridge current. |
| `tests/test_cgb_causal_wk_1y_v128_delivery.py` | Test CGB signature isolation, duplicate fan-out, and one-pass eligibility. |
| `docs/superpowers/specs/2026-08-06-cgb-weekly-batch-cutoff-identity-design.md` | Record implementation status and link this approved plan. |

### Task 1: Add the immutable, physically clipped DataBridge replay session

**Files:**

- Modify: `shared/input_artifacts.py:42-46, 711-809`
- Create: `tests/test_blackbox_gray_replay_session.py`

- [ ] **Step 1: Write focused failing session tests**

Create `tests/test_blackbox_gray_replay_session.py` with a fake `CurrentDataset` containing daily rows through `2026-08-06`, weekly keys through `202632`, and monthly keys through `202608`. Patch `shared.input_artifacts.check_current_dataset` and require one call while constructing a session whose frozen requests end at `2026-07-31`, `202631`, and `202607`.

```python
session = build_blackbox_gray_replay_session(
    session_id="a" * 64,
    source_identity=_source_identity(),
    request_cutoffs=(
        CutoffKeys("2026-07-24", "202630", "202606"),
        CutoffKeys("2026-07-31", "202631", "202607"),
    ),
    data_bridge_config=_config(tmp_path),
    output_root=tmp_path / "gray-replay",
)

self.assertEqual(current_read.call_count, 1)
self.assertEqual(_keys(session.snapshot.data_dir / "daily_output.csv", "date"), ["2026-07-24", "2026-07-31"])
self.assertEqual(_keys(session.snapshot.data_dir / "weekly_output.csv", "week_id"), ["202630", "202631"])
self.assertEqual(_keys(session.snapshot.data_dir / "monthly_output.csv", "month_id"), ["202606", "202607"])
self.assertTrue(session.manifest_path.is_file())
```

Add a source-mutation test that proves persisted snapshot bytes and manifest SHA stay unchanged after the source frames change. Add an absent-cutoff test that expects `ValueError` containing `gray replay cutoff`.

- [ ] **Step 2: Run the focused test to verify the API is absent**

Run: `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n bond_factor_lab_service python -m pytest -q tests/test_blackbox_gray_replay_session.py`

Expected: collection fails because `build_blackbox_gray_replay_session` is not yet exported by `shared.input_artifacts`.

- [ ] **Step 3: Implement the session only in `shared.input_artifacts`**

Add this data class and public constructor near the existing Blackbox snapshot helpers. Keep all DataBridge reading, validation, clipping, and artifact writing in this module.

```python
@dataclass(frozen=True)
class BlackboxGrayReplaySession:
    snapshot: BlackboxSnapshot
    manifest_path: Path
    manifest_sha256: str
    source_identity: dict[str, Any]
    max_cutoffs: CutoffKeys


def build_blackbox_gray_replay_session(
    *,
    session_id: str,
    source_identity: Mapping[str, Any],
    request_cutoffs: Iterable[CutoffKeys],
    data_bridge_config: DataBridgeRefreshConfig,
    output_root: str | Path = BLACKBOX_GRAY_REPLAY_SESSION_ROOT,
) -> BlackboxGrayReplaySession:
    """一次读取 current 后固化 gray replay 所有请求共用的三频父快照。"""
```

Normalize `session_id` as a lower-case SHA-256. Require exactly `generation_id`, `refresh_date`, `schema_version`, `business_digest`, `stable_identity_sha256`, and sorted `files` in `source_identity`; require at least one `CutoffKeys`. Call `check_current_dataset()` exactly once with `strict_read_only=True`, then compare generation, refresh date, schema, business digest, and file identities before using any frame.

```python
max_cutoffs = CutoffKeys(
    daily_cutoff_key=max(item.daily_cutoff_key for item in cutoffs),
    weekly_cutoff_key=max(item.weekly_cutoff_key for item in cutoffs),
    monthly_cutoff_key=max(item.monthly_cutoff_key for item in cutoffs),
)
clipped = {
    "daily_output.csv": _clip_gray_replay_daily(current.dataset.frames["daily_output.csv"], max_cutoffs.daily_cutoff_key),
    "weekly_output.csv": _clip_gray_replay_period(current.dataset.frames["weekly_output.csv"], "week_id", max_cutoffs.weekly_cutoff_key),
    "monthly_output.csv": _clip_gray_replay_period(current.dataset.frames["monthly_output.csv"], "month_id", max_cutoffs.monthly_cutoff_key),
}
```

Use `create_snapshot_from_frames()` with the existing schema columns, attach verified generation/refresh via `dataclasses.replace`, and atomically write a canonical session manifest under `backtest_artifacts/blackbox_v2/gray_replay_sessions/<session_id>/manifest.json`:

```python
{
    "manifest_version": "blackbox-gray-replay-session-v1",
    "session_id": session_id,
    "parent_snapshot_id": snapshot.snapshot_id,
    "parent_snapshot_manifest_sha256": _file_sha256(snapshot.manifest_path),
    "source_identity": normalized_source_identity,
    "max_cutoffs": asdict(max_cutoffs),
}
```

If the manifest already exists, rehash and compare canonical bytes; only return it if every field matches. Validate each requested cutoff exists in the parent snapshot and none is later than `max_cutoffs`.

- [ ] **Step 4: Run the session and input-artifact regressions**

Run: `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n bond_factor_lab_service python -m pytest -q tests/test_blackbox_gray_replay_session.py tests/test_input_artifacts.py`

Expected: all selected tests pass, proving one current read, three physical upper bounds, and an immutable manifest.

- [ ] **Step 5: Commit the isolated input-layer change**

Run: `git diff --check && git status --short && git add shared/input_artifacts.py tests/test_blackbox_gray_replay_session.py && git commit -m "feat: add blackbox gray replay snapshot session"`

Expected: only the shared input module and its focused test are staged; ignored runtime artifacts and unrelated working-tree files remain unstaged.

### Task 2: Execute a Blackbox scheme batch from the shared session

**Files:**

- Modify: `scheduler/executor.py:24-67, 801-1074`
- Modify: `tests/test_blackbox_v2_runner.py:417-509, 1554-1702`

- [ ] **Step 1: Add failing executor tests for one view, one parent snapshot, and original request dates**

Add a `BlackboxGrayReplayBatchTests` class to `tests/test_blackbox_v2_runner.py`. Construct three same-frequency `BlackboxRequest` objects with dates and cutoffs already frozen by the plan. Cross-frequency session sharing is covered in Task 3. Mock `BlackboxGrayReplaySession`, `open_blackbox_runtime_view`, and `run_blackbox_backtest`.

```python
records = run_blackbox_gray_replay_batch(
    cfg,
    requests=requests,
    session=session,
    engine=engine,
    algo_env="forecast_env",
    timeout_sec=600,
)

runtime_view.assert_called_once()
backtest.assert_called_once()
self.assertEqual(backtest.call_args.kwargs["data_snapshot_id"], "snapshot-parent")
self.assertEqual(
    [(row.predict_date, row.feature_date, row.target_date) for row in records],
    [(item.predict_date, item.feature_date, item.target_date) for item in requests],
)
self.assertTrue(all(row.extra["gray_replay_session_id"] == "a" * 64 for row in records))
```

Add a profile-cap case with 205 requests and `RuntimeProfile.for_tests(max_batch_requests=100)`. Assert that the batch runner receives one parent session and opens no second input snapshot; the existing internal Contract splitter may start three CLI batches. Add a cutoff-overrun case that asserts `run_blackbox_backtest()` is never called.

- [ ] **Step 2: Run the focused executor test to verify the API is absent**

Run: `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n bond_factor_lab_service python -m pytest -q tests/test_blackbox_v2_runner.py -k gray_replay_batch`

Expected: FAIL because `run_blackbox_gray_replay_batch` does not exist.

- [ ] **Step 3: Add `run_blackbox_gray_replay_batch()` without changing scheduled single-point execution**

Import `BlackboxGrayReplaySession`, `BlackboxRequest`, `RuntimeProfile`, `DEFAULT_RUNTIME_PROFILE`, and `run_blackbox_backtest`. Add this public executor function next to `run_blackbox_scheme_subprocess()`:

```python
def run_blackbox_gray_replay_batch(
    cfg: SchemeConfig,
    *,
    requests: Sequence[BlackboxRequest],
    session: BlackboxGrayReplaySession,
    engine: Any,
    algo_env: str,
    timeout_sec: int,
    profile: RuntimeProfile = DEFAULT_RUNTIME_PROFILE,
) -> list[PredictionRecord]:
    """在一个已冻结的 DataBridge gray replay session 内执行同一 Blackbox 方案。"""
```

Require `cfg.runtime_type == "blackbox_v2"`, `cfg.input_source == "data_bridge_current"`, non-empty unique request IDs, and three-key request cutoffs no later than `session.max_cutoffs`. Load delivery metadata once and require its scheme ID to equal `cfg.scheme_id`. Do not call `open_blackbox_input_snapshot()`, `resolve_blackbox_input_cutoffs()`, or `build_live_request()` here: all dates and cutoffs come from the frozen plan.

Capture declared platform inputs once per compatible platform-input signature, compose each bundle from `session.snapshot`, and open one `open_blackbox_runtime_view()` per resulting bundle. For the registered `api-wind-date-v1` provider, capture at the maximum weekly cutoff and require coverage for every request. If a provider produces different content for a request signature, split only runtime/model batches by complete cutoff signature and continue using the same `session.snapshot`.

Build the effective `RuntimeProfile` from the supplied `profile`, replacing its conda environment only when `algo_env` differs from the default. Call existing `run_blackbox_backtest()` with that effective profile for each runtime bundle. Before returning a record, preserve the existing `extra` dictionary and add:

```python
{
    "replay_semantics": "current_snapshot_as_of_not_historical_vintage",
    "gray_replay_session_id": session.manifest_path.parent.name,
    "gray_replay_manifest_sha256": session.manifest_sha256,
    "data_generation_id": session.snapshot.generation_id,
    "source_refresh_date": session.snapshot.refresh_date,
    "daily_cutoff_key": request.daily_cutoff_key,
    "weekly_cutoff_key": request.weekly_cutoff_key,
    "monthly_cutoff_key": request.monthly_cutoff_key,
}
```

Leave `run_blackbox_scheme_subprocess()` untouched for normal scheduled `predict` and the legacy one-point path; Task 3 moves only `signal-gap-fill` to the new API.

- [ ] **Step 4: Run batch and existing executor regressions**

Run: `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n bond_factor_lab_service python -m pytest -q tests/test_blackbox_v2_runner.py tests/test_databridge_generation_executor.py tests/test_executor_run_id.py`

Expected: all tests pass. Existing scheduled and bound-generation tests retain the old single-point path; new tests prove gray replay does not open mutable current input per request.

- [ ] **Step 5: Commit the executor batch API**

Run: `git diff --check && git status --short && git add scheduler/executor.py tests/test_blackbox_v2_runner.py && git commit -m "feat: batch blackbox gray replay execution"`

Expected: no DataBridge output directory, report, or unrelated diagnostics is staged.

### Task 3: Batch compatible signal-gap groups while keeping per-date writes atomic

**Files:**

- Modify: `harness/gates/signal_gap_fill_gate.py:70-85, 541-638, 801-1008`
- Modify: `harness/signal_gap_plan.py:654-703, 1100-1118, 2200-2310`
- Modify: `tests/test_signal_gap_fill_gate.py:1-166`
- Modify: `tests/test_signal_gap_plan.py`

- [ ] **Step 1: Write failing gate tests for a mixed daily/weekly/monthly job**

Extend `tests/test_signal_gap_fill_gate.py` with frozen-plan helpers that create three missing Blackbox actions sharing one base source identity but carrying independent frozen cutoff payloads:

```python
daily = _blackbox_action(
    scheme="daily_trial", frequency="daily", predict="2026-07-02",
    feature="2026-07-01", target="2026-07-02",
    cutoff=("2026-07-01", "202626", "202606"),
)
weekly = _blackbox_action(
    scheme="weekly_trial", frequency="weekly", predict="2026-07-06",
    feature="2026-07-03", target="2026-07-10",
    cutoff=("2026-07-03", "202627", "202606"),
)
monthly = _blackbox_action(
    scheme="monthly_trial", frequency="monthly", predict="2026-07-15",
    feature="2026-07-15", target="2026-08-14",
    cutoff=("2026-07-15", "202628", "202607"),
)
```

Patch the session builder, new executor batch runner, and mutable DataBridge authority resolver. Assert one session-builder call containing all three cutoff signatures, one executor batch call per config, and three `complete_gray_gap_run()` calls retaining original target keys and source authority. Assert the resolver is never called during fill preflight or postflight. Add a second test with a different monthly `generation_id` that asserts two sessions and no cross-source batch. Add a session-build failure test that asserts all created Blackbox runs fail and no completion write occurs.

- [ ] **Step 2: Run the focused gate test and observe the current per-date coordinator failure**

Run: `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n bond_factor_lab_service python -m pytest -q tests/test_signal_gap_fill_gate.py -k 'shared_snapshot or mixed_frequency or source_identity'`

Expected: FAIL because `_run_algorithms()` currently submits one `run_configured_scheme()` call for each `(base_scheme_id, predict_date)` group, and `plan_signal_gaps()` currently resolves mutable current DataBridge during every replay.

- [ ] **Step 3: Introduce Blackbox batch planning and record fan-out in the gate**

Add `databridge_authority_overrides: Mapping[str, Mapping[str, Any]] | None = None` to `plan_signal_gaps()`. When it is absent, retain the existing `resolve_stable_databridge_current_authority()` path used by the standalone plan command. When it is present, require its feature-date keys to exactly equal missing Blackbox feature dates, validate every frozen payload with the same grammar as `_databridge_authority_payload()`, and pass it directly to Blackbox action eligibility without calling the mutable resolver.

```python
def plan_signal_gaps(
    engine: Any,
    *,
    start_date: str,
    as_of_date: str,
    scope: SignalGapPlanScope | Mapping[str, Any] | None = None,
    databridge_config: DataBridgeRefreshConfig | None = None,
    databridge_authority_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """生成计划；override 只复用已冻结 authority，不读取 mutable DataBridge。"""
```

Normalize the override into `{feature_date: authority_payload}` and reject an override that has missing, extra, non-canonical, or cross-feature cutoff entries. The returned action payload must remain byte-for-byte canonical with the existing no-override path, so `canonical_plan_sha256()` retains one grammar.

Add a private immutable `_BlackboxReplayBatch` holding `source_identity`, `session_id`, `executions`, and ordered `BlackboxRequest` values. Define source identity by canonicalizing each group's frozen `input_authority` after removing only its per-request `cutoff` field. It must retain generation, refresh date, schema, business digest, stable identity hash, and file identities.

```python
def _blackbox_replay_source_identity(
    authority: Mapping[str, Any],
) -> dict[str, Any]:
    required = {
        "generation_id", "refresh_date", "schema_version",
        "business_digest", "stable_identity_sha256", "files",
    }
    return json.loads(_canonical_json({key: authority[key] for key in required}))
```

At the beginning of `run_signal_gap_fill()`, derive feature-date authority overrides from the frozen plan's Blackbox actions and use them for preflight planning. If that preflight reports `ALL_PRESENT`, return without creating a session. Otherwise validate configs and authorizations, then build exactly one `BlackboxGrayReplaySession` per source identity; that builder performs the only current-DataBridge read in this fill invocation and verifies it against the frozen source identity. Reuse the same authority overrides for the postflight plan replay.

After run creation, split executions into Native items and Blackbox batches. Preserve the Native pool and `_run_algorithm()` path. For Blackbox items, construct `BlackboxRequest` with `build_request()` from every frozen action's original dates and `input_authority["cutoff"]`, then call `run_blackbox_gray_replay_batch()` once per config and compatible platform bundle.

Map returned records by `record.extra["request_id"]`. Reject duplicate, missing, and unexpected IDs before assigning `item.records`; invoke existing `_validate_group_records()` for every item. Do not alter `_create_runs()`, authorization issuance/consumption, plan postflight, `complete_gray_gap_run()`, or Native branches.

- [ ] **Step 4: Run the signal-gap and repository validation set**

Run: `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n bond_factor_lab_service python -m pytest -q tests/test_signal_gap_fill_gate.py tests/test_signal_gap_plan.py tests/test_repository_registry.py`

Expected: all tests pass. The mixed-frequency test proves one parent snapshot per common source identity and one current-DataBridge read for the whole fill invocation, while each individual gray run still has one combined snapshot identity.

- [ ] **Step 5: Commit the coordinator-only change**

Run: `git diff --check && git status --short && git add harness/gates/signal_gap_fill_gate.py harness/signal_gap_plan.py tests/test_signal_gap_fill_gate.py tests/test_signal_gap_plan.py && git commit -m "feat: share snapshots across blackbox gray gaps"`

Expected: authorization and persistence code outside the gate remains untouched.

### Task 4: Fix CGB's complete cutoff-signature batch adapter

**Files:**

- Modify: `schemes/cgb_causal_wk_1y_v128/delivery/cgb_causal_wk_1y_v128.py:2764-2829`
- Create: `tests/test_cgb_causal_wk_1y_v128_delivery.py`

- [ ] **Step 1: Write failing delivery-wrapper tests with a deterministic pipeline substitute**

Load the delivery file by absolute path with `importlib.util.spec_from_file_location()` and patch only its non-frozen `_run_pipeline`. Build minimal real `weekly_output.csv`, `daily_output.csv`, and `api_wind_date.csv` frames so `truncate_for_request()` executes normally.

```python
def test_same_week_two_daily_cutoffs_are_not_reused_by_week_id(self):
    requests = [
        _request("w30-0804", daily="2026-08-04", weekly="202631", monthly="202608"),
        _request("w30-0805", daily="2026-08-05", weekly="202631", monthly="202608"),
    ]
    directions = module.predict_batch(frames, requests)
    self.assertEqual(directions, [module.predict_one(frames, requests[0]), module.predict_one(frames, requests[1])])
    self.assertNotEqual(directions[0], directions[1])
```

Also require duplicate complete signatures to execute once and fan out in caller order, a six-week complete sequence to use one one-pass run plus three deterministic checks, and a reversed 100-request alternating conflict batch to produce the same `request_id -> direction` mapping as forward input.

- [ ] **Step 2: Run the focused delivery tests and observe the known week collision**

Run: `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n forecast_env python -m pytest -q tests/test_cgb_causal_wk_1y_v128_delivery.py`

Expected: the same-week test fails because `_one_pass_rows()` returns `week_id -> row` and `predict_batch()` reuses that row for both cutoff states.

- [ ] **Step 3: Replace week-only identity with stable complete-signature identity**

Add these non-frozen adapter helpers before `_one_pass_rows()`:

```python
def _cutoff_signature(request: dict) -> tuple[str, str, str]:
    return (
        str(request["daily_cutoff_key"]),
        str(request["weekly_cutoff_key"]),
        str(request["monthly_cutoff_key"]),
    )


def _unique_requests_by_signature(requests: list[dict]) -> list[dict]:
    by_signature = {}
    for request in requests:
        by_signature.setdefault(_cutoff_signature(request), request)
    return [by_signature[key] for key in sorted(by_signature)]
```

Make `_one_pass_rows()` return `dict[tuple[str, str, str], pd.Series]`, and permit it only when each weekly key maps to one signature and each request's daily cutoff is the last daily observation for its weekly key in the widest input. Select widest input by `(daily_cutoff_key, weekly_cutoff_key, monthly_cutoff_key)`, never by an unstable weekly key alone.

Use `_unique_requests_by_signature()` for verification sampling and fallback caching. On failed eligibility or failed verification, run `predict_one()` once per signature and fan its result back in original input order. Do not edit any `_build_component_*` function, feature transform, model setting, or `predict_one()` date semantics.

- [ ] **Step 4: Run delivery, Contract, and zero-write BacktestGate checks**

Run: `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n forecast_env python -m pytest -q tests/test_cgb_causal_wk_1y_v128_delivery.py tests/test_blackbox_v2_contracts.py tests/test_blackbox_v2_runner.py`

Run: `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n bond_factor_lab_service python -m harness gate backtest --scheme-id cgb_causal_wk_1y_v128 --project-root . --algo-env forecast_env --timeout-sec 3600 --backtest-start-date 2025-01-01`

Expected: unit and Contract tests pass; BacktestGate runs without `--persist`, reports zero protected-table deltas, and verifies cutoff isolation.

- [ ] **Step 5: Commit the CGB adapter revision separately**

Run: `git diff --check && git status --short && git add schemes/cgb_causal_wk_1y_v128/delivery/cgb_causal_wk_1y_v128.py tests/test_cgb_causal_wk_1y_v128_delivery.py && git commit -m "fix: isolate CGB batch cutoff signatures"`

Expected: the delivery-script hash changes in this commit. Treat it as a new exact Blackbox version for subsequent Gate and registration work; do not reuse prior evidence or business writes.

### Task 5: Verify the integrated behavior and record its operational boundary

**Files:**

- Modify: `tests/test_signal_gap_fill_gate.py`
- Modify: `docs/superpowers/specs/2026-08-06-cgb-weekly-batch-cutoff-identity-design.md`
- Modify: `docs/superpowers/plans/2026-08-06-blackbox-gray-replay-shared-snapshot.md`

- [ ] **Step 1: Add a data-flow integration test before final verification**

Extend the Task 3 fixture to use the real `build_blackbox_gray_replay_session()` against a temporary DataBridge root and a fake batch delivery. Require one current read, one parent snapshot, one session manifest, and separate daily/weekly/monthly record dates after fan-out. Mutate source files after the session is built and assert the fake delivery receives the original parent snapshot path and original cutoff keys.

- [ ] **Step 2: Run the complete focused regression suite**

Run: `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n bond_factor_lab_service python -m pytest -q tests/test_blackbox_gray_replay_session.py tests/test_blackbox_v2_runner.py tests/test_databridge_generation_executor.py tests/test_signal_gap_fill_gate.py tests/test_signal_gap_plan.py tests/test_repository_registry.py tests/test_input_artifacts.py`

Run: `PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n forecast_env python -m pytest -q tests/test_cgb_causal_wk_1y_v128_delivery.py tests/test_blackbox_v2_contracts.py`

Expected: every test passes. The run must not use `--persist`, issue a gray-gap write token, activate a scheme, change Registry state, invoke `launchctl`, or write business predictions.

- [ ] **Step 3: Inspect the final diff, refresh design status, and commit documentation**

Update the design document's implementation status with the shipped session manifest fields, executor batch boundary, CGB complete-signature identity, and exact passing test commands. Keep explicit exclusions for Native, production writes, activation, and launchd.

Run: `git diff --check && git status --short && git diff -- docs/superpowers/specs/2026-08-06-cgb-weekly-batch-cutoff-identity-design.md docs/superpowers/plans/2026-08-06-blackbox-gray-replay-shared-snapshot.md`

Run: `git add docs/superpowers/specs/2026-08-06-cgb-weekly-batch-cutoff-identity-design.md docs/superpowers/plans/2026-08-06-blackbox-gray-replay-shared-snapshot.md && git commit -m "docs: record gray replay snapshot verification"`

Expected: documentation is committed separately from code; unrelated working-tree files remain untouched.

- [ ] **Step 4: Request fresh Blackbox admission evidence before any state-changing follow-up**

Because Task 4 changes the delivery script, record the newly computed exact scheme version and run the normal zero-write Blackbox `static`, `input`, `unit`, `dry-run`, `compare`, `backtest`, and `api-readiness` evidence sequence for that exact version. Do not run activation, persistent backtest, gray-gap write, Registry updates, scheduler admission, or launchd operations without separate explicit authorization.

## Plan self-review

- **Spec coverage:** Task 1 implements one DataBridge read, one persisted parent snapshot, immutable manifest, and physical three-frequency clipping. Task 2 reuses that snapshot at the runner layer. Task 3 makes daily/weekly/monthly gray replay use the session while preserving per-date write boundaries. Task 4 fixes CGB full-cutoff identity. Task 5 validates provenance, no-future-data behavior, and no-production-write boundaries.
- **Type consistency:** `BlackboxGrayReplaySession` is defined in Task 1 and consumed by `run_blackbox_gray_replay_batch()` in Task 2. Task 3 builds `BlackboxRequest` objects and passes the same session type to that runner. Task 4 remains delivery-local and does not depend on the session type.
- **Scope check:** The session, runner, coordinator, and CGB adapter form one delivery chain. Native input generation, schema migrations, Registry lifecycle, scheduler triggering, and launchd are deliberately excluded.
