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

## Native V1 全量迁移

按 2026-09-12 批准的[双机迁移计划](architecture/NATIVE_V1_TO_BLACKBOX_V2_MIGRATION.md)，
保留 26 个原方案 ID、30 个业务 target，只切执行版本；不再新建 successor 身份或重复回测。
ECS 已完成 17 个源码方案、21 个 target 的原 ID Blackbox 接管；用户要求先完成 ECS 陈旧代码清理，
验证后再一并同步 Mac3。W4 九个加密方案保持 Mac3 现有 Native 运行方式，不再改造 binary bundle。
不改变域名、DNS、Nginx 或隧道。已完成批次及精确证据只读迁移计划和现场，不重做。

1. 清理无调用者的已迁移方案专属复现 runner、已撤销的历史搬迁入口及对应一次性测试；
   不修改算法、当前 exact version、状态封装或历史数据库，不删除 W4 所需公共 Native 路径。
2. 版本绑定附件和迁移控制仍引用的临时目录分开处理：先证明调用/回滚依赖消失，再受控清理；
   不通过忽略 config/hash 校验直接删除附件，不把不可执行历史 DB 身份当成待删垃圾。
3. ECS 清理 release 通过全量回归、独立审查、原版本一致性和实际调度/Backend/Dashboard 核验后，
   再以同一 immutable archive 准备 Mac3 本机状态、版本与维护窗口；不重复已通过的历史算法计算。
4. Mac3 晋级保留自己的数据库、DataBridge、launchd 与 W4 原路径；不复制 ECS 业务记录，
   不改变公网入口。Mac3 尚未接管前，不宣称双机完成。
5. 两端接管及回滚边界验证后，继续删除失去用途的临时迁移工具与已迁移 Native 附件，
   但 W4 必需依赖保留。confidence DDL 仍分别请求两端独立确认。
   推送 `codex/develop`、更新到 `master` 的 PR；不合并或推送 `master`。

## 统一停止条件

- 需要改变 M0 信号、方向映射或已确认的 MID/CQ/SF 语义；
- 需要新增 schema、timer、plist、常驻 scheduler、ledger、第二写入入口或隐式 fallback；
- 需要覆盖或删除 insert-only 历史预测，或把 gray live 冒充 scheduled live；
- 文档、代码、输入摘要、数据库身份、release manifest、installed unit 或现场状态与计划前提不一致；
- 出现无法由权威日历、精确版本、持久化 Gate 或现场读回消除的不确定点。

触发停止条件时保留现场和证据，向用户说明确定事实、影响和需要选择的事项，不猜测继续。
