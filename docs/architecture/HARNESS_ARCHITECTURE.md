# 强约束 Harness 工程架构

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：Harness 开发、平台入库和安全审计人员
**最后核验日期**：2026-08-03

本文是双运行时 Harness 的强约束总纲。所有后续新方案只允许 Blackbox V2；Native V1 仅维护政策清单中的存量身份。Harness 统一编排 Gate，但按显式 `runtime_type` 选择检查和执行驱动。

---

## 1. 总原则

Harness 不是新的预测算法，也不是新的数据口径。Harness 的职责是检查、编排和留下可审计证据。

核心边界:

- `shared.input_artifacts` 是所有算法输入的唯一平台入口：Native V1 由它构建 DB artifact，Blackbox V2 由它复制 DataBridge 同代三频父快照，并组合显式声明的平台制品。
- `shared.calendar_service` 与 `scheduler.weekly_actuals_updater` 必须共享同一周历事实；源周历孤立 forward jump 只允许通过公共只读 normalizer 处理，不能在方案 adapter、core 或临时脚本里各自修正。
- Native V1 的 `core/` 和 `predict.py` 继续遵守纯算法与 adapter 边界；清单外 Native 身份必须在 StaticGate 和 ActivationGate fail-closed。
- Blackbox V2 的 delivery 两文件保持上游原始字节，平台不重写算法；脚本只读“三频父快照 + 显式声明的平台制品”的精确临时视图，通过 CLI 输出标准 Result。
- Native source-backed 保真由平台 core/benchmark 证据验证；Blackbox 内部保真由上游负责，平台验证确定性、截止隔离和标准结果。
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
│   ├── {native_id}/        # Native V1：config + predict + core，仅存量维护
│   └── {blackbox_id}/      # Blackbox V2：config + delivery/*.py/*.json
├── scheduler/              # 预测任务层: 发现、dry-run subprocess、正式写库、actuals
├── backtests/              # 历史复现层: 每个runner以scheme_id命名
├── backend/                # FastAPI查询和静态前端服务
├── frontend/               # 原生HTML/CSS/JS展示层
├── scripts/                # 人工运维、审计、对比、受控操作
├── harness/                # 强约束 gate / orchestrator / 授权 / 留证实现
├── tests/                  # 单元、集成、安全边界测试
├── source_evidence/        # 外部来源证据归档（benchmark_batches/{benchmark_id}/）
├── backtest_artifacts/     # 运行期输入和回测产物
├── reports/                # 审计和harness报告
└── docs/                   # 架构、SOP、测试、状态文档
```

`harness/` 已实现 Gate 检查和流程编排，不承载业务算法、不定义新数据口径、不直接代替 scheduler 执行正式调度；`python -m harness onboard ...` 是标准机器入口。

---

## 3. Harness Gate

> 本节定义已落地的强约束边界。共享契约见 [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md)，场景分派见[统一入库导航](../onboarding/README.md)。

`harness/` 按以下模块职责拆分:

| 模块 | 职责 | 禁止动作 |
|------|------|----------|
| `harness.contracts` | 校验入库政策、共享身份；按 runtime 校验 Native adapter 或 Blackbox Metadata/Request/Result | 不运行算法、不写库 |
| `harness.import_audit` | 静态扫描危险导入和绕路调用 | 不自动改代码 |
| `harness.input_gate` | Native 构建并核验 artifact；Blackbox 记录三频父快照、平台注册制品、组合输入身份和 Request 截止键 | 不直接调用源表写入，不把摘要证据夸大为 generation freshness |
| `harness.dry_run_gate` | 调用 `scheduler.scheme_runner`，核验 dry-run 不写正式表，并校验 `predict_date/feature_date/target_date` 语义 | 不调用 `scheduler.executor` |
| `harness.compare_gate` | Native 执行 source benchmark；Blackbox 验证 predict/backtest、重复、分批、顺序和未来行隔离 | 不用调参、改脚本或伪造结果 |
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

python -m harness onboard t1_daily \
  --predict-date 2026-06-06 \
  --stage all \
  --check-only

python -m harness gate live \
  --scheme-id t1_daily \
  --predict-date 2026-06-06 \
  --prediction-phase gray_live \
  --authorize "$TOKEN"
```

