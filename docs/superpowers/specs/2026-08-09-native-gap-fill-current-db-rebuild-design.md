# Native 单日信号补缺复用当前数据库设计

**日期：** 2026-08-09

**状态：** 已确认设计，待实施计划

**范围：** Native `signal-gap-fill` 输入重建与历史 artifact/prewarm 控制面退役

## 1. 决策摘要

Native 历史补缺不再以原始 sealed generation、历史输入文件或事后登记的 current-snapshot artifact 为运行前提。权威口径统一为：

```text
当前 active exact version
+ 当前数据库
+ 历史 predict_date 对应的 feature_date 截止
→ 重建并执行预测
```

补缺复用 Native 自然调度已有的 `run_configured_scheme()` 与 `shared.input_artifacts` 路径。输入 CSV 和 Phase-A cache 只存在于单次命令的隔离临时目录，执行结束即删除；不持久化输入内容、输入 SHA-256、generation、manifest 或 cache provenance。

人工入口保持唯一：

```bash
python -m harness signal-gap-fill --predict-date YYYY-MM-DD
```

## 2. 第一性原理边界

一次历史信号补缺只需要：

1. 当前生效的 exact 算法版本；
2. 当前权威数据库在历史 `feature_date` 截止下可见的数据；
3. 统一日历计算的 `predict_date / feature_date / target_date`；
4. 缺失业务键的 insert-only 写入。

平台不再尝试还原历史时点的数据库 vintage。数据库发生修订后，补缺使用修订后的当前权威数据。原始历史输入、事后重建 artifact 及其 SHA-256 不再构成业务事实。

`feature_date` 必须继续保留，因为它是预测的数据截止语义，不是额外审计字段。平台不再重复保存同义的 `cutoff_date`。

## 3. 统一执行数据流

```text
signal-gap-fill --predict-date
→ 扫描 active Registry 的真实缺口
→ 冻结 exact version、日期和 target keys
→ 创建命令级临时 input/cache root
→ 必要时通过正常预测入口先运行 Phase-A publisher
→ 通过正常预测入口运行缺口方案
→ 验证 scheme/version/target 和三个日期
→ 移除临时 input/cache provenance
→ 清理临时目录
→ 全部算法成功后按原子目标组 insert-only 提交
→ 重新扫描同日缺口并要求全部 SKIP_PRESENT
```

Native plan 不再查询 `t_input_generations`，也不再产生由 generation 缺失导致的 `BLOCKED_NO_GENERATION`。Blackbox gap-fill 数据流保持不变。

## 4. 正常路径复用

Native 补缺直接调用正常 `run_configured_scheme()`：

- 不传 `native_generation`；
- 不传历史补缺专用 execution mode；
- 使用 frozen plan 的历史 `predict_date`；
- 继续由各方案通过统一日历得到 `feature_date`；
- 继续由 `shared.input_artifacts` 从当前数据库构建日、周、月输入；
- 0629 source-backed 方案继续使用 Executor 已有的只读 source-runtime 数据库绑定。

平台不实现第二份数据库查询、日历或算法适配逻辑。

## 5. 临时输入与缓存

每次命令创建唯一、owner-only 的临时根目录：

```text
signal-gap-fill-<execution-id>/
├── inputs/
└── phase-a-cache/
```

Executor 只增加传递本次 input artifact root 的普通可选参数；已有 `phase_a_cache_root` 参数继续复用。未指定临时 root 时，自然调度行为保持不变。

算法成功、失败或超时后都必须清理临时目录。清理发生在 prediction commit 之前；清理失败视为本次补缺失败，prediction 零提交。

## 6. Phase-A publisher 依赖

部分 Liwei Native consumer 无权在空 cache root 中自行发布 Phase-A cache。该依赖不能被删除，但不再作为独立运维控制面存在。

补缺命令复用共享 cache contract 的唯一 publisher 关系：

```text
target 本身是 publisher
→ 正常执行 target 一次

target 是 consumer
→ 先通过正常预测入口执行唯一 publisher
→ 丢弃 publisher prediction
→ 在同一临时 cache root 中执行 target
```

publisher 只承担本次算法依赖准备：不写 prediction，不创建伪业务 run，不签发专项授权。publisher 缺失、身份漂移或执行失败时直接阻断依赖它的缺口组，整批 prediction 零提交。

Harness 不复制第二份 publisher 身份矩阵；依赖查询由现有共享 cache contract 统一提供。

## 7. 持久化边界

永久保留现有业务与运行事实：

- `run_id`；
- base scheme ID 和 exact version；
- `prediction_phase=gray_live`；
- `predict_date`、`feature_date`、`target_date`；
- tenor、horizon、预测方向、confidence；
- 模型内部得分和算法业务输出；
- 运行状态、耗时和原始失败原因。

