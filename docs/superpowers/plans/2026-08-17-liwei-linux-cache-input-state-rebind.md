# Liwei Linux Cache Input-State Rebind Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reuse the seven deployed Liwei Phase A cache results while creating secure, immutable child generations bound to the real Linux input state, then prove normal cache hits without invoking training.

**Architecture:** A closed receipt contract binds each approved parent generation, target Linux input content ID, spec, publisher, and baseline integrity evidence. A process-local authorization context lets only the one-shot operator CLI request `migration_rebind`; the normal publisher and consumer APIs have no flag or environment waiver. The cache module creates a child generation with an empty affected scope and a fully preserved parent scope, and secure lineage verification accepts it only when every receipt and parent/child digest matches.

**Tech Stack:** Python 3.12/3.13, pandas/numpy, immutable JSON/pickle cache generations, pytest, systemd one-shot execution on Linux.

---

## File map

- Create `shared/liwei_0616_cache_migration.py`: closed receipt schema, canonical digest validation, and process-local authorization context.
- Modify `shared/liwei_0616_cache_contract.py`: admit the exact `migration_rebind` acceptance mode while continuing to reject the generic `rebind` spelling.
- Modify `shared/liwei_0616_phase_a_cache.py`: create and verify migration child generations without calling training callbacks.
- Create `scripts/rebind_liwei_0616_phase_a_cache.py`: load one exact receipt and run the seven approved publisher schemes without repository persistence.
- Create `tests/test_liwei_0616_cache_migration.py`: receipt/context/CLI unit tests.
- Modify `tests/test_liwei_0616_private_cache.py`: rebind generation, tamper rejection, no-training, and subsequent-hit tests.
- Create `deploy/cache_migrations/liwei-phase-a-linux-x86_64-20260817-v1.json`: exact seven-family ECS receipt after read-only Linux input-state capture.

### Task 1: Closed receipt contract and process-local authorization

**Files:**
- Create: `shared/liwei_0616_cache_migration.py`
- Create: `tests/test_liwei_0616_cache_migration.py`

- [ ] **Step 1: Write failing receipt validation tests**

Add fixtures with this exact public shape and assertions for digest, duplicate family, malformed SHA, and mutation rejection:

```python
def test_validate_rebind_receipt_round_trips_exact_record() -> None:
    receipt = _receipt()
    assert validate_cache_rebind_receipt(receipt) == receipt


@pytest.mark.parametrize(
    "mutation, match",
    [
        (lambda value: value["entries"].append(value["entries"][0]), "duplicate"),
        (lambda value: value["entries"][0].update(target_input_content_id="bad"), "sha256"),
        (lambda value: value.update(receipt_sha256="0" * 64), "digest"),
    ],
)
def test_validate_rebind_receipt_rejects_mutation(mutation, match) -> None:
    receipt = _receipt()
    mutation(receipt)
    with pytest.raises(ValueError, match=match):
        validate_cache_rebind_receipt(receipt)
```

- [ ] **Step 2: Run tests and verify the missing module failure**

Run:

```bash
pytest -q tests/test_liwei_0616_cache_migration.py
```

Expected: collection fails because `shared.liwei_0616_cache_migration` does not exist.

- [ ] **Step 3: Implement the minimal closed schema and context**

The module must expose only these production entry points; the receipt and entry validators use exact field sets and recompute both canonical digests before returning a deep copy:

