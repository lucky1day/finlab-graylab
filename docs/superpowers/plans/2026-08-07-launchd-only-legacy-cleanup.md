# Launchd-only legacy cleanup Implementation Plan

**状态**：`COMPLETE`（2026-08-07）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Remove retired A/C code without changing the launchd-only production control plane or deleting live scheme, gray-gap, DataBridge, migration, or database capabilities.

**Architecture:** Delete only closed dead-code components. Before deleting C's coordinator closure, extract the two live generic primitives and decouple current DataBridge publication validation from occurrence/epoch state. Keep normal launchd one-shot execution, manual direct scheduling, input generation lineage, migrations, and installed plist state untouched.

**Tech Stack:** Python 3.12, `unittest`, `pytest`, SQLAlchemy read-only diagnostics, launchd plist readback, JSON policy files, Git.

---

## Evidence and scope lock

- [x] Reproduce the baseline: `python -m unittest discover -s tests -p 'test_*.py'` ran 1,680 tests and
  reported exactly three CGB admission-parity failures.
- [x] Review all A candidates by module stem, not only filename: four reproduction runners and the
  weekly-average adapter are live and excluded; nine scripts form one 2,892-line closed deletion set.
- [x] Read production entrypoints and state: natural cadence writers use
  `scheduler.launchd_prediction_runner`; `t_schedule_occurrences` is empty; stale installed plists are
  disabled/unloaded and out of scope.
- [x] Record the design in
  `docs/superpowers/specs/2026-08-07-launchd-only-legacy-cleanup-design.md`.

### Task 1: P0 — synchronize CGB's zero-capability exact identity

**Files:**

- Modify: `scheduler/blackbox_scheduler_admission.py`
- Modify: `deploy/blackbox_scheduler_admission_v1.json`
- Modify: `tests/test_blackbox_scheduler_admission.py`
- Test: `tests/test_blackbox_scheduler_admission.py`
- Test: `tests/test_launchd_prediction_runner.py`

- [x] **Step 1: Make the expected current identity explicit in the admission test.**

  In `EXPECTED_ADMISSIONS`, replace only the V128 key:

  ```python
  (
      "cgb_causal_wk_1y_v128",
      "59415aa789c5",
  ): _expected_admission(
      mode="gray",
      frequency="weekly",
      task_type="weekly_point",
      horizon=1,
      target_tenor="1Y",
      capabilities=NO_CAPABILITIES,
  ),
  ```

  Run:

  ```bash
  PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n bond_factor_lab_service python -m unittest \
    tests.test_blackbox_scheduler_admission.BlackboxSchedulerAdmissionTests.test_exact_control_plane_permission_matrix
  ```

  Expected: failure because the code/JSON still expose `ee921f65476c`.

- [x] **Step 2: Synchronize the two policy sources without adding a capability.**

  Replace the same V128 version in `EXPECTED_EXACT_ADMISSIONS` and the matching JSON row. Keep all
  metadata unchanged and keep `"capabilities": []`; do not add `launchd_one_shot`,
  `direct_scheduled`, or `daily_ledger`.

- [x] **Step 3: Verify the P0 contract and commit.**

  Run the two exact-admission tests plus
  `LaunchdPredictionRunnerTests.test_real_active_scope_three_cadences_use_no_write_control_plane_simulation`.
  Confirm that V128 is excluded from one-shot execution rather than treated as a malformed admission.
  Commit only these three files with `fix: align CGB gray admission identity`.

### Task 2: A — delete the isolated scripts closure

**Files:**

- Delete: `scripts/build_liwei_5y_all_k10_gray_benchmarks.py`
- Delete: `scripts/postonboard_common.py`
- Delete: `scripts/prewarm_liwei_0616_phase_a_cache.py`
- Delete: `scripts/rebuild_daily0529_scheme_benchmarks.py`
- Delete: `scripts/rebuild_daily_0629_benchmarks.py`
- Delete: `scripts/rebuild_monthly_0629_benchmarks.py`
- Delete: `scripts/rebuild_weekly_average_0529_benchmarks.py`
- Delete: `scripts/refresh_liwei_cache_spec_fingerprints.py`
- Delete: `scripts/verify_frontend_db.py`

- [x] **Step 1: Preserve the scope guard.**

  Do not delete `backtests/daily_0529_reproduction.py`, `backtests/daily_0629_reproduction.py`,
  `backtests/monthly_0629_reproduction.py`, `backtests/weekly_avg_lgbm_0529_reproduction.py`, or
  `shared/weekly_average_lgbm_predict_adapter.py`; each is imported by an active scheme or its runner.

- [x] **Step 2: Delete the exact closed set with a patch.**

  Delete all nine files in one `apply_patch` operation. The only internal edge,
  `verify_frontend_db.py → postonboard_common.py`, disappears in the same patch.

- [x] **Step 3: Prove absence and run the safe regression set.**

  Run `git grep -n -E` for all nine module stems and require no output/exit 1. Run
  `python -m compileall -q backend backtests harness scheduler shared schemes scripts`, then the full
  service `unittest` suite. Commit only the nine deletes with `chore: remove retired utility scripts`.

