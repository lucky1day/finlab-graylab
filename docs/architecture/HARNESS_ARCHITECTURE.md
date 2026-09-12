# 强约束 Harness 工程架构

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：Harness 开发、平台入库和安全审计人员
本文是双运行时的强约束总纲。所有后续新方案只允许 Blackbox V2；Native V1 仅维护政策清单中的 Mac3 W4 九方案及必要依赖。`harness onboard` 只编排 W4 Native Gate；普通 Blackbox 入库使用 Intake、持久化回测和 activate 三步链路，已退役的一次性迁移命令不作为公共接口。

---

## 1. 总原则

Harness 不是新的预测算法，也不是新的数据口径。Harness 的职责是检查、编排和留下可审计证据。

核心边界:

- `shared.input_artifacts` 是所有算法输入的唯一平台入口：Native V1 由它构建 DB artifact，Blackbox V2 由它提供 DataBridge 同代五文件 Snapshot；存量 Blackbox 只获得冻结四文件视图。
- `shared.calendar_service` 与 `scheduler.weekly_actuals_updater` 必须共享同一周历事实；源周历孤立 forward jump 只允许通过公共只读 normalizer 处理，不能在方案 adapter、core 或临时脚本里各自修正。
- Native V1 的 `core/` 和 `predict.py` 继续遵守纯算法与 adapter 边界；清单外 Native 身份必须在 StaticGate 和 ActivationGate fail-closed。
- Blackbox V2 的 delivery 两文件保持上游原始字节，平台不重写算法；新版脚本只读 DataBridge 五文件精确临时视图，通过 CLI 输出标准 Result。
- Native source-backed 的 source benchmark/CompareGate 是首次技术入库的保真证据；Blackbox 内部保真由上游负责，平台只验证接口与标准结果。**平台的验证边界等于平台自己新增或修改的边界**：数据接入、写出与平台侧逻辑必须验证；交付代码自身的性质（重复执行确定性、predict/backtest 一致、截止隔离、跨请求无状态）由上游按其交付契约保证，平台不重验。
- Native activation 的完整 `all` 与 `native-maintenance` profile 互斥，准入条件由[Native 维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md#4-自动-gate)统一定义；清单或证据不符必须 fail-closed。
- `scheduler.scheme_runner` 是只读 dry-run 边界，只输出 JSON，不写库。
- `scheduler.executor` / `scheduler.repository` 是正式预测写库边界。算法层不得直接写 `t_scheme_predictions` 或 `t_scheme_run_log`。
- `backtests.repository` 只写 `t_backtest_*` 不可变回测证据。Blackbox 首次授权 activation 经
  `scheduler.repository` 将历史产品事实 insert-only 发布到 `t_scheme_predictions`；历史与灰度按
  target 分界验收，不把历史回测伪装成 `gray_live` 或 `scheduled_live`。
- `scripts/` 只放人工 admin、审计、对比和受控写库脚本。普通方案不得通过临时脚本绕过标准 adapter。

---

## 2. Harness Gate

> 本节定义已落地的强约束边界。共享契约见 [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md)，场景分派见[统一入库导航](../onboarding/README.md)。

`harness/` 按以下模块职责拆分:

| 模块 | 职责 | 禁止动作 |
|------|------|----------|
| `harness.contracts` | 校验入库政策、共享身份；按 runtime 校验 Native adapter 或 Blackbox Metadata/Request/Result | 不运行算法、不写库 |
| `harness.contracts.import_rules` | 静态扫描危险导入和绕路调用 | 不自动改代码 |
| `harness.gates.dry_run_gate` | Native 调用既有 subprocess 执行路径，通过 Harness-only builder receipt 核验实际生成的主/辅助输入 artifact 身份、截止、流式 SHA-256/行数、日期语义和正式表零写入 | 不另跑一遍通用输入构建，不信任 symlink ancestor 或仅由算法自报的 source/version，不写业务表 |
| `harness.gates.compare_gate` | Native 执行 source benchmark | 不用调参、改脚本或伪造结果 |
| `harness.gates.native_maintenance_admission_gate` | 只读核验 Native 先前 `all + compare` 准入证据、匹配的 prior `static.business_identity` 快照、current exact version 与当前 expected Registry identity | current version 只可为 native `draft|active`；Registry 必须统一 paused（预激活）或 active（激活后），draft+active fail-closed；不执行 compare/backtest、不写业务表 |
| `harness.gates.backtest_gate` | Native 运行 `--no-persist`；Blackbox 只接受明确 `--persist`，执行完整历史 Request 批次并原子保存 exact version/输入/环境证据 | 只能写 `t_backtest_*`；不额外跑 predict 冒烟 |
| `harness.gates.dashboard_gate` | 激活后对唯一产品读模型 `/api/factor-lab/dashboard` 执行一次受限 GET，校验 V5 Summary 合同、active composite、展示身份、任务字段和 backtest 分区 | 不属于 `all`；不判断调度缺口，不证明 exact version，不写库 |
| `harness.persistence` | run 开始时 fail-early；Gate 完成后在一个事务中批量保存 evidence/errors 并完成 run；commit ACK 不确定时用新连接精确读回 run 状态、Gate multiset 和 summary | 不逐 Gate 开事务、不把未核对的 commit-unknown 当失败或成功、不写本地镜像报告、不改业务状态 |

Native Gate 编排与 Blackbox 三步入口分别见[Native 维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)
和[Blackbox 平台 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)。本节只维护 Gate 实现边界，
全仓目录与依赖方向见[代码架构](CODE_ARCHITECTURE.md)。

Native builder receipt 环境变量只由 DryRun 父进程显式注入，目录为本次运行独占的 `0700` 临时目录，
receipt 为 `0600` 普通文件并在 Gate 返回前删除。StaticGate 同时拒绝 Native `predict.py` 直接写文件。

Blackbox V2 不接受 `onboard`。Intake 定义交付结构、Metadata、固定 Profile/Schema 和平台安全静态
边界；完整持久化回测执行前复验脚本安全边界，并验证平台批量调用与标准输出。activate 不重复运行 AST/Metadata 校验，只严格加载 canonical 当前身份，并匹配 exact-version 与当前脚本校验策略摘要的成功回测证据。旧 Blackbox Harness `all` 证据不再是 backtest
或 activation 的前置条件。

`dashboard` 是激活后的可选只读产品检查；active 方案的空 live 明细合法，Gate 不读取 run 或日历重算调度状态。
`signal-gap-fill` 的单日与受支持区间模式都不属于入库门禁；它们复用现有 planner、executor 和 repository，
不引入新的 Harness 层。支持范围和命令见[平台 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#6-灰度区间批量物化)。

副作用命令绑定 canonical exact version、action、scheme、日期/回测起点和 operator，并保存 operation
scope SHA-256。Blackbox activation 直接读取同 exact version、同当前脚本校验策略的成功持久化回测，不再绑定 Harness
run。Blackbox 当前主机生命周期只由本机数据库的 exact version 与 composite Registry 表达；首次上线和
同身份修订都由 `activate` 在单个数据库事务中完成，不再维护 config overlay、journal 或 reconcile 命令。

`config.yaml.schedule.timeout_sec` 是 executor 层方案预算，harness config schema 对 Blackbox 要求该字段存在且为正整数。Blackbox predict 的最终预算取方案申请、版本化 Runtime Profile 平台上限和显式 operation deadline（如有）的最小值；deadline 只能收紧。Blackbox backtest 使用独立的 Profile 预算。任何运行预算都不能替代对应 runtime 的 Gate 证据，也不能作为放宽 source fidelity、日期语义或 protected table guard 的理由。

---

## 3. 数据库安全边界

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
同一事务读取一次 `api_wind_indicators_all`，生成第五文件 `factor_catalog.csv`。
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
- 绕过首次 Blackbox activation 事务或 repository，直接把历史回测结果写入 `t_scheme_predictions`。
- 旧的 broad `scheduler.executor` 全量 CLI 已退役；合法运行入口仅为宿主 launchd/systemd one-shot runner、受控 Harness Gate 和 `signal-gap-fill` 的受支持模式。不得新建等价批量入口。
- 通过临时脚本绕过 `shared.input_artifacts` 生成算法输入。

---

## 4. 验收证据

验收按以下权威文档执行，不在架构文档重复逐项清单：

| 证据范围 | 权威要求 |
|---|---|
| Blackbox exact version、回测、输入、环境和激活 | [平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)、[生产准备清单](../blackbox_v2/PRODUCTION_READINESS.md) |
| Native Gate profile、source benchmark 与 live-safe 证据 | [Native 维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)、[源算法保真](SOURCE_ALGORITHM_FIDELITY.md) |
| 三日期、历史/灰度分区、指标和前端展示 | [预测日期语义](PREDICTION_SEMANTICS.md) |
| installed/loaded、自然运行和唯一 Writer | [生产调度治理](PRODUCTION_SCHEDULING_GOVERNANCE.md) |

保存运行预算与实际耗时，证明预算只控制执行等待、不改变算法输出；任何授权写入都须有前后核验，
证明只影响对应方案和允许的表。证据不足不得标记完成，技术验证不能替代授权或真实自然调度观察。
