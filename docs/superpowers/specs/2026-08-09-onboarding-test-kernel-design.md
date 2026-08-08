# 方案入库测试核心集设计

**状态**：APPROVED

**日期**：2026-08-09

## 目标

将 `tests/` 从历史事故与实施过程档案，收敛为未来方案入库可重复使用的合同测试。测试文件从 96 个降至约 33 个，测试代码控制在 3 万行以内。

## 保留原则

仅保留能跨方案重复回答下列问题的测试：

1. 交付是否满足 Blackbox V2 Contract、Intake、discovery、确定性和数据截止隔离。
2. 配置、Registry、精确版本、激活和 scheduler admission 是否一致。
3. DataBridge 与 Native 输入是否保持 feature-date 截止及运行时隔离。
4. Harness 的 static、input、compare、backtest、API-readiness、authorization 和持久化合同是否成立。
5. active 方案是否能经 Registry/API/Dashboard 基础接口被读取。
6. Native 存量维护是否仍满足政策清单、输入隔离和 source runner 数据库隔离。
7. 架构分层、日期语义、日历及 tenor 映射是否保持平台不变量。

## 删除原则

删除以下测试及仅由其引用的 fixture：

- 固定迁移编号、恢复现场或历史 schema 的实施测试；
- 固定日期、固定 run、固定方案版本和单次生产缺口的回归测试；
- signal-gap、gray-backfill、cache-prewarm、launchd rollout 与公网验收测试；
- 前端像素、性能、缓存版本和已完成交互修复测试；
- execution token、run-id、SQL 形态和内部函数 monkeypatch 探针；
- 具体方案 delivery 的专项测试，改由 active Blackbox conformance 覆盖。

历史 migration SQL、生产代码、`source_evidence/` 和方案 `benchmarks/` 不因删除测试而删除。只有确认零生产引用、零文档入口且只服务于被删除测试的 helper 才进入死代码闭包。

## 保留集合

保留 33 个 `test_*.py`：

- 方案与配置：`test_active_scheme_contracts.py`、`test_config_schema.py`、`test_onboarding_policy.py`；
- Blackbox：`test_blackbox_v2_contracts.py`、`test_blackbox_v2_intake.py`、`test_blackbox_v2_discovery.py`、`test_active_blackbox_conformance.py`、`test_blackbox_v2_runner.py`、`test_blackbox_v2_harness_gates.py`、`test_blackbox_revision_activation.py`、`test_blackbox_scheduler_admission.py`；
- Harness：`test_harness_static_gate.py`、`test_compare_gate.py`、`test_api_readiness_gate.py`、`test_activation_gate.py`、`test_authorization.py`、`test_harness_persistence.py`；
- 数据与 Native：`test_databridge_input_generation.py`、`test_native_input_generation.py`、`test_native_generation_input_artifacts.py`、`test_native_generation_executor.py`、`test_source_runner_database_isolation.py`、`test_native_maintenance_admission.py`；
- Registry/API：`test_repository_registry.py`、`test_backend_api.py`、`test_factor_lab_dashboard_api.py`；
- 平台不变量：`test_architecture_boundaries.py`、`test_prediction_semantics.py`、`test_data_contract.py`、`test_calendar_service.py`、`test_tenor_mapping.py`、`test_onboarding_docs.py`；
- `test_weekly_10y_d_overlay_stable_order.py` 暂时保留，因为用户工作区未提交文档和 Native source-fidelity 注释仍直接引用它；不得在本批次覆盖用户修改。

同时保留 `tests/blackbox_backtest_fixtures.py`。其余三个 support fixture 随唯一调用测试删除。

## 验收

- 顶层 `test_*.py` 为 33 个，测试 Python 总行数低于 30,000。
- `docs/onboarding/README.md` 只列保留的可复用测试入口。
- 仓库不存在对已删除测试或 fixture 的活跃引用；历史用户工作区文件不在本次提交中修改。
- 保留测试全部通过；`source_evidence/` 和方案 `benchmarks/` 文件数不变。