```python
import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Iterator, Mapping

MIGRATION_REBIND_SCHEMA_VERSION = "liwei-0616-migration-rebind-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTIVE_RECEIPT: ContextVar[dict[str, object] | None] = ContextVar(
    "liwei_0616_active_cache_rebind_receipt",
    default=None,
)

def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"cache rebind {label} must be lowercase sha256")
    return value

def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"cache rebind {label} must be non-empty text")
    return value

def _validate_entry(raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise ValueError("cache rebind entry must be a mapping")
    value = deepcopy(dict(raw))
    if set(value) != {
        "cache_family", "tenor", "publisher_consumer_id",
        "spec_fingerprint", "parent_generation_id",
        "parent_manifest_sha256", "parent_generation_content_id",
        "parent_input_content_id", "target_input_content_id",
        "baselines", "entry_sha256",
    }:
        raise ValueError("cache rebind entry fields mismatch")
    for field in (
        "cache_family", "tenor", "publisher_consumer_id",
        "parent_generation_id",
    ):
        value[field] = _text(value[field], field)
    for field in (
        "spec_fingerprint", "parent_manifest_sha256",
        "parent_generation_content_id", "parent_input_content_id",
        "target_input_content_id",
    ):
        value[field] = _sha256(value[field], field)
    baselines = value["baselines"]
    if not isinstance(baselines, Mapping) or not baselines:
        raise ValueError("cache rebind baselines must be non-empty")
    for baseline, evidence in baselines.items():
        _text(baseline, "baseline")
        if not isinstance(evidence, Mapping) or set(evidence) != {
            "cache_content_sha256", "field_sha256",
            "test_date_count", "result_config_count",
        }:
            raise ValueError("cache rebind baseline evidence mismatch")
        _sha256(evidence["cache_content_sha256"], "cache content")
        fields = evidence["field_sha256"]
        if not isinstance(fields, Mapping) or set(fields) != {
            "test_dates", "results[].config", "results[].preds",
            "results[].probs",
        }:
            raise ValueError("cache rebind field evidence mismatch")
        for digest in fields.values():
            _sha256(digest, "field digest")
        for count in (evidence["test_date_count"], evidence["result_config_count"]):
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError("cache rebind evidence count is invalid")
    payload = {key: item for key, item in value.items() if key != "entry_sha256"}
    expected = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    if value["entry_sha256"] != expected:
        raise ValueError("cache rebind entry digest mismatch")
    return value

def validate_cache_rebind_receipt(
    raw: Mapping[str, object],
) -> dict[str, object]:
    value = deepcopy(dict(raw))
    if set(value) != {
        "schema_version", "migration_id", "entries", "receipt_sha256"
    }:
        raise ValueError("cache rebind receipt fields mismatch")
    if value["schema_version"] != MIGRATION_REBIND_SCHEMA_VERSION:
        raise ValueError("cache rebind receipt schema mismatch")
    if value["migration_id"] != "aliyun-linux-x86_64-20260817-v1":
        raise ValueError("cache rebind migration id mismatch")
    entries = value["entries"]
    if not isinstance(entries, list) or len(entries) != 7:
        raise ValueError("cache rebind receipt must contain seven entries")
    normalized = [_validate_entry(item) for item in entries]
    keys = [(item["cache_family"], item["tenor"]) for item in normalized]
    if keys != sorted(set(keys)):
        raise ValueError("cache rebind receipt contains duplicate entries")
    value["entries"] = normalized
    payload = {key: item for key, item in value.items() if key != "receipt_sha256"}
    expected = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    if value["receipt_sha256"] != expected:
        raise ValueError("cache rebind receipt digest mismatch")
    return value

@contextmanager
def authorized_cache_rebind(
    receipt: Mapping[str, object],
) -> Iterator[None]:
    validated = validate_cache_rebind_receipt(receipt)
    if _ACTIVE_RECEIPT.get() is not None:
        raise RuntimeError("cache rebind authorization is already active")
    token = _ACTIVE_RECEIPT.set(validated)
    try:
        yield
    finally:
        _ACTIVE_RECEIPT.reset(token)

def active_cache_rebind_entry(
    *, cache_family: str, tenor: str, publisher_consumer_id: str
) -> Mapping[str, object] | None:
    receipt = _ACTIVE_RECEIPT.get()
    if receipt is None:
        return None
    for entry in receipt["entries"]:
        if (
            entry["cache_family"] == cache_family
            and entry["tenor"] == tenor
            and entry["publisher_consumer_id"] == publisher_consumer_id
        ):
            return {
                "schema_version": receipt["schema_version"],
                "migration_id": receipt["migration_id"],
                "receipt_sha256": receipt["receipt_sha256"],
                "entry": deepcopy(entry),
            }
    return None
```

