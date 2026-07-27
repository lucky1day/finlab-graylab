# Blackbox Scheduler Admission MVP Design

## Goal

Allow an active Blackbox V2 scheme to remain visible in the gray lab and to be
run manually without automatically granting it a legacy APScheduler job.

## Design

Add one version-controlled control file,
`deploy/blackbox_scheduler_admission_v1.json`, keyed by exact
`scheme_id + scheme_version`.

- `formal` entries may be registered and executed by the scheduler.
- `gray` entries remain visible and manually executable but are excluded from
  scheduler registration, startup catch-up, and scheduled execution.
- A Blackbox V2 identity absent from the file is denied automatic scheduling.
- Native schemes keep their current behavior.
- The control file must equal the code-owned closed set of fourteen exact
  `scheme_id + scheme_version -> mode` entries. Missing, extra, mode-drifted,
  or version-drifted entries invalidate the policy.
- The fourteen base scheme IDs are reserved Blackbox identities. Reclassifying one
  as Native cannot bypass admission and is denied with a critical audit event.
- A missing or invalid admission file fails closed for all automatic Blackbox
  execution while Native jobs, actuals, and health/watchdog jobs keep their
  current behavior.

The current five production Blackbox schemes that were already scheduled before
this batch are frozen as `formal`. The five FengRL monthly schemes and four
10Y/T+5 daily schemes are frozen as `gray`.

## Enforcement points

1. `build_scheduler()` filters automatic prediction jobs before APScheduler
   registration.
2. `run_startup_prediction_catchup()` filters gray Blackbox schemes.
3. `run_scheduled_prediction_job()` performs one discovery, reloads and checks
   that exact discovered identity, then executes the same immutable
   `SchemeConfig`, providing a stale-job/revocation fence without a second
   discovery race.
4. Manual `run_prediction_job()` remains unchanged.

## Acceptance

- Existing formal Blackbox jobs remain present.
- All nine gray schemes remain active in Registry/API but have no
  scheduler job and cannot be executed through the scheduled wrapper.
- Formal daily policy, capacity candidate, isolated replay, and DailyRuntime
  select only the existing 21-item/25-target formal set from active discovery.
- Version drift and missing identities are denied.
- No migration, backend API, Registry mutation, scheduler restart, deployment,
  or BondProjectPro change is included.
