# 统一后续推进计划

**文档状态**：`CURRENT`

**整理日期**：2026-09-13；以下运营事项仍需各自现场核验，不因文档整理标记完成。

本文只保留尚未闭环的后续事项。当前稳定事实见[当前状态](CURRENT_STATUS.md)，生产规则见
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成事项通过 Git、外置材料、数据库
与目标机 journal 追溯，不在本文维护副本。

## 八套新方案自然观察

- 双机交付与模拟验收已完成，精确版本、覆盖范围及证据见[当前状态](CURRENT_STATUS.md)。
- 分别只读观察三套月频 2026-09-15 18:00、五套周频 2026-09-19 11:30 Asia/Shanghai 的首次自然运行。
  逐 ID 范围由当前状态的八套身份表确定；按[自然运行证据链](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)核对 installed/loaded、日志、真实 `scheduled_live` run、prediction、exact、本机输入和 Dashboard。模拟与灰度补缺不计作自然运行。
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
3. 公网刷新可靠性的 Actuals/预测窗口观察需核对既有日志与验收原件；旧执行记录未确认该观察完成。
   2026-09-13 平台清理发布时又观察到一次公网 504，后续降频复核通过；事实与证据见[当前状态](CURRENT_STATUS.md#已发布的平台清理与收盘候选修复)。需定位慢请求原因并完成窗口观察，不以单轮复核或删除历史流水标记可靠性问题解决，不重复注入生产故障。

## 尚缺现场验证的能力

- Blackbox `T+1/h1` target 半开区间批量已有本地候选与回归，尚缺现场验证；
  不据此宣称该路径已生产验收，也不为了验证向生产写入虚构或重复预测。

以上观察和验证队列不阻塞新 Blackbox 方案开始 Intake；入库仍按自身前置条件执行。

## Dashboard 认证探针入口

现有 Dashboard Gate 默认 HTTP fetcher 不携带登录会话，CLI 未提供认证参数，不能直接作为已启用认证环境的验收命令。当前可用方法见[Dashboard 合同](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#认证响应与合同验收)。通用 CLI 的完成标准是安全传递会话、保护凭据并验证真实 HTTP 合同，需单独安排实现与验收。

## 候选 release 手工启动入口

手工 Harness 的目标环境前提见[部署手册](../deploy/README.md#手工-harness-的目标环境绑定)。目前自然调度的环境加载不能直接等同于手工入口，执行前仍须审查候选 cwd、release/runtime、deployment target、数据库覆盖优先级及 Profile。后续若统一入口，应复用现有可信配置读取能力，在隔离环境验证两机路径、错误身份/覆盖值拒绝和零意外写入；不通过恢复批次脚本或修改生产调度解决。

所有后续工作遵守[根规范](../AGENTS.md)与[调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)的授权、停止与恢复边界；发现现场与计划不一致时保留证据，先确认再操作。
