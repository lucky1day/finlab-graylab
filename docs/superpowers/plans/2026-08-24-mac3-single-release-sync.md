# Mac3 Single Release Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. This task is executed inline because the user explicitly requested planning followed by uninterrupted execution.

**Status:** COMPLETE — release、15 条缺口 Prediction、20 条本地权威周期 Actual 与公网前端均已闭环。

**Goal:** Promote the exact ECS-validated `053d562fc40d7ecf5596f56f1beb00e4a3b58178` immutable source release to Mac3 once, reconcile the M0 monthly/quarterly/annual live gaps without copying source database identities, and verify the public frontend.

**Architecture:** Rebuild the deterministic archive from the exact Git commit and require the already verified ECS archive SHA-256 `58dd3398d3e85cd3f91497eb4f4b5113749c010780b183c14b215e8ffea4dadf`. Preinstall it into `/Users/macstudio0/bond-factor-lab-production/releases`, stop only the affected Mac3 writer plus the backend during cutover, atomically activate the release, and use the target release's `scheduler.repository` insert-only path to create new Mac3 runs without copying ECS database IDs. The initial annual-only sync was followed by a complete M0 diff: five monthly and five quarterly Predictions were also missing and were imported through the same boundary. Actuals were regenerated from Mac3's local authoritative daily source rather than copied across hosts. Existing installed launchd plists remain unchanged.

**Tech Stack:** deterministic Git archive, immutable source release installer, launchd one-shot control plane, Python 3.12, SQLAlchemy repository, MySQL 8, FastAPI, public HTTPS frontend.

---

### Task 1: Read-only identity and gap preflight

**Files:**
- Read: `/opt/bond-factor-lab/current/.bfl-release.env` on ECS
- Read: `/Users/macstudio0/bond-factor-lab-production/current/.bfl-release.env` on Mac3
- Read: ECS and Mac3 `t_scheme_versions`, `t_scheme_registry`, `t_scheme_predictions`
- Read: `/Users/macstudio0/Library/LaunchAgents/com.bond-factor-lab.monthly-predictions.plist`

- [x] Confirm ECS `current` is `053d562fc40d7ecf5596f56f1beb00e4a3b58178`, Mac3 `current` is `3adcc96f790c740e2052b3f6283704829d30f5d9`, and the latter is an ancestor of the former.
- [x] Confirm both databases are independently identified as `bond_db`, all five annual scheme versions are exact and active, ECS contains exactly five source rows for target date `2026-02-14`, and Mac3 contains none.
- [x] Run the existing read-only launchd drift audit and confirm the installed control plane remains compatible.

### Task 2: Build and preinstall the one approved release

**Files:**
- Read: Git commit `053d562fc40d7ecf5596f56f1beb00e4a3b58178`
- Create temporarily: deterministic archive and manifest under a private `mktemp -d` directory
- Create: `/Users/macstudio0/bond-factor-lab-production/releases/053d562fc40d7ecf5596f56f1beb00e4a3b58178`

- [x] Create a detached temporary worktree at the exact commit and run `scripts/build_source_release.py` from that clean tree.
- [x] Require archive SHA-256 `58dd3398d3e85cd3f91497eb4f4b5113749c010780b183c14b215e8ffea4dadf` and manifest commit `053d562fc40d7ecf5596f56f1beb00e4a3b58178`.
- [x] Run the candidate version of `scripts/install_source_release.py` without `--activate` against `/Users/macstudio0/bond-factor-lab-production` and `/Users/macstudio0/bond-factor-lab-runtime`.

### Task 3: Single cutover and initial annual five-row import

**Files:**
- Modify operationally: `/Users/macstudio0/bond-factor-lab-production/current`
- Modify operationally: Mac3 `t_scheme_runs`, `t_scheme_run_log`, and `t_scheme_predictions` only through `scheduler.repository`
- Read: `/Users/macstudio0/bond-factor-lab-runtime/config/service.env`