`--stage all` 的顺序固定为: static -> input -> unit -> dry-run -> compare -> backtest-no-persist -> api-readiness。任何一步失败都停止。Native Compare 是否允许跳过只由其存量维护契约判定；Blackbox Compare 必须完成确定性和隔离检查。`api-readiness` 只按其实现证据声明结构兼容性，不等同于真实 Registry、HTTP API 或 scheduler 探针。`live`、持久化 backtest、`activate` 不属于默认 `all`。

`--check-only` 只允许 canonical `--stage all`，仍执行七个自动 Gate，
仍可通过只读 Engine 捕获日历和构造 Request，但控制面零持久化、
业务表零写入。它不调用 Harness run/gate persistence，Backtest 固定
no-persist，并拒绝授权 token、持久化、shadow/activate/live 或其它
副作用上下文。各 Gate 的证据职责是：

- Static 只记录声明的 provider；
- Input 创建并记录组合输入身份；
- Unit/Dry-run/Compare/Backtest/API readiness 共享同一组合输入身份。

报告显式记录四个零写字段；本地通过报告不能代替可签发授权的持久
审计记录。

`config.yaml.schedule.timeout_sec` 是 executor 层运行预算，harness config schema 只校验其为正整数。它不能替代 Unit/Dry-run/Compare/Backtest 证据，也不能作为放宽 source fidelity、日期语义或 protected table guard 的理由。若方案依赖更长 timeout 才能完成，验证报告应同时记录实际 `duration_sec` 与配置值。

---

## 4. 入库与维护分派

### 4.1 Blackbox V2 新方案

1. 上游按 Contract 1.0 交付一个 `.py` 和一个 `.json`。
2. Intake 校验普通文件、八字段 Metadata、trial 身份和摘要，生成 `paused/draft` 平台配置。
3. 平台确认 Runtime Profile、最新通过校验的 DataBridge generation、三频父快照、声明制品、组合输入身份和七字段 Request。
4. 依次执行 `static -> input -> unit -> dry-run -> compare -> backtest -> api-readiness`。
5. 独立查询证明预测、回测等业务表零写入。
6. 通用入库授权只登记为 `shadow + paused`；生产准备通过的具体方案仍须取得独立专项授权，才能执行 ActivationGate、持久化回测或 LiveGate。

具体操作以[Blackbox 平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)为准。

### 4.2 Native V1 存量维护

1. 先确认 `scheme_id` 在版本化政策清单中，且改动不是新算法、新 target、新 task type 或新身份。
2. 按改动分级保留 source、输入口径和回归证据。
3. 使用相同七段自动 Gate 验证已有 adapter/core 行为。
4. 任何业务持久化、状态变化或 live 修复继续使用既有受控授权。

具体操作以[Native V1 存量维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)为准。清单外 Native ID 无论从 StaticGate 还是 ActivationGate 进入都必须失败。

---

## 5. 数据库安全边界

> 统一公共层（数据接入）已落地到 `shared.data_service`、`shared.input_artifacts`、`shared.calendar_service`。本节只约束读写边界。

源数据表永远只读:

- `api_wind_daily`
- `api_wind_derivative_daily`
- `api_wind_weekly`
- `api_wind_derivative_weekly`
- `api_wind_indicators_all`
- `api_wind_date`
- `t_trade_calendar`
- `t_pre_market_forecast`
- `t_shap`

`api_wind_date` 仅由平台输入 provider 通过只读连接捕获，用于
Harness/check-only 冻结平台注册日历；scheduled 复用已核验的 Native
generation 冻结副本。算法子进程、方案 adapter 和其它 Gate 不得直接
查询该表。

