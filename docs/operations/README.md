# 运维文档索引

**文档状态**：`CURRENT`

**目标读者**：平台运维和部署人员

**最后核验日期**：2026-08-23

| 文档 | 状态 | 用途 |
|---|---|---|
| [PUBLIC_FACTOR_LAB_PERFORMANCE.md](PUBLIC_FACTOR_LAB_PERFORMANCE.md) | `CURRENT` | 公网 dashboard 的 API 性能验收和故障处理边界 |

当前双主机事实与精确 release 身份见[当前状态](../CURRENT_STATUS.md)，调度和生产切换边界见
[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)，immutable release、launchd、
systemd 和现场核验入口见 [`deploy/README.md`](../../deploy/README.md)。迁移实施和逐次发布证据通过 Git、
目标机 journal 与数据库审计追溯，不在本目录维护重复交接文档。
