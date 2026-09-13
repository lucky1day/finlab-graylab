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
| `harness.gates.dashboard_gate` | 对唯一 Dashboard API 做受限 GET，对照本机 Registry 展示字段，验证 V6 Summary、active composite、任务字段和结果分区 | HTTP 与数据库均只读；不证明 exact、调度缺口或自然运行 |

## 2. 验证与执行边界

Blackbox 回测验证平台调用、输入与标准输出边界；交付算法的确定性、分批/顺序一致与未来数据隔离
由上游负责，见[源算法保真](SOURCE_ALGORITHM_FIDELITY.md)。activation 严格加载 canonical 当前身份，
只接受 exact version 和当前脚本校验策略匹配的成功回测，不重复执行 AST/Metadata 校验。

`config.yaml.schedule.timeout_sec` 是方案执行预算。Blackbox predict 的实际预算取方案申请、Runtime Profile
上限和显式 operation deadline（如有）的最小值；deadline 只能收紧，backtest 使用独立 Profile 预算。
证据保留实际耗时和采用的预算，不能以预算调整放宽输出、输入或保真要求。

DashboardGate 接受 active 方案尚无 live 记录的合法 Summary；它不读取 run 或日历重算调度状态。
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