Use exact field sets, lowercase 64-character SHA256 validation, sorted unique `(cache_family, tenor)` keys, canonical JSON digest recomputation, and a `ContextVar`. Reject nested authorization contexts. Do not read an environment variable and do not expose a force/skip option.

- [ ] **Step 4: Add and pass context isolation tests**

```python
def test_authorized_cache_rebind_is_process_local_and_exact() -> None:
    assert active_cache_rebind_entry(
        cache_family="family", tenor="5Y", publisher_consumer_id="publisher"
    ) is None
    with authorized_cache_rebind(_receipt()):
        assert active_cache_rebind_entry(
            cache_family="family", tenor="5Y", publisher_consumer_id="publisher"
        )["target_input_content_id"] == "4" * 64
        assert active_cache_rebind_entry(
            cache_family="family", tenor="5Y", publisher_consumer_id="consumer"
        ) is None
```

Run: `pytest -q tests/test_liwei_0616_cache_migration.py`

Expected: all tests pass.

- [ ] **Step 5: Commit the receipt contract**

```bash
git add shared/liwei_0616_cache_migration.py tests/test_liwei_0616_cache_migration.py
git commit -m "feat(cache): add closed migration rebind receipt"
```

### Task 2: Immutable migration child generation

**Files:**
- Modify: `shared/liwei_0616_cache_contract.py`
- Modify: `shared/liwei_0616_phase_a_cache.py`
- Modify: `tests/test_liwei_0616_private_cache.py`

- [ ] **Step 1: Write a failing no-training rebind test**

Build a normal parent fixture, compute a second input state from a one-value input revision, bind both states in a valid receipt, and call the normal publisher under `authorized_cache_rebind`:

```python
with authorized_cache_rebind(receipt):
    caches, audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=revised_daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=(("2024-01-02", "2024-01-02"),),
        train_missing=forbidden_train,
        cache_consumer_id="publisher",
        cache_root=root,
    )

assert audit["build_mode"] == "migration_rebind"
assert train_calls == []
assert _semantic_cache_hashes(caches) == parent_hashes
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `pytest -q tests/test_liwei_0616_private_cache.py -k migration_rebind`

Expected: failure because `migration_rebind` is not an accepted build mode and no migration branch exists.

- [ ] **Step 3: Add the minimal generation creation branch**

In `prepare_phase_a_caches`, after the input state and current generation are loaded but before ordinary build-mode selection:

```python
entry = active_cache_rebind_entry(
    cache_family=spec.cache_family,
    tenor=spec.tenor,
    publisher_consumer_id=cache_consumer_id,
)
if entry is not None:
    return _prepare_migration_rebind(
        spec=spec,
        family_root=family_root,
        current=current,
        requested_by_baseline=requested_by_baseline,
        input_state=input_state,
        input_change=input_change,
        receipt_entry=entry,
    )
```

`_prepare_migration_rebind` must secure-verify the parent lineage, compare every receipt identity and baseline integrity field, require all requested dates to be covered, require an unqualified parent, and create scopes with `affected_dates=[]` and all parent dates preserved. It builds fresh unqualified integrity evidence from the reused caches, creates the child, verifies the staged lineage, then uses the existing atomic current-pointer switch. It never receives or invokes `train_missing`.

- [ ] **Step 4: Extend acceptance and secure lineage narrowly**

Add only `"migration_rebind"` to `_BUILD_MODES`; keep `"rebind"` invalid. Add optional `migration_rebind_evidence` to rebind manifests. In `_verify_generation_acceptance_lineage`, recognize the special mode only after checking:

```python
if manifest.get("build_mode") == "migration_rebind":
    _verify_migration_rebind_evidence(
        generation=generation,
        parent=parent,
        spec=spec,
    )
    expected_mode = "migration_rebind"
