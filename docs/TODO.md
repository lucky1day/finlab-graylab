# 统一后续推进计划

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-08

本文只列未完成工作及其授权边界。已验证事实见[当前状态](CURRENT_STATUS.md)，带日期的执行证据见
[状态记录](records/status/README.md)，生产调度规则以
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)为准。历史执行计划不定义当前顺序或权限。

## 全局边界

- 当前开发分支为 `codex/audit-bugfixes-20260613`；未来新的 `master` 合并、覆盖、推送或生产操作仍须对应授权。
- `launchd + installed plist` 是唯一生产调度控制面；仓库文件、测试和灰度补写均不能证明现场自然触发。
- 不恢复 scheduler、ledger、occurrence、epoch 或 daily-gray 生产路径；历史修复只能写 `gray_live`，不能伪装为
  `scheduled_live`。
- 未取得专项授权时，不执行 launchctl、installed plist、服务操作、业务写入、持久化回测、DDL 或手工 SQL。
- 主工作区的 `KNOWN_ISSUES` 修改、`.superpowers/`、`reports/operations/` 和诊断脚本属于用户内容，不纳入提交。

## 已关闭基线（不重开）

- **R0：生产修复发布**：开发分支与 `master` 已在 2026-08-08 推送到同一验证提交；后续发布不沿用本次授权。
- **D1：Native 日频缺口闭环**：封存输入的受控 cache 预热成功，唯一缺失键由 run `2256` 写入一条
  `gray_live`；截至 2026-08-07 的只读报告为 `expected=863`、`present=863`、`missing=0`。
- **G3.1/G4**：G3 的 8 月 3 日补写、G4 唯一键修复及既有 Blackbox admission 保持关闭，不重复补写。
- **G1/G2、G5/G6 与 repo-only legacy cleanup**：DataBridge、launchd-only single-writer、前端验收和
  ledger/occurrence/epoch runtime 退役均保持关闭。
- **D0 文档治理**：保留当前架构、SOP、正式 onboarding/状态/审计证据及仍被代码引用的 Weekly 10Y
  stable-order 设计；已完成且被现行资料替代的过程计划从工作树删除，由 Git 历史追溯。

## 当前队列

| 顺序 | 工作流 | 当前状态 | 下一步 |
|---|---|---|---|
| 1 | G7.0：Native 版本模型 | `DECISION_DRAFT_READY` | 用户确认目标语义后再制定 G7.1 实施计划 |
| 2 | G8.0：历史 schema/archive | `REPOSITORY_RUNTIME_RETIRED_DECISION_DRAFT_READY` | 用户确认保留边界后再制定 G8.1 实施计划 |

### G7.0：Native 版本模型

- 事实矩阵见 [G7.0 记录](records/status/NATIVE_VERSION_MODEL_FACT_MATRIX_G7_0_20260806.md)。
- 当前只确认 config、精确 version、Registry exposure 与 admission evidence 是不同事实；未批准新的状态迁移。
- G7.1 未获批准前，不改算法、版本、Registry 或控制面，不写业务表。

### G8.0：历史 schema/archive

- 保留矩阵见 [G8.0 记录](records/status/G8_REPLAY_LEDGER_DECISION_MATRIX_20260806.md)。
- repo runtime 已退役；物理表、installed 状态、archive 与 DDL 仍是独立决策。
- G8.1 未获批准前，不执行 launchctl、migration、DDL 或手工 SQL，也不重建已退役 runtime。

## 统一停止条件

出现以下任一情况时，停止写入并回到只读核对：

- active scope、精确 version、交易日口径、DataBridge authority、输入 cutoff 或业务日期语义发生漂移；
- 需要绕过 Gate、恢复旧调度路径、扩大原子范围或增加 fallback 才能继续；
- 工作区出现与目标文件重叠的用户修改；
- 操作需要新的数据库、launchd、installed plist、服务或发布权限，但当前授权未覆盖。
