# Pytest Suite Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce the default pytest suite from 3356 cases and 152,992 lines to about 1708 cases and 76,000 lines while preserving compact guards for current production invariants.

**Architecture:** Add one data-driven active-scheme contract before deleting per-scheme tests. Delete complete historical responsibilities in three independently verified batches: scheme/onboarding tests, replay/ledger/gap/migration/backtest tests, then redundant Blackbox and performance matrices. Do not modify production code, schemes, migrations, launchd, databases, or runtime state.

**Tech Stack:** Python 3.12, pytest 9.1.1, unittest subtests, FastAPI, SQLAlchemy, APScheduler, Git.

---

### Task 1: Add the compact active-scheme contract

**Files:**
- Create: `tests/test_active_scheme_contracts.py`
- Test: `tests/test_config_schema.py`
- Test: `tests/test_scheduler_discovery.py`
- Test: `tests/test_blackbox_scheduler_admission.py`

- [ ] **Step 1: Record the current baseline**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest --collect-only -q | tail -3
find tests -type f -name 'test_*.py' | wc -l
find tests -type f -name 'test_*.py' -print0 | xargs -0 wc -l | tail -1
```

Expected: 3356 tests collected, 204 modules, and 152992 total lines.

- [ ] **Step 2: Create one data-driven active-scheme contract**

Create `tests/test_active_scheme_contracts.py` with one `unittest.TestCase` method. It must:

```python
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from scheduler.discovery import discover_schemes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMES_ROOT = PROJECT_ROOT / "schemes"


class ActiveSchemeContractTests(unittest.TestCase):
    def test_all_active_scheme_configs_have_runnable_platform_contracts(self) -> None:
        configs = discover_schemes(SCHEMES_ROOT)
        active_dirs = {
            path.parent.name
            for path in SCHEMES_ROOT.glob("*/config.yaml")
            if yaml.safe_load(path.read_text(encoding="utf-8")).get("status")
            == "active"
        }

        self.assertEqual({config.scheme_id for config in configs}, active_dirs)
        self.assertEqual(len(configs), len(active_dirs))

        for config in configs:
            with self.subTest(scheme_id=config.scheme_id):
                self.assertIn(config.runtime_type, {"native_adapter", "blackbox_v2"})
                self.assertTrue(config.scheme_version)
                self.assertTrue(config.tenors)
                self.assertGreater(config.horizon, 0)
                if config.runtime_type == "blackbox_v2":
                    self.assertIsNotNone(config.delivery_script)
                    self.assertIsNotNone(config.delivery_metadata)
                    self.assertTrue(config.delivery_script.is_file())
                    self.assertTrue(config.delivery_metadata.is_file())
                    self.assertEqual(config.contract_version, "1.0")
                else:
                    self.assertTrue(config.entry_point)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the replacement contract and generic discovery guards**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q \
  tests/test_active_scheme_contracts.py \
  tests/test_config_schema.py \
  tests/test_scheduler_discovery.py \
  tests/test_blackbox_scheduler_admission.py
```

Expected: exit code 0. If the exact `SchemeConfig` API differs, inspect `scheduler/discovery.py` and use its real public fields without adding production behavior.

- [ ] **Step 4: Commit the compact contract**

Check status, branch, diff and `git diff --check`, then:

```bash
git add tests/test_active_scheme_contracts.py
git commit -m "test: add compact active scheme contract"
```

### Task 2: Delete per-scheme and onboarding matrices

**Files:**
- Delete the following 45 modules under `tests/`:

```text
test_build_liwei_5y_all_k10_gray_benchmarks.py
test_daily_0529_reproduction_paradigm.py
test_daily_0629_certification.py
test_daily_0629_schemes.py
test_daily_0629_source_runner.py
test_daily_5y_2_v28.py
test_daily_5y_2_v28_backtest.py
test_daily_7y_1_v28.py
test_daily_7y_1_v28_backtest.py
test_generation_native_daily_certification.py
test_liwei_0616_10y01_cons_say_k3_div_k10.py
test_liwei_0616_10y01_cons_say_k3_div_k10_backtest.py
test_liwei_0616_10y02_cons_say_k3_div_k5.py
test_liwei_0616_10y02_cons_say_k3_div_k5_backtest.py
test_liwei_0616_5y_all_k10_gray_backtests.py
test_liwei_0616_5y_all_k10_gray_models.py
test_liwei_0616_7y01_cons_say_k3_div_k10.py
test_liwei_0616_7y01_cons_say_k3_div_k10_backtest.py
test_liwei_0616_7y03_cons_all_k3_div_k8.py
test_liwei_0616_7y03_cons_all_k3_div_k8_backtest.py
test_liwei_0616_cache_contract.py
test_liwei_0616_cache_production_integration.py
test_liwei_0616_cache_projection.py
test_liwei_0616_cons_sda_k3_div_k10.py
test_liwei_0616_cons_sda_k3_div_k10_backtest.py
test_liwei_0616_full_oos_gray_models.py
test_liwei_0616_phase_a_cache.py
test_liwei_0616_phase_a_cache_generations.py
test_monthly_0629_schemes.py
test_monthly_predict_adapter.py
test_monthly_reproduction.py
test_monthly_source_runner.py
test_prewarm_liwei_0616_phase_a_cache.py
test_rebuild_daily0529_scheme_benchmarks.py
test_refresh_liwei_cache_spec_fingerprints.py
test_weekly_10y_d_overlay_0529.py
test_weekly_10y_d_overlay_0529_backtest.py
test_weekly_5y_direct_0529.py
test_weekly_5y_direct_0529_backtest.py
test_weekly_7y_cross_d_overlay_0529.py
test_weekly_7y_cross_d_overlay_0529_backtest.py
test_weekly_average_0529_schemes.py
test_weekly_average_backtest_labels.py
test_weekly_average_lgbm_predict_adapter.py
test_weekly_average_source_evidence.py
```

- [ ] **Step 1: Verify the deletion set is exact**

Run pytest collection against the 45 paths. Expected: 524 tests collected. Run `wc -l` across the paths. Expected: 21508 lines.

- [ ] **Step 2: Delete exactly the 45 files with apply_patch**

Do not delete any scheme source, benchmark, config, source evidence or generic platform test.

- [ ] **Step 3: Run compact scheme and platform guards**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q \
  tests/test_active_scheme_contracts.py \
  tests/test_config_schema.py \
  tests/test_scheduler_discovery.py \
  tests/test_blackbox_scheduler_admission.py \
  tests/test_blackbox_v2_contracts.py \
  tests/test_blackbox_v2_discovery.py \
  tests/test_compare_gate.py
```