else:
    if manifest.get("migration_rebind_evidence") is not None:
        raise ValueError("non-migration generation carries rebind evidence")
    expected_mode = _lineage_build_mode(
        parent=parent,
        generation=generation,
        input_change=recomputed_change,
        spec=spec,
    )
```

The verifier must compare parent/child semantic and field hashes, exact full-date preserved scopes, receipt-entry digest, parent bindings, spec, target input content ID, and the unqualified Compare status.

- [ ] **Step 5: Add tamper and repeat-hit tests**

Cover receipt target mismatch, parent manifest mismatch, cache value tamper, missing migration evidence, nonempty affected scope, and a normal second call:

```python
caches, second_audit = prepare_phase_a_caches(
    spec=spec,
    daily_df=revised_daily,
    weekly_df=weekly,
    monthly_df=monthly,
    test_ranges=(("2024-01-02", "2024-01-02"),),
    train_missing=forbidden_train,
    cache_consumer_id="publisher",
    cache_root=root,
)
assert second_audit["build_mode"] == "hit"
assert train_calls == []
```

Run:

```bash
pytest -q tests/test_liwei_0616_private_cache.py -k 'migration_rebind or rejects_generation_binding'
pytest -q tests/test_liwei_0616_private_cache.py
```

Expected: focused and full cache test file pass.

- [ ] **Step 6: Commit the generation behavior**

```bash
git add shared/liwei_0616_cache_contract.py shared/liwei_0616_phase_a_cache.py tests/test_liwei_0616_private_cache.py
git commit -m "feat(cache): support exact Linux input-state rebind"
```

### Task 3: One-shot operator CLI

**Files:**
- Create: `scripts/rebind_liwei_0616_phase_a_cache.py`
- Modify: `tests/test_liwei_0616_cache_migration.py`

- [ ] **Step 1: Write failing CLI orchestration tests**

Patch `scheduler.scheme_runner.run_scheme` and assert exact publisher order, one prediction date, no repository/executor call, receipt context visibility, and JSON summary output:

```python
def test_cli_runs_only_receipt_publishers(monkeypatch, tmp_path, capsys) -> None:
    calls = []
    monkeypatch.setattr(module, "run_scheme", lambda scheme_id, day: calls.append((scheme_id, day)) or [_hit_record()])
    assert module.main(["--receipt", str(_write_receipt(tmp_path)), "--predict-date", "2026-08-17"]) == 0
    assert calls == [("publisher", "2026-08-17")]
    assert json.loads(capsys.readouterr().out)["training_calls"] == 0
