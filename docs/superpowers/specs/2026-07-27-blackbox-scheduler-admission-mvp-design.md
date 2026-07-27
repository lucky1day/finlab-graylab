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
- Invalid admission files fail scheduler startup rather than silently widening
  permission.

The current five production Blackbox schemes that were already scheduled before
this batch are frozen as `formal`. The five FengRL monthly schemes are frozen as
`gray`.

## Enforcement points

1. `build_scheduler()` filters automatic prediction jobs before APScheduler
   registration.
2. `run_startup_prediction_catchup()` filters gray Blackbox schemes.
3. `run_scheduled_prediction_job()` reloads and checks the exact identity before
   execution, providing a stale-job/revocation fence.
4. Manual `run_prediction_job()` remains unchanged.

## Acceptance

- Existing formal Blackbox jobs remain present.
- All five FengRL monthly schemes remain active in Registry/API but have no
  scheduler job and cannot be executed through the scheduled wrapper.
- Version drift and missing identities are denied.
- No migration, backend API, Registry mutation, scheduler restart, deployment,
  or BondProjectPro change is included.
