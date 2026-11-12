# Harness 验证与事务架构

**文档状态**：`CURRENT`
**目标读者**：Harness 开发与平台入库人员

本文定义 Blackbox 验证与业务事务的实现边界。入库步骤只见
[Blackbox 平台 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)；W4 只维持固定版本运行，见
[运行维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)。Harness 不实现算法、输入口径或独立调度框架。

## 1. 现役模块与 Gate 职责

| 模块 | 职责 | 边界 |
|---|---|---|
| `tests.support.import_rules` | 测试侧静态扫描生产包的分层依赖 | 不证明运行期 I/O 或操作权限 |
| `shared.blackbox_v2.intake` | 两文件交付入库 | 只准备 canonical，不等于数据库激活；写入的 `status: paused`/`version_status: draft` 不参与 exact，运行期由数据库生命周期覆盖 |
| `harness.blackbox_v2.gates` | 回测前复验交付安全边界；执行批量 Request、校验 Result 并保存 exact、输入和环境证据 | 必须 persist，只经 `backtests.repository` 写 `t_backtest_*`；不额外运行 predict 冒烟 |
| `harness.gates.activate_gate` 的严格身份入口与 `harness.blackbox_v2.activation` | 核对成功回测与当前 canonical，调用首次或修订生命周期事务 | 不读取、不修改 config 状态；不用技术验证替代操作授权 |
| `harness.gates.dashboard_gate` | 对唯一 Dashboard API 做受限 GET，对照本机 Registry 展示字段，验证 V7 Summary、active composite、任务字段和结果分区 | HTTP 与数据库均只读；不证明 exact、调度缺口或自然运行 |
| `harness.gates.data_consistency_gate` | 在一致性数据库快照中校验产品事实完整性与血缘（run、exact、backtest 引用、冻结清单），并要求真实 Summary/Detail 的 HTTP 响应等于后端构建函数在同一数据库上的输出 | 展示语义只在 backend 定义一处，Gate 不再独立重算聚合；不写运行台账、不推导日历缺口 |

## 2. 验证与执行边界

Blackbox 回测验证平台调用、输入与标准输出边界；交付算法的确定性、分批/顺序一致与未来数据隔离
由上游负责，见[源算法保真](SOURCE_ALGORITHM_FIDELITY.md)。activation 严格加载 canonical 当前身份，
只接受 exact version 和当前脚本校验策略匹配的成功回测，不重复执行 AST/Metadata 校验。

`config.yaml.schedule.timeout_sec` 是方案执行预算。Blackbox predict 的实际预算取方案申请、Runtime Profile
上限和显式 operation deadline（如有）的最小值；deadline 只能收紧，backtest 使用独立 Profile 预算。
证据保留实际耗时和采用的预算，不能以预算调整放宽输出、输入或保真要求。

DashboardGate 接受 active 方案尚无 live 记录的合法 Summary；它不读取 run 或日历重算调度状态。
一次 Dashboard 验收可重复指定多个 base scheme，Gate 只获取一次真实 V7 Summary、一次批量读取这些方案的
Registry，再按方案分别输出通过或失败。各方案结果引用同一个 `snapshot_id`、HTTP 获取时间和
`X-Request-ID`，不为每个方案重复请求 Summary，也不向运行服务增加缓存。

DataConsistencyGate 与 DashboardGate 分工不同：前者在同一一致性快照内做两类检查。完整性：用成功 run 与不可变回测事实建立预期业务键
（`scheme_id + target_tenor + horizon + target_date`，exact version 不参与去重），核对每个应有键恰好一条产品事实及 Actual 成熟度；
血缘：核对 live run、版本、exact、回测引用与输入快照证据，早于 exact 记录的回测事实按冻结清单判定。展示对账：用后端
`build_factor_lab_dashboard`/`build_factor_lab_dashboard_detail` 在同一数据库上生成预期载荷，要求所选方案的 Summary 条目与逐分区 Detail
的 HTTP 响应逐字段相等（忽略 `snapshot_id`/`generated_at`）；它验证传输、缓存与序列化没有改变内容，展示口径本身由 Dashboard 测试保护。

