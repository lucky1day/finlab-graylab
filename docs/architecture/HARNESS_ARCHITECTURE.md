# 强约束 Harness 工程架构

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：Harness 开发、平台入库和安全审计人员
本文是双运行时的强约束总纲。所有后续新方案只允许 Blackbox V2；Native V1 仅维护政策清单中的存量身份。`harness onboard` 只编排 Native Gate；Blackbox 使用 Intake、持久化回测和 activate 三步链路。

---

## 1. 总原则

Harness 不是新的预测算法，也不是新的数据口径。Harness 的职责是检查、编排和留下可审计证据。

核心边界:

- `shared.input_artifacts` 是所有算法输入的唯一平台入口：Native V1 由它构建 DB artifact，Blackbox V2 由它提供 DataBridge 同代四文件 Snapshot。
- `shared.calendar_service` 与 `scheduler.weekly_actuals_updater` 必须共享同一周历事实；源周历孤立 forward jump 只允许通过公共只读 normalizer 处理，不能在方案 adapter、core 或临时脚本里各自修正。
- Native V1 的 `core/` 和 `predict.py` 继续遵守纯算法与 adapter 边界；清单外 Native 身份必须在 StaticGate 和 ActivationGate fail-closed。
- Blackbox V2 的 delivery 两文件保持上游原始字节，平台不重写算法；脚本只读 DataBridge 四文件精确临时视图，通过 CLI 输出标准 Result。
- Native source-backed 的 source benchmark/CompareGate 是首次技术入库的保真证据；Blackbox 内部保真由上游负责，平台只验证接口与标准结果。**平台的验证边界等于平台自己新增或修改的边界**：数据接入、写出与平台侧逻辑必须验证；交付代码自身的性质（重复执行确定性、predict/backtest 一致、截止隔离、跨请求无状态）由上游按其交付契约保证，平台不重验。
- ActivationGate 的 Native profile 互斥：current exact version 的完整四段 `all` 通过时使用 `full_initial_onboarding_v1`，只要求当前四个 Gate（含 Compare；DryRun 已含真实输入合同），不要求 maintenance/prior snapshot；只有 prior `all` 的 `static.business_identity` 已持久化且与当前身份精确匹配的修订才使用三段 `native-maintenance`。该快照只含业务字段，不含代码/config/version hash。maintenance 的 current exact `t_scheme_versions` 行必须是 `native_adapter` 且 status 为 `draft|active`；expected Registry identity 可在预激活时统一为 `paused`，或在激活后统一为 `active`，但 draft version 配 active Registry 必须 fail-closed。ActivationGate 是唯一原子建立 active 状态的操作。prior snapshot 缺失、重复、损坏或不匹配时一律 fail-closed，不保留方案级例外。满足标准路径后，历史 source-benchmark 输入 vintage 漂移才只作归档诊断，不是 activation、历史补数、`gray_live`、`scheduled_live` 或 dashboard 的独立 blocker。
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

`harness/` 已实现 Native Gate 编排以及 Blackbox Intake、回测和激活入口，不承载业务算法、不定义新数据口径、不直接代替 scheduler 执行正式调度。`python -m harness onboard ...` 只用于 Native 存量流程。

---

## 3. Harness Gate

> 本节定义已落地的强约束边界。共享契约见 [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md)，场景分派见[统一入库导航](../onboarding/README.md)。

`harness/` 按以下模块职责拆分:

| 模块 | 职责 | 禁止动作 |
|------|------|----------|
| `harness.contracts` | 校验入库政策、共享身份；按 runtime 校验 Native adapter 或 Blackbox Metadata/Request/Result | 不运行算法、不写库 |
| `harness.import_audit` | 静态扫描危险导入和绕路调用 | 不自动改代码 |
| `harness.gates.dry_run_gate` | Native 调用既有 subprocess 执行路径，通过 Harness-only builder receipt 核验实际生成的主/辅助输入 artifact 身份、截止、流式 SHA-256/行数、日期语义和正式表零写入 | 不另跑一遍通用输入构建，不信任 symlink ancestor 或仅由算法自报的 source/version，不写业务表 |
| `harness.gates.compare_gate` | Native 执行 source benchmark | 不用调参、改脚本或伪造结果 |
| `harness.gates.native_maintenance_admission_gate` | 只读核验 Native 先前 `all + compare` 准入证据、匹配的 prior `static.business_identity` 快照、current exact version 与当前 expected Registry identity | current version 只可为 native `draft|active`；Registry 必须统一 paused（预激活）或 active（激活后），draft+active fail-closed；不执行 compare/backtest、不写业务表 |
| `harness.gates.backtest_gate` | Native 运行 `--no-persist`；Blackbox 只接受明确 `--persist`，执行完整历史 Request 批次并原子保存 exact version/输入/环境证据 | 只能写 `t_backtest_*`；不额外跑 predict 冒烟 |
| `harness.gates.dashboard_gate` | 激活后对唯一产品读模型 `/api/factor-lab/dashboard` 执行一次受限 GET 并复用 Backend payload 校验 | 不属于 `all`；不证明 exact version，不写库 |
| `harness.persistence` | run 开始时 fail-early；Gate 完成后在一个事务中批量保存 evidence/errors 并完成 run；commit ACK 不确定时用新连接精确读回 run 状态、Gate multiset 和 summary | 不逐 Gate 开事务、不把未核对的 commit-unknown 当失败或成功、不写本地镜像报告、不改业务状态 |