- [x] Export from ECS only `scheme_id`, `target_tenor`, `horizon`, `predict_date`, `feature_date`, `target_date`, `predicted_direction`, `confidence`, `scheme_version`, and necessary `extra`; exclude primary keys, ECS `run_id`, Actuals, backtests, and Harness history.
- [x] Boot out `com.bond-factor-lab.monthly-predictions` so the target annual writer cannot race the import; boot out the backend for the release cutover.
- [x] Activate the preinstalled release once with `--expected-current 3adcc96f790c740e2052b3f6283704829d30f5d9`.
- [x] Under the activated exact release, recheck all five Mac3 business keys are absent, create fresh Mac3 `gray_live` runs, and call `complete_gray_gap_run` once per exact scheme. Any existing key rejects the corresponding group; no update or overwrite is allowed.
- [x] Bootstrap the unchanged installed backend and monthly-predictions launchd plists.

### Task 4: Fresh production verification

**Files:**
- Read: Mac3 immutable release/install records and launchd state
- Read: Mac3 database and `http://127.0.0.1:8100`
- Read: `https://bond.finailab.cn/bond-factor-lab/`

- [x] Confirm `current=053d562fc40d7ecf5596f56f1beb00e4a3b58178` and `previous=3adcc96f790c740e2052b3f6283704829d30f5d9`, with the expected archive and installed source-tree identities.
- [x] Confirm five Mac3 prediction rows exist with fresh local run IDs and exact dates, directions, versions, and `gray_live` phase; confirm no 2026 annual Actual was fabricated.
- [x] Confirm backend is running, monthly writer is loaded/waiting, all other installed jobs remain loaded, and the read-only drift audit passes.
- [x] Confirm local health/dashboard and public HTTPS page/dashboard return successfully; confirm public JS/CSS are byte-identical to Mac3 local assets and the public annual-average 2026 live-row semantics match the local Dashboard exactly (`02/13`, target year `2026`, copied direction, Actual null/`待验证`).

### Task 5: Follow-up complete M0 reconciliation

**Files:**
- Read: ECS loopback and Mac3 public Dashboard payloads
- Modify operationally: Mac3 `t_scheme_runs`, `t_scheme_run_log`, `t_scheme_predictions`, and `t_scheme_period_average_actuals`
- No source-code or release change

- [x] Compare every M0 live business key across ECS and Mac3. The initial diff was exactly ten ECS-only Predictions: five monthly target-month `2026/06` rows and five quarterly target-quarter `2026/Q2` rows.
- [x] Stop the Mac3 monthly Prediction and Actuals LaunchAgents, verify exact release/config/version/Registry identity and all ten target keys are absent, then import the ten Predictions through `create_scheme_run` plus `complete_gray_gap_run`. Local run IDs are `3685–3694`; no source primary key, `run_id`, Actual, backtest, or Harness history was copied.
- [x] Build twenty due period-average Actual records from Mac3's authoritative daily source, compare their keys and values with the ECS read-only reference (`20/20` exact), and persist them only through `upsert_period_average_actuals`.
- [x] Restore the unchanged installed LaunchAgents. Backend remains running; monthly Prediction and Actuals jobs are loaded and idle/waiting.
- [x] Final Dashboard comparison: `M0_ECS_LIVE=100`, `M0_PUBLIC_MAC3_LIVE=100`, `ECS_ONLY=0`, `MAC3_ONLY=0`, `VALUE_DIFF=0`.
- [x] Browser verification: quarterly-average summary/trend contains both `2026/Q2` and `2026/Q3`; Q2 has Actual and a `1/1` verified sample, while Q3 remains pending.

### Closure

- ECS and Mac3 `current` both equal `053d562fc40d7ecf5596f56f1beb00e4a3b58178`; no second release was published.
- Mac3 `previous` remains `3adcc96f790c740e2052b3f6283704829d30f5d9`; ECS `previous` remains `86e9f32cda9d1cb573a14d202ed376c995e249f0`.
- Archive SHA-256 is `58dd3398d3e85cd3f91497eb4f4b5113749c010780b183c14b215e8ffea4dadf`; installed source-tree SHA-256 is `1c01ed4b67c657a3eb456dad39b0ccd4d2b5c3ec00a560519e6f2f7fd43adf05` on both hosts.
- Public acceptance URL: `https://bond.finailab.cn/bond-factor-lab/`.
