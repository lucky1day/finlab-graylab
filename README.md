# Bond Factor Lab 实盘测试系统

国债因子实验室实盘测试平台，在 Mac Studio 上运行多个国债方向预测方案，统一调度、写库、回测和前端展示。前端由 FastAPI serve，并嵌入 panda_quantflow 的 AIFin Lab Shell。

## 当前状态

截至 2026-07-19，仓库包含 29 个 Native V1 存量方案和 1 个 Blackbox V2 shadow 试验方案。后续新算法、新方案 ID、新目标、新任务和替代版本只允许 Blackbox V2；Native V1 仅做存量维护。具体运行状态以 [docs/CURRENT_STATUS.md](docs/CURRENT_STATUS.md) 为准。

以下是 2026-07-06 的 Native active 摘要，用于说明既有生产覆盖，不是新增方案模板：

| 方案 | 频率 | Horizon | 目标 |
|------|------|---------|------|
| `t1_daily` | daily | 1 | `5Y/10Y` |
| `t5_daily` | daily | 5 | `3Y/5Y/7Y/10Y` |
| `daily_1y_xgb_1y13_0629` / `daily_5y_lgbm_5y10_0629` / `daily_10y_lgbm_10y04_0629` | daily | 1 | `1Y/5Y/10Y` |
| `weekly_5y_direct_0529` / `weekly_7y_cross_d_overlay_0529` / `weekly_10y_d_overlay_0529` | weekly | 6 | `5Y/7Y/10Y` |
| `weekly_avg_1y_lgbm_0529` / `weekly_avg_5y_lgbm_0529` / `weekly_avg_10y_lgbm_0529` | weekly | 6 | `1Y/5Y/10Y` |
| `monthly_1y_rf_top30_0629` / `monthly_5y_knn_top20_0629` / `monthly_10y_rf_top5_0629` | monthly | 30 | `1Y/5Y/10Y` |

当前调度配置：

- 日频：`3 7 * * 1-5`
- 周频：`30 11 * * 6`
- 月频：`0 18 15 * *`（自然月 15 号预测一次，无论是否交易日）

旧周度方案 `weekly_10y_d_overlay`、`weekly_5y_direct_production`、`weekly_7y_cross_d_overlay` 已退役；当前 0529 周度单点覆盖 5Y/7Y/10Y，周平均独立 LGBM 原始方案覆盖 1Y/5Y/10Y（无 7Y）。旧 point-backed 周平均 `weekly_avg_5y_direct_0529` / `weekly_avg_7y_cross_d_overlay_0529` / `weekly_avg_10y_d_overlay_0529` 已暂停，仅保留历史审计。周平均 actual 方向固定为“目标周平均收益率 vs 当前周平均收益率”。月度 0629 三方案的 actual 方向固定为“目标月观测收益率 vs 当前 feature 月观测收益率”；当前灰度边界按 `target_date >= 2026-06-01` 判定，`2026-06-15` 与 `2026-07-15` 目标点都作为 `gray_live` 展示。日度 0629 三方案已完成 SOP 收口：latest backtest 均为 337 行，`target_date=2026-06-01..2026-07-01` 的 22 个交易日已补齐为 `gray_live`。

2026-07-06 运维复审结论：周度 `2026-06-26` 待验证根因是后端 weekly actual JOIN 误把 `predict_date` 审计字段当事实键，已改为按 `target_tenor + target_date + target_rule` 匹配；周度 `2026-07-03` 待验证根因分层处理，10Y 源指标已覆盖并完成 point/average actual 写入，1Y/5Y/7Y 源指标仍只到 `2026-07-01`，因此其 `target_date=2026-07-03` 继续待验证是正确状态。源周历中 `2026-07-03` 的孤立 forward jump 已由只读 `shared.week_calendar_normalizer` 在预测侧和 actuals updater 统一归一化，不改源表、不复用旧周信号。全量 active API 扫描覆盖 25 个前端方案、466 条明细，`semantic_mismatch_count=0`、`unexpected_issue_count=0`；剩余待验证均为源数据未覆盖或未来目标日。详细数据库快照、run_id、回测结果和剩余观察项见 [docs/CURRENT_STATUS.md](docs/CURRENT_STATUS.md)。

## 文档入口

