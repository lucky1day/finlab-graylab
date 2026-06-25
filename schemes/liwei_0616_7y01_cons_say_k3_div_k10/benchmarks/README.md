# liwei_0616 7Y_01 benchmark

Source:

- `/Users/macstudio0/Desktop/models-liwei-0616/bond_predict_merged/reproduction_outputs/latest_oos_20260616/outputs/7y/7Y_01_cons_SAY_k_3_DIV_K_10/predictions.csv`

Scope:

- Source execution mode: `source_original_reproduction`.
- Source `date` is platform `feature_date`.
- Strict key is `feature_date + target_date + target_tenor + horizon`.
- Coverage is the full source latest OOS set: `2026-05-06` through `2026-06-03`, 21 rows.
- Source latest OOS context is `source_end=2026-06-10`; changing that context can keep final directions unchanged while changing internal baseline scores.
- Rows with `target_date >= 2026-06-01` are benchmark rows but are not historical backtest rows; they must be checked against gray/live predictions after authorized live backfill.

Current generation:

- `current_predictions_sample.csv` is generated from `backtests.liwei_0616_7y01_cons_say_k3_div_k10_reproduction --no-persist --sample-dates ... --batch-mode monthly`.
- Audit target: `source_count=21`, `current_count=21`, direction mismatches `0`, label mismatches `0`, confidence mismatches `0`, internal score/sign mismatches `0`.
- Confidence is deterministic because the source model has no probability output: non-zero direction -> `1.0`, flat -> `0.0`.
- Source monthly metrics group by source `date` / platform `feature_date`; Factor Lab historical and live metrics group by platform `target_date`.
- Internal benchmark columns are pinned for source fidelity: `vote_score`, `STD_score/STD_dir`, `ACCWT_score/ACCWT_dir`, `CROSS_5Y_score/CROSS_5Y_dir`, and `DIV_score/DIV_dir`.
- `*_score` is source `vs_full`; `*_dir` is the voting sign `np.sign(vs_full)` used by the 7Y_01 consensus logic. Do not compare these `*_dir` columns to the source pickle baseline `final` array, which is that baseline's own thresholded trade direction rather than the composite vote input.
