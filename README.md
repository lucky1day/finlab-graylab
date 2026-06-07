# Bond Factor Lab 实盘测试系统

## 项目概述

国债因子实验室实盘测试平台。在Mac Studio测试机上运行多个国债方向预测方案，每日自动调度预测，统一存储结果，前端实时展示准确率指标。

## 项目状态

当前阶段：**新模拟生产机器已完成服务环境、正式平台表、actuals、周度 actuals、历史回测、周度 10Y 回测接入、launchd 常驻验证和 Git baseline 管理**。基础设施、t1/t5/weekly adapter、调度器、FastAPI 后端、前端 API 对接和 launchd 部署均已落地；`t1_daily` / `t5_daily` / `weekly_10y_d_overlay` 均为 `active`。2026-06-05 09:25 常驻 scheduler 已写入 t1/t5 正式预测记录；2026-06-06 已完成周度 10Y 受控 live 写库验收、单方案 scheduler 手动补跑，并注册周六 `11:30`（`30 11 * * 6`）自动调度。2026-06-07 已新增公共输入文件层 `shared.input_artifacts`: t1/t5/weekly live adapter 和历史复现 upstream 分支均先通过公共层调用对应 data service 生成输入 CSV，再读回给算法；日频使用 `shared.data_service` 生成 `daily_output`，周频使用 `weekly_data_service` 的 `wind_export(1)` 口径生成 `weekly_output`。2026-06-08 已按 SOP 接入 `weekly_5y_direct_production`，当前保持 `paused`，标准 dry-run 和 no-persist DB 回测均通过，但尚未同步 registry、未写入 `t_backtest_*`、未进入前端矩阵。artifact 命名已统一: 运行期输入位于 `backtest_artifacts/runtime_inputs/{scheme_id}/`，历史回测和数据差异报告位于 `backtest_artifacts/backtests/{benchmark_id}/`，前端展示使用 API 的中文 `display_name`。旧 `_original_source` 运行依赖已移除，历史 benchmark CSV 已固化到 `benchmarks/model_muti_0529/`。10Y 周度实际方向已写入独立 `t_scheme_weekly_actuals` 表 794 条；历史回测结果已写入独立 `t_backtest_*` 表，前端已展示当前 DB 版本的 `10Y国债活跃 · 周度` 回测格子: run_id=`13`，`68.9% (31/45)`。详见 [当前状态](docs/CURRENT_STATUS.md)。算法预测使用 conda `forecast_env`，后端/API/调度器使用独立 conda `bond_factor_lab_service`。

## 文档

- [PRD 需求文档](docs/PRD.md) — 完整需求、已确认决策、方案清单
- [架构设计](docs/ARCHITECTURE.md) — 系统图、数据流、DB Schema、API设计
- [迁移方案](docs/MIGRATION_PLAN.md) — t1/t5如何迁入，只改I/O不动核心逻辑
- [研究资料](docs/RESEARCH.md) — 现有方案的详细分析
- [实施计划](docs/IMPLEMENTATION_PLAN.md) — 分Phase的checklist
- [当前状态](docs/CURRENT_STATUS.md) — 当前完成度、等待事项和数据库快照
- [新增预测方案 SOP](docs/SCHEME_ONBOARDING_SOP.md) — 新方案命名、目录、接口、验证、上线和回滚流程
- [周度 live 上线计划](docs/WEEKLY_LIVE_ROLLOUT_PLAN.md) — weekly 10Y 从 paused 到受控 live 写库和自动调度的安全推进步骤
- [历史复现记录](docs/HISTORICAL_REPRODUCTION.md) — model_muti_0529 数据对齐、t1/t5 baseline 对比和验证口径
- [测试方案](docs/TEST_PLAN.md) — 逐项验证的详细TODO清单（11个Phase, 150+检查项）
- [测试机环境基线](docs/TEST_MACHINE_BASELINE.md) — 当前 Mac 上 Git/数据库/表数据状态快照
- [部署说明](docs/DEPLOYMENT.md) — `.env`、手动验证和 launchd 安装说明
- [iframe 集成说明](docs/IFRAME_INTEGRATION.md) — panda_quantflow 外层嵌入片段和验收点

## 运行环境

- 算法预测: `conda run -n forecast_env ...`
- 后端/API/调度器: `conda run -n bond_factor_lab_service ...`
- 服务依赖清单: [requirements-service.txt](requirements-service.txt)

## 目录说明

```
bond-factor-lab/
├── CLAUDE.md                    # 项目规范
├── benchmarks/                  # 历史复现 canonical 基准入口
├── backtests/                   # 历史复现 runner 与写库逻辑
├── migrations/                  # 数据库迁移脚本
├── shared/                      # 共享DB配置、公共输入文件层、数据服务、数据模型
├── scheduler/                   # 方案发现、执行器、APScheduler入口、actuals刷新
├── backend/                     # FastAPI API与静态前端serve入口
├── frontend/                    # 原生HTML/CSS/JS因子实验室页面
├── deploy/launchd/              # launchd plist模板
├── docs/                        # 所有设计文档
└── schemes/
    ├── t1_daily/                # T+1方案adapter与原始core副本
    ├── t5_daily/                # T+5方案adapter与原始core副本
    ├── weekly_10y_d_overlay/    # 周度10Y D-overlay adapter与冻结模型core
    └── weekly_5y_direct_production/ # 周度5Y direct-production adapter与冻结原始脚本
```

## 快速开始

当前可用验证入口:

```bash
curl -sS http://127.0.0.1:8100/api/health
conda run -n forecast_env python -m scheduler.scheme_runner --scheme-id t1_daily --predict-date 2026-06-03
conda run -n forecast_env python -m scheduler.scheme_runner --scheme-id t5_daily --predict-date 2026-06-03
conda run -n forecast_env python -m scheduler.scheme_runner --scheme-id weekly_10y_d_overlay --predict-date 2026-05-23
conda run -n forecast_env python -m scheduler.scheme_runner --scheme-id weekly_5y_direct_production --predict-date 2026-06-06
conda run -n forecast_env python scripts/audit_daily_data_service.py
conda run -n forecast_env python scripts/verify_backtest_reproduction.py
```

如 launchd 未运行，可临时启动后端:

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service uvicorn backend.main:app --host 127.0.0.1 --port 8100
```

写库类命令（例如 `scheduler.executor`、`scheduler.daily_actuals_updater`、`scheduler.weekly_actuals_updater`、历史回测 runner）只在明确需要刷新正式表时执行；执行前后应记录受保护表和目标表行数。`scheduler.weekly_actuals_updater` 只写独立 `t_scheme_weekly_actuals`；`backtests.weekly_10y_d_overlay_reproduction` 和 `backtests.weekly_5y_direct_production_reproduction` 只写各自 `scheme_id` 对应的 `t_backtest_*` 回测记录，不写实盘预测表；当前 5Y 周度仅运行过 `--no-persist`。`/api/backtests/factor-lab` 已改为只读获取 scheme metadata；`/api/schemes` 仍会同步 scheme registry，不适合作为严格只读数据库保护探针。
