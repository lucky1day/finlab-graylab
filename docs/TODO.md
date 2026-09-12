# 统一后续推进计划

**文档状态**：`CURRENT`

**整理日期**：2026-09-13；以下运营事项仍需各自现场核验，不因文档整理标记完成。

本文只保留尚未闭环的后续事项。当前稳定事实见[当前状态](CURRENT_STATUS.md)，生产规则见
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成事项通过 Git、Harness、数据库
与目标机 journal 追溯，不在本文维护副本。

## 五套新方案自然观察

- 双机交付与模拟验收已完成，精确版本、覆盖范围及证据见[当前状态](CURRENT_STATUS.md)。
- 分别只读观察三套月频 2026-09-15 18:00、两套周频 2026-09-19 11:30 Asia/Shanghai 的首次自然运行。
  核对 installed/loaded、日志、真实 `scheduled_live` run、prediction、exact、本机输入和 Dashboard；
  当前模拟与灰度补缺不计作自然运行。
- 当前任务已安排“五套新方案双机自然运行观察”只读 follow-up（automation ID `automation`），每日 19:30 检查，
  未到窗口或无新变化时保持安静；全部十个主机/方案通过后暂停，最迟 2026-09-21 报告未通过项后暂停。
  不授权自动重跑、补缺、业务写库、服务/调度修改或 Git 修改。

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
2. 只读核验 M0 周平均五方案及同期 ECS 周频方案在 2026-08-29 11:30 的首次自然触发证据；
   该时间已过去，不能继续描述为等待未来触发，也不能未经核验标记完成。
3. 未来若把生产域名或 Writer 从 Mac3 切到 ECS，必须作为新生产项目设计数据库 authority、单 Writer、
   DNS/Nginx、窗口和回滚，不能从灰度验收外推授权。
4. 公网刷新可靠性的 Actuals/预测窗口观察需核对既有日志与验收原件；旧执行记录未确认该观察完成。
   不因删除历史发布流水而标记通过，不重复注入生产故障。

## 尚缺现场验证的能力

- Blackbox `T+1/h1` target 半开区间批量已有本地候选与回归，尚缺现场验证；
  不据此宣称该路径已生产验收，也不为了验证向生产写入虚构或重复预测。
- 已完成迁移、W4 存量范围及只读历史来源见[当前状态](CURRENT_STATUS.md)。
  本文运营队列不是新 Blackbox 方案开始 Intake 的阻塞条件。

## 统一停止条件

- 需要改变 M0 信号、方向映射或已确认的 MID/CQ/SF 语义；
- 需要新增 schema、timer、plist、常驻 scheduler、ledger、第二写入入口或隐式 fallback；
- 需要覆盖或删除 insert-only 历史预测，或把 gray live 冒充 scheduled live；
- 文档、代码、输入摘要、数据库身份、release manifest、installed unit 或现场状态与计划前提不一致；
- 出现无法由权威日历、精确版本、持久化 Gate 或现场读回消除的不确定点。

触发停止条件时保留现场和证据，向用户说明确定事实、影响和需要选择的事项，不猜测继续。