预测结果与终态审计的正式生产成功提交，只允许四个专用原子完成边界:

- `scheduler.repository.complete_active_native_run()` 提交普通 active Native run
- `scheduler.repository.complete_approved_blackbox_run()` 提交普通已批准 Blackbox run
- `scheduler.repository.complete_scheduled_attempt()` 是 legacy daily ledger attempt 的
  既存完成边界；不得为新方案、新调度或过渡路径扩展它
- `scheduler.repository.complete_gray_gap_run()` 提交受控 gray gap run

`create_scheme_run()` 只建立执行前的 `running` 审计行；`write_run_log()` 仅用于尚未进入
专用完成事务的早期失败或跳过。`_insert_run_predictions_conn()` 是 repository
内部 private helper，不是对外写库 API。

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

每次 Blackbox 新方案入库或 Native 存量维护，至少保留以下证据；运行时不适用的项目必须明确标为不适用及原因，不能静默省略：

- 静态检查结论: 目录、命名、接口、危险导入全部通过。
- 输入 artifact 结论: 主输入 frequency、path、source、行列规模、日期/week 覆盖；如声明 `auxiliary_inputs`，同时保留每个辅助输入的 frequency、path、source、data_version、行列规模、覆盖范围和缺列结论。
- dry-run 结论: JSON 输出、预测条数、关键字段、正式表行数不变。
- 执行预算结论: 若方案配置 `schedule.timeout_sec`，记录实际运行耗时、timeout 配置和是否仍在预算内；确认该字段只影响 executor 等待，不改变算法输出。
- 回测结论: `--no-persist` summary、样本总数、`metric_samples`、准确率、月度分布；预测为“平”的样本计入样本总数但不进入任何指标分母。
- 算法保真结论: Native 记录 source 口径、L0/L1/L2 分级、原始 hash 和内部 benchmark；Blackbox 记录上游脚本/Metadata hash、确定性、分批/顺序一致性和未来行隔离，不宣称平台已检查黑盒内部模型。
- 日期语义结论: 回测样本满足 `predict_date == feature_date` 且最早 `predict_date >= 2025-01-01`；实盘样本满足对应频率的发出规则；周频实盘必须由 `feature_date=previous_trading_day(predict_date)` 再映射 `feature_week_id`，输入使用 `end_week=feature_week_id/as_of_date=feature_date`；月度 source-backed 方案若声明自然 15 号触发，必须证明 `predict_date` 保留自然 15 号，`feature_date/target_date` 分别取对应月 15 号及以前最近交易日；前端/业务表达数据截止时只用 `feature_date`，不依赖 `anchor_date`。
- 实盘阶段结论: 灰度实盘和正式实盘必须能区分为 `gray_live` / `scheduled_live`；当前 V28/0629 灰度观察区按 `target_date >= 2026-06-01` 判定，后续方案使用方案级生命周期配置；月度回补必须按目标月枚举，不能按 `predict_date >= gray_start` 漏掉首个 target 月。
- 若落库: 写库前后受保护表行数对比，证明只影响授权表和授权 scheme。
- 前端/API 结论: `/api/backtests/factor-lab` 或 `/api/metrics/{scheme_id}` 可读，矩阵格子不消失；前端月度样本数展示 `samples`，准确率括号展示 `correct/metric_samples`，每日/周度验证表中预测为“平”的行展示 `-`；有 live 区间时，前端必须按 live `target_date` 月份插入虚线分隔，backtest 区不得含 `target_date >= gray_start` 的 target 月。
- 文档结论: 当前状态、测试记录、历史复现或上线计划已更新。

没有这些证据时不得标记技术入库完成。Blackbox 自动 Gate 通过最多支持技术验收和 `shadow + paused` 登记；只有生产准备核验与专项授权同时成立，才能宣称对应方案完成受控 active、live 或 Production Observed。
