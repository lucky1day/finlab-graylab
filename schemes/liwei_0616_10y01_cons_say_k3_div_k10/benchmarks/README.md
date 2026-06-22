# liwei_0616 10Y_01 benchmark

Source evidence:
- `/Users/macstudio0/Desktop/models-liwei-0616/bond_predict_merged/reproduction_outputs/latest_oos_20260616/outputs/10y/10Y_01_cons_SAY_k_3_DIV_K_10/predictions.csv`

Platform benchmark files use canonical strict feature/target schema:
`feature_date,target_date,target_tenor,horizon,direction,confidence,label,is_correct`.

Source `date` is interpreted as platform `feature_date`. Source latest_oos batch is retained as evidence only; current/original benchmark rows are generated through the platform PIT runner. Monthly fast path was accepted after targeted daily strict validation on 9 dates with diff_count=0.

Source report month buckets use `feature_date`; Factor Lab API/front-end metrics use `target_date`.

Validation status:
- Benchmark sample has 21 rows: 13 historical rows with `target_date < 2026-06-01`, 8 gray/live boundary rows with `target_date >= 2026-06-01`.
- `original_predictions_sample.csv` and `current_predictions_sample.csv` match on strict key and values with diff_count=0.
- Historical benchmark rows match `/api/backtests/factor-lab?benchmark_id=liwei_0616_10y_01&data_source=framework_db_aligned` latest run_id 123 with diff_count=0.
- Gray/live boundary rows match `/api/metrics/liwei_0616_10y01_cons_say_k3_div_k10__h5__10Y` with diff_count=0.
- As of 2026-06-22 the scheme is active and Production Observed: metrics include 18 `gray_live` rows plus 1 natural `scheduled_live` row. The scheduled row is outside the 21-row source benchmark window.
