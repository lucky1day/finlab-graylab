# Daily Production-Style Local Adapter

This directory wraps the final selected daily candidates in the production-style
layout required by `db_data/迁移模版.md`.

## Candidates

- `D1Y` -> `1Y13`
- `D5Y` -> `5Y10`
- `D10Y` -> `10Y04`

All candidates are fixed configurations and are refit/replayed at run time from
the current database-derived daily snapshot. The adapter does not import
external `evel_select` or `product_env` paths, and it does not read package-local
historical data as a deployment input.

## Run

```bash
DRY_RUN=1 DAILY_N_JOBS=1 bash daily_project/src/run_daily.sh run 2026-06-10
```

Production mode defaults to `DRY_RUN=0` and writes DB payloads through the
shared `db_writer.py`. Use `DRY_RUN=1` for local simulation.

## Outputs

For run date `YYYY-MM-DD`, the adapter writes:

- `check_data/YYYY-MM-DD/model_input/daily_model_input.csv`
- `check_data/YYYY-MM-DD/metadata/daily_model_input_metadata.json`
- `output/YYYY-MM-DD/prediction/daily_prediction_signal.json`
- `output/YYYY-MM-DD/prediction/daily_prediction_detail.json`
- `output/YYYY-MM-DD/prediction/daily_selected_prediction_rows.csv`
- `output/YYYY-MM-DD/db_payload/daily_pre_market_forecast_payload.json`
- `output/YYYY-MM-DD/shap/daily_shap_summary.json`
- `output/YYYY-MM-DD/backtest/{1y13,5y10,10y04}/`

Yield-direction mapping for the payload is fixed as `1 -> 空`, `-1 -> 多`,
`0 -> 平`.