CLI 标准入口:

```bash
python -m harness gate static \
  --scheme-id t1_daily

python -m harness onboard t1_daily \
  --predict-date 2026-06-06 \
  --stage all

```

`--stage all` 只支持 Native V1，固定为
`static -> dry-run -> compare -> backtest-no-persist`。Native DryRun 从同一真实执行中读取共享
builder receipt 并与最终输入和 PredictionRecord 交叉核对；首次 Native 技术入库必须保留 source
benchmark/CompareGate。

Native builder receipt 环境变量只由 DryRun 父进程显式注入，目录为本次运行独占的 `0700` 临时目录，
receipt 为 `0600` 普通文件并在 Gate 返回前删除。StaticGate 同时拒绝 Native `predict.py` 直接写文件。

Blackbox V2 不接受 `onboard`。Intake 定义交付结构、Metadata、固定 Profile/Schema 和平台安全静态
边界；完整持久化回测与 activate 直接复用同一纯校验，完整回测另验证平台批量调用与标准输出。旧 Blackbox Harness `all` 证据不再是 backtest
或 activation 的前置条件。

Native 四段 evidence profile 和三段 `native-maintenance` 的既有准入、保真与身份规则保持不变；
Blackbox 不接受 `native-maintenance`。

`dashboard` 是激活后的可选只读产品检查；单日补缺仍使用
`signal-gap-fill --predict-date YYYY-MM-DD [--scheme-id BASE_SCHEME_ID]`，两者都不属于入库门禁。

副作用命令绑定 canonical exact version、action、scheme、日期/回测起点和 operator，并保存 operation
scope SHA-256。Blackbox activation 直接读取同 exact version 的成功持久化回测，不再绑定 Harness
run。Blackbox lifecycle 只保留 `activate` 和异常恢复 `lifecycle-reconcile`；pending journal
继续阻断新的生命周期操作。

`config.yaml.schedule.timeout_sec` 是 executor 层方案预算，harness config schema 对 Blackbox 要求该字段存在且为正整数。Blackbox predict 的最终预算取方案申请、版本化 Runtime Profile 平台上限和显式 operation deadline（如有）的最小值；deadline 只能收紧。Blackbox backtest 使用独立的 Profile 预算。任何运行预算都不能替代对应 runtime 的 Gate 证据，也不能作为放宽 source fidelity、日期语义或 protected table guard 的理由。

---

## 4. 入库与维护分派

### 4.1 Blackbox V2 新方案

1. 上游按 Contract 1.0 交付一个 `.py` 和一个 `.json`。
2. Intake 一次校验两普通文件、Metadata、固定 Profile/Schema 和平台安全静态边界，生成 `paused/draft` 配置。
3. DataBridge producer 独立发布 generation 并准备 snapshot；方案流程只读现有 ready receipt。
4. 明确历史/live 分界后执行一次完整持久化回测。
5. 取得独立激活授权后执行 `activate`；首次 draft 身份建立与 active 切换在同一命令完成。
6. 正式 `scheduled_live` 仍只由目标主机已安装的 one-shot 调度触发。

具体操作以[Blackbox 平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)为准。

### 4.2 Native V1 存量维护

1. 先确认 `scheme_id` 在版本化政策清单中，且改动不是新算法、新 target、新 task type 或新身份。
2. 按改动分级保留 source、输入口径和回归证据。
3. 首次技术入库使用 Native 四段 `all`，其中 source benchmark/CompareGate 是必留证据。
4. 只有已有通过 `all + compare` 的不同 Native active version，且其 `static.business_identity` 已持久化并与当前 expected Registry business identity 精确匹配时，修订 version 才可使用三段 `native-maintenance`；缺少标准快照时必须走 current exact version 的完整 `all`。current exact `t_scheme_versions` 还必须是 native `draft|active`，Registry 必须统一 paused（预激活）或 active（激活后），draft+active fail-closed。ActivationGate 是唯一原子建立 active 的操作。当前 exact version 的 full-`all` profile 与 maintenance profile 互斥。历史 benchmark vintage 漂移只归档，不得被写成 current Compare pass 或人工豁免。
5. 任何业务持久化、状态变化或 live 修复继续使用既有受控授权、当前输入截止、统一周历、日期语义和 live-safe oracle。

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

