# Blackbox V2 试验记录索引

**文档状态**：`HISTORICAL`

**目标读者**：平台入库、审计和生产灰度人员

**最后核验日期**：2026-07-31

这些记录提供时点证据，不定义通用接口或默认授权。

- [全方案问题、结论与验证台账](../../records/SCHEME_ISSUE_LEDGER.md)：跨 Native V1/Blackbox V2 的统一方案问题入口；本目录的具体入库记录作为其证据来源。
- [ONBOARDING_TRIAL_LEDGER.md](ONBOARDING_TRIAL_LEDGER.md)：追加式入库试验台账。
- [FULL_PIPELINE_STABILITY_AUDIT_20260719.md](FULL_PIPELINE_STABILITY_AUDIT_20260719.md)：2026-07-19 全链路认证。
- [PRODUCTION_GRAY_ACTIVATION_20260720.md](PRODUCTION_GRAY_ACTIVATION_20260720.md)：2026-07-20 专项生产灰度激活。
- [PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md](PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md)：2026-07-20 四个 1Y T+5 真实方案的分阶段生产灰度记录。
- [GRAY_ONBOARDING_10Y_T5_4SCHEMES_20260726.md](GRAY_ONBOARDING_10Y_T5_4SCHEMES_20260726.md)：2026-07-26 四个 10Y T+5 方案的 exact active、历史入库和手工灰度验收，以及自动调度隔离边界。
- [RECERTIFICATION_10Y_T5_MAJ3_K3_IC_STATIC_20260726.evidence.json](RECERTIFICATION_10Y_T5_MAJ3_K3_IC_STATIC_20260726.evidence.json)：`ten_y_t5_maj3_k3_ic_static_v1` 的 description waiver 技术复认证机器证据。
- [RECERTIFICATION_10Y_T5_MAJ4_K3_IC_STATIC_20260726.evidence.json](RECERTIFICATION_10Y_T5_MAJ4_K3_IC_STATIC_20260726.evidence.json)：`ten_y_t5_maj4_k3_ic_static_v1` 的 description waiver 技术复认证机器证据。
- [RECERTIFICATION_10Y_T5_MAJ4_K3_IC_YEARLY_20260726.evidence.json](RECERTIFICATION_10Y_T5_MAJ4_K3_IC_YEARLY_20260726.evidence.json)：`ten_y_t5_maj4_k3_ic_yearly_v1` 的 description waiver 技术复认证机器证据。
- [RECERTIFICATION_10Y_T5_SAY_K5_SHARPE_STATIC_20260726.evidence.json](RECERTIFICATION_10Y_T5_SAY_K5_SHARPE_STATIC_20260726.evidence.json)：`ten_y_t5_say_k5_sharpe_static_v1` 的 description waiver 技术复认证机器证据。
- [GRAY_ACCEPTANCE_10Y_T5_4SCHEMES_20260726.evidence.json](GRAY_ACCEPTANCE_10Y_T5_4SCHEMES_20260726.evidence.json)：四方案 `333 + 39 = 372` 的最终只读 DB/API/前端机器可读未签名摘要。
- [MONTHLY_ONBOARDING_FENGRL_5SCHEMES_20260726.md](MONTHLY_ONBOARDING_FENGRL_5SCHEMES_20260726.md)：2026-07-26 五个月度方案的技术入库和 2026-07-27 手工灰度终验记录。
- [FENGRL_MONTHLY_GRAY_PREFLIGHT_20260727.evidence.json](FENGRL_MONTHLY_GRAY_PREFLIGHT_20260727.evidence.json)：五个月度方案 integration / production-readonly 预检的机器可读零写入证据。
- [FENGRL_MONTHLY_GRAY_ACCEPTANCE_20260727.evidence.json](FENGRL_MONTHLY_GRAY_ACCEPTANCE_20260727.evidence.json)：五个月度方案 `16 + 3 = 19`、全批 95 条的 DB/API/前端终验机器可读未签名摘要。
- [CGB_CAUSAL_WK_1Y_ONBOARDING_20260730.md](CGB_CAUSAL_WK_1Y_ONBOARDING_20260730.md)：1Y 周度方案两文件 Intake、权威周历输入和七 Gate 技术验收。
- [CGB_CAUSAL_WK_1Y_ONBOARDING_20260730.evidence.json](CGB_CAUSAL_WK_1Y_ONBOARDING_20260730.evidence.json)：`cgb_causal_wk_1y@05022a0eeec7` 的 check-only 机器证据。
- [CGB_CAUSAL_WK_1Y_PRODUCTION_ACCEPTANCE_20260730.evidence.json](CGB_CAUSAL_WK_1Y_PRODUCTION_ACCEPTANCE_20260730.evidence.json)：该方案 active、72 条历史、9 条灰度和 API 可见性的生产终验摘要。
- [CGB_CAUSAL_WK_3Y_ONBOARDING_20260730.md](CGB_CAUSAL_WK_3Y_ONBOARDING_20260730.md)：3Y 周度方案两文件 Intake、权威周历、生产激活、历史和灰度入库记录。
- [CGB_CAUSAL_WK_3Y_ONBOARDING_20260730.evidence.json](CGB_CAUSAL_WK_3Y_ONBOARDING_20260730.evidence.json)：`cgb_causal_wk_3y@4b8db29b2f74` 的七 Gate、输入和上游 snapshot 边界证据。
- [CGB_CAUSAL_WK_3Y_PRODUCTION_ACCEPTANCE_20260730.evidence.json](CGB_CAUSAL_WK_3Y_PRODUCTION_ACCEPTANCE_20260730.evidence.json)：该方案 active、72 条历史、9 条灰度、API/前端和调度隔离的生产终验摘要。
- [ONE_Y_T1_QUOTE_STATE_HV_ONBOARDING_20260730.md](ONE_Y_T1_QUOTE_STATE_HV_ONBOARDING_20260730.md)：1Y T+1 日频方案的 PR 修复、七 Gate、生产激活、337 条历史、43 条灰度和既有 launchd 日频链路验收记录。
- [ONE_Y_T1_QUOTE_STATE_HV_ONBOARDING_20260730.evidence.json](ONE_Y_T1_QUOTE_STATE_HV_ONBOARDING_20260730.evidence.json)：`one_y_t1_quote_state_hv_v1@d6d0cb43aacd` 的 DB/API/前端、runner canary 与 launchd 挂载机器可读未签名摘要。
- [WAVG_GAPFLIP_V5_5SCHEMES_ONBOARDING_20260731.md](WAVG_GAPFLIP_V5_5SCHEMES_ONBOARDING_20260731.md)：PR #20 五个周平均 GAPFLIP V5 方案的生产激活、360 条历史、45 条灰度、API/前端和调度隔离记录。
- [WAVG_GAPFLIP_V5_5SCHEMES_ONBOARDING_20260731.evidence.json](WAVG_GAPFLIP_V5_5SCHEMES_ONBOARDING_20260731.evidence.json)：五个 exact identity/version、35 个持久化 Gate、run 和日期分区的机器可读未签名摘要。
- [THREE_Y_ADYN_T1_2SCHEMES_ONBOARDING_20260731.md](THREE_Y_ADYN_T1_2SCHEMES_ONBOARDING_20260731.md)：PR #21 两个 3Y T+1 ADYN 方案的生产激活、674 条历史、86 条灰度、API/公网和既有 daily-gray 挂载记录。
- [THREE_Y_ADYN_T1_2SCHEMES_ONBOARDING_20260731.evidence.json](THREE_Y_ADYN_T1_2SCHEMES_ONBOARDING_20260731.evidence.json)：两个 exact identity/version、14 个持久化 Gate、历史/灰度分区和 launchctl 状态的机器可读未签名摘要。
- [CGB_CAUSAL_WK_1Y_V128_ONBOARDING_20260806.md](CGB_CAUSAL_WK_1Y_V128_ONBOARDING_20260806.md)：1Y 周度 V1.28 方案的交付适配（去 bundler、去 metadata 依赖、等价一次性计算）、七 Gate、生产激活、72 条历史、8 条灰度与 admission 隔离记录。
- [BACKTEST_PER_REQUEST_RECOMPUTE_FINDING_20260806.md](BACKTEST_PER_REQUEST_RECOMPUTE_FINDING_20260806.md)：walk-forward 类算法在逐 Request 独立截断下被放大成 O(N) 完整重算的成本观察、三点抽样对照证据，以及「上游声明 + Gate 抽样验证」的合同层改进建议；不修改 §6.3 或任何现行约束。
