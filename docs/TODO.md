# 统一后续推进计划

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-24

本文只保留尚未发生的后续事项。当前稳定事实见[当前状态](CURRENT_STATUS.md)，生产规则见
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。M0 月均、季均、年均 15 个方案已在
ECS 与 Mac3 达到 Onboarding Complete，已到期历史 Prediction/Actual 和公网展示均已闭环；一次性入库与同步
清单已从本文清理。

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

1. `five_y_factor_rule_online_v1` 与 `ten_y_factor_level_ensemble_v1` 等待 2026-08-24 07:03
   Asia/Shanghai 的首次真实 daily 自然触发；不得 kickstart、覆盖日期或倒签信号。
2. M0 周平均五方案及同期 ECS 周频方案等待 2026-08-29 11:30 的首次自然触发。
3. 将已验证的快速补缺方式固化为受控工具：同一 immutable release、方案版本、预测日期、输入业务摘要和
   lineage 全部匹配时，优先复用合格缓存或从 ECS 只读复制精确核心预测结果；目标 Writer 必须先停止，
   Mac3 只经 repository insert-only 导入，已有键整组拒绝。不得复制数据库主键、`run_id`、Actuals、回测或
   Harness 历史；源端不存在的业务键才重新计算。工具落地前不得临时放宽现有 launcher 或缓存校验。
4. 未来若把生产域名或 Writer 从 Mac3 切到 ECS，必须作为新生产项目设计数据库 authority、单 Writer、
   DNS/Nginx、窗口和回滚，不能从灰度验收外推授权。

ECS 继续独立灰度运行；Mac3 域名、Nginx、DNS、数据库 authority 和生产 Writer 保持不变。

## 统一停止条件

- 需要改变 M0 信号、方向映射或已确认的 MID/CQ/SF 语义；
- 需要新增 schema、timer、plist、常驻 scheduler、ledger、第二写入入口或隐式 fallback；
- 需要覆盖或删除 insert-only 历史预测，或把 gray live 冒充 scheduled live；
- 文档、代码、输入摘要、数据库身份、release manifest、installed unit 或现场状态与计划前提不一致；
- 出现无法由权威日历、精确版本、持久化 Gate 或现场读回消除的不确定点。

触发停止条件时保留现场和证据，向用户说明确定事实、影响和需要选择的事项，不猜测继续。
