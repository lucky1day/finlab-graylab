# G3 All-Active Gap Planning Design

**Goal:** 让缺口规划器以当前所有 `status='active'` 的方案为全集，按日频、周频和月频各自的交易日历枚举应有信号；不再因旧数量快照停止。

## 已确认的问题

`harness.signal_gap_plan` 在生成任何 expected case 前，要求全部 active Registry 恰好为 44 个 target、40 个 execution（daily=29、weekly=7、monthly=8）。2026-08-04 的只读一致性快照为 53 个 target、50 个 execution（daily=32、weekly=13、monthly=8），所以工具以 `ACTIVE_REGISTRY_SCOPE_DRIFT` 停止，无法枚举真实缺口。

此前把 active 与“允许补数”分开是为了隔离 7Y 灰度方案；用户现已明确要求：**全部 active 方案都需要补齐**。因此 7Y 与所有其它 active 方案都应进入规划全集，但必须按各自 cadence 的日期语义处理，不能把周频/月频错误地写成 2026-08-03 的日频记录。

项目当前的 `scheme_version` 由代码与配置 hash 推导；因此要让真实补数在代码改动后免激活，必须改造全局版本生命周期、记录标记与 repository 校验，超出本阶段的最小边界。本阶段保留现有版本与 hash 约束。

## 最小方案

只修改现有 `signal-gap-plan` 的一个旧阻断条件：

1. 删除 44/40/daily=29/weekly=7/monthly=8 的静态数量守卫。
2. 当 active Registry 为空时，以 `NO_ACTIVE_REGISTRY_TARGETS` 明确 fail-closed，避免空集合被误报为 READY。

不新增 scope 文件、manifest、CLI 参数、通用框架、数据库表、scheduler、ledger 或新 writer。现有 CLI 仍是：

```bash
python -m harness signal-gap-plan --start YYYY-MM-DD --as-of YYYY-MM-DD
```

它已经会按 Registry `frequency` 使用 daily/weekly/monthly context builder；改动后只需让这条现有路径接受当前的全量 active 集合。

## 保留的 fail-closed 边界

“不锁 hash”不等于允许未知方案或错误日期进入补数：

- 只读取 `t_scheme_registry.status='active'` 的 composite target；paused/archived 永不纳入。
- 每个 target 仍须有 discovery identity、匹配的 `scheme_version`、匹配的 runtime type、合法 task type/horizon/tenor/frequency，且版本基数必须成立。
- daily 只按日频 context、weekly 只按周频 context、monthly 只按月频 context 建 case；G3、G4、G5 的实际补数仍分别按其授权和日期窗口执行。
- 日期、业务键、输入 authority、actual/prediction contract、重复记录和显式授权仍由既有 gates/repository 检查。
- `scheme_version`、`code_hash`、`config_hash`、source package、输入 artifact 与数据截止的既有校验均保持不变；代码改动后的真实补数仍需遵循既有版本激活与授权流程。

## 结果与阶段边界

规划输出继续报告实时 active target/execution/frequency 计数，并生成覆盖全部 active 方案的 action 集。新增或停用方案会自然反映在下一次只读 plan 中，而不是被旧数量常量拒绝。

本阶段不执行 DataBridge publish、Registry 激活、`gray_live`/`scheduled_live` 写入、installed plist 变更、`launchctl` 或服务重启。规划结果只是后续 G3（日频）、G4（周频）和 G5（周/月频）在专项授权下补数的事实输入。

## 验收与测试

实现必须用 TDD 覆盖：

1. 53-target/50-execution 的 active snapshot 可以规划，并正确报告 daily=32、weekly=13、monthly=8。
2. 两套 active 7Y V2 与其它 active daily/weekly/monthly 方案均进入对应 cadence 的 expected case；paused/archived 不进入。
3. active 集合增加或减少时，plan 反映新集合，不再因数量变化阻断。
4. 空 active Registry 以 `NO_ACTIVE_REGISTRY_TARGETS` fail-closed，而不是返回 READY 的空计划。
5. discovery identity 缺失、scheme_version/runtime/task/horizon/tenor/frequency/hash 不匹配、非法日期语义、输入 authority 或观测 contract 不成立时，仍在写入前 fail-closed。

完成本地实现、独立审查和完整相关回归后，G3 的开发闭环单独提交。真实缺口补数、生产 DataBridge 恢复与调度挂载仍需后续专项授权。
