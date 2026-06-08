# Common Input Artifacts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** All prediction adapters generate model input files through one shared layer before algorithms read data.

**Architecture:** Add `shared/input_artifacts.py` as the single public entry point for daily and weekly input file generation. Daily inputs delegate to `shared.data_service`; weekly inputs originally delegated to the existing weekly data service, save a CSV, and read it back. 2026-06-08 update: `shared.data_service` has been replaced by the user-provided unified daily/weekly/monthly exporter, so weekly inputs now also delegate to `shared.data_service`. Adapters receive DataFrames only from the returned `InputArtifact`.

**Tech Stack:** Python 3.13 in `forecast_env`, pandas, SQLAlchemy engine passed through existing helpers, CSV artifacts under `backtest_artifacts/runtime_inputs/`.

---

### Task 1: Public Input Artifact Layer

**Files:**
- Create: `shared/input_artifacts.py`
- Test: `tests/test_input_artifacts.py`

- [ ] Write tests for `InputArtifact`, daily artifact generation, weekly artifact generation, and path sanitization.
- [ ] Run `python -m unittest tests.test_input_artifacts` and confirm the module is missing.
- [ ] Implement `InputArtifact`, `input_artifact_path`, `build_daily_input_artifact`, and `build_weekly_input_artifact`.
- [ ] Re-run `python -m unittest tests.test_input_artifacts` and confirm it passes.

### Task 2: Adapter Cutover

**Files:**
- Modify: `schemes/t1_daily/predict.py`
- Modify: `schemes/t5_daily/predict.py`
- Modify: `schemes/weekly_10y_d_overlay/predict.py`
- Test: `tests/test_daily_input_data_service.py`
- Test: `tests/test_input_artifacts.py`

- [ ] Update adapter tests so t1/t5/weekly patch `build_daily_input_artifact` or `build_weekly_input_artifact`.
- [ ] Run adapter tests and confirm failures show old direct bridge/service usage.
- [ ] Change adapters to call the common input artifact layer only.
- [ ] Re-run adapter tests and confirm they pass.

### Task 3: Backtest and Documentation

**Files:**
- Modify: `backtests/daily_0529_reproduction.py`
- Modify: `README.md`
- Modify: `docs/CURRENT_STATUS.md`
- Modify: `docs/HISTORICAL_REPRODUCTION.md`
- Modify: `docs/TEST_PLAN.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/RESEARCH.md`
- Modify: `docs/MIGRATION_PLAN.md`
- Modify: `docs/SCHEME_ONBOARDING_SOP.md`

- [ ] Route historical upstream daily generation through `build_daily_input_artifact`.
- [ ] Replace references to per-scheme data generation with the common input artifact layer.
- [ ] Record the new artifact directory and verification commands.

### Task 4: Verification

- [ ] Run `python -m unittest tests.test_input_artifacts tests.test_daily_input_data_service`.
- [ ] Run `python -m py_compile` for changed Python files.
- [ ] Run t1/t5/weekly read-only dry-run functions for known dates.
- [ ] Confirm generated files exist under `backtest_artifacts/runtime_inputs/{scheme_id}/`.
