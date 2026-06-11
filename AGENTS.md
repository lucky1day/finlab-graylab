# Bond Factor Lab — 项目规范

> 本文是项目根规范（`CLAUDE.md` 与 `AGENTS.md` 内容一致）。架构细节以 `docs/` 为准，入口见 [docs/README.md](docs/README.md)。

## 项目定位

独立的国债因子实盘测试平台，前端通过 iframe 嵌入 panda_quantflow 的 AIFin Lab Shell。

## 技术栈

- **后端**: Python 3.12 + FastAPI + SQLAlchemy + APScheduler
- **前端**: 原生 HTML/CSS/JS（从 panda_quantflow 提取的因子实验室页面）
- **数据库**: MySQL 8.0 (bond_db)
- **部署**: Mac Studio, launchd 管理进程
- **环境**: conda 双环境 —— 算法 `forecast_env`、后端/调度 `bond_factor_lab_service`

## 目录结构约定

```
bond-factor-lab/
├── shared/            # L1 统一公共层：data_service(唯一DB导出) / input_artifacts(唯一输入入口)
│                      #    / calendar_service(唯一日历) / models / db_config / artifact_paths
├── schemes/           # L2 算法层（约定式发现）
│   └── {scheme_id}/
│       ├── config.yaml
│       ├── predict.py  # adapter，暴露 run(predict_date: str) -> list[PredictionRecord]
│       └── core/       # 纯算法逻辑（零 DB、零写库、零跨方案），legacy_*.py 为归档
├── scheduler/         # L3 预测任务层：discovery / scheme_runner / executor / repository / *_actuals_updater
├── backend/           # L4 FastAPI 后端 + 静态前端 serve
├── backtests/         # L4 历史复现 runner（写 t_backtest_*，支持 --no-persist）
├── tests/             # L4 单元/集成测试
├── harness/           # L5 强约束 harness（横切）：gates / contracts / probes / authorization / cli
├── frontend/          # 原生 HTML/CSS/JS 因子实验室页面
├── migrations/        # SQL 迁移脚本
├── scripts/           # 审计/对比/受控 admin 脚本
├── benchmarks/        # canonical 历史基准输入
├── backtest_artifacts/ # 运行期输入与回测产物（gitignore）
├── reports/           # 审计与 harness 报告（gitignore）
├── deploy/            # launchd plist
└── docs/              # 项目文档（入口 docs/README.md；archive/ 归档，legacy_sources/ 算法来源档）
```

## 强约束分层边界（不可破坏的三条不变量）

1. **输入单点**：算法输入只能经 `shared.input_artifacts` 产出；adapter / backtest runner 不得自拼 DB 输入。
2. **写库单点**：只有 `scheduler.repository` / `backtests.repository` / `*_actuals_updater` 能写库；其余层零写库。
3. **core 纯净**：`schemes/*/core/`（非 legacy）零 DB、零写库、零跨方案 import。

完整依赖方向规则见 [docs/CODE_ARCHITECTURE.md](docs/CODE_ARCHITECTURE.md)；边界总纲见 [docs/HARNESS_ARCHITECTURE.md](docs/HARNESS_ARCHITECTURE.md)。

## 方案接口规范

每个方案在 `predict.py` 暴露：

```python
SCHEME_ID = "<scheme_id>"   # 必须 == 目录名 == config.scheme_id

def run(predict_date: str) -> list[PredictionRecord]:
    """
    Args:
        predict_date: 预测发出日期，格式 YYYY-MM-DD
    Returns:
        预测记录列表，每个 tenor 一条记录
    """
```

完整契约（config.yaml schema、extra 必填键、core 约束，机器可校验）见 [docs/SCHEME_CONTRACT.md](docs/SCHEME_CONTRACT.md)。

## 方案入库流程（强约束 harness）

新增方案**不改框架代码**：放进 `schemes/{scheme_id}/`，再由 harness 按 Gate 驱动。

```bash
python -m harness onboard {scheme_id} --predict-date YYYY-MM-DD --stage all
# 自动段：static → input → unit → dry-run → backtest → api（fail-fast，退出码 0/1/2）
# 副作用段（live 写库 / activate）不在 all 内，必须显式 --authorize <TOKEN>（fail-closed）
```

新增方案前先读 T0 强约束范式 [docs/sop/SCHEME_ONBOARDING_T0.md](docs/sop/SCHEME_ONBOARDING_T0.md)，再按 [docs/sop/SCHEME_ONBOARDING_SOP.md](docs/sop/SCHEME_ONBOARDING_SOP.md) 执行；harness 边界见 [docs/HARNESS_ARCHITECTURE.md](docs/HARNESS_ARCHITECTURE.md)。

## 数据库表

源数据表只读：`api_wind_date`、`api_wind_daily/weekly/monthly`(+derivative)、`api_wind_indicators_all`、`t_trade_calendar`。

写库表：
- `t_scheme_predictions` — 统一预测结果表
- `t_scheme_actuals` / `t_scheme_weekly_actuals` — 实际方向表（日频 / 周频）
- `t_scheme_registry` — 方案注册表
- `t_scheme_run_log` — 运行日志表
- `t_target_registry` — Y 标的注册与展示名
- `t_backtest_*` — 历史复现结果（独立于实盘预测）

## 编码规范

- Python: 遵循 PEP 8, type hints, docstring 用中文
- 前端: 原生 JS，无构建步骤，直接由 FastAPI serve
- 数据库字段: snake_case
- API 路径: kebab-case
- `scheme_id`(方案) / `benchmark_id`(基准批次) / `data_source`(数据口径) 三者命名分离

## 关键设计决策

1. 所有方案预测结果写入同一张 MySQL 表，通过 `scheme_id` 隔离。
2. 准确率指标由后端实时计算（JOIN predictions 和 actuals 表）。
3. 方案通过约定式目录结构自动发现，新增方案无需改动框架代码。
4. 前端构建为静态文件，由 FastAPI serve。
5. 强约束分层 + 横切 harness：依赖只向下，副作用（写库/激活）须授权，StaticGate 机器守护依赖规则。
6. 算法在 `forecast_env` 子进程运行，与服务环境依赖隔离（JSON stdout 解耦）。
