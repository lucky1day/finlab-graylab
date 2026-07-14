# liwei_0616 5Y_01 benchmark

Source:

- `/Users/macstudio0/Desktop/models-liwei-0616/bond_predict_merged/reproduction_outputs/latest_oos_20260616/outputs/5y/5Y_01_cons_SDA_k_3_DIV_K_10/predictions.csv`

Scope:

- Source `date` is platform `feature_date`.
- Strict key is `feature_date + target_date + target_tenor + horizon`.
- Coverage is the full source latest OOS set: `2026-05-06` through `2026-06-03`, 21 rows.
- Canonical benchmark rows reproduce `/reproduction_outputs/latest_oos_20260616/latest_oos_runner.py`: it patches only date-window constants, keeping baseline configs and model logic unchanged.
- Source window constants are fixed for this benchmark: `context_start=2025-05-01`, `latest_start=2026-05-01`, `data_end=2026-06-10`, `prior_start=2025-05-01`, `prior_end=2025-06-30`. The resulting test ranges are `2025-05-01..2025-06-30` and `2026-05-01..2026-06-10`.
- The IC screening cutoff remains the original source anchor `2024-01-01`; do not move it with the latest OOS window.
- Rows with `target_date >= 2026-06-01` are benchmark rows but are not historical backtest rows; they must be checked against gray/live predictions after authorized live backfill.
- Direction benchmark for this sample is `metric_samples=19`, `correct=12`, `accuracy=63.16%`, `no_trade=2`.
- The benchmark CSVs include source-internal audit columns: `vote_score`, `STD_score/STD_dir`, `DIV_score/DIV_dir`, and `ACCWT_score/ACCWT_dir`. The `*_score` columns are source `vs_full` / platform baseline `vote_score`; the `*_dir` columns are `np.sign(vs_full)`, not VT-filtered baseline `final`.

Current generation:

- `current_predictions_sample.csv` was regenerated through `schemes.liwei_0616_cons_sda_k3_div_k10.inference` with `--batch-mode monthly`, matching the source latest OOS batch window above.
- Audit result: `source_count=21`, `current_count=21`, direction mismatches `0`, label mismatches `0`, internal score/dir mismatches `0`.
- Confidence is deterministic because the source model has no probability output: non-zero direction -> `1.0`, flat -> `0.0`.