2026-11-12 起，历史一致性证据账本（旧迁移兼容清单、纠正版 exact 证据、旧回测血缘兼容清单、历史回测
引用兼容清单及其解析代码）已退役。曾命中这些例外的历史事实不再有任何特殊通道：按通用规则判定——缺少
run 血缘、`persisted_prediction_count`、exact 版本血缘或输入身份的，作为 authority gap（结论 BLOCKED，
不是通过，也不崩溃）；跨方案引用的回测 run、哈希或快照与版本行不符、日期违反现行任务合同、事实
缺失或漂移的，仍判错误。没有回测分区的 active 范围同样只是 authority gap。新事实不享有任何例外。

早于 exact 记录发布的回测产品事实（`scheme_version` 为空，且来自回测 run）按决定不补写 exact。
[`unversioned_backtest_facts_v1.json`](../../deploy/unversioned_backtest_facts_v1.json) 冻结其
方案、期限、horizon、条数与规范事实摘要。血缘检查按组逐一核对：与清单完全一致的记为一条已知
authority gap（结论保持 BLOCKED，不是通过）；清单之外的空 exact、条数或摘要变化、无法规范化的事实
判错误，且不中断同一范围内其它方案的检查。实盘事实的空 exact 仍直接失败。清单随历史冻结，
不为新事实提供例外；修订必须两端只读重新生成并逐项核对。

一次一致性验收先读取一个 repeatable-read 只读数据库快照，再获取一次真实 Summary 和所需 Detail；HTTP
对账后重读同一选定范围的摘要围栏。若末次重读失败、首尾摘要不同，或上海展示日跨界，本次返回
`blocked`，要求在稳定输入窗口重试，不能把可观测到的自然增长误报为事实或展示错误；该围栏不是 CDC，
不承诺识别首尾内容完全恢复的瞬态变化。CLI 可重复 `--scheme-id` 批量选择 base
scheme，并复用 Dashboard Gate 的私有 session 文件或文件描述符入口；数据库必须通过显式
`engine_factory` 绑定 HTTP 服务所在环境，不回退默认库。
补缺复用既有 planner、executor 和 repository，支持范围只见
[平台 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#6-区间补跑)，不属于入库 Gate。

## 3. 业务事务与命令作用域

人工副作用命令绑定 canonical exact version、action、scheme、日期/回测起点与 operator，保存 operation
scope SHA-256。作用域校验限制本次命令所能操作的身份和区间，不是操作者身份认证或生产授权。
Blackbox 回测经 `backtests.repository` 原子写入不可变 `t_backtest_*`；首次 activation 经
`scheduler.repository` 在生命周期事务中 insert-only 发布历史产品事实并激活身份。
命令报告不代替事务结果，不能用补偿脚本覆盖已发布事实。

## 4. 证据归属

| 证据 | 权威要求 |
|---|---|
| exact、回测、输入、环境和激活 | [平台 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)、[生产准备](../blackbox_v2/PRODUCTION_READINESS.md) |
| W4 固定版本与运行依赖 | [W4 运行维护](../sop/NATIVE_V1_MAINTENANCE_SOP.md) |
| 历史源码、benchmark 与迁移依据 | [源算法保真](SOURCE_ALGORITHM_FIDELITY.md) |
| 三日期、业务键、Actual、指标与公开分区 | [预测语义](PREDICTION_SEMANTICS.md) |
| installed/loaded、唯一 Writer 与自然运行 | [调度治理](PRODUCTION_SCHEDULING_GOVERNANCE.md) |

具体 run、输入摘要和操作产物保留在控制面及外置证据目录；[当前状态](../CURRENT_STATUS.md)记录核验结论与证据位置。
