# liwei_0616 Incremental Phase A Cache Implementation Plan

> **文档状态：HISTORICAL。** 本文是内部实施记录，不是当前操作 SOP；当前入口见 [文档中心](../../README.md)。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the five liwei_0616 live T+5 schemes persist and share baseline Phase A outputs so every warm run trains only previously unseen feature dates.

**Architecture:** Add a scheme-agnostic cache store in `shared/` that owns fingerprints, locks, pkl envelopes, validation, merge, and atomic replacement. Each liwei_0616 inference adapter supplies its own unchanged `model_config`/`run_prediction` callbacks, trains only missing one-day `test_ranges`, then passes the complete cache into the existing full-window Phase B/C, seasonal VT, consensus, and fallback path.

**Tech Stack:** Python 3.12, pandas, NumPy, pickle, `fcntl.flock`, unittest/pytest, existing harness gates.

---

## File map

- Create `shared/liwei_0616_phase_a_cache.py`: generic cache identity, input-prefix fingerprints, locking, validation, merge, and atomic persistence.
- Create `tests/test_liwei_0616_phase_a_cache.py`: cache-store TDD tests, including append-only, invalidation, corruption, and locking behavior.
- Modify the five `schemes/liwei_0616_*/inference.py` files: opt-in live cache orchestration through callbacks; no file I/O in core.
- Modify the five `schemes/liwei_0616_*/predict.py` files: enable incremental cache for live predictions and expose audit fields in `extra`.
- Modify the five existing `tests/test_liwei_0616_*.py` files: inference wiring, paired baseline equivalence, and prediction audit assertions.
- Create `scripts/prewarm_liwei_0616_phase_a_cache.py`: no-DB-write cache prewarm entry point for 5Y, 7Y, and 10Y representative schemes.
- Create `tests/test_prewarm_liwei_0616_phase_a_cache.py`: prewarm scope and output tests.
- Modify `docs/CURRENT_STATUS.md`: record the verified behavior only after all tests and gates pass.

### Task 1: Generic cache envelope and append-only merge

**Files:**
- Create: `shared/liwei_0616_phase_a_cache.py`
- Create: `tests/test_liwei_0616_phase_a_cache.py`

- [ ] **Step 1: Write failing envelope, cold-build, hit, and extension tests**

Create fixtures with two LGBM config rows and a trainer that records the requested date ranges:

```python
def fake_phase_a(days: list[str]) -> dict[str, object]:
    return {
        "test_dates": days,
        "results": [
            {"config": {"window": 200}, "preds": np.arange(len(days), dtype=np.int32),
             "probs": np.linspace(0.4, 0.6, len(days), dtype=np.float64)},
            {"config": {"window": 350}, "preds": -np.arange(len(days), dtype=np.int32),
             "probs": np.linspace(0.3, 0.7, len(days), dtype=np.float64)},
        ],
    }
```

Assert:

```python
common = {
    "spec": spec,
    "daily_df": daily_df,
    "weekly_df": weekly_df,
    "monthly_df": monthly_df,
    "train_missing": trainer,
    "cache_root": tmp_path,
}
cold, cold_audit = prepare_phase_a_caches(
    **common,
    test_ranges=(("2026-07-01", "2026-07-02"),),
)
assert cold_audit["status"] == "cold_build"
assert trained_dates == ["2026-07-01", "2026-07-02"]

trained_dates.clear()
hit, hit_audit = prepare_phase_a_caches(
    **common,
    test_ranges=(("2026-07-01", "2026-07-02"),),
)
assert hit_audit["status"] == "hit"
assert trained_dates == []

extended, audit = prepare_phase_a_caches(
    **common,
    test_ranges=(("2026-07-01", "2026-07-03"),),
)
assert audit["status"] == "extended"
assert trained_dates == ["2026-07-03"]
assert extended["STD"]["test_dates"] == ["2026-07-01", "2026-07-02", "2026-07-03"]
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest -v tests.test_liwei_0616_phase_a_cache
```

