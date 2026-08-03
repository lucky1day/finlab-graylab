# 已废止的日频 Ledger SLA

**文档状态**：`HISTORICAL`

**最后核验日期**：2026-08-03

本文件曾描述以 ledger、occurrence、epoch 和日频 coordinator 为中心的日频目标。该模型
不再是本项目的生产方向，不能作为安装、迁移、调度、验收或回滚操作说明。

当前生产规则请使用
[生产信号与调度治理](PRODUCTION_SCHEDULING_GOVERNANCE.md)：`launchd + installed plist`
是唯一生产调度控制面；每个 cadence 只有一个生产 writer；自然写入与历史 `gray_live`
修复必须分开；输入不新鲜时 fail-closed。

需要理解旧 ledger 设计、cache 假设或当时的验证记录时，请通过 Git 历史或带日期的
[状态记录](../records/status/README.md)追溯。不得据此新建 ledger、occurrence、epoch、
daily-gray 或并行 APScheduler 生产路径。