Native gap prediction 写库前移除只与临时运行相关的 provenance：

- input artifact path 和内容 hash；
- generation ID、manifest URI/hash 和 exporter 信息；
- cache root、cache fingerprint 和 permit 信息；
- current-snapshot vintage disclaimer；
- 重复的 cutoff 字段。

不修改算法返回逻辑；该清理属于平台 L0 输出适配。

## 8. 退役范围

完整退役：

- `harness/signal_gap_native_artifact.py`；
- `shared/liwei_0616_signal_gap_prewarm.py`；
- `signal-gap-native-artifact prepare/register/prewarm-authority/prewarm`；
- `signal_gap_native_artifact_register` authorization action；
- `signal_gap_native_cache_prewarm` authorization action；
- Native `signal_gap_archived` execution mode；
- Native `signal_gap_current_snapshot` execution mode；
- Native `signal_gap_cache_prewarm` execution mode；
- cache prewarm permit/capability；
- Signal Gap Plan 的 Native generation eligibility 与 artifact verifier；
- Repository 的 `native_archived_generation` 和 `native_current_snapshot_artifact` gap authority 分支；
- 只服务上述登记流程、且静态复核后没有其它消费者的 generation registry 代码。

`shared.native_input_generation` 中仍被 DataBridge、正常调度或其它当前路径使用的通用能力不得删除，只移除本次退役造成的无用函数、常量和 import。

## 9. 失败语义

所有错误直接暴露，不重试、不切换旧版本、不切换输入来源：

- active config、exact version、Registry 或 cadence 漂移在算法前阻断；
- 日历无法计算、重复业务键或 frozen plan 漂移在算法前阻断；
- 当前数据库连接、数据完整性或输入构建失败时阻断；
- Phase-A publisher/consumer 失败时阻断；
- 返回的 scheme/version/target 或三个日期不一致时阻断；
- 临时目录清理失败时阻断；
- 任一算法失败时所有 prediction 零提交。

结构化 gap run 可以保留为 `failed`，旧失败 run 永不覆盖。内部 publisher 不创建独立业务 run；其失败原因记录到依赖它的 gap run。

提交阶段继续使用现有原子目标组与 insert-only 语义。若数据库提交发生真实部分完成，报告 `completed` 和 `remaining` 并退出非零，不自动重试。

## 10. 测试与验收

聚焦测试必须证明：

1. 没有 `t_input_generations` 行时，active Native 缺口仍为 `GRAY_LIVE_GAP`；
2. Native plan 不再查询 generation；
3. 已存在 prediction 保持 `SKIP_PRESENT`；
4. Native gap 使用正常 `run_configured_scheme()`，不传 generation 或特殊 mode；
5. 历史 `predict_date` 与临时 input/cache root 正确传递；
6. consumer 先执行唯一 publisher，publisher 输出不写库；
7. target 本身是 publisher 时不重复执行；
8. publisher、consumer、日期验证或清理失败时 prediction 零提交；
9. 普通 Native、0629 source-backed Native 和 Phase-A Native 都复用正常入口；
10. 临时路径/hash/generation/cache provenance 被移除，模型内部数值保留；
11. 成功和失败路径都不留下临时输入或缓存；
12. active version/Registry 漂移与 singleton lock 仍然有效；
13. Blackbox gap-fill 行为不变；
14. CLI 和 Authorization 不再接受退役命令/action；
15. 静态搜索不存在退役控制面的当前有效引用；
16. 全量测试无回归。

不增加历史 vintage 对比、输入 SHA-256 持久化、自动重试、cache fallback、launchd、前端或数据库 DDL 测试。

## 11. 明确非目标

- 不修改 Native 算法核心、特征、窗口、模型参数或结果映射；
- 不给方案增加新配置字段；
- 不建立新的 input snapshot、generation 或 cache 表；
- 不物理删除 `t_input_generations` 表、历史行或既有报告；
- 不修改 Blackbox DataBridge snapshot；
- 不修改 Backend、前端、plist 或 launchd；
- 不建立批次级跨方案数据库一致性快照。

各方案在命令执行期间分别读取当时的当前数据库，与自然生产执行语义一致。若未来需要跨方案批次快照，应作为输入模块独立评审，不能重新并入补缺控制面。

## 12. 完成条件

完成后，仓库中人工信号修复仍只有一个入口；Native 缺口不依赖任何历史输入或 generation；所有输入和缓存只在当前命令内临时存在；算法和日期错误直接失败；Blackbox、自然调度和历史数据库记录保持不变。
