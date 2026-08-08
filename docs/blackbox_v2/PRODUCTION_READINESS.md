# Blackbox V2 生产晋级条件

**文档状态**：`BLOCKED_DRAFT`

**目标读者**：平台负责人、运维和生产授权审批人员

**最后核验日期**：2026-08-02

本文定义“任意后续 Blackbox V2 交付可走标准生产流程”之前仍需完成的广义平台
条件。它不撤销已经取得的逐方案专项授权，也不为未授权 identity 自动放行。
日频运行以[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)为当前契约。

## 1. 当前边界

当前已具备并完成隔离认证：

- 两文件 Intake、Metadata、目录身份和 Runtime Profile 校验；
- DataBridge 三频同代父快照、freshness、Snapshot identity 和 Request；
- 七个自动 Gate、重复运行、no-persist 回测和零写库 check-only；
- shadow/draft 登记、ActivationGate、完整历史持久化、gray live、actual join、
  API/前端探针和失败恢复；
- sandbox 文件 allowlist、环境清理、严格整数 Result 和 stale generation 拒绝；
- 日频目标代码路径支持单 coordinator、单 occurrence、17 Native + 8 V2、29/29
  target receipt 和 V2 最大并发 2；production 仍为 migration 017 / legacy，
  7 个 production Liwei family 的 schema 3 cache bootstrap 与同 authority 7/7
  warm hit 已完成，但 ledger、migration 018 与 epoch cutover 尚未执行。

当前仍不能形成面向任意新交付的通用 `PRODUCTION_READY`：

- 真实交付代表性、依赖栈和日/周/月任务覆盖仍需持续扩展；
- 每个新 identity/version/runtime 仍需自己的 generation、确定性、超时、截止
  隔离、结果结构、失败恢复和标准结果证据；
- 通用责任人、暂停/回退权限和 durable operator report 尚未形成最终 SOP；
- 已有专项授权、active Registry、gray/formal 标签或另一方案的运行记录都不能
  外推为新方案授权。

## 2. 必须保持的八项能力

| 编号 | 条件 | 当前状态 |
|---|---|---|
| PR-01 | Gate 绑定 generation/freshness，stale generation fail-closed | `PASS` |
| PR-02 | Runtime Profile 是环境、资源和权限的唯一配置源 | `PASS` |
| PR-03 | Harness 审计持久化与授权 identity fail-closed | `PASS` |
| PR-04 | shadow/draft journal、补偿、reconciliation 和并发锁完整 | `PASS` |
| PR-05 | activate、persist、gray/live 均由 Blackbox 专项门禁控制 | `PASS` |
| PR-06 | Registry、API、scheduler、actual 和前端探针一致 | `PASS` |
| PR-07 | Result 方向只接受严格整数 `-1/0/1` | `PASS` |
| PR-08 | sandbox 读取、网络、环境变量和数据目录权限收紧 | `PASS` |

八项 PASS 说明生产路径能力存在，不等于具体方案已获生产授权。

## 3. 标准生产 SOP 形成条件

1. 至少三个独立真实上游交付批次覆盖日、周、月，并对每批重复 Intake、Gate、
   历史、live、actual、API、前端和恢复矩阵。
2. 不同依赖栈必须在版本化 Runtime Profile 中可重复运行，不临时安装依赖。
3. 业务、平台和运维明确通用激活、暂停、纠错、回退和审计责任人。
4. 将标准操作文档标记为 `CURRENT`；在此之前只接受逐方案专项授权。
5. 日频方案必须进入同一 25/29 occurrence，绑定当天 DataBridge generation、
   execution envelope、run fence 和 target receipt；不得恢复 per-scheme cron。
6. 每个生产日保存 occurrence、Registry/code/config digest、Native/DataBridge
   generation、8 个 V2 release/start/accepted 时间、29 target receipt 和
   write-once SLA 结果。
7. storage/input/cache 必须通过 owner/mode/inode、非 symlink、canonical path、
   manifest/payload hash 和 cutoff 验证；Liwei consumer 必须零写。

本文件保持 `BLOCKED_DRAFT`，直到“任意新交付的标准授权流程”形成。该状态不是
日频运行的并发或性能门禁，也不改变已有专项授权。

## 4. 每次复核记录

新的复核证据由 Harness 控制面与本机 ignored reports 保存。本文件只更新当前条件，
不追加运行 ID、generation ID、数据库计数或旧控制面结论。