- [docs/README.md](docs/README.md) — 文档索引与阅读路径
- [docs/onboarding/README.md](docs/onboarding/README.md) — 所有方案入库和维护场景的唯一导航
- [docs/CURRENT_STATUS.md](docs/CURRENT_STATUS.md) — 当前状态单一来源
- [docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md](docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md) — 所有新方案的上游 Contract 1.0
- [docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md](docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md) — Blackbox V2 Intake 至 Shadow 平台 SOP
- [docs/native_v1/README.md](docs/native_v1/README.md) — Native V1 存量维护文档域
- [docs/SOURCE_ALGORITHM_FIDELITY.md](docs/SOURCE_ALGORITHM_FIDELITY.md) — source-backed 方案源算法保真强约束
- [docs/SCHEME_CONTRACT.md](docs/SCHEME_CONTRACT.md) — 双运行时共享身份、日期、结果和生命周期契约
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 系统架构
- [docs/CODE_ARCHITECTURE.md](docs/CODE_ARCHITECTURE.md) — 代码架构与分层边界
- [docs/HARNESS_ARCHITECTURE.md](docs/HARNESS_ARCHITECTURE.md) — harness 与安全边界

## 运行环境

- 算法预测：`conda run -n forecast_env ...`
- 后端/API/调度器：`conda run -n bond_factor_lab_service ...`
- Blackbox 算法：只按 `blackbox-v2-v1` Runtime Profile 执行，不从文档或环境名称猜测解释器版本

常用只读验证：

```bash
curl -sS http://127.0.0.1:8100/api/health
curl -sS "http://127.0.0.1:8100/api/metrics/weekly_avg_5y_lgbm_0529__h6__5Y"
curl -sS http://127.0.0.1:8100/api/backtests/factor-lab
```

方案 dry-run 示例：

```bash
conda run -n bond_factor_lab_service python -m scheduler.scheme_runner --scheme-id t1_daily --predict-date 2026-06-11
conda run -n bond_factor_lab_service python -m scheduler.scheme_runner --scheme-id weekly_avg_5y_lgbm_0529 --predict-date 2026-06-06
```

写库类命令（`scheduler.executor`、actuals updater、backtest persist、activation）只在明确需要刷新正式表时执行，并按 SOP 记录 gate 证据。

慢速 source-backed 方案可以在 `config.yaml` 中配置 `schedule.timeout_sec` 覆盖 executor 默认运行预算；它只影响算法子进程等待时间，不改变 cron、日期语义、输入截止或 source 算法逻辑。配置变更后必须重启 scheduler 并复核启动日志。

## 目录说明

```text
bond-factor-lab/
├── shared/            # 公共数据层、输入 artifact、日历服务、模型
├── schemes/           # 双运行时方案目录；Native 存量 + Blackbox 新增
│   ├── t1_daily/
│   ├── t5_daily/
│   ├── daily_1y_xgb_1y13_0629/
│   ├── daily_5y_lgbm_5y10_0629/
│   ├── daily_10y_lgbm_10y04_0629/
│   ├── weekly_5y_direct_0529/
│   ├── weekly_7y_cross_d_overlay_0529/
│   ├── weekly_10y_d_overlay_0529/
│   ├── weekly_avg_1y_lgbm_0529/
│   ├── weekly_avg_5y_lgbm_0529/
│   ├── weekly_avg_10y_lgbm_0529/
│   ├── monthly_1y_rf_top30_0629/
│   ├── monthly_5y_knn_top20_0629/
│   └── monthly_10y_rf_top5_0629/
├── scheduler/         # discovery / executor / repository / actuals updater
├── backend/           # FastAPI API 与静态前端 serve
├── backtests/         # 历史回测 runner
├── harness/           # 强约束 gate 与授权
├── frontend/          # 原生 HTML/CSS/JS 因子实验室页面
├── source_evidence/   # 外部来源证据归档（benchmark_batches/{benchmark_id}/）
├── backtest_artifacts/ # 运行期输入与回测产物（gitignore）
├── reports/           # 审计与 harness 报告（gitignore）
├── migrations/        # SQL 迁移脚本
├── deploy/            # launchd plist
└── docs/              # 文档入口见 docs/README.md
```

逐方案 CompareGate 基线固定放在 `schemes/{scheme_id}/benchmarks/`；`source_evidence/` 只做外部交付原始证据归档，不作为 active runner 默认输入。source-backed 方案必须遵守 [docs/SOURCE_ALGORITHM_FIDELITY.md](docs/SOURCE_ALGORITHM_FIDELITY.md)：平台只做适配，不改原始算法逻辑。
