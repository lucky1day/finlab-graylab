# 统一后续推进计划

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-28

本文只保留尚未发生的后续事项。当前稳定事实见[当前状态](CURRENT_STATUS.md)，生产规则见
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成事项通过 Git、Harness、数据库
与目标机 journal 追溯，不在本文维护副本。

## M0 周期均值自然观察

1. 15 个新方案继续复用 ECS 已安装的 close-period systemd one-shot/timer；不新增或修改 timer，不伪造日期、
   kickstart 或倒签信号。
2. 权威日历当前给出的下一月均 MID 锚点为 2026-09-15，下一季均 CQ 锚点为 2026-09-30。到期后读回
   journal、run、`scheduled_live`、scheme version、DataBridge generation、Actual 和 Dashboard；全部成功后才
   把对应方案标记为 Production Observed。
3. ECS 当前权威交易日历止于 2026-12-31，尚不能权威推导下一年均 SF 锚点。日历自然扩展后重新计算，不按
   历史节假日或自然年猜测日期。
4. 现有人工补缺与本地权威 Actual 刷新不计作自然观察。未完成目标桶继续显示 pending；只有 installed
   systemd/launchd 在权威锚点自然触发并产生 `scheduled_live` 后，才更新 Production Observed 状态。

## 其他未闭环队列

1. 只读核验 `five_y_factor_rule_online_v1` 与 `ten_y_factor_level_ensemble_v1` 在 2026-08-24 07:03
   Asia/Shanghai 的首次真实 daily 自然触发结果；在 journal、run、`scheduled_live`、输入 generation 和
   Dashboard 证据完整前，不得标记 Production Observed，也不得 kickstart、覆盖日期或倒签信号。
2. M0 周平均五方案及同期 ECS 周频方案等待 2026-08-29 11:30 的首次自然触发。
3. 未来若把生产域名或 Writer 从 Mac3 切到 ECS，必须作为新生产项目设计数据库 authority、单 Writer、
   DNS/Nginx、窗口和回滚，不能从灰度验收外推授权。

## 稳定观察与后续晋级

1. Native 迁移近期暂停，最早在周末稳定窗口重新启动；窗口前只收集现场耗时和结果证据，不改方案身份、算法
   或 Registry 所有权。

## 周末 Native 迁移候选队列

26 个 active Native 已按依赖关系分类。10 个 `liwei_0616_*` 方案依赖跨方案 Phase-A cache，继续保留 Native；
只有上游能提供无需该持久共享 cache、可独立高效运行的 Blackbox V2 两文件 successor 时才重新评估。

其余 16 个只在周末稳定窗口且收到合法 successor 交付后，按共享代码和耦合关系分批迁移，顺序固定为：

1. monthly 0629 三方案；
2. weekly average 0529 三方案；
3. daily 0629 三方案；
4. V28 两方案；
5. weekly point 0529 三方案；
6. 相互依赖的 `t1_daily + t5_daily` 两方案。

每批开始条件是上游交付使用新 successor base ID 的合法两文件方案；平台不得从 Native 源码自行包装、改写或
冒充 Blackbox。每批必须先冻结结果和性能基线，再完成 Intake、持久化回测、日期/结果/必要 extra 等价、性能不
降低、双环境灰度、Registry 所有权切换与回滚窗口。只有该批闭环后才能删除该批专属 adapter、source/backtest
runner、CompareGate 和测试；共享给未迁移 Native 的代码和测试不得提前删除。

P2 真实零写入模拟已把完整 daily 墙钟降至约 12 分钟；P4 调度计划中的 discovery 与 lifecycle 解析仅为毫秒级，
不是下一瓶颈。后续必须先用现场耗时证明算法执行中的明确收益，再修改代码；不为推测性收益增加缓存、模块、
审计字段或第二套控制面。

ECS 继续独立灰度运行；Mac3 域名、Nginx、DNS、数据库 authority 和生产 Writer 保持不变。

## 统一停止条件

- 需要改变 M0 信号、方向映射或已确认的 MID/CQ/SF 语义；
- 需要新增 schema、timer、plist、常驻 scheduler、ledger、第二写入入口或隐式 fallback；
- 需要覆盖或删除 insert-only 历史预测，或把 gray live 冒充 scheduled live；
- 文档、代码、输入摘要、数据库身份、release manifest、installed unit 或现场状态与计划前提不一致；
- 出现无法由权威日历、精确版本、持久化 Gate 或现场读回消除的不确定点。

触发停止条件时保留现场和证据，向用户说明确定事实、影响和需要选择的事项，不猜测继续。