Expected: collection/import failure because `shared.liwei_0616_phase_a_cache` does not exist.

- [ ] **Step 3: Implement the public cache API and canonical merge**

Implement these public types and function:

```python
CACHE_SCHEMA_VERSION = 1
PHASE_A_CACHE_ABI_VERSION = "liwei_0616.phase_a.v1"

@dataclass(frozen=True)
class PhaseACacheSpec:
    cache_family: str
    tenor: str
    baselines: tuple[str, ...]
    baseline_configs: Mapping[str, Mapping[str, Any]]
    source_ic_screen_start: str
    horizon: int
    purge_gap: int

def prepare_phase_a_caches(
    *,
    spec: PhaseACacheSpec,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    test_ranges: tuple[tuple[str, str], ...],
    train_missing: Callable[[str, tuple[tuple[str, str], ...]], Mapping[str, Any]],
    cache_root: str | Path | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
```

For each baseline, derive requested dates from `daily_df.date`, the configured `close` column, and the original ranges. On a miss, call `train_missing` with one-day ranges only. Merge by canonical JSON of each result `config`, preserve `int32/float64`, sort dates, and reject conflicting duplicate dates.

- [ ] **Step 4: Run the focused tests and confirm they pass**

Run the Task 1 pytest command. Expected: all Task 1 cases pass.

- [ ] **Step 5: Commit the generic cache slice**

```bash
git add shared/liwei_0616_phase_a_cache.py tests/test_liwei_0616_phase_a_cache.py
git commit -m "feat: add liwei phase a cache store"
```

### Task 2: Fingerprints, invalidation, locking, and atomic recovery

**Files:**
- Modify: `shared/liwei_0616_phase_a_cache.py`
- Modify: `tests/test_liwei_0616_phase_a_cache.py`

- [ ] **Step 1: Write failing invalidation and concurrency tests**

Add tests that:

Add these exact test names: `test_historical_daily_revision_forces_cold_rebuild`,
`test_appended_week_and_month_rows_do_not_invalidate_old_prefix`,
`test_baseline_config_change_forces_cold_rebuild`,
`test_corrupt_pickle_is_quarantined_before_cold_rebuild`,
`test_second_lock_holder_reloads_and_observes_first_extension`,
`test_failed_training_keeps_previous_cache_byte_for_byte`, and
`test_older_request_does_not_lower_watermark`.

The historical-revision test changes a value before the old watermark and expects `cold_build`; the append test adds only higher date/week/month rows and expects `extended` with one missing date.

- [ ] **Step 2: Run each new test and confirm failure**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest -v tests.test_liwei_0616_phase_a_cache
```

Expected: failures for missing prefix validation, quarantine, lock re-read, or atomic-write behavior.

- [ ] **Step 3: Implement deterministic fingerprints and recovery**

Implement `_frame_prefix_fingerprint(df, key, bound)`,
`_baseline_fingerprint(spec, baseline)`, `_runtime_fingerprint()`,
`_exclusive_lock(path)`, `_atomic_pickle_dump(path, payload)`, and
`_quarantine_invalid_cache(path)` as focused private helpers.

Use daily `date`, weekly `week_id`, and monthly `month_id` prefix bounds. Include schema, ABI, canonical baseline config, source anchor, horizon/purge gap, and Python/NumPy/pandas/LightGBM versions. Under the lock, always reload before deciding `missing_dates`. Write a temp file in the same directory, `flush`, `os.fsync`, validate, then `os.replace`.

- [ ] **Step 4: Run the cache tests and confirm all pass**

Run the Task 2 pytest command. Expected: all cache-store tests pass.

- [ ] **Step 5: Commit cache hardening**

```bash
git add shared/liwei_0616_phase_a_cache.py tests/test_liwei_0616_phase_a_cache.py
git commit -m "feat: harden liwei phase a cache lifecycle"
```

### Task 3: Wire the 5Y live scheme without changing core

**Files:**
- Modify: `schemes/liwei_0616_cons_sda_k3_div_k10/inference.py`
- Modify: `schemes/liwei_0616_cons_sda_k3_div_k10/predict.py`
- Modify: `tests/test_liwei_0616_cons_sda_k3_div_k10.py`

- [ ] **Step 1: Write failing 5Y live-cache wiring tests**

Patch `prepare_phase_a_caches` and call `run_5y01_for_feature_date` with the
existing dataframe arguments plus `use_incremental_cache=True`:

```python
cache_mock.return_value = ({"STD": {"test_dates": [feature_date], "results": []}}, audit)
row = inference.run_5y01_for_feature_date(
    **inference_kwargs,
    use_incremental_cache=True,
)
assert window_mock.call_args.kwargs["phase_a_caches"] == cache_mock.return_value[0]
assert row["phase_a_cache_audit"] == audit
```

Update the prediction test to require `use_incremental_cache=True` and the five top-level `phase_a_cache_*` keys in `PredictionRecord.extra`.

- [ ] **Step 2: Run the 5Y test file and confirm failure**

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest -v tests.test_liwei_0616_cons_sda_k3_div_k10
```