```

- [ ] **Step 2: Run and verify missing script failure**

Run: `pytest -q tests/test_liwei_0616_cache_migration.py -k cli`

Expected: import failure because the operator script does not exist.

- [ ] **Step 3: Implement the narrow CLI**

The CLI accepts exactly `--receipt` and `--predict-date`. It validates the root-owned receipt, enters `authorized_cache_rebind`, and invokes each receipt entry's exact publisher through `scheduler.scheme_runner.run_scheme`. It validates each returned record's `extra.phase_a_cache` audit has `build_mode=migration_rebind`, the expected family, and no missing dates. It prints a JSON summary containing only non-secret IDs, hashes, cache modes, and elapsed seconds. It never imports `scheduler.repository` or `scheduler.executor`.

- [ ] **Step 4: Pass CLI and contract tests**

Run: `pytest -q tests/test_liwei_0616_cache_migration.py`

Expected: all tests pass.

- [ ] **Step 5: Commit the CLI**

```bash
git add scripts/rebind_liwei_0616_phase_a_cache.py tests/test_liwei_0616_cache_migration.py
git commit -m "feat(cache): add one-shot Liwei rebind command"
```

### Task 4: Build and pin the exact ECS receipt

**Files:**
- Create: `deploy/cache_migrations/liwei-phase-a-linux-x86_64-20260817-v1.json`

- [ ] **Step 1: Capture target input IDs without training or DB writes**

On ECS, run each approved publisher through a read-only wrapper that intercepts `prepare_phase_a_caches` after `_input_generation_state` and before build selection. Record only family, tenor, publisher, spec fingerprint, exact current parent identities, target Linux input content ID, and parent baseline integrity evidence.

Expected: seven unique entries; no generation directory, current pointer, database table, run row, or timer changes.

- [ ] **Step 2: Create the canonical receipt**

Use `canonical_json_bytes` from `shared.liwei_0616_cache_migration` to calculate `receipt_sha256`, then add the exact JSON with `apply_patch`. Do not include host credentials, DSN, server UUID, or prediction values.

- [ ] **Step 3: Validate the pinned receipt locally and on ECS**

Run:

```bash
python -c 'import json; from pathlib import Path; from shared.liwei_0616_cache_migration import validate_cache_rebind_receipt; p=Path("deploy/cache_migrations/liwei-phase-a-linux-x86_64-20260817-v1.json"); print(validate_cache_rebind_receipt(json.loads(p.read_text()))["receipt_sha256"])'
```

Expected: one 64-character SHA256 and seven entries.

- [ ] **Step 4: Commit the exact receipt**

```bash
git add deploy/cache_migrations/liwei-phase-a-linux-x86_64-20260817-v1.json
git commit -m "deploy(cache): pin ECS Linux rebind receipt"
```

### Task 5: Regression, deployment, and cache-hit proof

**Files:**
- Modify: `docs/operations/ALIYUN_MIGRATION_ASSESSMENT.md` only if the final operator evidence must be recorded there.

- [ ] **Step 1: Run local regression**

Run:

```bash
pytest -q tests/test_liwei_0616_cache_migration.py tests/test_liwei_0616_private_cache.py
pytest -q tests/test_scheme_runner.py tests/test_executor.py tests/test_repository_registry.py
git diff --check
```

Expected: all selected tests pass and no whitespace errors.

- [ ] **Step 2: Build and install a new exact release**

Archive the exact commit, verify the archive/release SHA and clean tree, extract to `/opt/bond-factor-lab/releases/<exact-commit>`, and atomically move `/opt/bond-factor-lab/current` only after import and receipt validation succeed. Keep all timers disabled/inactive.

- [ ] **Step 3: Execute the one-shot rebind**

Run the CLI with `forecast_env`, the isolated cache root `/var/lib/bond-factor-lab/cache-builds/linux-x86_64-20260817-v1/liwei_0616`, the exact receipt, and predict date `2026-08-17` under a transient systemd service with a bounded timeout. Do not start a timer.

Expected: seven `migration_rebind` results, zero training calls, no failed process, and seven new immutable child generations.

- [ ] **Step 4: Verify 7/7 normal cache hits**

Without any authorization context, run the normal publisher/consumer cache path and read back:

- all seven current pointers target child generations;
- all seven secure lineage checks pass;
- parent and child semantic/field hashes are identical;
- every audit is `status=hit`, `build_mode=hit`, with no missing dates;
- no `.prewarmer.lock` holder, build process, OOM, or failed rebind unit exists;
- every Bond Factor Lab timer is still disabled/inactive.

- [ ] **Step 5: Continue manual DB-write deployment**

Start only the already authorized one-shot prediction services, serially. For each cadence, read back run/log/prediction evidence before advancing. Stop on the first failure. Confirm the nine paused Native schemes never run, and close any failed run atomically without deleting audit history.

- [ ] **Step 6: Final verification and evidence commit**

Run fresh local regression plus ECS read-only current/lineage/timer/process/DB queries. If the migration assessment is updated, commit only that evidence file after `git status --short`, `git diff --check`, and staged-file review.
