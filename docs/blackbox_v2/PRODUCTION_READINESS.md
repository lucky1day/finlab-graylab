# Blackbox V2 生产准备清单

**文档状态**：`CURRENT`

**目标读者**：单人平台维护者和运维操作者

**最后核验日期**：2026-08-23

本文只定义单个 Blackbox exact version 进入生产前必须满足的条件，不记录具体方案、历史 rollout、运行数量或一次性证据。操作步骤见[平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)，自然调度见[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。

## 必须满足

1. 两文件 Intake、Metadata、目录身份、Contract 和 Runtime Profile 全部通过。
2. 当前 exact version 的 Harness `all` 全部通过：Blackbox V2 为四段
   `static → input → unit → compare`（原 dry-run 与 CompareGate 的 baseline 是同一次 predict，
   已并入）。报告中的 version、输入 snapshot、Request、cutoff 和结果身份精确一致。
   技术 `all` 不访问 Backend。
3. DataBridge current snapshot 通过 schema、freshness、cutoff 和完整性校验；平台注册输入由调用方只读数据库连接捕获，不存在 Native 二级 generation。
4. 入库 StaticGate、运行后输入目录指纹复验、超时、环境 allowlist 和严格 `-1/0/1` Result 均通过。
   确定性、顺序/分批一致性与未来数据隔离**不在平台验收范围内**——它们是交付代码自身的性质，
   由上游按 [上游交付契约](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md) 保证；生产准备核验
   不得据此宣称平台已验证过这些性质。
5. activation、持久化回测等直接副作用命令自动绑定 scheme、exact version、相关 Harness run、日期/起点与非秘密 operator；单日 `signal-gap-fill` 则以命令执行权表达本次补缺授权，由 planner 绑定当前 active identity、业务键和输入 authority，不接收 operator 或 DirectOperation scope。两类授权都不得外推到其它方案或操作，也不生成密钥/token。正式 `scheduled_live` 只由目标主机已安装的 one-shot 调度触发。
6. activation 后 config、exact version 与全部 Registry target 均为 active；paused、draft、retired 或 cadence 不匹配的身份不得进入对应 one-shot runner。
7. 激活后的 HTTP 验收只使用 `DashboardGate` 检查 `/api/factor-lab/dashboard` 当前业务可见性；Dashboard 响应不携带 exact version，不能替代 exact version、Gate 或生命周期证据。
8. 自然生产观察必须由 installed plist、loaded state、日志、run、prediction、API/Dashboard 相互一致证明；仓库模板和测试不替代现场证据。
9. `monthly_average`、`quarterly_average`、`annual_average` 方案进入目标环境前，必须先确认 migration 020
   已由受控迁移入口应用且 closed-world schema 校验通过，再部署会读取周期 actual 表的 Backend。其自然运行
   只允许复用 installed close-period 控制面；不得为三种任务分别增加 timer，或把仓库每日 18:00 模板当成
   installed/loaded 证明。
10. 若完整区间使用等价一次性 batch，必须在计算前冻结 `gray_target_start`、exact version、输入身份、lineage
    和应有 Request 集；通过逐 Request cutoff 与批内等价证据后，只计算一次并按 `target_date` 分流。历史段写
    新 immutable canonical backtest，gray 段只通过 repository insert-only 物化并重新生成 live
    `predict_date`；已有键整组拒绝，旧 run 保留审计。不能证明 live-safe 等价时不得复用。

## 生命周期异常

- 任一 pending lifecycle journal 都必须阻断新的 lifecycle 动作，不允许激活、shadow 或 revision 路径隐式恢复。
- 只有独立执行 `gate lifecycle-reconcile` 命令才可以把身份恢复到 journal 记录的 previous safe state；原 journal 保持不变，并新增 linked reconciliation journal 记录恢复结果。

## 禁止替代

- 不得用另一方案的 Gate、历史 admission、旧版本、旧 snapshot 或前端显示替代当前 exact version 的证据。
- 不得恢复 Backend trigger、direct scheduling、Admission capability、ledger、occurrence、epoch、常驻 scheduler 或 per-scheme cron。
- 输入、算法或生命周期异常必须直接失败并保留证据，不自动 fallback、重试、切换旧版本或覆盖业务键。
- 本清单通过不授权修改 installed plist、执行 launchctl、重启服务、写业务表或应用 DDL；这些操作仍需单独授权。
