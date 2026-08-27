# Native 当日单日补缺增量缓存设计

**文档状态**：`CURRENT`

**实现状态**：`IMPLEMENTED_LOCAL`（尚未发布生产 release）

**适用场景**：权威输入已就绪，需要补齐一个 `predict_date` 下尚未写入的 active 方案预测

**不适用场景**：历史修订、跨多日回放、方案版本变化、缓存损坏或需要重建历史缓存

**最后核验日期**：2026-08-27

## 1. 结论

Native 单日 `signal-gap-fill` 只做三件事：

1. 复用自然调度使用的持久化 Phase-A cache，只允许命中或追加一个尾部日期。
2. 每个 `base_scheme_id` 成功后立即通过现有 repository 原子提交；其他方案失败不丢弃成功结果。
3. 协调器中断时终止正在运行的算法进程，并关闭尚未完成的 run。

不新增结果缓存、数据库表、审计字段、报告 schema、命令、服务、Gate 或第二套 repository。
算法文件、模型参数、结果合同、并发上限和区间 gap-fill 行为均不改变。

性能目标是在 cache 可安全增量、输入已就绪的前提下，将 10 个 Liwei Native 方案控制在 35 分钟内，
当天全部 active 方案控制在 45 分钟内。是否继续优化必须以这一阶段的真实耗时为依据。

## 2. 原有冗余

原单日链路把两种不同状态错误地绑定在一起：

```text
每方案临时 runtime root
  ├─ 临时 Native 输入
  └─ 临时 Phase-A cache + private_build
       ├─ 完整历史训练
       ├─ cold/cached/uncached 对比
       └─ 命令结束后删除
```

结果是每个 Liwei 方案分别重建共享的历史训练结果。任一方案超时后，Harness 又将全部方案标记失败，
下一次从头执行所有方案。

优化后，临时 root 只隔离 Native 输入；Phase-A cache 使用自然调度的持久化 root。数据库中已存在的
immutable prediction 业务键直接表示该方案已经完成，不再建立第二套完成状态。

## 3. 单日执行流程

```text
规划当前日期缺失业务键
  → 创建现有 scheme run
  → 执行 Blackbox
       └─ 每个成功方案独立提交
  → 执行 Native publisher 批次（最多 2 worker）
       ├─ cache hit：不训练
       └─ append：每 family 追加一个尾部日期
  → 执行其他 Native 批次（最多 2 worker）
       └─ consumer 只读命中 publisher 已发布 generation
  → 每个完成的 future 立即校验并独立提交
  → 对本次全部业务键做一次权威读回
```

publisher 继续由既有 `APPROVED_PHASE_A_CACHE_PUBLISHERS` 定义，不新增 family/consumer 映射。
所有 publisher 批次结束后才开始其他 Native。publisher 失败不会触发私有 cache 或完整训练；依赖旧
cache 无法命中的 consumer 按现有合同失败，其他独立方案继续运行。

单独指定 consumer 时，Harness 不扩大授权去额外运行 publisher。如果现有 generation 未覆盖请求，
consumer 直接失败。

## 4. `incremental_only` 策略

`incremental_only` 是现有 cache mutation policy 的一个内部取值，只由单日 gap-fill 显式传入。
公共 CLI 不增加参数，其他执行路径未传入时保持原有行为。

### 4.1 允许行为

- 现有 generation 已覆盖请求日期：直接 `hit`，不调用 `train_missing`。
- 现有 build decision 为 `append`，且所有 baseline 的缺失日期并集为空或只有一个日期：沿用现有
  family lock、staging、完整性校验和 `current.json` 原子切换。
- 输入被安全截断但现有 generation 已覆盖请求：沿用现有 truncated hit。

如果确实缺少一个日期，该日期必须严格晚于现有 cache watermark。每个 family 仍只有已批准的
publisher 能够发布 generation，consumer 始终只读。

### 4.2 禁止行为

现有 cache build decision 不是 `append` 时，在首次调用训练函数前失败，包括：

- 没有 current generation；
- `full`；
- `suffix`；
- qualification 或 migration；
- spec、baseline、ABI、lineage 或输入历史不兼容；
- generation、manifest 或 cache 内容损坏；
- 缺失多个日期；
- 需要重训 watermark 当日或更早日期；
- runtime compare callback 非空。

这些身份和完整性判断继续复用现有 cache 实现。Harness 不提前再计算一遍，不重复读取、哈希或扫描
cache。失败沿用现有算法执行错误链路，不增加专用错误码、异常类型或报告字段，也不得降级为
`private_build`。

### 4.3 Compare 边界

单日补缺不运行 cold cache、cached/uncached 完整输出或 CompareGate。结果等价验证只属于永久自动测试
或显式离线验证，不进入每次补缺执行。

## 5. 按方案提交与断点续跑

单日模式的事务边界是一个 `base_scheme_id`：

