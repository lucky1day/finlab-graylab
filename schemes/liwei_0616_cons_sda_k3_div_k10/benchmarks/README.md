# liwei_0616 5Y_01 benchmark

Source:

- `/Users/macstudio0/Desktop/models-liwei-0616/bond_predict_merged/reproduction_outputs/latest_oos_20260616/outputs/5y/5Y_01_cons_SDA_k_3_DIV_K_10/predictions.csv`

Scope:

- Source `date` is platform `feature_date`.
- Strict key is `feature_date + target_date + target_tenor + horizon`.
- Coverage is the full source latest OOS set: `2026-05-06` through `2026-06-03`, 21 rows.
- Rows with `target_date >= 2026-06-01` are benchmark rows but are not historical backtest rows; they must be checked against gray/live predictions after authorized live backfill.

Current generation:

- `current_predictions_sample.csv` was regenerated through `schemes.liwei_0616_cons_sda_k3_div_k10.inference`, using the platform PIT windows for `2026-05-29` and `2026-06-03`.
- Audit result: `source_count=21`, `current_count=21`, direction mismatches `0`, label mismatches `0`.
- Confidence is deterministic because the source model has no probability output: non-zero direction -> `1.0`, flat -> `0.0`.
