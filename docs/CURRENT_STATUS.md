# 当前状态

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-06

本文只保留当前已验证事实；带日期的执行证据在[状态记录](records/status/README.md)。未完成工作的顺序、
并行关系和授权闸门见[统一后续推进计划](TODO.md)；生产调度规则以
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)为准；最近的 G3.1 闭环证据见
[日频覆盖与治理闭环计划](records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md)。

## 当前政策

- `launchd + installed plist` 是唯一生产调度控制面；仓库代码/模板不单独证明生产挂载。
- 自然时钟写 `scheduled_live`；经授权、insert-only 的历史修复写 `gray_live`；两者不可互相替代。
- `ledger`、`occurrence`、`epoch`、daily-gray、常驻 APScheduler 和旧预检不再是新建或
  过渡生产路径。
- 新的 installed plist、launchctl、服务、激活、admission、业务写入、持久化回测和 DDL 都须先只读核对并取得
  独立授权。

## 已闭环的生产治理

### G3.1：日频覆盖闭环（2026-08-06）

- 2026-08-01 是非交易日，不应有日频信号。
- 五个精确 Blackbox identity 已仅获得 `launchd_one_shot` admission（commit `e830f9e`）；没有 legacy、ledger 或
  direct capability，也没有执行 launchd/plist/服务操作。
- 写前受限 plan SHA `cda60ed5dd9c604223620c46cbb269be371da78399c9b0ec2659c234d107331e` 冻结 12 个 T+1 key / 11 组；
  一次 `signal-gap-fill` Gate 以 `gray_live` insert-only 写入，runs `2156`–`2166` 全部 success。
- 写后 plan SHA `aa5e915519aa754aa9518b42231f5a5ec20b3026cdf454e76eeea351f45c8596` 为 20 个 `SKIP_PRESENT`；没有新增
  `scheduled_live`、T+5、8 月 1 日或范围外 target。
- 本次 `gray_live` 历史修复本身**不授予 scheduler admission**，也不证明 installed plist 已挂载或自然时钟已现场触发；
  五项 exact admission 是独立、仅限 `launchd_one_shot` 的仓库变更。
- 34 个 active 日频 composite scope 在 DB raw、Dashboard canonical 与 fresh served API 一致：2026-08-03/04/05
  均为 T+1 `10/10`、T+5 `24/24`。前端以 `target_date` 过滤 `2026-08` 的 live 行集为 207，未将指标“样本”
  数当作 live 行数。
- DataBridge current authority 为 `refresh_date=2026-08-06`，8 月 3/4 的 cutoff 精确匹配；12 条 provenance 为
  10 条 DataBridge current generation 和 2 条 Native current-snapshot artifact。HMAC token、DSN 与凭据未写入文档。

## 已验证的 7Y 灰度闭环与阶段摘要

- 两套 7Y v2 已完成 Gate、入库、历史 `gray_live`、served API 与前端读回；其列入 G3.1 的 exact version
  现为 `launchd_one_shot` only。该事实不外推到其它 7Y identity 或控制面。
- G1/G2 的 DataBridge 与 launchd-only 单 writer、G3 的 8 月 3 日补写、G4 的 D-overlay 唯一 key、
  G5/G6 的无写库功能验收均已闭环；自然时钟继续作为非阻塞观测。
- G8.1–G8.24 的 repo-only 零消费者清理已完成；不能继续零散删除仍有消费者的 replay/ledger 闭包。

## 已闭环的前端 UX

- 因子实验室已完成 CSS 紧凑化：任务格为 `76px`、趋势图和空态为 `244px`，且不再保留矩阵 `min-height: 560px`。
- 用户确认的 `>= 60%` 视觉语义是**仅准确率数字红色**，而非任务格绿底。现有 `overall`、`upPrecision`、
  `downPrecision` 排序、有限值和阈值判断保持不变；`samples`、缺失值与 `< 60` 不高亮，hover/selected 也不退化。
- CSS 通过其精确内容 SHA-256 URL 交付，防止缓存客户端继续读取旧样式；前端行为、静态缓存和文档测试均已通过。

## 已完成的只读诊断与治理设计

- D1 的 publisher-first repository 修复已由当前分支祖先 `d440091` 和回归测试确认；事故前 consumer 位于共享 cache
  publisher 之前仍只是最高置信的条件控制面因果推断，原始 failed run-log 异常未读取。当前配置的受控只读快照未观察到
  该单键且缺精确 Native generation，因此未再次写入；这不推断其它环境的数据状态。详见
  [D1 诊断](records/status/LIWEI_10Y01_T5_0811_FAILURE_DIAGNOSIS_20260806.md)。
- G7.0 已形成 Native 26 identity / 30 composite Registry 的事实矩阵和最小目标模型草案；DB/API/installed 状态仍须
  后续单独只读核验，G7.1 等待用户确认目标语义。详见
  [G7.0 矩阵](records/status/NATIVE_VERSION_MODEL_FACT_MATRIX_G7_0_20260806.md)。
- G8.0 已形成 replay/ledger/legacy 的候选决策矩阵：isolated replay 的保留目的、历史 ledger 的 archive 策略和
  capability/legacy mode 的目标语义均等待用户确认。019 inspection 仅限 `APPLYING` recovery；在新的受控只读
  inventory capability 设计并获授权前，forward DDL 继续阻断。详见
  [G8.0 矩阵](records/status/G8_REPLAY_LEDGER_DECISION_MATRIX_20260806.md)。
- D0 已按用户确认完成文档清理：仅删除 2 份 superseded front-end cutover 草案，G3.1 与当前规范/证据均保留；此事不改变
  D1、G7.0 或 G8.0 的独立状态。详见 [D0 审计](records/status/D0_DOCUMENT_LIFECYCLE_AUDIT_20260806.md)。

## 未完成的生产治理

- G7.1：Native 版本模型收敛等待用户确认 G7.0 目标语义；确认前没有授权的代码、数据库或控制面动作。
- G8.1：最终退役等待用户确认 replay/recovery、legacy mode 和历史 ledger 数据保留；任何 installed plist 或
  数据库迁移仍须独立设计与授权。
- D1 的原始异常读取或任何当前配置快照未见的 2026-08-11 T+5 recovery 均须独立的只读/业务写入 scope；完整排序和边界以
  [统一后续推进计划](TODO.md)为准。