Expected: unexpected keyword or missing cache audit assertions.

- [ ] **Step 3: Add the 5Y cache callback and audit projection**

Add `use_incremental_cache: bool = False` and `cache_root` to the inference function. The callback must call unchanged core behavior:

```python
ctx = run_prediction(model_config(
    baseline,
    daily_df=daily_df,
    weekly_df=weekly_df,
    monthly_df=monthly_df,
    date_to_week=date_to_week,
    test_start=missing_ranges[0][0],
    test_end=missing_ranges[-1][1],
    test_ranges=missing_ranges,
    require_labels=False,
    emit_report=False,
    phase_a_only=True,
    n_workers=n_workers,
))
return ctx["phase_a_cache"]
```

Use cache family `liwei_0616_5y_v31`. In `predict.py`, pass `use_incremental_cache=True` and map the audit envelope into `extra`.

- [ ] **Step 4: Run 5Y tests and relevant StaticGate**

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest -v tests.test_liwei_0616_cons_sda_k3_div_k10
conda run --no-capture-output -n bond_factor_lab_service python -m harness gate static --scheme-id liwei_0616_cons_sda_k3_div_k10
```

Expected: tests and StaticGate pass; core remains free of pickle/file writes.

- [ ] **Step 5: Commit 5Y live integration**

```bash
git add schemes/liwei_0616_cons_sda_k3_div_k10/inference.py schemes/liwei_0616_cons_sda_k3_div_k10/predict.py tests/test_liwei_0616_cons_sda_k3_div_k10.py
git commit -m "feat: enable incremental cache for liwei 5y"
```

### Task 4: Wire and prove 7Y shared baselines

**Files:**
- Modify: `schemes/liwei_0616_7y01_cons_say_k3_div_k10/inference.py`
- Modify: `schemes/liwei_0616_7y01_cons_say_k3_div_k10/predict.py`
- Modify: `schemes/liwei_0616_7y03_cons_all_k3_div_k8/inference.py`
- Modify: `schemes/liwei_0616_7y03_cons_all_k3_div_k8/predict.py`
- Modify: `tests/test_liwei_0616_7y01_cons_say_k3_div_k10.py`
- Modify: `tests/test_liwei_0616_7y03_cons_all_k3_div_k8.py`

- [ ] **Step 1: Write failing shared-family and zero-retrain tests**

Assert both modules use cache family `liwei_0616_7y_v31`, canonical baseline configs match, and `required_baselines()` yields `STD`, `ACCWT`, `CROSS_5Y`, `DIV` for 7Y_01. With one temporary cache root, call the cache manager through 7Y_01 and then 7Y_03; the second trainer must record no dates.

- [ ] **Step 2: Run both 7Y test files and confirm failure**

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest -v tests.test_liwei_0616_7y01_cons_say_k3_div_k10 tests.test_liwei_0616_7y03_cons_all_k3_div_k8
```