`api_wind_date` 仅由 DataBridge exporter 在每个 generation 的同一只读
一致性事务中捕获一次，并作为固定第四文件 `api_wind_date.csv` 发布。
Harness、自然调度和历史 replay 只消费该 generation；算法子进程、方案
adapter 和其它 Gate 不得直接查询该表。

预测结果与终态审计的正式生产成功提交，只允许三个专用原子完成边界:

- `scheduler.repository.complete_active_native_run()` 提交普通 active Native run
- `scheduler.repository.complete_approved_blackbox_run()` 提交普通已批准 Blackbox run
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
- 旧的 broad `scheduler.executor` 全量 CLI 已退役；合法运行入口仅为宿主 launchd/systemd one-shot runner、受控 Harness Gate 和单日 `signal-gap-fill`。不得新建等价批量入口。
- 通过临时脚本绕过 `shared.input_artifacts` 生成算法输入。

---

## 6. 验收证据

每次 Blackbox 新方案入库或 Native 存量维护，至少保留对应运行时的以下证据：

- 静态检查结论: Blackbox 由 Intake 定义并在持久化回测与 activate 复验交付、Metadata、固定 Profile/Schema 和安全静态边界；Native 由 StaticGate 记录。
- 输入 artifact 结论: Blackbox 在完整回测中保留 generation/snapshot；Native 在 DryRun 证据中保留实际 builder receipt，两者都不重复构建输入。
- 执行结论: Blackbox 保留完整批量回测的 Result 回显、数量、日期与 repository 提交证据；Native 保留 dry-run 输出和正式表行数不变。
- 执行预算结论: Native 若配置 `schedule.timeout_sec`，记录实际耗时、配置值和是否仍在预算内；Blackbox 记录方案申请、Runtime Profile 的 predict/backtest 预算、实际耗时及任何独立 operation deadline，并证明最终 predict 预算取三层最小值。确认预算只影响 executor 等待，不改变算法输出。
- 回测结论: Native 保留 `--no-persist` summary；Blackbox 必须保留完整区间、批次、预算、exact version/输入/环境和原子持久化证据。
- 算法保真结论: Native 保留 source benchmark 与 full/maintenance profile；Blackbox 记录上游脚本/Metadata hash，不宣称平台已重验黑盒内部性质。
- 日期语义结论: 回测样本满足 `predict_date == feature_date` 且最早 `predict_date >= 2025-01-01`；实盘样本满足对应频率的发出规则；周频实盘必须由 `feature_date=previous_trading_day(predict_date)` 再映射 `feature_week_id`，输入使用 `end_week=feature_week_id/as_of_date=feature_date`；月度 source-backed 方案若声明自然 15 号触发，必须证明 `predict_date` 保留自然 15 号，`feature_date/target_date` 分别取对应月 15 号及以前最近交易日；前端/业务表达数据截止时只用 `feature_date`，不依赖 `anchor_date`。
- 实盘阶段结论: 灰度实盘和正式实盘必须能区分为 `gray_live` / `scheduled_live`；灰度观察区按方案级 `target_date >= gray_target_start` 判定；月度回补必须按目标月枚举，不能按 `predict_date >= gray_start` 漏掉首个 target 月。
- 若落库: 写库前后受保护表行数对比，证明只影响授权表和授权 scheme。
- 前端/API 结论: 激活后 DashboardGate 对 `/api/factor-lab/dashboard` 的快照校验通过，矩阵格子不消失；它只证明当前业务视图可读，不证明 exact version。前端月度样本数展示 `samples`，准确率括号展示 `correct/metric_samples`，每日/周度验证表中预测为“平”的行展示 `-`；有 live 区间时，前端必须按 live `target_date` 月份插入虚线分隔，backtest 区不得含 `target_date >= gray_start` 的 target 月。
- 文档结论: 当前状态、测试记录、历史复现或上线计划已更新。

没有这些证据时不得标记技术入库完成。Blackbox 只有在 Intake、完整持久化回测与授权 activate 均完成后才能称为 active；Production Observed 仍需真实调度证据。
