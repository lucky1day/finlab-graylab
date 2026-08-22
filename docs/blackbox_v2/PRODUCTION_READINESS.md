# Blackbox V2 生产准备清单

**文档状态**：`CURRENT`

**目标读者**：平台负责人、运维和生产授权人员

**最后核验日期**：2026-08-10

本文只定义单个 Blackbox exact version 进入生产前必须满足的条件，不记录具体方案、历史 rollout、运行数量或一次性证据。操作步骤见[平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)，自然调度见[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。

## 必须满足

1. 两文件 Intake、Metadata、目录身份、Contract 和 Runtime Profile 全部通过。
2. 当前 exact version 的 Harness `all` 全部通过：Blackbox V2 为四段
   `static → input → unit → compare`（dry-run 与 no-persist backtest 的断言已由同一 runtime 的
   CompareGate 更强覆盖，故不再重复执行）。报告中的 version、输入 snapshot、Request、cutoff 和
   结果身份精确一致。技术 `all` 不访问 Backend。
3. DataBridge current snapshot 通过 schema、freshness、cutoff 和完整性校验；平台注册输入由调用方只读数据库连接捕获，不存在 Native 二级 generation。
4. 入库 StaticGate、运行后输入目录指纹复验、超时、环境 allowlist、确定性、顺序/分批一致性、未来数据隔离和严格 `-1/0/1` Result 均通过。
5. activation、持久化回测和 live 分别使用精确的一次性授权；其它方案、旧版本或旧 run 的授权不得外推。
6. activation 后 config、exact version 与全部 Registry target 均为 active；paused、draft、retired 或 cadence 不匹配的身份不得进入对应 one-shot runner。
7. 激活后的 HTTP 验收只使用 `DashboardGate` 检查 `/api/factor-lab/dashboard` 当前业务可见性；Dashboard 响应不携带 exact version，不能替代 exact version、Gate 或生命周期证据。
8. 自然生产观察必须由 installed plist、loaded state、日志、run、prediction、API/Dashboard 相互一致证明；仓库模板和测试不替代现场证据。

## 生命周期异常

- 任一 pending lifecycle journal 都必须阻断新的 lifecycle 动作，不允许激活、shadow 或 revision 路径隐式恢复。
- 只有显式 `blackbox_reconcile` HMAC 授权可以把身份恢复到 journal 记录的 previous safe state；原 journal 保持不变，并新增 linked reconciliation journal 记录恢复结果。

## 禁止替代

- 不得用另一方案的 Gate、历史 admission、旧版本、旧 snapshot 或前端显示替代当前 exact version 的证据。
- 不得恢复 Backend trigger、direct scheduling、Admission capability、ledger、occurrence、epoch、常驻 scheduler 或 per-scheme cron。
- 输入、算法或生命周期异常必须直接失败并保留证据，不自动 fallback、重试、切换旧版本或覆盖业务键。
- 本清单通过不授权修改 installed plist、执行 launchctl、重启服务、写业务表或应用 DDL；这些操作仍需单独授权。