Expected: missing cache family/wiring/audit assertions.

- [ ] **Step 3: Implement both 7Y adapters**

Repeat the Task 3 callback with each scheme's own core module, identical cache family, and `required_baselines()`. Preserve each scheme's original `PROD_CONFIG`, source test ranges, consensus, and streak K. Enable the cache only from live `predict.py`.

- [ ] **Step 4: Run 7Y tests and StaticGates**

Run both test files plus:

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m harness gate static --scheme-id liwei_0616_7y01_cons_say_k3_div_k10
conda run --no-capture-output -n bond_factor_lab_service python -m harness gate static --scheme-id liwei_0616_7y03_cons_all_k3_div_k8
```

Expected: all pass.

- [ ] **Step 5: Commit 7Y sharing**

```bash
git add schemes/liwei_0616_7y01_cons_say_k3_div_k10 schemes/liwei_0616_7y03_cons_all_k3_div_k8 tests/test_liwei_0616_7y01_cons_say_k3_div_k10.py tests/test_liwei_0616_7y03_cons_all_k3_div_k8.py
git commit -m "feat: share incremental cache across liwei 7y schemes"
```

### Task 5: Wire and prove 10Y shared baselines

**Files:**
- Modify: `schemes/liwei_0616_10y01_cons_say_k3_div_k10/inference.py`
- Modify: `schemes/liwei_0616_10y01_cons_say_k3_div_k10/predict.py`
- Modify: `schemes/liwei_0616_10y02_cons_say_k3_div_k5/inference.py`
- Modify: `schemes/liwei_0616_10y02_cons_say_k3_div_k5/predict.py`
- Modify: `tests/test_liwei_0616_10y01_cons_say_k3_div_k10.py`
- Modify: `tests/test_liwei_0616_10y02_cons_say_k3_div_k5.py`

- [ ] **Step 1: Write failing 10Y family and full-OOS preservation tests**

Assert both modules use `liwei_0616_10y_v61`, baseline configs match, and the 10Y_02 full-OOS `test_ranges=(("2024-01-01", feature_date),)` remains unchanged for the final full-window run. Verify the second 10Y scheme performs zero Phase A training after the first fills the shared cache.

- [ ] **Step 2: Run both 10Y tests and confirm failure**

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest -v tests.test_liwei_0616_10y01_cons_say_k3_div_k10 tests.test_liwei_0616_10y02_cons_say_k3_div_k5
```

Expected: missing cache wiring/family/audit assertions.

- [ ] **Step 3: Implement both 10Y adapters**

Use each scheme's own unchanged core callback, shared family `liwei_0616_10y_v61`, and required baselines `STD`, `ACCWT`, `V55_7Y`, `DIV`. Do not convert 10Y_02 full-OOS into the two-range window used by 10Y_01.

- [ ] **Step 4: Run 10Y tests and StaticGates**

Run both test files and both scheme StaticGates. Expected: all pass.

- [ ] **Step 5: Commit 10Y sharing**

```bash
git add schemes/liwei_0616_10y01_cons_say_k3_div_k10 schemes/liwei_0616_10y02_cons_say_k3_div_k5 tests/test_liwei_0616_10y01_cons_say_k3_div_k10.py tests/test_liwei_0616_10y02_cons_say_k3_div_k5.py
git commit -m "feat: share incremental cache across liwei 10y schemes"
```

### Task 6: Controlled prewarm entry point

**Files:**
- Create: `scripts/prewarm_liwei_0616_phase_a_cache.py`
- Create: `tests/test_prewarm_liwei_0616_phase_a_cache.py`

- [ ] **Step 1: Write failing prewarm tests**

Patch `scheduler.scheme_runner.run_scheme` and assert the script executes exactly:

```python
PREWARM_SCHEMES = (
    "liwei_0616_cons_sda_k3_div_k10",
    "liwei_0616_7y01_cons_say_k3_div_k10",
    "liwei_0616_10y02_cons_say_k3_div_k5",
)
```