### Task 3: C1a — extract live generic lock and runtime-path primitives

**Files:**

- Create: `shared/exclusive_file_lock.py`
- Create: `shared/runtime_paths.py`
- Modify: `scheduler/daily_coordinator.py`
- Modify: `harness/gates/signal_gap_fill_gate.py`
- Modify: `shared/daily_storage_preflight.py`
- Test: `tests/test_signal_gap_fill_gate.py`
- Test: `tests/test_daily_storage_preflight.py`
- Test: coordinator lock tests moved or rewritten as `tests/test_exclusive_file_lock.py`

- [x] **Step 1: Create a failing lock import/use test.**

  Test that the generic lock rejects a second holder and safely releases after the first context exits:

  ```python
  from shared.exclusive_file_lock import ExclusiveFileLock, ExclusiveFileLockUnavailable

  with ExclusiveFileLock(lock_path):
      with self.assertRaises(ExclusiveFileLockUnavailable):
          with ExclusiveFileLock(lock_path):
              pass
  ```

- [x] **Step 2: Move the current inode-safe file-lock implementation verbatim in behavior.**

  Export `ExclusiveFileLock` and `ExclusiveFileLockUnavailable` from `shared/exclusive_file_lock.py`.
  Change signal-gap fill to import these names there. Keep the old coordinator aliases only until all
  C1 callers are migrated; delete the aliases in Task 5.

- [x] **Step 3: Extract runtime-root resolution.**

  Move `resolve_daily_runtime_root()` into `shared/runtime_paths.py` as
  `resolve_runtime_artifact_root()`, preserving its ownership and path-safety validation. Change
  signal-gap fill and storage preflight to use the new name; test equal valid/invalid-path behavior.

- [x] **Step 4: Run focused tests and commit.**

  Run lock, signal-gap fill, storage-preflight, and architecture-boundary tests. Commit with
  `refactor: extract shared cleanup primitives`.

### Task 4: C1b — remove coordinator-mode propagation while preserving active launchd semantics

**Files:**

- Modify: `scheduler/executor.py`
- Modify: `shared/liwei_0616_phase_a_cache.py`
- Modify: `scheduler/blackbox_scheduler_admission.py`
- Modify: `deploy/blackbox_scheduler_admission_v1.json`
- Modify: `tests/test_executor_run_id.py`
- Modify: `tests/test_native_generation_executor.py`
- Modify: `tests/test_blackbox_scheduler_admission.py`
- Modify: `tests/test_direct_prediction.py`
- Modify: `tests/test_backend_api.py`

- [x] **Step 1: Write/update tests for the surviving two control planes.**

  Assert that Blackbox admission accepts only `DIRECT_SCHEDULED` and `LAUNCHD_ONE_SHOT`, and that a
  zero-capability gray identity remains excluded. Assert that child algorithm environments contain no
  `BOND_DAILY_COORDINATOR_MODE` contract.

- [x] **Step 2: Remove only the mode plumbing.**

  Delete `DAILY_LEDGER`, `LEGACY_AUTOMATIC`, `require_daily_coordinator_mode`, and
  `strip_daily_coordinator_mode` from execution/admission paths. Retain the existing
  `direct_scheduled` denial rules and the `launchd_one_shot` creation fence. In the Liwei cache, make
  compare-gate necessity depend only on `require_compare_gate is True`.

- [x] **Step 3: Verify and commit.**

  Run direct-prediction, executor, native-generation, admission, backend API, and launchd runner
  focused tests. Commit with `refactor: remove daily coordinator mode plumbing`.

### Task 5: C1c — decouple current DataBridge publication identity from occurrence/epoch state

**Files:**

- Modify: `shared/data_bridge/refresh.py`
- Modify: `shared/data_bridge/authority.py`
- Modify: `harness/signal_gap_plan.py`
- Modify: `tests/test_data_bridge_refresh.py`
- Modify: `tests/test_data_bridge_current_authority.py`
- Modify: `tests/test_signal_gap_plan.py`

- [x] **Step 1: Capture the current launchd publication contract in tests.**

  Cover `run_full_refresh(..., enforce_legacy_publication_fence=False)`, current manifest continuity,
  strict authority read, and v4 frozen-plan readback. The test must assert that an existing v4 plan can
  still be parsed for audit, even if newly written plans use a newer schema.

- [x] **Step 2: Remove occurrence-bound publication capabilities.**

  Replace `DailyCoordinatorPublicationCapability`/stable-publication checks with a neutral immutable
  publication identity built from generation, manifest and current-artifact continuity. Remove the
  occurrence/epoch fields from new signal-gap-plan canonical payloads; version the new plan schema and
  retain a read-only normalizer for v4.

- [x] **Step 3: Verify and commit.**

  Run DataBridge refresh/current-authority/signal-gap plan and fill tests. Commit with
  `refactor: decouple databridge publication from ledger`.

### Task 6: C2 — remove the ledger/occurrence closure and repository APIs

**Files:**