Expected: exit code 0.

- [ ] **Step 4: Run the full suite**

Run `conda run -n bond_factor_lab_service python -m pytest -q`.
Expected: exit code 0 and 2833 collected tests, including the new compact contract.

- [ ] **Step 5: Commit batch 1**

Inspect the exact deletion list before staging, then:

```bash
git add -u tests
git commit -m "test: remove per-scheme regression matrices"
```

### Task 3: Delete completed replay, ledger, gap, migration and backtest tests

**Files:**
- Delete the following 51 modules under `tests/`:

```text
test_backtest_factor_lab_readonly.py
test_backtest_gate.py
test_backtest_repository_immutable.py
test_base_runner.py
test_benchmark_paradigm.py
test_blackbox_v2_gray_backfill_gate.py
test_compare_refactor_outputs.py
test_daily_coordinator.py
test_daily_coordinator_epoch_operator.py
test_daily_coordinator_mode.py
test_daily_coordinator_mvp_mysql.py
test_daily_gray_runner.py
test_daily_ledger.py
test_daily_native_coordinator_matrix.py
test_daily_native_coordinator_mysql.py
test_daily_policy_v2_coordinator_mysql.py
test_daily_real_replay_execute_mysql.py
test_daily_real_replay_gate.py
test_daily_real_replay_mysql_lifecycle.py
test_daily_real_replay_operator.py
test_daily_real_replay_runtime.py
test_daily_v2_coordinator_matrix.py
test_daily_v2_coordinator_mysql.py
test_delete_backtest_runs.py
test_delete_retired_weekly_average_schemes.py
test_generate_benchmark_samples.py
test_ledger_018_started_at_preflight.py
test_migration_005_lifecycle.py
test_migration_006_predictions_runid_uk.py
test_migration_007_backtest_immutable.py
test_migration_009_backtest_latest_view.py
test_migration_010_prediction_semantics.py
test_migration_011_registry_per_tenor.py
test_migration_013_add_1y_target_registry.py
test_migration_014_weekly_average_actuals.py
test_migration_015_monthly_actuals.py
test_migration_016_runtime_type.py
test_orchestrator_compare.py
test_postonboard_scripts.py
test_replace_active_blackbox_version.py
test_repository_daily_ledger.py
test_repository_gray_gap.py
test_signal_gap_fill_authorization.py
test_signal_gap_fill_gate.py
test_signal_gap_input_authority.py
test_signal_gap_native_artifact_authority.py
test_signal_gap_native_artifact_mvp.py
test_signal_gap_plan.py
test_signal_gap_plan_segment_scope.py
test_weekly_base_runner.py
test_weekly_metrics.py
```

- [ ] **Step 1: Verify the deletion set is exact**

Expected aggregate: 662 tests and 43437 lines. Confirm with collection and `wc -l` before deletion.

- [ ] **Step 2: Run retained authority guards before deletion**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q \
  tests/test_actuals_launchd.py \
  tests/test_daily_actuals.py \
  tests/test_weekly_actuals.py \
  tests/test_monthly_actuals.py \
  tests/test_scheduler_direct_authority.py \
  tests/test_daily_direct_cache_runtime.py \
  tests/test_daily_runtime.py \
  tests/test_scheduler_main.py \
  tests/test_repository_registry.py \
  tests/test_apply_migrations.py \
  tests/test_migration_017_daily_schedule_ledger.py \
  tests/test_migration_017_mysql_recovery.py \
  tests/test_migration_018_schedule_run_started_at.py \
  tests/test_migration_runner_module.py
