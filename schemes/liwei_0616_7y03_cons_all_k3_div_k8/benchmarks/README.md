# liwei_0616 7Y_03 benchmark

Source evidence:

- `/Users/macstudio0/Desktop/models-liwei-0616/bond_predict_merged/reproduction_outputs/latest_oos_20260616/outputs/7y/7Y_03_cons_ALL_k_3_DIV_K_8/predictions.csv`
- `/Users/macstudio0/Desktop/models-liwei-0616/bond_predict_merged/reproduction_outputs/latest_oos_20260616/patched_modules/7y/cache/*.pkl`

Scope:

- Source `date` is platform `feature_date`.
- Strict key is `feature_date + target_date + target_tenor + horizon`.
- Coverage mirrors the source-original targeted feature-date set: `2026-05-06` through `2026-06-03`, 21 rows.
- Source-original reproduction follows the `latest_oos_20260616` runner: `context_start=2025-05-01`, `latest_start=2026-05-01`, `source_end=2026-06-10`, and `test_ranges=(2025-05-01..2025-06-30, 2026-05-01..2026-06-10)`. Do not replace this with a single `2024-01-01..source_end` full-OOS sequence; that flips 2026-05-18/19/21/22/25 versus source.
- Rows with `target_date >= 2026-06-01` are benchmark rows but are not historical backtest rows; they must be checked against gray/live predictions after authorized live backfill.
- Direction benchmark for this sample is `metric_samples=20`, `correct=15`, `accuracy=75.0%`, `no_trade=1`.

Current generation:

- `current_predictions_sample.csv` uses the same feature/target schema expected from `schemes.liwei_0616_7y03_cons_all_k3_div_k8.inference`, with source-original latest_oos execution context preserved.
- Audit target: `source_count=21`, `current_count=21`, direction mismatches `0`, label mismatches `0`, confidence mismatches `0`, internal score/sign mismatches `0`.
- Confidence is deterministic because the source model has no probability output: non-zero direction -> `1.0`, flat -> `0.0`.
- Source monthly metrics group by source `date` / platform `feature_date`; Factor Lab historical and live metrics group by platform `target_date`.
- Internal benchmark columns are pinned for source fidelity: `vote_score`, `STD_score/STD_dir`, `DIV_score/DIV_dir`, `ACCWT_score/ACCWT_dir`, and `CROSS_5Y_score/CROSS_5Y_dir`.
- DB/API latest backtest and gray/live rows still need a controlled refresh after this source benchmark correction; do not use older strict/full-OOS run_id/API rows as proof of source-original alignment.
