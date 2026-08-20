# Scheduled Live Prediction Immutability Design

**Status:** Approved design (方案 A)

**Goal:** Once a live prediction business key has been persisted, no later rerun, source-data revision, code revision, or retry may change that prediction row.

## Scope

This change applies only to live writes into `t_scheme_predictions` through the Native and Blackbox active completion paths.

It does not change:

- source-data or actual-value upserts;
- input-artifact persistence;
- backtest tables;
- cache revision or suffix-rebuild behavior;
- gray-gap authorization or target selection;
- database schema or existing prediction rows;
- Mac3/ECS scheduling configuration.

The existing unique key remains the database authority for prediction identity:

```text
(scheme_id, target_tenor, horizon, target_date)
```

## Confirmed Root Cause

`_insert_run_predictions_conn` currently defaults to UPSERT. Normal Native completion uses that default, and normal fresh Blackbox completion asks for UPSERT. A repeated run for an existing business key can therefore update prediction direction, confidence, run lineage, version, dates, phase, model metadata, and `extra`.

The cache design does not protect persisted predictions. It only controls which training/cache suffix is recomputed.

## Considered Approaches

### A. Repository transaction classifies conflicts and inserts only (selected)

The completion transaction validates returned records, reads the existing business keys, classifies the set as `none`, `all`, or `partial`, and then completes the run according to the approved state table below. The final prediction SQL is plain INSERT.

Advantages:

- enforces the rule at the only prediction write boundary;
- gives Native and Blackbox identical behavior;
- reuses the existing unique key and lifecycle/Registry transaction locks;
- needs no schema migration, trigger, ledger, or new service;
- preserves an auditable run and run-log outcome.

### B. Executor preflight before running the algorithm (rejected)

This could save compute for obvious duplicates, but the executor does not yet have every authoritative `target_date` produced by all daily, weekly, and monthly algorithms. It would duplicate date semantics above the repository and still require database protection for races.

An early optimization may be considered separately after correctness is closed, but it is not part of this change.

### C. Database trigger or append-only history table (rejected)

A trigger could reject UPDATE, and a new history table could retain multiple attempts. Both introduce schema and operational complexity without improving the required behavior. The current unique key plus insert-only repository logic is sufficient.

## Approved State Machine

For the complete set of validated records returned by one scheme run:

| Existing business keys | Prediction writes | `t_scheme_runs.status` | `t_scheme_run_log.status` | Meaning |
|---|---:|---|---|---|
| None | All records, plain INSERT | `success` | `success` | First valid publication |
| All | 0 | `skipped` | `skipped` | Idempotent duplicate; existing predictions remain authoritative |
| Some, but not all | 0 | `failed` | `failed` | Inconsistent partial batch; operator investigation required |

For both `skipped` and `failed` outcomes:

- `records_returned` records what the algorithm returned;
- `records_written` is exactly `0`;
- `finished_at` is set;
- `error_message` / run-log reason is stable and non-secret;
- no existing prediction field, including `updated_at`, is changed.

Stable reason codes:

```text
prediction_keys_already_exist
partial_prediction_key_conflict
```

The partial-conflict message may additionally include the sorted existing and missing business keys for operator diagnosis.

## Transaction and Concurrency Design

The decision remains inside the existing Native or Blackbox completion transaction, after all current checks have passed:

1. Revalidate the exact active scheme version, active Registry target set, running run identity, prediction phase, returned target set, dates, and scheme version.
2. While the current lifecycle/Registry database locks are held, derive every prediction business key from the validated `PredictionRecord` objects.
3. Read those keys from `t_scheme_predictions` with row locking where supported.
4. Classify the result as none, all, or partial.
5. Apply exactly one state-machine branch.
6. Finish `t_scheme_runs` and append `t_scheme_run_log` in the same transaction.

The existing unique key remains the final race barrier. Plain INSERT must never contain `ON DUPLICATE KEY UPDATE` or `ON CONFLICT ... DO UPDATE`. An unexpected duplicate-key error rolls the transaction back and is reported as a failed run; it must never be converted into an UPDATE.

No new lock table or distributed lock is introduced.

## Code Shape

The implementation should remain local to the current write boundary:

- add one private repository helper that derives/classifies existing business keys;
- make `_insert_run_predictions_conn` unconditionally insert-only and remove its `insert_only` switch;
- make both Native and Blackbox completion return `(status, records_written, error_message)`;
- update the executor to propagate the returned completion status instead of assuming Blackbox success;
- reuse `_finish_scheme_run_conn` and `_write_run_log_conn` for all three terminal states.

The gray-gap path remains stricter: any pre-existing gray-gap business key continues to reject the entire authorized gap group. Its behavior is not broadened to `skipped` by this change.

## Error Handling

- Invalid record sets, dates, versions, phases, or Registry state continue to fail before conflict classification.
- A full duplicate is not an exception; it atomically completes as `skipped`.
- A partial duplicate is not allowed to reach INSERT; it atomically completes as `failed` with zero writes.
- An unexpected database error rolls back prediction, run completion, and run log together; the existing executor failure-audit path records the failed attempt in a separate transaction.
- The implementation must not compare prediction values to decide whether replacement is safe. Even byte-identical reruns are `skipped`, because immutability is based on business identity rather than value equality.

## Verification Contract

Repository tests must cover Native and Blackbox independently:

1. First publication inserts all expected targets and completes `success`.
2. Full duplicate completes `skipped`, writes zero rows, and leaves every field of the original rows unchanged.
3. Full duplicate remains `skipped` even when the rerun returns a different direction, confidence, version-derived metadata, or `extra`.
4. Partial duplicate completes `failed`, writes zero rows, and leaves both the existing row and the missing target absent.
5. Returned duplicate targets and Registry mismatches still fail before business-key classification.
6. An injected INSERT failure rolls back predictions, run status, and run log.
7. Gray-gap duplicate rejection remains unchanged.
8. Blackbox precommit validation still executes only on the first-publication success path and rolls the transaction back on failure.

Executor/control-plane tests must verify that:

- Native and Blackbox `skipped` results propagate to the one-shot summary;
- partial conflicts propagate as `failed` and cause the batch/unit failure behavior already used for scheme failures;
- no scheduler or systemd configuration is required for the database guarantee.

After focused tests pass, run the complete suite. No production write is needed to prove the repository contract locally. ECS installation and a disposable isolated database smoke test are separate follow-up operations after code review and release promotion approval.

## Acceptance Criteria

The design is complete when all of the following are true:

- no live prediction code path contains prediction UPSERT behavior;
- a second completion for the same full business-key set cannot change any existing prediction column;
- all-existing, none-existing, and partial-existing outcomes exactly match the approved state table;
- run status, run log, and prediction writes are atomic for each outcome;
- Native, Blackbox, and gray-gap regression tests pass;
- the full local test suite passes;
- no database migration, new table, new daemon, new branch, or new scheduling control plane is introduced.
