# 强约束 Harness 工程架构

**更新日期**: 2026-06-12

本文是 Bond Factor Lab 后续方案入库的强约束总纲。目标是把“用户给出一个预测方案”变成可重复执行的工程流程: 改造、输入生成、测试、回测、前端验收、受控实盘、自动调度。任何新增日频、周频、月频方案都必须先满足本文约束，再进入实盘链路。预测日期与实盘阶段语义以 [PREDICTION_SEMANTICS.md](PREDICTION_SEMANTICS.md) 为准。

---

## 1. 总原则

Harness 不是新的预测算法，也不是新的数据口径。Harness 的职责是检查、编排和留下可审计证据。

核心边界:

- `shared.data_service` 是唯一底层日频、周频、月频 DB 导出标准。普通方案接入时不得修改它的业务逻辑。
- `shared.input_artifacts` 是所有算法输入文件的唯一入口。预测 adapter 和历史回测 runner 都必须先通过它生成输入 CSV，再读回 DataFrame 给算法。
- `schemes/{scheme_id}/core/` 只放算法逻辑。core 禁止写库、禁止调 scheduler、禁止直接拼 DB 输入。
- `schemes/{scheme_id}/predict.py` 只做 adapter: 解析预测上下文、获取公共输入 artifact、调用 core、返回 `list[PredictionRecord]`。
- `scheduler.scheme_runner` 是只读 dry-run 边界，只输出 JSON，不写库。
- `scheduler.executor` / `scheduler.repository` 是正式预测写库边界。算法层不得直接写 `t_scheme_predictions` 或 `t_scheme_run_log`。
- `backtests/` 只写历史回测结果表 `t_backtest_*`，不得把历史回测混入实盘预测表。
- `scripts/` 只放人工 admin、审计、对比和受控写库脚本。普通方案不得通过临时脚本绕过标准 adapter。

---

## 2. 目标目录职责

```text
bond-factor-lab/
├── shared/                 # 共享基础层: DB配置、数据导出、输入artifact、统一模型
├── schemes/                # 方案层: 每个scheme_id一个目录
│   └── {scheme_id}/
│       ├── config.yaml     # 方案元数据和调度状态
│       ├── predict.py      # 唯一预测adapter入口
│       └── core/           # 纯算法逻辑和legacy归档
├── scheduler/              # 预测任务层: 发现、dry-run subprocess、正式写库、actuals
├── backtests/              # 历史复现层: 每个runner以scheme_id命名
├── backend/                # FastAPI查询和静态前端服务
├── frontend/               # 原生HTML/CSS/JS展示层
├── scripts/                # 人工运维、审计、对比、受控操作
├── harness/                # 强约束 gate / orchestrator / 授权 / 留证实现
├── tests/                  # 单元、集成、安全边界测试
├── benchmarks/             # canonical历史输入
├── backtest_artifacts/     # 运行期输入和回测产物
├── reports/                # 审计和harness报告
└── docs/                   # 架构、SOP、测试、状态文档
```

`harness/` 已实现 gate 检查和流程编排，不承载业务算法、不定义新数据口径、不直接代替 scheduler 执行正式调度。当前目录包含 27 个 Python 模块，`python -m harness onboard ...` 是标准机器入口。

---

## 3. Harness Gate

> 本节定义已落地的强约束边界。方案契约的机器校验规范见 [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md)；新增方案的 T0 必读范式见 [sop/SCHEME_ONBOARDING_T0.md](sop/SCHEME_ONBOARDING_T0.md)。

`harness/` 按以下模块职责拆分:

| 模块 | 职责 | 禁止动作 |
|------|------|----------|
| `harness.contracts` | 校验 `config.yaml`、目录结构、`predict.run()` 签名、`PredictionRecord` 字段 | 不运行算法、不写库 |
| `harness.import_audit` | 静态扫描危险导入和绕路调用 | 不自动改代码 |
| `harness.input_gate` | 调用公共输入层生成主 artifact 和 `input_spec.auxiliary_inputs` 辅助 artifact；按 `feature_date=previous_trading_day(predict_date)` 约束 daily/monthly 窗口，按 `feature_week_id + as_of_date=feature_date` 约束 weekly 输入，并在 `auxiliary_input_artifacts` 留证 | 不直接调用源表写入 |
| `harness.dry_run_gate` | 调用 `scheduler.scheme_runner`，核验 dry-run 不写正式表，并校验 `predict_date/feature_date/target_date` 语义 | 不调用 `scheduler.executor` |
| `harness.backtest_gate` | 先跑 `--no-persist`，生成回测摘要和报告；历史排行样本统一要求 `predict_date >= 2025-01-01`，并对受保护表做前后快照 | 未授权不落 `t_backtest_*`；授权落库时也只能改 `t_backtest_*` |
| `harness.live_gate` | 受控单方案写库前的 readiness、dry-run、行数保护；必须显式传入 `prediction_phase=gray_live/scheduled_live` | 不批量执行所有 active 方案 |
| `harness.report` | 输出 JSON/Markdown 证据到 `reports/harness/{scheme_id}/` | 不改业务状态 |

CLI 标准入口:

