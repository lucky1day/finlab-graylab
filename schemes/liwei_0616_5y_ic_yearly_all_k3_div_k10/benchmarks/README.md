# 5Y ALL_K10 source-configured experiment benchmark

- Scheme: `liwei_0616_5y_ic_yearly_all_k3_div_k10`
- Screening: `ic/yearly`
- Role: `historical/source-configured-experiment`
- Scope: `experimental/platform_live_pit_variant`
- Original: exact ALL_K10 consensus and DIV_K10 streak-break from clean prod_screen pkl.
- Current: actual platform core re-execution using strictly converted source Phase-A caches.
- Alignment: DB trading calendar, target date is the fifth trading day after feature date.
- Accuracy: flat predictions are excluded from the denominator.
- Yearly wiring: prod_screen runner wf_ic wiring fix: wf_ic={'rebal':'yearly','cap':2000} supplied before clean cache generation
