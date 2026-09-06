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
原子迁移工具、日频 `T+1` gray target 区间能力及 6 个 T1/T5、3 个 weekly point successor；它们尚未形成
ECS 当前 generation 的完整持久化 backtest 与受控 comparator receipt，因此不得执行 ECS activation、Registry
切换、业务写库或 systemd 操作。

V28 的 W2 与 Liwei 的 W3A-W3D 均已在冻结五文件和真实 Request 下触发性能硬停止条件，现有 Native 保持
不变。W2 已有两文件候选和方向零差异证据，但 ECS 4-vCPU 现场仍不能满足完整区间 1800 秒门槛；W3A-W3D
连单条 predict 的 120 秒门槛都未通过。daily/monthly 0629 与 weekly average 0529 共 9 个编译主体方案已按
用户决定进入 manifest-bound Mac3-only binary bundle 例外，不再等待可读源码，也不进入 ECS；它们必须在前
17 个可读源码 Native 完成 ECS 验证并以同一 immutable archive 晋级 Mac3 后再推进。因此当前先决阻塞是
W2/W3 的独立算法性能或 ECS 计算规格，不是 SSH、W4 源码或接入合同。Mac3 晋级、launchd 操作与 confidence
DDL 仍是后续独立门槛，最终 Native 清理和 migration 025 不能提前执行。

## 统一停止条件

- 需要改变 M0 信号、方向映射或已确认的 MID/CQ/SF 语义；
- 需要新增 schema、timer、plist、常驻 scheduler、ledger、第二写入入口或隐式 fallback；
- 需要覆盖或删除 insert-only 历史预测，或把 gray live 冒充 scheduled live；
- 文档、代码、输入摘要、数据库身份、release manifest、installed unit 或现场状态与计划前提不一致；
- 出现无法由权威日历、精确版本、持久化 Gate 或现场读回消除的不确定点。

触发停止条件时保留现场和证据，向用户说明确定事实、影响和需要选择的事项，不猜测继续。
