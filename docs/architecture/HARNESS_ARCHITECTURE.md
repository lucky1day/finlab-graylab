# Harness 验证与事务架构

**文档状态**：`CURRENT`
**目标读者**：Harness 开发与平台入库人员

本文定义 Blackbox 验证与业务事务的实现边界。入库步骤只见
[Blackbox 平台 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)；W4 只维持固定版本运行，见
[运行维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)。Harness 不实现算法、输入口径或独立调度框架。

## 1. 现役模块与 Gate 职责

| 模块 | 职责 | 边界 |
|---|---|---|
| `harness.contracts.onboarding_policy` | 加载 W4 存量清单，供公共集合合同检查 | 不提供 Native 版本准入 |
| `harness.contracts.import_rules` | 静态扫描生产包的分层依赖 | 不证明运行期 I/O 或操作权限 |
| `shared.blackbox_v2.intake` | 两文件交付入库 | 只准备 canonical，不等于数据库激活 |
| `harness.blackbox_v2.gates` | 回测前复验交付安全边界；执行批量 Request、校验 Result 并保存 exact、输入和环境证据 | 必须 persist，只经 `backtests.repository` 写 `t_backtest_*`；不额外运行 predict 冒烟 |
| `harness.gates.activate_gate` 的严格身份入口与 `harness.blackbox_v2.activation` | 核对成功回测与当前 canonical，调用生命周期事务 | 不用技术验证替代操作授权 |
| `harness.gates.dashboard_gate` | 对唯一 Dashboard API 做受限 GET，对照本机 Registry 展示字段，验证 V7 Summary、active composite、任务字段和结果分区 | HTTP 与数据库均只读；不证明 exact、调度缺口或自然运行 |
| `harness.gates.data_consistency_gate` | 在独立数据库快照中校验预测业务键、三日期、run 引用与 Actual 关联，并独立聚合后对账真实 Summary/Detail | 不调用 Dashboard builder 或其聚合 helper；不写运行台账、不推导日历缺口 |

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

DataConsistencyGate 与 DashboardGate 分工不同：前者把
`scheme_id + target_tenor + horizon + target_date` 作为预测业务键，exact version 不参与去重；校验三日期、
方向和 `run_id/backtest_run_id` 的互斥及同方案引用后，按 Registry `task_type` 对应的 target rule、tenor、
target date 关联 Actual。回测发布事实只用 immutable `backtest_actual_direction`，live 事实只关联 Actual 表，
未成熟目标保留 pending。Gate 再独立形成 month/source 的已验证样本、指标样本、方向分布、正确数和 true
positive，并逐分区请求真实 Detail 对账；不得调用 Dashboard builder 或聚合 helper 生成标准答案。

缺少原生 exact 的已发布旧事实默认阻断。唯一例外是
[`legacy_prediction_migration_compatibility_v1.json`](../../deploy/legacy_prediction_migration_compatibility_v1.json)
精确登记的只读迁移批次：清单同时绑定旧输入、summary、source/product 全事实、日期差异集合及经业务确认的
纠正版 Blackbox exact 身份；纠正版 archived Registry、retired version、成功回测和逐事实原件由
[`legacy_prediction_migration_evidence_v1.json`](../../deploy/legacy_prediction_migration_evidence_v1.json)
冻结，Gate 会重算其摘要并验证现行三日期合同。Gate 只在整批证据全部命中时把它报告为
`legacy_migration`；不回填数据库，
不宣称旧事实由纠正版 exact 原生生成，也不允许该例外用于未来写入。任一事实或摘要漂移仍失败。

该清单登记的 `liwei_0616_5y01_full_oos_k3_div_k10` 5Y/h5 已单独授权清除旧回测产品事实。
若该范围没有回测分区，DataConsistency 只在保留的 78 条 live 事实与清单所冻结的日期范围、条数及
业务值 SHA-256 完全一致时接受 live-only；缺行或方向漂移为失败。仅这些事实引用的 retired Native
版本可容忍原本缺失的 `manifest_hash` 和空 `input_artifact_id`；已有非空但失效的字段、范围外事实及
后续 Blackbox 仍按完整合同检查。此例外不恢复、推断或声称已删除的回测执行血缘存在。

早期 Blackbox 回测若只缺少后来新增到 `t_backtest_runs` 的冗余血缘列，也不能按名称推断来源。
[`legacy_backtest_lineage_compatibility_v1.json`](../../deploy/legacy_backtest_lineage_compatibility_v1.json)
只登记已由 canonical 交付字节、版本 code/config/manifest hash、输入 snapshot、不可变 benchmark、完整
summary 摘要以及 raw/product 逐事实摘要共同唯一证明的历史 run。Gate 仅在整批证据同时命中时从版本行恢复
该 run 的只读解释；不更新数据库、不接受部分事实，也不对未登记 run 放宽血缘要求。

一次一致性验收先读取一个 repeatable-read 只读数据库快照，再获取一次真实 Summary 和所需 Detail；HTTP
对账后重读同一选定范围的摘要围栏。若末次重读失败、首尾摘要不同，或上海展示日跨界，本次返回
`blocked`，要求在稳定输入窗口重试，不能把可观测到的自然增长误报为事实或展示错误；该围栏不是 CDC，
不承诺识别首尾内容完全恢复的瞬态变化。CLI 可重复 `--scheme-id` 批量选择 base
scheme，并复用 Dashboard Gate 的私有 session 文件或文件描述符入口；数据库必须通过显式
`engine_factory` 绑定 HTTP 服务所在环境，不回退默认库。
补缺复用既有 planner、executor 和 repository，支持范围只见
[平台 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#6-灰度区间批量物化)，不属于入库 Gate。

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
