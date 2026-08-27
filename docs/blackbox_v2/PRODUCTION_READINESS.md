# Blackbox V2 生产准备清单

**文档状态**：`CURRENT`

**目标读者**：单人平台维护者和运维操作者

本文只定义单个 Blackbox exact version 进入生产前必须满足的条件，不记录具体方案、历史 rollout、运行数量或一次性证据。操作步骤见[平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)，自然调度见[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。

## 必须满足

1. 新 ID 的两文件 Intake 已通过；同 ID 修订的 canonical 两文件已在持久化回测前通过 Metadata、目录身份、Contract、Runtime Profile 和脚本安全边界校验。activation 严格加载 canonical 当前字节，并只接受同 exact version、同当前脚本校验策略的成功持久化回测证据。
2. 当前 exact version 已完成一次完整持久化回测，回测 durable summary 中的
   version/code/config/manifest、脚本校验策略摘要、Runtime Profile、环境指纹、generation 和 snapshot 精确一致。校验策略变化后，旧回测不再能直接用于激活。
3. DataBridge producer 独立完成四文件 generation 的 schema、freshness、cutoff、完整性校验和 ready Snapshot 构建；方案只读取已有 receipt 并使用对应只读版本，不触发构建、修复、哈希或 CSV 复核。私有运行视图只做 producer seal 核对、稳定复制和进程前后篡改检查。启用新版 receipt 的 release 后，目标环境必须先由该 release 完成一次 DataBridge publish 并写出 ready gate，旧 receipt 不自动升级；在此之前 Harness 与 scheduler 均 fail-closed。旧三文件 current 只允许由 producer 在 identity/manifest 校验后作为一次升级 continuity 基线，下一次原子发布必须恢复严格四文件；普通消费者仍拒绝三文件 current。
4. canonical 两文件安全静态边界、回测运行后输入目录指纹复验、超时、环境 allowlist 和严格 `-1/0/1` Result 均通过。
   确定性、顺序/分批一致性与未来数据隔离**不在平台验收范围内**——它们是交付代码自身的性质，
   由上游按 [上游交付契约](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md) 保证；生产准备核验
   不得据此宣称平台已验证过这些性质。
5. activation 和持久化回测绑定 scheme、exact version、日期/起点与 operator；activation 只读取匹配的成功回测，不绑定 Harness run。单日 `signal-gap-fill` 仍由 planner 绑定 active identity、业务键和输入 authority。
6. activation 后 config、exact version 与全部 Registry target 均为 active；paused、draft、retired 或 cadence 不匹配的身份不得进入对应 one-shot runner。
7. 激活后的 HTTP 验收只使用 `DashboardGate` 检查 `/api/factor-lab/dashboard` 当前业务可见性；Dashboard 响应不携带 exact version，不能替代 exact version、Gate 或生命周期证据。
8. 自然生产观察必须由 installed plist、loaded state、日志、run、prediction、API/Dashboard 相互一致证明；仓库模板和测试不替代现场证据。
9. `monthly_average`、`quarterly_average`、`annual_average` 方案进入目标环境前，必须先确认 migration 020
   已由受控迁移入口应用且 closed-world schema 校验通过，再部署会读取周期 actual 表的 Backend。其自然运行
   只允许复用 installed close-period 控制面；不得为三种任务分别增加 timer，或把仓库每日 18:00 模板当成
   installed/loaded 证明。
10. 历史回测与灰度实盘分别执行一次 batch。灰度 target 区间必须在计算前冻结 exact version、输入身份、
    lineage 和完整 Request 集；通过逐 Request cutoff 与 producer-ready receipt 身份核验后，一个方案只物化
    一次私有运行视图并启动一个算法 batch，再通过 repository 原子 insert-only 物化。已有键整组拒绝；不能证明 live-safe
    等价时不得使用区间批量。

## 生命周期异常

- 任一 pending lifecycle journal 都必须阻断新的 lifecycle 动作，不允许激活或 revision 路径隐式恢复。
- 只有独立执行 `gate lifecycle-reconcile` 命令才可以把身份恢复到 journal 记录的 previous safe state；原 journal 保持不变，并新增 linked reconciliation journal 记录恢复结果。

## 禁止替代

- 不得用另一方案的 Gate、历史 admission、旧版本、旧 snapshot 或前端显示替代当前 exact version 的证据。
- 不得恢复 Backend trigger、direct scheduling、Admission capability、ledger、occurrence、epoch、常驻 scheduler 或 per-scheme cron。
- 输入、算法或生命周期异常必须直接失败并保留证据，不自动 fallback、重试、切换旧版本或覆盖业务键。
- 本清单通过不授权修改 installed plist、执行 launchctl、重启服务、写业务表或应用 DDL；这些操作仍需单独授权。
