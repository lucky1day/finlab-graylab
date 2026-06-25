# liwei_0616 10Y_01 benchmark

Source evidence:
- `/Users/macstudio0/Desktop/models-liwei-0616/bond_predict_merged/reproduction_outputs/latest_oos_20260616/outputs/10y/10Y_01_cons_SAY_k_3_DIV_K_10/predictions.csv`
- `/Users/macstudio0/Desktop/models-liwei-0616/bond_predict_merged/reproduction_outputs/latest_oos_20260616/patched_modules/10y/cache/*.pkl`

Benchmark files use source-original latest_oos feature/target schema:
`feature_date,target_date,target_tenor,horizon,direction,confidence,label,is_correct,vote_score,STD_score,STD_dir,ACCWT_score,ACCWT_dir,V55_7Y_score,V55_7Y_dir,DIV_score,DIV_dir`.

Source `date` is interpreted as platform `feature_date`. 10Y01 source-original reproduction follows the `latest_oos_20260616` runner: `context_start=2025-05-01`, `latest_start=2026-05-01`, `source_end=2026-06-10`, and `test_ranges=(2025-05-01..2025-06-30, 2026-05-01..2026-06-10)`. Do not replace this with a single `2024-01-01..source_end` full-OOS sequence; that changes streak-break state and flips the 2026-05-19/20 and 2026-05-26/28 trade decisions.

Source report month buckets use `feature_date`; Factor Lab API/front-end metrics use `target_date`.

Validation status:
- Benchmark sample has 21 rows: 13 historical rows with `target_date < 2026-06-01`, 8 gray/live boundary rows with `target_date >= 2026-06-01`.
- `original_predictions_sample.csv` and `current_predictions_sample.csv` match on strict key, final direction, label, confidence and internal source-compatible scores with diff_count=0.
- Current runner no-persist output matches source `predictions.csv` on `pred/true/is_trade/is_correct` for 21/21 rows.
- Current runner no-persist output matches source pkl internal `vs_full` scores and voting signs for `vote_score`, `STD`, `ACCWT`, `V55_7Y`, and `DIV` with max_abs_diff=0.
- DB/API latest backtest and gray/live rows still need a controlled refresh after this source benchmark correction; do not use older strict PIT run_id/API rows as proof of source-original alignment.
