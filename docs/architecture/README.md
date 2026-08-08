# 架构与契约索引

**文档状态**：`CURRENT`

**目标读者**：平台开发、架构评审和代码审计人员

**最后核验日期**：2026-08-03

本目录只保存长期有效的系统规则和实现边界，不记录具体方案状态或单次测试结论。

| 文档 | 权威范围 |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 系统部署、数据流、Registry、API 和双运行时执行流 |
| [PRODUCTION_SCHEDULING_GOVERNANCE.md](PRODUCTION_SCHEDULING_GOVERNANCE.md) | launchd + installed plist-only、单 writer、阶段语义、输入新鲜度和生产授权 |
| [CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md) | 分层、依赖方向、输入和写库单点 |
| [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) | Gate、授权、证据和副作用边界 |
| [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) | 双运行时共享身份、日期、结果和生命周期契约 |
| [PREDICTION_SEMANTICS.md](PREDICTION_SEMANTICS.md) | 三日期、任务组合和实盘语义 |
| [SOURCE_ALGORITHM_FIDELITY.md](SOURCE_ALGORITHM_FIDELITY.md) | Native 与 Blackbox 的算法保真责任 |
| [BLACKBOX_V2_PLATFORM.md](BLACKBOX_V2_PLATFORM.md) | Blackbox V2 执行器、快照和结果转换架构 |
