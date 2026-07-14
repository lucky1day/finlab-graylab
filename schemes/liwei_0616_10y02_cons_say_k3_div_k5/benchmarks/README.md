# liwei_0616 10Y_02 benchmark

Source evidence:
- `/Users/macstudio0/Downloads/pred_target_202604_config2_with_signals_data2.csv`
- `/Users/macstudio0/Downloads/pred_target_202604_config2_with_signals.csv`

Benchmark files use source-original full-OOS feature/target schema:
`feature_date,target_date,target_tenor,horizon,direction,confidence,label,is_correct,vote_score,STD_score,STD_dir,ACCWT_score,ACCWT_dir,V55_7Y_score,V55_7Y_dir,DIV_score,DIV_dir`.

Source `预测日` is interpreted as platform `feature_date`; source `目标日` is platform `target_date`. 10Y02 source-original reproduction is a single full-OOS test sequence from `2024-01-01` through `source_end`, then the requested current-window feature dates are selected. For the 2026-04 target-date benchmark, the selected feature dates are `2026-03-25..2026-04-23` and `source_end/backtest_input_end=2026-04-30`.

Source report month buckets use `feature_date`; Factor Lab API/front-end metrics use `target_date`.

Validation status:
- Benchmark sample has 21 rows, all with April 2026 `target_date`.
- `original_predictions_sample.csv` and `current_predictions_sample.csv` match on key, final direction, label, confidence and internal source-compatible scores with diff_count=0.
- The current runner output matches the user `data2` source CSV on final direction and label for 21/21 rows. Accuracy is 15/20 traded samples = 75.0%.
- Internal score residuals against the user `data2` CSV remain recorded in `*_backtest_summary.json`: max abs diff is `STD_score=0.0006387865658800673`, `ACCWT_score=0.0006780977824691337`, `V55_7Y_score=0.014488635118651505`, `DIV_score=3.3333333332441484e-07`; all internal directions still match.
- Do not tune core parameters, dates, features, weekly/monthly alignment, voting or fallback to fit the remaining internal residuals. The residual must be closed only with the exact source CSV generation script, dependency versions and input bundle.
