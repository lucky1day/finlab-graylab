# 统一后续推进计划

**文档状态**：`CURRENT`

**整理日期**：2026-09-16；以下运营事项仍需各自现场核验，不因文档整理标记完成。

本文只保留尚未闭环的后续事项。当前稳定事实见[当前状态](CURRENT_STATUS.md)，生产规则见
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成事项通过 Git、外置材料、数据库
与目标机 journal 追溯，不在本文维护副本。

## 二次审计修复（2026-09-16）

以下问题以 `399df5bf` 为修复基线，按 P0 → P1 → P2 顺序处理；代码、隔离测试和真实环境验收是三个独立状态，
未完成现场读回前不得标记发布闭环。

| 编号 | 优先级 | 当前状态 | 待完成事项 |
|---:|:---:|---|---|
| 01 | P0 | 已实现待现场验收 | `factor-lab-http.js` 精确白名单已由 `3a665c36` 发布；页面资源自动发现、临时 Nginx 冒烟及新版公网入口检查已实现，待候选与公网读回。 |
| 02 | P0 | 已实现待验收 | DataConsistency 按 runtime 校验 Blackbox/W4 Native 合同，并只按精确证据允许历史跨 ID 回测引用。 |
| 03 | P0 | 已实现待现场验收 | Dashboard/DataConsistency Gate 分离安全 Origin 与应用 `api_prefix`，支持真实 `/bond-factor-lab` 公网路径。 |
| 04 | P1 | 已实现待验收 | HTTP 客户端在响应头阶段处理 401、request ID 与 Retry-After，不依赖错误正文完成。 |
| 05 | P1 | 已实现待验收 | 认证前端复用有界 HTTP 客户端，并隔离注销或身份切换后的旧请求。 |
| 06 | P1 | 已实现待验收 | 认证 JSON body 使用单一有界缓冲及总读取期限，正确处理断开。 |
| 07 | P1 | 已实现待现场验收 | Dashboard 流式读取提前退出时失效专用连接；隔离 MySQL 已验证清理时间和池恢复，待候选现场读回。 |
| 08 | P1 | 已实现待验收 | DataConsistency 分离完整性、血缘和展示结果，缺少权威范围时明确 BLOCKED。 |
| 09 | P1 | 已实现待 CI 验收 | 已建立 Python、Node、MySQL 和 release 四个独立任务，待远端候选确认必选层均执行且通过。 |
| 10 | P1 | 已实现待验收 | release 补齐日志/字节码规则，将 `source_evidence` 宽泛豁免收敛为摘要清单。 |
| 11 | P1 | 已实现待现场验收 | 公网脚本已删除匿名 Dashboard 200 与重复 schema，认证正向检查委托公共 Gate。 |
| 12 | P1 | 已实现待验收 | Actual 认领使用 INSERT `lastrowid`，删除逐行 `SELECT LAST_INSERT_ID()` 并显式命名写副作用。 |
| 13 | P1 | 已实现待验收 | DataConsistency 按 Actual 作用域去重、分块读取并把总 deadline 下传到每条 SQL。 |
| 14 | P2 | 已实现待验收 | 合并重复前端测试、引入可控计时器、删除 Python Node 转发并修正 Dashboard 测试文件名。 |
| 15 | P2 | 已实现待验收 | 删除 Dashboard 查询薄转发；保留公开 builder 与 `scheduler.repository` 统一写入口。 |
| 16 | P2 | 已实现待最终同步 | TODO、Dashboard 合同、验证矩阵和当前 CLI 命令已同步；发布通过后移除本轮临时清单，保留独立自然运行观察。 |

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
