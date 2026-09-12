# 统一后续推进计划

**文档状态**：`CURRENT`

**最后核验日期**：2026-09-12（Native 迁移队列；其他队列仍需各自现场核验）

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

## Native 后续维护边界

17 个源码方案的原 ID 双机接管已验收，迁移过程与清洁发布核验见
[迁移验收记录](architecture/NATIVE_V1_TO_BLACKBOX_V2_MIGRATION.md)，不再安排重复算法验证或历史重算。
W4 九个加密方案保持 Mac3 Native 及必要依赖，没有待执行的 binary bundle 改造计划。
两个 archived W3A 临时身份已获批作为只读历史来源保留，执行版本 retired、不具备 Writer；
不再安排物理删除，不为消除名称而修改原 ID 的四条历史预测及其来源外键。
历史搬迁/删除及域名切换不是本轮剩余步骤。独立授权的平台 confidence 退役已完成双机部署、DDL 和验收，
见[当前状态](CURRENT_STATUS.md#平台-confidence-退役验收)，不再保留可重复执行的生产清理队列。
上面的 M0/其他方案自然观察属于独立运营队列，不是本次迁移或新 Blackbox 方案开始 Intake 的阻塞条件。


## 统一停止条件

- 需要改变 M0 信号、方向映射或已确认的 MID/CQ/SF 语义；
- 需要新增 schema、timer、plist、常驻 scheduler、ledger、第二写入入口或隐式 fallback；
- 需要覆盖或删除 insert-only 历史预测，或把 gray live 冒充 scheduled live；
- 文档、代码、输入摘要、数据库身份、release manifest、installed unit 或现场状态与计划前提不一致；
- 出现无法由权威日历、精确版本、持久化 Gate 或现场读回消除的不确定点。

触发停止条件时保留现场和证据，向用户说明确定事实、影响和需要选择的事项，不猜测继续。
