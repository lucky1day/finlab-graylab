# Blackbox Formal API Gate 精确回测探针修订

## 目标

消除正式 Blackbox API Gate 对全量回测响应的非必要读取，使已完成持久化认证的方案只读取其
已验证的精确 `benchmark_id`；保持服务实例指纹校验、回测 provenance 校验和 1 MiB 响应上限不变。

## 已确认根因

`BlackboxApiGate` 先从数据库读取并验证唯一的 persisted backtest evidence，其中已经包含
`benchmark_id`，随后却以 `data_source=blackbox_v2_current_snapshot_as_of` 请求全量
`/api/backtests/factor-lab`。当前全量响应为 1,225,294 bytes，超过 probe 的 1 MiB 上限；同一
已验证 benchmark 的精确响应为 76,594 bytes。

## 设计

1. `harness/blackbox_v2/api_gate.py` 的回测请求改为
   `factor_lab_url(base_url, benchmark_id=expected_backtest["benchmark_id"])`。
2. 如果 persisted evidence 不存在或无有效 `benchmark_id`，Gate 保留已有 evidence error 并
   fail-closed：不得退回无过滤或仅按 `data_source` 的大响应请求。
3. 不修改 backend API、`DEFAULT_MAX_RESPONSE_BYTES`、服务实例指纹、scheduler、plist 或数据库。
4. 新增独立单元测试，验证精确 URL、完整契约通过路径，以及 evidence 缺失时无 unfiltered
   factor-lab 请求。

## 运行控制面

服务实例指纹不匹配是独立的运行进程状态：backend 启动时绑定 Git HEAD 与 instance nonce。
在 Harness 修订提交并通过测试后，按用户专项授权只读核对仓库/installed plist 与 loaded state，
再受控重启 backend 并重跑 formal API Gate。若现场状态与预期不一致，停止 restart 并报告。
