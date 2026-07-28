# Liwei Daily Incremental Cache MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ordinary Liwei daily execution reuse existing Phase A results and train only new or genuinely affected dates, without changing Native algorithm semantics or blocking unrelated daily schemes.

**Architecture:** Each Liwei inference adapter uses its own core alignment functions to build a dated weekly/monthly effective-input projection. The shared immutable cache compares that projection, enforces a single publisher and monotonic coverage, and retains family-local full rebuild as a recovery fallback. Legacy schema 2 generations without projection/mapping proof are not adopted; the affected family performs one automatic full fallback and then uses schema 3 incremental generations.

**Tech Stack:** Python 3.12, pandas, immutable JSON/pickle cache generations, pytest, existing daily ledger and temporary MySQL 8.0.45 harness.

---

## File map

- Create `shared/liwei_0616_cache_projection.py`: build and validate the dated effective auxiliary projection without importing any Native core.
- Modify `shared/liwei_0616_phase_a_cache.py`: persist projection state, decide append/suffix/full, enforce publisher-only writes and monotonic coverage.
- Modify the ten `schemes/liwei_0616_*/inference.py` adapters: supply exact core callbacks, proof files and publisher identity.
- Create `tests/test_liwei_0616_cache_projection.py`: projection and dependency-state unit tests.
- Modify `tests/test_liwei_0616_phase_a_cache.py`: real false-invalidation and append tests.
- Modify `tests/test_liwei_0616_phase_a_cache_generations.py`: publisher, monotonic coverage, suffix and atomicity tests.
- Modify `tests/test_liwei_0616_cache_contract.py`: all ten adapters declare the exact projection and publisher contract.
- Modify `docs/CURRENT_STATUS.md` and `docs/TODO.md` only after the final candidate passes.

### Task 1: Effective auxiliary projection

**Files:**
- Create: `shared/liwei_0616_cache_projection.py`
- Create: `tests/test_liwei_0616_cache_projection.py`

- [ ] **Step 1: Write the failing projection tests**

Add tests that supply small daily, weekly and monthly frames plus callbacks representing the existing core:

```python
def test_projection_excludes_unused_and_future_auxiliary_values():
    projection = build_auxiliary_dependency_projection(
        daily_df=daily_through("2026-07-27"),
        weekly_df=weekly_with_unused_change(),
        monthly_df=monthly_with_202608_values(),
        date_to_week=calendar_map(),
        prepare_model_frames=exact_prepare_model_frames,
        build_wkmo_features=exact_build_wkmo_features,
        proof_files=(core_file, alignment_file),
    )
    assert projection.frame["date"].max() == "2026-07-27"
    assert "unused_weekly" not in projection.frame
    assert "unused_monthly" not in projection.frame
    assert projection.proof["date_to_week_sha256"]
```

Also cover explicit versus fallback week mapping, duplicate period keys, cross-year dates, missing periods, stable column order and deterministic content SHA.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
/Users/macstudio0/miniconda3/envs/get_factor/bin/python -m pytest -q \
  tests/test_liwei_0616_cache_projection.py
```

Expected: collection fails because `shared.liwei_0616_cache_projection` does not exist.

- [ ] **Step 3: Implement the projection value object and builder**

Implement:

```python
@dataclass(frozen=True)
class AuxiliaryDependencyProjection:
    frame: pd.DataFrame
    proof: Mapping[str, object]
    content_sha256: str


def build_auxiliary_dependency_projection(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: Mapping[str, int | str] | None,
    prepare_model_frames: Callable[..., tuple[pd.DataFrame, pd.DataFrame]],
    build_wkmo_features: Callable[[pd.DataFrame, pd.DataFrame], pd.DataFrame],
    proof_files: tuple[Path, ...],
) -> AuxiliaryDependencyProjection:
    ...
```

The builder must normalize and sort daily dates, invoke the supplied exact core callbacks, insert the normalized `date` key, reject duplicate/non-monotonic dates, hash `date_to_week`, proof-file bytes, columns, dtypes and row values, and never import from `schemes`.

- [ ] **Step 4: Run projection tests and verify GREEN**

Run the Task 1 test file. Expected: all tests pass in under ten seconds.

- [ ] **Step 5: Commit Task 1**

```bash
git add shared/liwei_0616_cache_projection.py \
  tests/test_liwei_0616_cache_projection.py
