# model-mutitest-0529 Benchmark

本目录是上游 `model-mutitest-0529` 在当前仓库中的稳定基准入口。

- `daily_output.csv` 是 canonical 历史输入，已固化为真实 Git 文件。
- 上游 t1/t5 两份 `daily_output.csv` 已验证完全一致，因此仓库只保留这一份 canonical CSV。
- 本目录只保存批次级 canonical 输入归档，不保存逐方案 CompareGate baseline。
- 逐方案 original/current benchmark 必须放在 `schemes/{scheme_id}/benchmarks/`。
- 当前 canonical CSV 截至 `2026-05-28`；T1/T5 逐方案 benchmark 为覆盖完整 2026-05 目标月，会经 `shared.input_artifacts` 从 DB 补齐 `2026-05-29` 目标验证日。T1 最后一条 5 月目标日是 `target_date=2026-05-29`，对应模型站位 `feature_date=2026-05-28`；T5 最后一周目标日是 `target_date=2026-05-25..2026-05-29`，对应 source T / `feature_date=2026-05-18..2026-05-22`。补齐行只用于 label/actual，不把模型输入截止推到 target 日之后。
- 当前测试 Mac 上解压出来的 `model-mutitest-0529/` 含有本地数据库密码配置，已被 `.gitignore` 忽略，不进入业务运行路径。
- 历史复现 runner 会只读使用本目录作为 benchmark 输入，复现结果写入独立 backtest 表，不写入 `t_scheme_predictions`。
- 运行期输入 artifact 统一放在 `backtest_artifacts/runtime_inputs/{scheme_id}/`；历史回测 artifact 统一放在 `backtest_artifacts/backtests/{benchmark_id}/`。
