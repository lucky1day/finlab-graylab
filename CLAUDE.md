# Bond Factor Lab — 项目规范

> 本文是项目根规范（`CLAUDE.md` 与 `AGENTS.md` 内容一致）。架构细节以 `docs/` 为准，入口见 [docs/README.md](docs/README.md)。

## 项目定位

独立的国债因子实盘测试平台，前端通过 iframe 嵌入 panda_quantflow 的 AIFin Lab Shell。

## 当前工作上下文（必须遵守）

- 当前开发分支：`codex/audit-bugfixes-20260613`。
- 生产分支：`master`。经过验证的开发分支完成后，必须合并或覆盖到 `master` 并推送远程；`master` 是后续生产版本的基准。
- `codex/p1-runner-factorlab-slim` 不再作为备用生产分支使用；除非用户明确要求，不要再把新提交同步到该分支，也不要把它当成发布目标。
- 用户口头说根目录 `agent.md` 时，优先理解为根目录 `AGENTS.md`；本项目要求 `AGENTS.md` 与 `CLAUDE.md` 内容一致，更新根规范时两者要同步。
- 分支操作、提交或暂存前必须先核对 `git status --short` 和相关分支列表，避免把未跟踪的新方案、`outputs/` 产物或其他草稿混入当前任务提交。
- 当前未跟踪的 `outputs/` 属于临时分析/导出产物；除非用户明确要求，不要纳入文档、方案或修复提交。

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
├── source_evidence/   # 外部来源证据归档（benchmark_batches/{benchmark_id}/）
├── backtest_artifacts/ # 运行期输入与回测产物（gitignore）
├── reports/           # 审计与 harness 报告（gitignore）
├── deploy/            # launchd plist
└── docs/              # 项目文档（入口 docs/README.md；只保留当前规范和必要设计文档）
```

## 强约束分层边界（不可破坏的四条不变量）

1. **输入单点**：算法输入只能经 `shared.input_artifacts` 产出；adapter / backtest runner 不得自拼 DB 输入。
2. **写库单点**：只有 `scheduler.repository` / `backtests.repository` / `*_actuals_updater` 能写库；其余层零写库。
3. **core 纯净**：`schemes/*/core/`（非 legacy）零 DB、零写库、零跨方案 import。
4. **源算法保真**：source-backed 方案不得修改原始算法逻辑；时间起点、窗口、回测分组键、每组 `source_end/current_start/current_end`、特征、对齐、模型参数、投票/fallback、内部 score 映射都必须按原始脚本复现。平台只做输入/输出/日期/落库适配；若方向或内部模型数值不一致，先查输入 artifact 和 source 口径，不得用调参或改算法贴结果。原始算法能导出的 `vote_score`、baseline `*_score`/`*_vs`、`*_dir`/`*_sign`、probability/confidence 等内部字段必须进入逐方案 benchmark 和 CompareGate；只做到最终方向一致不得宣称算法逻辑完全一致。若 source-original batch 的 `source_end` 或 test window 晚于样本 `feature_date`，该 batch 只能验收 source-original backtest，不能直接当作 gray/scheduled live 逐日内部数值真值；live 必须保持 `feature_date` 硬截止并用同口径 live-safe oracle 验收。跨灰度边界的 `original_predictions_sample.csv` 必须先判定每行 benchmark role；`TOTAL_BAD=0` 只表示 live 行结构、版本和 source-compatible scope 通过，不表示 live 内部数值可与固定 source batch benchmark 混称“完全一致”。

source-backed 方案入库或修复前必须做算法改动分级：L0 只允许平台 I/O、日期字段、extra、缓存、落库和审计适配；L1 是 source runner 明确暴露的上下文参数传递，必须逐项证明没有移动未 patch 的固定算法锚点；L2 是算法内部改动，默认禁止并 fail-closed。移动 IC screening cutoff、把 source 两段窗口改成单段窗口、把 target-date 月分组改成 feature 月或全局 `source_end`、改变特征列顺序、VT/selector/streak/fallback、内部 score 映射，都属于 L2；除非用户明确批准为新实验方案，不得纳入原始方案修复。

完整依赖方向规则见 [docs/CODE_ARCHITECTURE.md](docs/CODE_ARCHITECTURE.md)；源算法保真规则见 [docs/SOURCE_ALGORITHM_FIDELITY.md](docs/SOURCE_ALGORITHM_FIDELITY.md)；边界总纲见 [docs/HARNESS_ARCHITECTURE.md](docs/HARNESS_ARCHITECTURE.md)。

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

## 方案身份与 Registry

`config.yaml` 里的 `scheme_id`、目录名、`PredictionRecord.scheme_id` 是算法执行身份，也称 `base_scheme_id`。`t_scheme_registry` 是唯一方案注册表，每一行是一个前端/业务方案，唯一键只有 registry `scheme_id`，格式为 `{base_scheme_id}__h{horizon}__{target_tenor}`。即使原算法只预测一个标的，也必须使用这个 composite registry ID；多标的算法在 registry 中拆成多行，但 scheduler 仍按 `base_scheme_id` 只挂载一个执行任务。

前端任务格子由 `target_tenor + task_type` 定义，不再由 `frequency/horizon` 隐式推断。`task_type` 固定取值为 `T+1`、`T+5`、`weekly_point`、`weekly_average`、`monthly`，并存储在 `t_scheme_registry.task_type`；API 返回缺失或非法值必须 fail-closed。

前端、`/api/schemes`、`/api/metrics/{scheme_id}` 和 `/api/backtests/factor-lab` 只使用 `status='active'` 的 registry composite `scheme_id`，并依赖 registry `task_type` 分列；`/api/metrics/{base_scheme_id}?tenor=...` 不是合法调用。`paused` / `archived` registry 行只用于管理或审计，不进入当前前端/业务 API，不允许 trigger，也不允许 scheduler 新写入该 target。预测表、run 表和 backtest 表继续保存 base `scheme_id`，同时用 `target_tenor` 区分目标标的。

## 预测日期与实盘阶段语义

平台、业务和前端统一使用三类日期字段：

- `predict_date` — 信号发出日 / 调度运行日
- `feature_date` — 数据截止日 / 预测站位日
- `target_date` — 验证目标日，用于展示、去重、actual join 和月度统计归属

`feature_date` 是唯一标准数据截止字段；`anchor_date` 只允许作为方案内部算法变量或审计 extra，前端和业务规则不得依赖它。实盘分为 `gray_live`（灰度实盘）和 `scheduled_live`（正式 scheduler 实盘），二者都必须满足 `predict_date=T+1`、`feature_date=T`、`target_date=T+horizon`；历史回测必须满足 `predict_date=feature_date=T`、`target_date=T+horizon`。原始算法 benchmark 里的 `T/date/predict_date` 表达 source T / 预测站位日，进入平台后必须对齐 DB 明细的 `feature_date`，不是对齐 live `predict_date`；跨灰度边界的样本必须先按 `target_date` 和 benchmark role 分流，同执行口径才可对实盘表断言数值一致，否则用 live-safe oracle 核验。完整规则见 [docs/PREDICTION_SEMANTICS.md](docs/PREDICTION_SEMANTICS.md)。

## 方案入库流程（强约束 harness）

新增方案**不改框架代码**：放进 `schemes/{scheme_id}/`，再由 harness 按 Gate 驱动。

```bash
python -m harness onboard {scheme_id} --predict-date YYYY-MM-DD --stage all
# 自动段：static → input → unit → dry-run → compare → backtest → api-readiness（fail-fast，退出码 0/1/2）
# 副作用段（backtest persist / live 写库 / activate）不在 all 内，必须显式 --authorize <TOKEN>（fail-closed）
# 激活后再单独运行 active-only `api` gate 验收前端/API 可见性。
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
- registry `scheme_id`(业务方案) / `base_scheme_id`(算法执行身份) / `benchmark_id`(基准批次) / `data_source`(数据口径) 命名分离

## 关键设计决策

1. 所有方案预测结果写入同一张 MySQL 表，通过 base `scheme_id + target_tenor + horizon + target_date` 隔离；前端业务身份由 registry composite `scheme_id` 表达。
2. 准确率指标由后端实时计算（JOIN predictions 和 actuals 表）。
3. 方案通过约定式目录结构自动发现，新增方案无需改动框架代码。
4. 前端构建为静态文件，由 FastAPI serve。
5. 强约束分层 + 横切 harness：依赖只向下，副作用（写库/激活）须授权，StaticGate 机器守护依赖规则。
6. 算法在 `forecast_env` 子进程运行，与服务环境依赖隔离（JSON stdout 解耦）。
