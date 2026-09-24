# 统一后续推进计划

**文档状态**：`CURRENT`

**整理日期**：2026-09-21；以下运营事项仍需各自现场核验，不因文档整理标记完成。

本文只保留尚未闭环的后续事项。当前稳定事实见[当前状态](CURRENT_STATUS.md)，生产规则见
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成事项通过 Git、外置材料、数据库
与目标机 journal 追溯，不在本文维护副本。

## 旧增量方案缓存恢复与信号补齐

2026-09-21 九个组合缓存恢复和 39 条补缺已完成，完成证据见
[当前状态](CURRENT_STATUS.md#旧增量方案缓存恢复与信号补齐)。本节仅保留后续未闭环项：

- 修复后的 Mac3 六套、ECS 三套须在真实后续日频窗口逐主机、逐方案核验 `scheduled_live`、宿主
  触发、本机输入、exact、prediction 与 Dashboard。当前 installed/loaded 的下一日频窗口为
  2026-09-22 07:03 Asia/Shanghai；人工缓存验收与 39 条 `gray_live` 不算自然成功。
- 本次范围的全历史 DataConsistency 仍未通过：旧回测缺持久化样本数依据、旧产品记录 exact 为空，
  以及 2025-01-26 旧目标日期与现行 T+5 日历合同不符。逐机旧 run、数量和例子见
  [维护报告](/Users/macstudio0/bond-factor-lab-runtime/releases/four-v3-20260920/state-repair-20260921/README.md)。
  需先核对原始证据与迁移合同，单独形成处理方案；本轮未授权历史重写或删除，不伪填字段或放宽 Gate。
- 三套 5Y AUC/IC 的周数据成熟会改变已保存周前缀；本次重建未修改算法规则，不证明长期不再复发。
  算法修订由上游按冷/热等价合同交付后另行正式入库，不把一次重建当作此问题关闭。

## 四套 V3 新方案自然观察与页面验收

- 四套身份、exact 与部署验收见[当前状态](CURRENT_STATUS.md#四套-v3-新方案交付状态)。截至
  2026-09-24 凌晨，严格自然验收仍为 2/8（ECS 日频两套）；9 月 21—23 日两机日频累计
  十二条成功 `scheduled_live`、预测、exact、本机输入及 Dashboard 均通过，无本批日频缺口。
  Mac3 两项仍待宿主触发事件：9 月 22、23 日的统一日志查询无匹配，现存轮转日志不覆盖
  对应 07:03 时段；须在后续自然触发后日志尚保留时取回宿主事件，不能仅凭执行器输出关闭。
  证据见[本次观察](/Users/macstudio0/bond-factor-lab-runtime/releases/four-v3-20260920/observations/20260924/README.md)。
- 周频四项首次窗口为 2026-09-26 11:30 Asia/Shanghai，feature 2026-09-24、target 2026-09-30，
  尚未到期。按各机时钟、权威日历和真实 installed/loaded 再核验，不把 `gray_live` 补齐算作自然运行。
- 逐主机、逐方案只读核对触发日志、成功 `scheduled_live` run、prediction、exact、实际 release、
  本机输入与 Dashboard。八个组合全部取得证据后才关闭自然观察项；不授权重跑、补缺、业务写库、
  DDL、服务重启或调度/网络/认证配置变更。
- 本任务跟进 `v3` 已读回 ACTIVE，每日 19:45 检查；无变化时保持安静，仅通知新成功、失败或需处理
  事项，全部通过后暂停，最迟 2026-09-27 汇报未通过项后暂停。证据保存至
  [本机交付目录](/Users/macstudio0/bond-factor-lab-runtime/releases/four-v3-20260920/README.md)。
- Mac3 公网连接可靠性及额外全量 DataConsistency 复核仍待完成：两轮展示对账及一次登录前复核遭遇 TLS EOF，
  本机同一合同与公网入口专项检查全部通过；尚未确定连接断开的根因。
  保留外置失败回执，只读定位可以继续，代理/网络配置修复须另行明确授权。
- 浏览器视觉刷新仍待验收。Computer Use 已因当前 URL 限制终止交互，不重试受限路径；HTTP Gate
  与公网资源检查不代表视觉刷新通过，该待办独立于自然运行观察。

## 1Y T+1 跨期限日内方案自然观察

- 2026-09-17 07:03 Asia/Shanghai 后，只读核验 `one_y_t1_cross_tenor_intraday_v1` 在 ECS 和 Mac3
  分别产生 exact `4f0b95e57fdf` 的真实 `scheduled_live` run 与 2026-09-17 target 预测；同时核对两机
  installed/loaded 控制面、日志、本机 DataBridge generation 和 Dashboard。2026-09-16 完成的 77 条
  `gray_live` 补齐不计作自然运行。
- 观察不授权手工运行算法、补缺、业务写库、服务重启或调度修改。双机全部通过后，将证据更新到
  [当前状态](CURRENT_STATUS.md#1y-t1-跨期限日内方案交付状态)和本机
  [外置证据索引](/Users/macstudio0/bond-factor-lab-runtime/releases/one-y-t1-cross-tenor-20260916/README.md)，
  再从本文移除该待办。

## 八套新方案自然观察

- 分别只读观察三套月频 2026-09-15 18:00、五套周频 2026-09-19 11:30 Asia/Shanghai 的首次自然运行。
  逐 ID 范围由[当前状态的八套身份表](CURRENT_STATUS.md#八套新方案交付状态)确定；按[自然运行证据链](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)核对 installed/loaded、日志、真实 `scheduled_live` run、prediction、exact、本机输入和 Dashboard。模拟与灰度补缺不计作自然运行。
- 2026-09-13 记录的“八套新方案双机自然运行观察”只读 follow-up（automation ID `automation`）每日 19:30 检查；实际启停状态以自动化配置读回为准。
  未到窗口或无新变化时保持安静；全部十六个主机/方案通过后暂停，最迟 2026-09-21 报告未通过项后暂停。
  不授权自动重跑、补缺、业务写库、服务/调度修改或 Git 修改。

## M0 周期均值自然观察

1. 15 个新方案继续复用 ECS 已安装的 close-period systemd one-shot/timer；不新增或修改 timer，不伪造日期、
   kickstart 或倒签信号。
2. 权威日历当前给出的下一月均 MID 锚点为 2026-09-15，下一季均 CQ 锚点为 2026-09-30。到期后读回
   journal、run、`scheduled_live`、scheme version、DataBridge generation、Actual 和 Dashboard；全部成功后才
   把对应方案标记为 Production Observed。
3. ECS 当前权威交易日历止于 2026-12-31，尚不能权威推导下一年均 SF 锚点。日历自然扩展后重新计算，不按
   历史节假日或自然年猜测日期。
4. 未完成目标桶继续显示 pending；人工补缺和 Actual 刷新不能代替第 2 项的自然运行证据。

## 其他未闭环队列

1. 只读核验 `five_y_factor_rule_online_v1` 与 `ten_y_factor_level_ensemble_v1` 在 2026-08-24 07:03
   Asia/Shanghai 的首次真实 daily 自然触发结果；在 journal、run、`scheduled_live`、输入 generation 和
   Dashboard 证据完整前，不得标记 Production Observed，也不得 kickstart、覆盖日期或倒签信号。
2. 只读核验 M0 周平均五方案及同期 ECS 周频方案在 2026-08-29 11:30 的首次自然触发证据；
   该时间已过去，不能继续描述为等待未来触发，也不能未经核验标记完成。
3. 公网刷新可靠性的 Actuals/预测窗口观察仍未闭环。需定位[当前状态所记公网 504](CURRENT_STATUS.md#已发布的平台清理与收盘候选修复)的慢请求原因并完成窗口观察；单轮降频复核不代表可靠性问题解决，不重复注入生产故障。

## 尚缺现场验证的能力

- Blackbox `T+1/h1` target 半开区间批量已有本地候选与回归，尚缺现场验证；
  不据此宣称该路径已生产验收，也不为了验证向生产写入虚构或重复预测。

以上观察和验证队列不阻塞新 Blackbox 方案开始 Intake；入库仍按自身前置条件执行。

## 候选 release 手工启动入口

手工 Harness 的前置条件和现有能力限制见[部署手册](../deploy/README.md#手工-harness-的目标环境绑定)。后续若统一入口，应复用现有可信配置读取能力，在隔离环境验证两机路径、错误身份/覆盖值拒绝和零意外写入；不通过恢复批次脚本或修改生产调度解决。

所有后续工作遵守[根规范](../AGENTS.md)与[调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)的授权、停止与恢复边界；发现现场与计划不一致时保留证据，先确认再操作。
