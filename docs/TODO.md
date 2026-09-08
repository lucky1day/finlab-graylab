# 统一后续推进计划

**文档状态**：`CURRENT`

**最后核验日期**：2026-09-06

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

## Native V1 全量迁移

26 个 Native base / 30 个业务 target 已进入按 wave 迁移到全新 Blackbox V2 successor 的执行阶段，权威身份
映射、批次顺序、验收、回滚、停止条件和清理边界见
[Native V1 全量迁移至 Blackbox V2](architecture/NATIVE_V1_TO_BLACKBOX_V2_MIGRATION.md)。首夜本地候选已完成
原子迁移工具、日频 `T+1` gray target 区间能力及 6 个 T1/T5、3 个 weekly point successor。

2026-09-08 最新授权覆盖此前推进顺序：本阶段仅 ECS，Mac3 的 current/previous、数据库、launchd、对外
域名及流量不变；不自动晋级 Mac3，W4 九个 binary bundle 暂停，恢复须另获授权。

1. 复用 W3A 已完成的完整原始等价与正式回测：full-OOS 等价输入为 09-05，cons-sda 等价输入为 09-08，
   两者正式回测均为 09-08。受控 receipt 按各算法保留输入身份，不能为旧 wave 顶层单输入结构重复计算。
   原始证据至少保留到 receipt 与切换验收闭环；不重跑已通过算法，不恢复重复/倒序/乱序/子集矩阵。
2. 算法等价与正式入库各自绑定其输入。保留同代码、环境、完整日期覆盖及各自内部一致性；补齐 W3A 受控
   凭据接入，不手写成功 receipt。正式回测、单 Writer、原子 cutover/rollback、旧事实不变仍是必要边界。
3. ECS release 严格完整性、部署矩阵、增量预热和证据均就绪后，推进 W3A 切换与真实 systemd one-shot 模拟。
4. 随后完成 W2 与 W3B-D 的必要算法改造、完整同输入旧新回测对照及受控 ECS 替换。
5. 不以本阶段 ECS 完成宣布全局 Native 退役；Mac3 仍依赖的旧路径不得提前删除，confidence DDL 仍须独立授权。

## 统一停止条件

- 需要改变 M0 信号、方向映射或已确认的 MID/CQ/SF 语义；
- 需要新增 schema、timer、plist、常驻 scheduler、ledger、第二写入入口或隐式 fallback；
- 需要覆盖或删除 insert-only 历史预测，或把 gray live 冒充 scheduled live；
- 文档、代码、输入摘要、数据库身份、release manifest、installed unit 或现场状态与计划前提不一致；
- 出现无法由权威日历、精确版本、持久化 Gate 或现场读回消除的不确定点。

触发停止条件时保留现场和证据，向用户说明确定事实、影响和需要选择的事项，不猜测继续。
