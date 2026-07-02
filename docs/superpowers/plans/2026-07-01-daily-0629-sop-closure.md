# Daily 0629 SOP Closure Execution Record

**Status**: Completed on 2026-07-01; formal docs refreshed on 2026-07-02.

## Scope

This record covers the SOP closure for:

- `daily_1y_xgb_1y13_0629`
- `daily_5y_lgbm_5y10_0629`
- `daily_10y_lgbm_10y04_0629`

The source-original encrypted daily 0629 algorithm was not modified. Platform work was limited to historical output filtering, benchmark refresh, authorized latest backtest persist, gray-live backfill, live `model_version` storage adaptation, verification, and documentation.

## Final Outcome

- Historical benchmark/current outputs use `feature_date >= 2025-01-01` and `target_date < 2026-06-01`.
- Latest backtest rows are 337 per scheme.
- Authorized latest backtest run IDs are `155`, `156`, and `157`.
- Gray-live rows are complete for 22 target trading days from `2026-06-01` through `2026-07-01`.
- 5Y live top-level `model_version` now uses short select ID `5Y10`; the full source model ID is preserved in `extra.source_model_id`.
- Frontend/API verification confirmed the three T+1 task-grid candidates and gray-live details are visible.

## Verification Evidence

- Final `stage=all` reports:
  - `reports/harness/daily_1y_xgb_1y13_0629/20260701T093840Z/onboard_report.json`
  - `reports/harness/daily_5y_lgbm_5y10_0629/20260701T093911Z/onboard_report.json`
  - `reports/harness/daily_10y_lgbm_10y04_0629/20260701T093943Z/onboard_report.json`
- Harness run IDs:
  - `hr_20260701T093840Z_10f6f1b2591f`
  - `hr_20260701T093911Z_b28d6f4f6d81`
  - `hr_20260701T093943Z_a6a300cb390b`
- Regression suite: `tests.test_daily_0629_source_runner`, `tests.test_daily_0629_schemes`, `tests.test_benchmark_paradigm`, `tests.test_config_schema`, `tests.test_harness_static_gate`, `tests.test_backend_api` all passed, 80 tests total.
- Active-only ApiGate passed for all three schemes.

## Formal Documentation

The canonical status and rules live in:

- `docs/CURRENT_STATUS.md`
- `docs/README.md`
- `docs/PREDICTION_SEMANTICS.md`
- `docs/SCHEME_CONTRACT.md`
- `docs/sop/SCHEME_ONBOARDING_SOP.md`
- `docs/sop/SCHEME_POST_ONBOARDING_TEST_SOP.md`

This file is an execution-note companion, not the source of truth.
