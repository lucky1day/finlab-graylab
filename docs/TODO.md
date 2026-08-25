# 统一后续推进计划

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-25

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
3. 将已验证的快速补缺方式固化为受控工具：同一 immutable release、方案版本、预测日期、输入业务摘要和
   lineage 全部匹配时，优先复用合格缓存或从 ECS 只读复制精确核心预测结果；目标 Writer 必须先停止，
   Mac3 只经 repository insert-only 导入，已有键整组拒绝。不得复制数据库主键、`run_id`、Actuals、回测或
   Harness 历史；源端不存在的业务键才重新计算。工具落地前不得临时放宽现有 launcher 或缓存校验。
4. 未来若把生产域名或 Writer 从 Mac3 切到 ECS，必须作为新生产项目设计数据库 authority、单 Writer、
   DNS/Nginx、窗口和回滚，不能从灰度验收外推授权。

## Native V1 收敛到 Blackbox V2

当前盘点为 26 个 Native base（日频 17、周频 6、月频 3）。由于 Blackbox 两文件合同是单 target，
`t1_daily` / `t5_daily` 必须从两个多 target base 拆为六个 successor，因此最终不少于 30 个新的 Blackbox
base。迁移使用新 successor ID，不在同一 ID 下原地改 `runtime_type`。

1. 冻结 Native V1 能力面：只修复政策清单内存量方案；所有新算法、新身份、新目标、新任务和替代版本只走
   Blackbox V2 两文件 Intake。
2. 先扩展向后兼容的 Blackbox Result 合同，使其能够承载必须保留的 `confidence` 与必要算法审计字段；同步建立
   一次性跨运行时等价检查。现有 Blackbox CompareGate 只验证合法输出，不能替代 Native oracle 对比。
3. 按执行族迁移：三个 weekly-point 规则 → 两个 V28 → `t1_daily` / `t5_daily` 的六个单 target successor →
   九个 source-package 方案（周均、月度、日度）→ 十个 Liwei cache 方案。source-package 必须先提取真实 read-set；
   Liwei 必须最后解决无跨批状态与缓存性能等价。
4. 每批先冻结 exact version、DataBridge generation、日历、输入 cutoff、日期、完整结果字段、canonical backtest、
   业务身份和耗时基线，再形成独立两文件交付。相同输入下逐行结果、全历史回测、请求拆分/顺序独立性和多次
   运行耗时中位数都不得退化。
5. successor 先 paused/shadow；等价证据和 Blackbox Gate 通过后，在单独授权窗口停止对应 Writer，原子化暂停
   Native Registry 并激活 successor。禁止双写、覆盖历史预测或让两个 Writer 同时服务同一业务键；旧 immutable
   release 与身份映射必须保留用于回滚。
6. 只有一批自然观察稳定且回滚窗口闭环后，才删除该批对应的 Native adapter、source runner、回测 runner、
   专属 Gate 和测试。全部批次完成后，再统一删除 Native discovery、subprocess、maintenance lifecycle、政策
   清单及文档分支，最终只保留 Blackbox V2 一套入库范式；历史 benchmark/evidence 与可执行代码分开归档。

ECS 继续独立灰度运行；Mac3 域名、Nginx、DNS、数据库 authority 和生产 Writer 保持不变。

## 统一停止条件

- 需要改变 M0 信号、方向映射或已确认的 MID/CQ/SF 语义；
- 需要新增 schema、timer、plist、常驻 scheduler、ledger、第二写入入口或隐式 fallback；
- 需要覆盖或删除 insert-only 历史预测，或把 gray live 冒充 scheduled live；
- 文档、代码、输入摘要、数据库身份、release manifest、installed unit 或现场状态与计划前提不一致；
- 出现无法由权威日历、精确版本、持久化 Gate 或现场读回消除的不确定点。

触发停止条件时保留现场和证据，向用户说明确定事实、影响和需要选择的事项，不猜测继续。