```bash
python -m harness onboard t1_daily \
  --predict-date 2026-06-06 \
  --stage static

python -m harness onboard t1_daily \
  --predict-date 2026-06-06 \
  --stage all

python -m harness gate live \
  --scheme-id t1_daily \
  --predict-date 2026-06-06 \
  --prediction-phase gray_live \
  --authorize "$TOKEN"
```

`--stage all` 的顺序固定为: static -> input -> unit -> dry-run -> compare -> backtest-no-persist -> api-readonly。任何一步失败都停止（compare 缺 benchmark 时跳过，不阻断）。`live` 和持久化 backtest 不属于默认 `all`，必须显式授权。

---

## 4. 方案入库 SOP

新增方案必须按以下顺序推进:

1. **Intake**: 明确 `scheme_id`、frequency、horizon、tenors、预测语义、调度时间、原始算法文件、样本 Excel/CSV、是否需要历史回测。
2. **Normalize**: 新建 `schemes/{scheme_id}/`，原始脚本归档到 core，实盘 core 改造成 DataFrame 输入函数。
3. **Input Gate**: adapter 和 backtest runner 必须调用 `shared.input_artifacts`，并记录 `input_artifact_path` / `input_artifact_source`。
4. **Static Gate**: 静态检查目录、命名、接口、危险导入、直接写库、绕过公共输入层等问题。
5. **Unit Gate**: 覆盖 core 输出、adapter 输出、公共输入层调用、`PredictionRecord` 字段。
6. **Dry-run Gate**: 通过 `scheduler.scheme_runner` 返回 JSON，并确认 `t_scheme_predictions` / `t_scheme_run_log` 行数不变，同时校验 live 日期语义。
7. **Backtest Gate**: 先 `--no-persist`，确认样本数、月度分布、准确率和最早 `predict_date >= 2025-01-01`；用户授权后才写 `t_backtest_*`，并用 protected table snapshot 阻断越界写库。
8. **Live Gate**: 用户授权并显式传 `prediction_phase` 后，只写该 `scheme_id` 的 prediction/run_log，不能触发其他方案。
9. **Activation**: 通过全部 gate 后，才允许从 `paused` 改为 `active` 并重启 scheduler。
10. **Documentation**: 更新状态、回测、测试记录和 harness 报告路径。

---

## 5. 数据库安全边界

> 统一公共层（数据接入）已落地到 `shared.data_service`、`shared.input_artifacts`、`shared.calendar_service`。本节只约束读写边界。

源数据表永远只读:

- `api_wind_daily`
- `api_wind_derivative_daily`
- `api_wind_weekly`
- `api_wind_derivative_weekly`
- `api_wind_indicators_all`
- `t_trade_calendar`
- `t_pre_market_forecast`
- `t_shap`

正式预测写库只允许这些边界:

- `scheduler.repository.create_scheme_run()` 写 `t_scheme_runs`
- `scheduler.repository.insert_run_predictions()` 写 `t_scheme_predictions`
- `scheduler.repository.write_run_log()` 写 `t_scheme_run_log`
- actuals updater 写 `t_scheme_actuals` / `t_scheme_weekly_actuals`
- backtest repository 写 `t_backtest_*`
- 专用受控 admin 脚本在文档授权范围内调用上述 repository

禁止行为:

- 方案 core 或 `predict.py` 直接执行 `INSERT/UPDATE/DELETE/ALTER/DROP`。
- 为了让算法跑通而修改源数据表。
- 用历史回测结果写入 `t_scheme_predictions`。
- 用 broad `scheduler.executor --include-paused` 或全量 run-once 替代单方案 gate。
- 通过临时脚本绕过 `shared.input_artifacts` 生成算法输入。

---

## 6. 验收证据

每个新增方案合入前，至少保留以下证据:

- 静态检查结论: 目录、命名、接口、危险导入全部通过。
- 输入 artifact 结论: 主输入 frequency、path、source、行列规模、日期/week 覆盖；如声明 `auxiliary_inputs`，同时保留每个辅助输入的 frequency、path、source、data_version、行列规模、覆盖范围和缺列结论。
- dry-run 结论: JSON 输出、预测条数、关键字段、正式表行数不变。
- 回测结论: `--no-persist` summary、样本数、准确率、月度分布。
- 日期语义结论: 回测样本满足 `predict_date == feature_date` 且最早 `predict_date >= 2025-01-01`；实盘样本满足 `predict_date=T+1/feature_date=T`；前端/业务表达数据截止时只用 `feature_date`，不依赖 `anchor_date`。
- 实盘阶段结论: 灰度实盘和正式实盘必须能区分为 `gray_live` / `scheduled_live`；当前 V28 批次灰度观察区按 `target_date >= 2026-06-01` 判定，后续方案使用方案级生命周期配置。
- 若落库: 写库前后受保护表行数对比，证明只影响授权表和授权 scheme。
- 前端/API 结论: `/api/backtests/factor-lab` 或 `/api/metrics/{scheme_id}` 可读，矩阵格子不消失。
- 文档结论: 当前状态、测试记录、历史复现或上线计划已更新。

没有这些证据时，不得把方案标记为架构完成或 live ready。