10Y 代表必须选择完整 OOS 日期集合的 10Y_02；它是 10Y_01 两段窗口的超集，反向预热不能覆盖 10Y_02 的首次运行。

Assert returned JSON contains per-scheme cache status/watermark and the module never imports `scheduler.repository` or calls a persistence API.

- [ ] **Step 2: Run the prewarm test and confirm failure**

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest -v tests.test_prewarm_liwei_0616_phase_a_cache
```

Expected: module import failure.

- [ ] **Step 3: Implement prewarm CLI**

Expose:

```python
def prewarm(predict_date: str, run_scheme_fn=run_scheme) -> list[dict[str, Any]]:
    results = []
    for scheme_id in PREWARM_SCHEMES:
        records = run_scheme_fn(scheme_id, predict_date)
        cache = dict((records[0].get("extra") or {}).get("phase_a_cache") or {})
        results.append({"scheme_id": scheme_id, **cache})
    return results
```

`main()` accepts `--predict-date`, prints JSON, and performs no DB writes.

- [ ] **Step 4: Run the prewarm test and help output**

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest -v tests.test_prewarm_liwei_0616_phase_a_cache
conda run --no-capture-output -n forecast_env python -m scripts.prewarm_liwei_0616_phase_a_cache --help
```

Expected: tests pass and help documents `--predict-date`.

- [ ] **Step 5: Commit prewarm support**

```bash
git add scripts/prewarm_liwei_0616_phase_a_cache.py tests/test_prewarm_liwei_0616_phase_a_cache.py
git commit -m "feat: add liwei phase a cache prewarm"
```

### Task 7: Fidelity, platform gates, and status documentation

**Files:**
- Modify: `docs/CURRENT_STATUS.md`
- Verify all files modified above.

- [ ] **Step 1: Run all targeted unit tests**

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest -v \
  tests.test_liwei_0616_phase_a_cache \
  tests.test_liwei_0616_cons_sda_k3_div_k10 \
  tests.test_liwei_0616_7y01_cons_say_k3_div_k10 \
  tests.test_liwei_0616_7y03_cons_all_k3_div_k8 \
  tests.test_liwei_0616_10y01_cons_say_k3_div_k10 \
  tests.test_liwei_0616_10y02_cons_say_k3_div_k5 \
  tests.test_prewarm_liwei_0616_phase_a_cache
```

Expected: all pass.

- [ ] **Step 2: Run static/input/unit gates for all five schemes**

Run `python -m harness gate static`, `input`, and `unit` for each of the five
scheme IDs listed in the spec, using `bond_factor_lab_service` for static/input
and the configured `forecast_env` subprocess for unit execution. Expected: all
15 gate invocations pass.

- [ ] **Step 3: Prove cached versus uncached fidelity on consecutive live-safe dates**

Use an isolated temporary cache root. For one representative 5Y, 7Y, and 10Y baseline family:

1. Produce the first date with cold cache.
2. Produce the next date by extension.
3. Produce the same next date with cache disabled/reference full run.
4. Compare final direction, confidence, `vote_score`, all baseline score/sign fields, and target dates.

Expected: zero direction/sign mismatch and numeric tolerance no looser than existing CompareGate.

- [ ] **Step 4: Run Dry-run and CompareGate for all five schemes**

Use each scheme's existing benchmark-compatible predict date and current harness commands. Expected: all gates pass, protected DB table deltas remain zero.

- [ ] **Step 5: Update current status with measured evidence**

Add a dated entry that states only verified facts: cache families, cold/hit/extended behavior, zero old-date training calls, paired-scheme sharing, fidelity results, and exact test/gate commands.

- [ ] **Step 6: Run final repository checks**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only intended implementation/docs files plus the pre-existing untracked launchd plist.

- [ ] **Step 7: Commit verification documentation**

```bash
git add docs/CURRENT_STATUS.md
git commit -m "docs: record liwei incremental cache verification"
```

Do not merge or push to `master`. Production cache prewarm and scheduler restart remain separate operational actions unless explicitly authorized after review.
