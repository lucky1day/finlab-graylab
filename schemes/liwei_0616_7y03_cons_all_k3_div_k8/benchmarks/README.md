# liwei_0616 7Y_03 benchmark

Source evidence:

- `/Users/macstudio0/Desktop/models-liwei-0616/bond_predict_merged/reproduction_outputs/latest_oos_20260616/outputs/7y/7Y_03_cons_ALL_k_3_DIV_K_8/predictions.csv`

Scope:

- Source `date` is platform `feature_date`.
- Strict key is `feature_date + target_date + target_tenor + horizon`.
- Coverage mirrors the source-original targeted feature-date set: `2026-05-06` through `2026-06-03`, 21 rows.
- Canonical benchmark rows are produced by the original full-OOS source sequence: one model run over `2024-01-01` through `source_end=2026-06-10`, then selecting the target feature dates. Do not replace this with per-day or current-month PIT windows.
- Rows with `target_date >= 2026-06-01` are benchmark rows but are not historical backtest rows; they must be checked against gray/live predictions after authorized live backfill.
- Direction benchmark for this sample is `metric_samples=21`, `correct=11`, `accuracy=52.38%`, `no_trade=0`.

Current generation:

- `current_predictions_sample.csv` uses the same feature/target schema expected from `schemes.liwei_0616_7y03_cons_all_k3_div_k8.inference`, with source-original full-OOS execution context preserved.
- Audit target: `source_count=21`, `current_count=21`, direction mismatches `0`, label mismatches `0`, confidence mismatches `0`.
- Confidence is deterministic because the source model has no probability output: non-zero direction -> `1.0`, flat -> `0.0`.
- Source monthly metrics group by source `date` / platform `feature_date`; Factor Lab historical and live metrics group by platform `target_date`.
