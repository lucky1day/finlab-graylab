# model-mutitest-0529 Source Evidence

本目录是上游 `model-mutitest-0529` 在当前仓库中的外部来源证据归档。

- `daily_output.csv` 是外部交付的 source-evidence CSV，已固化为真实 Git 文件。
- 上游 t1/t5 两份 `daily_output.csv` 已验证完全一致，因此仓库只保留这一份外部证据 CSV。
- 本目录只保存批次级外部来源证据，不是平台输入真源，不保存逐方案 CompareGate baseline。
- 逐方案 original/current benchmark 必须放在 `schemes/{scheme_id}/benchmarks/`。
- 当前 source-evidence CSV 截至 `2026-05-28`；T1/T5 逐方案 benchmark 为覆盖完整 2026-05 目标月，会经 `shared.input_artifacts` 从 DB 补齐 `2026-05-29` 目标验证日。T1 最后一条 5 月目标日是 `target_date=2026-05-29`，对应模型站位 `feature_date=2026-05-28`；T5 最后一周目标日是 `target_date=2026-05-25..2026-05-29`，对应 source T / `feature_date=2026-05-18..2026-05-22`。补齐行只用于 label/actual，不把模型输入截止推到 target 日之后。
- 当前测试 Mac 上解压出来的 `model-mutitest-0529/` 含有本地数据库密码配置，已被 `.gitignore` 忽略，不进入业务运行路径。
- `retired/t1_daily/shap_analysis.py.source` 仅用于 source-fidelity audit 和历史解释复核，不是平台输入、不得被 import，也不声称代表当前 SHAP 输出。
- 历史复现 runner 只有在显式 source-evidence/audit 模式下才会只读使用本目录；默认运行输入必须来自 `shared.input_artifacts`。
- 运行期输入 artifact 统一放在 `backtest_artifacts/runtime_inputs/{scheme_id}/`；历史回测 artifact 统一放在 `backtest_artifacts/backtests/{benchmark_id}/`。
