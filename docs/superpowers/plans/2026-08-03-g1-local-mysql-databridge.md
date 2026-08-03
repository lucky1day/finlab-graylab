# G1 本机 MySQL DataBridge 实施计划

**目标**：用本机 MySQL 的只读、Repeatable Read 一致性快照构造 DataBridge 三频 artifact，保留双轮稳定、连续性、原子发布与 fail-closed 语义；不执行真实发布、不替换 installed plist、不重启任何服务。

## 任务 1：先固定截止与来源契约（TDD）

- [x] 为 `validate_dataset` 增加日频 `feature_date` 上界测试：晚于截止的 daily row 必须拒绝；此前只检查“过早”的路径保留。
- [x] 新建 MySQL exporter 单元测试，证明每一轮只使用一个 `REPEATABLE READ`、`READ ONLY` consistent snapshot，九张实际依赖表（六张因子表、metadata、两张日历）不可读时 fail-closed，并复用 `shared.data_service` 生成日/周/月输出。
- [x] weekly/monthly 在 SQL 与输出构造两层均按源 `rdate <= feature_date` 过滤，不按 period label 简单比较；state/atomic marker 封存 feature cutoff 与不含敏感信息的 source snapshot provenance。

## 任务 2：实现 MySQL round source 与通用 refresh 编排

- [x] 新增 `shared/data_bridge/mysql_exporter.py`：在单个 caller-owned connection 内检查来源、调用 `shared.data_service`、获取 `shared.data_contract` 水位证据并返回标准三频 DataFrame。
- [x] 将 `run_full_refresh` 接受的 round builder 抽象为可替换源；HTTP 路径保留为 legacy compatibility，MySQL 路径不依赖 HTTP client、ledger 或 scheduler。
- [x] 保持连续两轮 output digest 与 MySQL source token 稳定、previous current 连续性、锁、候选目录和原子发布；state 使用 `source_mode=local_mysql`，并由 v2 current marker 保存 selected round 的 cutoff/provenance。v1 current 只能用于连续性基线，不能满足本机 freshness read。

## 任务 3：把 direct CLI 与 V2 ready gate 接到新源（TDD）

- [x] 改写 `scripts/refresh_data_bridge_current.py`，不再 import `scheduler.main` 或 `DataBridgeClient`；用共享日历和本机 DB 计算 feature date、建立 MySQL exporter、读回 current。
- [x] 成功 publish 后严格读回 current 并写 ready gate；失败写 blocked gate；不会 kickstart/restart scheduler。
- [x] 保留 `--check-only` stale fail-closed，输出不含 DB 凭据或 DSN。

## 任务 4：验证与授权边界

- [x] 已运行 DataBridge、CLI、V2 Gate、输入 generation、data-contract 和相关 scheduler 单元测试（三组回归命令分别为 203、139、25 项；后两组与首组有重叠）；静态检查确认 direct CLI 不 import `scheduler.main` 或 HTTP client。
- [x] 新增仓库 one-shot plist 与 installed/loaded 变更留到 G2 的独立开发和生产授权窗口；真实 `--publish`、installed plist、`launchctl`、scheduler 重启均不在本阶段代码验证内。

## 已知边界

- `SourceCommitEvidence` 记录的是同一一致性快照、`rdate <= feature_date`、可用的 `create_time`/metadata 水位；现有六张因子表的通用证据不声称能够检测 `create_time` 不变的原地更新。没有经过字段能力审计前，不把该未证明性质写成已保证的 refresh 合同。
- 这份计划只记录开发验证。首次真实 `--publish`、installed plist 变更与 launchd 触发仍是 G2 的独立生产授权事项。
