# Active Voting Scheme Gap Audit

- Audit date: 2026-07-14
- Audit mode: read-only DB queries plus no-persist scheme runner verification
- Release mode: user-authorized one-time `activate`, `live_write`, and `backtest_persist` gates
- Scope: 19 active base schemes, 23 active registry targets
- Excluded: monthly binary (3), weekly average LGBM (3), daily 1Y active-abstain (1)
- Audit-phase database writes: none
- Authorized release writes: 4 live runs/predictions/logs and 2 latest backtest runs with 144 predictions

| Base scheme | Targets | Classification | Evidence |
|---|---:|---|---|
| `daily_10y_lgbm_10y04_0629` | 1 | normal direction | Latest persisted direction `-1`, successful run on 2026-07-14 |
| `daily_5y_2_v28` | 1 | normal direction | Latest persisted direction `1`, successful run on 2026-07-14 |
| `daily_5y_lgbm_5y10_0629` | 1 | normal direction | Latest persisted direction `-1`, successful run on 2026-07-14 |
| `daily_7y_1_v28` | 1 | normal direction | Latest persisted direction `1`, successful run on 2026-07-14 |
| `liwei_0616_10y01_cons_say_k3_div_k10` | 1 | normal direction | Latest persisted direction `1`, successful run on 2026-07-14 |
| `liwei_0616_10y01_full_oos_k3_div_k10` | 1 | normal direction | Latest persisted direction `1`, successful run on 2026-07-14 |
| `liwei_0616_10y02_cons_say_k3_div_k5` | 1 | normal direction | Latest persisted direction `1`, successful run on 2026-07-14 |
| `liwei_0616_5y_auc_static_all_k3_div_k10` | 1 | normal direction | Latest persisted direction `-1`, successful run on 2026-07-14 |
| `liwei_0616_5y_auc_yearly_all_k3_div_k10` | 1 | normal direction | Latest persisted direction `-1`, successful run on 2026-07-14 |
| `liwei_0616_5y_ic_yearly_all_k3_div_k10` | 1 | normal direction | Latest persisted direction `-1`, successful run on 2026-07-14 |
| `liwei_0616_5y01_full_oos_k3_div_k10` | 1 | normal direction | Latest persisted direction `-1`, successful run on 2026-07-14 |
| `liwei_0616_7y01_cons_say_k3_div_k10` | 1 | normal direction | Latest persisted direction `1`, successful run on 2026-07-14 |
| `liwei_0616_7y03_cons_all_k3_div_k8` | 1 | normal direction | Latest persisted direction `1`, successful run on 2026-07-14 |
| `liwei_0616_cons_sda_k3_div_k10` | 1 | native flat | Latest persisted direction `0`; no `signal_policy_applied` marker |
| `t1_daily` | 2 | normal direction | Both active targets have direction `-1`, successful run on 2026-07-14 |
| `t5_daily` | 4 | normal direction | Four active targets have direction `-1/1`, successful run on 2026-07-14 |
| `weekly_5y_direct_0529` | 1 | algorithm no-signal | 2026-07-04 and 2026-07-11 persisted as audited policy flat in run_id `874/876` |
| `weekly_7y_cross_d_overlay_0529` | 1 | algorithm no-signal | 2026-07-04 and 2026-07-11 persisted as audited policy flat in run_id `875/877` |
| `weekly_10y_d_overlay_0529` | 1 | data fault | Current weekly source retains conflicting `202625/202626` rows and the current historical artifact has source-data drift; no flat generated |

## Totals

- Normal direction: 15 base schemes
- Native flat: 1 base scheme
- Algorithm no-signal: 2 base schemes
- Data fault: 1 base scheme
- Run fault: 0 base schemes

## Backtest Note

The persisted 5Y/7Y latest runs are now `163/164`, each with 72 platform rows and `no_signal_to_flat_v1` metadata for 1/4 policy-generated flats. The source benchmark export remains 71/68 rows because policy rows are filtered. The 10Y latest remains run `108` with 72 rows; its new candidate is blocked by historical calendar week `200901`, conflicting current `202625/202626` source rows, and current artifact drift, so it must not be policy-filled.

## Release Evidence

- Activated versions: 5Y `c3526a721528`; 7Y `e0f46b070b0e`.
- Live run IDs: `874/875` for 2026-07-04 and `876/877` for 2026-07-11.
- Every live row has direction `0`, confidence `0.0`, and complete policy audit metadata.
- Latest backtest IDs: 5Y `163` with `samples=72,metric_samples=71`; 7Y `164` with `samples=72,metric_samples=68`.
- Active-only ApiGate passed for both composite registry targets.
- Frontend verification shows “平” for both dates, `-` in the result column, and flat rows excluded from the accuracy denominator.
- No 10Y authorization token was issued or consumed. The 5Y/7Y ActivationGate registry sync registered current 10Y config version `23ed6b85cf08` as active, but the 10Y Gate remains blocked by governed source data faults; no 10Y live row was written and latest backtest stays at run `108`.
