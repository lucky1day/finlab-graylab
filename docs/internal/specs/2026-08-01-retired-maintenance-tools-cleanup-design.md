# 退役维护工具清理设计

**文档状态**：`HISTORICAL`

**目标**：删除已被现行 Gate 或 reproduction 实现替代、且重新运行会违反当前契约的旧维护脚本，同时补齐证据索引。

## 删除范围

本批删除以下七个无生产入口、无现行 SOP 调用、无测试消费者的脚本：

- `scripts/audit_daily_data_service.py`
- `scripts/compare_refactor_outputs.py`
- `scripts/delete_backtest_runs.py`
- `scripts/generate_benchmark_samples.py`
- `scripts/normalize_weekly_scheme_benchmarks.py`
- `scripts/run_baseline.py`
- `scripts/run_framework_repro.py`

其中 baseline/repro/JSON compare 已由 CompareGate、BacktestGate 和方案 reproduction
替代；daily data audit 已内建到 0529 reproduction；delete CLI 与“不手工删除历史版本”
规则冲突；两个 benchmark 脚本仍输出缺少 `benchmark_role`、strict key 或内部字段的旧
格式，重新运行会降级当前基准。

## 文档规整

从 `reports/README.md` 删除三个退役报告入口。为四份
`RECERTIFICATION_10Y_T5_*.evidence.json` 在 Blackbox V2 records 索引增加链接；不修改
任何历史记录正文。

## 明确保留

- `verify_frontend_db.py`、`verify_scheduler_mount.py` 及其共享
  `postonboard_common.py`，因为现行 Native SOP 仍要求独立 DB/API/scheduler 核验。
- 所有 active 方案 benchmark builder 和两个 Native certification CLI。
- 内部 T+5 设计、system-check、OPS audit、环境快照和生产验收 evidence。

## 验证边界

删除后执行残留引用扫描、文档门禁、Python 编译和完整 pytest。相对本批基线，
`scheduler/`、`harness/`、`backend/`、`shared/`、`backtests/`、`schemes/`、`deploy/`
及 `migrations/` 必须零变化。本轮设计和计划在完成后从最终树删除。