- 算法必须返回该方案全部 active target 的完整结果。
- repository 只选取本次预检确认缺失的 target。
- 同一方案多个 target 在一个事务中提交。
- prediction、run 完成和 run log 继续由现有 `complete_gray_gap_run` 负责。
- live 业务键继续 insert-only，不覆盖、不删除、不部分写入。

一个方案算法失败、结果校验失败或提交失败时，只关闭该方案的 run。其他方案继续执行。已成功提交的
execution 不得再进入批量失败清理。

最终报告沿用 `single-date-signal-gap-fill-v2`：

- 全部完成：`PASSED`。
- 部分完成：`FAILED`，使用既有 `completed` 和 `remaining`。
- 重试同一命令：planner 根据 prediction 业务键跳过已完成方案，只规划剩余缺口。

不保存算法输出文件，不创建候选表、临时结果 cache 或独立 ledger。

## 6. 中断处理

Native 子进程继续使用独立 process group。执行器在等待子进程期间检查同一次 gap-fill 的取消信号：

- Ctrl-C 或协调器异常后停止继续启动任务；
- 取消尚未开始的 future；
- 正在运行的 worker 使用现有 `scheduler.process_control` 终止并确认进程组消失；
- 已提交方案保持成功；
- 尚未提交的 run 使用现有失败接口关闭；
- 不留下 `running` run 或孤儿 conda/算法进程。

不增加取消服务、后台守护进程或任务状态表。

## 7. Range 模式保持不变

本设计不改变 target range gap-fill：

- 仍只支持现有允许的 Blackbox 范围；
- 仍使用现有 replay session 和 batch executor；
- 仍在算法全部成功后调用 `complete_gray_gap_runs_atomic`；
- 区间内任一业务键冲突仍整段拒绝；
- 整个区间仍在同一事务中提交。

按方案断点续跑只作用于单日多方案补缺。

## 8. 实现范围

必要改动只在既有实现中完成：

| 位置 | 改动 |
|---|---|
| `harness/signal_gap_fill.py` | 单日按方案完成、publisher 两批执行、失败隔离和中断收口 |
| `scheduler/executor.py` | 临时输入与 Phase-A cache 解耦，传递内部策略和取消信号 |
| `shared/liwei_0616_cache_contract.py` | 增加 `incremental_only` 枚举值 |
| `shared/liwei_0616_phase_a_cache.py` | 在既有 build decision 后、训练前限制为 hit/单日 append |

不修改 `schemes/*/core/`、数据库 migration、Registry、日期语义、launchd plist、systemd unit、
Dashboard 或 repository 实现。

## 9. 验收标准

### 9.1 正确性

- cache 完整覆盖时 publisher/consumer 的训练次数为 0。
- 只缺尾部日期时每 family 只有 publisher 追加一次，consumer 不训练。
- `full/suffix/no-current/历史修订/多日缺失` 均在历史训练前失败。
- runtime compare callback 调用次数为 0。
- 一个方案失败时其他成功方案已经提交。
- 第二次规划只返回上次未完成的方案。
- 单方案多 target 仍原子提交，注入提交失败时没有部分 prediction。
- Blackbox 失败不阻止 Native，Native 失败不回滚已完成 Blackbox。
- 中断后没有孤儿进程或 `running` run。
- 优化前后 prediction 的方向、confidence、三个日期、scheme version 和原有必要 extra 逐字段一致。
- target range 的执行和原子提交行为不变。

### 9.2 性能

- 不再为每个 Liwei 方案构建私有完整 Phase-A cache。
- 每个 family 最多发生一次尾部追加。
- consumer 的 Phase-A 训练次数为 0。
- 重试时已完成方案的算法启动次数为 0。
- 10 个 Liwei Native 方案不超过 35 分钟。
- 当天全部 active 方案不超过 45 分钟。

达到目标后不继续增加共享输入或更高并发。

## 10. 暂不处理的输入重复

10 个 Liwei 方案当前使用相同的日、周、月输入参数，因此仍存在 30 次 DB 构建、CSV 写入和读回。
跨子进程复用这些输入需要定义新的 job-scoped 输入生命周期、并发创建和 source database 身份，复杂度
明显高于本轮三项改动。

本轮先消除小时级 Phase-A 重训并实测。只有在达到正确性要求后，输入物化仍是超过目标的主要耗时，
才单独评估最小共享输入方案；不能预先引入第二层 cache。

## 11. 当前历史修订边界

如果源数据修订了现有 cache watermark 当日或更早的数据，现有 build decision 会得到 `suffix` 或
`full`。单日 gap-fill 必须立即失败：

- 不自动重建；
- 不回退私有 cache；
- 不继续重复训练全部方案；
- 持久化 cache 恢复作为独立受控操作处理。

相关总纲：[代码架构](CODE_ARCHITECTURE.md)、[Harness 架构](HARNESS_ARCHITECTURE.md)、
[预测日期与实盘阶段语义](PREDICTION_SEMANTICS.md)。