```

Expected: exit code 0.

- [ ] **Step 3: Delete exactly the 51 files with apply_patch**

Do not modify the corresponding production or admin modules in this test-only batch.

- [ ] **Step 4: Run retained authority guards and the full suite**

Re-run Step 2, then run the complete pytest suite.
Expected: exit code 0 and 2171 collected tests, including the compact contract.

- [ ] **Step 5: Commit batch 2**

Inspect and stage only deleted test files, then:

```bash
git add -u tests
git commit -m "test: remove completed control-plane regressions"
```

### Task 4: Delete redundant Blackbox and performance matrices

**Files:**
- Delete the following 20 modules under `tests/`:

```text
test_blackbox_v2_activation.py
test_blackbox_v2_api_gate.py
test_blackbox_v2_backtest_persistence.py
test_blackbox_v2_bootstrap.py
test_blackbox_v2_draft_register.py
test_blackbox_v2_draft_register_mysql.py
test_blackbox_v2_harness_dispatch.py
test_blackbox_v2_history.py
test_blackbox_v2_input_artifacts.py
test_blackbox_v2_lifecycle.py
test_blackbox_v2_live_gate.py
test_blackbox_v2_metadata.py
test_blackbox_v2_persisted_gate_provenance.py
test_blackbox_v2_platform_inputs.py
test_blackbox_v2_requests.py
test_blackbox_v2_runtime_profile.py
test_blackbox_v2_snapshot.py
test_factor_lab_dashboard_semantics.py
test_factor_lab_performance_tools.py
test_http_compression.py
```

- [ ] **Step 1: Verify the deletion set is exact**

Expected aggregate: 463 tests and 12416 lines.

- [ ] **Step 2: Run retained Blackbox and serving guards before deletion**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q \
  tests/test_activation_gate.py \
  tests/test_api_readiness_gate.py \
  tests/test_authorization.py \
  tests/test_blackbox_scheduler_admission.py \
  tests/test_blackbox_v2_contracts.py \
  tests/test_blackbox_v2_discovery.py \
  tests/test_blackbox_v2_harness_gates.py \
  tests/test_blackbox_v2_intake.py \
  tests/test_blackbox_v2_runner.py \
  tests/test_backend_serving.py \
  tests/test_factor_lab_dashboard.py \
  tests/test_factor_lab_dashboard_api.py \
  tests/test_frontend_factor_lab.py
```

Expected: exit code 0.

- [ ] **Step 3: Delete exactly the 20 files with apply_patch**

Retain all files named in Step 2.

- [ ] **Step 4: Run retained guards and measure the final suite**

Re-run Step 2. Then run:

```bash
/usr/bin/time -p conda run -n bond_factor_lab_service python -m pytest -q
conda run -n bond_factor_lab_service python -m pytest --collect-only -q | tail -3
find tests -type f -name 'test_*.py' | wc -l
find tests -type f -name 'test_*.py' -print0 | xargs -0 wc -l | tail -1
```

Expected: about 1708 collected tests, 89 test modules, about 75700 lines, and approximately 90 seconds or less.

- [ ] **Step 5: Commit batch 3**

Inspect and stage only deleted test files, then:

```bash
git add -u tests
git commit -m "test: consolidate platform regression coverage"
```

### Task 5: Remove temporary design and plan records

**Files:**
- Delete: `docs/internal/specs/2026-08-01-pytest-suite-reduction-design.md`
- Delete: `docs/internal/plans/2026-08-01-pytest-suite-reduction.md`
- Modify: `docs/internal/specs/README.md`
- Modify: `docs/internal/plans/README.md`

- [ ] **Step 1: Delete the two temporary records and their index entries**

Use `apply_patch`. Preserve all older internal records.

- [ ] **Step 2: Run documentation guards**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q tests/test_onboarding_docs.py
```

Expected: exit code 0 and no broken indexes or links.

- [ ] **Step 3: Commit record cleanup**

```bash
git add -u docs/internal
git commit -m "docs: remove pytest cleanup work records"
```

### Task 6: Final verification and branch preservation

**Files:**
- Verify only.

- [ ] **Step 1: Run final canonical pytest with wall time**

Run `/usr/bin/time -p conda run -n bond_factor_lab_service python -m pytest -q`.
Expected: all retained tests pass, about 1708 collected cases, and approximately 90 seconds or less.

- [ ] **Step 2: Run compile and boundary checks**

Run:

```bash
conda run -n bond_factor_lab_service python -m compileall -q tests
git diff --check
git diff --exit-code 07b5b19..HEAD -- \
  scheduler backend shared harness backtests scripts migrations schemes deploy
```

Expected: no production file changes.

- [ ] **Step 3: Verify final repository state**

Run:

```bash
git status --short --branch
git branch --show-current
git rev-parse master origin/master
git rev-list --left-right --count \
  origin/codex/audit-bugfixes-20260613...HEAD
```

Expected: clean `codex/audit-bugfixes-20260613`, unchanged local/remote master, and no remote push.
