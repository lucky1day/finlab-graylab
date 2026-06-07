# model-mutitest-0529 Benchmark

本目录是上游 `model-mutitest-0529` 在当前仓库中的稳定基准入口。

- `daily_output.csv` 是 canonical 历史输入，指向 `schemes/_original_source/t5/data/daily_output.csv`。
- 上游 t1/t5 两份 `daily_output.csv` 已验证完全一致，因此仓库只保留这一份 canonical CSV。
- 当前测试 Mac 上解压出来的 `model-mutitest-0529/` 含有本地数据库密码配置，已被 `.gitignore` 忽略，不进入业务运行路径。
- 历史复现 runner 会只读使用本目录和 `schemes/_original_source` 中的算法材料，复现结果写入独立 backtest 表，不写入 `t_scheme_predictions`。