git commit -m "feat: build effective liwei cache projections"
```

### Task 2: Projection-aware append and suffix decisions

**Files:**
- Modify: `shared/liwei_0616_phase_a_cache.py`
- Modify: `tests/test_liwei_0616_phase_a_cache.py`
- Modify: `tests/test_liwei_0616_phase_a_cache_generations.py`

- [ ] **Step 1: Add RED tests for the 2026-07-27 to 2026-07-28 regression**

The fixture must reproduce:

```python
assert raw_change["weekly"] == "revision"
assert raw_change["monthly"] == "revision"
assert effective_projection_change == "append"
assert audit["build_mode"] == "append"
assert trained_dates == ["2026-07-24", "2026-07-27"]
```

Add separate tests for an effective weekly revision and effective monthly revision. Both must calculate the earliest changed projection date `D`, preserve dates before `D`, and train every cached/requested date from `D` onward. Missing or changed proof must remain `full`.

- [ ] **Step 2: Run only the new tests and verify RED**

Run the named tests with `-q`. Expected: current cache reports `full`.

- [ ] **Step 3: Extend cache input state without weakening raw provenance**

Add an optional required-for-new-ABI argument:

```python
auxiliary_projection: AuxiliaryDependencyProjection | None = None
```

Persist both:

```python
{
    "frames": {"daily": ..., "weekly": ..., "monthly": ...},
    "effective_auxiliary": {
        "frame": _frame_generation_state(projection.frame, "date"),
        "proof": projection.proof,
        "content_sha256": projection.content_sha256,
    },
}
```

Raw weekly/monthly changes remain audit evidence but no longer decide rebuild when both parent and current have valid matching projection proof. Daily revision semantics remain unchanged.

- [ ] **Step 4: Implement effective projection change analysis**

Use existing key-fingerprint comparison for the dated projection:

```python
if proof_changed_or_missing:
    return ("full", "auxiliary_projection_proof_changed", None)
if effective_change == "revision":
    return ("suffix", "proven_auxiliary_input_revision", earliest_date)
if effective_change in {"unchanged", "append"}:
    return ("append", "effective_auxiliary_append", None)
```

If `earliest_date` precedes the earliest cached test date, the suffix naturally rebuilds the complete retained coverage. Unknown, schema drift or invalid projection stays full.

- [ ] **Step 5: Run the two cache test files**

Expected: the new regression tests pass and existing fail-closed tests remain green after updating only assertions whose contract intentionally changed.

- [ ] **Step 6: Commit Task 2**

```bash
git add shared/liwei_0616_phase_a_cache.py \
  tests/test_liwei_0616_phase_a_cache.py \
  tests/test_liwei_0616_phase_a_cache_generations.py
git commit -m "fix: rebuild only affected liwei cache dates"
```

### Task 3: Single publisher and monotonic family coverage

**Files:**
- Modify: `shared/liwei_0616_phase_a_cache.py`
- Modify: `tests/test_liwei_0616_phase_a_cache_generations.py`

- [ ] **Step 1: Write RED tests for the 618-to-42 shrink**

Create a current generation with 618 dates, then invoke a consumer requesting 42 dates:

```python
assert audit["status"] == "hit"
assert published_generation_id == original_generation_id
assert len(load_current_cache()["STD"]["test_dates"]) == 618
assert trainer_calls == []
```

Add a test where the publisher handles a real revision and must rebuild the union of retained and requested dates. Add a consumer-before-publisher test that fails with a stable `CACHE_PUBLISHER_REQUIRED` code without changing current.

- [ ] **Step 2: Run the named tests and verify RED**

Expected: the current implementation publishes the narrow generation or permits the consumer to write.

- [ ] **Step 3: Add publisher identity to `PhaseACacheSpec`**

Add:

```python
publisher_consumer_id: str
```

Include it in the spec fingerprint. In `prepare_phase_a_caches()`, require a non-empty `cache_consumer_id`. A non-publisher may only return a validated hit whose dates cover its request; it cannot train, stage or publish.

- [ ] **Step 4: Make coverage monotonic**

For publisher rebuilds with a trusted parent:

```python
desired_dates = sorted(
    set(parent_cache["test_dates"]) | set(requested_dates)
)
```

Full and suffix decisions must use `desired_dates`; a new current may not drop a retained date unless the spec fingerprint changes and no adoption is claimed.

- [ ] **Step 5: Run generation and concurrency tests**

Expected: publisher-only writes, no shrink, existing atomic pointer and lock tests pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add shared/liwei_0616_phase_a_cache.py \
  tests/test_liwei_0616_phase_a_cache_generations.py
git commit -m "fix: preserve shared liwei cache coverage"
```

### Task 4: Wire all seven families through ten inference adapters

