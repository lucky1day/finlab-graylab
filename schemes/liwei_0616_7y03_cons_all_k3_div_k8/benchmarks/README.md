# liwei_0616 7Y_03 benchmark

Source evidence:

- `/Users/macstudio0/Desktop/models-liwei-0616/bond_predict_merged/reproduction_outputs/latest_oos_20260616/outputs/7y/7Y_03_cons_ALL_k_3_DIV_K_8/predictions.csv`

Scope:

- Source `date` is platform `feature_date`.
- Strict key is `feature_date + target_date + target_tenor + horizon`.
- Coverage mirrors the full source latest OOS feature-date set: `2026-05-06` through `2026-06-03`, 21 rows.
- Canonical benchmark rows use platform strict PIT. Historical rows use `current_end=feature_date`; live-boundary rows are checked against authorized gray/live predictions.
- Rows with `target_date >= 2026-06-01` are benchmark rows but are not historical backtest rows; they must be checked against gray/live predictions after authorized live backfill.
- The retained source latest OOS batch uses a later batch window (`current_end=2026-06-03`) and differs from strict PIT on `2026-05-19`, `2026-05-20`, and `2026-05-21`; those differences are recorded in the summary JSON files and are not used as canonical platform predictions.

Current generation:

- `current_predictions_sample.csv` uses the same strict PIT feature/target schema expected from `schemes.liwei_0616_7y03_cons_all_k3_div_k8.inference`.
- Audit target: `source_count=21`, `current_count=21`, direction mismatches `0`, label mismatches `0`, confidence mismatches `0`.
- Confidence is deterministic because the source model has no probability output: non-zero direction -> `1.0`, flat -> `0.0`.
- Source monthly metrics group by source `date` / platform `feature_date`; Factor Lab historical and live metrics group by platform `target_date`.
