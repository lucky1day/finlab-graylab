# liwei_0616 10Y_02 benchmark

Source evidence:
- `/Users/macstudio0/Desktop/models-liwei-0616/bond_predict_merged/reproduction_outputs/latest_oos_20260616/outputs/10y/10Y_02_cons_SAY_k_3_DIV_K_5/predictions.csv`

Platform benchmark files use canonical strict feature/target schema:
`feature_date,target_date,target_tenor,horizon,direction,confidence,label,is_correct`.

Source `date` is interpreted as platform `feature_date`. Source latest_oos batch is retained as evidence only; current/original benchmark rows are generated through the platform PIT runner. Monthly fast path was accepted after targeted daily strict validation on 14 dates (`2026-05-06,2026-05-13,2026-05-19,2026-05-20,2026-05-21,2026-05-22,2026-05-25,2026-05-26,2026-05-27,2026-05-28,2026-05-29,2026-06-01,2026-06-02,2026-06-03`) with diff_count=0.

Source report month buckets use `feature_date`; Factor Lab API/front-end metrics use `target_date`.

Validation status:
- Benchmark sample has 21 rows: 13 historical rows with `target_date < 2026-06-01`, 8 gray/live boundary rows with `target_date >= 2026-06-01`.
- `original_predictions_sample.csv` and `current_predictions_sample.csv` match on strict key and values with diff_count=0.
- Historical/API and gray/live API alignment has been verified after authorized backtest persist, activation and gray_live write: 13 historical rows align to latest backtest run_id `124`, and 8 gray/live boundary rows align to `/api/metrics/liwei_0616_10y02_cons_say_k3_div_k5__h5__10Y`, diff_count=0.
- The scheme is active and has reached Onboarding Complete. The first natural scheduler attempt on 2026-06-23 failed as run_id `210` because source data was incomplete; after data repair, controlled rerun run_id `233` wrote one `scheduled_live` row for `predict_date=2026-06-23`, `feature_date=2026-06-22`, `target_date=2026-06-29`. It must not be marked Production Observed until the next natural scheduler run succeeds.