**Files:**
- Modify: `schemes/liwei_0616_*/inference.py` for the exact ten active Liwei schemes
- Modify: `tests/test_liwei_0616_cache_contract.py`

- [ ] **Step 1: Extend the contract matrix and verify RED**

For every adapter assert:

- it imports its own core `prepare_model_frames` and `build_wkmo_features`;
- it supplies core and data-alignment source files as projection proof;
- its `publisher_consumer_id` matches the seven-family table in the design;
- consumers in a shared family declare the same spec fingerprint.

- [ ] **Step 2: Run the contract test and verify RED**

Expected: all ten adapters are missing projection wiring and publisher identity.

- [ ] **Step 3: Wire each adapter**

Each adapter builds:

```python
auxiliary_projection = build_auxiliary_dependency_projection(
    daily_df=daily_df,
    weekly_df=weekly_df,
    monthly_df=monthly_df,
    date_to_week=date_to_week,
    prepare_model_frames=prepare_model_frames,
    build_wkmo_features=build_wkmo_features,
    proof_files=(Path(v31_common.__file__), Path(data_alignment.__file__)),
)
```

Then passes the projection to `prepare_phase_a_caches()`. Do not change any core file, model config, test window or result mapping.

- [ ] **Step 4: Run contract and inference regression tests**

Run:

```bash
/Users/macstudio0/miniconda3/envs/get_factor/bin/python -m pytest -q \
  tests/test_liwei_0616_cache_contract.py \
  tests/test_liwei_0616_cache_production_integration.py \
  tests/test_prewarm_liwei_0616_phase_a_cache.py
```

Expected: all pass.

- [ ] **Step 5: Commit Task 4**

```bash
git add schemes/liwei_0616_*/inference.py \
  tests/test_liwei_0616_cache_contract.py
git commit -m "feat: use effective cache inputs for liwei schemes"
```

### Task 5: Legacy cache decision

- [x] The no-training adoption experiment was timeboxed and reverted.
- [x] Real schema 2 generations were confirmed to lack effective projection and
  `date_to_week` proof, so automatic adoption is fail-closed.
- [x] Keep family-local automatic full fallback for the first schema 3 build;
  do not add a generic adoption CLI or a centralized prewarm step.
- [ ] A future adoption design may proceed only for generations carrying
  complete projection, mapping and lineage evidence.

### Task 6: Minimal candidate verification

**Files:**
- Modify only if a red test exposes one cache-specific root cause.

- [ ] **Step 1: Run focused unit and contract tests**

Run all Liwei cache/projection tests. Expected: zero failures.

- [ ] **Step 2: Run seven-family warm incremental validation**

Use an owner-only isolated copy of the persistent cache and the frozen 2026-07-27/28 input fixtures. Assert each publisher trains only new/suffix dates and shared consumers make zero trainer calls.

- [ ] **Step 3: Run one temporary-MySQL 25/29 matrix**

Use the existing opt-in daily MVP MySQL test with controlled executors. Expected: 25 winning attempts, 29 accepted targets, reentry adds zero rows, 28/29 stays `BREACHED`.

- [ ] **Step 4: Run one real warm-incremental 25/29 replay**

Keep backend/frontend online, use isolated MySQL and isolated cache copy, and never write production. Expected: 29/29, no duplicate/partial/orphan, with per-family cache build mode and trained-date counts recorded.

- [ ] **Step 5: Run the final candidate gate once**

```bash
/Users/macstudio0/miniconda3/envs/get_factor/bin/python -m pytest -q
/Users/macstudio0/miniconda3/envs/get_factor/bin/python -m compileall -q \
  backend backtests harness scheduler shared schemes tests
git diff --check
```

Expected: zero failures and a clean diff.

- [ ] **Step 6: Update status documents**

Record the exact test counts, seven-family incremental evidence, real 25/29 result, runtime, remaining production authorization boundary and deferred forced-cold/long-term work in `docs/CURRENT_STATUS.md` and `docs/TODO.md`.

- [ ] **Step 7: Commit verification documents**

```bash
git add docs/CURRENT_STATUS.md docs/TODO.md
git commit -m "docs: record liwei incremental cache mvp"
```

## Execution rules

- One code writer; independent agents may perform read-only reviews only.
- One root cause and one reversible commit at a time.
- Do not rerun real algorithms or full pytest after every commit.
- Do not modify Native core algorithms, production database, backend/frontend service, BondProjectPro, `master` or remote branches.
- A focused test taking longer than 30 minutes is not a unit gate; stop it, record the issue and continue with the smallest blocking root cause.
- No production cache adoption, scheduler switch, service restart, merge, push or deployment without separate explicit authorization.