- Delete: `scheduler/daily_ledger.py`
- Delete: `scheduler/daily_runtime.py`
- Delete: `scheduler/scheduled_executor.py`
- Delete: `scheduler/daily_control_plane_probe.py`
- Delete: `scheduler/daily_direct_authority.py`
- Delete: `scheduler/capacity_candidate_runtime.py`
- Delete: `scheduler/capacity_attestation.py`
- Delete: `scheduler/daily_coordinator.py`
- Delete: `harness/daily_real_replay.py`
- Delete: `harness/daily_real_replay_operator.py`
- Move: `harness/daily_real_replay_mysql.py` → `tests/isolated_mysql.py`
- Delete: `deploy/daily_scheduler_policy_v1.json`
- Delete: `deploy/daily_scheduler_policy_v2.json`
- Delete: `deploy/daily_coordinator_rollout_v1.json`
- Delete: `deploy/daily_coordinator_epoch_contract_v1.json`
- Delete: `deploy/daily_coordinator_epoch_genesis_v1.json`
- Modify: `scheduler/repository.py`
- Modify: `scheduler/generation_registry.py`
- Modify: `scheduler/daily_policy.py` or migrate its surviving source constants before deletion
- Modify: `harness/cli.py`
- Modify: `harness/gates/unit_gate.py`
- Update/delete: ledger-only tests listed in the design document

- [x] **Step 1: Make all production callers compile against C1 primitives.**

  Before any deletion, run `python -m compileall -q scheduler harness shared` and use `rg` to prove no
  live caller imports the delete set. Migrate only the surviving source-input constants and
  input-generation lineage code; do not remove `complete_gray_gap_run`, normal run completion, or
  registry APIs.

- [x] **Step 2: Delete occurrence family atomically.**

  Remove `Schedule*` dataclasses and all occurrence/item/target/heartbeat/attempt/SLA/retry/fence APIs
  from `scheduler/repository.py`, including the daily-ledger prediction-key guard. Remove the CLI parser
  and imports for `daily-real-replay`. Delete only tests whose purpose is the deleted closure; retain
  migration-history tests.

- [x] **Step 3: Verify repository and launchd behavior.**

  Run repository launchd-one-shot, registry, generation, migration-017, architecture-boundary,
  signal-gap and launchd prediction runner tests. Confirm launchd one-shot can create normal runs with
  legacy schedule linkage columns NULL.

- [x] **Step 4: Commit C2.**

  Commit only the C closure deletion/rewrite with `refactor: retire daily ledger runtime`.

### Task 7: Documentation audit and final verification

**Files:**

- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: current architecture/SOP documents that present removed code as active
- Preserve: historical records and `migrations/017_daily_schedule_ledger.sql`

- [x] **Step 1: Update only current guidance.**

  State that launchd one-shot is the sole scheduler control plane, legacy ledger code has been retired,
  and historical migration/table retirement is deferred. Do not rewrite historical audit records.

- [x] **Step 2: Run final evidence commands.**

  ```bash
  git diff --check
  PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n bond_factor_lab_service python -m unittest discover -s tests -p 'test_*.py'
  PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n forecast_env_blackbox_v1 python tests/test_cgb_causal_wk_1y_v128_delivery.py -q
  ```

  Audit source references with `rg '(daily_ledger|daily_real_replay|BOND_DAILY_COORDINATOR_MODE)'` and
  allow only explicit historical migration/record mentions. Verify no installed plist or launchctl state
  changed, and stage only cleanup-owned files.

- [x] **Step 3: Commit documentation separately.**

  Commit current guidance and this completed plan with `docs: record launchd-only cleanup`.

## 实施结果与验收

- 已按提交边界完成 P0/A/C1a/C1b/C1c/C2：`2383acb`、`00f831a`、`0aba9e4`、`09dc5fb`、
  `757499f`、`c9b5b94`。`harness/daily_real_replay_mysql.py` 被收敛为 migration 测试支持模块
  `tests/isolated_mysql.py`，而非丢失 recovery 覆盖。
- C2 后仓库不再保留 ledger/occurrence/epoch 的 runtime、policy、replay 或 coordinator 闭包；
  migration-017、历史数据库对象、`t_input_generations` lineage、DataBridge current publication
  contract、launchd one-shot 入口和 manual direct admission 均保留。
- 本次只修改仓库代码与当前指导文档：未修改 installed plist、未调用 `launchctl`、未执行 DDL、未写入
  生产/业务数据库；物理 schema/archive 退役仍是独立、待授权的后续工作。
- 最终验收已通过：文档/架构测试 107 tests、CGB Blackbox delivery test 6 tests、全量 service
  `unittest` exit 0、`git diff --check` 以及遗留引用审计。运行时目录没有任何已删除模块 import；
  唯一剩余命中为 migration-017/recovery 测试、v4 frozen-plan 只读兼容，以及未改动的 backend
  plist 中惰性环境变量。
- 独立审查发现 API 曾会把通过 historical `direct_scheduled` admission 的 identity 入队，而执行器
  随后按 launchd-only fence 拒绝它。已补同一无副作用 preflight contract：没有 manual 实盘阶段时
  API 直接 409、不入队；没有新增或扩大 capability、没有把手工调用伪装成 `scheduled_live`。
